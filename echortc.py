from typing import Dict, Tuple, Optional
import tempfile
import asyncio
import random
import string
import os

from attr import dataclass
from aioice import Candidate
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.rtcicetransport import candidate_from_aioice
from aiortc.contrib.media import MediaBlackhole, MediaRecorder, MediaPlayer, Frame, MediaStreamError

from mautrix.types import (EventType, CallEvent, CallInviteEventContent, CallAnswerEventContent,
                           CallData, CallDataType, CallCandidatesEventContent, RoomID, UserID,
                           CallHangupEventContent)

from maubot import Plugin
from maubot.handlers import event

UniqueCallID = Tuple[RoomID, UserID, str]


class ProxyTrack(MediaStreamTrack):
    _source: Optional[MediaStreamTrack]
    _source_wait: Optional[asyncio.Future]

    def __init__(self, source: Optional[MediaStreamTrack] = None, kind: Optional[str] = None
                 ) -> None:
        super().__init__()
        self.kind = kind or source.kind
        self._source = source
        self._source_wait = asyncio.get_running_loop().create_future() if source is None else None

    def set_source(self, source: MediaStreamTrack) -> None:
        self._source = source
        if self._source_wait:
            self._source_wait.set_result(None)

    async def recv(self) -> Frame:
        if self._source is None:
            await self._source_wait
        try:
            return await self._source.recv()
        except MediaStreamError:
            self._source = None
            self._source_wait = asyncio.get_running_loop().create_future()
            await self._source_wait
            return await self._source.recv()

    def stop(self) -> None:
        print("Proxy track stop 3:")
        self._source.stop()
        super().stop()


@dataclass
class WrappedConn:
    pc: RTCPeerConnection
    prepare_waiter: asyncio.Future
    candidate_waiter: asyncio.Future


class EchoRTCBot(Plugin):
    conns: Dict[UniqueCallID, WrappedConn]
    party_id: str

    async def start(self) -> None:
        self.conns = {}
        self.party_id = "".join(random.choices(string.ascii_letters + string.digits, k=8))

    async def stop(self) -> None:
        for (room_id, _, call_id), conn in self.conns.items():
            hangup = CallHangupEventContent(call_id=call_id, version=1, party_id=self.party_id)
            await self.client.send_message_event(room_id, EventType.CALL_HANGUP, hangup)
            await conn.pc.close()
        pass

    @event.on(EventType.CALL_CANDIDATES)
    async def candidates(self, evt: CallEvent[CallCandidatesEventContent]) -> None:
        unique_id = (evt.room_id, evt.sender, evt.content.call_id)
        try:
            conn = self.conns[unique_id]
        except KeyError:
            return
        await conn.prepare_waiter
        for raw_candidate in evt.content.candidates:
            if not raw_candidate.candidate:
                # End of candidates
                conn.candidate_waiter.set_result(None)
                break
            try:
                candidate = candidate_from_aioice(Candidate.from_sdp(raw_candidate.candidate))
            except ValueError as e:
                print(f"{e}: {raw_candidate.serialize()}")
                continue
            candidate.sdpMid = raw_candidate.sdp_mid
            candidate.sdpMLineIndex = raw_candidate.sdp_m_line_index
            self.log.info("Adding candidate %s for %s", candidate, evt.content.call_id)
            await conn.pc.addIceCandidate(candidate)
        await self.client.send_receipt(evt.room_id, evt.event_id)

    @event.on(EventType.CALL_HANGUP)
    async def hangup(self, evt: CallEvent[CallHangupEventContent]) -> None:
        try:
            await self.conns.pop((evt.room_id, evt.sender, evt.content.call_id)).pc.close()
            await self.client.send_receipt(evt.room_id, evt.event_id)
        except KeyError:
            return

    @event.on(EventType.CALL_INVITE)
    async def invite(self, evt: CallEvent[CallInviteEventContent]) -> None:
        if evt.content.version != "1":
            raise RuntimeError(f"evt.content.version={evt.content.version}")
            #return
        offer = RTCSessionDescription(sdp=evt.content.offer.sdp, type=str(evt.content.offer.type))
        pc = RTCPeerConnection()
        unique_id = (evt.room_id, evt.sender, evt.content.call_id)

        def log_info(msg, *args):
            self.log.info("%s " + msg, evt.content.call_id, *args)

        log_info("Created for %s", evt.sender)

        conn = self.conns[unique_id] = WrappedConn(pc=pc,
                                                   candidate_waiter=self.loop.create_future(),
                                                   prepare_waiter=self.loop.create_future())

        # TODO load these from the plugin archive
        hello = MediaPlayer("hello.wav")
        beep = MediaPlayer("beep.wav")
        bye = MediaPlayer("bye.wav")
        input_tracks = {}
        output_tracks = {
            "audio": ProxyTrack(source=hello.audio),
            "video": ProxyTrack(kind="video"),
        }
        blackhole = MediaBlackhole()

        async def task() -> None:
            log_info("Starting task")
            await asyncio.sleep(8.1)
            log_info("Stopping blackhole")
            await blackhole.stop()
            with tempfile.TemporaryDirectory() as tmpdir:
                log_info("Starting recording to %s", tmpdir)
                wav_file = os.path.join(tmpdir, "recording.wav")
                mp4_file = os.path.join(tmpdir, "recording.mp4")
                audio_recorder = MediaRecorder(wav_file, format="wav")
                audio_recorder.addTrack(input_tracks["audio"])
                await audio_recorder.start()
                if "video" in input_tracks:
                    video_recorder = MediaRecorder(mp4_file, format="mp4")
                    video_recorder.addTrack(input_tracks["video"])
                    await video_recorder.start()
                else:
                    video_recorder = None
                log_info("Waiting for recording to finish")
                await asyncio.sleep(10)
                log_info("Stopping recording and beeping")
                await audio_recorder.stop()
                if video_recorder:
                    await video_recorder.stop()
                output_tracks["audio"].set_source(beep.audio)
                log_info("Re-enabling blackhole")
                for track in input_tracks.values():
                    blackhole.addTrack(track)
                await blackhole.start()
                log_info("Waiting for beep to finish")
                await asyncio.sleep(1.5)
                log_info("Playing back recording")
                audio_playback = MediaPlayer(wav_file, format="wav")
                output_tracks["audio"].set_source(audio_playback.audio)
                if video_recorder:
                    video_playback = MediaPlayer(mp4_file, format="mp4")
                    output_tracks["video"].set_source(video_playback.video)
                log_info("Waiting for playback to finish")
                await asyncio.sleep(10)
            log_info("Stopping playback")
            output_tracks["audio"].set_source(bye.audio)
            log_info("Waiting for end message")
            await asyncio.sleep(5)
            log_info("Hanging up")
            self.conns.pop(unique_id, None)
            hangup = CallHangupEventContent(call_id=evt.content.call_id, version=1,
                                            party_id=self.party_id)
            await self.client.send_message_event(evt.room_id, EventType.CALL_HANGUP, hangup)
            await pc.close()

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            log_info("Connection state is %s", pc.connectionState)
            if pc.connectionState == "failed":
                await pc.close()
                self.conns.pop(unique_id, None)
            if pc.connectionState == "connected":
                asyncio.create_task(task())

        @pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            input_tracks[track.kind] = track
            blackhole.addTrack(track)
            pc.addTrack(output_tracks[track.kind])

            @track.on("ended")
            async def on_ended() -> None:
                log_info("Track %s ended", track.kind)
                await blackhole.stop()

        await pc.setRemoteDescription(offer)
        await blackhole.start()

        log_info("Ready to receive candidates")
        conn.prepare_waiter.set_result(None)
        await self.client.send_receipt(evt.room_id, evt.event_id)
        await conn.candidate_waiter
        log_info("Got candidates")

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        response = CallAnswerEventContent(call_id=evt.content.call_id,
                                          party_id=self.party_id,
                                          version="1",
                                          answer=CallData(
                                              sdp=pc.localDescription.sdp,
                                              type=CallDataType(pc.localDescription.type),
                                          ))
        await self.client.send_message_event(evt.room_id, EventType.CALL_ANSWER, response)
