"""Beamer Audio for the Mac: a menu-bar companion that plays a paired PC's sound on this Mac.

    .venv-audio/bin/python tools/audio_menu_mac.py

Runs beside the official Beamer and reads its pairing; it never writes Beamer's settings. Its own
choices (on or off, which machines may play, the output, the buffer) are kept in audio.json next to
Beamer's settings.json. See docs/audio-experiment.md.
"""

import argparse
import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rumps  # noqa: E402

import audio_service  # noqa: E402
from audio_service import PairingError, audio_stream  # noqa: E402

LOGGER = logging.getLogger("Beamer")

BUFFERS = ((40, "40 ms — wired"), (80, "80 ms — good Wi-Fi"), (120, "120 ms — busy Wi-Fi"), (200, "200 ms — weak Wi-Fi"))
DEFAULTS = {"receive": True, "blocked": [], "device": None, "latency_ms": 40}
TITLE_PLAYING, TITLE_LISTENING, TITLE_OFF = "♪ ●", "♪", "♪ ○"


class AudioMenu(rumps.App):
    def __init__(self, settings_path, port):
        super().__init__("Beamer Audio", title=TITLE_LISTENING, quit_button=None)
        self.settings_path = settings_path
        self.port = port
        self.prefs = audio_service.Preferences(audio_service.preferences_path(settings_path), DEFAULTS)
        self.peers = []
        self.problem = None
        self.session = None
        self._lock = threading.Lock()
        self._playing = None
        self._shape = None
        self._reload_peers()
        self._apply()
        self._rebuild()
        self._timer = rumps.Timer(self._tick, 1)
        self._timer.start()
        self._seconds = 0

    # What it plays ------------------------------------------------------------------------

    def _reload_peers(self):
        try:
            self.peers = audio_service.paired_peers(self.settings_path)
            if self.problem and self.problem.startswith("No machine"):
                self.problem = None
        except PairingError as error:
            self.peers = []
            self.problem = str(error)

    def _allowed(self):
        blocked = set(self.prefs["blocked"])
        return {entry["token"]: audio_service.label(entry, self.peers)
                for entry in self.peers if audio_service.peer_key(entry) not in blocked}

    def _apply(self):
        """Starts, restarts or stops the receiver to match the preferences."""
        self._stop_session()
        if not self.prefs["receive"] or not self.peers:
            return
        allowed = self._allowed()
        if not allowed:
            self.problem = "No machine is allowed to play here. Tick one under Play sound from."
            return
        try:
            self.session = audio_service.ReceiveSession(
                allowed, port=self.port, device=self.prefs["device"], latency_ms=self.prefs["latency_ms"],
                on_change=self._changed)
            self.problem = None
        except OSError as error:
            if getattr(error, "errno", None) == 48:
                self.problem = f"Another receiver is already using UDP port {self.port}. Quit it first."
            else:
                self.problem = f"Could not start: {error}"
        except Exception as error:
            # PortAudio's own errors, such as an output device that has gone.
            self.problem = f"Could not start: {error}"

    def _stop_session(self):
        session, self.session = self.session, None
        with self._lock:
            self._playing = None
        if session:
            session.stop()

    def _changed(self, name):
        with self._lock:
            self._playing = name

    # The menu -----------------------------------------------------------------------------

    def _status(self):
        with self._lock:
            playing = self._playing
        if self.problem:
            return self.problem, None, TITLE_OFF
        if not self.prefs["receive"]:
            return "Off", None, TITLE_OFF
        if not self.session:
            return "Starting…", None, TITLE_OFF
        if playing:
            stats = self.session.stats()
            detail = (f"{self.session.latency_ms:.0f} ms buffer · {stats['lost']} lost · {stats['late']} late · "
                      f"{stats['underruns']} gaps")
            return f"Playing sound from {playing}", detail, TITLE_PLAYING
        return f"Listening on {self.session.output_name}", "Nothing is sending sound yet.", TITLE_LISTENING

    def _rebuild(self):
        status, detail, title = self._status()
        self.title = title
        self.menu.clear()
        self.status_item = rumps.MenuItem(status)
        self.detail_item = rumps.MenuItem(detail or "")
        items = [self.status_item]
        if detail:
            items.append(self.detail_item)
        receive = rumps.MenuItem("Receive sound", callback=self._toggle_receive)
        receive.state = int(bool(self.prefs["receive"]))
        items += [None, receive, None, rumps.MenuItem("Play sound from")]
        blocked = set(self.prefs["blocked"])
        for entry in self.peers:
            key = audio_service.peer_key(entry)
            item = rumps.MenuItem(f"    {audio_service.label(entry, self.peers)}",
                                  callback=lambda sender, key=key: self._toggle_peer(key))
            item.state = int(key not in blocked)
            items.append(item)
        if not self.peers:
            items.append(rumps.MenuItem("    No paired machines"))
        items += [None, self._output_menu(), self._buffer_menu(), None,
                  rumps.MenuItem("Quit Beamer Audio", callback=self._quit)]
        self.menu.update(items)
        self._shape = (status, bool(detail))

    def _output_menu(self):
        menu = rumps.MenuItem("Output")
        try:
            devices = audio_service.output_devices()
        except Exception:
            devices = []
        current = self.prefs["device"]
        default = rumps.MenuItem("System default", callback=lambda sender: self._set(device=None))
        default.state = int(current is None)
        menu.add(default)
        for name in devices:
            item = rumps.MenuItem(name, callback=lambda sender, name=name: self._set(device=name))
            item.state = int(current == name)
            menu.add(item)
        return menu

    def _buffer_menu(self):
        menu = rumps.MenuItem("Buffer")
        for value, text in BUFFERS:
            item = rumps.MenuItem(text, callback=lambda sender, value=value: self._set(latency_ms=value))
            item.state = int(self.prefs["latency_ms"] == value)
            menu.add(item)
        return menu

    # Choices ------------------------------------------------------------------------------

    def _set(self, **changes):
        self.prefs.set(**changes)
        self._apply()
        self._rebuild()

    def _toggle_receive(self, _sender):
        self._set(receive=not self.prefs["receive"])

    def _toggle_peer(self, key):
        blocked = set(self.prefs["blocked"])
        blocked.symmetric_difference_update({key})
        self._set(blocked=sorted(blocked))

    def _quit(self, _sender):
        self._timer.stop()
        self._stop_session()
        rumps.quit_application()

    # Every second -------------------------------------------------------------------------

    def _tick(self, _timer):
        self._seconds += 1
        if self._seconds % 5 == 0:
            before = [(entry["token"], audio_service.label(entry, self.peers)) for entry in self.peers]
            self._reload_peers()
            after = [(entry["token"], audio_service.label(entry, self.peers)) for entry in self.peers]
            if before != after:
                self._apply()
                self._rebuild()
                return
            if self.prefs["receive"] and not self.session and self.peers:
                # A port that was busy, or an output that was missing, may be free now.
                self._apply()
        status, detail, title = self._status()
        if (status, bool(detail)) != self._shape:
            self._rebuild()
            return
        self.title = title
        self.status_item.title = status
        if detail:
            self.detail_item.title = detail


def main(argv=None):
    parser = argparse.ArgumentParser(description="Beamer Audio: play a paired PC's sound on this Mac.")
    parser.add_argument("--settings", default=audio_service.default_settings_path(),
                        help="Beamer's settings.json (default: this user's)")
    parser.add_argument("--port", type=int, default=audio_stream.AUDIO_PORT, help="UDP port (default: %(default)s)")
    args = parser.parse_args(argv)
    audio_service.setup_logging(args.settings)
    import AppKit
    # A menu-bar app: no Dock icon and no app menu.
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    AudioMenu(args.settings, args.port).run()


if __name__ == "__main__":
    main()
