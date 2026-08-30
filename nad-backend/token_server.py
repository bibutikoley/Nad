"""Minimal token endpoint for clients (nad-ios) to join a room.

GET /token?room=<name>&identity=<user>
Header: Authorization: Bearer <TOKEN_SERVER_AUTH_TOKEN>
  ->  {"url": ..., "token": ...}

Binds 0.0.0.0 by design: the iOS client reaches this over the LAN, not just
localhost. That's why it needs the shared-secret check below rather than
relying on the bind address for protection. Still dev-grade — swap for
per-user auth (e.g. validate a Sign in with Apple/session token instead of a
single shared secret) before this leaves your LAN.
Run:  uv run token_server.py
"""

import hmac
import os
import uuid

from aiohttp import web
from aiohttp.typedefs import Handler
from dotenv import load_dotenv
from livekit import api

load_dotenv()

AUTH_TOKEN = os.environ.get("TOKEN_SERVER_AUTH_TOKEN", "")
if not AUTH_TOKEN:
    raise RuntimeError(
        "TOKEN_SERVER_AUTH_TOKEN is not set. Copy .env.example to .env and fill it in "
        "(generate one with: openssl rand -hex 24)."
    )
AUTH_TOKEN_BYTES = AUTH_TOKEN.encode()

LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_URL):
    raise RuntimeError(
        "LIVEKIT_API_KEY, LIVEKIT_API_SECRET, and LIVEKIT_URL must all be set. "
        "Copy .env.example to .env and fill them in."
    )


@web.middleware
async def require_auth(request: web.Request, handler: Handler) -> web.StreamResponse:
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    # Compare as bytes: hmac.compare_digest on two `str`s raises TypeError if either
    # contains non-ASCII, which would turn a malformed header into a 500 not a 401.
    if scheme != "Bearer" or not hmac.compare_digest(presented.encode(), AUTH_TOKEN_BYTES):
        raise web.HTTPUnauthorized(reason="missing or invalid bearer token")
    return await handler(request)


async def token(request: web.Request) -> web.Response:
    room = request.query.get("room") or f"nad-{uuid.uuid4().hex[:8]}"
    identity = request.query.get("identity") or f"user-{uuid.uuid4().hex[:8]}"

    jwt = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .to_jwt()
    )
    return web.json_response({"url": LIVEKIT_URL, "room": room, "token": jwt})


app = web.Application(middlewares=[require_auth])
app.add_routes([web.get("/token", token)])

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("TOKEN_SERVER_PORT", "8787")))
