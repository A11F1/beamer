"""The audio experiment's moving parts, shared by the command line (audio_share.py) and the two
companion apps (audio_menu_mac.py, audio_window_win.py): Beamer's pairing, the companion's own
preferences, and a sending and a receiving session that start and stop cleanly.

Nothing here writes Beamer's settings.json; the companions keep their preferences beside it in
audio.json, so the official Beamer never sees them.
"""

import json
import math
import os
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import audio_stream, peerlist, protocol  # noqa: E402

SOURCE_LOOPBACK = "loopback"
SOURCE_TONE = "tone"


# Beamer's pairing ---------------------------------------------------------------------------

def default_settings_path():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Beamer" / "settings.json"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "Beamer" / "settings.json"
    return Path.home() / ".config" / "Beamer" / "settings.json"


class PairingError(Exception):
    """Something about Beamer's pairing the user has to fix, in words to show them."""


def paired_peers(path):
    """The peers that may stream: paired by Beamer 1.5 or later."""
    try:
        settings = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PairingError(f"Could not read Beamer's settings at {path}: {error}")
    peers = [entry for entry in settings.get("peers", []) if protocol.linkable(entry)]
    if not peers:
        raise PairingError("No machine is paired with Beamer 1.5 or later. Pair them in Beamer first.")
    return peers


def label(entry, peers):
    return peerlist.labels(peers).get(entry["token"]) or entry.get("name") or peerlist.UNNAMED


def peer_key(entry):
    """What the companions remember a peer by: its machine id, which survives renaming."""
    return entry.get("id") or entry["token"][:8]


def find_peer(peers, key):
    return next((entry for entry in peers if peer_key(entry) == key), None)


def choose_peer(peers, wanted=None):
    """The peer named `wanted`, or the one obvious receiver when nothing is named."""
    if wanted:
        folded = wanted.casefold()
        matches = [entry for entry in peers
                   if folded in (label(entry, peers).casefold(), str(entry.get("name", "")).casefold())]
        if not matches:
            names = ", ".join(label(entry, peers) for entry in peers)
            raise PairingError(f"No paired machine is called {wanted!r}. Paired: {names}.")
        return matches[0]
    macs = [entry for entry in peers if entry.get("platform") == "macos" and entry.get("host")]
    if len(macs) == 1:
        return macs[0]
    reachable = [entry for entry in peers if entry.get("host")]
    if len(reachable) == 1:
        return reachable[0]
    names = ", ".join(label(entry, peers) for entry in peers)
    raise PairingError(f"Several machines are paired ({names}); name one.")


# The companions' own preferences ------------------------------------------------------------

class Preferences:
    """A small JSON file beside Beamer's settings.json. Unreadable or missing means the defaults;
    each change is written at once, through a temporary file so a crash never leaves half of one."""

    def __init__(self, path, defaults):
        self.path = Path(path)
        self.values = dict(defaults)
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.values.update({key: value for key, value in saved.items() if key in defaults})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.values[key]

    def set(self, **changes):
        self.values.update(changes)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass


def preferences_path(settings_path):
    return Path(settings_path).with_name("audio.json")


def setup_logging(settings_path):
    """Logs to the console, or with none (pythonw, a launcher) to audio.log beside the preferences,
    uncaught errors included, so a companion that fails to start says why somewhere."""
    import logging
    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return
    path = Path(settings_path).with_name("audio.log")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, filename=str(path), encoding="utf-8",
                            format="%(asctime)s %(levelname)s %(message)s")
    except (OSError, TypeError):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return
    logger = logging.getLogger("Beamer")

    def log_uncaught(kind, value, trace):
        logger.error("audio: stopped by an error", exc_info=(kind, value, trace))

    sys.excepthook = log_uncaught
    threading.excepthook = lambda args: log_uncaught(args.exc_type, args.exc_value, args.exc_traceback)


# Sources ------------------------------------------------------------------------------------

def tone_source(deliver, stop, frequency=440.0):
    """A soft sine tone, one packet at a time, paced against the clock as a sound card would be."""
    step = 2 * math.pi * frequency / audio_stream.SAMPLE_RATE
    phase = 0.0
    started = time.monotonic()
    sent = 0
    while not stop.is_set():
        frames = []
        for _ in range(audio_stream.PACKET_FRAMES):
            value = int(0.2 * 32767 * math.sin(phase))
            frames.append(struct.pack("<hh", value, value))
            phase = (phase + step) % (2 * math.pi)
        deliver(b"".join(frames))
        sent += 1
        delay = started + sent * audio_stream.PACKET_SECONDS - time.monotonic()
        if delay > 0:
            stop.wait(delay)


def _win_app():
    path = str(ROOT / "win_app")
    if path not in sys.path:
        sys.path.insert(0, path)


def _mac_app():
    path = str(ROOT / "mac_app")
    if path not in sys.path:
        sys.path.insert(0, path)


MUTE_WORDS = {
    "muted": "Muted {name} while its sound plays on the other machine.",
    "unmuted": "Unmuted {name}.",
    "recovered": "Unmuted {name}, left muted by an earlier run.",
    "gave_up": ("Muting {name} silences the stream too on this device, so it stays unmuted. "
                "Turn it down on the headset itself instead."),
}


# Sending ------------------------------------------------------------------------------------

class SendSession:
    """Streams this machine's sound (or a test tone) to one peer until `stop()`.

    With `mute_local`, this PC's own output is muted while the peer is playing. Windows' audio
    calls need a thread whose COM is set up for them, so the session never makes them itself:
    whoever owns that thread calls `tick()` every quarter second and `stop()` from it too (the
    command line's main thread; the window's COM worker). `notice(text)` is told what the mute did.
    """

    def __init__(self, peer, peers, *, host=None, port=audio_stream.AUDIO_PORT, source=SOURCE_LOOPBACK,
                 device=None, mute_local=False, notice=None):
        self.name = label(peer, peers)
        self.host = host or peer.get("host")
        if not self.host:
            raise PairingError(f"Beamer has no address for {self.name}.")
        self.port = port
        self.source = source
        self.notice = notice or (lambda text: None)
        self._stop = threading.Event()
        self._mute = None
        self.output = None
        if source == SOURCE_LOOPBACK:
            if sys.platform != "win32":
                raise PairingError("Capturing this machine's sound works on Windows only; use the test tone.")
            _win_app()
            import audio_capture_win
            self._capture = audio_capture_win
            self.output = audio_capture_win.output_name(device)
            if mute_local:
                self._mute = self._make_mute(self.output)
        self._device = device
        self.sender = audio_stream.AudioSender(peer["token"], self.host, port)
        self._thread = None

    def _make_mute(self, output):
        _win_app()
        import audio_local_mute
        try:
            endpoint = audio_local_mute.WindowsEndpoint(output)
        except Exception as error:
            self.notice(f"This PC's own output stays on: its mute could not be reached ({error}).")
            return None
        return audio_local_mute.LocalMute(
            endpoint, marker=audio_local_mute.marker_path(),
            notice=lambda event, name: self.notice(MUTE_WORDS[event].format(name=name)))

    @property
    def what(self):
        return "a 440 Hz test tone" if self.source == SOURCE_TONE else f"what {self.output} plays"

    @property
    def streaming(self):
        return self.sender.streaming

    @property
    def sent(self):
        return self.sender.sent

    def start(self):
        self.sender.start()
        if self.source == SOURCE_TONE:
            target, args = tone_source, (self.sender.send, self._stop)
        else:
            mute, capture = self._mute, self._capture

            def deliver(pcm):
                self.sender.send(pcm)
                if mute:
                    mute.heard(capture.peak(pcm))

            target, args = capture.capture, (deliver, self._stop, self._device)
        self._thread = threading.Thread(target=target, args=args, name="audio-source", daemon=True)
        self._thread.start()
        return self

    def tick(self):
        if self._mute:
            self._mute.streaming(self.sender.streaming)

    def restore(self):
        """Puts this PC's output back; safe from any thread with COM set up, and more than once."""
        if self._mute:
            self._mute.restore()

    def stop(self):
        self._stop.set()
        self.restore()
        self.sender.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(1.5)


def output_devices():
    """The output devices this machine can capture (Windows) or play on (Mac), by name."""
    if sys.platform == "win32":
        import soundcard
        return [speaker.name for speaker in soundcard.all_speakers()]
    if sys.platform == "darwin":
        import sounddevice
        return [device["name"] for device in sounddevice.query_devices() if device["max_output_channels"] > 0]
    return []


# Receiving ----------------------------------------------------------------------------------

class ReceiveSession:
    """Plays what the allowed peers send until `stop()`. `peers` maps token to the name to show."""

    def __init__(self, peers, *, port=audio_stream.AUDIO_PORT, device=None, latency_ms=40.0, on_change=None):
        target = max(1, round(latency_ms / 1000 / audio_stream.PACKET_SECONDS))
        self.latency_ms = target * audio_stream.PACKET_SECONDS * 1000
        self.buffer = audio_stream.JitterBuffer(target=target, limit=max(target * 4, target + 20))
        self.receiver = audio_stream.AudioReceiver(peers, self.buffer, port=port, on_change=on_change)
        self.port = self.receiver.port
        _mac_app()
        import audio_playback_mac
        try:
            self.output = audio_playback_mac.open_output(self.buffer, device)
            self.output_name = audio_playback_mac.output_name(device)
        except Exception:
            self.receiver.stop()
            raise
        self.receiver.start()

    @property
    def playing(self):
        return self.receiver.core.playing

    def stats(self):
        core, jitter = self.receiver.core.stats, self.buffer.stats
        return {"packets": core["packets"], "buffer": self.buffer.depth, "rejected": core["rejected"], **jitter}

    def stop(self):
        try:
            self.output.stop()
            self.output.close()
        finally:
            self.receiver.stop()
