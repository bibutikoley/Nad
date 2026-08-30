"""Tests for the noise gate's decision logic.

Deliberately covers only the pure parts -- the level measurement, the accept/reject rules
and the transcript predicate -- so the whole file runs in under a second with no LiveKit
session, no network and no audio device. The parts that need a real room are covered by the
console smoke test in README.md instead.

Segments are synthesised in the shape the VAD actually produces: `prefix_padding_duration`
of ambient, then the event, then `min_silence_duration` of ambient again. Testing against
bare speech would miss the padding, which is the thing the measurement is designed around.

Run:  uv run --group dev pytest
"""

from __future__ import annotations

import numpy as np
import pytest
from livekit import rtc
from livekit.agents import llm, stt
from livekit.agents.types import NOT_GIVEN

import agent as agent_mod
from agent import DrugCorrectedSTT, NadAssistant, _looks_like_noise
from drug_lexicon import DrugLexicon
from noise_gate import (
    FLOOR_CLAMP,
    MIN_VOICED_MS,
    SNR_MIN_DB,
    NoiseGate,
    NoiseGatedSTT,
    measure,
)

SAMPLE_RATE = 24000

# Matches inference.VAD's defaults, which agent.py pins.
PREFIX_PAD_S = 0.5
TRAILING_SILENCE_S = 0.25


def _noise(duration_s: float, dbfs: float, rng: np.random.Generator) -> np.ndarray:
    """Gaussian noise at a target RMS level. Stands in for room tone."""
    amplitude = (10.0 ** (dbfs / 20.0)) * 32768.0
    return rng.normal(0.0, amplitude, int(duration_s * SAMPLE_RATE))


def _segment(
    *,
    ambient_dbfs: float,
    body_dbfs: float,
    body_s: float,
    seed: int = 0,
) -> rtc.AudioFrame:
    """A VAD segment: pre-roll ambient, the thing that triggered it, trailing ambient."""
    rng = np.random.default_rng(seed)
    parts = [
        _noise(PREFIX_PAD_S, ambient_dbfs, rng),
        _noise(body_s, body_dbfs, rng),
        _noise(TRAILING_SILENCE_S, ambient_dbfs, rng),
    ]
    pcm = np.clip(np.concatenate(parts), -32768, 32767).astype(np.int16)
    return rtc.AudioFrame(
        data=pcm.tobytes(),
        sample_rate=SAMPLE_RATE,
        num_channels=1,
        samples_per_channel=len(pcm),
    )


def _frame(samples: np.ndarray) -> rtc.AudioFrame:
    pcm = samples.astype(np.int16)
    return rtc.AudioFrame(
        data=pcm.tobytes(),
        sample_rate=SAMPLE_RATE,
        num_channels=1,
        samples_per_channel=len(pcm),
    )


def _verdict(gate: NoiseGate, frame: rtc.AudioFrame) -> str:
    return gate.evaluate(measure(frame)).reason


# --- measure() ------------------------------------------------------------------------


def test_percentiles_recover_both_levels_through_the_padding():
    """The whole design rests on this: ambient and speech are both readable from one
    buffer, despite the buffer being mostly padding."""
    stats = measure(_segment(ambient_dbfs=-55.0, body_dbfs=-25.0, body_s=0.7))
    assert stats is not None
    assert stats.noise_dbfs == pytest.approx(-55.0, abs=3.0)
    assert stats.speech_dbfs == pytest.approx(-25.0, abs=3.0)
    assert stats.snr_db == pytest.approx(30.0, abs=5.0)


def test_voiced_ms_measures_the_body_not_the_buffer():
    """Buffer length cannot separate a click from a word -- both arrive ~0.8 s long
    because of the padding. voiced_ms is what actually distinguishes them."""
    word = measure(_segment(ambient_dbfs=-55.0, body_dbfs=-25.0, body_s=0.30))
    click = measure(_segment(ambient_dbfs=-55.0, body_dbfs=-10.0, body_s=0.04))

    # Both buffers are about the same length...
    assert word.duration_s == pytest.approx(click.duration_s, abs=0.3)
    # ...but only one of them contains a word's worth of energy.
    assert word.voiced_ms == pytest.approx(300, abs=60)
    assert click.voiced_ms < MIN_VOICED_MS


def test_measure_returns_none_for_an_unframeable_buffer():
    assert measure(_frame(np.zeros(100, dtype=np.int16))) is None


def test_measure_accepts_a_list_of_frames():
    samples = np.concatenate(
        [np.full(6000, 200, dtype=np.int16), np.full(6000, 6000, dtype=np.int16)]
    )
    one = measure(_frame(samples))
    split = measure([_frame(samples[:6000]), _frame(samples[6000:])])
    assert one.snr_db == pytest.approx(split.snr_db, abs=0.1)
    assert one.duration_s == pytest.approx(split.duration_s)


def test_measure_flags_clipping():
    stats = measure(_frame(np.full(SAMPLE_RATE // 2, 32767, dtype=np.int16)))
    assert stats.is_clipping
    assert stats.peak_dbfs == pytest.approx(0.0, abs=0.1)


def test_measure_survives_digital_silence():
    stats = measure(_frame(np.zeros(SAMPLE_RATE // 2, dtype=np.int16)))
    assert stats.noise_dbfs < -100.0
    assert stats.snr_db == pytest.approx(0.0, abs=0.1)
    assert not stats.is_clipping


# --- NoiseGate ------------------------------------------------------------------------


def test_speech_over_a_quiet_room_is_accepted():
    gate = NoiseGate()
    assert _verdict(gate, _segment(ambient_dbfs=-55.0, body_dbfs=-25.0, body_s=0.8)) == "speech"


def test_flat_room_tone_is_rejected():
    """Steady noise has no internal contrast, so nothing in it reads as a word."""
    gate = NoiseGate()
    flat = _segment(ambient_dbfs=-52.0, body_dbfs=-52.0, body_s=1.0)
    assert _verdict(gate, flat) == "no-speech"


def test_a_burst_just_above_the_room_is_rejected():
    """Loud enough to trip Silero, not loud enough to be someone talking to the phone."""
    gate = NoiseGate()
    murmur = _segment(ambient_dbfs=-50.0, body_dbfs=-44.0, body_s=0.8)
    assert _verdict(gate, murmur) == "low-snr"


def test_a_click_is_rejected_however_loud():
    gate = NoiseGate()
    click = _segment(ambient_dbfs=-55.0, body_dbfs=-8.0, body_s=0.04)
    assert _verdict(gate, click) == "no-speech"


def test_a_quiet_event_during_a_lull_is_rejected():
    """What the absolute test catches that the SNR test cannot: a segment with perfectly
    good internal contrast, but sitting far below the level this user actually speaks at.
    Happens when a loud room goes briefly quiet and something distant trips the VAD."""
    gate = NoiseGate()
    for i in range(20):  # settle the session floor into a loud room
        gate.evaluate(measure(_segment(ambient_dbfs=-30.0, body_dbfs=-20.0, body_s=0.5, seed=i)))

    lull = _segment(ambient_dbfs=-62.0, body_dbfs=-50.0, body_s=0.8)
    stats = measure(lull)
    assert stats.snr_db > SNR_MIN_DB  # would pass the contrast test on its own...
    assert _verdict(gate, lull) == "below-floor"  # ...but not the absolute one


def test_unframeable_segment_is_rejected():
    gate = NoiseGate()
    assert gate.evaluate(None).reason == "too-short"


def test_clipping_is_always_accepted():
    gate = NoiseGate()
    assert _verdict(gate, _frame(np.full(SAMPLE_RATE // 2, 32767, dtype=np.int16))) == "clipping"


def test_clipped_segments_do_not_move_the_floor():
    gate = NoiseGate()
    gate.evaluate(measure(_segment(ambient_dbfs=-55.0, body_dbfs=-25.0, body_s=0.5)))
    before = gate.floor_dbfs
    gate.evaluate(measure(_frame(np.full(SAMPLE_RATE // 2, 32767, dtype=np.int16))))
    assert gate.floor_dbfs == pytest.approx(before)


def test_long_utterances_bypass_both_contrast_tests():
    """In a long sentence the 10th percentile stops being pre-roll and becomes inter-word
    gaps, so both in-segment contrast measures collapse together. Rejecting on either would
    drop real speech, which is why the bypass has to cover both."""
    gate = NoiseGate()
    rng = np.random.default_rng(1)
    # Ten seconds of continuous speech-level energy: no internal contrast at all.
    long_speech = _frame(np.clip(_noise(10.0, -25.0, rng), -32768, 32767))
    stats = measure(long_speech)

    assert stats.snr_db < SNR_MIN_DB
    assert stats.voiced_ms < MIN_VOICED_MS
    assert gate.evaluate(stats).accepted  # ...accepted anyway, on length


def test_the_bypass_only_applies_to_genuinely_long_segments():
    """Guard against the bypass becoming a hole: the same flat audio, shorter, is rejected."""
    gate = NoiseGate()
    rng = np.random.default_rng(1)
    short_flat = _frame(np.clip(_noise(2.0, -25.0, rng), -32768, 32767))
    assert not gate.evaluate(measure(short_flat)).accepted


def test_speech_does_not_poison_the_floor():
    """The floor is fed from each segment's *own* pre-roll, so it tracks the room even
    when every segment is loud speech."""
    gate = NoiseGate()
    for i in range(40):
        gate.evaluate(measure(_segment(ambient_dbfs=-55.0, body_dbfs=-20.0, body_s=0.8, seed=i)))
    assert gate.floor_dbfs == pytest.approx(-55.0, abs=4.0)


def test_the_floor_seeds_from_the_first_segment_not_a_constant():
    gate = NoiseGate()
    assert gate.floor_dbfs == FLOOR_CLAMP[0]
    gate.evaluate(measure(_segment(ambient_dbfs=-40.0, body_dbfs=-20.0, body_s=0.5)))
    assert gate.floor_dbfs == pytest.approx(-40.0, abs=3.0)


def test_the_first_segment_is_never_rejected_on_absolute_level():
    """Failing open on the very first segment is the right direction: the user's opening
    words matter more than catching the first noise."""
    gate = NoiseGate()
    assert _verdict(gate, _segment(ambient_dbfs=-70.0, body_dbfs=-45.0, body_s=0.8)) == "speech"


def test_gates_carry_no_shared_state():
    """Two concurrent rooms on one worker must not see each other's noise floor."""
    loud, quiet = NoiseGate(), NoiseGate()
    for i in range(20):
        loud.evaluate(measure(_segment(ambient_dbfs=-30.0, body_dbfs=-20.0, body_s=0.5, seed=i)))
        quiet.evaluate(measure(_segment(ambient_dbfs=-60.0, body_dbfs=-25.0, body_s=0.5, seed=i)))
    assert loud.floor_dbfs > quiet.floor_dbfs + 20.0


# --- NoiseGatedSTT --------------------------------------------------------------------


class _StubSTT(stt.STT):
    """Stands in for openai.STT so the wrapper runs without a speech server."""

    def __init__(self) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.calls = 0

    @property
    def model(self) -> str:
        return "stub-model"

    @property
    def provider(self) -> str:
        return "stub-provider"

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options):
        self.calls += 1
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="en", text="hello there")],
        )


async def _recognize(gated: NoiseGatedSTT, frame: rtc.AudioFrame) -> str:
    event = await gated.recognize(buffer=frame, language="en")
    return event.alternatives[0].text if event.alternatives else ""


async def test_rejected_audio_never_reaches_the_wrapped_stt():
    """The gate must save the HTTP round trip, not merely discard the answer."""
    inner = _StubSTT()
    gated = NoiseGatedSTT(wrapped=inner)
    flat = _segment(ambient_dbfs=-52.0, body_dbfs=-52.0, body_s=1.0)

    assert await _recognize(gated, flat) == ""
    assert inner.calls == 0


async def test_accepted_audio_is_delegated():
    inner = _StubSTT()
    gated = NoiseGatedSTT(wrapped=inner)
    speech = _segment(ambient_dbfs=-55.0, body_dbfs=-25.0, body_s=0.8)

    assert await _recognize(gated, speech) == "hello there"
    assert inner.calls == 1


def test_wrapper_forwards_identity_and_capabilities():
    """AgentSession reads capabilities to decide on the StreamAdapter; metrics read model."""
    gated = NoiseGatedSTT(wrapped=_StubSTT())
    assert gated.capabilities.streaming is False
    assert gated.model == "stub-model"
    assert gated.provider == "stub-provider"


# --- DrugCorrectedSTT -----------------------------------------------------------------


class _MisheardSTT(_StubSTT):
    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options):
        self.calls += 1
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="en", text="I take met formin daily")],
        )


async def test_drug_names_are_corrected_on_the_final_transcript():
    inner = _MisheardSTT()
    corrected = DrugCorrectedSTT(wrapped=inner, lexicon=DrugLexicon(["metformin"]))
    speech = _segment(ambient_dbfs=-55.0, body_dbfs=-25.0, body_s=0.8)

    assert await _recognize(corrected, speech) == "I take metformin daily"
    assert inner.calls == 1


async def test_rejected_segments_stay_empty_through_the_corrector():
    inner = _MisheardSTT()
    corrected = DrugCorrectedSTT(wrapped=inner, lexicon=DrugLexicon(["metformin"]))
    flat = _segment(ambient_dbfs=-52.0, body_dbfs=-52.0, body_s=1.0)

    assert await _recognize(corrected, flat) == ""
    assert inner.calls == 0


# --- uncertainty read-back ------------------------------------------------------------
#
# The lexicon's weakest tier ("spine") matches on consonants alone, having thrown the vowels
# away. MEDICAL_CONFIRM turns those into a spoken read-back rather than a silent assumption;
# the plumbing below is what carries them from the STT to the agent.


class _SpineMisheardSTT(_StubSTT):
    """Returns "azampic" -- a vowel-mangled Ozempic that only the spine tier recovers."""

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options=None):
        self.calls += 1
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="en", text="I started azampic")],
        )


async def test_spine_matches_are_flagged_as_uncertain():
    uncertain: list[str] = []
    corrected = DrugCorrectedSTT(
        wrapped=_SpineMisheardSTT(), lexicon=DrugLexicon(["Ozempic"]), uncertain=uncertain
    )
    speech = _segment(ambient_dbfs=-55.0, body_dbfs=-25.0, body_s=0.8)

    assert await _recognize(corrected, speech) == "I started Ozempic"
    assert uncertain == ["Ozempic"]


async def test_confident_matches_are_not_flagged():
    # An exact fold match is not in doubt. Flagging it would make the read-back habit
    # meaningless, so only the spine tier may populate the list.
    uncertain: list[str] = []
    corrected = DrugCorrectedSTT(
        wrapped=_MisheardSTT(), lexicon=DrugLexicon(["metformin"]), uncertain=uncertain
    )
    speech = _segment(ambient_dbfs=-55.0, body_dbfs=-25.0, body_s=0.8)

    assert await _recognize(corrected, speech) == "I take metformin daily"
    assert uncertain == []


async def test_rejected_segment_flags_nothing():
    uncertain: list[str] = []
    corrected = DrugCorrectedSTT(
        wrapped=_SpineMisheardSTT(), lexicon=DrugLexicon(["Ozempic"]), uncertain=uncertain
    )
    flat = _segment(ambient_dbfs=-52.0, body_dbfs=-52.0, body_s=1.0)

    assert await _recognize(corrected, flat) == ""
    assert uncertain == []


async def test_confirmation_note_is_injected_for_uncertain_terms(monkeypatch):
    # The integration point: the list DrugCorrectedSTT fills is the list NadAssistant drains.
    # Unit-testing the two halves separately would not catch them being wired to different
    # lists, which is the way this actually breaks.
    monkeypatch.setattr(agent_mod, "MEDICAL_CONFIRM", True)
    uncertain = ["Ozempic"]
    assistant = NadAssistant(uncertain=uncertain)
    ctx = llm.ChatContext.empty()

    await assistant.on_user_turn_completed(
        ctx, llm.ChatMessage(role="user", content=["I started Ozempic last month"])
    )

    notes = [i for i in ctx.items if getattr(i, "role", None) == "system"]
    assert len(notes) == 1
    assert "Ozempic" in notes[0].text_content
    assert uncertain == [], "must drain, or the next turn re-asks about this one"


async def test_no_note_when_confirmation_is_off(monkeypatch):
    monkeypatch.setattr(agent_mod, "MEDICAL_CONFIRM", False)
    uncertain = ["Ozempic"]
    ctx = llm.ChatContext.empty()

    await NadAssistant(uncertain=uncertain).on_user_turn_completed(
        ctx, llm.ChatMessage(role="user", content=["I started Ozempic last month"])
    )

    assert not [i for i in ctx.items if getattr(i, "role", None) == "system"]
    assert uncertain == []


async def test_discarded_noise_turn_drops_its_uncertain_terms(monkeypatch):
    # The turn never reaches the LLM, so a confirmation about it would arrive attached to
    # whatever the user says next.
    monkeypatch.setattr(agent_mod, "MEDICAL_CONFIRM", True)
    uncertain = ["Ozempic"]

    with pytest.raises(llm.StopResponse):
        await NadAssistant(uncertain=uncertain).on_user_turn_completed(
            llm.ChatContext.empty(), llm.ChatMessage(role="user", content=["uh"])
        )

    assert uncertain == []


# --- transcript gate ------------------------------------------------------------------


@pytest.mark.parametrize(
    "transcript",
    [
        "", "   ", ".", "?", "...", "uh", "Um.", "  Hmm  ",
    ],
)
def test_noise_transcripts_are_rejected(transcript):
    assert _looks_like_noise(transcript)


@pytest.mark.parametrize(
    "transcript",
    [
        "yes",
        "No.",
        "stop",
        "Wait!",
        "okay",
        "hey",
        "thanks",
        "thank you",
        "what time is it",
        "um, what were we talking about",
        "hmm let me think",
        "you were saying",
        "how are you",
        # A transducer emits words only where the audio supports them, so a multi-word
        # transcript is speech by construction -- including the phrases a subtitle-trained
        # decoder would have invented out of silence. These are here to pin that: under
        # Whisper they were noise, under an RNNT they can only be someone talking.
        "thanks for watching",
        "please subscribe to my channel",
        "subtitles by the Amara.org community",
        # A VAD-clipped fragment of a real turn. Answering it beats discarding it: the
        # fault is the segmentation, not the speaker.
        "can you",
    ],
)
def test_real_speech_survives(transcript):
    assert not _looks_like_noise(transcript)


def test_allowlist_beats_the_noise_list():
    """Nothing in the allow-list may ever be dropped, whatever else the lists say."""
    from agent import _ALWAYS_ALLOWED

    for word in _ALWAYS_ALLOWED:
        assert not _looks_like_noise(word)
