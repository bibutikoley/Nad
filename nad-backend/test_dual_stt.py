"""Tests for the interim + final STT merge.

Everything is stubbed: a VAD that treats every `flush()` as one utterance, a batch STT that
returns a fixed transcript, and a streaming STT that emits whatever events the test scripts.
No models, no network, no room.

Run:  uv run --group dev pytest test_dual_stt.py
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from livekit import rtc
from livekit.agents import stt, vad
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
)

from dual_stt import DualSTT

SpeechEventType = stt.SpeechEventType


def _frame(*, speech: bool = True) -> rtc.AudioFrame:
    """20 ms of audio. The stub VAD reads `speech` off the samples: any non-zero is speech."""
    n = 16000 * 20 // 1000
    data = np.full(n, 1000 if speech else 0, dtype=np.int16)
    return rtc.AudioFrame(
        data=data.tobytes(), sample_rate=16000, num_channels=1, samples_per_channel=n
    )


# --- stubs ---------------------------------------------------------------------------


class _FlushVAD(vad.VAD):
    """One segment per flush(): START_OF_SPEECH on the first speech frame, END on flush.

    Silent frames are ignored, like a real VAD ignores room tone."""

    def __init__(self) -> None:
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.02))

    def stream(self) -> vad.VADStream:
        return _FlushVADStream(self)


class _FlushVADStream(vad.VADStream):
    async def _main_task(self) -> None:
        frames: list[rtc.AudioFrame] = []
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                if frames:
                    self._event_ch.send_nowait(
                        vad.VADEvent(
                            type=vad.VADEventType.END_OF_SPEECH,
                            samples_index=0,
                            timestamp=0.0,
                            speech_duration=0.02 * len(frames),
                            silence_duration=0.0,
                            frames=frames,
                        )
                    )
                    frames = []
                continue
            if not np.frombuffer(item.data, dtype=np.int16).any():
                continue
            if not frames:
                self._event_ch.send_nowait(
                    vad.VADEvent(
                        type=vad.VADEventType.START_OF_SPEECH,
                        samples_index=0,
                        timestamp=0.0,
                        speech_duration=0.0,
                        silence_duration=0.0,
                    )
                )
            frames.append(item)


class _BatchSTT(stt.STT):
    def __init__(self, text: str = "I take metformin") -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False)
        )
        self.text = text
        self.calls = 0

    @property
    def model(self) -> str:
        return "batch-model"

    @property
    def provider(self) -> str:
        return "batch-provider"

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options):
        self.calls += 1
        return stt.SpeechEvent(
            type=SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="en", text=self.text)],
        )


class _StreamingSTT(stt.STT):
    """Emits the scripted events, one per pushed frame, then whatever is left on end_input."""

    def __init__(
        self, script: list[stt.SpeechEvent] | None = None, *, fail: bool = False
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=True, interim_results=True)
        )
        self.script = list(script or [])
        self.fail = fail
        self.frames = 0

    @property
    def model(self) -> str:
        return "stream-model"

    @property
    def provider(self) -> str:
        return "stream-provider"

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options):
        raise NotImplementedError

    def stream(self, *, language=NOT_GIVEN, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return _StreamingStream(self, conn_options=conn_options)


class _StreamingStream(stt.RecognizeStream):
    def __init__(self, stt_: _StreamingSTT, *, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=stt_, conn_options=conn_options)
        self._owner = stt_

    async def _run(self) -> None:
        if self._owner.fail:
            raise RuntimeError("realtime server unreachable")
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                continue
            self._owner.frames += 1
            if self._owner.script:
                # A real streaming model answers hundreds of ms after the audio; the VAD's
                # START_OF_SPEECH (a few hops away in the final path) is long gone by then.
                await asyncio.sleep(0.01)
                self._event_ch.send_nowait(self._owner.script.pop(0))


def _interim(text: str) -> stt.SpeechEvent:
    return stt.SpeechEvent(
        type=SpeechEventType.INTERIM_TRANSCRIPT,
        alternatives=[stt.SpeechData(language="en", text=text)],
    )


def _final(text: str) -> stt.SpeechEvent:
    return stt.SpeechEvent(
        type=SpeechEventType.FINAL_TRANSCRIPT,
        alternatives=[stt.SpeechData(language="en", text=text)],
    )


async def _drive(dual: DualSTT, *, frames_then_flush: list[int]) -> list[stt.SpeechEvent]:
    """Push `n` frames then flush, for each n; collect every event the merged stream emits."""
    stream = dual.stream()
    events: list[stt.SpeechEvent] = []

    async def collect() -> None:
        async for ev in stream:
            events.append(ev)

    task = asyncio.create_task(collect())
    for n in frames_then_flush:
        for _ in range(n):
            stream.push_frame(_frame())
            await asyncio.sleep(0.02)  # one scripted event per frame, in order
        stream.flush()
        await asyncio.sleep(0.05)
    stream.end_input()
    await asyncio.wait_for(task, 2)
    await stream.aclose()
    return events


def _types(events: list[stt.SpeechEvent]) -> list[SpeechEventType]:
    return [e.type for e in events]


def _texts(events: list[stt.SpeechEvent], type_: SpeechEventType) -> list[str]:
    return [e.alternatives[0].text for e in events if e.type == type_]


# --- tests ---------------------------------------------------------------------------


def test_capabilities_advertise_streaming_with_interims():
    dual = DualSTT(final=_BatchSTT(), interim=_StreamingSTT(), vad=_FlushVAD())
    assert dual.capabilities.streaming is True
    assert dual.capabilities.interim_results is True
    assert dual.model == "batch-model"


def test_rejects_the_wrong_kinds_of_stt():
    with pytest.raises(ValueError):
        DualSTT(final=_StreamingSTT(), interim=_StreamingSTT(), vad=_FlushVAD())
    with pytest.raises(ValueError):
        DualSTT(final=_BatchSTT(), interim=_BatchSTT(), vad=_FlushVAD())


async def test_interims_come_from_the_streaming_stt_and_finals_from_the_batch_one():
    batch = _BatchSTT("I take metformin")
    streaming = _StreamingSTT([_interim("I"), _interim("I take"), _interim("I take met")])
    events = await _drive(
        DualSTT(final=batch, interim=streaming, vad=_FlushVAD()), frames_then_flush=[3]
    )

    assert _texts(events, SpeechEventType.INTERIM_TRANSCRIPT) == ["I", "I take", "I take met"]
    assert _texts(events, SpeechEventType.FINAL_TRANSCRIPT) == ["I take metformin"]
    assert batch.calls == 1
    assert streaming.frames == 3


async def test_event_order_is_start_interims_end_final():
    streaming = _StreamingSTT([_interim("a"), _interim("ab")])
    events = await _drive(
        DualSTT(final=_BatchSTT(), interim=streaming, vad=_FlushVAD()), frames_then_flush=[2]
    )

    assert _types(events) == [
        SpeechEventType.START_OF_SPEECH,
        SpeechEventType.INTERIM_TRANSCRIPT,
        SpeechEventType.INTERIM_TRANSCRIPT,
        SpeechEventType.END_OF_SPEECH,
        SpeechEventType.FINAL_TRANSCRIPT,
    ]


async def test_streaming_finals_and_speech_markers_are_dropped():
    """Only the batch side may say what was said or when speech started and stopped."""
    streaming = _StreamingSTT(
        [
            stt.SpeechEvent(type=SpeechEventType.START_OF_SPEECH),
            _final("eye take met foreman"),
            stt.SpeechEvent(type=SpeechEventType.END_OF_SPEECH),
        ]
    )
    events = await _drive(
        DualSTT(final=_BatchSTT(), interim=streaming, vad=_FlushVAD()), frames_then_flush=[3]
    )

    assert _texts(events, SpeechEventType.FINAL_TRANSCRIPT) == ["I take metformin"]
    assert _types(events).count(SpeechEventType.START_OF_SPEECH) == 1
    assert _types(events).count(SpeechEventType.END_OF_SPEECH) == 1


async def test_a_late_interim_after_the_final_is_suppressed():
    """A delta that lands after the batch transcript would otherwise linger on screen."""
    # Two frames in the segment, so the segment's own interim is emitted on frame 1 and the
    # late one on the first frame of the *next* push, which arrives after the final.
    streaming = _StreamingSTT([_interim("I take"), _interim("I take met foreman")])
    dual = DualSTT(final=_BatchSTT(), interim=streaming, vad=_FlushVAD())
    stream = dual.stream()
    events: list[stt.SpeechEvent] = []

    async def collect() -> None:
        async for ev in stream:
            events.append(ev)

    task = asyncio.create_task(collect())
    stream.push_frame(_frame())
    await asyncio.sleep(0.02)
    stream.flush()
    await asyncio.sleep(0.05)  # batch final lands, gate closes
    # Trailing silence (the VAD opens no new segment) carries the stale delta.
    stream.push_frame(_frame(speech=False))
    await asyncio.sleep(0.05)
    stream.end_input()
    await asyncio.wait_for(task, 2)
    await stream.aclose()

    assert _texts(events, SpeechEventType.INTERIM_TRANSCRIPT) == ["I take"]
    assert _texts(events, SpeechEventType.FINAL_TRANSCRIPT) == ["I take metformin"]
    assert streaming.frames == 2  # the streaming side did see the trailing frame


async def test_interim_failure_degrades_to_finals_only():
    streaming = _StreamingSTT(fail=True)
    events = await _drive(
        DualSTT(final=_BatchSTT(), interim=streaming, vad=_FlushVAD()),
        frames_then_flush=[3, 3],
    )

    assert _texts(events, SpeechEventType.INTERIM_TRANSCRIPT) == []
    assert _texts(events, SpeechEventType.FINAL_TRANSCRIPT) == [
        "I take metformin",
        "I take metformin",
    ]
