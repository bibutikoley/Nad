#!/usr/bin/env bash
# Starts the mlx-audio server (Apple Silicon / MLX) that serves both
#   POST /v1/audio/transcriptions  (STT: Parakeet / Whisper)
#   POST /v1/audio/speech          (TTS: Kokoro)
# with OpenAI-compatible request shapes. Runs natively — MLX can't run in Docker.
set -euo pipefail

PORT="${SPEECH_PORT:-8000}"

# webrtcvad (a transitive dep, used for VAD) still does `import pkg_resources`,
# which newer setuptools flags as deprecated on every import — cosmetic (it's
# just an import-time warning, nothing breaks), but noisy on every startup.
# webrtcvad itself hasn't been updated to drop the import, so it's silenced by
# module rather than fixed at the source.
export PYTHONWARNINGS="ignore::UserWarning:webrtcvad"

# mlx-audio's Kokoro TTS needs `misaki[en]` for English text-to-phoneme processing,
# which isn't pulled in by mlx-audio's own extras — without it, /v1/audio/speech
# returns 200 then drops the connection mid-stream on an ImportError raised from
# inside the response body iterator (agent.py sees this as APIConnectionError:
# "peer closed connection without sending complete message body"). misaki[en] in
# turn needs the spaCy `en_core_web_sm` model, which isn't a normal PyPI package —
# it ships as a wheel on GitHub releases, so it's pinned directly by URL. Both are
# added via `--with` rather than mlx-audio's own extras since mlx-audio doesn't
# declare a language-specific extra that pulls either one in.
exec uvx --python 3.12 --from "mlx-audio[server,stt,tts]" \
  --with "misaki[en]" \
  --with "en_core_web_sm@https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl" \
  mlx_audio.server --host 0.0.0.0 --port "$PORT"
