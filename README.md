# Computer Use Agent

General-purpose LLM-driven browser agent. Validated on Brett Adcock's 30-step web challenge ([source](https://x.com/adcock_brett/status/2018417226895028414)) — a React SPA with overlays, canvas drawing, drag-and-drop, hidden codes, and base64 puzzles.

- **Benchmark human solving time:** 5 minutes
- **Agent solving time:** 4 minutes 10 seconds (27 solvable steps; 3 steps proven unsolvable due to bugs in the challenge site's JS)
- **Model:** Llama 3.3 70B on Cerebras (~0.4s per call)
- **Stack:** Python 3.10+, Playwright, OpenAI SDK (Cerebras-compatible endpoint)
- **Scope:** ~4,250 lines across 7 modules, single author

## What's in the repo

```
src/
  orchestrator.py   Challenge runner (~140 lines)
  agent.py          WebAgent loop, LLM client, anti-stuck logic (~1,130 lines)
  browser_env.py    Playwright wrapper (~185 lines)
  perception.py     DOM serializer, compression, diff engine (~1,145 lines)
  prompts.py        System prompt + tool schemas (~266 lines)
  actions.py        11-tool action layer (~562 lines)
  tools_helper.js   Injected JS for canvas/DnD (~195 lines)
ARCHITECTURE.md     Full internal deep-dive (~630 lines)
perception_comparison/  Side-by-side dumps: raw DOM vs. a11y tree vs. compressed
```

## How it works (60 seconds)

1. `orchestrator.py` navigates to each step and calls `WebAgent.run(page, objective)`.
2. `WebAgent` loops: serialize the DOM → call the LLM with tool definitions → execute **one** tool → check if the URL changed → repeat.
3. Perception emits a compact text snapshot with stable `ref=eN` IDs on every interactive element, overlay/main section routing, and `COVERED` flags on elements blocked by other elements.
4. From turn 2, only a diff of the snapshot is sent (~85% token reduction).
5. Anti-stuck layers escalate from warnings → fresh conversation reset → fresh agent continuation.

## Roadmap

Extending the `WebAgent` core (objective + `is_done` predicate) to consumer automation: bill payment portals and insurance claim processing. Both share the properties that made the challenge a useful proving ground — real React SPAs, dynamic overlays, multi-step forms, no stable test IDs.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # set CEREBRAS_API_KEY
```

## Run

```bash
python -m src.orchestrator                    # full 30-step run, headed
python -m src.orchestrator --steps 1,2,3      # specific steps
```

Config lives in `src/agent.py` (`MAX_TURNS=14`, `MAX_RESETS=2`, `LLM_TEMPERATURE=0.0`, etc.) and can be overridden via env vars.

---

## Appendix — Technical Notes

Short notes on the specific design choices, grouped by subsystem. Full reasoning in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

### A1. Perception: DOM serializer, not accessibility tree

The a11y tree discards position, z-index, computed cursor, and hidden-but-present text. All four matter for overlay reasoning and for reading codes hidden via `display:none`. A custom `page.evaluate()` walker emits an annotated text tree instead.

### A2. MAIN / OVERLAY routing

Elements with `position:fixed` or (`position:absolute` AND `z-index > 100`) are routed to an `=== OVERLAYS ===` section. Everything else goes to `=== MAIN ===`. Spatial intuition for the LLM without making it parse CSS.

### A3. Prominence and semantic color

Every ref'd element gets `WxH` dimensions plus a `PROMINENT` flag (area > 1% of viewport AND font-size ≥ 16px). Background colors snap to the nearest named color (`bg=green`, `bg=red`) — compact hints for disambiguating the real CTA from decoys.

### A4. Snapshot compression

`group_similar_buttons()` collapses 4+ consecutive absolute-positioned decoys into one line. `trim_snapshot()` collapses 3+ repetitions of structurally-identical (digit/ref-normalized) blocks. Typical reduction: 30–50% on decoy-heavy pages.

### A5. Ref system

`data-ref="eN"` is stamped onto the DOM by the serializer; `ref=eN` appears in the snapshot text. The LLM targets elements by short ID instead of CSS selectors. `resolve_ref()` tries `[data-ref="eN"]` → stored CSS path → scoped text (short text for `[draggable="true"]`) → general text.

### A6. Stale refs

If a ref no longer exists, `resolve_ref()` raises `ValueError` with the currently available ref list. The agent loop feeds that back into the next prompt, so the model recovers with ground truth instead of hallucinating.

### A7. Coverage check

A second `page.evaluate()` pass calls `elementFromPoint()` at each ref'd element's center. If the topmost hit isn't the target (or an ancestor/descendant), the ref is flagged `COVERED`. The click handler short-circuits to BLOCKED without invoking Playwright, saving ~2s per doomed click.

### A8. Force click

When BLOCKED, the LLM retries with `force=true`. Force click uses `page.evaluate("el => el.click()", handle)` — dispatches directly on the DOM node, bypassing hit-testing.

### A9. No popup killer

Rejected a global overlay-dismiss script. It's fragile (breaks on legitimate overlays) and opaque to the model. Surfacing overlays via `COVERED` + providing a force-click escape hatch is more general.

### A10. One action per turn

Earlier two-phase `perceive → plan → execute` design produced plans that assumed predicted page states that didn't match reality. One tool call per turn with full conversation history eliminated 8 distinct failure modes on Llama 3.3 70B.

### A11. Diff-mode snapshots

Turn 1 sends the full snapshot. Turn 2+ sends a compact `PAGE SUMMARY` (one-line ref enumeration) plus a unified diff with `[NEW]` markers. Fallback to full snapshot if diff > 50% additions. Measured: ~12K chars → ~1–2K chars per turn.

### A12. Conversation trimming

When history exceeds `MAX_CONVERSATION_CHARS` (50K, ~12K tokens), cut complete exchanges from the oldest end at `user` message boundaries — avoids orphaning `tool` messages that require a matching `assistant` with `tool_calls`.

### A13. Anti-stuck layer 1 — change detection

Line-level set diff between old and new snapshots after each action. Identical → `consecutive_unchanged += 1`. Quantified feedback ("+3 lines, 2 new interactive elements") is injected into the next user message.

### A14. Anti-stuck layer 2 — escalating warnings

Turn ≥ 2, unchanged ≥ 1: inject `WARNING`. Turn ≥ 4, unchanged ≥ 2: inject `CRITICAL: try a COMPLETELY different approach`.

### A15. Anti-stuck layer 3 — cycle / duplicate detection

Last 6 action keys scanned for 3+ duplicates or length-1-3 cycles. Detection forces `consecutive_unchanged = 3`, cascading into layer 4.

### A16. Anti-stuck layer 4 — fresh conversation reset

When `consecutive_unchanged >= 3`, discard the entire conversation. Rebuild with the system prompt + objective + an explicit list of failed actions ("Previous approaches that did NOT work...") + the current snapshot. Up to `MAX_RESETS=2` per step. The key insight: flush the polluted context, preserve the negative knowledge.

### A17. Anti-stuck layer 5 — fresh agent continuation

When the turn budget is exhausted, `WebAgent.run()` can spawn a new attempt (up to `max_continuations`) on the same page state, seeded with the last 8 actions the previous attempt tried.

### A18. LLM failure modes

Three observed error classes, each with a specific recovery path:
- **Empty response** → exponential backoff retry (`MAX_RETRIES=5`).
- **`tool_use_failed` HTTP 400** → parse the `failed_generation` field (e.g., `fill(ref=e16, value=5E8YN2)`), wrap in a synthetic `ChatCompletion` message, dispatch as if the call succeeded.
- **Text-emitted tool calls** → Llama sometimes emits the JSON as plain text. `_try_parse_text_tool_call()` extracts JSON objects with `"name"` fields from the text body. After 3 consecutive text-only responses, inject a reminder; after 4, break.

### A19. Conditional tool selection

`get_tools_for_snapshot(snapshot)` hides tools that don't apply: `draw` only if `<canvas` appears, `drag_and_drop` only if `draggable` appears, `decode_base64` only if `base64|encoded|decode` appears. Six core tools (click, fill, hover, scroll, wait, press) are always available.

### A20. Turn budget exemptions

BLOCKED errors and informational tools (`decode_base64`) don't consume a turn — the agent retries or acts on the result within the same budget. The 14-turn budget tracks meaningful page interactions, not raw LLM calls.

### A21. Scroll: inner container detection

Many steps have scrollable inner containers. The handler scrolls the main viewport, then scans all descendants via `getComputedStyle()` for `overflow-y: auto|scroll` AND `scrollHeight > clientHeight`, scrolls the first hit via `el.scrollBy()`, and dispatches a `scroll` event for React compatibility.

### A22. Draw: canvas event sequencing

`drawStrokes()` fires pointer + mouse events on canvas with 50ms pauses at stroke start and end so React can flush `setIsDrawing(true)` state. Without the pauses, the canvas records nothing.

### A23. Drag and drop: three strategies

`simulateDrag()` fires HTML5 `DragEvent` (with `DataTransfer`), then `PointerEvent` with 5 interpolated moves, then `MouseEvent` with 5 interpolated moves. Covers native HTML5 DnD, react-dnd, react-beautiful-dnd, and custom pointer-based libraries. Success is detected by comparing source element position/parent before and after.

### A24. SPA navigation

`page.goto()` resets session state. Instead: `pushState` + `dispatchEvent(new PopStateEvent('popstate'))` triggers the React router without a reload, preserving `sessionStorage` across steps.

### A25. Adaptive settling

`BrowserEnv.wait_for_settle()` polls `document.body.innerHTML.length` every 200ms, returns after two consecutive identical readings, with a 1500ms ceiling. Per-action hints: 50ms for `fill/hover`, 150ms for `click`, 200ms for `scroll`. Saves ~100–120s on a full run vs. a fixed 2s wait.

### A26. Benchmark ground-truth bugs

Three steps never pass. Read the minified site JS to root-cause:

| Step | Challenge | Root cause |
|---|---|---|
| 19 | `service_worker` | Intermittent code collision in the site's own random generation |
| 24 | `recursive_iframe` | Early-return guard `if (p \|\| u < y) return` with `u` bounded to `y-1` — fires every iteration |
| 30 | `websocket` | Loop runs `s <= 30`, but `markChallengeComplete(30)` calls `codes.get(31)` — off-by-one |

### A27. Step 30 bypass

Not patched in site code. `bypass_step_30()` reads the XOR-encoded `sessionStorage` session (XOR key `"WO_2024_CHALLENGE"`, base64), adds 30 to the completed set, re-encodes, and SPA-navigates to `/finish?version=2`.
