"""Reject room noise before it can become a turn.

The pipeline in `agent.py` uses a batch STT, so `AgentSession` wraps it in a
`stt.StreamAdapter`: Silero VAD cuts one WAV per speech segment and each one is POSTed to
mlx-audio. That makes the VAD the *sole* gatekeeper for what Parakeet ever sees, and Silero
fires on almost any energy transient. Parakeet is a transducer, so it dutifully emits text
for whatever it is handed -- and a non-empty transcript is all the end-of-turn model needs
to commit a turn. Net effect: a fan, a keyboard or a TV makes the agent talk to the room.

This module inserts a gate between the two. `NoiseGatedSTT` wraps the real STT, measures
each VAD segment, and returns an empty transcript for segments that look like ambient noise
rather than someone talking to the microphone. `StreamAdapter` drops empty results outright
(`stt/stream_adapter.py`: `elif not t_event.alternatives[0].text: continue`), so a rejected
segment costs nothing downstream -- no turn, no LLM call, and not even the HTTP round trip
to the speech server.

Rejecting *after* the segment exists is deliberate. The alternative -- raising Silero's
activation threshold -- would trade away responsiveness on short utterances, and this gate
adds no latency to the speech that does get through.

What this does NOT fix: the gate measures energy relative to the room, so it rejects *quiet*
noise. A loud TV or a second person talking at conversational volume is, by every energy
measure available here, indistinguishable from the user. Only speaker identification
(enrolling the user's voice) genuinely solves that, and that is a much larger project.
`_looks_like_noise` in `agent.py` catches part of the residue on the transcript side.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from livekit.agents import stt, utils
from livekit.agents.types import NOT_GIVEN, APIConnectOptions, NotGivenOr

if TYPE_CHECKING:
    from livekit.agents.voice.events import ConversationItemAddedEvent

logger = logging.getLogger("nad")

# Verbose per-segment logging for tuning the constants below against a real room.
# See README.md -> "Turn-taking & barge-in".
AUDIO_DEBUG = os.environ.get("NAD_AUDIO_DEBUG", "").lower() not in ("", "0", "false", "no")

# --- Gate constants -------------------------------------------------------------------
# Biased toward responsiveness: these reject clear ambient noise and little else. Raise
# SNR_MIN_DB first if noise still gets through, and watch for missed short answers.

FRAME_MS = 20.0
"""Analysis hop. Fine enough to resolve a single word, coarse enough to be stable."""

NOISE_PERCENTILE = 10
SPEECH_PERCENTILE = 90
"""The two modes of a segment's level distribution. A VAD segment is bimodal by
construction -- see `measure` -- so reading the two percentiles recovers "how loud is this
room" and "how loud is the thing that triggered the VAD" from the same buffer."""

VOICED_MARGIN_DB = 6.0
"""How far above the segment's own ambient a frame must sit to count as voiced."""

MIN_VOICED_MS = 120.0
"""Less voiced energy than this is a click, not a word. Measured on voiced frames rather
than on buffer length, which is meaningless here -- see `measure`."""

SNR_MIN_DB = 9.0
"""Required contrast between the loud part of a segment and its own ambient. Scale
invariant, so it survives whatever gain the capture chain applied."""

ABS_MARGIN_DB = 10.0
"""How far the loud part must also sit above the *session* floor. Catches the quiet-but-
structured case -- a distant TV has fine internal contrast yet sits well below the level
the user actually speaks at."""

MAX_SNR_EVAL_S = 8.0
"""Beyond this the SNR test is skipped. In a long utterance the 10th percentile stops being
the pre-roll and starts being inter-word gaps, which sit above ambient, so measured SNR
collapses and a real sentence would be rejected. Silero does not sustain a false positive
for eight seconds, so long segments are safe to wave through."""

CLIPPED_FRACTION = 0.02
"""Above this share of near-full-scale samples the level statistics are unreliable. Such a
segment is accepted and excluded from the floor estimate."""

FLOOR_ALPHA = 0.10
"""EMA weight for the session noise floor."""

FLOOR_CLAMP = (-85.0, -25.0)
"""Keeps one pathological segment from parking the floor somewhere it can never recover
from."""

_CLIPPED_SAMPLE = 32000
_INT16_FULL_SCALE = 32768.0
_SILENCE_DBFS = -120.0
"""Stand-in for log(0) on a digitally silent segment."""


@dataclass(frozen=True)
class SegmentStats:
    """Level statistics for one VAD-delimited audio segment."""

    duration_s: float
    voiced_ms: float
    noise_dbfs: float
    speech_dbfs: float
    peak_dbfs: float
    clipped_fraction: float

    @property
    def snr_db(self) -> float:
        return self.speech_dbfs - self.noise_dbfs

    @property
    def is_clipping(self) -> bool:
        return self.clipped_fraction > CLIPPED_FRACTION


@dataclass(frozen=True)
class GateVerdict:
    accepted: bool
    reason: str
    floor_dbfs: float
    snr_db: float


def _dbfs(amplitude: float) -> float:
    """Linear int16 amplitude -> dBFS, with a floor instead of -inf for digital silence."""
    if amplitude <= 0:
        return _SILENCE_DBFS
    return 20.0 * math.log10(amplitude / _INT16_FULL_SCALE)


def measure(buffer: utils.AudioBuffer) -> SegmentStats | None:
    """Frame-level statistics for one VAD segment, or None if it is too short to frame.

    Why percentiles rather than one RMS over the whole buffer: a VAD segment is *not* just
    the speech. `inference.VAD` keeps a rolling pre-roll and hands back
    `prefix_padding_duration` (0.5 s) of guaranteed sub-threshold ambient at the head, plus
    roughly `min_silence_duration` of silence at the tail (see `_reset_write_cursor` /
    `_copy_speech_buffer` in `livekit/agents/inference/vad.py`). Between 40% and 70% of a
    typical buffer is therefore padding.

    Two consequences drive this whole function:

    - Buffer *length* cannot tell a click from a word. A 200 ms "yes" and a 40 ms keyboard
      strike both arrive as ~0.8-0.95 s buffers. `voiced_ms` -- the time spent above this
      segment's own ambient -- can, and is immune to the padding.
    - A mean RMS is dominated by that padding and drifts with utterance length. The 10th and
      90th percentiles read the two modes directly instead.

    The pre-roll is also a free, per-segment, un-poisonable ambient sample: `noise_dbfs` is
    ambient even when the segment is loud speech, which is what makes the session floor
    below immune to being dragged up by the user talking.

    `AudioBuffer` is `rtc.AudioFrame | list[rtc.AudioFrame]`; `StreamAdapter` hands over a
    single already-merged frame, but the type allows both so normalise first.
    """
    frame = utils.merge_frames(buffer) if isinstance(buffer, list) else buffer
    samples = np.frombuffer(frame.data, dtype=np.int16)

    if frame.num_channels > 1:
        # Defensive: RoomIO delivers mono, but the buffer type does not promise it.
        samples = samples.reshape(-1, frame.num_channels).mean(axis=1)

    hop = max(1, int(FRAME_MS * frame.sample_rate / 1000))
    n_frames = samples.size // hop
    if n_frames < 5:
        # Too short to say anything statistically. The caller treats this as a reject.
        return None

    # float32 is plenty for per-frame RMS and keeps the reshape cheap; the int16 squares
    # would overflow, so the cast is not optional.
    framed = samples[: n_frames * hop].astype(np.float32).reshape(n_frames, hop)
    frame_rms = np.sqrt(np.mean(np.square(framed), axis=1))
    frame_dbfs = 20.0 * np.log10(np.maximum(frame_rms, 1e-9) / _INT16_FULL_SCALE)

    noise_dbfs = float(np.percentile(frame_dbfs, NOISE_PERCENTILE))
    magnitude = np.abs(samples.astype(np.int32))

    return SegmentStats(
        duration_s=samples.size / frame.sample_rate,
        voiced_ms=float(np.count_nonzero(frame_dbfs > noise_dbfs + VOICED_MARGIN_DB)) * FRAME_MS,
        noise_dbfs=noise_dbfs,
        speech_dbfs=float(np.percentile(frame_dbfs, SPEECH_PERCENTILE)),
        peak_dbfs=_dbfs(float(np.max(magnitude)) if magnitude.size else 0.0),
        clipped_fraction=float(np.mean(magnitude >= _CLIPPED_SAMPLE)) if magnitude.size else 0.0,
    )


class NoiseGate:
    """Accept/reject decision for one session's worth of VAD segments.

    Holds mutable per-room state (the session floor), so one instance belongs to one job --
    see the construction site in `agent.py`.
    """

    def __init__(
        self,
        *,
        snr_min_db: float = SNR_MIN_DB,
        abs_margin_db: float = ABS_MARGIN_DB,
        min_voiced_ms: float = MIN_VOICED_MS,
        floor_alpha: float = FLOOR_ALPHA,
    ) -> None:
        self._snr_min_db = snr_min_db
        self._abs_margin_db = abs_margin_db
        self._min_voiced_ms = min_voiced_ms
        self._floor_alpha = floor_alpha
        self._floor_dbfs: float | None = None

    @property
    def floor_dbfs(self) -> float:
        """The tracked ambient level. Reads as the bottom of the clamp before any audio,
        which makes the absolute test inert for exactly one segment -- deliberate: letting
        the first thing through is the right direction to fail."""
        return FLOOR_CLAMP[0] if self._floor_dbfs is None else self._floor_dbfs

    def _update_floor(self, stats: SegmentStats) -> None:
        """Fold one segment's ambient estimate into the session floor.

        Seeded directly from the first segment rather than from a constant, so there is no
        warm-up window in which the absolute test misbehaves.
        """
        if self._floor_dbfs is None:
            nxt = stats.noise_dbfs
        else:
            nxt = self._floor_dbfs + self._floor_alpha * (stats.noise_dbfs - self._floor_dbfs)
        self._floor_dbfs = float(np.clip(nxt, *FLOOR_CLAMP))

    def evaluate(self, stats: SegmentStats | None) -> GateVerdict:
        # Read before _update_floor below: a segment is judged against the room as it was,
        # not against a floor it has just moved itself.
        floor = self.floor_dbfs

        if stats is None:
            return GateVerdict(False, "too-short", floor_dbfs=floor, snr_db=0.0)

        def verdict(accepted: bool, reason: str) -> GateVerdict:
            return GateVerdict(accepted, reason, floor_dbfs=floor, snr_db=stats.snr_db)

        if stats.is_clipping:
            # Percentiles are meaningless once the capture chain ran out of headroom, and a
            # clipped segment tells us nothing about the ambient level either. Wave it
            # through and keep it out of the floor: non-speech transcribes to "" anyway.
            return verdict(True, "clipping")

        self._update_floor(stats)

        # Both tests below read the same thing -- contrast *within* the segment -- so they
        # fail together, and they fail on exactly one kind of real audio: a long utterance,
        # where the 10th percentile stops being the pre-roll and becomes inter-word gaps
        # that sit well above ambient. Skip both rather than just the SNR one, or a long
        # sentence gets rejected as "no-speech" instead of "low-snr" and the bypass buys
        # nothing. Silero does not sustain a false positive for this long, so the audio is
        # almost certainly real; the absolute test below still applies.
        if stats.duration_s <= MAX_SNR_EVAL_S:
            if stats.voiced_ms < self._min_voiced_ms:
                # Either a brief burst (a click) or a flat one with no speech structure at
                # all (steady room tone). Both are "nothing was said here".
                return verdict(False, "no-speech")

            if stats.snr_db < self._snr_min_db:
                return verdict(False, "low-snr")

        if stats.speech_dbfs < floor + self._abs_margin_db:
            return verdict(False, "below-floor")

        return verdict(True, "speech")


class NoiseGatedSTT(stt.STT):
    """Wraps an STT and swallows segments the gate rejects.

    Returns an empty `FINAL_TRANSCRIPT` rather than raising, because `StreamAdapter` already
    treats an empty transcript as "nothing was said" and skips it silently.
    """

    def __init__(self, *, wrapped: stt.STT, gate: NoiseGate | None = None) -> None:
        # Mirror the wrapped capabilities so AgentSession still sees a batch STT and applies
        # the StreamAdapter it would have applied anyway.
        super().__init__(capabilities=wrapped.capabilities)
        self._wrapped = wrapped
        self._gate = gate or NoiseGate()
        self._segment = 0

    @property
    def wrapped_stt(self) -> stt.STT:
        return self._wrapped

    # Forwarded so metrics stay labelled with the real model rather than this shim.
    @property
    def model(self) -> str:
        return self._wrapped.model

    @property
    def provider(self) -> str:
        return self._wrapped.provider

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        self._segment += 1
        stats = measure(buffer)
        verdict = self._gate.evaluate(stats)

        if not verdict.accepted:
            self._log(verdict, stats, transcript="")
            # Empty alternatives: StreamAdapter skips this without emitting anything at all,
            # so no transcript, no end-of-turn inference, no preemptive generation.
            return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[])

        event = await self._wrapped.recognize(
            buffer=buffer, language=language, conn_options=conn_options
        )
        self._log(
            verdict, stats, transcript=event.alternatives[0].text if event.alternatives else ""
        )
        return event

    def _log(self, verdict: GateVerdict, stats: SegmentStats | None, *, transcript: str) -> None:
        """One line per VAD segment. Pair it with the accepted-transcript logging in
        `agent.py` to see the whole path, and tune the constants above against real numbers
        rather than guesses -- see README.md."""
        if not AUDIO_DEBUG:
            return
        extra: dict[str, object] = {
            "segment": self._segment,
            "verdict": verdict.reason,
            "floor_dbfs": round(verdict.floor_dbfs, 1),
            "lk.pii.transcript": transcript,
        }
        if stats is not None:
            extra |= {
                "duration_s": round(stats.duration_s, 2),
                "voiced_ms": round(stats.voiced_ms),
                "noise_dbfs": round(stats.noise_dbfs, 1),
                "speech_dbfs": round(stats.speech_dbfs, 1),
                "peak_dbfs": round(stats.peak_dbfs, 1),
                "snr_db": round(stats.snr_db, 1),
                "clipped": round(stats.clipped_fraction, 4),
            }
        logger.info("vad segment %s", "accepted" if verdict.accepted else "rejected", extra=extra)

    # --- Pass-throughs. StreamAdapter calls these on us; they belong to the real STT. ---

    def prewarm(self) -> None:
        self._wrapped.prewarm()

    def _update_session_keyterms(self, keyterms: list[str]) -> None:
        self._wrapped._update_session_keyterms(keyterms)

    def _push_conversation_item(self, ev: ConversationItemAddedEvent) -> None:
        self._wrapped._push_conversation_item(ev)

    async def aclose(self) -> None:
        await self._wrapped.aclose()
