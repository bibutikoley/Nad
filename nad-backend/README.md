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
                │                               │
   stt_server.py (native, :8001)   mlx-audio server (native, :8000)
     Omi Med STT v1 on MLX              Kokoro on MLX
```

`docker compose up` runs the token server + LiveKit together — the only two of the above the
iOS client ever talks to directly. `scripts/dev.sh` runs agent.py, the mlx-audio server and
the STT server together.

## Processes

| What | How it runs | Why |
|---|---|---|
| LiveKit server | `docker compose up` | SFU / signalling. Portable to the eventual prod host. |
| Token server | `docker compose up` (same command — both containers come up together) | Mints join tokens for the iOS client. Requires a shared bearer token (`TOKEN_SERVER_AUTH_TOKEN`) — still dev-grade, not per-user auth. |
| Speech server (TTS) | `scripts/dev.sh` (native, via `uvx`) | MLX needs Apple Silicon directly — it can't run inside Docker. Also serves STT for any `STT_MODEL` it can route. |
| STT server | `scripts/dev.sh` (native, via `uv run --no-project`) | mlx-audio can't route the omi repo id, and the medical adapter has to be installed before the weights load. Its own uv environment on purpose — see "Swapping models". |
| Agent worker | `scripts/dev.sh` (same command — they all come up together) | The STT → LLM → TTS pipeline, VAD, turn detection, barge-in. Stays native for `download-files` weights and `dev` auto-reload. |

## First-time setup

```bash
brew install livekit-cli       # the `lk` CLI — drives the agent worker (dev/start/console)
brew install ffmpeg            # the omi/parakeet backends decode audio by shelling out to
                               # a *system* ffmpeg and have no bundled fallback
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
scripts/dev.sh                            # 2. speech server + STT server + agent worker
```

The token server's image builds automatically on the first `docker compose up`. After editing
`token_server.py`, it needs `docker compose up --build` — the script is baked into the image,
not bind-mounted like `livekit.yaml`, so a plain `up` keeps running the old code.

`scripts/dev.sh` starts `scripts/speech-server.sh`, `scripts/stt-server.sh` and
`lk agent dev agent.py` together (first
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
scripts/speech-server.sh                  # mlx-audio (TTS, and STT for mlx-audio models)
scripts/stt-server.sh                     # STT server (backend from STT_BACKEND)
set -a && source .env && set +a && lk agent dev agent.py   # agent worker
```

The agent warms the speech models itself at the start of every job (`_warm_speech_models()`
in `agent.py`), before it reports `lk.agent.state = "listening"` — so a client that waits for
that state gets a genuinely ready agent rather than one that still has to download Kokoro and
the STT model on the first turn. A warm-up failure is logged and the job continues, so a cold or
broken speech server degrades to a slow first turn rather than a dead session.

The STT server also preloads its own model at startup, and does it *after* binding the port so a
first-ever run (which fetches ~940 MB) makes requests queue rather than refusing connections.
That matters more than convenience: `livekit-plugins-openai` gives each transcription 30 s and
the timeout is not configurable from `agent.py`, so a lazily-loaded model would surface as a
mystery failure on the user's first sentence rather than a slow one. `curl localhost:8001/health`
reports `loaded` either way.

To warm them by hand anyway (or to check the servers directly):

```bash
curl -s localhost:8000/v1/audio/speech -H 'content-type: application/json' \
  -d '{"model":"mlx-community/Kokoro-82M-bf16","voice":"af_heart","input":"hello","response_format":"wav"}' -o /dev/null
curl -s localhost:8001/v1/audio/transcriptions -F file=@/path/to/any.wav -F response_format=json
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
`/v1/audio/transcriptions` and `/v1/audio/speech` works. One URL each — `STT_BASE_URL`
(`:8001`) and `TTS_BASE_URL` (`:8000`), both defaulted in `agent.py`, so `.env` only has to
name them when they differ from that.

- STT: `omi-health/omi-med-stt-v1-mlx-q8` (current default; English only, medical domain,
  **served by `scripts/stt-server.sh`, not mlx-audio**),
  `mlx-community/nemotron-3.5-asr-streaming-0.6b` (multilingual, general),
  `mlx-community/whisper-large-v3-asr-fp16` (multilingual, slowest, most robust),
  `mlx-community/parakeet-tdt-0.6b-v3` (English only, fastest)
- TTS: `mlx-community/Kokoro-82M-bf16` (or the `-8bit` / `-4bit` variants); voices: `af_heart`, `bf_alice`, …

`STT_BACKEND` picks which runtime `stt_server.py` loads. It is optional and defaults to
`omi`; the server itself always runs, so `STT_BASE_URL` always has a listener:

| `STT_BACKEND` | Loads via | For |
|---|---|---|
| `omi` | `omi_stt.mlx_runtime` | The medical model. The only backend with no alternative. |
| `parakeet` | `parakeet_mlx.from_pretrained` | Parakeet checkpoints |
| `mlx-audio` | `mlx_audio.stt.utils.load_model` | Whisper, Nemotron, ~24 others |
| `whisper`, `nemotron` | (aliases for `mlx-audio`) | Naming the family, when that's how you think about it |
| *unset* | (as `omi`) | The default |

The extra backends exist so this one server can serve everything, rather than because they're
needed — for anything mlx-audio can route, pointing `STT_BASE_URL` at `:8000` and ignoring
this server entirely is cheaper, since that process is already running.

So going back to a general model is `STT_MODEL` plus a matching `STT_BACKEND`.
`scripts/stt-server.sh` reads the same variable and installs only that backend's
dependencies, so the environment stays lean.

`dev.sh` starts the server unconditionally. It was briefly gated on `STT_BACKEND` being set,
which meant deleting that one line from `.env` failed as a **connection refused on the first
spoken turn** rather than at startup — everything came up looking healthy and only the first
sentence found the hole. The default model cannot be served by anything else, so this server
is part of the stack rather than an option.

Switching to a multilingual STT model also needs `STT_LANGUAGE` in `.env` updated to the
language you'll actually speak — `agent.py` still pins a single language per session, it
does not auto-detect. It is inert for the current default, which is English-only and has no
language conditioning at all.

**Not every model is servable by mlx-audio, and the failure is not obvious.** Its router picks
a module from `config.json["model_type"]`, falling back to the first dash-token of the repo
name and then to any name token matching a directory under `mlx_audio/stt/models/`. The omi
build ships a NeMo-style config with no usable `model_type` and no colliding name token, so it
resolves to a nonexistent `mlx_audio.stt.models.omi` and raises `ValueError: Model type omi not
supported for stt.` Even routed to the `parakeet` module it would not load: the rank-128
medical adapter has to be installed into every Conformer block *before* the weights, which the
vendor's runtime does by monkey-patching `parakeet_mlx.conformer.ConformerBlock.__call__`.
Their docs are blunt about it — "Do not call stock `parakeet-mlx` directly for this model."
`stt_server.py` is the HTTP around their loader that this implies; it runs in its own uv
environment (`uv run --no-project`) so that global monkey-patch, and a `numpy<2.3` ceiling,
never reach the agent worker.

That monkey-patch is also why the server loads **one backend and one model per process**, both
fixed by env, and ignores the request's `model` field. `_install_omi_adapter_runtime` doesn't
wrap `ConformerBlock.__call__`, it *replaces* it, process-wide and permanently, with its own
reimplementation of the block's forward pass. A plain Parakeet loaded afterwards would skip the
adapter (there's a `hasattr` guard) but still run that reimplementation.

It also serialises transcriptions on a single lock — one model, one MLX stream, and a vendor
`@lru_cache` that is not thread-safe on a miss — so two rooms on one worker will queue.

**Two things are tuned per model family, so revisit both when changing `STT_MODEL`:**

- The transcript blocklist in `agent.py` (`_NOISE_TOKENS`, and the shape of
  `_looks_like_noise` around it). Transducers — omi-med-stt, Nemotron, Parakeet — emit a token
  only when the audio supports one, so on noise they return an empty string and the worst they
  produce on marginal audio is a *truncated* real phrase. Whisper's decoder is autoregressive
  and trained on subtitle data, so near-silence makes it invent fluent boilerplate instead —
  "Thanks for watching", "Please subscribe", a bare "you". The Whisper phrase list is gone
  from the file rather than sitting there dead; `git log -S "amara" -- agent.py` brings it
  back if you switch families.

  This list did **not** change in the move to omi-med-stt, which is worth stating so it doesn't
  read as an oversight: omi is a fine-tune of `nvidia/parakeet-tdt-0.6b-v2`, the same TDT/RNNT
  family, and its adapter sits *inside* each encoder block — it changes what the encoder
  represents, not how the decoder emits. What is new is that the empty transcript now survives
  a hop that natively destroys it: `omi_stt.mlx_runtime.transcribe_mlx` *raises* on an empty
  result, and `stt_server.py` catches exactly that and returns `{"text": ""}`. Remove that
  catch and every noise segment becomes an HTTP 500 and a retry.
- Latency. Measured on this Mac, warm, median of five, same two clips:

  | model | 6.1 s clip | 1.8 s clip | on disk |
  |---|---|---|---|
  | `parakeet-tdt-0.6b-v3` | ~0.14 s | ~0.07 s | 0.6 GB |
  | `omi-med-stt-v1-mlx-q8` (current) | TBD | TBD | ~0.9 GB |
  | `nemotron-3.5-asr-streaming-0.6b` | ~0.71 s | ~0.23 s | 1.2 GB |
  | `whisper-large-v3-asr-fp16` | ~1.32 s | ~0.92 s | 2.9 GB |

  The omi row is deliberately unmeasured rather than estimated — every other number in this
  table came off this machine. Expect it near the Parakeet row (same architecture and
  parameter count) plus a fixed overhead the mlx-audio path doesn't pay: a temp-file write and
  an ffmpeg subprocess per request, order tens of milliseconds. Much worse than ~0.25 s on the
  6.1 s clip means the model is reloading per request — check `_load_model`'s cache is being
  hit, i.e. that the repo id string is identical every call.

  Whisper's cost is close to flat: it pads every clip to 30 s, so a short "yes" costs most
  of what a whole sentence does. Both transducers scale with the audio, which is why the gap
  widens on short turns — and voice turns are mostly short.

  Accuracy is not free at the fast end, but the gap is narrower than the sizes suggest. On
  the same clips degraded four ways (distant, noisy, VAD-clipped, noise-prefixed) Nemotron
  matched Whisper everywhere except one substitution under heavy additive noise, and beat it
  on the two noise-only clips, where Whisper returned "Thank you." and Nemotron returned
  nothing. Whisper's remaining edge is robustness on genuinely bad room audio; Parakeet's
  cost is being English-only.

  **Quantisation does not buy latency back on Whisper**: 4-bit and 8-bit builds both measured
  slightly *slower* than fp16, because the bottleneck is the fixed-size encoder pass rather
  than weight bandwidth. Distilled Whisper builds (fewer decoder layers) are genuinely
  faster, but pay for it in accuracy on exactly the audio that is already hard — accented
  speech and real rooms.

  Nemotron is a cache-aware *streaming* model, and none of that is being used here: it is
  driven batch-mode through `/v1/audio/transcriptions`, one WAV per VAD segment, like every
  other model above. Streaming is used for a different job — see "Interim transcripts".

Domain jargon is the other cost of a general-purpose model: it has never seen your
vocabulary, so it returns whatever ordinary English the audio resembled — Whisper renders
"Kubernetes" as "Kuber NetEase" and "Prometheus" as "ProMe the Us". Nothing in the pipeline
corrects that today, and the switch to a transducer removes the one lever that looked
promising rather than adding one. Both halves of that:

- Whisper has a `prompt` field, and `agent.py` could never reach it: **mlx-audio ignores
  `prompt` and `temperature` alike**, since its transcription handler declares neither and
  FastAPI drops unknown form fields silently (the same clip transcribes byte-identically
  with and without one; its own `context` and `text` fields do not bias Whisper either).
- Nemotron has no equivalent at all. Its only conditioning input is the language prompt
  index that `STT_LANGUAGE` selects, and mlx-audio's `nemotron_asr.generate()` takes no
  `hotwords` argument — unlike several other backends in that package, which route one
  through `merge_hotwords()` into whatever biasing field they natively have.

So biasing needs either a speech server that implements the full OpenAI shape *and* a model
family that has a prompt, or correction after the fact. An earlier attempt at the latter —
fuzzy-matching transcripts against a domain lexicon — was built and removed as not worth its
weight (`git log -- entity_correction.py`). It is back, narrower: `drug_lexicon.py` snaps
misheard *medical terms* onto a fixed vocabulary — drug names from `data/drugs.txt` and
condition names from `data/conditions.txt` — because even the medical fine-tune below still
mangles them and a greedy transducer has no other lever. `MEDICAL_LEXICON=` turns it off;
the `medical lexicon:` log lines show every correction it makes, which is how to judge
whether it is earning its keep this time.

Conditions were added after drugs and fail the same way — a long Latin or Greek compound
comes back as English-looking pieces ("a trial fibrillation", "my cardial infarction",
"thrombo cyto penia") — so they share one matcher and one scan. The risk they add is the
opposite one: ~370 more fuzzy candidates is ~370 more chances to capture ordinary speech,
where a correction is *worse* than a miss because nothing in the transcript shows it
happened. `test_ordinary_speech_is_untouched` pins that down; "an email" (one edit from
"anemia", rejected only by `LENGTH_RATIO`) is the canary for any threshold change. On the
30 hand-written manglings in `test_drug_lexicon.py` the combined lexicon corrects 30 and
fires on none of 101 ordinary sentences.

Holding that line as the lexicon grew needed one new rule: `SHORT_WINDOW_THRESHOLD` requires
a one- or two-word window to score 0.85 rather than 0.80. A four-word window folding to
within 20% of a drug ("lice in a pril" → lisinopril) is a coincidence English does not
produce; a two-word one is ("really nice" → repaglinide, 0.800; "to mention" → tolmetin,
0.824). Raising the flat `THRESHOLD` instead would drop "a tour of statin" → atorvastatin,
which also scores exactly 0.800 — the window length is what separates them, not the score.
It also retired a standing `xfail`: "I need a new statin" no longer becomes nystatin.

`data/conditions.txt` is hand-picked, not generated, and deliberately omits everyday
one-word complaints ("cough", "rash", "fever") that a general model already gets right:
they would add fuzzy attractors and buy nothing.

Three files ship, and the split is not cosmetic — it is what keeps the false-positive rate at
zero while the name count goes up:

| file | names | how it is curated |
| --- | --- | --- |
| `data/drugs.txt` | 250 | hand-picked commons; **the only place brand names live** |
| `data/ingredients.txt` | 3502 | generated: every FDA active ingredient |
| `data/conditions.txt` | 372 | hand-picked conditions |

`data/ingredients.txt` is regenerated from the Drugs@FDA data files zip
(https://www.fda.gov/media/89850/download — fda.gov is not reachable from every network, so
this is a manual step):

    uv run --no-project scripts/build_drug_lexicon.py ~/Downloads/drugsatfda.zip \
        --no-brands -o data/ingredients.txt

Active ingredients are lowercased and salt-stripped ("metformin", not "metformin
hydrochloride"); names with digits or symbols and generic "x and y" combinations are dropped.

**`--no-brands` is load-bearing.** Dropping it adds the FDA's 5228 brand names and wrecks
ordinary speech: measured over 101 non-medical sentences, the ingredients corrupt **0** and
the brands corrupt **6** — "for the" → Forteo, "the oven" → Theovent (0.93!), "violin" →
V-cillin, "I cannot" → Ycanth, "invoice" → Invirase, "machine" → Marcaine. Brand names are
coined to be short and English-shaped, which is exactly what a phonetic matcher cannot tell
from English; no threshold separates them, so they are excluded wholesale and the few dozen
patients actually say are kept by hand in `data/drugs.txt`. Re-run the probe before ever
loading them.

At ~4100 names the matcher blocks candidates by first letter and length (~3 ms per utterance),
and names under six folded letters only ever match exactly — too many short drug names are
also English words for a near miss to mean anything.

**The third option that paragraph missed is a model that already knows the vocabulary**, and
it is the one the current default takes. `omi-med-stt-v1` is `parakeet-tdt-0.6b-v2` fine-tuned
on clinical dialogue, dictation, medication review and procedure/device/test names; the vendor
measures ~97.6% recall on medical terms and 8.61% WER on 7.18 h of clinical speech. It needs no
prompt, no hotwords and no post-hoc correction, because the biasing is in the weights — which
is exactly why it sidesteps every blocker above.

The price is that this is a *narrowing*, not a widening: English only, and tuned toward
clinical language, so ordinary conversation gets slightly worse odds on vocabulary the
fine-tune wasn't built for. That is a domain decision rather than a latency one, and it is the
thing to measure before trusting it — see the A/B under "Tuning". If Nad ever needs a second
domain, the same shape applies: find a fine-tune, not a prompt.

## Interim transcripts

With a batch STT the client sees nothing while the user talks, then a whole sentence at
once. Setting `INTERIM_STT_MODEL` adds a second, *streaming* model whose only job is to show
words as they are spoken:

- `dual_stt.py` sends every audio frame to both. The streaming model's deltas become
  `INTERIM_TRANSCRIPT` events (display only — `AgentSession` never puts interims into the
  chat context); the batch model, in the same VAD `StreamAdapter` it always had, still
  produces `FINAL_TRANSCRIPT`, which is what the LLM reads and the app saves. So the noise
  gate and the medical lexicon are unaffected, and a general model showing "met foreman" for a
  second is replaced by the medical model's "metformin" when the turn ends.
- The streaming model is served by mlx-audio's `/v1/realtime` websocket (the OpenAI Realtime
  transcription protocol, which `livekit-plugins-openai` already speaks). In mlx-audio 0.5
  the only model that implements the server's streaming-session protocol is
  `mlx-community/Voxtral-Mini-4B-Realtime-2602-4bit`; Nemotron has the streaming internals
  but is not wired to that endpoint. The model loads on first connect; `agent.py` warms it
  during the initializing window alongside TTS and STT.
- Best-effort: if the realtime side is down the stream logs one warning and continues with
  finals only. The agent never goes deaf because the cosmetic path broke.

What this does *not* change is the time from the end of speech to the agent's first word.
For that, `agent.py` now logs `turn timing:` lines per turn — `eou` (end of speech → turn
decided), `stt` (→ transcript), `llm first token`, `tts first audio` — so a slow reply can
be pinned on a stage rather than guessed at.

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
thing deciding what the STT ever sees — so without a gate a fan or a TV becomes a turn.
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
3. **A transcript gate** in `NadAssistant.on_user_turn_completed` discards turns that are
   only filler ("uh", "mm"). A backstop: by then a preemptive LLM call has already been
   spent. Model-specific, and thinner than it was — the current transducer returns nothing
   at all on noise, where Whisper returned subtitle boilerplate that had to be listed out.
   On the current path that emptiness also has to survive an HTTP hop whose runtime treats
   it as an error; `stt_server.py` is what preserves it. See "Swapping models".
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

To debug *transcription* quality rather than gate behaviour, set `NAD_AUDIO_DUMP=/some/dir`
as well. Every accepted segment is written there as `segNNNN.wav` plus a `.txt` of what the
STT made of it — the exact bytes the model received, so a bad transcript can be replayed
against another model offline instead of guessed at. This records the user: opt in
deliberately, and delete the files afterwards.

### Measuring medical accuracy for real

Everything in `test_drug_lexicon.py` is a *hand-written* guess at how the model mishears a
term. That is enough to tune a threshold; it is not an accuracy number. `data/calibration.txt`
plus `scripts/medical_eval.py` produce one from real speech:

```bash
NAD_AUDIO_DUMP=/tmp/nad-medical scripts/dev.sh     # then read calibration.txt aloud,
                                                   # one line per turn
uv run --group dev scripts/medical_eval.py /tmp/nad-medical
```

The dumped `.txt` is the transcript *before* the lexicon runs, so one recording scores both
stages and reports every term as one of:

| | meaning |
| --- | --- |
| `raw` | the STT spelled it correctly by itself |
| `recovered` | the STT missed it, the lexicon put it back — the lexicon earning its keep |
| `lost` | neither; a real defect, and the line to add to the lexicon or escalate to the model |
| `corrupted` | a **control** line with no medical content got "corrected" — worse than lost |

`calibration.txt` is deliberately hostile: long compounds, near-homophone pairs
(losartan/valsartan, hydralazine/hydroxyzine, prednisone/prednisolone), and eight control
lines with no medical content at all, because the false-positive rate is the number that
actually matters. Alignment is by similarity rather than order, so a segment the gate drops
or a line you skip is reported unscored instead of silently shifting every later result.
Exit status is non-zero if anything was lost or corrupted, so it can gate a model swap.

Add `--stt http://localhost:8001/v1` to re-transcribe the wavs against a running server
instead of scoring the dumped text — that is how to compare two STT models on one recording
without speaking twice.

To get a number without speaking at all — useful for regression-checking a lexicon or
threshold change — synthesise the prompts with macOS `say` and feed them straight to the STT:

```bash
say -v Samantha --data-format=LEI16@16000 -o seg0000.wav "I take metformin twice a day."
curl -s -F model=omi -F file=@seg0000.wav localhost:8001/v1/audio/transcriptions
```

Do that for each non-comment line of `calibration.txt` into a directory, write each response's
`text` beside its wav as `segNNNN.txt`, and score it as above. It is not a substitute for a
human run — see the caveats under the table — but it is repeatable, and it is what the tiers
below were tuned on.

#### Where it stands

Measured against `omi-med-stt-v1-mlx-q8` on `calibration.txt` synthesised with three macOS
`say` voices — 61 terms each, same audio down every column:

| voice | STT alone | curated 250-name lexicon | current (4122 names + tiers) |
| --- | --- | --- | --- |
| Samantha (US) | 49/61 (80.3%) | 59/61 (96.7%) | **60/61 (98.4%)** |
| Karen (AU) | 42/61 (68.9%) | 55/61 (90.2%) | **59/61 (96.7%)** |
| Daniel (UK) | 44/61 (72.1%) | 55/61 (90.2%) | **57/61 (93.4%)** |
| all three | 135/183 (73.8%) | 169/183 (92.3%) | **176/183 (96.2%)** |

Control lines corrupted: **0**, in every run.

#### When it is still wrong

The ceiling above is not a tuning problem. The vendor measures ~97.6% recall on medical terms,
and every remaining lever in `drug_lexicon.py` trades precision for recall at a bad rate (see
its `skeleton` docstring for the two that were measured and refused). So the last defence is
not hearing better but **failing louder**: `MEDICAL_CONFIRM=1` makes the agent read a term back
when the lexicon recovered it from consonants alone.

Only the weakest tier qualifies. `"met formin"` → metformin is an exact fold match and is never
questioned; `"azampic"` → Ozempic matched on the spine `smpk` with every vowel discarded, and
that is the case worth a "you said Ozempic, is that right?". Off by default because it changes
how the agent talks — turn it on when transcripts are clinical rather than conversational. This
is standard practice for spoken medication orders, and it is the only mechanism here that
converts an unavoidable recognition error into a *caught* one rather than a silent one.

**Quantization is not costing anything.** The default `omi-med-stt-v1-mlx-q8` (941 MB) and the
full-precision `omi-med-stt-v1-mlx` (2497 MB) produced **byte-identical transcripts** across all
59 segments — same 49/61 raw, same 60/61 corrected. Don't reach for the bigger checkpoint hoping
to recover terms; on this material there is nothing there to recover, and q8 loads faster and
holds less memory. (Re-test if the audio changes character — this was synthesised speech.)

Two caveats that matter more than the numbers. **Synthetic speech flatters the model** — no
disfluency, no room, no accent drift — so treat these as a ceiling, not as real-use accuracy;
the human-voice run is still worth doing and will score lower. And **accent moves the result
more than any tuning did**: the UK voice sits five points below the US one on identical text,
which is the strongest argument for re-running this against your own voice before trusting
any of it.

Both later tiers came out of this measurement rather than from intuition. `skeleton()` exists
because Samantha's run lost `enoxaparin` to "anoxoperin" and `Ozempic` to "azampic" — vowel
errors at ~0.72, which `fold()` preserves by design. The non-word rule exists because
Daniel's run lost `Humira` to "humera" (0.833) and `prednisone` to "pridnisome" (0.800), both
just under the short-window bar and neither an English word.

That dump is also how to A/B the current domain-specific default against a general model —
worth doing before trusting it, since a medical fine-tune is a bet on your vocabulary. Talk
for a few minutes covering ordinary chit-chat, one-word answers ("yes", "stop"), whatever
domain terms matter, and some deliberate room noise with nobody speaking, then replay each
`segNNNN.wav` against the other model and diff against its `.txt`:

```bash
for f in /some/dir/*.wav; do
  echo "== $f"; cat "${f%.wav}.txt"          # what the default said, live
  curl -s localhost:8000/v1/audio/transcriptions \
    -F model=mlx-community/nemotron-3.5-asr-streaming-0.6b -F language=en \
    -F response_format=json -F file=@"$f"    # what a general model says
done
```

In priority order, what to look at: the **noise-only segments must still transcribe to `""`**
(the entire gate design rests on that, and a domain-biased decoder is exactly the kind of
thing that might start emitting a low-confidence clinical token on hiss — if any noise clip
returns text, `_NOISE_TOKENS` needs revisiting and the claims above need amending); then that
short answers survive, which is invisible in WER and very visible in a voice UI; then
general-English regressions, which are the expected cost and should be counted rather than
eyeballed; then domain terms, which are the expected win.

```bash
uv run --group dev pytest      # unit tests for the gate's decision logic and the STT server
```

## Notes

- The transcript the LLM reads is batch (one HTTP call per utterance) rather than streaming;
  `INTERIM_STT_MODEL` only adds live text for the screen. Latency is dominated by model
  speed on your Mac — see the table under "Swapping models" for measured numbers.
- The STT server is the second process that can't be containerised, for the same MLX reason,
  and it is deliberately in its own uv environment rather than a `pyproject.toml` dependency
  group. `mlx` publishes wheels only for macos-arm64 and `uv lock` resolves universally, so a
  group would make `uv.lock` unresolvable for the linux token-server image (`Dockerfile`:
  `uv sync --locked --only-group token-server`) — the same reason `mlx-audio` isn't in
  `pyproject.toml` either. Its pins live in `scripts/stt-server.sh`, next to the command
  that installs them.
- stt_server.py writes each uploaded segment to a temp file, because the vendor runtime takes
  paths rather than bytes, and unlinks it in a `finally`. Same privacy caveat as
  `NAD_AUDIO_DUMP`, minus the persistence.
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
