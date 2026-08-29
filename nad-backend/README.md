# nad-backend

Self-hosted real-time voice agent on LiveKit. Everything runs on your own machine/infra;
the only external call is the LLM endpoint you configure.

```
iOS app ──WebRTC──▶ LiveKit server (Docker, :7880)
                          │
                          ▼
                    agent.py (uv)  ── VAD + end-of-turn model run in-process
                     │        │
        STT ◀────────┘        └────────▶ TTS          LLM ──▶ your OpenAI-compatible endpoint
        mlx-audio server (native, :8000) — Parakeet/Whisper + Kokoro on MLX
```

## Processes

| What | How it runs | Why |
|---|---|---|
| LiveKit server | `docker compose up` | SFU / signalling. Portable to the eventual prod host. |
| Speech server (STT + TTS) | `scripts/speech-server.sh` (native, via `uvx`) | MLX needs Apple Silicon directly — it can't run inside Docker. |
| Agent worker | `uv run agent.py dev` | The STT → LLM → TTS pipeline, VAD, turn detection, barge-in. |
| Token server | `uv run token_server.py` | Mints join tokens for the iOS client. Dev-only, no auth. |

## First-time setup

```bash
cd nad-backend
cp .env.example .env          # then fill in LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
uv sync                       # creates .venv, installs livekit-agents
uv run -m livekit.agents download-files   # fetches VAD / turn-detector weights
```

## Run (4 terminals)

```bash
docker compose up                         # 1. LiveKit server
scripts/speech-server.sh                  # 2. mlx-audio (first request per model downloads it from HF)
uv run agent.py dev                       # 3. agent worker
uv run token_server.py                    # 4. token endpoint on :8787
```

Warm the speech models once so the first real turn isn't slow:

```bash
curl -s localhost:8000/v1/audio/speech -H 'content-type: application/json' \
  -d '{"model":"mlx-community/Kokoro-82M-bf16","voice":"af_heart","input":"hello","response_format":"wav"}' -o /dev/null
curl -s localhost:8000/v1/audio/transcriptions -F model=mlx-community/parakeet-tdt-0.6b-v3 -F file=@/path/to/any.wav -F response_format=json
```

Quick smoke test without the iOS app:

```bash
uv run agent.py console        # talk to the agent from the terminal mic/speaker
```

## Swapping models

STT and TTS are just OpenAI-compatible base URLs, so any server that speaks
`/v1/audio/transcriptions` and `/v1/audio/speech` works — change `SPEECH_BASE_URL`,
`STT_MODEL`, `TTS_MODEL` in `.env`. Examples for mlx-audio:

- STT: `mlx-community/parakeet-tdt-0.6b-v3` (fast, English), `mlx-community/whisper-large-v3-turbo-asr-fp16` (multilingual)
- TTS: `mlx-community/Kokoro-82M-bf16` (or the `-8bit` / `-4bit` variants); voices: `af_heart`, `bf_alice`, …

## Turn-taking & barge-in

All of this is local (no LiveKit Cloud inference):

- **VAD** — Silero, in-process (`inference.VAD`). Segments audio for the batch STT and is the
  fast trigger for interruptions.
- **End-of-turn** — `inference.TurnDetector(version="v1-mini")`, an on-device model that
  decides whether the user has finished, rather than relying on silence alone.
- **Endpointing** — 0.3 s min / 2.0 s max wait after speech before replying.
- **Barge-in** — user speech ≥ 0.3 s stops the agent. If no words come through within 2 s
  (cough, "mm-hm"), the agent resumes where it stopped.

Tune these in `agent.py` → `TurnHandlingOptions`.

## Notes

- STT is batch (one HTTP call per utterance) rather than streaming. Latency is dominated by
  model speed on your Mac; Parakeet is much faster than Whisper for this.
- Prod: keep `livekit.yaml` keys in sync with `.env`, set `use_external_ip: true`, and put
  auth in front of `token_server.py`.
