"""DOM compact serializer, compression pipeline, ref system, and diff engine.

Converts the live page DOM into a compact text representation:
- serialize_dom(): JS-based DOM tree walker via page.evaluate()
- serialize_dom_v2(): Spatial reordering (MAIN/OVERLAY) + prominence + hidden elements
- compress_snapshot(): group similar buttons, strip layout classes
- trim_snapshot(): collapse repeated sections
- assign_refs(): mark interactive elements with ref=eN
- compute_diff_markers(): annotate new snapshot with [NEW] markers
- generate_page_summary(): compact page recap with all refs for diff mode
- compute_diff_sections(): unified-diff hunks for turn 2+ context
"""

import re
import difflib

# ---------------------------------------------------------------------------
# JS DOM serializer -- executed via page.evaluate()
# ---------------------------------------------------------------------------

_SERIALIZE_DOM_JS = r"""
(function() {
  const SKIP_TAGS = new Set([
    'script','style','svg','noscript','path','br','hr','meta','link'
  ]);

  const VOID_ELEMENTS = new Set([
    'area','base','br','col','embed','hr','img','input',
    'link','meta','param','source','track','wbr'
  ]);

  // --- computed style helpers ---------------------------------------------

  // Map RGB color to nearest named color for compact annotation
  const COLOR_MAP = [
    { name: 'red',    r: 239, g: 68,  b: 68  },
    { name: 'orange', r: 249, g: 115, b: 22  },
    { name: 'yellow', r: 234, g: 179, b: 8   },
    { name: 'green',  r: 34,  g: 197, b: 94  },
    { name: 'blue',   r: 59,  g: 130, b: 246 },
    { name: 'purple', r: 168, g: 85,  b: 247 },
    { name: 'pink',   r: 236, g: 72,  b: 153 },
    { name: 'gray',   r: 156, g: 163, b: 175 },
    { name: 'white',  r: 255, g: 255, b: 255 },
    { name: 'black',  r: 0,   g: 0,   b: 0   },
  ];

  function parseRgb(str) {
    const m = str.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (!m) return null;
    return { r: +m[1], g: +m[2], b: +m[3] };
  }

  function nearestColor(rgb) {
    if (!rgb) return null;
    // Transparent/near-transparent → skip
    if (rgb.r === 0 && rgb.g === 0 && rgb.b === 0) {
      // Could be black or transparent — check if it's actually rgba(0,0,0,0)
      return null; // will check bg separately
    }
    let best = null, bestDist = Infinity;
    for (const c of COLOR_MAP) {
      const d = (rgb.r-c.r)**2 + (rgb.g-c.g)**2 + (rgb.b-c.b)**2;
      if (d < bestDist) { bestDist = d; best = c.name; }
    }
    // Skip if too far from any named color (>100 euclidean)
    if (bestDist > 10000) return null;
    return best;
  }

  function getBgColor(cs) {
    const bg = cs.backgroundColor;
    if (!bg || bg === 'transparent' || bg === 'rgba(0, 0, 0, 0)') return null;
    const rgb = parseRgb(bg);
    return nearestColor(rgb);
  }

  // --- attribute collection ----------------------------------------------

  const ATTR_NAMES = [
    'id','role','draggable','type','placeholder','disabled','href',
    'aria-label','aria-expanded','aria-hidden','data-testid',
    'tabindex','contenteditable','checked','selected','value'
  ];

  // Attributes that are boolean (present = true)
  const BOOL_ATTRS = new Set(['disabled','checked','selected','contenteditable']);

  // Form elements where we include value
  const FORM_TAGS = new Set(['input','select','textarea']);

  function collectAttrs(el, tag) {
    const parts = [];

    // NO CSS classes — replaced by computed style annotations below

    for (const name of ATTR_NAMES) {
      // value only for form elements
      if (name === 'value' && !FORM_TAGS.has(tag)) continue;

      if (BOOL_ATTRS.has(name)) {
        if (el.hasAttribute(name)) parts.push(name);
        continue;
      }

      let val = el.getAttribute(name);
      if (val == null || val === '') continue;

      // Truncate href to 80 chars
      if (name === 'href' && val.length > 80) {
        val = val.slice(0, 80) + '...';
      }

      parts.push(name + '="' + val + '"');
    }

    // Computed style annotations — framework-agnostic
    try {
      const cs = window.getComputedStyle(el);

      // Position + z-index (for overlay/layering awareness)
      const pos = cs.position;
      if (pos === 'fixed' || pos === 'absolute') {
        const z = cs.zIndex;
        const zVal = (z === 'auto') ? '0' : z;
        parts.push('pos=' + pos + ' z=' + zVal);
      }

      // Background color (semantic: green=success, red=danger, etc.)
      const bg = getBgColor(cs);
      if (bg && bg !== 'white' && bg !== 'black') {
        parts.push('bg=' + bg);
      }

      // Cursor (interactability signal)
      const cursor = cs.cursor;
      if (cursor === 'pointer') parts.push('cursor=pointer');
      else if (cursor === 'grab' || cursor === 'move') parts.push('cursor=' + cursor);
      else if (cursor === 'not-allowed') parts.push('cursor=not-allowed');

      // Visibility
      if (cs.display === 'none') parts.push('hidden');
      if (cs.visibility === 'hidden') parts.push('invisible');
      if (parseFloat(cs.opacity) < 0.5) parts.push('dimmed');

      // Pointer-events (inert elements)
      if (cs.pointerEvents === 'none') parts.push('inert');
    } catch(e) {}

    return parts.length ? ' ' + parts.join(' ') : '';
  }

  // --- ref system -------------------------------------------------------

  // Clean stale data-ref attributes from previous snapshot
  document.querySelectorAll('[data-ref]').forEach(el => el.removeAttribute('data-ref'));

  const refs = [];
  let refCounter = 0;

  const INTERACTIVE_TAGS = new Set(['button','a','input','select','textarea']);
  const INTERACTIVE_ROLES = new Set([
    'button','link','textbox','checkbox','radio','combobox',
    'listbox','menuitem','tab','slider','switch','treeitem'
  ]);

  function shouldGetRef(el, tag, cs) {
    if (INTERACTIVE_TAGS.has(tag)) return true;
    if (el.getAttribute('draggable') === 'true') return true;
    if (cs) {
      const cursor = cs.cursor;
      if (cursor === 'pointer' || cursor === 'grab' || cursor === 'move') return true;
    }
    const role = el.getAttribute('role');
    if (role && INTERACTIVE_ROLES.has(role)) return true;
    if (el.getAttribute('onclick') || el.getAttribute('onmousedown') || el.getAttribute('ontouchstart')) return true;
    return false;
  }

  function getCssPath(el) {
    const parts = [];
    let current = el;
    while (current && current !== document.body) {
      let selector = current.tagName.toLowerCase();
      if (current.id) {
        selector += '#' + current.id;
        parts.unshift(selector);
        break;
      }
      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === current.tagName);
        if (siblings.length > 1) {
          const idx = siblings.indexOf(current) + 1;
          selector += ':nth-of-type(' + idx + ')';
        }
      }
      parts.unshift(selector);
      current = parent;
    }
    return parts.join(' > ');
  }

  // --- tree walk ---------------------------------------------------------

  function truncate(text, max) {
    if (text.length <= max) return text;
    return text.slice(0, max) + '...';
  }

  const lines = [];

  function walk(node, depth) {
    if (node.nodeType === 3) {
      // Text node
      const t = node.textContent.trim();
      if (t) {
        lines.push('  '.repeat(depth) + truncate(t, 100));
      }
      return;
    }

    if (node.nodeType !== 1) return; // only Element nodes

    const tag = node.tagName.toLowerCase();
    if (SKIP_TAGS.has(tag)) return;

    const attrs = collectAttrs(node, tag);

    // Gather direct text from child text nodes
    let directText = '';
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        const t = child.textContent.trim();
        if (t) directText += (directText ? ' ' : '') + t;
      }
    }

    // Check if element has child elements
    let hasChildElements = false;
    for (const child of node.childNodes) {
      if (child.nodeType === 1 && !SKIP_TAGS.has(child.tagName.toLowerCase())) {
        hasChildElements = true;
        break;
      }
    }

    // Skip empty elements: no text, no child elements, no meaningful attributes
    // Exception: canvas and audio elements are inherently empty but meaningful
    const KEEP_EMPTY = new Set(['canvas', 'audio', 'video']);
    if (!directText && !hasChildElements && !attrs.trim() && !KEEP_EMPTY.has(tag)) return;

    const indent = '  '.repeat(depth);

    // Check if element should get a ref
    let refAttr = '';
    try {
      const cs = window.getComputedStyle(node);
      if (shouldGetRef(node, tag, cs) || KEEP_EMPTY.has(tag)) {
        refCounter++;
        const refId = 'e' + refCounter;
        const fillable = (tag === 'input' || tag === 'textarea' || tag === 'select'
          || node.getAttribute('contenteditable') === 'true');
        refAttr = ' ref=' + refId + (fillable ? '[fillable]' : '');
        node.setAttribute('data-ref', refId);
        refs.push({
          id: refId,
          css: getCssPath(node),
          tag: tag,
          text: (directText || '').slice(0, 50).trim()
        });
      }
    } catch(e) {}

    // Build the opening tag line with inline direct text
    // ref= appears before other attrs for LLM visibility
    let line = indent + '<' + tag + refAttr + attrs + '>';
    if (directText) {
      line += truncate(directText, 100);
    }
    lines.push(line);

    // Void elements have no children to walk
    if (VOID_ELEMENTS.has(tag)) return;

    // Walk child elements (text nodes already captured as directText)
    for (const child of node.childNodes) {
      if (child.nodeType === 1) {
        walk(child, depth + 1);
      }
    }
  }

  if (document.body) {
    walk(document.body, 0);
  }

  return JSON.stringify({html: lines.join('\n'), refs: refs});
})()
"""


_CHECK_COVERAGE_JS = r"""
(function(selectors) {
    // selectors: { refId: cssPath, ... }
    // Returns: list of refIds that are covered by another element
    const covered = [];
    for (const [refId, css] of Object.entries(selectors)) {
        try {
            const el = document.querySelector(css);
            if (!el) continue;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            const topEl = document.elementFromPoint(cx, cy);
            if (!topEl) continue;
            // If the element at center is not our target (or a descendant/ancestor),
            // then something is covering it
            if (topEl !== el && !el.contains(topEl) && !topEl.contains(el)) {
                covered.push(refId);
            }
        } catch(e) {}
    }
    return covered;
})
"""


async def serialize_dom(page):
    """Serialize the page DOM into compact text with ref annotations.

    Calls page.evaluate() with a JS DOM walker that:
    - Walks depth-first from document.body
    - Skips non-visual tags (script, style, svg, etc.)
    - Strips layout/utility CSS classes, keeps semantic ones
    - Includes key attributes (id, role, aria-*, data-*, etc.)
    - Annotates positioned elements with pos=/z= pseudo-attrs
    - Assigns ref=eN to interactive elements (buttons, links, inputs,
      draggable elements, elements with pointer/grab/move cursor, etc.)
    - Truncates text to 100 chars, href to 80 chars
    - Omits closing tags to save tokens
    - Skips empty elements with no text/children/attributes
    - Checks element coverage via elementFromPoint and annotates
      covered elements with COVERED in the snapshot

    Returns:
        tuple: (snapshot_str, ref_map) where snapshot_str is the compact
        DOM text (typically 3,000-6,000 chars) and ref_map maps ref IDs
        like "e1" to dicts with "css", "tag", "text" keys.
    """
    import json
    raw = await page.evaluate(_SERIALIZE_DOM_JS)
    data = json.loads(raw)
    ref_map = {}
    for r in data.get('refs', []):
        ref_map[r['id']] = {
            'css': r['css'],
            'tag': r['tag'],
            'text': r['text'],
        }

    # Check which ref'd elements are covered by overlays
    html = data['html']
    covered_list = []
    if ref_map:
        selectors = {rid: info['css'] for rid, info in ref_map.items()}
        try:
            covered_list = await page.evaluate(_CHECK_COVERAGE_JS, selectors)
            # Annotate covered elements inline: ref=eN → ref=eN COVERED
            # Prominent standalone annotation right after ref (before size/attrs)
            # Handles: ref=eN>, ref=eN ..., ref=eN[fillable]>, ref=eN[fillable] ...
            for rid in sorted(covered_list, key=len, reverse=True):
                html = re.sub(
                    rf'(ref={rid}(?:\[fillable\])?)(?=[ >])',
                    rf'\1 COVERED',
                    html,
                )
            # Mark covered refs in ref_map so action layer can check
            for rid in covered_list:
                if rid in ref_map:
                    ref_map[rid]['covered'] = True
        except Exception:
            pass  # coverage check is best-effort

    return html, ref_map


# ---------------------------------------------------------------------------
# Compression pipeline
# ---------------------------------------------------------------------------

def group_similar_buttons(text: str) -> str:
    """Group 4+ consecutive absolute-positioned buttons into summary lines."""
    lines = text.split('\n')
    result = []
    i = 0
    # Pattern: lines like "  <button ... pos=absolute z=N>Label" or "  <a ... pos=absolute ...>Label"
    button_re = re.compile(r'^(\s*)<(?:button|a)\b[^>]*pos=(?:absolute|fixed)[^>]*>(.*)$')

    while i < len(lines):
        # Try to find a run of button lines
        run_start = i
        labels = []
        indent = None
        pos_info = None

        while i < len(lines):
            m = button_re.match(lines[i])
            if m:
                if indent is None:
                    indent = m.group(1)
                    # Extract pos and z info from the line
                    pz = re.search(r'pos=\S+ z=\S+', lines[i])
                    pos_info = pz.group(0) if pz else 'pos=absolute'
                label = m.group(2).strip()
                if label:
                    labels.append(label)
                i += 1
            else:
                break

        if len(labels) >= 4:
            # Collapse into summary
            shown = ', '.join(labels[:5])
            if len(labels) > 5:
                shown += ', ...'
            result.append(f'{indent}[{len(labels)} buttons {pos_info}: {shown}]')
        else:
            # Not enough for a group, keep original lines
            for j in range(run_start, i):
                result.append(lines[j])

        if i == run_start:
            result.append(lines[i])
            i += 1

    return '\n'.join(result)


def trim_snapshot(text: str) -> str:
    """Collapse 3+ repetitions of similar lines or multi-line blocks."""
    lines = text.split('\n')

    def normalize(line):
        """Replace digits with N and refs with N for comparison."""
        s = re.sub(r'\d+', 'N', line)
        s = re.sub(r'ref=e\w+', 'ref=eN', s)
        return s

    result = []
    i = 0
    while i < len(lines):
        collapsed = False
        # Try block sizes 4, 3, 2, 1 (greedy: larger blocks first)
        for block_size in [4, 3, 2, 1]:
            if i + block_size * 3 > len(lines):
                continue
            # Normalize the candidate block
            block_norm = tuple(normalize(lines[i + j]) for j in range(block_size))
            # Count consecutive repetitions of this block
            reps = 1
            pos = i + block_size
            while pos + block_size <= len(lines):
                next_norm = tuple(normalize(lines[pos + j]) for j in range(block_size))
                if next_norm == block_norm:
                    reps += 1
                    pos += block_size
                else:
                    break
            if reps >= 3:
                # Keep first 2 blocks, collapse rest
                for j in range(block_size * 2):
                    result.append(lines[i + j])
                remaining = reps - 2
                result.append(f'  [... {remaining} more similar sections]')
                i = pos
                collapsed = True
                break
        if not collapsed:
            result.append(lines[i])
            i += 1

    return '\n'.join(result)


_OVERLAY_RE = re.compile(r'pos=(?:fixed|absolute)')
_CLOSE_DISMISS_RE = re.compile(
    r'(?:Close|Dismiss|X|Cancel|Got it|OK|Accept)', re.IGNORECASE
)
_REF_RE = re.compile(r'ref=(e\d+)')

# Minimum number of overlay container lines before collapsing kicks in.
_OVERLAY_COLLAPSE_THRESHOLD = 3


def collapse_overlays(text: str) -> str:
    """Collapse pos=fixed/absolute overlay sections into a summary line.

    Walks lines top-down. When a line at indent level N contains
    pos=fixed or pos=absolute, everything at indent > N (its children)
    is suppressed. Overlay container lines themselves are also suppressed.
    At the end, a summary line is emitted listing the count and any
    useful close/dismiss refs found in the overlays.

    If fewer than _OVERLAY_COLLAPSE_THRESHOLD overlay containers are found,
    returns the text unchanged (not worth collapsing).
    """
    lines = text.split('\n')
    main_lines = []
    overlay_count = 0
    overlay_refs = []  # (ref_id, label_text) for close/dismiss refs

    # Track overlay suppression state
    in_overlay = False
    overlay_indent = 0  # indent level of the overlay container

    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        # Check if this line starts an overlay container
        is_overlay_line = bool(_OVERLAY_RE.search(line))

        if in_overlay:
            # If indent <= overlay container indent, we've exited the overlay
            if indent <= overlay_indent and stripped:
                in_overlay = False
                # Fall through to check if this new line is also an overlay
            else:
                # Still inside overlay subtree — collect useful refs, suppress
                ref_m = _REF_RE.search(line)
                if ref_m and _CLOSE_DISMISS_RE.search(line):
                    # Extract label text after >
                    label = ''
                    if '>' in line:
                        label = line.split('>')[-1].strip()
                    overlay_refs.append((ref_m.group(1), label))
                continue

        if is_overlay_line:
            overlay_count += 1
            in_overlay = True
            overlay_indent = indent
            # Collect refs on the container line itself
            ref_m = _REF_RE.search(line)
            if ref_m and _CLOSE_DISMISS_RE.search(line):
                label = ''
                if '>' in line:
                    label = line.split('>')[-1].strip()
                overlay_refs.append((ref_m.group(1), label))
            continue

        main_lines.append(line)

    # Don't collapse if too few overlays
    if overlay_count < _OVERLAY_COLLAPSE_THRESHOLD:
        return text

    # Build summary line
    parts = [f'[{overlay_count} overlay elements']
    if overlay_refs:
        ref_strs = [f'{rid}="{lbl}"' if lbl else rid for rid, lbl in overlay_refs]
        parts.append(f'dismiss: {", ".join(ref_strs)}')
    parts_str = ' — '.join(parts) + ']'
    main_lines.append(parts_str)

    return '\n'.join(main_lines)


def compress_snapshot(raw_dom: str) -> str:
    """Apply full compression pipeline to serialized DOM.

    Steps:
    1. Group similar buttons (decoy button collapsing)
    2. Trim repeated sections (filler content collapsing)

    Note: collapse_overlays was removed — the agent should see all page
    content and reason about overlays using pos=/z= and COVERED annotations.

    Returns compressed snapshot string.
    """
    text = group_similar_buttons(raw_dom)
    text = trim_snapshot(text)
    return text


def compute_diff_markers(old_snapshot: str, new_snapshot: str) -> str:
    """Annotate new snapshot lines with [NEW] markers for lines not in old snapshot.

    Computes line-level set difference and appends '  [NEW]' to lines in
    new_snapshot that weren't present in old_snapshot. Also prepends a
    brief change summary line.

    Args:
        old_snapshot: Previous turn's snapshot string
        new_snapshot: Current turn's snapshot string

    Returns:
        The new_snapshot with [NEW] markers on changed/added lines,
        plus a CHANGES summary line at the top.
    """
    old_lines = set(old_snapshot.split('\n'))
    new_lines = new_snapshot.split('\n')

    added = 0
    removed = len(old_lines - set(new_lines))
    marked = []

    for line in new_lines:
        if line not in old_lines:
            marked.append(line + '  [NEW]')
            added += 1
        else:
            marked.append(line)

    if added > 0 or removed > 0:
        summary = f'CHANGES: +{added} new, -{removed} removed'
        return summary + '\n' + '\n'.join(marked)
    else:
        return '\n'.join(marked)


# ---------------------------------------------------------------------------
# Diff-only snapshot mode (turn 2+)
# ---------------------------------------------------------------------------

def generate_page_summary(snapshot: str, ref_map: dict, task_text: str = "") -> str:
    """Generate a compact page summary with all interactive ref IDs.

    Lists all interactive elements compactly (eN=tag "text"), partitioned
    into main content vs overlay refs. Optionally includes a task description
    if provided by the caller (from the model's objective output).

    Called each turn with current ref_map (refs may shift as DOM mutates).
    Typically produces 500-1000 chars vs 12-14K for full snapshot.

    Args:
        snapshot: Compressed DOM snapshot string
        ref_map: Dict mapping ref IDs to {css, tag, text} dicts
        task_text: Optional task description (from model objective)

    Returns:
        Compact summary string for diff-mode context.
    """
    lines = snapshot.split('\n')

    # Identify which refs appear on overlay lines (pos=fixed/absolute)
    overlay_ref_ids = set()
    overlay_re = re.compile(r'pos=(?:fixed|absolute)')
    ref_re = re.compile(r'ref=(e\d+)')
    # Walk lines: if a line has pos=fixed/absolute, any ref on it is overlay.
    # Also track overlay container indent to mark children.
    in_overlay = False
    overlay_indent = 0
    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        is_overlay_line = bool(overlay_re.search(line))

        if in_overlay:
            if indent <= overlay_indent and stripped:
                in_overlay = False
            else:
                rm = ref_re.search(line)
                if rm:
                    overlay_ref_ids.add(rm.group(1))
                continue

        if is_overlay_line:
            in_overlay = True
            overlay_indent = indent
            rm = ref_re.search(line)
            if rm:
                overlay_ref_ids.add(rm.group(1))
            continue

    # Build compact ref lists, partitioned: main vs overlay
    def _build_ref_list(ref_ids_subset):
        """Build deduped ref list for a subset of ref IDs."""
        parts = []
        seen_groups = {}
        for ref_id in sorted(ref_ids_subset, key=lambda r: int(r[1:]) if r[1:].isdigit() else 0):
            if ref_id not in ref_map:
                continue
            info = ref_map[ref_id]
            tag = info.get('tag', '?')
            text = info.get('text', '').strip()
            if len(text) > 30:
                text = text[:27] + '...'
            key = (tag, text)
            if key not in seen_groups:
                seen_groups[key] = []
            seen_groups[key].append(ref_id)

        for (tag, text), rids in seen_groups.items():
            if len(rids) > 3:
                label = f'{tag} "{text}"' if text else tag
                parts.append(f'[{len(rids)}x {label}: {rids[0]}..{rids[-1]}]')
            else:
                for rid in rids:
                    if text:
                        parts.append(f'{rid}={tag} "{text}"')
                    else:
                        parts.append(f'{rid}={tag}')
        return parts

    main_ref_ids = [r for r in ref_map if r not in overlay_ref_ids]
    overlay_ref_id_list = [r for r in ref_map if r in overlay_ref_ids]

    main_parts = _build_ref_list(main_ref_ids)
    overlay_parts = _build_ref_list(overlay_ref_id_list)

    # Assemble summary
    result = ["PAGE SUMMARY:"]
    if task_text:
        result.append(f"Task: {task_text}")
    if main_parts:
        result.append("Interactive: " + " | ".join(main_parts))
    if overlay_parts:
        result.append(
            f"OVERLAYS ({len(overlay_ref_ids)} refs): "
            + " | ".join(overlay_parts)
        )

    return "\n".join(result)


_PAGE_UNCHANGED_MSG = (
    "PAGE UNCHANGED since last turn. Previous action had no visible effect.\n"
    "Try a COMPLETELY different approach — different elements, different interaction."
)


def compute_diff_sections(
    old_snapshot: str,
    new_snapshot: str,
    context_lines: int = 2,
) -> str | None:
    """Compute compact unified diff between two snapshots.

    Returns only changed sections with surrounding context lines,
    formatted for LLM consumption.

    Args:
        old_snapshot: Previous turn's compressed snapshot
        new_snapshot: Current turn's compressed snapshot
        context_lines: Lines of unchanged context around each hunk

    Returns:
        Formatted diff string with change hunks, or
        _PAGE_UNCHANGED_MSG if snapshots are identical, or
        None if >50% of new lines are additions (caller should
        fall back to sending the full snapshot).
    """
    old_lines = old_snapshot.split('\n')
    new_lines = new_snapshot.split('\n')

    # Quick identity check
    if old_lines == new_lines:
        return _PAGE_UNCHANGED_MSG

    # Generate unified diff hunks
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        n=context_lines,
        lineterm='',
    ))

    # Parse hunks and count changes
    hunks = []
    current_hunk = []
    added = 0
    removed = 0

    for line in diff_lines:
        # Skip file headers (--- / +++)
        if line.startswith('---') or line.startswith('+++'):
            continue
        if line.startswith('@@'):
            if current_hunk:
                hunks.append('\n'.join(current_hunk))
            current_hunk = [line]
        elif current_hunk is not None:
            current_hunk.append(line)
            if line.startswith('+') and not line.startswith('+++'):
                added += 1
            elif line.startswith('-') and not line.startswith('---'):
                removed += 1

    if current_hunk:
        hunks.append('\n'.join(current_hunk))

    # No actual hunks found (shouldn't happen if lines differ, but be safe)
    if not hunks:
        return _PAGE_UNCHANGED_MSG

    # Fallback: if diff is too large, caller should send full snapshot
    if added > len(new_lines) * 0.5:
        return None

    header = f"CHANGES SINCE LAST TURN (+{added} added, -{removed} removed):"
    return header + "\n" + "\n\n".join(hunks)


# ---------------------------------------------------------------------------
# V2 DOM serializer — spatial reordering + prominence + hidden elements
# ---------------------------------------------------------------------------

_SERIALIZE_DOM_V2_JS = r"""
(function() {
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  const SKIP_TAGS = new Set([
    'script','style','svg','noscript','path','br','hr','meta','link'
  ]);
  const VOID_ELEMENTS = new Set([
    'area','base','br','col','embed','hr','img','input',
    'link','meta','param','source','track','wbr'
  ]);

  // --- color helpers (same as v1) ---
  const COLOR_MAP = [
    { name: 'red',    r: 239, g: 68,  b: 68  },
    { name: 'orange', r: 249, g: 115, b: 22  },
    { name: 'yellow', r: 234, g: 179, b: 8   },
    { name: 'green',  r: 34,  g: 197, b: 94  },
    { name: 'blue',   r: 59,  g: 130, b: 246 },
    { name: 'purple', r: 168, g: 85,  b: 247 },
    { name: 'pink',   r: 236, g: 72,  b: 153 },
    { name: 'gray',   r: 156, g: 163, b: 175 },
    { name: 'white',  r: 255, g: 255, b: 255 },
    { name: 'black',  r: 0,   g: 0,   b: 0   },
  ];

  function parseRgb(str) {
    const m = str.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (!m) return null;
    return { r: +m[1], g: +m[2], b: +m[3] };
  }

  function nearestColor(rgb) {
    if (!rgb) return null;
    if (rgb.r === 0 && rgb.g === 0 && rgb.b === 0) return null;
    let best = null, bestDist = Infinity;
    for (const c of COLOR_MAP) {
      const d = (rgb.r-c.r)**2 + (rgb.g-c.g)**2 + (rgb.b-c.b)**2;
      if (d < bestDist) { bestDist = d; best = c.name; }
    }
    if (bestDist > 10000) return null;
    return best;
  }

  function getBgColor(cs) {
    const bg = cs.backgroundColor;
    if (!bg || bg === 'transparent' || bg === 'rgba(0, 0, 0, 0)') return null;
    return nearestColor(parseRgb(bg));
  }

  // --- attribute collection ---
  const ATTR_NAMES = [
    'id','role','draggable','type','placeholder','disabled','href',
    'aria-label','aria-expanded','aria-hidden','data-testid',
    'tabindex','contenteditable','checked','selected','value'
  ];
  const BOOL_ATTRS = new Set(['disabled','checked','selected','contenteditable']);
  const FORM_TAGS = new Set(['input','select','textarea']);

  function collectAttrs(el, tag) {
    const parts = [];
    for (const name of ATTR_NAMES) {
      if (name === 'value' && !FORM_TAGS.has(tag)) continue;
      if (BOOL_ATTRS.has(name)) {
        if (el.hasAttribute(name)) parts.push(name);
        continue;
      }
      let val = el.getAttribute(name);
      if (val == null || val === '') continue;
      if (name === 'href' && val.length > 80) val = val.slice(0, 80) + '...';
      parts.push(name + '="' + val + '"');
    }

    try {
      const cs = window.getComputedStyle(el);
      const pos = cs.position;
      if (pos === 'fixed' || pos === 'absolute') {
        const z = (cs.zIndex === 'auto') ? '0' : cs.zIndex;
        parts.push('pos=' + pos + ' z=' + z);
      }
      const bg = getBgColor(cs);
      if (bg && bg !== 'white' && bg !== 'black') parts.push('bg=' + bg);
      const cursor = cs.cursor;
      if (cursor === 'pointer') parts.push('cursor=pointer');
      else if (cursor === 'grab' || cursor === 'move') parts.push('cursor=' + cursor);
      else if (cursor === 'not-allowed') parts.push('cursor=not-allowed');
      if (parseFloat(cs.opacity) < 0.5 && parseFloat(cs.opacity) >= 0.1) parts.push('dimmed');
      if (cs.pointerEvents === 'none') parts.push('inert');
    } catch(e) {}

    return parts.length ? ' ' + parts.join(' ') : '';
  }

  // --- ref system ---
  const INTERACTIVE_TAGS = new Set(['button','a','input','select','textarea']);
  const INTERACTIVE_ROLES = new Set([
    'button','link','textbox','checkbox','radio','combobox',
    'listbox','menuitem','tab','slider','switch','treeitem'
  ]);

  function shouldGetRef(el, tag, cs) {
    if (INTERACTIVE_TAGS.has(tag)) return true;
    if (el.getAttribute('draggable') === 'true') return true;
    if (cs) {
      const cursor = cs.cursor;
      if (cursor === 'pointer' || cursor === 'grab' || cursor === 'move') return true;
    }
    const role = el.getAttribute('role');
    if (role && INTERACTIVE_ROLES.has(role)) return true;
    if (el.getAttribute('onclick') || el.getAttribute('onmousedown') || el.getAttribute('ontouchstart')) return true;
    return false;
  }

  function getCssPath(el) {
    const parts = [];
    let current = el;
    while (current && current !== document.body) {
      let selector = current.tagName.toLowerCase();
      if (current.id) {
        selector += '#' + current.id;
        parts.unshift(selector);
        break;
      }
      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === current.tagName);
        if (siblings.length > 1) {
          const idx = siblings.indexOf(current) + 1;
          selector += ':nth-of-type(' + idx + ')';
        }
      }
      parts.unshift(selector);
      current = parent;
    }
    return parts.join(' > ');
  }

  // --- state ---
  document.querySelectorAll('[data-ref]').forEach(el => el.removeAttribute('data-ref'));

  const refs = [];
  let refCounter = 0;
  const mainLines = [];
  const overlayLines = [];

  function truncate(text, max) {
    if (text.length <= max) return text;
    return text.slice(0, max) + '...';
  }

  // --- walk with region routing ---
  function walk(node, depth, target, baseDepth) {
    if (node.nodeType === 3) {
      // Text node — only if parent walk didn't capture as directText
      // (dead code path in normal usage, kept for safety)
      return;
    }
    if (node.nodeType !== 1) return;

    const tag = node.tagName.toLowerCase();
    if (SKIP_TAGS.has(tag)) return;

    let cs;
    try { cs = window.getComputedStyle(node); } catch(e) { return; }

    // Hidden elements: include with [hidden] if they have text content
    const isHidden = cs.display === 'none' || cs.visibility === 'hidden'
                     || parseFloat(cs.opacity) < 0.1;
    if (isHidden) {
      const text = (node.textContent || '').trim();
      if (text && text.length > 1) {
        const indent = '  '.repeat(Math.max(0, depth - baseDepth));
        target.push(indent + '<' + tag + ' [hidden]>' + truncate(text, 200));
      }
      return; // Don't walk children of hidden elements
    }

    // Region detection: switch to overlay section
    let outTarget = target;
    let outBase = baseDepth;
    const isOverlayRoot = cs.position === 'fixed'
      || (cs.position === 'absolute' && parseInt(cs.zIndex || '0') > 100);
    if (isOverlayRoot && target === mainLines) {
      outTarget = overlayLines;
      outBase = depth; // Reset indentation for overlay section
    }

    const attrs = collectAttrs(node, tag);

    // Gather direct text from child text nodes
    let directText = '';
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        const t = child.textContent.trim();
        if (t) directText += (directText ? ' ' : '') + t;
      }
    }

    // Check for child elements
    let hasChildElements = false;
    for (const child of node.childNodes) {
      if (child.nodeType === 1 && !SKIP_TAGS.has(child.tagName.toLowerCase())) {
        hasChildElements = true;
        break;
      }
    }

    // Skip empty elements
    // Exception: canvas and audio elements are inherently empty but meaningful
    const KEEP_EMPTY = new Set(['canvas', 'audio', 'video']);
    if (!directText && !hasChildElements && !attrs.trim() && !KEEP_EMPTY.has(tag)) return;

    const indent = '  '.repeat(Math.max(0, depth - outBase));

    // Ref assignment with prominence
    let refAttr = '';
    try {
      if (shouldGetRef(node, tag, cs) || KEEP_EMPTY.has(tag)) {
        refCounter++;
        const refId = 'e' + refCounter;
        const fillable = (tag === 'input' || tag === 'textarea' || tag === 'select'
          || node.getAttribute('contenteditable') === 'true');
        refAttr = ' ref=' + refId + (fillable ? '[fillable]' : '');

        // Prominence: WxH + PROMINENT flag
        const rect = node.getBoundingClientRect();
        const w = Math.round(rect.width);
        const h = Math.round(rect.height);
        if (w > 0 && h > 0) {
          refAttr += ' ' + w + 'x' + h;
          const area = w * h;
          const fontSize = parseFloat(cs.fontSize) || 16;
          if (area > vw * vh * 0.01 && fontSize >= 16) {
            refAttr += ' PROMINENT';
          }
        }

        node.setAttribute('data-ref', refId);
        refs.push({
          id: refId,
          css: getCssPath(node),
          tag: tag,
          text: (directText || '').slice(0, 50).trim()
        });
      }
    } catch(e) {}

    // Build line: ref before attrs for LLM visibility
    let line = indent + '<' + tag + refAttr + attrs + '>';
    if (directText) line += truncate(directText, 100);
    outTarget.push(line);

    if (VOID_ELEMENTS.has(tag)) return;

    // Walk child elements
    for (const child of node.childNodes) {
      if (child.nodeType === 1) {
        walk(child, depth + 1, outTarget, outBase);
      }
    }
  }

  if (document.body) {
    walk(document.body, 0, mainLines, 0);
  }

  return JSON.stringify({
    main: mainLines.join('\n'),
    overlay: overlayLines.join('\n'),
    refs: refs
  });
})()
"""


async def serialize_dom_v2(page):
    """Serialize DOM with spatial reordering, prominence, and hidden elements.

    Like serialize_dom() but with three enhancements:
    1. MAIN/OVERLAY split — main content renders first, overlays second.
       Overlay = position:fixed OR position:absolute with z-index > 100.
    2. Prominence — interactive elements get WxH size + PROMINENT flag.
    3. Hidden elements — display:none/visibility:hidden included with [hidden].

    Returns:
        tuple: (snapshot_str, ref_map)
    """
    import json
    raw = await page.evaluate(_SERIALIZE_DOM_V2_JS)
    data = json.loads(raw)

    ref_map = {}
    for r in data.get('refs', []):
        ref_map[r['id']] = {
            'css': r['css'],
            'tag': r['tag'],
            'text': r['text'],
        }

    # Assemble: MAIN first, OVERLAY second
    main = data['main']
    overlay = data['overlay']

    parts = []
    if main:
        parts.append("=== MAIN ===")
        parts.append(main)
    if overlay:
        parts.append("")
        parts.append("=== OVERLAYS ===")
        parts.append(overlay)

    html = '\n'.join(parts)

    # Coverage check (same as v1)
    covered_list = []
    if ref_map:
        selectors = {rid: info['css'] for rid, info in ref_map.items()}
        try:
            covered_list = await page.evaluate(_CHECK_COVERAGE_JS, selectors)
            # Annotate covered elements inline: ref=eN → ref=eN COVERED
            for rid in sorted(covered_list, key=len, reverse=True):
                html = re.sub(
                    rf'(ref={rid}(?:\[fillable\])?)(?=[ >])',
                    rf'\1 COVERED',
                    html,
                )
            for rid in covered_list:
                if rid in ref_map:
                    ref_map[rid]['covered'] = True
        except Exception:
            pass

    return html, ref_map
