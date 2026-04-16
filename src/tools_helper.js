/**
 * JS Utility Layer — browser interaction primitives for the agent.
 * Injected via Playwright add_init_script().
 * Exposes window.__tools.{drawStrokes, simulateDrag, clickAt}.
 */
(function toolsHelper() {
  "use strict";

  /**
   * Click at specific coordinates within an element.
   * @param {string} selector - CSS selector for the target element
   * @param {number} x - X offset within the element
   * @param {number} y - Y offset within the element
   */
  function clickAt(selector, x, y) {
    var el = document.querySelector(selector);
    if (!el) throw new Error("clickAt: element not found: " + selector);
    var rect = el.getBoundingClientRect();
    var clientX = rect.left + x;
    var clientY = rect.top + y;
    var opts = { bubbles: true, clientX: clientX, clientY: clientY };
    el.dispatchEvent(new MouseEvent("mousedown", opts));
    el.dispatchEvent(new MouseEvent("mouseup", opts));
    el.dispatchEvent(new MouseEvent("click", opts));
  }

  /**
   * Draw strokes on a canvas element by dispatching mouse events.
   * Async with delays between phases so React can flush state updates
   * (e.g. isDrawing must be true before mousemove events are processed).
   * Events target the canvas element directly — overlays don't interfere.
   * @param {string} selector - CSS selector for the canvas
   * @param {Array<Array<[number,number]>>} strokes - Array of strokes, each an array of [x,y] points
   */
  async function drawStrokes(selector, strokes) {
    var canvas = document.querySelector(selector);
    if (!canvas) throw new Error("drawStrokes: element not found: " + selector);
    var rect = canvas.getBoundingClientRect();

    for (var s = 0; s < strokes.length; s++) {
      var points = strokes[s];
      if (points.length === 0) continue;

      var cx = rect.left + points[0][0];
      var cy = rect.top + points[0][1];

      // Fire both pointer and mouse events for maximum compatibility
      var startOpts = { bubbles: true, clientX: cx, clientY: cy, button: 0, pointerId: 1, pointerType: "mouse" };
      canvas.dispatchEvent(new PointerEvent("pointerdown", startOpts));
      canvas.dispatchEvent(new MouseEvent("mousedown", startOpts));

      // Let React flush state (e.g. setIsDrawing(true)) before move events
      await _delay(50);

      // mousemove/pointermove through all points
      for (var p = 1; p < points.length; p++) {
        var mx = rect.left + points[p][0];
        var my = rect.top + points[p][1];
        var moveOpts = { bubbles: true, clientX: mx, clientY: my, button: 0, pointerId: 1, pointerType: "mouse" };
        canvas.dispatchEvent(new PointerEvent("pointermove", moveOpts));
        canvas.dispatchEvent(new MouseEvent("mousemove", moveOpts));
      }

      // pointerup/mouseup at last point
      var last = points[points.length - 1];
      var ex = rect.left + last[0];
      var ey = rect.top + last[1];
      var endOpts = { bubbles: true, clientX: ex, clientY: ey, button: 0, pointerId: 1, pointerType: "mouse" };
      canvas.dispatchEvent(new PointerEvent("pointerup", endOpts));
      canvas.dispatchEvent(new MouseEvent("mouseup", endOpts));

      // Let React flush stroke count update before next stroke
      await _delay(50);
    }
  }

  /**
   * Small async delay to let React re-render between drag events.
   * @param {number} ms - Delay in milliseconds
   */
  function _delay(ms) {
    return new Promise(function(r) { setTimeout(r, ms); });
  }

  /**
   * Resolve an element by CSS selector, falling back to text content matching.
   * For short text (<=3 chars), scopes to draggable elements first.
   */
  function _resolveElement(selectorOrText) {
    // Try CSS selector first
    try {
      var el = document.querySelector(selectorOrText);
      if (el) return el;
    } catch (e) { /* invalid CSS, fall through to text matching */ }
    // Text-based matching — scope to draggable elements for short text
    var text = selectorOrText.trim();
    var area = document.body;
    if (text.length <= 3) {
      var draggables = area.querySelectorAll('[draggable="true"]');
      for (var i = 0; i < draggables.length; i++) {
        if (draggables[i].textContent.trim() === text) return draggables[i];
      }
    }
    // General text match — find element whose trimmed text content matches exactly
    var candidates = area.querySelectorAll('*');
    for (var j = 0; j < candidates.length; j++) {
      var c = candidates[j];
      if (c.children.length <= 1 && c.textContent.trim() === text) return c;
    }
    return null;
  }

  async function simulateDrag(fromSelector, toSelector) {
    var src = _resolveElement(fromSelector);
    var dst = _resolveElement(toSelector);
    if (!src) throw new Error("simulateDrag: source not found: " + fromSelector);
    if (!dst) throw new Error("simulateDrag: target not found: " + toSelector);

    var srcRect = src.getBoundingClientRect();
    var dstRect = dst.getBoundingClientRect();
    var srcX = srcRect.left + srcRect.width / 2;
    var srcY = srcRect.top + srcRect.height / 2;
    var dstX = dstRect.left + dstRect.width / 2;
    var dstY = dstRect.top + dstRect.height / 2;

    // Strategy 1: HTML5 Drag Events
    var dataTransfer = new DataTransfer();
    src.dispatchEvent(new DragEvent("dragstart", {
      bubbles: true, clientX: srcX, clientY: srcY, dataTransfer: dataTransfer,
    }));
    await _delay(50);
    dst.dispatchEvent(new DragEvent("dragenter", {
      bubbles: true, clientX: dstX, clientY: dstY, dataTransfer: dataTransfer,
    }));
    dst.dispatchEvent(new DragEvent("dragover", {
      bubbles: true, clientX: dstX, clientY: dstY, dataTransfer: dataTransfer,
    }));
    await _delay(50);
    dst.dispatchEvent(new DragEvent("drop", {
      bubbles: true, clientX: dstX, clientY: dstY, dataTransfer: dataTransfer,
    }));
    await _delay(50);
    src.dispatchEvent(new DragEvent("dragend", {
      bubbles: true, clientX: dstX, clientY: dstY, dataTransfer: dataTransfer,
    }));

    await _delay(30);

    // Strategy 2: Pointer Events
    src.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true, clientX: srcX, clientY: srcY, pointerId: 1,
    }));
    await _delay(30);
    var steps = 5;
    for (var i = 1; i <= steps; i++) {
      var mx = srcX + (dstX - srcX) * i / steps;
      var my = srcY + (dstY - srcY) * i / steps;
      document.elementFromPoint(mx, my) && document.elementFromPoint(mx, my).dispatchEvent(
        new PointerEvent("pointermove", {
          bubbles: true, clientX: mx, clientY: my, pointerId: 1,
        })
      );
    }
    await _delay(30);
    dst.dispatchEvent(new PointerEvent("pointerup", {
      bubbles: true, clientX: dstX, clientY: dstY, pointerId: 1,
    }));

    await _delay(30);

    // Strategy 3: Mouse Events
    src.dispatchEvent(new MouseEvent("mousedown", {
      bubbles: true, clientX: srcX, clientY: srcY,
    }));
    for (var j = 1; j <= steps; j++) {
      var mmx = srcX + (dstX - srcX) * j / steps;
      var mmy = srcY + (dstY - srcY) * j / steps;
      document.elementFromPoint(mmx, mmy) && document.elementFromPoint(mmx, mmy).dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true, clientX: mmx, clientY: mmy,
        })
      );
    }
    await _delay(30);
    dst.dispatchEvent(new MouseEvent("mouseup", {
      bubbles: true, clientX: dstX, clientY: dstY,
    }));
  }

  window.__tools = {
    clickAt: clickAt,
    drawStrokes: drawStrokes,
    simulateDrag: simulateDrag,
  };
})();
