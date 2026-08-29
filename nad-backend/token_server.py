"""Minimal token endpoint for clients (nad-ios) to join a room.

GET /token?room=<name>&identity=<user>  ->  {"url": ..., "token": ...}

Dev only: no auth. Put real auth in front of this before exposing it.
Run:  uv run token_server.py
"""

import os
import uuid

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

load_dotenv()


async def token(request: web.Request) -> web.Response:
    room = request.query.get("room") or f"nad-{uuid.uuid4().hex[:8]}"
    identity = request.query.get("identity") or f"user-{uuid.uuid4().hex[:8]}"

    jwt = (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .to_jwt()
    )
    return web.json_response({"url": os.environ["LIVEKIT_URL"], "room": room, "token": jwt})


app = web.Application()
app.add_routes([web.get("/token", token)])

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("TOKEN_SERVER_PORT", "8787")))
