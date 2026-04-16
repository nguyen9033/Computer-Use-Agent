"""Challenge-specific runner for the 30-step browser navigation challenge.

Uses WebAgent (general-purpose) to solve each step. This module contains
challenge-specific knowledge: step numbering, URL patterns, skip list,
site URL, and the challenge objective.

Also re-exports agent helpers for test backward compatibility.
"""

import logging
import asyncio

from .agent import (
    # Re-exports for test backward compatibility
    WebAgent,
    cerebras_call,
    _serialize_assistant_msg,
    _trim_messages,
    _format_tool_result,
    _format_action_entry,
    _format_action_history,
    _get_stuck_warning,
    _snapshot_changed,
    _try_parse_text_tool_call,
    _get_snapshot,
    _action_key,
    _detect_action_repetition,
    MAX_TURNS,
    MAX_RESETS,
    MAX_CONTINUATIONS,
    MAX_CONVERSATION_CHARS,
)
from .browser_env import BrowserEnv, SKIP_STEPS

logger = logging.getLogger(__name__)

# Continuations: fresh agent spawned on same page when turns exhausted
STEP_CONTINUATIONS = 3

# Challenge-specific objective — passed to WebAgent per step
CHALLENGE_OBJECTIVE = (
    "I'm solving a 30-step web challenge. Each page has a puzzle — "
    "complete it, find the code that appears, enter it, and submit "
    "to advance to the next page. Go fast."
)


async def solve_step(env: BrowserEnv, step: int) -> bool:
    """Solve a single challenge step using WebAgent.

    Challenge-specific wrapper that:
    1. Navigates to the step URL
    2. Constructs the per-step objective
    3. Defines completion as URL change
    4. Delegates to WebAgent.run()

    Returns True if step completed (URL changed), False otherwise.
    """
    logger.info(f"--- Step {step}/30 ---")

    # Navigate to step and settle
    await env.navigate_to_step(step)

    # Challenge-specific objective with step context
    objective = f"Step {step}/30. {CHALLENGE_OBJECTIVE}"

    # Completion: URL changes from current page
    prev_url = env.page.url

    def is_done(page):
        return env.url_changed_from(prev_url)

    # Use the general agent — with continuations for reliability
    agent = WebAgent(max_continuations=STEP_CONTINUATIONS)
    return await agent.run(
        env.page,
        objective,
        is_done=is_done,
        settle_fn=env.wait_for_settle,
    )


async def main():
    """Run the challenge. Use --steps to run specific steps only."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Parse --steps 10,11,16 or run all 1-30
    steps_to_run = list(range(1, 31))
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--steps" and i < len(sys.argv):
            steps_to_run = [int(s) for s in sys.argv[i + 1].split(",")]
            break

    results = {}  # step -> "pass" | "fail" | "skip"

    async with BrowserEnv(headless=False) as env:
        # Navigate to landing page and start
        await env.navigate_to_site()
        await env.start_challenge()

        for step in steps_to_run:
            if step in SKIP_STEPS:
                reason = SKIP_STEPS[step]
                logger.info(f"Step {step}: SKIP — {reason}")
                results[step] = "skip"
                continue

            success = await solve_step(env, step)
            results[step] = "pass" if success else "fail"

        # Bypass step 30 to reach the challenge completion page.
        # Step 30 has an off-by-one bug; we solve it programmatically
        # without the agent. This is NOT tracked in results.
        try:
            await env.bypass_step_30()
        except Exception as e:
            logger.warning(f"Step 30 bypass failed: {e}")

    # Print summary
    passed = sum(1 for v in results.values() if v == "pass")
    failed = sum(1 for v in results.values() if v == "fail")
    skipped = sum(1 for v in results.values() if v == "skip")

    print(f"\n{'='*50}")
    print(f"RESULTS: {passed} pass, {failed} fail, {skipped} skip")
    print(f"{'='*50}")
    for step in sorted(results):
        status = results[step]
        marker = {"pass": "OK", "fail": "FAIL", "skip": "SKIP"}.get(status, "???")
        print(f"  Step {step:2d}: {marker}")


if __name__ == "__main__":
    asyncio.run(main())
