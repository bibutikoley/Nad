"""Minimal token endpoint for clients (nad-ios) to join a room.

GET /token?room=<name>&identity=<user>
Header: Authorization: Bearer <TOKEN_SERVER_AUTH_TOKEN>
  ->  {"url": ..., "token": ...}

POST /history      {"room": ..., "messages": [{"role": "user"|"assistant", "text": ...}]}
GET  /history/<room>  ->  {"messages": [...]}

The /history pair is a handoff for resuming a past conversation: the client parks the
earlier transcript here under the room name it is *about* to join, then the agent picks
it up when it gets the job and seeds its chat context with it. Going through this server
rather than the join token keeps the transcript out of the LiveKit connect URL (the token
is carried in its query string, so a long conversation would bloat it) and imposes no
size limit. It is also race-free: the client stores before it connects, and connecting is
what dispatches the agent.

Deliberately in-memory: this is a few-seconds-long handoff, not the history itself. The
client owns the durable copy. A restart drops parked entries, which costs at most the
memory of one resume.

Binds 0.0.0.0 by design: the iOS client reaches this over the LAN, not just
localhost. That's why it needs the shared-secret check below rather than
relying on the bind address for protection. Still dev-grade — swap for
per-user auth (e.g. validate a Sign in with Apple/session token instead of a
single shared secret) before this leaves your LAN.
Run:  uv run token_server.py
"""

import hmac
import os
import time
import uuid
from typing import Any

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
    if request.path == "/health":  # unauthenticated liveness probe for docker-compose
        return await handler(request)
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    # Compare as bytes: hmac.compare_digest on two `str`s raises TypeError if either
    # contains non-ASCII, which would turn a malformed header into a 500 not a 401.
    if scheme != "Bearer" or not hmac.compare_digest(presented.encode(), AUTH_TOKEN_BYTES):
        raise web.HTTPUnauthorized(reason="missing or invalid bearer token")
    return await handler(request)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


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


# --- Conversation resume handoff -------------------------------------------------

# Long enough to cover a cold agent start (process spawn + model warm-up); short
# enough that abandoned resumes don't accumulate.
HISTORY_TTL_SECONDS = 900
MAX_HISTORY_BYTES = 512 * 1024
VALID_ROLES = {"user", "assistant"}

# room name -> (stored_at_monotonic, messages)
_parked_history: dict[str, tuple[float, list[dict[str, str]]]] = {}


def _prune_history() -> None:
    now = time.monotonic()
    for room in [r for r, (at, _) in _parked_history.items() if now - at > HISTORY_TTL_SECONDS]:
        del _parked_history[room]


def _clean_messages(raw: Any) -> list[dict[str, str]]:
    """Keep only well-formed {role, text} pairs. The transcript is replayed straight
    into an LLM prompt, so anything unexpected is dropped rather than passed along."""
    if not isinstance(raw, list):
        raise web.HTTPBadRequest(reason="'messages' must be a list")
    cleaned: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = item.get("text")
        if role in VALID_ROLES and isinstance(text, str) and text.strip():
            cleaned.append({"role": role, "text": text.strip()})
    return cleaned


async def put_history(request: web.Request) -> web.Response:
    if (request.content_length or 0) > MAX_HISTORY_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_HISTORY_BYTES, actual_size=request.content_length or 0
        )
    try:
        body = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(reason="body must be JSON") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(reason="body must be a JSON object")

    room = body.get("room")
    if not isinstance(room, str) or not room.strip():
        raise web.HTTPBadRequest(reason="'room' is required")

    messages = _clean_messages(body.get("messages"))
    _prune_history()
    _parked_history[room.strip()] = (time.monotonic(), messages)
    return web.json_response({"status": "ok", "messages": len(messages)})


async def get_history(request: web.Request) -> web.Response:
    _prune_history()
    entry = _parked_history.get(request.match_info["room"])
    if entry is None:
        # The overwhelmingly common case: a fresh conversation with nothing parked.
        raise web.HTTPNotFound(reason="no history parked for this room")
    return web.json_response({"messages": entry[1]})


app = web.Application(middlewares=[require_auth])
app.add_routes(
    [
        web.get("/token", token),
        web.get("/health", health),
        web.post("/history", put_history),
        web.get("/history/{room}", get_history),
    ]
)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("TOKEN_SERVER_PORT", "8787")))
