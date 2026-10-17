"""Jobby's own connection to Wajo's Fo over MCP (spec Section 9, option B).

Logs in once through the browser (OAuth with PKCE; Wajo lets new clients register themselves)
and keeps the tokens in fo_token.json, like token.json for Google. Fo is asynchronous: ask_fo
may answer "working", and get_fo_reply is polled until the turn is complete or failed.

    python fo_client.py login           # one-time browser login
    python fo_client.py ask "message"   # send Fo a request and print its reply"""
import asyncio
import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

import config

TOKEN_FILE = config.ROOT / "fo_token.json"
CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
MAX_WAIT_SECONDS = 600


class FoError(Exception):
    pass


class FileTokenStorage(TokenStorage):
    def _load(self):
        return json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else {}

    def _save(self, key, value):
        data = self._load()
        data[key] = value.model_dump(mode="json", exclude_none=True)
        TOKEN_FILE.write_text(json.dumps(data, indent=2))

    async def get_tokens(self):
        data = self._load().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens):
        self._save("tokens", tokens)

    async def get_client_info(self):
        data = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info):
        self._save("client_info", client_info)


def is_logged_in():
    return TOKEN_FILE.exists() and "tokens" in json.loads(TOKEN_FILE.read_text())


# ---------- browser login ----------

async def _open_browser(url):
    print(f"Approve Jobby's access to Fo in your browser:\n{url}")
    webbrowser.open(url)


async def _wait_for_callback():
    """Catch the OAuth redirect on localhost and return the code."""
    result, done = {}, threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Jobby is connected to Fo. You can close this tab.".encode())
            done.set()

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", CALLBACK_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if not await asyncio.to_thread(done.wait, 300):
            raise FoError("Timed out waiting for the Fo login in the browser")
    finally:
        server.shutdown()
    if "code" not in result:
        raise FoError(f"Fo login failed: {result.get('error_description') or result.get('error') or result}")
    return AuthorizationCodeResult(code=result["code"], state=result.get("state"), iss=result.get("iss"))


def _auth(interactive):
    async def no_browser(url):
        raise FoError("Fo login expired or missing; run: python fo_client.py login")

    return OAuthClientProvider(
        server_url=config.WAJO_MCP_URL,
        client_metadata=OAuthClientMetadata(client_name="Jobby", redirect_uris=[REDIRECT_URI],
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"], token_endpoint_auth_method="none"),
        storage=FileTokenStorage(),
        redirect_handler=_open_browser if interactive else no_browser,
        callback_handler=_wait_for_callback if interactive else None,
    )


# ---------- calling Fo ----------

async def _call(session, tool, arguments):
    result = await session.call_tool(tool, arguments)
    if getattr(result, "isError", False):
        raise FoError(f"{tool} failed: {result.content}")
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"status": "complete", "response": text}


async def ask_fo_async(message, new_conversation=True, interactive=False):
    """Send one request and wait for Fo to finish. Returns Fo's final reply dict
    (status complete|failed, response, ...)."""
    async with create_mcp_http_client(auth=_auth(interactive)) as http:
        async with streamable_http_client(config.WAJO_MCP_URL, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                reply = await _call(session, "ask_fo", {"message": message, "newConversation": new_conversation})
                waited = 0
                while reply.get("status") == "working" and waited < MAX_WAIT_SECONDS:
                    args = {"waitSeconds": 40}
                    if reply.get("conversationId"):
                        args["conversationId"] = reply["conversationId"]
                    reply = await _call(session, "get_fo_reply", args)
                    waited += 40
                if reply.get("status") == "working":
                    raise FoError(f"Fo still working after {MAX_WAIT_SECONDS}s: {reply.get('progress')}")
                return reply


TOKEN_ENDPOINT = "https://mcp.api.wajo.ai/oauth/token"
REFRESH_MARGIN_SECONDS = 600


def refresh_if_needed(force=False):
    """Renew the access token (it lasts an hour) with the saved refresh token, before it expires.
    The MCP library would otherwise fall back to a browser login, which a running bot can't do."""
    data = json.loads(TOKEN_FILE.read_text())
    tokens, client = data["tokens"], data["client_info"]
    age = time.time() - TOKEN_FILE.stat().st_mtime
    if not force and age < tokens.get("expires_in", 3600) - REFRESH_MARGIN_SECONDS:
        return False
    body = urlencode({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                      "client_id": client["client_id"], "resource": config.WAJO_MCP_URL}).encode()
    request = Request(TOKEN_ENDPOINT, data=body, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                         "Accept": "application/json"})
    with urlopen(request, timeout=30) as resp:
        new = json.loads(resp.read())
    new.setdefault("refresh_token", tokens["refresh_token"])  # some servers rotate it, some don't
    data["tokens"] = new
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    return True


def ask_fo(message, new_conversation=True):
    """Blocking version for worker threads. Never opens a browser."""
    refresh_if_needed()
    return asyncio.run(ask_fo_async(message, new_conversation, interactive=False))


async def _login():
    async with create_mcp_http_client(auth=_auth(interactive=True)) as http:
        async with streamable_http_client(config.WAJO_MCP_URL, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                # list_tools works without a login, so make one authenticated call to prove it.
                reply = await _call(session, "list_fo_conversations", {})
                print(f"Connected to Fo. Tools: {', '.join(t.name for t in tools.tools)}")
                print(f"Authenticated call OK ({type(reply).__name__} returned).")


if __name__ == "__main__":
    if sys.argv[1:2] == ["login"]:
        asyncio.run(_login())
    elif sys.argv[1:2] == ["ask"] and len(sys.argv) > 2:
        print(json.dumps(asyncio.run(ask_fo_async(" ".join(sys.argv[2:]), interactive=True)), indent=2))
    else:
        print(__doc__)
