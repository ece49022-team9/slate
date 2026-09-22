from datetime import datetime

from slate.browser.tools import (
    browser_act,
    browser_activity,
    browser_clear_activity,
    browser_observe,
    browser_open,
)


def get_current_time():
    return datetime.now().astimezone().isoformat()


TOOLS = {
    "get_current_time": lambda args: get_current_time(),
    "browser_open": browser_open,
    "browser_observe": browser_observe,
    "browser_act": browser_act,
    "browser_activity": browser_activity,
    "browser_clear_activity": browser_clear_activity,
}


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current local date and time.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": "Open a URL in the browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to open.",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_observe",
            "description": (
                "Inspect the current browser page and return its URL, "
                "title, visible text, and interactive elements."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_act",
            "description": (
                "Interact with the current webpage. Use this for clicking, "
                "typing, pressing keys, scrolling, going back, or going forward."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "click",
                            "type",
                            "press",
                            "scroll",
                            "back",
                            "forward",
                        ],
                    },
                    "element_id": {
                        "type": "integer",
                        "description": (
                            "The interactive element ID from browser_observe. "
                            "Required for click and type."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type, or scroll amount.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Keyboard key to press, such as Enter.",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_activity",
            "description": "Get the recent actions performed by the browser.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_clear_activity",
            "description": "Clear the browser activity history.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]
