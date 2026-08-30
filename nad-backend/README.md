# nad-backend

Self-hosted real-time voice agent on LiveKit. Everything runs on your own machine/infra;
the only external call is the LLM endpoint you configure.

```
iOS app ──① token──▶ token server (Docker, :8787)
   │
   └──② WebRTC (token)──▶ LiveKit server (Docker, :7880)
                                 │
                                 ▼
                           agent.py (uv)  ── VAD + end-of-turn model run in-process
                            │        │
               STT ◀────────┘        └────────▶ TTS          LLM ──▶ your OpenAI-compatible endpoint
               mlx-audio server (native, :8000) — Parakeet/Whisper + Kokoro on MLX
```

`docker compose up` runs the token server + LiveKit together — the only two of the above the
iOS client ever talks to directly. `scripts/dev.sh` runs agent.py + mlx-audio together.

## Processes

| What | How it runs | Why |
|---|---|---|
| LiveKit server | `docker compose up` | SFU / signalling. Portable to the eventual prod host. |
| Token server | `docker compose up` (same command — both containers come up together) | Mints join tokens for the iOS client. Requires a shared bearer token (`TOKEN_SERVER_AUTH_TOKEN`) — still dev-grade, not per-user auth. |
| Speech server (STT + TTS) | `scripts/dev.sh` (native, via `uvx`) | MLX needs Apple Silicon directly — it can't run inside Docker. |
| Agent worker | `scripts/dev.sh` (same command — both come up together) | The STT → LLM → TTS pipeline, VAD, turn detection, barge-in. Stays native for `download-files` weights and `dev` auto-reload. |

## First-time setup

```bash
brew install livekit-cli       # the `lk` CLI — drives the agent worker (dev/start/console)
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

`scripts/dev.sh` starts `scripts/speech-server.sh` and `lk agent dev agent.py` together (first
request per model still downloads it from HF, same as running them separately). It also sources
`.env` and exports it into the shell first — `lk` itself needs `LIVEKIT_URL`/`LIVEKIT_API_KEY`/
`LIVEKIT_API_SECRET` as real env vars before it gets anywhere near importing `agent.py`, so
`agent.py`'s own `load_dotenv()` is too late for `lk`'s purposes (see the comment at the top of
`agent.py`). Ctrl+C stops both — and everything each one spawned, including `lk`'s own `uv run`
→ `python -m livekit.agents` chain and `uvx`'s `mlx_audio.server` — and if either one exits on
its own (crash), the script notices within a second and stops the other rather than leaving it
running headless. Run them in separate terminals instead when you want to restart or watch one
independently of the other:

```bash
scripts/speech-server.sh                  # mlx-audio (STT + TTS)
set -a && source .env && set +a && lk agent dev agent.py   # agent worker
```

The agent warms the speech models itself at the start of every job (`_warm_speech_models()`
in `agent.py`), before it reports `lk.agent.state = "listening"` — so a client that waits for
that state gets a genuinely ready agent rather than one that still has to download Kokoro and
Parakeet on the first turn. A warm-up failure is logged and the job continues, so a cold or
broken speech server degrades to a slow first turn rather than a dead session.

To warm them by hand anyway (or to check the server directly):

```bash
curl -s localhost:8000/v1/audio/speech -H 'content-type: application/json' \
  -d '{"model":"mlx-community/Kokoro-82M-bf16","voice":"af_heart","input":"hello","response_format":"wav"}' -o /dev/null
curl -s localhost:8000/v1/audio/transcriptions -F model=mlx-community/parakeet-tdt-0.6b-v3 -F file=@/path/to/any.wav -F response_format=json
```

Check the TTS call actually returned audio, not just HTTP 200 — Kokoro can fail *inside* a
streamed 200 response (e.g. a missing `misaki`/spaCy dependency; see `scripts/speech-server.sh`
for what's pinned and why) and the connection just drops mid-body, which the agent worker
surfaces as `APIConnectionError: peer closed connection without sending complete message body`.
`curl -o /dev/null` above swallows that silently — pass `-w '%{size_download}\n'` (or open the
saved file) if you want the check to actually catch it.

Quick smoke test without the iOS app:

```bash
set -a && source .env && set +a && lk agent console agent.py   # talk to the agent from the terminal mic/speaker
```

(`lk` detects this is a `uv` project and runs `agent.py` inside its venv automatically — no
need to `uv run` it yourself. `lk agent console` handles your terminal's mic/speaker directly,
same as before; it's `livekit-agents`' own built-in `dev|start|console` CLI that's deprecated,
which is why `agent.py` and `scripts/dev.sh` go through `lk` instead now.)

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

Handled on the client side now: `nad-ios/Info.plist` carries the `NSAppTransportSecurity` →
`NSAllowsLocalNetworking` exception (iOS ATS otherwise blocks cleartext `ws://`/`http://` to
non-localhost hosts), and `INFOPLIST_KEY_NSLocalNetworkUsageDescription` in the Xcode build
settings supplies the string iOS 14+ prompts the user with on first connection. See
`nad-ios/README.md` for what the app itself needs configured (the token-server URL and bearer
token) before it can reach this backend at all.

## Exposing beyond the LAN (Tailscale Funnel)

Not set up yet — for later, when the token server and LiveKit need to be reachable from
outside the LAN over [Tailscale Funnel](https://tailscale.com/kb/1223/funnel). Prerequisite:
Funnel enabled for the tailnet in the Tailscale admin console, and `tailscale up` already
running on this Mac. Run these once `docker compose up` (and `scripts/dev.sh`) are already up:

```bash
tailscale funnel --bg localhost:8787              # token server → public :443 (https)
tailscale funnel --bg --https=8443 localhost:7880 # LiveKit signalling → public :8443 (wss)
```

Funnel only publishes on three fixed ports (443, 8443, 10000 — no arbitrary port), which is
why these land on two different ones. Check what's live, or tear one down:

```bash
tailscale funnel status
tailscale funnel 443 off
tailscale funnel --https=8443 off
```

**This alone does not carry audio.** Funnel is TLS/TCP-only and cannot forward UDP at all, so
the WebRTC media range (`50000-50100/udp`) can't go through it — token requests and LiveKit's
signalling become reachable from anywhere, but no audio will flow. The actual fix for that,
when this gets picked up for real: enroll the client device on the same tailnet as this Mac
and point `LIVEKIT_URL`/`LIVEKIT_NODE_IP` at the Mac's tailscale IP instead of Funnel — real
UDP over Tailscale's own tunnel, not a TCP relay. (Since Funnel does terminate real TLS, a
`wss://`/`https://` `LIVEKIT_URL` here would also sidestep the ATS cleartext exception above —
worth remembering if the tailnet route isn't the one taken.)

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
- **Noise gate** — `noise_gate.py`, between the VAD and the speech server. See below.

Tune these in `agent.py` → `TurnHandlingOptions`.

## Background noise

Silero fires on any energy transient, and because the STT is batch the VAD is the *only*
thing deciding what Parakeet ever sees — so without a gate a fan or a TV becomes a turn.
Four layers deal with that, in order of how much they buy:

1. **No double AGC.** `session.start(room_options=...)` sets `auto_gain_control=False`.
   The iPhone already runs Apple's Voice-Processing I/O; livekit-agents otherwise adds a
   second WebRTC AGC ahead of the VAD, and two cascaded gain controllers lift room noise
   toward speech level during pauses. Cheapest and largest single win.
2. **`noise_gate.py`** wraps the STT and drops VAD segments that carry no plausible
   near-field speech, comparing the loud part of each segment against both its own ambient
   and a session-wide floor. A rejected segment costs nothing downstream — `StreamAdapter`
   discards an empty transcript outright, so there is no turn, no end-of-turn inference and
   no HTTP call.
3. **A transcript gate** in `NadAssistant.on_user_turn_completed` discards filler-only
   turns. A backstop: by then a preemptive LLM call has already been spent.
4. **iOS capture** enables `highpassFilter` and `typingNoiseDetection`, which the LiveKit
   SDK leaves off by default (`VoiceSessionController.swift`).

Krisp is **not** an option here: both the Python `livekit-plugins-noise-cancellation` (BVC)
and `livekit/swift-krisp-noise-filter` are LiveKit Cloud features. This deployment is
self-hosted, so don't spend time re-investigating them.

None of this can reject a loud TV or a second person talking near the phone — that is
high-SNR near-field speech, and every layer above is blind to *who* is speaking. Speaker
verification is the only real answer to that, and it is a much bigger project.

### Tuning

Set `NAD_AUDIO_DEBUG=1` to get one log line per VAD segment (level, in-segment SNR, tracked
floor, verdict, transcript) plus a line for every transcript that survives. Play a fixed
noise clip at a fixed volume and distance, stay silent for a minute, then repeat while
speaking — the `snr_db` values for the two should separate cleanly. If they overlap, no
threshold in `noise_gate.py` will work. Then adjust the constants at the top of that file.

Note that `lk agent console` builds no RoomIO, so layer 1 is inactive there and layer 4 is
iOS-only; the console exercises layers 2 and 3 only. Test the rest on a device.

```bash
uv run --group dev pytest      # unit tests for the gate's decision logic
```

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
