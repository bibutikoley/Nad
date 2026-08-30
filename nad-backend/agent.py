"""Nad voice agent worker.

Pipeline: LiveKit room audio -> VAD -> STT (stt_server.py) -> LLM (OpenAI-compatible)
          -> TTS (mlx-audio) -> back into the room.

Run:  lk agent dev agent.py       (local dev, auto-reload)
      lk agent start agent.py     (production)
      lk agent console agent.py   (talk to it from your terminal mic/speaker)

`lk` (the LiveKit CLI, `brew install livekit-cli`) needs LIVEKIT_URL / LIVEKIT_API_KEY /
LIVEKIT_API_SECRET as real shell env vars, not just in .env — it reads them itself before
it ever gets to importing this module, so `load_dotenv()` below is too late for `lk`'s own
use of them (scripts/dev.sh sources .env for this reason; export them yourself if running
one of the commands above directly).
"""

import logging
import os
import re

import aiohttp
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    TurnHandlingOptions,
    UserInputTranscribedEvent,
    inference,
    llm,
)
from livekit.agents.types import ATTRIBUTE_AGENT_STATE
from livekit.agents.voice import room_io
from livekit.plugins import openai

from noise_gate import AUDIO_DEBUG, NoiseGate, NoiseGatedSTT

load_dotenv()

logger = logging.getLogger("nad")

# Two speech servers, one URL each: STT on stt_server.py (:8001), TTS on mlx-audio
# (:8000) -- see README.md -> "Processes". A single SPEECH_BASE_URL covered both while
# one mlx-audio process served both endpoints; it is gone rather than kept as a third
# way to say the same thing, now that the STT model has outgrown that server.
# `or` rather than a default argument: an empty `STT_BASE_URL=` in .env is an easy typo,
# and falling back to the default beats building an unusable base URL out of it.
STT_BASE_URL = os.environ.get("STT_BASE_URL") or "http://localhost:8001/v1"
TTS_BASE_URL = os.environ.get("TTS_BASE_URL") or "http://localhost:8000/v1"
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "en")

# Where to collect a transcript the client parked for a resumed conversation. The token
# server runs in Docker with 8787 published, so localhost works from this host process.
TOKEN_SERVER_URL = os.environ.get("TOKEN_SERVER_URL", "http://localhost:8787").rstrip("/")
TOKEN_SERVER_AUTH_TOKEN = os.environ.get("TOKEN_SERVER_AUTH_TOKEN", "")


def _require_env(*names: str) -> dict[str, str]:
    """Fetch required env vars, failing fast with all missing names at once
    instead of a bare KeyError on whichever one happens to be read first."""
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )
    return values


# Checked at import time (not inside entrypoint, which only runs per job) so a
# misconfigured worker fails `lk agent dev agent.py` immediately instead of only
# blowing up when the first user joins.
ENV = _require_env("STT_MODEL", "TTS_MODEL", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY")


# Second line of defence behind noise_gate.py, which rejects on signal level alone. This one
# reads the transcript, and so catches noise that was loud enough to pass but decoded to
# nothing meaningful.
#
# This list is transducer-specific, and would be the wrong list for Whisper. The current
# default is an RNNT: it emits a token only when the audio supports one, so on noise it
# returns an empty string (measured: silence, hiss and a desk thump all transcribe to ""),
# and StreamAdapter drops those before they ever reach a turn. What it *does* produce on
# marginal audio is truncated real speech -- a VAD-clipped "Can you" -- never invented
# content.
#
# Unchanged across the swap from Nemotron 3.5 ASR to omi-med-stt-v1, and worth saying so
# rather than leaving it to read as an oversight: omi is a fine-tune of
# nvidia/parakeet-tdt-0.6b-v2, a FastConformer TDT/RNNT from the same family, and its medical
# adapter is a residual block inserted *inside* each encoder block. It changes what the
# encoder represents, not how the decoder emits, so the emit-only-when-supported property
# this list rests on is intact.
#
# One thing is new, though: that empty transcript now survives a hop that natively destroys
# it. `omi_stt.mlx_runtime.transcribe_mlx` *raises* on an empty result, and stt_server.py
# catches exactly that and returns {"text": ""}. Remove that catch and every noise segment
# becomes an HTTP 500 and a retry -- the gate design inverts.
#
# Whisper fails the opposite way and needs a different list entirely. Its decoder is
# autoregressive and trained on subtitle data, so near-silence makes it invent a fluent
# sentence drawn from the boilerplate padding those corpora: "Thank you.", "Thanks for
# watching", "Subtitles by the Amara.org community". That list lived here until the model
# switched; `git log -S "amara" -- agent.py` restores it if STT_MODEL goes back to Whisper.
# README.md -> "Swapping models" says which family is which.
_NOISE_TOKENS = frozenset({"uh", "um", "mm", "hmm", "mhm", "hm", "ah", "eh", "huh"})

# Checked first, so a real one-word reply can never be caught by the list above. Short
# answers are exactly what an over-eager filter breaks, and they matter in a voice UI.
_ALWAYS_ALLOWED = frozenset(
    {
        "yes", "no", "yeah", "nope", "ok", "okay", "sure", "stop", "wait",
        "hi", "hey", "hello", "thanks", "next", "back", "repeat", "again",
    }
)

_WORD_RE = re.compile(r"[^\w']+")


def _looks_like_noise(transcript: str) -> bool:
    """True when a transcript is an artifact rather than something said to the agent."""
    words = [word for word in _WORD_RE.split(transcript.lower()) if word]

    if not words:
        # Empty, or punctuation only. StreamAdapter already drops genuinely empty
        # transcripts, so this catches the "." / "?" case.
        return True

    if len(words) > 1:
        # A transducer only emits words the audio supports, so anything it strung together
        # is something that was actually said. There is no multi-word artifact to match
        # against -- worst case it is a clipped fragment of a real turn, which is a VAD
        # problem, not a noise problem, and answering it beats discarding it.
        return False

    # Checked first, so a real one-word reply can never be caught by the list below.
    return words[0] not in _ALWAYS_ALLOWED and words[0] in _NOISE_TOKENS


class NadAssistant(Agent):
    def __init__(self, chat_ctx: llm.ChatContext | None = None) -> None:
        super().__init__(
            instructions=(
                "You are Nad, a friendly real-time voice assistant. "
                "Answer briefly and conversationally. Plain spoken sentences only: "
                "no markdown, lists, emojis or symbols, since your words are read aloud."
            ),
            # Populated when the user resumed an earlier conversation, so the agent
            # genuinely remembers it rather than just being shown next to its transcript.
            chat_ctx=chat_ctx,
        )

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        """Drop turns that are room noise rather than speech aimed at the agent.

        A backstop, not the primary defence -- that is noise_gate.py, which rejects before
        a transcript exists at all. By the time this runs, preemptive generation (on by
        default) has already spent one LLM call on the turn, so a rejection here wastes
        work that a rejection in the gate does not. Nothing is spoken, because
        preemptive_tts is off by default. If this starts firing often, tighten the gate
        rather than widening the lists below.

        StopResponse is the framework's sanctioned discard: livekit-agents catches it and
        returns without generating a reply *and* without appending the message to chat_ctx,
        so a rejected turn never pollutes the conversation history either.

        Deliberately not gated on `new_message.transcript_confidence`: the openai STT plugin
        never sets SpeechData.confidence, so it is always its 0.0 default and any threshold
        on it would reject every turn.
        """
        transcript = new_message.text_content or ""
        if _looks_like_noise(transcript):
            logger.info("discarded noise turn: %r", transcript)
            raise llm.StopResponse()


server = AgentServer()

# Long enough to cover a first-ever HuggingFace download of Kokoro and the STT model,
# which is the worst case this warm-up exists to absorb (~940 MB for omi-med-stt q8, and
# nearly 3 GB if STT_MODEL is pointed back at Whisper large-v3).
_WARMUP_TIMEOUT = aiohttp.ClientTimeout(total=600)


async def _warm_speech_models() -> None:
    """Load both speech models before we claim to be listening.

    Each server loads its model on the first real request, so without this the first user
    turn pays the download+load cost -- and the client has already been told the agent is
    listening. The plugins' own prewarm() hooks don't help: openai.TTS.prewarm is a bare
    `GET /` and the base STT.prewarm is a no-op, neither of which touches model weights. So
    do a real synthesis and feed its audio straight back into a real transcription.

    Crosses two servers now (TTS on mlx-audio, STT on stt_server.py) and needs no special
    handling for it, since the WAV already round-trips through this process between the two
    calls. That makes it a better check than it was: it now proves both endpoints answer.
    stt_server.py also preloads itself at startup, so in practice this finds the STT side
    already warm -- it stays for the TTS half and for the end-to-end signal.

    Best-effort: a failure here just restores the previous behaviour (slow first turn),
    so it must never fail the job.
    """
    try:
        async with aiohttp.ClientSession(timeout=_WARMUP_TIMEOUT) as http:
            async with http.post(
                f"{TTS_BASE_URL}/audio/speech",
                json={
                    "model": ENV["TTS_MODEL"],
                    "voice": os.environ.get("TTS_VOICE", "af_heart"),
                    "input": "Ready.",
                    "response_format": "wav",
                },
            ) as response:
                response.raise_for_status()
                wav = await response.read()

        # Kokoro can fail *inside* a streamed 200 and just drop the connection, which
        # surfaces as a short/empty body rather than an HTTP error -- see README.
        if len(wav) < 1024:
            raise RuntimeError(f"TTS returned {len(wav)} bytes; expected real audio")

        form = aiohttp.FormData()
        form.add_field("model", ENV["STT_MODEL"])
        form.add_field("response_format", "json")
        form.add_field("file", wav, filename="warmup.wav", content_type="audio/wav")

        async with aiohttp.ClientSession(timeout=_WARMUP_TIMEOUT) as http:
            async with http.post(
                f"{STT_BASE_URL}/audio/transcriptions", data=form
            ) as response:
                response.raise_for_status()
                transcription = await response.json()

        # Cheap early warning that the STT is serving something other than what we think:
        # this audio is synthesised speech, so an empty transcript here means the model
        # loaded but is not hearing words. Not fatal -- the whole function is best-effort.
        if not (transcription.get("text") or "").strip():
            logger.warning("STT returned nothing for synthesised speech; check STT_MODEL")

        logger.info("speech models warm")
    except Exception:
        logger.warning(
            "speech model warm-up failed; the first turn will be slow", exc_info=True
        )


async def _fetch_parked_history(room: str) -> list[dict[str, str]]:
    """Collect a transcript the client parked on the token server for this room.

    Returns an empty list for a fresh conversation (the token server 404s), and on any
    failure: losing prior context degrades to a normal new conversation, which is far
    better than failing the job.
    """
    if not TOKEN_SERVER_AUTH_TOKEN:
        return []
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"Authorization": f"Bearer {TOKEN_SERVER_AUTH_TOKEN}"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as http:
            async with http.get(f"{TOKEN_SERVER_URL}/history/{room}") as response:
                if response.status == 404:
                    return []
                response.raise_for_status()
                payload = await response.json()
        messages = payload.get("messages", [])
        return messages if isinstance(messages, list) else []
    except Exception:
        logger.warning("could not fetch parked history; starting fresh", exc_info=True)
        return []


def _build_chat_ctx(history: list[dict[str, str]]) -> llm.ChatContext | None:
    if not history:
        return None
    chat_ctx = llm.ChatContext.empty()
    for message in history:
        role = message.get("role")
        text = (message.get("text") or "").strip()
        if role in ("user", "assistant") and text:
            chat_ctx.add_message(role=role, content=text)
    return chat_ctx if chat_ctx.items else None


@server.rtc_session()
async def entrypoint(ctx: agents.JobContext) -> None:
    # Join the room before building the session, so the client has a participant to
    # observe for the whole cold start instead of sitting in an empty room.
    # session.start() connects too, but connect() is idempotent (it early-returns on
    # self._connected), so calling it here first is safe.
    await ctx.connect()

    # livekit-agents never actually publishes "initializing": AgentSession's constructor
    # presets that value, so the _update_agent_state("initializing") inside start() hits
    # the unchanged-value guard and writes no attribute. The first thing a client would
    # otherwise see is "listening" -- published before any TTS has run, i.e. while the
    # models below are still cold. Publish it ourselves so "listening" stays honest and
    # the client can show a real loading state.
    await ctx.room.local_participant.set_attributes(
        {ATTRIBUTE_AGENT_STATE: "initializing"}
    )

    # Runs inside the initializing window, alongside the model warm-up below.
    history = await _fetch_parked_history(ctx.room.name)
    chat_ctx = _build_chat_ctx(history)
    if chat_ctx is not None:
        logger.info("resuming conversation with %d prior messages", len(chat_ctx.items))

    await _warm_speech_models()

    # Built after the attribute is set: constructing the VAD and turn detector below is
    # what triggers their lazy native model loads (~108 MB for the EOT model), and that
    # time belongs inside the initializing window too.
    session = AgentSession(
        # Local Silero VAD (runs in-process via livekit-local-inference).
        # Drives speech segmentation for the non-streaming STT below, and is the
        # fast path for barge-in detection.
        vad=inference.VAD(
            model="silero",
            # A Silero window is 32 ms, so the 0.05 default latches on two consecutive
            # windows -- a key press, a cup on a desk. The shortest utterance worth
            # keeping ("yes") carries 250-350 ms of continuous energy, so 0.12 has
            # roughly 2x headroom while still rejecting transients.
            min_speech_duration=0.12,
            min_silence_duration=0.25,
            # Nudged up from the 0.5 default, but deliberately not to 0.7+: Silero scores
            # TV dialogue and second-party speech about as highly as the user, so a high
            # threshold buys nothing there while it does start dropping soft onsets. The
            # real rejection happens in noise_gate.py, which costs no latency.
            activation_threshold=0.55,
            # Pinned rather than left to derive (activation - 0.15 = 0.40). The wider
            # hysteresis stops the raised activation threshold from chopping an utterance
            # at a mid-word energy dip.
            deactivation_threshold=0.35,
            # Left at the default, but pinned because it is load-bearing twice over: it is
            # the only thing protecting word onsets from being clipped off the WAV the
            # StreamAdapter cuts, *and* noise_gate.py estimates the ambient level from
            # exactly this pre-roll. Shrinking it silently degrades the gate.
            prefix_padding_duration=0.5,
        ),
        # STT_BASE_URL exposes an OpenAI-compatible /v1/audio/transcriptions -- by default
        # stt_server.py on :8001, or mlx-audio on :8000 for any model it can route.
        # The plugin treats non-realtime models as batch STT; the session wraps it
        # with a VAD StreamAdapter automatically.
        #
        # Wrapped in the noise gate so ambient room noise never reaches the model: Silero
        # fires on any energy transient, and every such segment otherwise costs a real HTTP
        # round trip to the speech server. The transducer does return a blank for most of
        # them -- unlike Whisper, which hallucinates a sentence -- so the gate is now about
        # latency and wasted work more than about false turns, but it is load-bearing either
        # way. It holds per-room state, so it is built here (per job) and never shared.
        stt=NoiseGatedSTT(
            wrapped=openai.STT(
                model=ENV["STT_MODEL"],
                base_url=STT_BASE_URL,
                api_key="local",
                use_realtime=False,
                # Inert for the current default: omi-med-stt-v1 is English-only and has no
                # language conditioning of any kind, and stt_server.py ignores the field.
                # Still sent, and still worth keeping: it is load-bearing the moment STT_MODEL
                # goes back to a multilingual model on mlx-audio (Nemotron conditions its
                # encoder on a learned per-language prompt vector, Whisper takes it as a
                # decoding hint), and it is what the plugin tags the SpeechEvent's language
                # with -- correct at "en" either way.
                language=STT_LANGUAGE,
                # `temperature` is deliberately absent. It was here for Whisper, whose
                # near-silence hallucinations come from sampling; an RNNT decodes greedily
                # by construction, so there is nothing for it to mean. It was a no-op
                # regardless -- neither mlx-audio's /v1/audio/transcriptions nor stt_server.py
                # declares such a field, and both drop unrecognised form fields silently.
            ),
            gate=NoiseGate(),
        ),
        llm=openai.LLM(
            model=ENV["LLM_MODEL"],
            base_url=ENV["LLM_BASE_URL"],
            api_key=ENV["LLM_API_KEY"],
        ),
        # mlx-audio exposes an OpenAI-compatible /v1/audio/speech.
        # WAV avoids the ffmpeg dependency mlx-audio needs for mp3/opus.
        tts=openai.TTS(
            model=ENV["TTS_MODEL"],
            voice=os.environ.get("TTS_VOICE", "af_heart"),
            base_url=TTS_BASE_URL,
            api_key="local",
            response_format="wav",
        ),
        turn_handling=TurnHandlingOptions(
            # Local end-of-turn model (v1-mini runs on-device; the default "v1"
            # would call LiveKit Cloud inference, which we don't use).
            turn_detection=inference.TurnDetector(version="v1-mini"),
            # How long to wait after the user stops before the agent replies.
            endpointing={"min_delay": 0.3, "max_delay": 2.0},
            # Barge-in: the user can talk over the agent; the agent stops within
            # min_duration of detected speech. If the "interruption" turns out to be
            # a cough / backchannel (no transcript within the timeout), the agent
            # resumes where it left off.
            #
            # min_words is deliberately left at its 0 default. Raising it would make the
            # agent wait for a batch-STT round trip before it stops talking -- a visible
            # latency regression, and a *correctness* one here: every extra millisecond the
            # agent keeps talking is another millisecond of its own voice echoing into the
            # segment the STT is about to transcribe. resume_false_interruption below
            # already undoes a barge-in caused by noise, which gets the same outcome free.
            #
            # min_duration is the length of that overlap, so it is the one knob that
            # directly bounds double-talk contamination: the agent keeps speaking for at
            # least this long after the user starts, and the mic hears both. Lowered from
            # 0.3 for that reason. It cannot go to zero -- a single transient would stop
            # the agent mid-word -- and it does not fix the problem on its own, because the
            # VAD's 0.5 s pre-roll captures agent audio from *before* the user's onset
            # regardless. See README.md -> "Background noise" on talking over the agent.
            interruption={
                "enabled": True,
                "min_duration": 0.2,
                "resume_false_interruption": True,
                "false_interruption_timeout": 2.0,
            },
        ),
    )

    if AUDIO_DEBUG:
        # Pairs with the per-segment logging in noise_gate.py: that shows every segment the
        # gate saw, this shows the ones that made it all the way to a transcript. Together
        # they are what the gate constants should be tuned against -- see README.md.
        @session.on("user_input_transcribed")
        def _log_transcript(ev: UserInputTranscribedEvent) -> None:
            if ev.is_final:
                logger.info("transcript accepted: %r", ev.transcript)

    # Publishes lk.agent.state = "listening", which now genuinely means ready.
    await session.start(
        room=ctx.room,
        agent=NadAssistant(chat_ctx=chat_ctx),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                # Off, against the library default. The iPhone already runs Apple's
                # Voice-Processing I/O (echo cancellation, noise suppression and AGC) before
                # the audio ever leaves the device; livekit-agents then applies a *second*
                # WebRTC AGC here, ahead of the VAD. Two cascaded gain controllers
                # re-normalise an already-normalised signal, which lifts room noise toward
                # speech level during the pauses -- exactly what makes Silero fire on a fan.
                #
                # Note this has no effect under `lk agent console`, which builds no RoomIO.
                auto_gain_control=False,
            ),
        ),
    )

    if chat_ctx is not None:
        await session.generate_reply(
            instructions=(
                "You are picking up an earlier conversation with this user. Welcome them "
                "back in one short sentence and invite them to continue. Do not summarise "
                "or repeat what was already said unless they ask."
            )
        )
    else:
        await session.generate_reply(instructions="Greet the user in one short sentence.")

# No __main__ block: run this via `lk agent dev|start|console agent.py` (see
# README.md), which imports this module and discovers `server` directly rather
# than executing the file as a script. `lk` wraps `uv run` automatically when it
# detects a uv project, so it picks up this venv without extra setup.
