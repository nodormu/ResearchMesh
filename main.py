import asyncio
import os
import sys
import tomllib
from contextlib import AsyncExitStack

from anthropic import Anthropic

from core import local_tools, process_reaper
from core.chat import Chat
from core.claude import Claude, refresh_claude_models
from core.cli import CliApp
from mcp_client import MCPClient

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

# Claude model: the first entry of config.toml [claude] claude_models is what
# every new session starts on. refresh_claude_models() (core/claude.py) is
# TTL-gated: most starts read the cached array with no network call, and about
# once a day it re-scans /v1/models and updates the cache in place. No env var
# overrides it; /model swap changes the model mid-session (core/cli.py).
# mcp_server.py does `import main as app` and reads `app.claude_model` from
# here, so an instance served over MCP gets the same default.
_claude_models = refresh_claude_models()
claude_model = _claude_models[0] if _claude_models else "claude-sonnet-5"

# MCP servers (Streamable HTTP), declared as a list in config.toml so adding
# one is a config edit. Bearer tokens stay in the environment: each entry's
# `token_env` names the variable that holds its token.
_mcp_config = _config.get("mcp", {})
MCP_ENABLED = _mcp_config.get("enabled", True)  # default on
MCP_SERVERS = _mcp_config.get("servers", [])


def _expand(value):
    """Expand `~` and `$VAR`/`${VAR}` in a config value, recursing into lists
    and dicts.

    TOML does no substitution, so `/home/$USER/...` would reach the subprocess
    literally. An undefined variable is left as is (`expandvars` behaviour), so
    a typo shows up in the error instead of collapsing to `/home//`.
    """
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _expand_paths(server: dict) -> dict:
    """A copy of a [mcp].servers entry with `~`/`$VAR` expanded in `command`,
    `url` and `env` values, so config.toml can be checked in without
    anyone's home directory baked in.

    `env` keys and `token_env` are names and are left alone; `description` is
    prose for the model and is not expanded.
    """
    expanded = dict(server)
    for key in ("command", "url", "env"):
        if key in expanded:
            expanded[key] = _expand(expanded[key])
    return expanded


def build_client(server: dict, name: str) -> MCPClient:
    """One MCPClient from a [mcp].servers entry.

    Two kinds of entry:

    - Streamable HTTP (remote server):
        { name = "...", url = "http://host:port/...", token_env = "..." }

    - stdio (local subprocess):
        { name = "...", command = ["node", "/path/to/bin.js"], env = { ... } }
      `command` is the full argv. `env` is extra environment for the
    subprocess; the MCP SDK merges it with a safe default set (PATH, HOME,
    ...).

    Paths must arrive already expanded (`_connect_mcp_servers` runs
    `_expand_paths` first); a raw config entry passes `$USER` to the subprocess
    unsubstituted.
    """
    if "command" in server:
        command_list = server.get("command")
        if not isinstance(command_list, list) or not command_list:
            raise ValueError(
                "'command' must be a non-empty list, e.g. "
                '["node", "/path/to/bin.js"]'
            )
        command, *args = command_list
        env = server.get("env")
        return MCPClient(command=command, args=args, env=env, transport="stdio")

    url = server.get("url")
    if not url:
        raise ValueError("entry needs either 'url' (http) or 'command' (stdio)")

    token_env = server.get("token_env")
    token = os.getenv(token_env) if token_env else None
    if token_env and not token:
        print(
            f"[mcp] {name}: {token_env} is not set — connecting without auth",
            file=sys.stderr,
        )
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return MCPClient(transport="http", url=url, headers=headers)


async def _connect_mcp_servers(stack: AsyncExitStack, clients: dict) -> None:
    """Connect every configured server. A server that fails is reported and
    skipped, so one unreachable endpoint doesn't take the whole app down."""
    for index, server in enumerate(MCP_SERVERS):
        name = server.get("name") or f"server_{index}"

        if not server.get("enabled", True):
            print(f"[mcp] {name}: disabled in config.toml")
            continue

        # Do this before build_client so the failure message below also shows
        # the real path rather than the `$USER` the file was written with.
        server = _expand_paths(server)

        try:
            client = build_client(server, name)
        except ValueError as e:
            print(f"[mcp] {name}: skipped — {e}", file=sys.stderr)
            continue

        try:
            await client.connect()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            # A failed connect raises CancelledError from connect() and the
            # real cause (e.g. ConnectError) from cleanup(); swallow both and
            # report the endpoint instead of a traceback.
            try:
                await client.cleanup()
            except BaseException as cleanup_error:
                # Same rule as core/local_tools.shutdown(): cleanup must not
                # fail, and must not fail silently. This is already an error
                # path, so a swallowed second failure would be the least
                # visible place in the app.
                print(
                    f"[mcp] {name}: cleanup after failed connect also failed "
                    f"(ignored): {cleanup_error}",
                    file=sys.stderr,
                )
            target = server.get("url") or " ".join(server.get("command", []))
            print(
                f"[mcp] {name}: could not reach/launch {target} — skipped",
                file=sys.stderr,
            )
            continue

        stack.push_async_callback(client.cleanup)
        clients[name] = client
        print(f"[mcp] {name}: connected")


def _reap_orphans_on_exit() -> None:
    """Last-line safety net, registered first so AsyncExitStack's LIFO unwind
    runs it last, after local_tools.shutdown() and each MCP client's
    cleanup. It checks the real OS child-process tree
    (core/process_reaper.py), which no tool's own bookkeeping can see.
    Wrapped so cleanup cannot turn an ordinary exit into a traceback.
    """
    try:
        reaped = process_reaper.reap_orphans()
    except Exception as e:
        print(f"[shutdown] orphan check failed (ignored): {e}", file=sys.stderr)
        return
    if reaped:
        print(f"[shutdown] reaped {len(reaped)} leftover process(es): {', '.join(reaped)}")
    else:
        print("[shutdown] clean exit, no leftover processes")


async def main():
    claude_service = Claude(model=claude_model)

    server_scripts = sys.argv[1:]
    clients = {}

    async with AsyncExitStack() as stack:
        # Pushed first so it runs LAST (AsyncExitStack unwinds LIFO) --
        # after every MCP client's cleanup below and local_tools.shutdown.
        stack.callback(_reap_orphans_on_exit)

        if MCP_ENABLED and MCP_SERVERS:
            await _connect_mcp_servers(stack, clients)
            if not clients:
                print(
                    "[mcp] no server connected — running with local tools only",
                    file=sys.stderr,
                )
        elif not MCP_ENABLED:
            print("[mcp] disabled in config.toml — running with local tools only")
        else:
            print("[mcp] no servers configured — running with local tools only")

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
