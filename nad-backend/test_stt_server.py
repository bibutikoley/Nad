"""Tests for the STT server's HTTP behaviour and backend wiring.

Deliberately covers only the wrapper -- request parsing, the response shape, temp-file
handling, the lock, backend selection -- with the models themselves stubbed out. Nothing
here downloads weights, loads MLX or touches the network, so the whole file runs in well
under a second. Transcription accuracy is covered by the manual sequence in README.md
instead.

This is also why every backend in `stt_server` imports its dependencies *inside* its loader:
it lets this file import the module with none of omi-med-stt, parakeet-mlx, mlx-audio or
ffmpeg installed in .venv, which they deliberately are not -- see scripts/stt-server.sh.

Run:  uv run --group dev pytest
"""

from __future__ import annotations

import asyncio
import io
import sys
import time
import types
import wave
from pathlib import Path

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import stt_server


def _wav(seconds: float = 0.1, sample_rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    return buffer.getvalue()


@pytest.fixture
async def client():
    """A server with no preload hook -- the backend is stubbed per test."""
    async with TestClient(TestServer(stt_server.build_app())) as client:
        yield client


@pytest.fixture(autouse=True)
def _no_leaked_backend():
    """`_transcriber` is module-level state cached across calls; don't let it cross tests."""
    stt_server._transcriber = None
    yield
    stt_server._transcriber = None


async def _post(client, payload: bytes, **fields: str):
    form = FormData()
    for name, value in fields.items():
        form.add_field(name, value)
    form.add_field("file", payload, filename="file.wav", content_type="audio/wav")
    return await client.post("/v1/audio/transcriptions", data=form)


def _fake_omi_runtime(monkeypatch, transcribe):
    """Install a stand-in for the vendor package so `_load_omi` can be exercised for real."""
    module = types.ModuleType("omi_stt.mlx_runtime")
    module.transcribe_mlx = transcribe
    package = types.ModuleType("omi_stt")
    package.mlx_runtime = module
    monkeypatch.setitem(sys.modules, "omi_stt", package)
    monkeypatch.setitem(sys.modules, "omi_stt.mlx_runtime", module)


# --- the empty-transcript contract ------------------------------------------------------


def test_the_omi_backend_turns_an_empty_transcript_into_a_string(monkeypatch):
    """The single most important behaviour in the file.

    The vendor's `transcribe_mlx` raises on an empty result; this pipeline needs an empty
    *string*. `stt.StreamAdapter` drops empty transcripts outright, so a rejected segment
    costs no turn and no LLM call -- but only if it arrives as one. Let this become a 500
    and every fan and desk thump becomes an API error and a retry. See agent.py's
    `_NOISE_TOKENS` comment and CLAUDE.md.

    Exercises the real `_load_omi` against a stubbed vendor module, rather than stubbing
    out the translation being tested.
    """

    def transcribe_mlx(paths, repo_id):
        raise RuntimeError(f"MLX runtime produced an empty transcript for {paths[0]}")

    _fake_omi_runtime(monkeypatch, transcribe_mlx)
    assert stt_server._load_omi()("/tmp/whatever.wav") == ""


def test_the_omi_backend_does_not_swallow_a_real_error(monkeypatch):
    """Only the empty-transcript RuntimeError is translated.

    parakeet-mlx raises RuntimeError for a missing ffmpeg and for decode failures too.
    Reporting those as "the user said nothing" would hide a broken install behind a
    permanently deaf agent.
    """

    def transcribe_mlx(paths, repo_id):
        raise RuntimeError("FFmpeg is not installed or not in your PATH.")

    _fake_omi_runtime(monkeypatch, transcribe_mlx)
    with pytest.raises(RuntimeError, match="FFmpeg"):
        stt_server._load_omi()("/tmp/whatever.wav")


async def test_a_backend_error_reaches_the_client_as_a_failure(client, monkeypatch):
    def stub(path: str) -> str:
        raise RuntimeError("FFmpeg is not installed or not in your PATH.")

    monkeypatch.setattr(stt_server, "_transcriber", stub)

    response = await _post(client, _wav())
    assert response.status >= 500


# --- backend selection ------------------------------------------------------------------


def test_every_backend_name_resolves_to_a_loader():
    assert set(stt_server._BACKENDS) == {"omi", "parakeet", "mlx-audio", "whisper", "nemotron"}


def test_whisper_and_nemotron_are_aliases_for_the_mlx_audio_loader():
    """They need no separate implementation -- mlx-audio routes them from the repo config."""
    assert stt_server._BACKENDS["whisper"] is stt_server._load_mlx_audio
    assert stt_server._BACKENDS["nemotron"] is stt_server._load_mlx_audio


def test_an_unknown_backend_exits_with_the_valid_names(monkeypatch):
    monkeypatch.setattr(stt_server, "BACKEND", "sphinx")
    with pytest.raises(SystemExit) as exc:
        stt_server._check_config()
    assert "omi" in str(exc.value) and "mlx-audio" in str(exc.value)


async def test_the_backend_is_built_once_and_reused(client, monkeypatch):
    """`_transcriber` caches the loaded model; rebuilding per request would reload weights."""
    builds: list[int] = []

    def loader():
        builds.append(1)
        return lambda path: "ok"

    monkeypatch.setitem(stt_server._BACKENDS, stt_server.BACKEND, loader)
    for _ in range(3):
        response = await _post(client, _wav())
        assert response.status == 200
    assert len(builds) == 1


# --- request/response shape -------------------------------------------------------------


async def test_transcript_round_trips(client, monkeypatch):
    monkeypatch.setattr(stt_server, "_transcriber", lambda path: "patient denies chest pain")

    response = await _post(client, _wav(), model="omi-health/omi-med-stt-v1-mlx-q8", language="en")
    assert response.status == 200
    assert (await response.json())["text"] == "patient denies chest pain"


async def test_the_uploaded_bytes_reach_the_backend_intact(client, monkeypatch):
    """Guards the streamed multipart read -- a truncated WAV would transcribe to plausible
    nonsense rather than failing, which is the worst way for this to break."""
    seen: dict[str, bytes] = {}

    def stub(path: str) -> str:
        seen["bytes"] = Path(path).read_bytes()
        return "ok"

    monkeypatch.setattr(stt_server, "_transcriber", stub)

    payload = _wav(seconds=0.5)
    await _post(client, payload)
    assert seen["bytes"] == payload


async def test_the_request_model_field_does_not_choose_the_weights(client, monkeypatch):
    """A request must not be able to trigger an arbitrary multi-GB HuggingFace download,
    nor load a second model into a process whose ConformerBlock has been replaced."""
    seen: list[str] = []
    monkeypatch.setattr(stt_server, "_transcriber", lambda path: seen.append(stt_server.MODEL) or "ok")

    await _post(client, _wav(), model="somebody-else/some-other-model")
    assert seen == [stt_server.MODEL]


async def test_a_missing_file_part_is_a_400(client, monkeypatch):
    monkeypatch.setattr(stt_server, "_transcriber", lambda path: "unreachable")

    form = FormData()
    form.add_field("model", "omi-health/omi-med-stt-v1-mlx-q8")
    response = await client.post("/v1/audio/transcriptions", data=form)
    assert response.status == 400


async def test_health_reports_the_backend_and_model(client):
    body = await (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["backend"] == stt_server.BACKEND
    assert body["model"] == stt_server.MODEL


# --- resource handling ------------------------------------------------------------------


async def test_a_long_upload_is_not_rejected(client, monkeypatch):
    """aiohttp's 1 MB default would 413 anything past ~11 s at 48 kHz, and the VAD buffers
    up to 60 s. This is the kind of bug that only appears on a long sentence in a real call."""
    monkeypatch.setattr(stt_server, "_transcriber", lambda path: "ok")

    response = await _post(client, _wav(seconds=30.0, sample_rate=48000))
    assert response.status == 200


@pytest.mark.parametrize("outcome", ["ok", "raise"])
async def test_the_temp_file_is_always_removed(client, monkeypatch, outcome):
    paths: list[str] = []

    def stub(path: str) -> str:
        paths.append(path)
        if outcome == "raise":
            raise ValueError("boom")
        return "ok"

    monkeypatch.setattr(stt_server, "_transcriber", stub)

    await _post(client, _wav())
    assert paths and not Path(paths[0]).exists()


async def test_transcriptions_never_overlap(client, monkeypatch):
    """One model, one MLX stream, and on the omi backend a process-global monkey-patched
    ConformerBlock: concurrent calls buy nothing and the vendor's model cache is not
    thread-safe on a miss."""
    inside = False
    overlapped = False

    def stub(path: str) -> str:
        nonlocal inside, overlapped
        if inside:
            overlapped = True
        inside = True
        time.sleep(0.05)
        inside = False
        return "ok"

    monkeypatch.setattr(stt_server, "_transcriber", stub)

    await asyncio.gather(*(_post(client, _wav()) for _ in range(4)))
    assert not overlapped
