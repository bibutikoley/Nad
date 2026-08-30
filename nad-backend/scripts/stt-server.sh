#!/usr/bin/env bash
# Starts the STT server (stt_server.py), which serves
#   POST /v1/audio/transcriptions
# with the same OpenAI-compatible shape mlx-audio uses. Runs natively — MLX can't
# run in Docker, same as scripts/speech-server.sh.
#
# It exists because of one model. omi-health/omi-med-stt-v1-* cannot be served by the
# mlx-audio server on :8000, twice over:
#   1. mlx-audio's router picks a module from config.json["model_type"], falling back
#      to the first dash-token of the repo name. omi ships a NeMo-style config with no
#      usable model_type, and no token of "omi-med-stt-v1-mlx-q8" matches a directory
#      under mlx_audio/stt/models/, so it resolves to a nonexistent
#      mlx_audio.stt.models.omi and raises `ValueError: Model type omi not supported`.
#   2. Even routed to the parakeet module, the rank-128 medical adapter has to be
#      installed into every Conformer block *before* the weights load — the vendor is
#      explicit: "Do not call stock parakeet-mlx directly for this model."
# omi_stt.mlx_runtime is the only supported loader and ships as a CLI with no HTTP
# server, hence the wrapper.
#
# The other backends are here so one server can serve everything if you prefer that to
# running two. They are not needed: for anything mlx-audio can route, pointing
# STT_BASE_URL at :8000 is cheaper — that process is already up, and this one would
# hold a second resident copy of the weights.
set -euo pipefail
cd "$(dirname "$0")/.."

# STT_BACKEND / STT_MODEL come from .env, the same file agent.py reads, so the two
# can't disagree about what is being served. scripts/dev.sh has already exported them;
# sourcing here covers running this script on its own.
if [ -f .env ]; then
  set -a
  # shellcheck source=/dev/null
  source .env
  set +a
fi

# Only the chosen backend's dependencies, so the environment stays lean instead of
# carrying all three. stt_server.py validates the same variable and exits with the
# list of valid values, so an unknown one fails there rather than as a resolver error.
case "${STT_BACKEND:-omi}" in
  omi)
    # Pulls parakeet-mlx (the actual runtime) and mlx-audio, which mlx_runtime.py never
    # imports. Kept as the vendor-documented extra anyway; the uv cache is shared with
    # speech-server.sh, which already has mlx-audio, so it's ~free.
    backend_deps=(--with "omi-med-stt[mlx]")
    ;;
  parakeet)
    backend_deps=(--with "parakeet-mlx")
    ;;
  *)
    # mlx-audio / whisper / nemotron — one dependency, since mlx-audio routes the
    # architecture itself from the repo's config.
    backend_deps=(--with "mlx-audio[stt]")
    ;;
esac

# --no-project is load-bearing. Without it, `uv run --with` layers these on top of
# nad-backend's own venv, which would resolve mlx/librosa/transformers against
# livekit-agents and impose omi's numpy<2.3 ceiling on the agent worker. They share
# nothing on purpose: omi's runtime monkey-patches
# parakeet_mlx.conformer.ConformerBlock.__call__ globally and permanently, and that
# must never land in the worker's process.
#
# These pins live here rather than in pyproject.toml for the same reason mlx-audio's
# do: mlx publishes wheels only for macos-arm64, `uv lock` resolves universally, and
# the Dockerfile runs `uv sync --locked --only-group token-server` on linux — a group
# here would make uv.lock unresolvable for the token-server image.
exec uv run --python 3.12 --no-project \
  "${backend_deps[@]}" \
  --with "aiohttp~=3.12" \
  stt_server.py
