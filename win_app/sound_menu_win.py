"""Sound, in Beamer's tray menu on Windows: plays this PC's sound on a paired Mac, and mutes this PC
meanwhile.

Part of the local audio experiment (docs/audio-experiment.md), not of any release. Its choices,
where to send and whether to mute, live in audio.json beside settings.json, so Beamer's own
settings and wire are untouched.

Windows' audio calls (soundcard's WASAPI loopback, pycaw's mute) need COM multithreaded, and Qt
makes the main thread single-threaded for drag and drop, so every one of them runs on one worker
thread of their own. The menu only ever reads what that thread last saw.
"""

import collections
import concurrent.futures
import logging
import threading
import time

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

from core import audio_service
from core.audio_service import PairingError
from core.i18n import tr

LOGGER = logging.getLogger("Beamer")

DEFAULTS = {"send_to": "", "mute_local": True}
TICK_MS = 250
RECHECK_SECONDS = 5.0
CLOSE_SECONDS = 4.0


def _com_ready():
    """The worker's start: soundcard's first import makes this thread's COM multithreaded."""
    import soundcard  # noqa: F401


class Engine:
    """Owns the sending session on the COM worker. Every method returns at once; what happened is
    read back through `snapshot()`."""

    def __init__(self, settings_path):
        self.settings_path = settings_path
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, initializer=_com_ready,
                                                             thread_name_prefix="audio-com")
        self._lock = threading.Lock()
        self._session = None
        self._problem = None
        self._notices = collections.deque(maxlen=1)
        self._wanted = None

    def _notice(self, text):
        LOGGER.info("audio: %s", text)
        with self._lock:
            self._notices.append(text)

    def apply(self, peer_key, mute_local):
        """Sends to the peer remembered as `peer_key`, or stops when it is empty."""
        wanted = (peer_key, mute_local)
        self._wanted = wanted
        self._worker.submit(self._apply, wanted)

    def _apply(self, wanted):
        if wanted != self._wanted:
            return
        self._stop_session()
        peer_key, mute_local = wanted
        with self._lock:
            self._problem = None
            self._notices.clear()
        if not peer_key:
            return
        try:
            peers = audio_service.paired_peers(self.settings_path)
            peer = audio_service.find_peer(peers, peer_key)
            if peer is None:
                raise PairingError(tr("That machine is no longer paired with this PC."))
            session = audio_service.SendSession(peer, peers, mute_local=mute_local, notice=self._notice)
            session.start()
            LOGGER.info("audio: sending %s to %s at %s", session.what, session.name, session.host)
        except Exception as error:
            LOGGER.warning("audio: could not start sending: %s", error)
            with self._lock:
                self._problem = str(error)
            return
        with self._lock:
            self._session = session

    def _stop_session(self):
        with self._lock:
            session, self._session = self._session, None
        if session:
            try:
                session.stop()
            except Exception:
                LOGGER.exception("audio: did not stop cleanly")

    def tick(self):
        self._worker.submit(self._tick)

    def _tick(self):
        with self._lock:
            session = self._session
        if session:
            try:
                session.tick()
            except Exception:
                LOGGER.exception("audio: the mute check failed")

    def snapshot(self):
        """What to show: a status line, and a detail line or an empty one."""
        with self._lock:
            session, problem = self._session, self._problem
            notice = self._notices[-1] if self._notices else ""
        if problem:
            return problem, ""
        if not session:
            if self._wanted and self._wanted[0]:
                return tr("Starting…"), ""
            return tr("Not sending sound"), ""
        if session.streaming:
            capturing = tr("Capturing {output}").format(output=session.output) if session.output else ""
            return tr("Playing on {name}").format(name=session.name), notice or capturing
        return tr("Waiting for {name}…").format(name=session.name), notice or tr(
            "Turn on Sound in Beamer's menu on {name}.").format(name=session.name)

    def close(self, timeout=CLOSE_SECONDS):
        """Stops sending, and puts this PC's output back before returning."""
        self._wanted = None
        future = self._worker.submit(self._stop_session)
        try:
            future.result(timeout)
        except Exception as error:
            LOGGER.warning("audio: did not stop cleanly: %s", error)
        self._worker.shutdown(wait=False)


class SoundMenu(QObject):
    """The Sound submenu. `before` is the tray menu's action it goes in front of."""

    def __init__(self, tray_menu, before, settings_path, parent=None):
        super().__init__(parent)
        self.settings_path = settings_path
        self.prefs = audio_service.Preferences(audio_service.preferences_path(settings_path), DEFAULTS)
        self.engine = Engine(settings_path)
        self.peers = []
        self._peer_actions = {}
        self._shown = None
        self._checked = 0.0
        self._closed = False

        self.menu = QMenu(tr("Sound"), tray_menu)
        tray_menu.insertMenu(before, self.menu)
        self.status_action = self.menu.addAction(tr("Not sending sound"))
        self.status_action.setEnabled(False)
        self.detail_action = self.menu.addAction("")
        self.detail_action.setEnabled(False)
        self.detail_action.setVisible(False)
        self.menu.addSeparator()
        self._targets_end = self.menu.addSeparator()
        self.mute_action = self.menu.addAction(tr("Mute this PC while its sound plays elsewhere"))
        self.mute_action.setCheckable(True)
        self.mute_action.setChecked(bool(self.prefs["mute_local"]))
        self.mute_action.triggered.connect(self._mute_chosen)

        self._read_peers()
        self._apply()
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.refresh()

    # Where to send ---------------------------------------------------------------------------

    def _read_peers(self):
        try:
            peers = audio_service.paired_peers(self.settings_path)
        except PairingError:
            peers = []
        # Only a Mac plays sound in this experiment.
        peers = [entry for entry in peers if entry.get("platform") == "macos"]
        shown = [(audio_service.peer_key(entry), audio_service.label(entry, peers)) for entry in peers]
        self.peers = peers
        if shown == self._shown:
            return
        self._shown = shown
        for action in self._peer_actions.values():
            self.menu.removeAction(action)
        self._peer_actions = {}
        for key, name in shown:
            action = QAction(tr("Play this PC's sound on {name}").format(name=name), self.menu)
            self.menu.insertAction(self._targets_end, action)
            action.setCheckable(True)
            action.triggered.connect(lambda on, key=key: self._target_chosen(key, on))
            self._peer_actions[key] = action
        if not shown:
            action = QAction(tr("No paired Mac to play on"), self.menu)
            self.menu.insertAction(self._targets_end, action)
            action.setEnabled(False)
            self._peer_actions[""] = action

    def _target_chosen(self, key, on):
        self.prefs.set(send_to=key if on else "")
        self._apply()
        self.refresh()

    def _mute_chosen(self, on):
        self.prefs.set(mute_local=bool(on))
        self._apply()

    def _apply(self):
        key = self.prefs["send_to"]
        if key and audio_service.find_peer(self.peers, key) is None:
            key = ""
        self.engine.apply(key, bool(self.prefs["mute_local"]))

    # Every quarter second --------------------------------------------------------------------

    def _tick(self):
        now = time.monotonic()
        if now - self._checked >= RECHECK_SECONDS:
            self._checked = now
            before = [entry["token"] for entry in self.peers]
            self._read_peers()
            if [entry["token"] for entry in self.peers] != before:
                self._apply()
        self.engine.tick()
        self.refresh()

    def refresh(self):
        status, detail = self.engine.snapshot()
        self.status_action.setText(status)
        self.detail_action.setText(detail)
        self.detail_action.setVisible(bool(detail))
        wanted = self.prefs["send_to"]
        for key, action in self._peer_actions.items():
            if action.isCheckable():
                action.setChecked(bool(key) and key == wanted)

    def close(self):
        """Stops sending and unmutes this PC; Beamer calls it on the way out."""
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self.engine.close()
