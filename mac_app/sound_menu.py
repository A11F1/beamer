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

import audio_capture_mac
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
DEFAULTS = {"receive": True, "mic": True, "latency_ms": 40, "mic_to": "", "sound_to": "",
            # The output this Mac had before it was switched to BlackHole 16ch to send its sound,
            # kept until it is put back, so a crash or a quit never leaves the Mac silent.
            "restore_output": ""}
# How often the pairing is read again, and a receiver that could not start is tried again.
RECHECK_SECONDS = 5.0
ADDRESS_IN_USE = 48
# The output the microphone stream plays into: BlackHole's two-channel device.
MIC_DEVICE_WORD = "BlackHole 2ch"
# A Mac's own speakers, the fallback when nothing better is known: a device name, never shown.
BUILT_IN_OUTPUT_WORD = "Speakers"
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
        # What this Mac sends to a PC, its microphone and its sound, and what each last said.
        self.senders = {MIC: None, SOUND: None}
        self.send_problems = {MIC: None, SOUND: None}
        self._send_items = {MIC: {}, SOUND: {}}
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
        self.sound_send_status_item = rumps.MenuItem(tr("Not sending this Mac's sound"))
        for child in (self.status_item, self.detail_item, None, self.receive_item, self.buffer_menu, None,
                      self.mic_status_item, self.mic_item, None, self.sound_send_status_item):
            self.item.add(rumps.separator if child is None else child)
        self._sound_send_anchor = self.sound_send_status_item.title
        self.item.add(rumps.separator)
        self.item.add(self.mic_send_status_item)
        self._mic_send_anchor = self.mic_send_status_item.title

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
        """One item per PC to send this Mac's sound to, and one to send its microphone to, each
        after the line that says how it goes."""
        targets = [entry for entry in self.peers if entry.get("platform") != "macos"]
        shown = [(audio_service.peer_key(entry), audio_service.label(entry, self.peers)) for entry in targets]
        if shown == self._send_shown:
            return
        self._send_shown = shown
        for kind, words, anchor in ((SOUND, tr("Send this Mac's sound to {name}"), self._sound_send_anchor),
                                    (MIC, tr("Send this Mac's microphone to {name}"), self._mic_send_anchor)):
            for item in self._send_items[kind].values():
                del self.item[item.title]
            self._send_items[kind] = {}
            for key, name in reversed(shown):
                item = rumps.MenuItem(words.format(name=name),
                                      callback=lambda _sender, key=key, kind=kind: self._toggle_send(kind, key))
                self.item.insert_after(anchor, item)
                self._send_items[kind][key] = item

    def _wanted(self, channel):
        return bool(self.prefs["receive" if channel.kind == SOUND else "mic"]) and bool(self.peers)

    def _apply(self):
        """Starts, restarts or stops every stream to match the choices and the pairing."""
        for channel in self.channels.values():
            self._stop(channel)
            channel.problem, channel.missing_device = None, False
        for kind in self.senders:
            self._stop_sending(kind)
        self._restore_output()
        targets = {kind: self._send_target(kind) for kind in self.senders}
        if not any(self._wanted(channel) for channel in self.channels.values()) and not any(targets.values()):
            return
        if targets[SOUND] is not None and not self._switch_output():
            targets[SOUND] = None
        # Nothing is open now, so PortAudio may start afresh and see the devices as they are.
        try:
            audio_playback_mac.refresh_devices()
        except Exception as error:
            self.logger.warning("audio: could not restart PortAudio: %s", error)
        self._output_id = audio_playback_mac.default_output_id()
        for channel in self.channels.values():
            if self._wanted(channel):
                self._start(channel)
        for kind, peer in targets.items():
            if peer is not None:
                self._start_sending(kind, peer)

    def _send_target(self, kind):
        key = self.prefs["sound_to" if kind == SOUND else "mic_to"]
        return audio_service.find_peer(self.peers, key) if key else None

    # Sending this Mac's sound: its output goes to BlackHole 16ch, and what reaches it is captured.

    def _switch_output(self):
        """Makes BlackHole 16ch this Mac's output, remembering the one it had. False if it is not
        installed, which the sound's line then says."""
        target = audio_capture_mac.SOUND_DEVICE
        current = audio_playback_mac.default_output_name()
        if current != target and not audio_playback_mac.set_default_output(target):
            self.send_problems[SOUND] = tr("Install BlackHole 16ch, then choose this again")
            return False
        if current and current != target:
            self.prefs.set(restore_output=current)
        self.logger.info("audio: this Mac's output is %s while its sound plays elsewhere", target)
        return True

    def _restore_output(self):
        """Puts back the output this Mac had before its sound went elsewhere, if it still has
        BlackHole 16ch; a choice made since is left alone."""
        previous = self.prefs["restore_output"]
        if not previous:
            return
        if audio_playback_mac.default_output_name() == audio_capture_mac.SOUND_DEVICE:
            if audio_playback_mac.set_default_output(previous):
                self.logger.info("audio: this Mac's output is %s again", previous)
            else:
                self.logger.warning("audio: could not put back %s", previous)
        self.prefs.set(restore_output="")

    def _real_output(self):
        """Where the sound from other machines plays: the default output, unless that is a
        BlackHole, when it is the output this Mac had before, so it is never sent back."""
        current = audio_playback_mac.default_output_name()
        if not current or "blackhole" not in current.casefold():
            return None
        return self.prefs["restore_output"] or audio_playback_mac.find_output(BUILT_IN_OUTPUT_WORD)

    def _start_sending(self, kind, peer):
        source = audio_service.SOURCE_LOOPBACK if kind == SOUND else audio_service.SOURCE_MIC
        try:
            sender = audio_service.SendSession(peer, self.peers, source=source).start()
            self.senders[kind] = sender
            self.logger.info("audio: sending %s to %s at %s", sender.what, sender.name, sender.host)
        except Exception as error:
            # Not tried again on its own: each try would interrupt the sound. Choosing it again does.
            self.send_problems[kind] = str(error)
            self.logger.warning("audio: could not send %s: %s", kind, error)
            if kind == SOUND:
                self._restore_output()

    def _stop_sending(self, kind):
        sender, self.senders[kind] = self.senders[kind], None
        self.send_problems[kind] = None
        if sender:
            try:
                sender.stop()
            except Exception:
                self.logger.exception("audio: sending %s did not stop cleanly", kind)

    def _start(self, channel):
        names = {entry["token"]: audio_service.label(entry, self.peers) for entry in self.peers}
        device = self._real_output() if channel.kind == SOUND else None
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
        for sender in self.senders.values():
            if sender:
                sender.close_stream()
        try:
            audio_playback_mac.refresh_devices()
        except Exception as error:
            self.logger.warning("audio: could not restart PortAudio: %s", error)
        self._output_id = audio_playback_mac.default_output_id()
        for channel in running:
            try:
                if channel.kind == SOUND:
                    channel.session.device = self._real_output()
                channel.session.open_output()
                self.logger.info("audio: %s reopened (%s), now on %s", channel.kind, why, channel.session.output_name)
            except Exception as error:
                self.logger.warning("audio: %s could not open its output again: %s", channel.kind, error)
                # The next recheck starts it afresh.
                self._stop(channel)
        for kind, sender in self.senders.items():
            if not sender:
                continue
            try:
                sender.open_stream()
            except Exception as error:
                self.logger.warning("audio: sending %s could not open again: %s", kind, error)
                self._stop_sending(kind)
                self.send_problems[kind] = tr("Sound could not start: {error}").format(error=error)

    # Choices ---------------------------------------------------------------------------------

    def _set(self, **changes):
        self.prefs.set(**changes)
        self._apply()
        self.refresh()

    def _toggle(self, _sender):
        self._set(receive=not self.prefs["receive"])

    def _toggle_send(self, kind, key):
        name = "sound_to" if kind == SOUND else "mic_to"
        self._set(**{name: "" if self.prefs[name] == key else key})

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
                  or any(sender and not (sender.input and sender.input.active) for sender in self.senders.values())):
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
        self.mic_send_status_item.title = self._send_status(MIC)
        self.sound_send_status_item.title = self._send_status(SOUND)
        for kind, chosen in ((MIC, self.prefs["mic_to"]), (SOUND, self.prefs["sound_to"])):
            for key, item in self._send_items[kind].items():
                item.state = int(key == chosen)

    def _send_status(self, kind):
        if self.send_problems[kind]:
            return self.send_problems[kind]
        sender = self.senders[kind]
        if not sender:
            return tr("Not sending this Mac's sound") if kind == SOUND else tr("Not sending the microphone")
        if sender.streaming:
            if kind == SOUND:
                return tr("Playing on {name}").format(name=sender.name)
            return tr("Microphone going to {name}").format(name=sender.name)
        return tr("Waiting for {name}…").format(name=sender.name)

    # The settings window's Sound page -----------------------------------------------------------

    def snapshot(self):
        """Everything the settings window's Sound page shows, as the menu shows it."""
        status, detail = self._sound_status()
        return {
            "status": status, "detail": detail, "mic_status": self._mic_status(),
            "send_status": {kind: self._send_status(kind) for kind in (SOUND, MIC)},
            "receive": bool(self.prefs["receive"]), "mic": bool(self.prefs["mic"]),
            "latency_ms": self.prefs["latency_ms"],
            "targets": list(self._send_shown or ()),
            "send_to": {SOUND: self.prefs["sound_to"], MIC: self.prefs["mic_to"]},
        }

    def choose(self, **changes):
        """A choice made on the Sound page: the same as making it in the menu, so the two agree."""
        if any(self.prefs[name] != value for name, value in changes.items()):
            self._set(**changes)

    @property
    def session(self):
        """The sound receiver, for the tests and the command line's habits."""
        return self.channels[SOUND].session

    def stop(self):
        for kind in self.senders:
            self._stop_sending(kind)
        self._restore_output()
        for channel in self.channels.values():
            self._stop(channel)
