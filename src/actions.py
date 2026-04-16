"""Action layer: tool execution with direct DOM dispatch and ref resolution.

Executes LLM tool calls against the browser:
- execute_tool(): dispatch tool calls to Playwright actions
- resolve_ref(): map ref=eN to Playwright locators (CSS path, text fallback)
- Direct DOM dispatch for click/hover (bypasses overlay interception)
- Custom handlers for draw and drag_and_drop via tools_helper.js
"""

import base64
import logging

logger = logging.getLogger(__name__)


async def resolve_ref(page, ref_map: dict, ref: str):
    """Resolve a ref like 'e5' to a Playwright Locator.

    Lookup strategy:
    1. Find ref in ref_map -> get dict with 'css', 'tag', 'text' keys.
    2. Primary: page.locator(css) — the CSS path from the serializer.
    3. Fallback: page.locator('{tag}:has-text("{text}")') if CSS returns 0 matches.
    4. For draggable elements with short text (<=3 chars), scope to
       [draggable="true"] first for more accurate matching.

    Args:
        page: Playwright Page object.
        ref_map: Dict mapping ref IDs to {css, tag, text} dicts.
        ref: Reference string like 'e5'.

    Returns:
        Playwright Locator for the resolved element.

    Raises:
        ValueError: If ref is not found in ref_map, or if no element matches.
    """
    # Normalize ref: LLM sometimes sends "46" instead of "e46"
    if ref not in ref_map and not ref.startswith("e"):
        ref = f"e{ref}"
    if ref not in ref_map:
        raise ValueError(f"ref '{ref}' not found in ref_map (available: {list(ref_map.keys())[:10]})")

    info = ref_map[ref]
    css = info["css"]
    tag = info["tag"]
    text = info.get("text", "")

    logger.debug(f"resolve_ref({ref}): css={css}, tag={tag}, text={text!r}")

    # Primary: data-ref attribute stamped on DOM during serialization
    data_ref_loc = page.locator(f'[data-ref="{ref}"]')
    data_ref_count = await data_ref_loc.count()
    if data_ref_count > 0:
        logger.debug(f"resolve_ref({ref}): data-ref matched")
        return data_ref_loc.first

    # Fallback 1: CSS path from the serializer
    loc = page.locator(css)
    count = await loc.count()
    if count > 0:
        logger.debug(f"resolve_ref({ref}): CSS fallback matched {count} element(s)")
        return loc.first

    # Fallback 2: draggable elements with short text — scope to [draggable="true"]
    if text and len(text) <= 3:
        draggable_loc = page.locator(f'[draggable="true"]:has-text("{text}")')
        draggable_count = await draggable_loc.count()
        if draggable_count > 0:
            logger.debug(f"resolve_ref({ref}): draggable text fallback matched {draggable_count} element(s)")
            return draggable_loc.first

    # Fallback 3: general text match
    if text:
        text_loc = page.locator(f'{tag}:has-text("{text}")')
        text_count = await text_loc.count()
        if text_count > 0:
            logger.debug(f"resolve_ref({ref}): text fallback matched {text_count} element(s)")
            return text_loc.first

    raise ValueError(f"resolve_ref({ref}): no element found for css={css}, tag={tag}, text={text!r}")


async def execute_tool(page, ref_map: dict, tool_name: str, params: dict) -> dict:
    """Dispatch a tool call to the appropriate handler.

    Args:
        page: Playwright Page object.
        ref_map: Dict mapping ref IDs to {css, tag, text} dicts.
        tool_name: Name of the tool to execute (e.g. 'click', 'fill').
        params: Tool parameters from the LLM.

    Returns:
        Dict with keys:
        - success (bool): Whether the action succeeded.
        - data (dict): Action-specific result info.
        - error (str|None): Error message if failed, None if success.
    """
    handlers = {
        "click": _handle_click,
        "fill": _handle_fill,
        "type": _handle_type,
        "hover": _handle_hover,
        "press": _handle_press,
        "scroll": _handle_scroll,
        "wait": _handle_wait,
        "select": _handle_select,
        "draw": _handle_draw,
        "draw_strokes": _handle_draw,  # legacy alias
        "drag_and_drop": _handle_drag_and_drop,
        "simulate_drag": _handle_drag_and_drop,  # legacy alias
        "decode_base64": _handle_decode_base64,
    }

    handler = handlers.get(tool_name)
    if handler is None:
        return {
            "success": False,
            "data": {},
            "error": f"Unknown tool: {tool_name}",
        }

    try:
        data = await handler(page, ref_map, params)
        return {"success": True, "data": data, "error": None}
    except Exception as e:
        logger.warning(f"Tool '{tool_name}' failed: {e}")
        return {"success": False, "data": {}, "error": str(e)}


# ---------------------------------------------------------------------------
# Individual tool handlers
# ---------------------------------------------------------------------------


async def _handle_click(page, ref_map, params):
    """Click an element by ref.

    Default: Playwright's real click (actionability checks).
    force=true: synthetic DOM dispatch bypassing hit-testing.

    Preemptive coverage check: if the element is marked as covered in
    ref_map (from elementFromPoint detection), skip the Playwright click
    entirely and return the BLOCKED directive immediately. This avoids
    a ~2s timeout on a doomed click.
    """
    ref = params["ref"]
    force = params.get("force", False)
    reason = params.get("reason", "")

    # Preemptive coverage check — skip doomed clicks
    if not force:
        lookup = ref if ref in ref_map else f"e{ref}" if not ref.startswith("e") else ref
        if ref_map.get(lookup, {}).get("covered"):
            msg = (
                f'BLOCKED. Element {ref} is covered by an overlay. '
                f'Your next action MUST be: click({{"ref": "{ref}", "force": true}})'
            )
            logger.info(f"click({ref}, reason={reason!r}): preemptive BLOCKED (covered in ref_map)")
            raise Exception(msg)

    loc = await resolve_ref(page, ref_map, ref)

    if force:
        handle = await loc.element_handle()
        await page.evaluate("el => el.click()", handle)
        logger.info(f"click({ref}, force=True, reason={reason!r})")
        return {"action": "click", "ref": ref, "force": True}

    try:
        await loc.click(timeout=500)
        logger.info(f"click({ref}, reason={reason!r})")
        return {"action": "click", "ref": ref}
    except Exception as e:
        err = str(e)
        if "intercept" in err.lower() or "another element" in err.lower() or "not visible" in err.lower():
            msg = (
                f'BLOCKED. Element {ref} is covered by an overlay. '
                f'Your next action MUST be: click({{"ref": "{ref}", "force": true}})'
            )
            logger.warning(f"click({ref}): blocked by overlay")
            raise Exception(msg) from e
        raise


async def _handle_fill(page, ref_map, params):
    """Fill a text input, select a dropdown option, or type sequentially.

    Auto-detects <select> elements from ref_map tag and uses select_option().
    If sequential=True in params, uses press_sequentially() for key events.
    Default: loc.fill() to replace existing content.
    """
    ref = params["ref"]
    value = params["value"]
    sequential = params.get("sequential", False)
    loc = await resolve_ref(page, ref_map, ref)

    # Normalize ref for ref_map lookup
    lookup = ref if ref in ref_map else f"e{ref}" if not ref.startswith("e") else ref
    tag = ref_map.get(lookup, {}).get("tag", "")

    if tag == "select":
        await loc.select_option(value)
        logger.info(f"fill({ref}, {value!r}) [select_option]")
        return {"action": "fill", "ref": ref, "value": value, "method": "select_option"}
    elif sequential:
        await loc.press_sequentially(value)
        logger.info(f"fill({ref}, {value!r}) [sequential]")
        return {"action": "fill", "ref": ref, "value": value, "method": "sequential"}
    else:
        await loc.fill(value)
        logger.info(f"fill({ref}, {value!r})")
        return {"action": "fill", "ref": ref, "value": value}


async def _handle_type(page, ref_map, params):
    """Type text character by character, triggering key events."""
    ref = params["ref"]
    text = params["text"]
    loc = await resolve_ref(page, ref_map, ref)
    await loc.press_sequentially(text)
    logger.info(f"type({ref}, {text!r})")
    return {"action": "type", "ref": ref, "text": text}


async def _handle_hover(page, ref_map, params):
    """Hover over an element.

    Default: Playwright's real hover.
    force=true: synthetic mouseenter/mouseover dispatch bypassing hit-testing.

    Preemptive coverage check same as _handle_click.
    """
    ref = params["ref"]
    force = params.get("force", False)

    # Preemptive coverage check — skip doomed hovers
    if not force:
        lookup = ref if ref in ref_map else f"e{ref}" if not ref.startswith("e") else ref
        if ref_map.get(lookup, {}).get("covered"):
            msg = (
                f'BLOCKED. Element {ref} is covered by an overlay. '
                f'Your next action MUST be: hover({{"ref": "{ref}", "force": true}})'
            )
            logger.info(f"hover({ref}): preemptive BLOCKED (covered in ref_map)")
            raise Exception(msg)

    loc = await resolve_ref(page, ref_map, ref)

    if force:
        handle = await loc.element_handle()
        await page.evaluate("""el => {
            el.dispatchEvent(new MouseEvent("mouseenter", {bubbles: true}));
            el.dispatchEvent(new MouseEvent("mouseover", {bubbles: true}));
        }""", handle)
        logger.info(f"hover({ref}, force=True)")
        return {"action": "hover", "ref": ref, "force": True}

    try:
        await loc.hover(timeout=500)
        logger.info(f"hover({ref})")
        return {"action": "hover", "ref": ref}
    except Exception as e:
        err = str(e)
        if "intercept" in err.lower() or "another element" in err.lower() or "not visible" in err.lower():
            msg = (
                f'BLOCKED. Element {ref} is covered by an overlay. '
                f'Your next action MUST be: hover({{"ref": "{ref}", "force": true}})'
            )
            logger.warning(f"hover({ref}): blocked by overlay")
            raise Exception(msg) from e
        raise


async def _handle_press(page, ref_map, params):
    """Press a keyboard key (no ref needed)."""
    key = params["key"]
    await page.keyboard.press(key)
    logger.info(f"press({key!r})")
    return {"action": "press", "key": key}


async def _handle_scroll(page, ref_map, params):
    """Scroll the page and inner scrollable containers.

    Scrolls both the main viewport (via mouse.wheel) and the first
    inner scrollable container found by computedStyle detection.
    Uses getComputedStyle to find actually-scrollable elements
    (scrollHeight > clientHeight), dispatches scroll events for React
    compatibility, and reports whether inner scrolling worked.
    """
    direction = params["direction"]
    amount = params.get("amount", 500)

    dx, dy = 0, 0
    if direction == "down":
        dy = amount
    elif direction == "up":
        dy = -amount
    elif direction == "right":
        dx = amount
    elif direction == "left":
        dx = -amount

    # Scroll the viewport
    await page.mouse.wheel(dx, dy)

    # Also scroll inner scrollable containers using computedStyle detection
    scrolled_inner = await page.evaluate("""(args) => {
        var area = document.body;
        for (var el of area.querySelectorAll('*')) {
            var style = window.getComputedStyle(el);
            var ov = args.dy !== 0 ? style.overflowY : style.overflowX;
            if ((ov === 'auto' || ov === 'scroll') &&
                (args.dy !== 0 ? el.scrollHeight > el.clientHeight
                               : el.scrollWidth > el.clientWidth)) {
                var before = args.dy !== 0 ? el.scrollTop : el.scrollLeft;
                el.scrollBy(args.dx, args.dy);
                el.dispatchEvent(new Event('scroll', { bubbles: true }));
                var after = args.dy !== 0 ? el.scrollTop : el.scrollLeft;
                if (after !== before) return true;
            }
        }
        return false;
    }""", {"dx": dx, "dy": dy})

    logger.info(f"scroll({direction}, {amount}, inner={scrolled_inner})")
    return {"action": "scroll", "direction": direction, "amount": amount,
            "scrolled_inner": bool(scrolled_inner)}


async def _handle_wait(page, ref_map, params):
    """Wait for specified milliseconds.

    Accepts 'timeout', 'duration', 'ms', or 'time' as the parameter name
    since the LLM may use any of these when generating plans without tool schemas.
    """
    timeout = (
        params.get("timeout")
        or params.get("duration")
        or params.get("ms")
        or params.get("time")
        or params.get("milliseconds")
        or 2000  # sensible default if all keys missing
    )
    # Clamp: at least 100ms, at most 10s
    timeout = max(100, min(int(timeout), 10_000))
    await page.wait_for_timeout(timeout)
    logger.info(f"wait({timeout}ms)")
    return {"action": "wait", "timeout": timeout}


async def _handle_select(page, ref_map, params):
    """Select a dropdown option by value."""
    ref = params["ref"]
    value = params["value"]
    loc = await resolve_ref(page, ref_map, ref)
    await loc.select_option(value)
    logger.info(f"select({ref}, {value!r})")
    return {"action": "select", "ref": ref, "value": value}



async def _handle_draw(page, ref_map, params):
    """Draw on a canvas element via JS synthetic events.

    Uses tools_helper.js drawStrokes() which dispatches MouseEvent and
    PointerEvent directly on the canvas element. Events target the element
    directly so overlays don't interfere. Async delays between event phases
    let React flush state updates (e.g. isDrawing flag) between mousedown
    and mousemove.
    """
    selector = params["selector"]
    strokes = params["strokes"]
    stroke_count = len(strokes) if isinstance(strokes, list) else 0

    await page.evaluate(
        "(args) => window.__tools.drawStrokes(args.selector, args.strokes)",
        {"selector": selector, "strokes": strokes},
    )

    logger.info(f"draw({selector!r}, {stroke_count} strokes)")
    return {"action": "draw", "selector": selector, "stroke_count": stroke_count}



def _extract_ref(selector: str) -> str | None:
    """Extract a ref ID from selector strings the LLM might produce.

    Recognizes: "e5", "[ref='e5']", "[ref=e5]", "ref=e5", "[ref=\"e5\"]"
    Returns the bare ref ID (e.g. "e5") or None if not a ref pattern.
    """
    import re
    # Bare ref: "e5", "e12"
    if re.fullmatch(r'e\d+', selector):
        return selector
    # Bracket patterns: [ref='e5'], [ref=e5], [ref="e5"]
    m = re.search(r"ref=['\"]?(e\d+)['\"]?", selector)
    if m:
        return m.group(1)
    return None


async def _resolve_drag_selector(page, ref_map, selector: str):
    """Resolve a drag selector to a CSS selector that works in JS querySelector.

    Resolution order:
    1. Ref ID (e.g. "e5", "[ref='e5']") → [data-ref="e5"]
    2. Bare number (e.g. "5") → [data-ref="e5"]
    3. Text match against ref_map entries → [data-ref="eN"] for first match
    4. Pass through as-is (CSS selector or text for JS _resolveElement)
    """
    ref_id = _extract_ref(selector)
    if ref_id and ref_id in ref_map:
        return f'[data-ref="{ref_id}"]'
    # Bare number → e{N}
    if ref_id is None and not selector.startswith("e"):
        bare = f"e{selector}" if selector.isdigit() else None
        if bare and bare in ref_map:
            return f'[data-ref="{bare}"]'
    # Text match: find ref_map entry whose text matches the selector string
    needle = selector.strip()
    for rid, info in ref_map.items():
        if info.get("text", "").strip() == needle:
            return f'[data-ref="{rid}"]'
    return selector


async def _handle_drag_and_drop(page, ref_map, params):
    """Drag an element to a target via JS synthetic events.

    If the LLM passes ref IDs as selectors (e.g. "e5", "[ref='e5']"),
    resolves them to [data-ref="eN"] selectors that work in JS querySelector.

    Uses tools_helper.js simulateDrag() which fires three event strategies
    directly on the resolved elements (DragEvent, PointerEvent, MouseEvent),
    covering all common DnD library types. Works regardless of overlay state
    since events target the elements directly.

    After dispatching, checks whether the source element moved by comparing
    its bounding rect before and after. Reports movement in the result so
    the agent knows whether the drag had any effect.
    """
    # Accept from_ref/to_ref (new) or from_selector/to_selector (legacy)
    from_raw = params.get("from_ref") or params.get("from_selector", "")
    to_raw = params.get("to_ref") or params.get("to_selector", "")
    if not from_raw or not to_raw:
        raise ValueError("drag_and_drop requires from_ref and to_ref (or from_selector and to_selector)")
    from_selector = await _resolve_drag_selector(page, ref_map, from_raw)
    to_selector = await _resolve_drag_selector(page, ref_map, to_raw)

    # Execute drag and detect changes in a single JS call.
    # We mark the target with a temp attribute BEFORE the drag so we can
    # find it AFTER even if its text content changes (React DnD re-renders).
    result = await page.evaluate("""async (args) => {
        // Resolve source
        var src = document.querySelector(args.from);
        if (!src) return {error: 'source not found: ' + args.from};

        // Resolve target: CSS first, then text match
        var dst = null;
        try { dst = document.querySelector(args.to); } catch(e) {}
        if (!dst) {
            var text = args.to.trim();
            for (var c of document.querySelectorAll('*')) {
                if (c.children.length <= 1 && c.textContent.trim() === text) { dst = c; break; }
            }
        }

        // Snapshot pre-state
        var srcRect = src.getBoundingClientRect();
        var preSrc = {top: srcRect.top, left: srcRect.left,
                      parent: src.parentElement ? src.parentElement.className : ''};
        var preDstText = dst ? dst.textContent.trim() : null;
        var preDstChildren = dst ? dst.children.length : null;

        // Mark target so we can find it after content changes
        if (dst) dst.setAttribute('data-drag-target', '1');

        // Execute drag
        await window.__tools.simulateDrag(args.from, args.to);

        // Wait for React to process
        await new Promise(function(r) { setTimeout(r, 200); });

        // Post-check source
        var srcAfter = document.querySelector(args.from);
        var srcGone = !srcAfter;
        var postSrc = null;
        if (srcAfter) {
            var r2 = srcAfter.getBoundingClientRect();
            postSrc = {top: r2.top, left: r2.left,
                       parent: srcAfter.parentElement ? srcAfter.parentElement.className : ''};
        }

        // Post-check target via marker attribute
        var dstAfter = document.querySelector('[data-drag-target]');
        var postDstText = dstAfter ? dstAfter.textContent.trim() : null;
        var postDstChildren = dstAfter ? dstAfter.children.length : null;

        // Clean up marker
        if (dstAfter) dstAfter.removeAttribute('data-drag-target');

        return {
            preSrc: preSrc, postSrc: postSrc, srcGone: srcGone,
            preDstText: preDstText, postDstText: postDstText,
            preDstChildren: preDstChildren, postDstChildren: postDstChildren,
        };
    }""", {"from": from_selector, "to": to_selector})

    if "error" in result:
        raise ValueError(result["error"])

    # Detect success via source movement, reparenting, removal, OR target content change
    src_moved = False
    src_reparented = False
    src_removed = result.get("srcGone", False)
    target_changed = False

    pre_src = result.get("preSrc")
    post_src = result.get("postSrc")
    if pre_src and post_src:
        src_moved = (abs(pre_src["top"] - post_src["top"]) > 2 or
                     abs(pre_src["left"] - post_src["left"]) > 2)
        src_reparented = pre_src["parent"] != post_src["parent"]

    pre_dst_text = result.get("preDstText")
    post_dst_text = result.get("postDstText")
    if pre_dst_text is not None and post_dst_text is not None:
        target_changed = (pre_dst_text != post_dst_text or
                          result.get("preDstChildren") != result.get("postDstChildren"))

    success = src_moved or src_reparented or src_removed or target_changed
    if src_moved:
        effect = "moved"
    elif src_reparented:
        effect = "reparented"
    elif src_removed:
        effect = "source_removed"
    elif target_changed:
        effect = "target_changed"
    else:
        effect = "no_movement"

    logger.info(f"drag_and_drop({from_selector!r} -> {to_selector!r}): {effect}")

    if not success:
        raise ValueError(
            f"Drag had no visible effect — element did not move or change parent. "
            f"Try different from_ref/to_ref values, or try a different drag strategy."
        )

    return {"action": "drag_and_drop", "from": from_selector, "to": to_selector, "effect": effect}


async def _handle_decode_base64(page, ref_map, params):
    """Decode a base64-encoded string."""
    value = params.get("value", "")
    if not value:
        raise ValueError("decode_base64 requires a 'value' parameter")
    decoded = base64.b64decode(value).decode("utf-8", errors="replace")
    logger.info(f"decode_base64({value[:30]!r}...) -> {decoded[:50]!r}")
    return {"action": "decode_base64", "decoded": decoded}
