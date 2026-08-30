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
| Token server | `docker compose up` (same command — both containers come up together) | Mints join tokens for the iOS client. Requires a shared bearer token (`TOKEN_SERVER_AUTH_TOKEN`) — still dev-grade, not per-user auth. |
| Speech server (STT + TTS) | `scripts/dev.sh` (native, via `uvx`) | MLX needs Apple Silicon directly — it can't run inside Docker. |
| Agent worker | `scripts/dev.sh` (same command — both come up together) | The STT → LLM → TTS pipeline, VAD, turn detection, barge-in. Stays native for `download-files` weights and `dev` auto-reload. |

## First-time setup

```bash
cd nad-backend
cp .env.example .env          # fill in LLM_BASE_URL / LLM_API_KEY / LLM_MODEL,
                               # LIVEKIT_URL / LIVEKIT_NODE_IP (your Mac's LAN IP,
                               # from `ipconfig getifaddr en0`), a generated
                               # LIVEKIT_API_KEY/_SECRET pair (see comment in the file),
                               # and TOKEN_SERVER_AUTH_TOKEN: openssl rand -hex 24
uv sync                       # creates .venv, installs livekit-agents
uv run -m livekit.agents download-files   # fetches VAD / turn-detector weights
```

## Run (2 terminals)

```bash
docker compose up                         # 1. LiveKit server + token endpoint (:7880, :8787)
scripts/dev.sh                            # 2. speech server + agent worker together
```

The token server's image builds automatically on the first `docker compose up`. After editing
`token_server.py`, it needs `docker compose up --build` — the script is baked into the image,
not bind-mounted like `livekit.yaml`, so a plain `up` keeps running the old code.

`scripts/dev.sh` starts `scripts/speech-server.sh` and `uv run agent.py dev` together (first
request per model still downloads it from HF, same as running them separately). Ctrl+C stops
both — and everything each one spawned, including `agent.py dev`'s own worker subprocess and
`uvx`'s `mlx_audio.server` — and if either one exits on its own (crash), the script notices
within a second and stops the other rather than leaving it running headless. Run them in
separate terminals instead when you want to restart or watch one independently of the other:

```bash
scripts/speech-server.sh                  # mlx-audio (STT + TTS)
uv run agent.py dev                       # agent worker
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

(`uv run agent.py dev|start|console` prints a `DeprecationWarning` for the built-in Python
CLI — expected on `livekit-agents` 1.7.1. Its replacement's `console` mode requires the
separate `lk` CLI driving a TCP dev channel rather than your terminal mic/speaker directly,
so this project intentionally stays on the deprecated entrypoint for now.)

Check the token endpoint is reachable and authenticated:

```bash
curl -s "http://<your-mac-lan-ip>:8787/token?room=test" \
  -H "Authorization: Bearer $(grep TOKEN_SERVER_AUTH_TOKEN .env | cut -d= -f2)"
```

## Connecting from a physical iPhone

`LIVEKIT_URL` in `.env` points at this Mac's **LAN IP** (`<your-mac-lan-ip>`) rather than
`localhost`, so a phone on the same Wi-Fi can reach it — the Simulator works with either,
since it shares the Mac's network stack. `livekit.yaml`'s `use_external_ip: false` matches
this: the server just needs to be reachable on the LAN, not have a real public IP advertised
via STUN.

With `use_external_ip: false`, LiveKit advertises `rtc.node_ip` in its ICE candidates
instead — and under Docker Desktop, the address it would auto-detect is the *container's*
internal `172.x` one, which neither the phone nor the Mac host can reach. Signalling on
`:7880` still succeeds in that case; only media fails, silently. `docker-compose.yml` avoids
this by passing `--node-ip` from `LIVEKIT_NODE_IP` in `.env`. (Alternative: run
`livekit-server` natively via `brew install livekit` instead of `docker compose up`, which
sidesteps Docker networking entirely on the Mac; keep compose for the real deploy host.)

If this Mac's LAN IP changes (new network, DHCP renewal), update both `LIVEKIT_URL` and
`LIVEKIT_NODE_IP` in `.env` (`ipconfig getifaddr en0`).

Not yet handled, since `nad-ios` has no networking code yet: iOS **App Transport Security**
blocks cleartext `ws://`/`http://` to non-localhost hosts by default. Once the app makes its
first request to this backend, add an `NSAppTransportSecurity` → `NSAllowsLocalNetworking`
exception to `nad-ios/nad-ios/Info.plist` (plus the `NSLocalNetworkUsageDescription` string
iOS 14+ prompts the user with) — or move both ends to TLS — or requests will silently fail.

## Swapping models

STT and TTS are just OpenAI-compatible base URLs, so any server that speaks
`/v1/audio/transcriptions` and `/v1/audio/speech` works — change `SPEECH_BASE_URL`,
`STT_MODEL`, `TTS_MODEL` in `.env`. Examples for mlx-audio:

- STT: `mlx-community/parakeet-tdt-0.6b-v3` (fast, English), `mlx-community/whisper-large-v3-turbo-asr-fp16` (multilingual)
- TTS: `mlx-community/Kokoro-82M-bf16` (or the `-8bit` / `-4bit` variants); voices: `af_heart`, `bf_alice`, …

Switching to a multilingual STT model also needs `STT_LANGUAGE` in `.env` updated to the
language you'll actually speak — `agent.py` still pins a single language per session, it
does not auto-detect.

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
- The token server is the one process that gains from being a container: no native deps (MLX
  needs the Mac directly) and no need for auto-reload (unlike the agent worker), so it's the
  only client-facing piece besides LiveKit itself. Its image installs the `token-server` group
  from `pyproject.toml` — not the full `livekit-agents` — so it stays tens of MB instead of the
  ~250 MB the STT/TTS/VAD stack would add.
- Containerizing the token server doesn't reduce what the iOS client configures — it already
  points at one endpoint (`http://<lan-ip>:8787`), since `token_server.py` hands back the
  LiveKit URL rather than the client hardcoding it. What changes is operational: one
  `docker compose up` instead of a fourth `uv run` terminal. The network still exposes three
  port groups (`:8787`, `:7880`, `50000–50100/udp`) — collapsing signalling and the token
  endpoint onto one port would need a reverse proxy in front of both, and WebRTC media UDP can
  never go through an HTTP proxy regardless, since it's ICE-negotiated end to end.
- `.env` is the single source of truth for LiveKit's keys and node IP — `livekit-server`
  reads keys from the `LIVEKIT_KEYS` env var (`key: secret`), and `docker-compose.yml` sets
  it from `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` and passes `LIVEKIT_NODE_IP` as `--node-ip`.
  `livekit.yaml` itself holds no secrets and nothing machine-specific, so it's committed.
- Prod: beyond a real DNS name and TLS (`wss://`, not `ws://`), set `use_external_ip: true`
  in `livekit.yaml` (this takes precedence over `--node-ip`, so drop `LIVEKIT_NODE_IP` at
  that point) and switch `LIVEKIT_URL` back off the LAN IP, and put real per-user auth in
  front of `token_server.py` — its current shared-bearer-token check only keeps strangers
  off your LAN from minting join tokens, not real users apart from each other.
