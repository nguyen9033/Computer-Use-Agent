"""System prompt and tool definitions for the LLM.

Contains:
- SYSTEM_PROMPT: Generic web agent instructions for one-action-per-turn execution
- TOOLS: OpenAI function-calling format tool definitions
- Tool descriptions for click, fill, hover, scroll, wait, press,
  draw, drag_and_drop (8 tools)
"""

SYSTEM_PROMPT = """You are a web agent. You interact with web pages by observing DOM snapshots and calling tools.

PERCEPTION:
- You see a structured DOM snapshot of the current page
- Interactive elements are labeled ref=eN — use these refs in tool calls
- Elements marked COVERED are hidden behind overlays — check this before clicking
- Position and z-index annotations show element layering
- [NEW] marks elements that appeared since your last action

ACTIONS:
- Call ONE tool per turn, then observe the result and updated page before acting again
- Match the tool to the element: use draw() on <canvas>, drag_and_drop() on draggable items, click() on buttons/links

TOOLS:
- click(ref, reason, force?) — click an element; use force=true when COVERED
- fill(ref, value) — type into inputs or select dropdown options
- hover(ref, force?) — hover to reveal hidden content
- scroll(direction, amount) — scroll viewport or inner containers
- wait(timeout) — pause in milliseconds
- press(key) — keyboard input (Enter, ArrowUp, Escape, Tab, etc.)
- draw(selector, strokes) — draw on a <canvas> element by dispatching mouse/pointer events along coordinate paths
- drag_and_drop(from_ref, to_ref) — drag one element onto another; works on draggable="true" items, sortable lists, puzzle pieces

PRINCIPLES:
- Read the page before acting — understand what is being asked
- Observe each result before deciding your next action
- Never repeat an action that had no effect — try something different
- Ground every action in what you observe — never assume or invent page content

EXAMPLE: If you see ref=e5 COVERED, click it with force=true:
  click(ref="e5", reason="Submit button is COVERED by overlay", force=true)

ARGUMENT REFERENCE:
- draw(selector, strokes): selector is a CSS selector like "canvas" or "#myCanvas". strokes is an array of paths, each path is an array of [x,y] points — e.g. strokes=[[[10,10],[200,10]], [[10,50],[200,50]]] draws two horizontal lines.
- drag_and_drop(from_ref, to_ref): from_ref and to_ref are ref IDs like "e5", CSS selectors, or visible text labels."""


CORE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an element by its ref.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Element reference (e.g. 'e5')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why you are clicking this element and what you expect to happen.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Force-click through overlays. Use when element is marked COVERED in the snapshot.",
                    },
                },
                "required": ["ref", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fill",
            "description": "Fill a text input or select a dropdown option. For <select> elements, automatically uses select_option. For text inputs, replaces existing content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Element reference (e.g. 'e3')",
                    },
                    "value": {
                        "type": "string",
                        "description": "Text to fill or option value to select",
                    },
                },
                "required": ["ref", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hover",
            "description": "Hover over an element to reveal tooltips or hidden content. Use force=true to hover through overlays.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Element reference (e.g. 'e7')",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Force hover through overlays. Default false.",
                    },
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the page. Also scrolls inner scrollable containers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right"],
                        "description": "Direction to scroll",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Pixels to scroll (default 500)",
                        "default": 500,
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait for specified milliseconds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "integer",
                        "description": "Time to wait in milliseconds",
                    },
                },
                "required": ["timeout"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press",
            "description": "Press a keyboard key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key name: 'Enter', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Escape', 'Tab', 'Backspace', 'Space', or a single character like 'a'",
                    },
                },
                "required": ["key"],
            },
        },
    },
]

DRAW_TOOL = {
    "type": "function",
    "function": {
        "name": "draw",
        "description": "Draw on a <canvas> element by dispatching mouse/pointer events along coordinate paths. Each stroke is an array of [x,y] points relative to the canvas top-left corner.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the canvas element (e.g. 'canvas', '#myCanvas')",
                },
                "strokes": {
                    "type": "array",
                    "description": "Array of strokes. Each stroke is an array of [x,y] coordinate points forming a path.",
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "number"},
                        },
                    },
                },
            },
            "required": ["selector", "strokes"],
        },
    },
}

DRAG_AND_DROP_TOOL = {
    "type": "function",
    "function": {
        "name": "drag_and_drop",
        "description": "Drag an element and drop it onto another element. Works with draggable items, sortable lists, and puzzle pieces.",
        "parameters": {
            "type": "object",
            "properties": {
                "from_ref": {
                    "type": "string",
                    "description": "Ref ID (e.g. 'e5'), CSS selector, or text label of the element to drag",
                },
                "to_ref": {
                    "type": "string",
                    "description": "Ref ID (e.g. 'e8'), CSS selector, or text label of the drop target",
                },
            },
            "required": ["from_ref", "to_ref"],
        },
    },
}

DECODE_BASE64_TOOL = {
    "type": "function",
    "function": {
        "name": "decode_base64",
        "description": "Decode a base64-encoded string. Use when you see encoded text on the page that you need to read.",
        "parameters": {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "description": "The base64-encoded string to decode",
                },
            },
            "required": ["value"],
        },
    },
}

TOOL_DEFINITIONS = CORE_TOOLS + [DRAW_TOOL, DRAG_AND_DROP_TOOL, DECODE_BASE64_TOOL]


def get_tools_for_snapshot(snapshot: str) -> list:
    """Return tool definitions based on DOM snapshot content.

    Core tools (click, fill, hover, scroll, wait, press) are always included.
    draw is added when <canvas is detected.
    drag_and_drop is added when draggable is detected.
    """
    tools = list(CORE_TOOLS)
    snap_lower = snapshot.lower()
    if "<canvas" in snapshot or "stroke" in snap_lower or "draw" in snap_lower:
        tools.append(DRAW_TOOL)
    if "draggable" in snapshot or "drag" in snap_lower:
        tools.append(DRAG_AND_DROP_TOOL)
    if "base64" in snap_lower or "encoded" in snap_lower or "decode" in snap_lower:
        tools.append(DECODE_BASE64_TOOL)
    return tools


def get_tools_openai():
    """Return all tool definitions in OpenAI function-calling format."""
    return TOOL_DEFINITIONS
