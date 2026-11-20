"""Sound, in Beamer's menu-bar menu: plays what a paired PC sends on this Mac's speakers, and its
microphone into BlackHole, where the Mac's own apps can choose it as a microphone.

Part of the local audio experiment (docs/audio-experiment.md), not of any release. Its choices live
in audio.json beside settings.json, so Beamer's own settings and wire are untouched. Every paired
machine may send here: pairing in Beamer already decides who is trusted.

macOS lets no ordinary app add a microphone, so the PC's arrives through BlackHole (MIT; installed
on its own, once): Beamer plays it into BlackHole's output, and BlackHole hands that to any app that
picks it as its input.

The two streams share PortAudio, and restarting PortAudio, the only way it sees a device that came
or went, closes every stream it has open. So outputs are only ever opened together: both closed,
PortAudio started afresh once, both opened again (`_reopen_streams`). The same goes for this Mac's
own microphone, when it is sent to a PC: its input stream is closed and opened with the outputs.

Everything here runs on the main thread, from the tray's status timer, except `_changed`, which a
receiver calls from its socket thread.
"""

import threading
import time

import rumps

import audio_playback_mac
from core import audio_service, audio_stream
from core.audio_service import PairingError
from core.i18n import N_, tr

BUFFERS = (
    (40, N_("40 ms, on a wire")),
    (80, N_("80 ms, for good Wi-Fi")),
    (120, N_("120 ms, for busy Wi-Fi")),
    (200, N_("200 ms, for weak Wi-Fi")),
)
DEFAULTS = {"receive": True, "mic": True, "latency_ms": 40, "mic_to": ""}
# How often the pairing is read again, and a receiver that could not start is tried again.
RECHECK_SECONDS = 5.0
ADDRESS_IN_USE = 48
# The output the microphone stream plays into: BlackHole's two-channel device.
MIC_DEVICE_WORD = "BlackHole"
SOUND, MIC = "sound", "mic"


class _Channel:
    """One receiver: what it is, its session, and what it last said."""

    def __init__(self, kind, stream, port):
        self.kind = kind
        self.stream = stream
        self.port = port
        self.session = None
        self.problem = None
        self.missing_device = False
        self.lock = threading.Lock()
        self.playing = None


class SoundMenu:
    def __init__(self, settings_path, logger, port=audio_stream.AUDIO_PORT, mic_port=audio_stream.MIC_PORT):
        self.settings_path = settings_path
        self.logger = logger
        self.prefs = audio_service.Preferences(audio_service.preferences_path(settings_path), DEFAULTS)
        self.channels = {SOUND: _Channel(SOUND, audio_stream.SOUND, port),
                         MIC: _Channel(MIC, audio_stream.MIC, mic_port)}
        self.peers = []
        self._checked = 0.0
        # The default output the sound was opened on, to follow it when it changes.
        self._output_id = None
        # This Mac's microphone, sent to a PC, and what it last said.
        self.mic_sender = None
        self.mic_send_problem = None
        self._send_items = {}
        self._send_shown = None

        self.item = rumps.MenuItem(tr("Sound"))
        self.status_item = rumps.MenuItem(tr("Sound is off"))
        self.detail_item = rumps.MenuItem("")
        self.receive_item = rumps.MenuItem(tr("Play other machines' sound here"), callback=self._toggle)
        self.buffer_menu = rumps.MenuItem(tr("Buffer"))
        self.buffer_items = {}
        for value, text in BUFFERS:
            choice = rumps.MenuItem(tr(text), callback=lambda _sender, value=value: self._set(latency_ms=value))
            self.buffer_items[value] = choice
            self.buffer_menu.add(choice)
        self.mic_status_item = rumps.MenuItem(tr("Microphone is off"))
        self.mic_item = rumps.MenuItem(tr("Use other machines' microphone here"), callback=self._toggle_mic)
        self.mic_send_status_item = rumps.MenuItem(tr("Not sending the microphone"))
        for child in (self.status_item, self.detail_item, None, self.receive_item, self.buffer_menu, None,
                      self.mic_status_item, self.mic_item, None, self.mic_send_status_item):
            self.item.add(rumps.separator if child is None else child)

        self._read_peers()
        self._apply()
        self.refresh()

    # The receivers ---------------------------------------------------------------------------

    def _read_peers(self):
        try:
            peers = audio_service.paired_peers(self.settings_path)
        except PairingError as error:
            self.logger.info("audio: %s", error)
            peers = []
        changed = [entry["token"] for entry in peers] != [entry["token"] for entry in self.peers]
        self.peers = peers
        self._send_targets()
        return changed

    def _send_targets(self):
        """One item per PC to send this Mac's microphone to, after the line that says how it goes."""
        targets = [entry for entry in self.peers if entry.get("platform") != "macos"]
        shown = [(audio_service.peer_key(entry), audio_service.label(entry, self.peers)) for entry in targets]
        if shown == self._send_shown:
            return
        self._send_shown = shown
        for item in self._send_items.values():
            del self.item[item.title]
        self._send_items = {}
        for key, name in shown:
            item = rumps.MenuItem(tr("Send this Mac's microphone to {name}").format(name=name),
                                  callback=lambda _sender, key=key: self._toggle_send(key))
            self.item.add(item)
            self._send_items[key] = item

    def _wanted(self, channel):
        return bool(self.prefs["receive" if channel.kind == SOUND else "mic"]) and bool(self.peers)

    def _apply(self):
        """Starts, restarts or stops every stream to match the choices and the pairing."""
        for channel in self.channels.values():
            self._stop(channel)
            channel.problem, channel.missing_device = None, False
        self._stop_sending()
        sending = self._send_target()
        if not any(self._wanted(channel) for channel in self.channels.values()) and sending is None:
            return
        # Nothing is open now, so PortAudio may start afresh and see the devices as they are.
        try:
            audio_playback_mac.refresh_devices()
        except Exception as error:
            self.logger.warning("audio: could not restart PortAudio: %s", error)
        self._output_id = audio_playback_mac.default_output_id()
        for channel in self.channels.values():
            if self._wanted(channel):
                self._start(channel)
        if sending is not None:
            self._start_sending(sending)

    def _send_target(self):
        key = self.prefs["mic_to"]
        return audio_service.find_peer(self.peers, key) if key else None

    def _start_sending(self, peer):
        try:
            self.mic_sender = audio_service.SendSession(peer, self.peers, source=audio_service.SOURCE_MIC).start()
            self.logger.info("audio: sending %s to %s at %s", self.mic_sender.what, self.mic_sender.name,
                             self.mic_sender.host)
        except Exception as error:
            # Not tried again on its own: each try would interrupt the sound. Choosing it again does.
            self.mic_send_problem = str(error)
            self.mic_sender = None
            self.logger.warning("audio: could not send the microphone: %s", error)

    def _stop_sending(self):
        sender, self.mic_sender = self.mic_sender, None
        self.mic_send_problem = None
        if sender:
            try:
                sender.stop()
            except Exception:
                self.logger.exception("audio: the microphone did not stop cleanly")

    def _start(self, channel):
        names = {entry["token"]: audio_service.label(entry, self.peers) for entry in self.peers}
        device = None
        if channel.kind == MIC:
            device = audio_playback_mac.find_output(MIC_DEVICE_WORD)
            if device is None:
                channel.missing_device = True
                return
        try:
            channel.session = audio_service.ReceiveSession(
                names, port=channel.port, device=device, latency_ms=self.prefs["latency_ms"],
                stream=channel.stream, on_change=lambda name, channel=channel: self._changed(channel, name))
            self.logger.info("audio: %s listening on UDP %s for %d machines", channel.kind, channel.port, len(names))
        except OSError as error:
            if getattr(error, "errno", None) == ADDRESS_IN_USE:
                channel.problem = tr("Something else is using UDP port {port}.").format(port=channel.port)
            else:
                channel.problem = tr("Sound could not start: {error}").format(error=error)
            self.logger.warning("audio: %s could not listen: %s", channel.kind, error)
        except Exception as error:
            # PortAudio's own errors, such as no output device at all.
            channel.problem = tr("Sound could not start: {error}").format(error=error)
            self.logger.warning("audio: %s could not open its output: %s", channel.kind, error)

    def _stop(self, channel):
        session, channel.session = channel.session, None
        with channel.lock:
            channel.playing = None
        if session:
            try:
                session.stop()
            except Exception:
                self.logger.exception("audio: %s did not stop cleanly", channel.kind)

    def _changed(self, channel, name):
        with channel.lock:
            channel.playing = name

    def _reopen_streams(self, why):
        """Every open stream closed (outputs and this Mac's microphone), PortAudio started afresh
        once, and every one opened again, while the receivers and the sender keep running: the PC
        never notices."""
        running = [channel for channel in self.channels.values() if channel.session]
        for channel in running:
            channel.session.close_output()
        if self.mic_sender:
            self.mic_sender.close_stream()
        try:
            audio_playback_mac.refresh_devices()
        except Exception as error:
            self.logger.warning("audio: could not restart PortAudio: %s", error)
        self._output_id = audio_playback_mac.default_output_id()
        for channel in running:
            try:
                channel.session.open_output()
                self.logger.info("audio: %s reopened (%s), now on %s", channel.kind, why, channel.session.output_name)
            except Exception as error:
                self.logger.warning("audio: %s could not open its output again: %s", channel.kind, error)
                # The next recheck starts it afresh.
                self._stop(channel)
        if self.mic_sender:
            try:
                self.mic_sender.open_stream()
            except Exception as error:
                self.logger.warning("audio: the microphone could not open again: %s", error)
                self._stop_sending()
                self.mic_send_problem = tr("Sound could not start: {error}").format(error=error)

    # Choices ---------------------------------------------------------------------------------

    def _set(self, **changes):
        self.prefs.set(**changes)
        self._apply()
        self.refresh()

    def _toggle(self, _sender):
        self._set(receive=not self.prefs["receive"])

    def _toggle_send(self, key):
        self._set(mic_to="" if self.prefs["mic_to"] == key else key)

    def _toggle_mic(self, _sender):
        # Turning it on also looks for BlackHole again, so installing it needs no restart.
        self._set(mic=not self.prefs["mic"])

    # Every status tick -----------------------------------------------------------------------

    def tick(self):
        now = time.monotonic()
        if now - self._checked >= RECHECK_SECONDS:
            self._checked = now
            # A receiver that failed is tried again; a missing BlackHole is not, as each try would
            # interrupt the sound. Turning the microphone off and on looks again.
            failed = any(self._wanted(channel) and not channel.session and not channel.missing_device
                         for channel in self.channels.values())
            if self._read_peers() or failed:
                self._apply()
            elif (any(channel.session and not (channel.session.output and channel.session.output.active)
                      for channel in self.channels.values())
                  or (self.mic_sender and not (self.mic_sender.input and self.mic_sender.input.active))):
                # A stream stopped under it: its device went away, or Core Audio reset.
                self._reopen_streams("stream-stopped")
        self._follow_default_output()
        self.refresh()

    def _follow_default_output(self):
        """Moves the sound to the Mac's default output when that changes: headphones plugged into
        the jack, AirPods connecting, another output chosen in Control Centre. The microphone stays
        on BlackHole, but is reopened with it, since PortAudio restarts for both."""
        if not self.channels[SOUND].session:
            return
        current = audio_playback_mac.default_output_id()
        if current is None or current == self._output_id:
            return
        self._reopen_streams("default-output-changed")

    def _sound_status(self):
        channel = self.channels[SOUND]
        with channel.lock:
            playing = channel.playing
        if channel.problem:
            return channel.problem, ""
        if not self.prefs["receive"]:
            return tr("Sound is off"), ""
        if not self.peers:
            return tr("No paired machine can send sound"), ""
        if not channel.session:
            return tr("Starting…"), ""
        if playing:
            stats = channel.session.stats()
            return (tr("Playing sound from {name}").format(name=playing),
                    tr("{lost} lost · {late} late · {gaps} gaps").format(
                        lost=stats["lost"], late=stats["late"], gaps=stats["underruns"]))
        return tr("Listening on {output}").format(output=channel.session.output_name), ""

    def _mic_status(self):
        channel = self.channels[MIC]
        with channel.lock:
            playing = channel.playing
        if channel.problem:
            return channel.problem
        if not self.prefs["mic"]:
            return tr("Microphone is off")
        if not self.peers:
            return tr("No paired machine can send sound")
        if channel.missing_device:
            return tr("Install BlackHole 2ch, then turn this off and on")
        if not channel.session:
            return tr("Starting…")
        if playing:
            return tr("Microphone from {name}, into {output}").format(
                name=playing, output=channel.session.output_name)
        return tr("Microphone waiting, into {output}").format(output=channel.session.output_name)

    def refresh(self):
        status, detail = self._sound_status()
        self.status_item.title = status
        self.detail_item.title = detail
        if detail:
            self.detail_item.show()
        else:
            self.detail_item.hide()
        self.receive_item.state = int(bool(self.prefs["receive"]))
        for value, choice in self.buffer_items.items():
            choice.state = int(self.prefs["latency_ms"] == value)
        self.mic_status_item.title = self._mic_status()
        self.mic_item.state = int(bool(self.prefs["mic"]))
        self.mic_send_status_item.title = self._mic_send_status()
        for key, item in self._send_items.items():
            item.state = int(key == self.prefs["mic_to"])

    def _mic_send_status(self):
        if self.mic_send_problem:
            return self.mic_send_problem
        sender = self.mic_sender
        if not sender:
            return tr("Not sending the microphone")
        if sender.streaming:
            return tr("Microphone going to {name}").format(name=sender.name)
        return tr("Waiting for {name}…").format(name=sender.name)

    @property
    def session(self):
        """The sound receiver, for the tests and the command line's habits."""
        return self.channels[SOUND].session

    def stop(self):
        self._stop_sending()
        for channel in self.channels.values():
            self._stop(channel)
