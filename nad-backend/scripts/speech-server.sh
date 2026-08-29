#!/usr/bin/env bash
# Starts the mlx-audio server (Apple Silicon / MLX) that serves both
#   POST /v1/audio/transcriptions  (STT: Parakeet / Whisper)
#   POST /v1/audio/speech          (TTS: Kokoro)
# with OpenAI-compatible request shapes. Runs natively — MLX can't run in Docker.
set -euo pipefail

PORT="${SPEECH_PORT:-8000}"

exec uvx --python 3.12 --from "mlx-audio[server,stt,tts]" \
  mlx_audio.server --host 0.0.0.0 --port "$PORT"
