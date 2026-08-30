"""Nad voice agent worker.

Pipeline: LiveKit room audio -> VAD -> STT (mlx-audio) -> LLM (OpenAI-compatible)
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

SPEECH_BASE_URL = os.environ.get("SPEECH_BASE_URL", "http://localhost:8000/v1")
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
# These lists are Whisper-specific, and would be the wrong lists for a transducer. Whisper's
# decoder is autoregressive and was trained on subtitle data, so on near-silence it does not
# emit a blank -- it invents a *fluent sentence*, drawn from the boilerplate that pads
# subtitle corpora ("Thank you.", "Subtitles by ...", "Please subscribe"). A transducer like
# Parakeet fails the opposite way, emitting short spurious fragments. Revisit both lists if
# STT_MODEL changes family; README.md -> "Swapping models" says which is which.
_NOISE_TOKENS = frozenset(
    {
        "uh", "um", "mm", "hmm", "mhm", "hm", "ah", "eh", "huh",
        # Whisper's single most common output on silence.
        "you",
    }
)

# Matched against the whole normalised transcript, never as a substring: a turn that merely
# contains one of these is a real turn ("thanks for watching the demo, what's next" must
# survive), whereas a turn that is *only* this is subtitle boilerplate.
#
# Bare "thanks" / "thank you" are deliberately absent. They are genuinely ambiguous -- both
# a hallucination and an ordinary thing to say to a voice assistant -- and dropping a real
# one is worse than passing a spurious one, which costs a single wasted reply.
_NOISE_PHRASES = frozenset(
    {
        "thanks for watching",
        "thank you for watching",
        "thanks for watching this video",
        "please subscribe",
        "please subscribe to my channel",
        "like and subscribe",
        "subtitles by the amara org community",
        "subtitles by the amaraorg community",
        "amara org",
        "subs by",
        "transcription by castingwords",
        "bye bye",
    }
)

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
        # Multi-word turns are rejected only when the *entire* transcript is known
        # subtitle boilerplate. Whisper hallucinates fluent sentences, so unlike a
        # transducer the word count alone says nothing about whether anything was said.
        #
        # The allow-list is not consulted here: it protects one-word answers, and
        # applying it to the first word of a longer turn would rescue every phrase that
        # merely opens with one ("thanks for watching").
        return " ".join(words) in _NOISE_PHRASES

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
# which is the worst case this warm-up exists to absorb (~2.9 GB for Whisper large-v3).
_WARMUP_TIMEOUT = aiohttp.ClientTimeout(total=600)


async def _warm_speech_models() -> None:
    """Force mlx-audio to load both speech models before we claim to be listening.

    mlx-audio loads each model on its first real request, so without this the first
    user turn pays the download+load cost -- and the client has already been told the
    agent is listening. The plugins' own prewarm() hooks don't help: openai.TTS.prewarm
    is a bare `GET /` and the base STT.prewarm is a no-op, neither of which touches
    model weights. So do a real synthesis and feed its audio straight back into a real
    transcription.

    Best-effort: a failure here just restores the previous behaviour (slow first turn),
    so it must never fail the job.
    """
    try:
        async with aiohttp.ClientSession(timeout=_WARMUP_TIMEOUT) as http:
            async with http.post(
                f"{SPEECH_BASE_URL}/audio/speech",
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
                f"{SPEECH_BASE_URL}/audio/transcriptions", data=form
            ) as response:
                response.raise_for_status()
                await response.read()

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
        # mlx-audio exposes an OpenAI-compatible /v1/audio/transcriptions.
        # The plugin treats non-realtime models as batch STT; the session wraps it
        # with a VAD StreamAdapter automatically.
        #
        # Wrapped in the noise gate so ambient room noise never reaches the model: Silero
        # fires on any energy transient, and Whisper does not return a blank for one -- it
        # hallucinates a sentence, which is worse than the spurious fragment a transducer
        # would emit. The gate holds per-room state, so it is built here (per job) and
        # never shared.
        stt=NoiseGatedSTT(
            wrapped=openai.STT(
                model=ENV["STT_MODEL"],
                base_url=SPEECH_BASE_URL,
                api_key="local",
                use_realtime=False,
                language=STT_LANGUAGE,
                # Greedy decoding: Whisper's hallucinations on near-silence come from
                # sampling, so 0 makes it less inclined to invent a sentence.
                #
                # Currently a no-op, and measured rather than assumed: mlx-audio's
                # /v1/audio/transcriptions declares no `temperature` (its handler takes
                # model/language/chunk_duration/context/text/...) and FastAPI silently
                # drops unrecognised form fields. Kept because it costs nothing and is
                # correct against any server that does implement the OpenAI shape.
                temperature=0.0,
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
            base_url=SPEECH_BASE_URL,
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
            # latency regression -- and resume_false_interruption below already undoes a
            # barge-in caused by noise, which gets the same outcome for free.
            interruption={
                "enabled": True,
                "min_duration": 0.3,
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
