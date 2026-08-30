"""OpenAI-compatible STT server with a pluggable backend, selected by `STT_BACKEND`.

    POST /v1/audio/transcriptions   (multipart: file=<wav>, ...)  ->  {"text": ...}
    GET  /health                                                  ->  {"status", "backend", "model", "loaded"}

It exists because of one model. `omi-health/omi-med-stt-v1-*` cannot be served by the
mlx-audio server this project already runs, twice over: mlx-audio's router picks a module
from `config.json["model_type"]`, falling back to the first dash-token of the repo name, and
the omi repo ships a NeMo-style config with no usable `model_type` and no name token matching
a directory under `mlx_audio/stt/models/` -- so it resolves to a nonexistent
`mlx_audio.stt.models.omi` and raises "Model type omi not supported for stt." And even routed
correctly it would not load, because the rank-128 medical adapter has to be installed into
every Conformer block *before* the weights, which the vendor's runtime does by monkey-patching
`parakeet_mlx.conformer.ConformerBlock.__call__`. Their docs: "Do not call stock
`parakeet-mlx` directly for this model."

The other backends are here so one server can serve everything if you prefer that to running
two. They are not *needed*: for anything mlx-audio can route, pointing `STT_BASE_URL` at the
mlx-audio server on :8000 is the cheaper path -- that process is already running, and this one
would cost a second resident copy of the weights. `omi` is the backend with no alternative.

    STT_BACKEND=omi                     the vendor runtime, adapter and all (default)
    STT_BACKEND=parakeet                parakeet-mlx directly
    STT_BACKEND=mlx-audio               mlx-audio's own router -- whisper, nemotron, +20 more
    STT_BACKEND=whisper | nemotron      aliases for mlx-audio, which auto-routes from config

Each backend installs a different dependency set -- see scripts/stt-server.sh, which
switches its `--with` flags on the same variable so the environment stays lean.

**One backend per process, by design.** `_install_omi_adapter_runtime` does not wrap
`ConformerBlock.__call__`, it *replaces* it, process-wide and permanently, with its own
reimplementation of the block's forward pass. A non-omi Parakeet loaded afterwards would skip
the adapter (there is a `hasattr` guard) but still run that reimplementation. So the backend
is fixed by env, the request's `model` field is ignored, and nothing here loads a second model.

Binds localhost and takes no auth, unlike token_server.py: that one is on the LAN for the iOS
client and needs a shared secret, whereas nothing but the agent worker on this machine ever
talks to this. The bind address *is* the protection -- don't move it off the loopback without
adding one.

Run:  scripts/stt-server.sh
"""

import asyncio
import inspect
import io
import logging
import os
import shutil
import sys
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nad.stt")

BACKEND = os.environ.get("STT_BACKEND", "omi").strip().lower()
MODEL = os.environ.get("STT_MODEL", "omi-health/omi-med-stt-v1-mlx-q8")
LANGUAGE = os.environ.get("STT_LANGUAGE", "").strip()
HOST = os.environ.get("STT_SERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("STT_SERVER_PORT", "8001"))

# aiohttp's default is 1 MB, which is smaller than the audio this actually receives:
# `inference.VAD`'s max_buffered_speech is 60 s, so one segment can reach ~5.8 MB of WAV at
# 48 kHz -- and even an ordinary 15 s sentence clears 1 MB. Left at the default, a long turn
# would fail as a 413 instead of transcribing. token_server.py never hits this because it
# only ever takes JSON.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

# One model, one MLX stream, and (on the omi backend) an adapter patched into a class the
# whole process shares. Overlapping calls buy nothing and cost two ways: the vendor's
# `_load_model` is `@lru_cache`d but not thread-safe on a miss, so two cold requests would
# each download and build the model. The agent sends one segment at a time anyway; two rooms
# on one worker will queue.
_lock = asyncio.Lock()
_loaded = False

# Built once, on first use, under the lock. Every backend resolves to the same shape:
# a blocking callable taking a path and returning the transcript, "" for "no words here".
_transcriber: Callable[[str], str] | None = None


# --- backends ---------------------------------------------------------------------------
#
# Each loader imports its own dependencies lazily, so this module can be imported -- and its
# HTTP behaviour tested -- with none of them installed. They deliberately are not in .venv;
# see scripts/stt-server.sh.


def _load_omi() -> Callable[[str], str]:
    """The vendor runtime, which installs the medical adapter before loading the weights.

    Its `transcribe_mlx` *raises* on an empty transcript, which inverts the contract this
    whole pipeline rests on -- `noise_gate.py` returns an empty transcript for segments it
    rejects, and `stt.StreamAdapter` drops empty transcripts without starting a turn, so
    "nothing was said" has to come back as 200 + "" rather than an error. Translate it here.

    Matched on the message rather than the type on purpose: ffmpeg and decode failures raise
    RuntimeError too, and those are real errors that must propagate -- reporting them as "the
    user said nothing" would hide a broken install behind a permanently deaf agent.
    """
    from omi_stt.mlx_runtime import transcribe_mlx

    def transcribe(path: str) -> str:
        try:
            return transcribe_mlx([path], MODEL)[0]
        except RuntimeError as exc:
            if "empty transcript" in str(exc):
                return ""
            raise

    return transcribe


def _load_parakeet() -> Callable[[str], str]:
    """parakeet-mlx directly. Returns an AlignedResult, whose .text is already stripped.

    No empty-transcript translation needed: unlike the vendor wrapper above, parakeet-mlx
    returns an empty string rather than raising.
    """
    from parakeet_mlx import from_pretrained

    model = from_pretrained(MODEL)

    def transcribe(path: str) -> str:
        return model.transcribe(Path(path)).text

    return transcribe


def _load_mlx_audio() -> Callable[[str], str]:
    """mlx-audio's own STT router -- whisper, nemotron, and ~24 other architectures.

    One branch rather than one per model: `load_model` reads the repo's config and picks the
    implementation itself, so this covers everything mlx-audio can serve without us
    re-deriving its routing table. Returns an STTOutput, whose .text is the transcript.

    Kwargs are filtered against the chosen model's own `generate` signature, the same way
    mlx-audio's server does it (`STTExecutionAdapter.run_serial`) -- the backends disagree
    about which parameters exist, and passing an unknown one is a TypeError rather than a
    silently-ignored field.
    """
    from mlx_audio.stt.utils import load_model

    model = load_model(MODEL)
    accepted = inspect.signature(model.generate).parameters

    def transcribe(path: str) -> str:
        kwargs = {}
        if LANGUAGE and "language" in accepted:
            kwargs["language"] = LANGUAGE
        return (model.generate(path, **kwargs).text or "").strip()

    return transcribe


_BACKENDS: dict[str, Callable[[], Callable[[str], str]]] = {
    "omi": _load_omi,
    "parakeet": _load_parakeet,
    "mlx-audio": _load_mlx_audio,
    # Aliases: mlx-audio routes these from the repo's config, so they need no separate
    # implementation -- they are accepted because naming the model family is how you think
    # about it when editing .env.
    "whisper": _load_mlx_audio,
    "nemotron": _load_mlx_audio,
}

# Backends that decode audio through parakeet-mlx, which shells out to a *system* ffmpeg by
# name (`shutil.which("ffmpeg") is None` -> RuntimeError) with no bundled fallback. omi's own
# `omi_stt.audio` has an imageio-ffmpeg path, but `transcribe_mlx` never takes it.
_NEEDS_SYSTEM_FFMPEG = {"omi", "parakeet"}


def _check_config() -> None:
    """Fail at startup rather than three libraries deep on the first real utterance."""
    if BACKEND not in _BACKENDS:
        sys.exit(
            f"STT_BACKEND={BACKEND!r} is not one of: {', '.join(sorted(_BACKENDS))}. "
            "For a model mlx-audio can serve, the cheaper option is to not run this process "
            "at all and point STT_BASE_URL at the mlx-audio server instead."
        )
    if BACKEND in _NEEDS_SYSTEM_FFMPEG and shutil.which("ffmpeg") is None:
        sys.exit(
            f"ffmpeg is not on PATH. The {BACKEND!r} backend decodes audio by shelling out "
            "to it and has no fallback. Install it with: brew install ffmpeg"
        )


def _transcribe(path: str) -> str:
    """Blocking. Builds the backend on first call, then reuses it."""
    global _transcriber
    if _transcriber is None:
        _transcriber = _BACKENDS[BACKEND]()
    return _transcriber(path)


# --- HTTP -------------------------------------------------------------------------------


async def transcriptions(request: web.Request) -> web.Response:
    """The OpenAI shape, as far as livekit-plugins-openai actually uses it.

    It POSTs `file` plus `model`, `language` and `response_format=json`, then reads nothing
    but `.text` off the reply (see `livekit/plugins/openai/stt.py::_recognize_impl`). The
    rest is accepted and ignored.

    `model` in particular does *not* select the weights: this serves exactly one repo id on
    exactly one backend, both fixed by env. Honouring the request field would let an
    unauthenticated localhost POST trigger an arbitrary multi-GB HuggingFace download, and
    on the omi backend it would let a second model load into a process whose
    `ConformerBlock.__call__` has already been replaced.
    """
    # Checked rather than assumed: aiohttp's MultipartReader asserts on the content type,
    # which would surface a malformed client as a 500 and a stack trace.
    if not (request.content_type or "").startswith("multipart/"):
        raise web.HTTPBadRequest(reason="expected a multipart/form-data body")

    reader = await request.multipart()
    path: str | None = None

    try:
        async for part in reader:
            if part.name != "file":
                continue
            # Streamed to disk rather than buffered: every backend takes a path, not bytes,
            # and these are multi-megabyte WAVs. This transiently writes user audio to
            # TMPDIR -- same privacy caveat as NAD_AUDIO_DUMP, minus the persistence.
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                path = tmp.name
                while chunk := await part.read_chunk():
                    tmp.write(chunk)
            break

        if path is None:
            raise web.HTTPBadRequest(reason="no 'file' part in the request")

        async with _lock:
            text = await asyncio.to_thread(_transcribe, path)
        return web.json_response({"text": text})
    finally:
        if path is not None:
            Path(path).unlink(missing_ok=True)


async def health(request: web.Request) -> web.Response:
    """`loaded` is what tells you whether a slow first request is the model still arriving."""
    return web.json_response(
        {"status": "ok", "backend": BACKEND, "model": MODEL, "loaded": _loaded}
    )


# --- warm-up ----------------------------------------------------------------------------


def _silent_wav() -> bytes:
    """A quarter-second of digital silence, for the warm-up below."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * 4000)
    return buffer.getvalue()


def _warm_blocking() -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(_silent_wav())
        path = tmp.name
    try:
        _transcribe(path)
    finally:
        Path(path).unlink(missing_ok=True)


async def _preload(app: web.Application) -> None:
    """Load the weights in the background, holding the lock so requests queue behind it.

    Deliberately not awaited: `on_startup` runs inside `AppRunner.setup()`, *before* the port
    is bound. Blocking here would mean a first-ever run -- which fetches ~940 MB on the omi
    backend -- refuses connections for the whole download, and the agent's own
    `_warm_speech_models()` would get ECONNREFUSED rather than a request that simply waits.
    Bind first, let the lock do the waiting.

    Preloading at all matters because the agent's STT client gives each request 30 s
    (`httpx.Timeout(30, ...)` in the plugin, not configurable from agent.py). Loaded lazily
    on the first real segment, a cold start would surface as a mystery timeout on the user's
    first sentence.

    Goes through the same path a real request takes, so a broken install shows up here rather
    than mid-call. Silence transcribes to "", which every backend handles without raising --
    so on omi this also proves its empty-transcript translation works, on every boot.
    """

    async def run() -> None:
        global _loaded
        logger.info("loading %s on the %r backend …", MODEL, BACKEND)
        async with _lock:
            try:
                await asyncio.to_thread(_warm_blocking)
            except Exception:
                # Best-effort, like agent.py's own warm-up: the first real request retries.
                # A slow first turn beats a dead server.
                logger.warning("preload failed; the first request will retry", exc_info=True)
                return
            _loaded = True
            logger.info("model ready")

    app["preload"] = asyncio.create_task(run())


async def _cancel_preload(app: web.Application) -> None:
    task = app.get("preload")
    if task is not None:
        task.cancel()


def build_app() -> web.Application:
    """A factory rather than a module-level app, so tests can build one without the preload."""
    app = web.Application(client_max_size=MAX_UPLOAD_BYTES)
    app.add_routes(
        [
            web.post("/v1/audio/transcriptions", transcriptions),
            # Alias: STT_BASE_URL-style clients already carry the /v1, but a bare curl
            # against this server shouldn't have to guess.
            web.post("/audio/transcriptions", transcriptions),
            web.get("/health", health),
        ]
    )
    return app


if __name__ == "__main__":
    _check_config()
    app = build_app()
    app.on_startup.append(_preload)
    app.on_cleanup.append(_cancel_preload)
    web.run_app(app, host=HOST, port=PORT)
