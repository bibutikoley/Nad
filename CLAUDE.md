# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Nad is a **self-hosted, real-time voice assistant**: a LiveKit Agents worker on Apple Silicon
(`nad-backend/`) plus a SwiftUI client (`nad-ios/`). Everything runs on the developer's own
machine — the only external call is the LLM endpoint configured in `.env`. Speech (STT + TTS)
runs locally on MLX, VAD and end-of-turn detection run in-process. **LiveKit Cloud features
(Krisp/BVC noise cancellation, cloud turn detection, `inference.TurnDetector(version="v1")`)
are unavailable by design — don't reach for them.**

`nad-backend/README.md` and `nad-ios/README.md` are unusually thorough and are the canonical
docs for setup, model swapping, noise tuning, and deployment. Read the relevant section before
changing behaviour in those areas, and keep them updated when you do.

## Commands

**Do not start the server yourself.** `docker compose up`, `scripts/dev.sh`,
`scripts/speech-server.sh`, `scripts/stt-server.sh`, `lk agent dev|start|console` and the
Xcode run are the user's to run — ask them to run it and report back, unless they explicitly
tell you to run it. Everything else here (setup, tests, one-off `curl` checks) is fine to run
directly.

All backend commands run from `nad-backend/`.

```bash
# First-time setup
brew install livekit-cli                    # the `lk` CLI drives the agent worker
cp .env.example .env                        # then fill it in (see below)
uv sync
uv run -m livekit.agents download-files     # VAD + turn-detector weights

# Run (two terminals)
docker compose up                           # LiveKit server (:7880) + token server (:8787)
scripts/dev.sh                              # mlx-audio TTS (:8000) + STT server (:8001)
                                            # + agent worker

# Or run the native processes separately
scripts/speech-server.sh
scripts/stt-server.sh
set -a && source .env && set +a && lk agent dev agent.py

# Talk to the agent from the terminal, no iOS app needed
set -a && source .env && set +a && lk agent console agent.py

# Tests
uv run --group dev pytest
uv run --group dev pytest test_noise_gate.py::test_real_speech_survives   # single test
```

`docker compose up` alone does **not** rebuild the token server after editing
`token_server.py` — the script is baked into the image (only `livekit.yaml` is bind-mounted).
Use `docker compose up --build`.

iOS: open `nad-ios/nad-ios.xcodeproj` in Xcode (26.3+) and run. Before the first build,
`cp nad-ios/nad-ios/Config/BackendConfig.example.swift.txt nad-ios/nad-ios/Config/BackendConfig.swift`
and fill in the token-server URL and `TOKEN_SERVER_AUTH_TOKEN`. That file is git-ignored.

## Architecture

```
nad-ios ──① GET /token (bearer)──▶ token_server.py   (Docker, :8787)
   │       POST /history                │
   │                                    │ agent GETs /history/<room> on job start
   └──② WebRTC ──▶ livekit-server (Docker, :7880, media 50000-50100/udp)
                          │
                          ▼
                      agent.py (native, via `lk agent dev`)
                   VAD ─▶ noise_gate ─▶ STT ─▶ LLM ─▶ TTS
                                        │       │      │
                      stt_server.py ────┘       │      └──── mlx-audio (native, :8000)
                       (native, :8001)          │
                                                └──▶ your OpenAI-compatible endpoint
```

Five processes, two commands. The split is not arbitrary: **MLX cannot run in Docker** (needs
Apple Silicon directly) and the agent worker wants host-side `download-files` + auto-reload, so
those stay native; LiveKit and the token server — the only two the iOS client talks to directly —
are containerised. STT is a *third* native process because mlx-audio cannot serve the current
default model at all, and because that model's runtime monkey-patches a third-party class
process-globally (see the invariants below).

### Backend files

- `agent.py` — the worker. Builds `AgentSession` per job: Silero VAD, noise-gated batch STT,
  OpenAI-compatible LLM, Kokoro TTS, local turn detector. Also holds the transcript-level
  noise filter and the conversation-resume wiring.
- `noise_gate.py` — `NoiseGatedSTT` wraps the real STT and drops VAD segments that carry no
  plausible near-field speech, based on level statistics. Pure and unit-tested.
- `token_server.py` — mints LiveKit join tokens (shared-bearer auth) and holds the in-memory
  `/history` handoff for conversation resume. The only containerised Python; installs the
  `token-server` dependency group only, not `livekit-agents`.
- `stt_server.py` — an OpenAI-compatible `/v1/audio/transcriptions` with a backend chosen by
  `STT_BACKEND` (`omi` | `parakeet` | `mlx-audio`, plus `whisper`/`nemotron` as aliases for the
  last). It exists for `omi`, which mlx-audio cannot serve — it raises `ValueError: Model type
  omi not supported for stt` for that repo id, and the vendor forbids loading it through stock
  `parakeet-mlx`. The other backends are a convenience, not a need: for anything mlx-audio can
  route, pointing `STT_BASE_URL` at `:8000` and ignoring this server is cheaper. Runs in its
  own uv environment, shares nothing with the worker, and is what converts the vendor's
  empty-transcript `RuntimeError` back into `{"text": ""}`.
- `scripts/dev.sh` — runs the three native processes together, with recursive process-tree
  cleanup (all die if any one does).

### iOS files

`VoiceSessionController` wraps LiveKit's `Session` and is the only thing views observe —
SwiftUI cannot observe a nested `ObservableObject` reached through a plain `let`, so anything
a view needs is republished here. `Networking/NadTokenSource` translates `token_server.py`'s
response shape (`{url, room, token}`) into what the SDK expects. `Storage/ConversationStore`
keeps history on-device (debounced writes). The project builds with
`SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`, so new types are main-actor-isolated by default —
`nonisolated` is required for anything read off the main actor (see `BackendConfig`).

## Load-bearing invariants

These span files and are easy to break:

- **`lk` reads `LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` from its own environment**
  before it ever imports `agent.py`, so `load_dotenv()` inside the module is too late for it.
  That's why `scripts/dev.sh` sources `.env` with `set -a`, and why running `lk` by hand needs
  the same prefix.
- **`LIVEKIT_URL` and `LIVEKIT_NODE_IP` must be this Mac's LAN IP**, never `localhost` and never
  `ws://livekit:7880`. The URL is handed to the iOS client verbatim; the node IP is passed as
  `--node-ip` because under Docker Desktop LiveKit would otherwise advertise its unreachable
  `172.x` address in ICE candidates — signalling succeeds and media silently fails.
- **The gate rejects by returning an empty transcript, not by raising.** `stt.StreamAdapter`
  skips empty results outright, so a rejected segment costs no turn, no end-of-turn inference,
  no LLM call, and not even the HTTP round trip. Anything that changes this contract breaks the
  whole design.

  That contract now has to survive a runtime that inverts it: `omi_stt.mlx_runtime.transcribe_mlx`
  *raises* `RuntimeError` on an empty result, and `stt_server.py` catches exactly that —
  matched on the message, so ffmpeg and decode failures still propagate — and returns
  `{"text": ""}`. Remove that catch and every noise segment becomes an HTTP 500 and a retry.
- **`_NOISE_TOKENS` / `_looks_like_noise` in `agent.py` are tuned per STT model family.**
  Transducers (omi-med-stt, Nemotron, Parakeet) return an empty string on noise; Whisper invents
  subtitle boilerplate and needs a phrase blocklist that is no longer in the file
  (`git log -S "amara" -- agent.py` restores it). The current default is Parakeet-family, so the
  list is unchanged from the Nemotron era. Revisit this and the latency table in
  `nad-backend/README.md` → "Swapping models" whenever `STT_MODEL` changes.
- **STT and TTS are separate servers with one base URL each.** `STT_BASE_URL` defaults to
  `http://localhost:8001/v1` (`stt_server.py`), `TTS_BASE_URL` to `http://localhost:8000/v1`
  (mlx-audio). There is no `SPEECH_BASE_URL` fallback any more — anything that assumes one
  server for both is wrong.
- **`scripts/dev.sh` starts the STT server unconditionally.** Don't re-gate it on
  `STT_BACKEND` or on the model name: when it was conditional, deleting that line from `.env`
  failed as a connection refused on the first spoken turn rather than at startup. The default
  model can't be served by anything else, so the server is part of the stack.
- **`stt_server.py` serves one backend and one model per process.** Both come from env, and
  the request's `model` field is deliberately ignored. This isn't just about untrusted input:
  `_install_omi_adapter_runtime` *replaces* `parakeet_mlx.conformer.ConformerBlock.__call__`
  process-wide and permanently, so a second model loaded afterwards would run that
  reimplementation too. Don't add request-driven model selection.
- **The Omi runtime must never be installed into `nad-backend/.venv`.** `mlx` has no linux
  wheels and `uv lock` resolves universally, so it would break the token-server image's
  `uv sync --locked`; and `_install_omi_adapter_runtime` patches
  `parakeet_mlx.conformer.ConformerBlock.__call__` globally and permanently, which must stay
  out of the worker's process. `scripts/stt-server.sh` uses `uv run --no-project --with`
  for exactly this — dropping `--no-project` silently reintroduces both problems.
- **`parakeet-mlx` needs a system `ffmpeg` on `PATH`** (`shutil.which("ffmpeg")`, no bundled
  fallback), so the `omi` and `parakeet` backends exit at startup without one rather than
  failing mid-call.
- **Noise suppression is four cooperating layers**, in order of value: `auto_gain_control=False`
  in `room_options` (no double AGC over Apple's Voice-Processing I/O), `noise_gate.py`, the
  transcript gate in `on_user_turn_completed`, and iOS-side `highpassFilter` /
  `typingNoiseDetection`. Tune the gate before widening the transcript lists.
- **`agent.py` publishes `lk.agent.state = "initializing"` by hand** and warms both speech
  models before `session.start()`, so `"listening"` genuinely means ready. `VoiceSessionController`
  depends on this: it holds the mic closed until the agent reports ready.
- **The agent registers with no name (implicit dispatch).** iOS must use
  `Session(tokenSource:options:)`, never `Session.withAgent(_:)`, and the SDK's own
  `agentConnectTimeout` never arms — hence the manual `readinessTimeout` in the controller.
- **Conversation resume goes through `POST /history` before connecting**, because connecting is
  what dispatches the agent. Keeping it out of the join token avoids bloating the connect URL's
  query string.
- **`nad-backend/.env` is the single source of truth** for LiveKit keys and node IP;
  `livekit.yaml` deliberately holds no secrets and nothing machine-specific, so it stays
  committed. Same split on the iOS side: `BackendConfig.example.swift.txt` is committed,
  `BackendConfig.swift` is not.

## Conventions

- Comments here explain **why**, not what — usually the alternative that was rejected and the
  measurement or failure that ruled it out. Match that density when touching this code; a
  constant or a disabled option without its rationale will read as arbitrary later.
- Constants that were tuned against real audio carry their reasoning in a docstring
  (`noise_gate.py`). Don't change one without saying what you measured.
- Commit subjects are short imperative statements of the behaviour change, often evocative
  ("Say nothing when nothing was said: switch STT to Nemotron 3.5").

## Debugging

- `NAD_AUDIO_DEBUG=1` — one log line per VAD segment (level, SNR, tracked floor, verdict,
  transcript) plus every accepted transcript. This is what the gate constants should be tuned
  against.
- `NAD_AUDIO_DUMP=/some/dir` — writes each accepted segment as `segNNNN.wav` + a `.txt` of what
  the STT made of it, for replaying bad transcripts against another model offline. **Records the
  user; opt in deliberately and delete afterwards.**
- `lk agent console` builds no RoomIO, so noise layers 1 and 4 are inactive there — it exercises
  the gate and the transcript filter only. Test the rest on a device.
- A Kokoro TTS failure can happen *inside* a streamed HTTP 200 (dropped connection mid-body),
  surfacing as `APIConnectionError: peer closed connection without sending complete message
  body`. Check the response body size, not just the status.
- `curl localhost:8001/health` reports the STT server's backend, model, and whether its
  preload has finished. A first turn that dies at exactly 30 s is `livekit-plugins-openai`'s
  hardcoded read timeout against a cold server, not a model problem.
