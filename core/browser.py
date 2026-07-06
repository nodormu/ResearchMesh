"""Custom Playwright browser tool — DOM automation (no GUI, no raw HTTP).

Claude learns these tools from their descriptions at runtime. A single headless
browser page is kept alive across tool calls so multi-step flows work
(navigate -> fill -> click -> extract). Every tool trims what it returns to
keep responses out of firehose territory.

Requires:  pip install playwright  &&  playwright install chromium
"""

TOOLS = [
    {
        "name": "browser_navigate",
        "description": (
            "Open a URL in a headless browser and return the page title plus its "
            "trimmed visible text. Use this to read a web page, including "
            "JavaScript-rendered content, before extracting or interacting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute URL to open, including http:// or https://.",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_extract",
        "description": (
            "Return the text (and href, for links) of elements on the CURRENT page "
            "matching a CSS selector. Use after browser_navigate to pull out specific "
            "content instead of the whole page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector, e.g. 'h1', '.price', 'a.result'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of matches to return (default 20).",
                },
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_click",
        "description": (
            "Click the first element matching a CSS selector on the current page, "
            "then return the resulting page's title and trimmed text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the element to click.",
                }
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_fill",
        "description": (
            "Fill a form field (input or textarea) matching a CSS selector with the "
            "given value. Follow with browser_click to submit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the input/textarea.",
                },
                "value": {"type": "string", "description": "Text to enter."},
            },
            "required": ["selector", "value"],
        },
    },
]

_TOOL_NAMES = {t["name"] for t in TOOLS}
_MAX_TEXT = 6000

# Lazily-initialised singletons so importing this module never requires
# playwright to be installed until a browser tool is actually used.
_playwright = None
_browser = None
_page = None


def handles(name: str) -> bool:
    return name in _TOOL_NAMES


def _trim(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > _MAX_TEXT:
        return text[:_MAX_TEXT] + " …[truncated]"
    return text


async def _ensure_page():
    global _playwright, _browser, _page
    if _page is None:
        from playwright.async_api import async_playwright

        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
        _page = await _browser.new_page()
    return _page


async def execute(name: str, tool_input: dict) -> str:
    try:
        page = await _ensure_page()

        if name == "browser_navigate":
            await page.goto(tool_input["url"], wait_until="domcontentloaded")
            title = await page.title()
            body = await page.inner_text("body")
            return f"Title: {title}\n\n{_trim(body)}"

        if name == "browser_extract":
            selector = tool_input["selector"]
            limit = int(tool_input.get("limit", 20))
            elements = await page.query_selector_all(selector)
            out = []
            for el in elements[:limit]:
                text = (await el.inner_text()).strip()
                href = await el.get_attribute("href")
                out.append(text + (f"  [{href}]" if href else ""))
            if not out:
                return f"No elements matched selector {selector!r}"
            return _trim("\n".join(out))

        if name == "browser_click":
            await page.click(tool_input["selector"])
            await page.wait_for_load_state("domcontentloaded")
            title = await page.title()
            body = await page.inner_text("body")
            return f"Clicked. Now on: {title}\n\n{_trim(body)}"

        if name == "browser_fill":
            await page.fill(tool_input["selector"], tool_input["value"])
            return f"Filled {tool_input['selector']!r}"

        return f"Error: unknown browser tool {name!r}"

    except Exception as e:
        return f"Browser error in {name}: {e}"


async def shutdown():
    """Close the headless browser. Safe to call even if never launched."""
    global _playwright, _browser, _page
    if _browser is not None:
        await _browser.close()
    if _playwright is not None:
        await _playwright.stop()
    _playwright = _browser = _page = None
