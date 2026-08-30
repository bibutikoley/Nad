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

import os

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, TurnHandlingOptions, inference
from livekit.plugins import openai

load_dotenv()

SPEECH_BASE_URL = os.environ.get("SPEECH_BASE_URL", "http://localhost:8000/v1")
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "en")


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


class NadAssistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are Nad, a friendly real-time voice assistant. "
                "Answer briefly and conversationally. Plain spoken sentences only: "
                "no markdown, lists, emojis or symbols, since your words are read aloud."
            ),
        )


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: agents.JobContext) -> None:
    session = AgentSession(
        # Local Silero VAD (runs in-process via livekit-local-inference).
        # Drives speech segmentation for the non-streaming STT below, and is the
        # fast path for barge-in detection.
        vad=inference.VAD(
            model="silero",
            min_speech_duration=0.05,
            min_silence_duration=0.25,
            activation_threshold=0.5,
        ),
        # mlx-audio exposes an OpenAI-compatible /v1/audio/transcriptions.
        # The plugin treats non-realtime models as batch STT; the session wraps it
        # with a VAD StreamAdapter automatically.
        stt=openai.STT(
            model=ENV["STT_MODEL"],
            base_url=SPEECH_BASE_URL,
            api_key="local",
            use_realtime=False,
            language=STT_LANGUAGE,
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
            interruption={
                "enabled": True,
                "min_duration": 0.3,
                "resume_false_interruption": True,
                "false_interruption_timeout": 2.0,
            },
        ),
    )

    await session.start(room=ctx.room, agent=NadAssistant())
    await session.generate_reply(instructions="Greet the user in one short sentence.")

# No __main__ block: run this via `lk agent dev|start|console agent.py` (see
# README.md), which imports this module and discovers `server` directly rather
# than executing the file as a script. `lk` wraps `uv run` automatically when it
# detects a uv project, so it picks up this venv without extra setup.
