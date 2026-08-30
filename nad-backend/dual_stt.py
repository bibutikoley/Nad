"""Two STTs, one stream: live interim text from a streaming model, the final from a batch one.

Why two: the medical model (omi-med-stt) is batch-only, so on its own the client sees nothing
while the user talks and then a whole sentence at once. A streaming model can show words as
they are spoken but is a general model that mangles drug names. So the streaming one feeds
INTERIM_TRANSCRIPT events (display only -- AgentSession never puts them in the chat context
unless a turn is committed manually) and the batch one, wrapped in the same VAD StreamAdapter
AgentSession would have used, feeds START/END_OF_SPEECH and FINAL_TRANSCRIPT, which is what
the LLM and the saved transcript get.

Audio goes to both. Events are merged with one rule: interim events pass only between the
VAD's START_OF_SPEECH and the batch FINAL_TRANSCRIPT for that segment, so a late delta from
the streaming model can't linger on screen after the real transcript has replaced it.

The interim side is best-effort. If it fails (server down, model not loaded) the stream logs
once and carries on as finals-only -- the agent must not go deaf because the cosmetic path
broke.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from livekit import rtc
from livekit.agents import stt, utils
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.agents.vad import VAD

if TYPE_CHECKING:
    from livekit.agents.voice.events import ConversationItemAddedEvent

logger = logging.getLogger("nad.dual_stt")

_FINAL_SIDE = (
    stt.SpeechEventType.START_OF_SPEECH,
    stt.SpeechEventType.END_OF_SPEECH,
    stt.SpeechEventType.FINAL_TRANSCRIPT,
    stt.SpeechEventType.RECOGNITION_USAGE,
)


class DualSTT(stt.STT):
    def __init__(self, *, final: stt.STT, interim: stt.STT, vad: VAD) -> None:
        if final.capabilities.streaming:
            raise ValueError(
                "`final` must be a batch STT (it gets its own StreamAdapter here)"
            )
        if not interim.capabilities.streaming:
            raise ValueError("`interim` must be a streaming STT")
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                keyterms=final.capabilities.keyterms,
                chat_context=final.capabilities.chat_context,
            )
        )
        self._final = stt.StreamAdapter(stt=final, vad=vad)
        self._interim = interim
        self._final.on("metrics_collected", self._on_metrics)
        self._final.on("error", self._on_error)

    # Identity and forwarding follow the final STT: it is the one whose output counts.
    @property
    def model(self) -> str:
        return self._final.model

    @property
    def provider(self) -> str:
        return self._final.provider

    def _update_session_keyterms(self, keyterms: list[str]) -> None:
        self._final._update_session_keyterms(keyterms)

    def _push_conversation_item(self, ev: ConversationItemAddedEvent) -> None:
        self._final._push_conversation_item(ev)

    def prewarm(self) -> None:
        self._final.prewarm()
        self._interim.prewarm()

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        # One-shot recognition has no interim side; the batch STT answers alone.
        return await self._final.recognize(
            buffer=buffer, language=language, conn_options=conn_options
        )

    def _on_metrics(self, *args, **kwargs) -> None:
        self.emit("metrics_collected", *args, **kwargs)

    def _on_error(self, *args, **kwargs) -> None:
        self.emit("error", *args, **kwargs)

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        return DualStream(
            self,
            final=self._final.stream(language=language, conn_options=conn_options),
            interim=self._interim.stream(language=language, conn_options=conn_options),
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        self._final.off("metrics_collected", self._on_metrics)
        self._final.off("error", self._on_error)
        await self._final.aclose()
        await self._interim.aclose()


class DualStream(stt.RecognizeStream):
    def __init__(
        self,
        stt_: DualSTT,
        *,
        final: stt.RecognizeStream,
        interim: stt.RecognizeStream,
        conn_options: APIConnectOptions,
    ) -> None:
        # No retries at this level: both inner streams already retry their own connections.
        super().__init__(
            stt=stt_, conn_options=APIConnectOptions(max_retry=0, timeout=conn_options.timeout)
        )
        self._final_stream = final
        self._interim_stream = interim
        # Open until the first final: the streaming model only speaks once someone has.
        self._interim_open = True
        self._interim_dead = False

    def _push_interim(self, item: rtc.AudioFrame | stt.RecognizeStream._FlushSentinel) -> None:
        if self._interim_dead:
            return
        try:
            if isinstance(item, self._FlushSentinel):
                self._interim_stream.flush()
            else:
                self._interim_stream.push_frame(item)
        except RuntimeError:
            # The inner stream closes its channels once its own retries are exhausted;
            # pushing after that raises. relay_interim logs the failure; here we just stop.
            self._interim_dead = True

    async def _run(self) -> None:
        async def forward_input() -> None:
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    self._final_stream.flush()
                else:
                    self._final_stream.push_frame(item)
                self._push_interim(item)
            self._final_stream.end_input()
            if not self._interim_dead:
                self._interim_stream.end_input()

        async def relay_final() -> None:
            async for ev in self._final_stream:
                if ev.type == stt.SpeechEventType.START_OF_SPEECH:
                    self._interim_open = True
                elif ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    self._interim_open = False
                if ev.type in _FINAL_SIDE:
                    self._event_ch.send_nowait(ev)

        async def relay_interim() -> None:
            try:
                async for ev in self._interim_stream:
                    if (
                        ev.type == stt.SpeechEventType.INTERIM_TRANSCRIPT
                        and self._interim_open
                    ):
                        self._event_ch.send_nowait(ev)
            except Exception:
                self._interim_dead = True
                logger.warning(
                    "interim STT stream failed; continuing with final transcripts only",
                    exc_info=True,
                )

        tasks = [
            asyncio.create_task(forward_input(), name="dual_stt.forward_input"),
            asyncio.create_task(relay_final(), name="dual_stt.relay_final"),
            asyncio.create_task(relay_interim(), name="dual_stt.relay_interim"),
        ]
        try:
            # The final relay ends when the input does; the interim relay may end early
            # (failure) or late (server flushing after end_input), and neither should hold
            # the stream open once finals are done.
            await asyncio.gather(tasks[0], tasks[1])
        finally:
            await utils.aio.cancel_and_wait(*tasks)
            await self._final_stream.aclose()
            await self._interim_stream.aclose()
