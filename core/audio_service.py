"""The audio experiment's moving parts, shared by Beamer's Sound menu on each platform
(mac_app/sound_menu.py, win_app/sound_menu_win.py) and the command line (tools/audio_share.py):
Beamer's pairing, the experiment's own preferences, and a sending and a receiving session that
start and stop cleanly.

Nothing here writes Beamer's settings.json; the experiment keeps its preferences beside it in
audio.json, so a release build of Beamer never sees them. See docs/audio-experiment.md.
"""

import json
import math
import os
import struct
import sys
import threading
import time
from pathlib import Path

from core import audio_stream, peerlist, protocol
from core.i18n import N_, tr

# The platform modules (audio_capture_win, audio_local_mute, audio_playback_mac) are imported by
# their top-level names, as they are inside a built app; run from source, the caller puts win_app/
# or mac_app/ on sys.path. Nothing here reads __file__: py2app would then copy all of core/, tests
# and all, into the Mac app.

SOURCE_LOOPBACK = "loopback"
SOURCE_TONE = "tone"
SOURCE_MIC = "mic"


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


MUTE_WORDS = {
    "muted": N_("Muted {name} while its sound plays on the other machine."),
    "unmuted": N_("Unmuted {name}."),
    "recovered": N_("Unmuted {name}, left muted by an earlier run."),
    "gave_up": N_("Muting {name} silences the stream too on this device, so it stays unmuted. "
                  "Turn it down on the headset itself instead."),
}


# Sending ------------------------------------------------------------------------------------

class SendSession:
    """Streams this machine's sound, its microphone (SOURCE_MIC) or a test tone to one peer until
    `stop()`. A microphone goes as its own kind of stream (audio_stream.MIC), to the peer's
    microphone receiver rather than its speakers.

    With `mute_local`, this PC's own output is muted while the peer is playing. Windows' audio
    calls need a thread whose COM is set up for them, so the session never makes them itself:
    whoever owns that thread calls `tick()` every quarter second and `stop()` from it too (the
    command line's main thread; the window's COM worker). `notice(text)` is told what the mute did.
    """

    def __init__(self, peer, peers, *, host=None, port=None, source=SOURCE_LOOPBACK,
                 device=None, mute_local=False, notice=None):
        self.name = label(peer, peers)
        self.host = host or peer.get("host")
        if not self.host:
            raise PairingError(f"Beamer has no address for {self.name}.")
        stream_port, salt = audio_stream.MIC if source == SOURCE_MIC else audio_stream.SOUND
        self.port = stream_port if port is None else port
        self.source = source
        self.notice = notice or (lambda text: None)
        self._stop = threading.Event()
        self._mute = None
        self.output = None
        if source == SOURCE_LOOPBACK and sys.platform == "darwin":
            # What this Mac plays reaches BlackHole 16ch once the Sound menu has made it the default
            # output; captured from there, as a stream its owner opens and closes.
            import audio_capture_mac
            self._capture = audio_capture_mac
            self.output = audio_capture_mac.find_input(audio_capture_mac.SOUND_DEVICE)
            if self.output is None:
                raise PairingError("BlackHole 16ch is not installed.")
        elif source == SOURCE_LOOPBACK:
            if sys.platform != "win32":
                raise PairingError("Capturing this machine's sound works on Windows and macOS only; use the test tone.")
            import audio_capture_win
            self._capture = audio_capture_win
            self.output = audio_capture_win.output_name(device)
            if mute_local:
                self._mute = self._make_mute(self.output)
        elif source == SOURCE_MIC and sys.platform == "win32":
            import audio_capture_win
            self._capture = audio_capture_win
            self.output = audio_capture_win.microphone_name(device)
        elif source == SOURCE_MIC and sys.platform == "darwin":
            # A PortAudio input stream, opened and closed by its owner (open_stream, close_stream)
            # around PortAudio restarts, rather than a thread of its own.
            import audio_capture_mac
            self._capture = audio_capture_mac
            self.output = audio_capture_mac.input_name(device)
            if self.output is None:
                raise PairingError("This Mac has no microphone to send.")
        elif source == SOURCE_MIC:
            raise PairingError("Sending this machine's microphone works on Windows and macOS only.")
        self._device = device
        self.sender = audio_stream.AudioSender(peer["token"], self.host, self.port, salt=salt)
        self._thread = None
        self.input = None

    def _make_mute(self, output):
        import audio_local_mute
        try:
            endpoint = audio_local_mute.WindowsEndpoint(output)
        except Exception as error:
            self.notice(tr("This PC's own output stays on: its mute could not be reached ({error}).").format(error=error))
            return None
        return audio_local_mute.LocalMute(
            endpoint, marker=audio_local_mute.marker_path(),
            notice=lambda event, name: self.notice(tr(MUTE_WORDS[event]).format(name=name)))

    @property
    def what(self):
        if self.source == SOURCE_TONE:
            return "a 440 Hz test tone"
        if self.source == SOURCE_MIC:
            return f"what {self.output} hears"
        return f"what {self.output} plays"

    @property
    def streaming(self):
        return self.sender.streaming

    @property
    def sent(self):
        return self.sender.sent

    @property
    def _stream_based(self):
        return self.source in (SOURCE_MIC, SOURCE_LOOPBACK) and sys.platform == "darwin"

    def open_stream(self):
        """The Mac's capture stream, opened; for a session whose capture is a PortAudio stream."""
        if self._stream_based and self.input is None:
            self.input = self._capture.open_input(self.sender.send, self.output,
                                                  stereo=self.source == SOURCE_LOOPBACK)

    def close_stream(self):
        stream, self.input = self.input, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def start(self):
        self.sender.start()
        if self._stream_based:
            self.open_stream()
            return self
        if self.source == SOURCE_TONE:
            target, args = tone_source, (self.sender.send, self._stop)
        else:
            mute, capture = self._mute, self._capture

            def deliver(pcm):
                self.sender.send(pcm)
                if mute:
                    mute.heard(capture.peak(pcm))

            reader = capture.capture_microphone if self.source == SOURCE_MIC else capture.capture
            target, args = reader, (deliver, self._stop, self._device)
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
        self.close_stream()
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
    """Plays what the allowed peers send until `stop()`. `peers` maps token to the name to show.
    `stream` is audio_stream.SOUND (a machine's output) or audio_stream.MIC (its microphone), and
    `device` the output to play on: None for the default, or a name such as "BlackHole 2ch"."""

    def __init__(self, peers, *, port=None, device=None, latency_ms=40.0, on_change=None,
                 stream=audio_stream.SOUND):
        target = max(1, round(latency_ms / 1000 / audio_stream.PACKET_SECONDS))
        self.latency_ms = target * audio_stream.PACKET_SECONDS * 1000
        self.buffer = audio_stream.JitterBuffer(target=target, limit=max(target * 4, target + 20))
        stream_port, salt = stream
        self.receiver = audio_stream.AudioReceiver(peers, self.buffer, port=stream_port if port is None else port,
                                                   on_change=on_change, salt=salt)
        self.port = self.receiver.port
        if sys.platform == "win32":
            import audio_playback_win as playback
        else:
            import audio_playback_mac as playback
        self._playback = playback
        self.device = device
        try:
            self.output = playback.open_output(self.buffer, device)
            self.output_name = playback.output_name(device)
        except Exception:
            self.receiver.stop()
            raise
        self.receiver.start()

    def close_output(self):
        """Stops playing, while the receiver keeps running and filling the buffer."""
        old, self.output = self.output, None
        if old is not None:
            try:
                old.stop()
                old.close()
            except Exception:
                pass

    def open_output(self):
        """Plays again, on the output as PortAudio now sees it."""
        self.output = self._playback.open_output(self.buffer, self.device)
        self.output_name = self._playback.output_name(self.device)

    def reopen_output(self):
        """Plays on the output as it is now, such as headphones just plugged in, while the
        receiver keeps running: the sender never notices, and the buffer carries on. Only for a
        process with no other PortAudio stream open: restarting PortAudio closes them all."""
        self.close_output()
        self._playback.refresh_devices()
        self.open_output()

    @property
    def playing(self):
        return self.receiver.core.playing

    def stats(self):
        core, jitter = self.receiver.core.stats, self.buffer.stats
        return {"packets": core["packets"], "buffer": self.buffer.depth, "rejected": core["rejected"], **jitter}

    def stop(self):
        try:
            if self.output is not None:
                self.output.stop()
                self.output.close()
        finally:
            self.receiver.stop()
