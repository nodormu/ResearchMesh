import asyncio
import sys
import os
import tomllib
from anthropic import Anthropic
from contextlib import AsyncExitStack
from mcp_client import MCPClient
from core.claude import Claude
from core.chat import Chat
from core.cli import CliApp
from core import local_tools

# Anthropic Config
api_key = os.getenv("ANTHROPIC_API_KEY") # api key is in .bashrc file, which is why this is here
client = Anthropic(api_key=api_key)
# Configuration file (config.toml) — non-secret settings.
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.toml")


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


_config = _load_config()

# Claude model: config.toml [claude] model, overridable by the CLAUDE_MODEL env var.
claude_model = os.getenv("CLAUDE_MODEL") or _config.get("claude", {}).get(
    "model", "claude-sonnet-5"
)

# n8n MCP server (Streamable HTTP). The endpoint URL comes from config.toml so
# it isn't hardcoded; the N8N_MCP_URL environment variable overrides it when set.
# The Bearer token stays in the environment (N8N_MCP_TOKEN) — never in config.
_n8n_config = _config.get("n8n", {})
N8N_ENABLED = _n8n_config.get("enabled", True)  # default on
N8N_MCP_URL = os.getenv("N8N_MCP_URL") or _n8n_config.get("url")
N8N_MCP_TOKEN = os.getenv("N8N_MCP_TOKEN")  # your n8n Bearer token (env only)


def build_primary_client() -> MCPClient:
    if not N8N_MCP_URL:
        raise SystemExit(
            "No n8n URL configured. Set [n8n] url in config.toml, "
            "or the N8N_MCP_URL environment variable."
        )
    headers = (
        {"Authorization": f"Bearer {N8N_MCP_TOKEN}"} if N8N_MCP_TOKEN else None
    )
    return MCPClient(transport="http", url=N8N_MCP_URL, headers=headers)


async def _connect_n8n(stack: AsyncExitStack, clients: dict) -> bool:
    """Connect the n8n client and register it. On failure, print an actionable
    message (no traceback) and return False so the caller can exit cleanly."""
    n8n_client = build_primary_client()
    try:
        await n8n_client.connect()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        # A failed connect raises CancelledError from connect() and surfaces the
        # real cause (e.g. ConnectError) from cleanup(); swallow both quietly.
        try:
            await n8n_client.cleanup()
        except BaseException:
            pass
        print(
            f"\nCould not connect to the n8n MCP server at {N8N_MCP_URL}.\n"
            "  - Start (or restore network access to) your n8n server, then retry, or\n"
            "  - Set  enabled = false  under [n8n] in config.toml to run without it\n"
            "    (the app still works with its local tools: bash, editor, web,\n"
            "    browser, python, documents, config, sql, trash).",
            file=sys.stderr,
        )
        return False
    stack.push_async_callback(n8n_client.cleanup)
    clients["n8n"] = n8n_client
    return True


async def main():
    claude_service = Claude(model=claude_model)

    server_scripts = sys.argv[1:]
    clients = {}

    async with AsyncExitStack() as stack:
        if N8N_ENABLED:
            if not await _connect_n8n(stack, clients):
                return
        else:
            print("[n8n MCP disabled in config.toml — running with local tools only]")

        for i, server_script in enumerate(server_scripts):
            client_id = f"client_{i}_{server_script}"
            client = await stack.enter_async_context(
                MCPClient(command="python", args=[server_script])
            )

            clients[client_id] = client

        # Close anything a local tool started (browser, IPython kernel, DuckDB).
        stack.push_async_callback(local_tools.shutdown)

        chat = Chat(
            clients=clients,
            claude_service=claude_service,
        )

        cli = CliApp(chat)
        await cli.run()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
