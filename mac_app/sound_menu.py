"""Sound, in Beamer's menu-bar menu: plays what a paired PC sends on this Mac's speakers.

Part of the local audio experiment (docs/audio-experiment.md), not of any release. Its choices, on
or off and how much to keep in hand, live in audio.json beside settings.json, so Beamer's own
settings and wire are untouched. Every paired machine may play here: pairing in Beamer already
decides who is trusted.

Everything here runs on the main thread, from the tray's status timer, except `_changed`, which
the receiver calls from its socket thread.
"""

import threading
import time

import rumps

from core import audio_service, audio_stream
from core.audio_service import PairingError
from core.i18n import N_, tr

BUFFERS = (
    (40, N_("40 ms, on a wire")),
    (80, N_("80 ms, for good Wi-Fi")),
    (120, N_("120 ms, for busy Wi-Fi")),
    (200, N_("200 ms, for weak Wi-Fi")),
)
DEFAULTS = {"receive": True, "latency_ms": 40}
# How often the pairing is read again, and a receiver that could not start is tried again.
RECHECK_SECONDS = 5.0
ADDRESS_IN_USE = 48


class SoundMenu:
    def __init__(self, settings_path, logger, port=audio_stream.AUDIO_PORT):
        self.settings_path = settings_path
        self.logger = logger
        self.port = port
        self.prefs = audio_service.Preferences(audio_service.preferences_path(settings_path), DEFAULTS)
        self.session = None
        self.problem = None
        self.peers = []
        self._lock = threading.Lock()
        self._playing = None
        self._checked = 0.0

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
        for child in (self.status_item, self.detail_item, None, self.receive_item, self.buffer_menu):
            self.item.add(rumps.separator if child is None else child)

        self._read_peers()
        self._apply()
        self.refresh()

    # The receiver ----------------------------------------------------------------------------

    def _read_peers(self):
        try:
            peers = audio_service.paired_peers(self.settings_path)
        except PairingError as error:
            self.logger.info("audio: %s", error)
            peers = []
        changed = [entry["token"] for entry in peers] != [entry["token"] for entry in self.peers]
        self.peers = peers
        return changed

    def _apply(self):
        """Starts, restarts or stops the receiver to match the choices and the pairing."""
        self._stop_session()
        self.problem = None
        if not self.prefs["receive"] or not self.peers:
            return
        names = {entry["token"]: audio_service.label(entry, self.peers) for entry in self.peers}
        try:
            self.session = audio_service.ReceiveSession(
                names, port=self.port, latency_ms=self.prefs["latency_ms"], on_change=self._changed)
            self.logger.info("audio: listening on UDP %s for %d machines", self.port, len(names))
        except OSError as error:
            if getattr(error, "errno", None) == ADDRESS_IN_USE:
                self.problem = tr("Something else is using UDP port {port}.").format(port=self.port)
            else:
                self.problem = tr("Sound could not start: {error}").format(error=error)
            self.logger.warning("audio: could not listen: %s", error)
        except Exception as error:
            # PortAudio's own errors, such as no output device at all.
            self.problem = tr("Sound could not start: {error}").format(error=error)
            self.logger.warning("audio: could not open the output: %s", error)

    def _stop_session(self):
        session, self.session = self.session, None
        with self._lock:
            self._playing = None
        if session:
            try:
                session.stop()
            except Exception:
                self.logger.exception("audio: did not stop cleanly")

    def _changed(self, name):
        with self._lock:
            self._playing = name

    # Choices ---------------------------------------------------------------------------------

    def _set(self, **changes):
        self.prefs.set(**changes)
        self._apply()
        self.refresh()

    def _toggle(self, _sender):
        self._set(receive=not self.prefs["receive"])

    # Every status tick -----------------------------------------------------------------------

    def tick(self):
        now = time.monotonic()
        if now - self._checked >= RECHECK_SECONDS:
            self._checked = now
            if self._read_peers() or (self.prefs["receive"] and self.peers and not self.session):
                self._apply()
            elif self.session and not self.session.output.active:
                # The output stopped under it: its device went away, or Core Audio reset.
                self.logger.info("audio: the output stopped; opening it again")
                self._apply()
        self.refresh()

    @property
    def playing(self):
        with self._lock:
            return self._playing

    def _status(self):
        playing = self.playing
        if self.problem:
            return self.problem, ""
        if not self.prefs["receive"]:
            return tr("Sound is off"), ""
        if not self.peers:
            return tr("No paired machine can send sound"), ""
        if not self.session:
            return tr("Starting…"), ""
        if playing:
            stats = self.session.stats()
            return (tr("Playing sound from {name}").format(name=playing),
                    tr("{lost} lost · {late} late · {gaps} gaps").format(
                        lost=stats["lost"], late=stats["late"], gaps=stats["underruns"]))
        return tr("Listening on {output}").format(output=self.session.output_name), ""

    def refresh(self):
        status, detail = self._status()
        self.status_item.title = status
        self.detail_item.title = detail
        if detail:
            self.detail_item.show()
        else:
            self.detail_item.hide()
        self.receive_item.state = int(bool(self.prefs["receive"]))
        for value, choice in self.buffer_items.items():
            choice.state = int(self.prefs["latency_ms"] == value)

    def stop(self):
        self._stop_session()
