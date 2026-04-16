"""General-purpose web agent: one-action-per-turn loop with LLM.

WebAgent.run(page, objective) drives the agent loop:
snapshot → LLM call (one tool) → execute → check completion → repeat.
No challenge-specific knowledge. Receives objective + completion check from caller.

Also contains shared LLM client, conversation helpers, and anti-stuck detection.
"""

import os
import re
import json
import time
import logging
import uuid
from openai import OpenAI
from dotenv import load_dotenv

from .perception import (
    serialize_dom_v2, compress_snapshot, compute_diff_markers,
    generate_page_summary, compute_diff_sections,
)
from .prompts import SYSTEM_PROMPT, get_tools_for_snapshot
from .actions import execute_tool

load_dotenv()
logger = logging.getLogger(__name__)

# LLM Configuration
DEFAULT_MODEL = "llama-3.3-70b"
REASONING_EFFORT = ""
LLM_TEMPERATURE = 0.0
LLM_TIMEOUT = 30.0
LLM_INFERENCE_TIMEOUT = 3.0  # Per-call ceiling; gpt-oss is fast on Cerebras
MAX_RETRIES = 5

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            base_url="https://api.cerebras.ai/v1",
            api_key=os.getenv("CEREBRAS_API_KEY"),
            timeout=LLM_TIMEOUT,
        )
    return _client


def _make_synthetic_message(tool_name, params):
    """Build a synthetic OpenAI-style message object from a parsed tool call.

    Used when Cerebras returns tool_use_failed but includes a parseable
    failed_generation string.
    """
    synthetic_id = f"synth_{uuid.uuid4().hex[:8]}"

    class _Func:
        pass
    func = _Func()
    func.name = tool_name
    func.arguments = json.dumps(params)

    class _TC:
        pass
    tc = _TC()
    tc.id = synthetic_id
    tc.type = "function"
    tc.function = func

    class _Msg:
        pass
    msg = _Msg()
    msg.content = None
    msg.tool_calls = [tc]
    return msg


def _parse_failed_generation(failed_gen):
    """Try to parse a Cerebras failed_generation string into (tool_name, params).

    Handles formats like:
        fill(ref=e16, value=5E8YN2)
        click(ref=e1)
        click(ref=e5, reason=submit, force=True)
        {"type": "function", "name": "fill", "parameters": {"ref": "e16", "value": "US3B5Y"}}
        {"name": "fill", "arguments": "{\"ref\": \"e23\", \"value\": \"YLL68R\"}"}

    Also handles multi-line output (model tries multiple tool calls at once).
    In that case, parses only the FIRST valid tool call.

    Returns (tool_name, params_dict) or None.
    """
    if not failed_gen:
        return None

    # --- Try JSON format first ---
    try:
        obj = json.loads(failed_gen)
        if isinstance(obj, dict) and "name" in obj:
            tool_name = obj["name"]
            # Format 1: {"name": "fill", "parameters": {"ref": "e16", ...}}
            if "parameters" in obj and isinstance(obj["parameters"], dict):
                return (tool_name, obj["parameters"])
            # Format 2: {"name": "fill", "arguments": "{\"ref\": ...}"}
            if "arguments" in obj:
                args = obj["arguments"]
                if isinstance(args, str):
                    args = json.loads(args)
                if isinstance(args, dict):
                    return (tool_name, args)
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    # --- Fallback: function-call style like fill(ref=e16, value=X) ---
    # Split on actual newlines or literal \n sequences (from repr'd error strings)
    lines = re.split(r'\n|\\n', failed_gen)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^(\w+)\((.+)\)$', line)
        if not m:
            continue
        tool_name = m.group(1)
        args_str = m.group(2)
        params = {}
        # Parse key=value pairs (values may contain commas inside quotes, but
        # Cerebras failed_generation is typically simple k=v, k=v)
        for part in re.findall(r'(\w+)=([^,]+?)(?:,\s*|$)', args_str):
            key, val = part
            val = val.strip().strip('"').strip("'")
            # Convert booleans
            if val.lower() == 'true':
                val = True
            elif val.lower() == 'false':
                val = False
            params[key] = val
        if params:
            return (tool_name, params)

    return None


def cerebras_call(messages, tools, max_retries=MAX_RETRIES, tool_choice="auto"):
    """Call the Cerebras LLM with retry logic.

    Features:
    - Per-call inference timeout (LLM_INFERENCE_TIMEOUT): if Cerebras takes
      longer than this, the call is aborted and retried immediately. This
      avoids 10-17s queue stalls that dominate step latency.
    - tool_use_failed recovery: if Cerebras returns 400 with a
      failed_generation field, we parse it into a synthetic tool call and
      return it instead of crashing.

    Args:
        messages: List of message dicts (role, content)
        tools: List of tool definitions in OpenAI format
        max_retries: Maximum retry attempts
        tool_choice: Tool selection mode ("auto", "required", "none")

    Returns:
        The response.choices[0].message object

    Raises:
        Exception: If all retries exhausted
    """
    client = _get_client()
    model = os.getenv("MODEL_NAME", DEFAULT_MODEL)
    inference_timeout = float(os.getenv("LLM_INFERENCE_TIMEOUT", LLM_INFERENCE_TIMEOUT))
    last_error = None

    reasoning = os.getenv("REASONING_EFFORT", REASONING_EFFORT)
    extra = {}
    if reasoning:
        extra["reasoning_effort"] = reasoning

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                temperature=float(os.getenv("LLM_TEMPERATURE", LLM_TEMPERATURE)),
                timeout=inference_timeout,
                **extra,
            )
            msg = response.choices[0].message

            # Check for empty response
            if not msg.content and not msg.tool_calls:
                logger.warning(f"Empty response on attempt {attempt + 1}/{max_retries}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise Exception("LLM returned empty response after all retries")

            return msg

        except Exception as e:
            last_error = e
            error_str = str(e)

            # Fix 2: Recover from tool_use_failed by parsing failed_generation
            if "tool_use_failed" in error_str:
                # Prefer extracting from exception body (clean Python string)
                # over regex on repr'd error_str (which double-escapes quotes)
                failed_gen = None
                if hasattr(e, 'body') and isinstance(e.body, dict):
                    failed_gen = e.body.get('failed_generation')
                if not failed_gen:
                    failed_gen = _extract_failed_generation(error_str)
                if failed_gen:
                    parsed = _parse_failed_generation(failed_gen)
                    if parsed:
                        tool_name, params = parsed
                        logger.info(
                            f"Recovered tool_use_failed: {tool_name}({params}) "
                            f"from failed_generation"
                        )
                        return _make_synthetic_message(tool_name, params)
                # Could not parse — treat as retryable (model hallucinated junk)
                logger.warning(f"tool_use_failed but could not parse: {error_str[:200]}")
                if attempt < max_retries - 1:
                    logger.info("Retrying after unparseable tool_use_failed")
                    continue

            # Check if retryable (timeout, 429, 502, 503)
            retryable = any(x in error_str.lower() for x in [
                "429", "502", "503", "timeout", "timed out",
                "empty response", "connection",
            ])

            if retryable and attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 2)  # Cap backoff at 2s
                logger.warning(
                    f"Retryable error on attempt {attempt + 1}: "
                    f"{error_str[:120]}. Waiting {wait_time}s"
                )
                time.sleep(wait_time)
            elif not retryable:
                raise

    raise Exception(f"LLM call failed after {max_retries} retries. Last error: {last_error}")


def _extract_failed_generation(error_str):
    """Extract the failed_generation value from a Cerebras error string.

    The error string looks like:
    Error code: 400 - {'message': "Failed to generate...",
    'failed_generation': 'fill(ref=e16, value=5E8YN2)'}
    """
    m = re.search(r"'failed_generation':\s*'([^']+)'", error_str)
    if m:
        return m.group(1)
    m = re.search(r'"failed_generation":\s*"([^"]+)"', error_str)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Conversation helpers
# ---------------------------------------------------------------------------

MAX_TURNS = 14
MAX_RESETS = 2  # Max fresh conversation resets when stuck
MAX_CONTINUATIONS = 0  # Fresh-agent continuations on turn exhaustion (0 = off)
MAX_CONVERSATION_CHARS = 50_000  # ~12K tokens; trim oldest exchanges beyond this

# Settle times per action type (ms). Actions that don't trigger page changes
# (fill, type, hover, press) need minimal settle. Click/scroll/select may
# trigger re-renders. Drag/draw are JS-dispatched with built-in delays.
_SETTLE_MS = {
    "fill": 50,
    "type": 50,
    "hover": 50,
    "press": 50,
    "click": 150,
    "scroll": 200,
    "select": 150,
    "wait": 0,       # wait() already sleeps internally
    "draw": 50,      # JS dispatch has built-in delays
    "drag_and_drop": 50,
    "decode_base64": 0,
}
DEFAULT_SETTLE_MS = 300

# Tools that return information without changing the page.
# After these, skip the snapshot refresh and don't count a turn —
# instead let the LLM reason about the result in the same turn.
INFORMATIONAL_TOOLS = {"decode_base64"}


def _settle_ms_for_action(tool_name: str) -> int:
    """Return adaptive settle time in ms based on action type."""
    return _SETTLE_MS.get(tool_name, DEFAULT_SETTLE_MS)


def _snapshot_changed(old: str, new: str) -> bool:
    """Content-aware change detection using line-level set diff.

    Returns True if any lines were added or removed between snapshots.
    """
    old_lines = set(old.split("\n"))
    new_lines = set(new.split("\n"))
    return old_lines != new_lines


def _describe_snapshot_change(old: str, new: str) -> str:
    """Return a short structural description of what changed between snapshots.

    Replaces the binary "page changed"/"no visible change" with quantified info
    like "+3 elements, -1 element, 2 inputs visible".
    """
    old_lines = set(old.split("\n"))
    new_lines = set(new.split("\n"))
    added = new_lines - old_lines
    removed = old_lines - new_lines

    if not added and not removed:
        return "no visible change"

    parts = []
    if added:
        parts.append(f"+{len(added)} lines")
    if removed:
        parts.append(f"-{len(removed)} lines")

    # Count structural additions: new interactive elements, inputs, buttons
    new_inputs = sum(1 for l in added if "<input" in l or "<textarea" in l or "<select" in l)
    new_buttons = sum(1 for l in added if "<button" in l or 'role="button"' in l)
    new_refs = sum(1 for l in added if "ref=e" in l)

    if new_refs:
        parts.append(f"{new_refs} new interactive elements")
    elif new_inputs:
        parts.append(f"{new_inputs} new inputs")
    elif new_buttons:
        parts.append(f"{new_buttons} new buttons")

    return ", ".join(parts)


def _action_key(tool_name: str, params: dict) -> str:
    """Extract a canonical key from a tool call for deduplication.

    Captures the semantically meaningful parts: tool name + ref/direction/key.
    Ignores ephemeral fields like 'reason' or 'force'.
    """
    ref = params.get("ref", "")
    if tool_name == "scroll":
        return f"scroll({params.get('direction', '')},{params.get('amount', '')})"
    if tool_name == "press":
        return f"press({params.get('key', '')})"
    if tool_name == "fill":
        return f"fill({ref},{params.get('value', '')})"
    if tool_name in ("drag_and_drop", "simulate_drag"):
        fr = params.get("from_ref") or params.get("from_selector", "")
        to = params.get("to_ref") or params.get("to_selector", "")
        return f"drag_and_drop({fr},{to})"
    if tool_name == "decode_base64":
        return f"decode_base64({params.get('value', '')[:20]})"
    return f"{tool_name}({ref})"


def _detect_action_repetition(action_keys: list, window: int = 6) -> str | None:
    """Detect stuck patterns in recent action keys.

    Checks for two patterns:
    A) Cycle detection — repeating sequences of length 1-3 in the last `window` actions.
       e.g. [A,B,A,B] or [A,B,C,A,B,C]
    D) Duplicate detection — same exact action appearing 3+ times in last `window` actions.

    Returns a warning string describing the pattern, or None if no repetition found.
    """
    if len(action_keys) < 3:
        return None

    recent = action_keys[-window:]

    # D) Duplicate: same action 3+ times in window
    from collections import Counter
    counts = Counter(recent)
    for action, count in counts.most_common(1):
        if count >= 3:
            return f"REPEATED ACTION: You have done '{action}' {count} times recently. This is not working. Try something completely different."

    # A) Cycle detection: check for repeating sequences of length 1-3
    for cycle_len in range(1, 4):
        if len(recent) < cycle_len * 2:
            continue
        # Check if the last cycle_len*2 actions form a repeating pattern
        tail = recent[-(cycle_len * 2):]
        first_half = tail[:cycle_len]
        second_half = tail[cycle_len:]
        if first_half == second_half:
            pattern = " → ".join(first_half)
            return f"CYCLE DETECTED: You are repeating [{pattern}] in a loop. Break the cycle — try a completely different element or strategy."

    return None


def _get_stuck_warning(consecutive_unchanged: int, turn: int) -> str:
    """Return escalating warning text when agent appears stuck."""
    if turn >= 4 and consecutive_unchanged >= 2:
        return (
            "\n\nCRITICAL: Page has not changed after multiple actions. "
            "Try a COMPLETELY different approach — different elements, "
            "different interaction pattern."
        )
    if turn >= 2 and consecutive_unchanged >= 1:
        return (
            "\n\nWARNING: Page appears unchanged. Try a different approach "
            "— click different elements, scroll, or wait."
        )
    return ""


def _serialize_assistant_msg(msg):
    """Convert OpenAI response message object to a dict for conversation history."""
    d = {"role": "assistant"}
    if msg.content:
        d["content"] = msg.content
    if msg.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return d


def _trim_messages(messages, max_chars=MAX_CONVERSATION_CHARS):
    """Trim conversation history to stay under token budget.

    Removes complete exchanges from the beginning, cutting at user-message
    boundaries so we never leave orphaned tool-result messages.
    Keeps: system prompt + most recent exchanges.
    """
    total_chars = sum(len(m.get("content", "") or "") for m in messages)
    if total_chars <= max_chars:
        return messages

    # Find safe cut points — indices where a user message starts a new exchange
    cut_points = [i for i in range(1, len(messages)) if messages[i]["role"] == "user"]

    # Try cutting from oldest exchanges forward (skip index 0 = system)
    for cut_idx in cut_points[1:]:  # cut_points[0] is the first user msg
        trimmed = [messages[0]] + messages[cut_idx:]
        chars = sum(len(m.get("content", "") or "") for m in trimmed)
        if chars <= max_chars:
            logger.info(f"Trimmed conversation: {len(messages)} -> {len(trimmed)} messages")
            return trimmed

    # Even last exchange is too long — keep system + last user message only
    last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
    if last_user:
        logger.info(f"Trimmed conversation to system + last user message")
        return [messages[0], last_user]
    return messages[:2]


def _format_action_entry(
    tool_name: str,
    params: dict,
    ref_map: dict,
    success: bool,
    error: str = "",
    data: dict | None = None,
) -> str:
    """Build a rich action history entry with element names and outcome.

    For tools that return informational data (not page interactions), the result
    is included so it persists in action history across conversation trimming.

    Examples:
        click(e1=button "Submit") → OK
        fill(e2=input, value="test@example.com") → OK
        scroll(direction=down, amount=300) → OK
        decode_base64(value=REVDT0RF...) → OK, decoded="DECODE_ME_15"
        click(e99) → FAILED: Element not found
    """
    ref = params.get("ref", "")

    # Try to resolve ref to element description
    if ref:
        # Normalize: LLM sometimes sends "46" instead of "e46"
        lookup_ref = ref
        if lookup_ref not in ref_map and not lookup_ref.startswith("e"):
            lookup_ref = f"e{lookup_ref}"

        if lookup_ref in ref_map:
            info = ref_map[lookup_ref]
            tag = info.get("tag", "")
            text = info.get("text", "")
            if text:
                elem_desc = f'{ref}={tag} "{text[:20]}"'
            else:
                elem_desc = f"{ref}={tag}"
        else:
            elem_desc = ref

        # For fill/type, also show the value
        other_params = {k: v for k, v in params.items() if k != "ref"}
        if other_params:
            param_parts = ", ".join(f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}" for k, v in other_params.items())
            desc = f"{tool_name}({elem_desc}, {param_parts})"
        else:
            desc = f"{tool_name}({elem_desc})"
    else:
        # Non-ref tools: show all params compactly
        param_parts = ", ".join(f"{k}={v}" for k, v in params.items())
        desc = f"{tool_name}({param_parts})" if param_parts else f"{tool_name}()"

    # Append outcome
    if success:
        # Include result data that adds information beyond the input params.
        # Skip "action" (redundant with tool name) and any key already in params
        # (e.g. ref, value, force — already shown in the description above).
        result_parts = []
        if data:
            for k, v in data.items():
                if k == "action" or k in params:
                    continue
                if isinstance(v, str) and len(v) > 0:
                    result_parts.append(f'{k}="{v}"')
                elif v is not None:
                    result_parts.append(f"{k}={v}")
        if result_parts:
            return f"{desc} → OK, {', '.join(result_parts)}"
        return f"{desc} → OK"
    else:
        err_msg = (error[:50]) if error else "unknown error"
        return f"{desc} → FAILED: {err_msg}"


def _format_action_history(entries: list) -> str:
    """Format action history entries as a numbered list for user messages.

    Returns empty string if no entries. Shows last 8 entries.
    """
    if not entries:
        return ""
    recent = entries[-8:]
    lines = [f"{i + 1}. {e}" for i, e in enumerate(recent)]
    return "\nPrevious actions:\n" + "\n".join(lines)


def _format_tool_result(result: dict) -> str:
    """Format an execute_tool result dict as a concise string for the LLM."""
    if result["success"]:
        data = result["data"]
        # Build a brief summary from the data dict
        parts = ["OK"]
        for k, v in data.items():
            if k == "action":
                continue  # redundant with tool name
            parts.append(f"{k}={v}")
        return "; ".join(parts)
    else:
        return f"ERROR: {result['error']}"


def _try_parse_text_tool_call(text: str):
    """Try to extract a tool call from plain text that contains tool-call JSON.

    Llama 3.3 70B sometimes emits tool calls as plain text instead of structured
    tool_calls. This parses two known formats:
      1. {"name": "click", "arguments": "{\"ref\": \"e1\"}"}
      2. {"type": "function", "name": "click", "parameters": {"ref": "e1"}}

    Returns (tool_name, params_dict) if a valid tool call is found, else None.
    """
    if not text:
        return None

    # Try to find JSON object(s) in the text
    # Look for outermost { ... } containing "name"
    candidates = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:i + 1])
                start = None

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(obj, dict) or "name" not in obj:
            continue

        tool_name = obj["name"]

        # Format 1: arguments as JSON string
        if "arguments" in obj:
            args = obj["arguments"]
            if isinstance(args, str):
                try:
                    params = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    continue
            elif isinstance(args, dict):
                params = args
            else:
                continue
            return (tool_name, params)

        # Format 2: parameters as dict
        if "parameters" in obj and isinstance(obj["parameters"], dict):
            return (tool_name, obj["parameters"])

    return None


# ---------------------------------------------------------------------------
# Snapshot helper
# ---------------------------------------------------------------------------

async def _get_snapshot(page):
    """Get compressed DOM snapshot and ref_map from page using v2 serializer."""
    raw_html, ref_map = await serialize_dom_v2(page)
    snapshot = compress_snapshot(raw_html)
    return snapshot, ref_map


# ---------------------------------------------------------------------------
# WebAgent — the general-purpose agent loop
# ---------------------------------------------------------------------------

class WebAgent:
    """General-purpose web agent that solves tasks by observing and acting.

    Usage:
        agent = WebAgent()
        success = await agent.run(page, "Find and click the login button")

        # With custom completion check:
        agent = WebAgent()
        success = await agent.run(
            page,
            "Fill out the form and submit",
            is_done=lambda p: "thank you" in p.url,
        )
    """

    def __init__(
        self,
        system_prompt: str | None = None,
        max_turns: int = MAX_TURNS,
        max_resets: int = MAX_RESETS,
        max_continuations: int = MAX_CONTINUATIONS,
        max_conversation_chars: int = MAX_CONVERSATION_CHARS,
    ):
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.max_turns = max_turns
        self.max_resets = max_resets
        self.max_continuations = max_continuations
        self.max_conversation_chars = max_conversation_chars

    async def run(
        self,
        page,
        objective: str,
        is_done=None,
        settle_fn=None,
    ) -> bool:
        """Drive the agent loop with fresh-agent continuations on turn exhaustion.

        On each attempt: snapshot → LLM picks ONE tool → execute → observe → repeat.
        If the turn budget is exhausted, a fresh agent is spawned on the same page
        state with hints about previous actions (up to max_continuations times).

        Args:
            page: Playwright Page object.
            objective: Text describing the task (shown as first user message).
            is_done: Optional callable(page) -> bool for completion check.
                     Default: check if URL changed from initial URL.
            settle_fn: Optional async callable(ms) to wait for page settling.
                       Default: page.wait_for_timeout.

        Returns True if task completed, False otherwise.
        """
        logger.info(f"WebAgent.run: {objective[:80]}")

        # Default completion: URL changed
        initial_url = page.url
        if is_done is None:
            is_done = lambda p: p.url != initial_url

        # Default settle: simple timeout
        if settle_fn is None:
            settle_fn = page.wait_for_timeout

        all_action_history = []  # accumulated across continuations

        for attempt in range(1 + self.max_continuations):
            snapshot, ref_map = await _get_snapshot(page)

            # Build continuation hint from previous attempts
            hint = ""
            if attempt > 0:
                logger.info(f"CONTINUATION #{attempt}: fresh agent on same page")
                if all_action_history:
                    recent = all_action_history[-8:]
                    actions_str = "\n".join(
                        f"  {i+1}. {a}" for i, a in enumerate(recent)
                    )
                    hint = (
                        f"\n\nPrevious agent exhausted its turn budget after "
                        f"{len(all_action_history)} actions. Recent actions:\n"
                        f"{actions_str}\n"
                        "Continue from the current page state. "
                        "Avoid repeating failed approaches."
                    )

            success, action_history = await self._run_attempt(
                page, objective, is_done, settle_fn, snapshot, ref_map, hint,
            )

            if success:
                return True

            all_action_history.extend(action_history)

        total_actions = len(all_action_history)
        attempts = 1 + self.max_continuations
        logger.warning(f"FAILED after {attempts} attempt(s), {total_actions} total actions")
        return False

    async def _run_attempt(
        self, page, objective, is_done, settle_fn, snapshot, ref_map, hint="",
    ) -> tuple[bool, list]:
        """Run one attempt of the agent loop.

        Returns (success, action_history) so the caller can accumulate
        history across continuations.
        """
        prev_snapshot = ""
        consecutive_unchanged = 0
        consecutive_text_only = 0
        total_resets = 0
        action_history = []
        action_keys = []  # canonical keys for cycle/duplicate detection

        # Build conversation: system + first user message with objective + snapshot
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": f"{objective}{hint}\n\nDOM SNAPSHOT:\n{snapshot}"},
        ]

        turn = 0
        llm_calls = 0  # safety cap: prevent infinite loop on repeated BLOCKED
        while turn < self.max_turns and llm_calls < self.max_turns * 2:
            llm_calls += 1
            turn += 1  # counts by default; decremented if BLOCKED overlay
            logger.info(f"Turn {turn}/{self.max_turns}")

            # Trim if conversation has grown too large
            messages = _trim_messages(messages, self.max_conversation_chars)

            # Call LLM — tool_choice="required" forces structured output
            try:
                tools = get_tools_for_snapshot(snapshot)
                msg = cerebras_call(messages, tools, tool_choice="required")
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                break

            # No structured tool calls — try parsing tool JSON from text (Llama fallback)
            if not msg.tool_calls:
                parsed = _try_parse_text_tool_call(msg.content)
                if parsed:
                    tool_name, params = parsed
                    logger.info(f"Parsed text-only tool call: {tool_name}({params})")

                    # Synthesize a tool_call_id for conversation history
                    synthetic_id = f"text_{uuid.uuid4().hex[:8]}"

                    # Append as if it were a structured tool call
                    messages.append({
                        "role": "assistant",
                        "content": msg.content,
                        "tool_calls": [{
                            "id": synthetic_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(params),
                            },
                        }],
                    })

                    # Execute the parsed tool call
                    result = await execute_tool(page, ref_map, tool_name, params)

                    action_history.append(_format_action_entry(
                        tool_name, params, ref_map,
                        success=result["success"],
                        error=result.get("error", ""),
                        data=result.get("data", {}),
                    ))

                    messages.append({
                        "role": "tool",
                        "tool_call_id": synthetic_id,
                        "content": _format_tool_result(result),
                    })

                    consecutive_text_only = 0

                    if not result["success"]:
                        logger.warning(f"{tool_name} (text-parsed) failed: {result['error']}")
                        if "BLOCKED" in result.get("error", ""):
                            turn -= 1
                            continue

                    # Only record successfully executed actions for cycle detection
                    action_keys.append(_action_key(tool_name, params))

                    # Settle and check completion
                    await settle_fn(_settle_ms_for_action(tool_name))
                    if is_done(page):
                        logger.info("COMPLETED after text-parsed tool call")
                        return True, action_history

                    # Update snapshot
                    prev_snapshot = snapshot
                    snapshot, ref_map = await _get_snapshot(page)

                    change_desc = _describe_snapshot_change(prev_snapshot, snapshot)
                    changed = _snapshot_changed(prev_snapshot, snapshot)
                    if changed:
                        consecutive_unchanged = 0
                        if action_history:
                            action_history[-1] += f", {change_desc}"
                    else:
                        consecutive_unchanged += 1
                        if action_history:
                            action_history[-1] += ", no visible change"

                    # Action repetition detection (cycle + duplicate)
                    # Only check when page didn't change — repeated actions that produce
                    # visible changes are intentional
                    if consecutive_unchanged > 0:
                        repetition_warning = _detect_action_repetition(action_keys)
                        if repetition_warning:
                            logger.info(f"Action repetition detected (text-parsed): {repetition_warning}")
                            consecutive_unchanged = 3  # trigger reset below

                    # Auto-wait-and-recheck if deeply stuck
                    if consecutive_unchanged >= 3:
                        await settle_fn(300)
                        snapshot, ref_map = await _get_snapshot(page)
                        if is_done(page):
                            logger.info("COMPLETED after text-parsed auto-wait")
                            return True, action_history
                        if total_resets < self.max_resets:
                            total_resets += 1
                            failed_actions = [a for a in action_history if "no visible change" in a or "FAILED" in a]
                            from collections import Counter
                            key_counts = Counter(action_keys)
                            repeated_keys = {k for k, c in key_counts.items() if c >= 2}
                            repeated_actions = [a for a, k in zip(action_history, action_keys) if k in repeated_keys]
                            seen = set()
                            all_bad = []
                            for a in failed_actions + repeated_actions:
                                if a not in seen:
                                    seen.add(a)
                                    all_bad.append(a)
                            hint = ""
                            if all_bad:
                                actions_str = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(all_bad[-6:]))
                                hint = f"\n\nIMPORTANT: Previous approaches that did NOT work:\n{actions_str}\nTry a COMPLETELY different strategy. Do not repeat these actions."
                            messages = [
                                {"role": "system", "content": self.system_prompt},
                                {"role": "user", "content": f"{objective}{hint}\n\nDOM SNAPSHOT:\n{snapshot}"},
                            ]
                            consecutive_unchanged = 0
                            consecutive_text_only = 0
                            action_keys.clear()
                            prev_snapshot = snapshot
                            continue

                    # Build next user message
                    remaining = self.max_turns - turn
                    budget_tag = f"[Turn {turn}/{self.max_turns} — {remaining} remaining]"
                    if turn == 0:
                        context_block = f"DOM SNAPSHOT:\n{snapshot}"
                    else:
                        diff = compute_diff_sections(prev_snapshot, snapshot)
                        if diff is None:
                            context_block = f"DOM SNAPSHOT:\n{compute_diff_markers(prev_snapshot, snapshot)}"
                        else:
                            summary = generate_page_summary(snapshot, ref_map)
                            context_block = f"{summary}\n\n{diff}"

                    warning = _get_stuck_warning(consecutive_unchanged, turn)
                    history_str = _format_action_history(action_history)
                    change_note = f" Effect: {change_desc}." if turn > 0 else ""
                    messages.append({
                        "role": "user",
                        "content": f"{budget_tag}{change_note}{warning}{history_str}\n\n{context_block}",
                    })
                    continue

                # Genuine text-only response (no parseable tool call)
                consecutive_text_only += 1
                logger.info(f"Text-only response ({consecutive_text_only} consecutive): {msg.content[:200] if msg.content else 'None'}")

                # Append the text-only response to conversation
                messages.append(_serialize_assistant_msg(msg))

                if consecutive_text_only >= 4:
                    logger.info(f"Breaking after {consecutive_text_only} consecutive text-only responses")
                    break

                if consecutive_text_only >= 3:
                    # Inject a reminder to use tools
                    messages.append({
                        "role": "user",
                        "content": "You MUST call a tool to interact with the page. Analyze the DOM snapshot and use click, fill, or another tool.",
                    })

                continue

            # Tool call received — reset text-only counter
            consecutive_text_only = 0

            # Append assistant message (with tool_calls) to conversation
            messages.append(_serialize_assistant_msg(msg))

            # Execute FIRST tool call only (one action per turn)
            tc = msg.tool_calls[0]
            tool_name = tc.function.name
            try:
                params = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                raw_args = tc.function.arguments
                logger.warning(f"Bad JSON in tool args: {raw_args}")
                # Try fuzzy ref extraction: find first eN pattern
                ref_match = re.search(r'e(\d+)', raw_args)
                if ref_match:
                    params = {"ref": ref_match.group(0)}
                    logger.info(f"Fuzzy-extracted ref={params['ref']} from malformed args")
                else:
                    # Return clear error to LLM with correct format
                    result = {
                        "success": False,
                        "data": {},
                        "error": f'Malformed arguments: {raw_args[:100]}. '
                                 f'Expected JSON like {{"ref": "e5"}}. Fix the JSON and retry.',
                    }
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _format_tool_result(result),
                    })
                    continue

            result = await execute_tool(page, ref_map, tool_name, params)

            # Build rich action history entry
            action_history.append(_format_action_entry(
                tool_name, params, ref_map,
                success=result["success"],
                error=result.get("error", ""),
                data=result.get("data", {}),
            ))
            # Send tool result back to LLM
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _format_tool_result(result),
            })

            if not result["success"]:
                logger.warning(f"{tool_name} failed: {result['error']}")
                # BLOCKED overlay errors get a free retry — don't count as a turn
                # and don't record in action_keys (never executed, shouldn't trigger cycle detection)
                if "BLOCKED" in result.get("error", ""):
                    turn -= 1
                    continue

            # Only record successfully executed actions for cycle detection
            action_keys.append(_action_key(tool_name, params))

            # Informational tools (no page side-effect): skip snapshot
            # refresh and don't consume a turn — let the LLM reason
            # about the result before choosing its next page action.
            if tool_name in INFORMATIONAL_TOOLS:
                turn -= 1
                continue

            # Settle after action — adaptive based on action type
            await settle_fn(_settle_ms_for_action(tool_name))

            # Check completion
            if is_done(page):
                logger.info(f"COMPLETED (URL: {page.url})")
                return True, action_history

            # Update snapshot for next turn
            prev_snapshot = snapshot
            snapshot, ref_map = await _get_snapshot(page)

            # Anti-stuck detection: content-aware line-level diff
            change_desc = _describe_snapshot_change(prev_snapshot, snapshot)
            changed = _snapshot_changed(prev_snapshot, snapshot)
            if changed:
                consecutive_unchanged = 0
                if action_history:
                    action_history[-1] += f", {change_desc}"
            else:
                consecutive_unchanged += 1
                logger.debug(f"Snapshot unchanged ({consecutive_unchanged} consecutive)")
                if action_history:
                    action_history[-1] += ", no visible change"

            # Action repetition detection (cycle + duplicate)
            # Only check when page didn't change — repeated actions that produce
            # visible changes (e.g. carousel next, mutation trigger) are intentional
            if consecutive_unchanged > 0:
                repetition_warning = _detect_action_repetition(action_keys)
                if repetition_warning:
                    logger.info(f"Action repetition detected: {repetition_warning}")
                    consecutive_unchanged = 3  # trigger the reset below

            # Auto-wait-and-recheck if deeply stuck
            if consecutive_unchanged >= 3:
                logger.info(f"Auto-wait-and-recheck (stuck {consecutive_unchanged} turns)")
                await settle_fn(300)
                snapshot, ref_map = await _get_snapshot(page)

                # Check completion again after wait
                if is_done(page):
                    logger.info("COMPLETED after auto-wait")
                    return True, action_history

                # Fresh reset: discard polluted conversation, restart with anti-repeat hints
                if total_resets < self.max_resets:
                    total_resets += 1
                    logger.info(f"FRESH RESET #{total_resets} (stuck {consecutive_unchanged} turns)")

                    # Build anti-repeat hint from failed + repeated actions
                    failed_actions = [a for a in action_history if "no visible change" in a or "FAILED" in a]
                    # Also include actions that were part of detected cycles
                    from collections import Counter
                    key_counts = Counter(action_keys)
                    repeated_keys = {k for k, c in key_counts.items() if c >= 2}
                    repeated_actions = [a for a, k in zip(action_history, action_keys) if k in repeated_keys]
                    # Merge both lists, dedup, keep order
                    seen = set()
                    all_bad = []
                    for a in failed_actions + repeated_actions:
                        if a not in seen:
                            seen.add(a)
                            all_bad.append(a)

                    hint = ""
                    if all_bad:
                        actions_str = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(all_bad[-6:]))
                        hint = (
                            f"\n\nIMPORTANT: Previous approaches that did NOT work:\n{actions_str}"
                            "\nTry a COMPLETELY different strategy. Do not repeat these actions."
                        )

                    # Reset conversation with fresh context
                    messages = [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": f"{objective}{hint}\n\nDOM SNAPSHOT:\n{snapshot}"},
                    ]
                    consecutive_unchanged = 0
                    consecutive_text_only = 0
                    action_keys.clear()
                    prev_snapshot = snapshot
                    continue  # Skip to next turn with fresh context

            # Build next user message with updated snapshot context
            remaining = self.max_turns - turn
            budget_tag = f"[Turn {turn}/{self.max_turns} — {remaining} remaining]"
            if turn == 0:
                # Turn 2 (after first action): full snapshot still useful
                context_block = f"DOM SNAPSHOT:\n{snapshot}"
            else:
                # Turn 3+: diff-only mode
                diff = compute_diff_sections(prev_snapshot, snapshot)
                if diff is None:
                    # Diff too large — fall back to full snapshot with diff markers
                    context_block = f"DOM SNAPSHOT:\n{compute_diff_markers(prev_snapshot, snapshot)}"
                    logger.debug("Diff fallback to full snapshot (too many changes)")
                else:
                    summary = generate_page_summary(snapshot, ref_map)
                    context_block = f"{summary}\n\n{diff}"
                    logger.debug(f"Diff mode ({len(context_block)} chars vs {len(snapshot)} full)")

            # Anti-stuck warning
            warning = _get_stuck_warning(consecutive_unchanged, turn)

            # Append updated context as new user message
            history_str = _format_action_history(action_history)
            change_note = f" Effect: {change_desc}." if turn > 0 else ""
            messages.append({
                "role": "user",
                "content": f"{budget_tag}{change_note}{warning}{history_str}\n\n{context_block}",
            })

        logger.warning(f"Attempt exhausted after {turn} turns ({llm_calls} LLM calls)")
        return False, action_history
