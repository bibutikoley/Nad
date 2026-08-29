"""Nad voice agent worker.

Pipeline: LiveKit room audio -> VAD -> STT (mlx-audio) -> LLM (OpenAI-compatible)
          -> TTS (mlx-audio) -> back into the room.

Run:  uv run agent.py dev      (local dev, auto-reload)
      uv run agent.py start    (production)
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
    env = _require_env("STT_MODEL", "TTS_MODEL", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY")

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
            model=env["STT_MODEL"],
            base_url=SPEECH_BASE_URL,
            api_key="local",
            use_realtime=False,
            language=STT_LANGUAGE,
        ),
        llm=openai.LLM(
            model=env["LLM_MODEL"],
            base_url=env["LLM_BASE_URL"],
            api_key=env["LLM_API_KEY"],
        ),
        # mlx-audio exposes an OpenAI-compatible /v1/audio/speech.
        # WAV avoids the ffmpeg dependency mlx-audio needs for mp3/opus.
        tts=openai.TTS(
            model=env["TTS_MODEL"],
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


if __name__ == "__main__":
    # agents.cli.run_app() is deprecated in favor of `python -m livekit.agents` /
    # the `lk` CLI, but its replacement's "console" mode talks to the Go `lk` CLI's
    # TCP dev channel rather than the terminal mic/speaker directly, which the `lk`
    # CLI isn't part of this project. Keeping run_app() (still functional, just
    # warns) preserves the `uv run agent.py dev|start|console` workflow in the
    # README without adding that dependency.
    agents.cli.run_app(server)
