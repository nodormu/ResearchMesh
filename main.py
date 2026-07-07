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
from core import browser

# Anthropic Config
api_key = os.getenv("ANTHROPIC_API_KEY") # api key is in .bashrc file, which is why this is here
client = Anthropic(api_key=api_key)
claude_model = "claude-sonnet-5"

# n8n MCP server (Streamable HTTP). The endpoint URL comes from config.toml so
# it isn't hardcoded; the N8N_MCP_URL environment variable overrides it when set.
# The Bearer token stays in the environment (N8N_MCP_TOKEN) — never in config.
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.toml")


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


_config = _load_config()
N8N_MCP_URL = os.getenv("N8N_MCP_URL") or _config.get("n8n", {}).get("url")
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


async def main():
    claude_service = Claude(model=claude_model)

    server_scripts = sys.argv[1:]
    clients = {}

    async with AsyncExitStack() as stack:
        n8n_client = await stack.enter_async_context(build_primary_client())
        clients["n8n"] = n8n_client

        for i, server_script in enumerate(server_scripts):
            client_id = f"client_{i}_{server_script}"
            client = await stack.enter_async_context(
                MCPClient(command="python", args=[server_script])
            )

            clients[client_id] = client

        # Close the headless browser (if it was ever launched) on exit.
        stack.push_async_callback(browser.shutdown)

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
