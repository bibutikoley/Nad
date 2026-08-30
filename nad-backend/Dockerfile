# Token server only. The agent worker and speech server stay native (MLX needs
# Apple Silicon directly, and the agent wants download-files + `dev` auto-reload
# on the host) — see README.md "Notes".
FROM python:3.12-slim

# Pinned uv, copied from the official distroless image (no curl|sh bootstrap).
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Only the token-server group (see pyproject.toml) — not livekit-agents and its
# ~200 MB of STT/TTS/VAD deps the token server never imports.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --only-group token-server

COPY token_server.py ./

RUN useradd --system --uid 10001 appuser
USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    TOKEN_SERVER_PORT=8787
EXPOSE 8787
CMD ["python", "token_server.py"]
