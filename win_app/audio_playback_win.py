"""Plays a paired Mac's microphone into a virtual cable on this PC, where Windows apps can choose it
as a microphone.

Windows lets no ordinary app add a microphone, so the Mac's arrives through VB-CABLE (VB-Audio,
donationware; installed on its own, once): Beamer plays into "CABLE Input", and apps record from
"CABLE Output". Through the `soundcard` package, as capture is; its player blocks until each block
is queued, so it runs on a thread of its own, pulling from the JitterBuffer.

Part of the local audio experiment only: Beamer's Sound menu (sound_menu_win.py) opens it.
"""

import logging
import threading

from core import audio_stream

LOGGER = logging.getLogger("Beamer")

# The cable's playback end; its recording end is "CABLE Output".
CABLE_WORD = "CABLE Input"
# How much is handed to WASAPI at a time: two packets, 10 ms.
BLOCK_FRAMES = 2 * audio_stream.PACKET_FRAMES


def find_output(word):
    """The full name of the first output whose name holds `word`, or None."""
    import soundcard
    for speaker in soundcard.all_speakers():
        if word.casefold() in speaker.name.casefold():
            return speaker.name
    return None


def output_name(device=None):
    import soundcard
    return (soundcard.get_speaker(device) if device else soundcard.default_speaker()).name


def refresh_devices():
    """Nothing to do: soundcard asks Windows for the devices each time."""


class _Player:
    """Plays `buffer` on `device` from its own thread until `stop()`. `active` goes False if the
    device fails, so the Sound menu can open it again."""

    def __init__(self, buffer, device):
        self._buffer = buffer
        self._device = device
        self._stop = threading.Event()
        self.active = True
        self._thread = threading.Thread(target=self._run, name="audio-playback", daemon=True)
        self._thread.start()

    def _run(self):
        import numpy
        import soundcard
        try:
            speaker = soundcard.get_speaker(self._device) if self._device else soundcard.default_speaker()
            with speaker.player(samplerate=audio_stream.SAMPLE_RATE, channels=audio_stream.CHANNELS,
                                blocksize=BLOCK_FRAMES) as player:
                size = BLOCK_FRAMES * audio_stream.FRAME_BYTES
                while not self._stop.is_set():
                    pcm = self._buffer.pull(size)
                    samples = numpy.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
                    player.play(samples.reshape(-1, audio_stream.CHANNELS))
        except Exception as error:
            LOGGER.warning("audio: playback on %s stopped: %s", self._device, error)
        finally:
            self.active = False

    def stop(self):
        self._stop.set()
        self._thread.join(1.0)

    def close(self):
        pass


def open_output(buffer, device=None):
    """Plays `buffer` on `device` (a name, or None for the default output), started."""
    return _Player(buffer, device)
