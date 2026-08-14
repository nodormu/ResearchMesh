"""Registry of the client-executed tools.

Every module listed in MODULES exposes the same three names — `TOOLS` (Anthropic
tool schemas), `handles(name)`, and `await execute(name, input)` — so adding a
tool means writing one module and adding it here, rather than editing the chat
loop's declaration list and its routing chain separately.

Optional third-party packages are imported inside each module's `execute`, so a
tool whose dependency is missing declares itself normally and returns an install
hint if the model reaches for it.
"""

from core import browser
from core import claude_learned_schemas as learned
from core import computer
from core import config_edit
from core import data
from core import documents
from core import files
from core import kernel
from core import memory
from core import processes

MODULES = [
    learned,     # bash, text editor, web_search, web_fetch
    memory,      # cross-session memory (learned schema)
    computer,    # screen/mouse/keyboard control (learned schema, beta-gated)
    browser,     # Playwright DOM surfing
    documents,   # LibreOffice / pandoc conversion
    kernel,      # stateful IPython
    processes,   # pexpect interactive commands
    config_edit,  # comment-preserving YAML/TOML/JSON edits
    data,        # DuckDB
    files,       # trash
]

TOOLS = [tool for module in MODULES for tool in module.TOOLS]

_DUPLICATES = {
    name
    for name in (t["name"] for t in TOOLS)
    if [t["name"] for t in TOOLS].count(name) > 1
}
if _DUPLICATES:
    raise ValueError(f"duplicate local tool names: {sorted(_DUPLICATES)}")


def handles(name: str) -> bool:
    return any(module.handles(name) for module in MODULES)


async def execute(name: str, tool_input: dict) -> str | None:
    """Run a local tool, or return None if no local module owns that name."""
    for module in MODULES:
        if module.handles(name):
            return await module.execute(name, tool_input)
    return None


async def shutdown():
    """Release everything a local tool may have started. Safe if unused."""
    await browser.shutdown()
    await kernel.shutdown()
    data.close()
