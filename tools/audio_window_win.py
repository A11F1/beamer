"""Beamer Audio for Windows: a small window and tray icon that send this PC's sound to a paired Mac.

    .venv-audio\\Scripts\\pythonw tools\\audio_window_win.py

Runs beside the official Beamer and reads its pairing; it never writes Beamer's settings. Its own
choices (where to send, what to capture, whether to mute this PC) are kept in audio.json next to
Beamer's settings.json. See docs/audio-experiment.md.

Windows' audio calls (soundcard's WASAPI, pycaw's mute) need COM set up multithreaded, and Qt sets
up its own thread single-threaded for drag and drop, so every one of them runs on one worker
thread of their own and the window only ever reads what that thread last saw.
"""

import argparse
import collections
import concurrent.futures
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QAction, QIcon, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QMenu,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)

import audio_service  # noqa: E402
from audio_service import PairingError, audio_stream  # noqa: E402

LOGGER = logging.getLogger("Beamer")

DEFAULTS = {"send_to": "", "last_send_to": "", "device": None, "mute_local": True, "told_about_tray": False}
TONE = "tone"
OFF, WAITING, STREAMING, PROBLEM = "off", "waiting", "streaming", "problem"
DOTS = {OFF: "#8a8f98", WAITING: "#d39b1e", STREAMING: "#2fa55a", PROBLEM: "#d0453a"}
ICON = audio_service.ROOT / "Beamer.ico"


def _com_ready():
    """The worker thread's start: COM multithreaded, as soundcard sets it and comtypes is told to."""
    if sys.platform == "win32":
        sys.coinit_flags = 0x0
        import soundcard  # noqa: F401  (initialises COM on this thread)


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
        with self._lock:
            self._notices.append(text)

    def apply(self, peer_key, device, mute_local):
        """Send to the peer remembered as `peer_key` (empty: stop), capturing `device` (None for the
        default output, TONE for the test tone)."""
        wanted = (peer_key, device, mute_local)
        self._wanted = wanted
        self._worker.submit(self._apply, wanted)

    def _apply(self, wanted):
        if wanted != self._wanted:
            return
        self._stop_session()
        peer_key, device, mute_local = wanted
        with self._lock:
            self._problem = None
            self._notices.clear()
        if not peer_key:
            return
        try:
            peers = audio_service.paired_peers(self.settings_path)
            peer = audio_service.find_peer(peers, peer_key)
            if peer is None:
                raise PairingError("That machine is no longer paired with this PC in Beamer.")
            source = audio_service.SOURCE_TONE if device == TONE else audio_service.SOURCE_LOOPBACK
            session = audio_service.SendSession(peer, peers, source=source,
                                                device=None if device == TONE else device,
                                                mute_local=mute_local and source == audio_service.SOURCE_LOOPBACK,
                                                notice=self._notice)
            session.start()
        except Exception as error:
            with self._lock:
                self._problem = str(error)
            return
        with self._lock:
            self._session = session

    def _stop_session(self):
        with self._lock:
            session, self._session = self._session, None
        if session:
            session.stop()

    def tick(self):
        self._worker.submit(self._tick)

    def _tick(self):
        with self._lock:
            session = self._session
        if session:
            session.tick()

    def snapshot(self):
        with self._lock:
            session, problem = self._session, self._problem
            notice = self._notices[-1] if self._notices else None
        if problem:
            return PROBLEM, problem, None
        if not session:
            return (OFF, "Not sending.", None) if not (self._wanted and self._wanted[0]) else (WAITING, "Starting…", None)
        if session.streaming:
            source = f"Capturing {session.output}." if session.output else "Sending a test tone."
            detail = f"{session.sent:,} packets sent. {source}"
            return STREAMING, f"Playing on {session.name}", notice or detail
        return WAITING, f"Waiting for {session.name}…", notice or (
            f"Start Beamer Audio on {session.name} and turn on Receive sound there.")

    def devices(self):
        """The output devices, as a Future of a list of names, or an empty list off Windows."""
        return self._worker.submit(lambda: audio_service.output_devices() if sys.platform == "win32" else [])

    def close(self, timeout=4.0):
        """Stops sending and puts this PC's output back before returning."""
        self._wanted = None
        future = self._worker.submit(self._stop_session)
        try:
            future.result(timeout)
        except Exception as error:
            LOGGER.warning("audio: did not stop cleanly: %s", error)
        self._worker.shutdown(wait=False)


class AudioWindow(QWidget):
    def __init__(self, settings_path):
        super().__init__()
        self.settings_path = settings_path
        self.prefs = audio_service.Preferences(audio_service.preferences_path(settings_path), DEFAULTS)
        self.engine = Engine(settings_path)
        self.peers = []
        self._quitting = False
        self.setWindowTitle("Beamer Audio")
        if ICON.exists():
            self.setWindowIcon(QIcon(str(ICON)))
        self._build()
        self._load_peers()
        self._load_devices()
        self._build_tray()
        self._timer = QTimer(self, interval=250, timeout=self._tick)
        self._timer.start()
        self._peer_timer = QTimer(self, interval=10_000, timeout=self._load_peers)
        self._peer_timer.start()
        self._apply()

    # Layout -------------------------------------------------------------------------------

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        heading = QLabel("Send this PC's sound")
        font = heading.font()
        font.setPointSizeF(font.pointSizeF() * 1.35)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        layout.addWidget(self._quiet("Plays on a paired Mac running Beamer Audio, over your local network."))

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.target = QComboBox()
        self.target.setAccessibleName("Play on")
        self.target.currentIndexChanged.connect(self._target_chosen)
        form.addRow("Play on", self.target)
        self.capture = QComboBox()
        self.capture.setAccessibleName("Capture")
        self.capture.currentIndexChanged.connect(self._capture_chosen)
        form.addRow("Capture", self.capture)
        layout.addLayout(form)

        self.mute = QCheckBox("Mute this PC while its sound plays elsewhere")
        self.mute.setChecked(bool(self.prefs["mute_local"]))
        self.mute.toggled.connect(self._mute_chosen)
        layout.addWidget(self.mute)
        layout.addWidget(self._quiet("Unmuted again when sending stops or Beamer Audio quits."))

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        status = QHBoxLayout()
        status.setSpacing(8)
        self.dot = QLabel("●")
        self.dot.setAccessibleName("Status")
        status.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.status = QLabel()
        bold = self.status.font()
        bold.setBold(True)
        self.status.setFont(bold)
        self.detail = self._quiet("")
        texts.addWidget(self.status)
        texts.addWidget(self.detail)
        status.addLayout(texts, 1)
        layout.addLayout(status)
        layout.addStretch(1)
        self.setMinimumWidth(420)

    def _quiet(self, text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        return label

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        menu.addAction(QAction("Open Beamer Audio", menu, triggered=self._show))
        self.toggle_action = QAction("Stop sending", menu, triggered=self._toggle_sending)
        menu.addAction(self.toggle_action)
        menu.addSeparator()
        menu.addAction(QAction("Quit Beamer Audio", menu, triggered=self.quit))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._show() if reason in (
            QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
        self._tray_menu = menu
        self.tray.show()

    # Choices ------------------------------------------------------------------------------

    def _load_peers(self):
        try:
            peers = audio_service.paired_peers(self.settings_path)
        except PairingError:
            peers = []
        shape = [(audio_service.peer_key(entry), audio_service.label(entry, peers)) for entry in peers]
        if shape == [(audio_service.peer_key(entry), audio_service.label(entry, self.peers)) for entry in self.peers] \
                and self.target.count():
            return
        self.peers = peers
        self.target.blockSignals(True)
        self.target.clear()
        self.target.addItem("Off — not sending", "")
        for key, name in shape:
            platform = next(entry.get("platform") for entry in peers if audio_service.peer_key(entry) == key)
            self.target.addItem(f"{name}" + ("" if platform == "macos" else f"  ({platform})"), key)
        wanted = self.prefs["send_to"]
        index = self.target.findData(wanted)
        self.target.setCurrentIndex(index if index >= 0 else 0)
        self.target.blockSignals(False)

    def _load_devices(self):
        future = self.engine.devices()
        try:
            names = future.result(5.0)
        except Exception:
            names = []
        self.capture.blockSignals(True)
        self.capture.clear()
        self.capture.addItem("Default output", None)
        for name in names:
            self.capture.addItem(name, name)
        self.capture.addItem("Test tone (440 Hz)", TONE)
        index = self.capture.findData(self.prefs["device"])
        self.capture.setCurrentIndex(index if index >= 0 else 0)
        self.capture.blockSignals(False)

    def _target_chosen(self, _index):
        key = self.target.currentData() or ""
        self.prefs.set(send_to=key, **({"last_send_to": key} if key else {}))
        self._apply()

    def _capture_chosen(self, _index):
        self.prefs.set(device=self.capture.currentData())
        self._apply()

    def _mute_chosen(self, on):
        self.prefs.set(mute_local=bool(on))
        self._apply()

    def _toggle_sending(self):
        if self.prefs["send_to"]:
            self.target.setCurrentIndex(0)
            return
        index = self.target.findData(self.prefs["last_send_to"])
        if index < 1 and self.target.count() > 1:
            index = 1
        if index >= 1:
            self.target.setCurrentIndex(index)
        else:
            self._show()

    def _apply(self):
        self.engine.apply(self.prefs["send_to"], self.prefs["device"], bool(self.prefs["mute_local"]))
        self.mute.setEnabled(self.prefs["device"] != TONE)

    # Every quarter second -----------------------------------------------------------------

    def _tick(self):
        self.engine.tick()
        state, text, detail = self.engine.snapshot()
        self.dot.setStyleSheet(f"color: {DOTS[state]};")
        self.status.setText(text)
        self.detail.setText(detail or "")
        self.detail.setVisible(bool(detail))
        if self.tray:
            self.tray.setToolTip(f"Beamer Audio — {text}")
            self.toggle_action.setText("Stop sending" if self.prefs["send_to"] else "Start sending")

    # Closing ------------------------------------------------------------------------------

    def _show(self):
        self._load_peers()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self._quitting or not self.tray:
            self.quit()
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self.prefs["told_about_tray"]:
            self.tray.showMessage("Beamer Audio", "Still running here. Quit it from this icon's menu.",
                                  QSystemTrayIcon.MessageIcon.Information, 4000)
            self.prefs.set(told_about_tray=True)

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        self._timer.stop()
        self._peer_timer.stop()
        self.engine.close()
        if self.tray:
            self.tray.hide()
        QApplication.instance().quit()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Beamer Audio: send this PC's sound to a paired Mac.")
    parser.add_argument("--settings", default=audio_service.default_settings_path(),
                        help="Beamer's settings.json (default: this user's)")
    parser.add_argument("--hidden", action="store_true", help="start in the tray, without the window")
    args = parser.parse_args(argv)
    audio_service.setup_logging(args.settings)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Beamer Audio")
    app.setQuitOnLastWindowClosed(False)
    window = AudioWindow(args.settings)
    app.aboutToQuit.connect(window.quit)
    if not (args.hidden and window.tray):
        window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
