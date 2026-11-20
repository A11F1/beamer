"""What this PC is playing, as 48 kHz 16-bit stereo PCM, for the experimental audio stream
(core/audio_stream.py).

WASAPI loopback on the default output device, through the `soundcard` package (BSD-3-Clause,
cffi, needs numpy). Shared-mode WASAPI converts whatever rate and channel count the device mixes at
into what is asked for. Loopback hands over nothing while nothing is playing, so the stream simply
goes quiet then, and the receiver plays silence.

Part of the local audio experiment only: Beamer's Sound menu (sound_menu_win.py) and
tools/audio_share.py import it when asked to send.
"""

import logging
import threading
import time
import warnings

from core import audio_stream

LOGGER = logging.getLogger("Beamer")

# How much is read from WASAPI at a time: two packets, 10 ms, so a read is never far behind.
READ_FRAMES = 2 * audio_stream.PACKET_FRAMES


def default_output_name():
    import soundcard
    return soundcard.default_speaker().name


def output_name(device=None):
    """The full name of the output `device` names (soundcard matches part of a name), or of the
    default output."""
    import soundcard
    return (soundcard.get_speaker(device) if device else soundcard.default_speaker()).name


def peak(pcm):
    """The loudest sample in a block of 16-bit PCM, 0 to 32768."""
    import numpy
    samples = numpy.frombuffer(pcm, dtype="<i2")
    if not samples.size:
        return 0
    return max(int(samples.max()), -int(samples.min()))


def capture(deliver, stop: threading.Event, device=None):
    """Calls `deliver(pcm_bytes)` with what the default output (or the speaker named `device`)
    plays until `stop` is set. If the device goes away (unplugged, or the default changed), it is
    opened again, following the new default."""
    import numpy
    import soundcard

    warnings.filterwarnings("ignore", category=soundcard.SoundcardRuntimeWarning)
    while not stop.is_set():
        try:
            speaker = soundcard.get_speaker(device) if device else soundcard.default_speaker()
            loopback = soundcard.get_microphone(id=str(speaker.name), include_loopback=True)
            LOGGER.info("audio: capturing %s", speaker.name)
            with loopback.recorder(samplerate=audio_stream.SAMPLE_RATE, channels=audio_stream.CHANNELS,
                                   blocksize=READ_FRAMES) as recorder:
                current = speaker.name
                checked = time.monotonic()
                while not stop.is_set():
                    data = recorder.record(numframes=READ_FRAMES)
                    pcm = (numpy.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                    deliver(pcm)
                    if device is None and time.monotonic() - checked > 2.0:
                        checked = time.monotonic()
                        if soundcard.default_speaker().name != current:
                            LOGGER.info("audio: default output changed, following it")
                            break
        except Exception as error:
            LOGGER.warning("audio: capture stopped (%s); trying again", error)
            stop.wait(1.0)


def microphone_name(device=None):
    """The full name of the microphone `device` names, or of the default microphone."""
    import soundcard
    return (soundcard.get_microphone(device) if device else soundcard.default_microphone()).name


def capture_microphone(deliver, stop: threading.Event, device=None):
    """Calls `deliver(pcm_bytes)` with what the default microphone (or the one named `device`)
    hears until `stop` is set, as 48 kHz 16-bit stereo: a headset's one channel on both sides. If
    the microphone goes away, or the default changes, it is opened again.

    Windows hands a desktop app silence if microphone access is off for desktop apps (Settings,
    Privacy & security, Microphone); nothing here can tell that from a quiet room."""
    import numpy
    import soundcard

    warnings.filterwarnings("ignore", category=soundcard.SoundcardRuntimeWarning)
    while not stop.is_set():
        try:
            microphone = soundcard.get_microphone(device) if device else soundcard.default_microphone()
            LOGGER.info("audio: capturing the microphone %s", microphone.name)
            with microphone.recorder(samplerate=audio_stream.SAMPLE_RATE, channels=1,
                                     blocksize=READ_FRAMES) as recorder:
                current = microphone.name
                checked = time.monotonic()
                while not stop.is_set():
                    data = recorder.record(numframes=READ_FRAMES)
                    mono = numpy.clip(data[:, 0], -1.0, 1.0)
                    stereo = numpy.repeat(mono, audio_stream.CHANNELS)
                    deliver((stereo * 32767.0).astype("<i2").tobytes())
                    if device is None and time.monotonic() - checked > 2.0:
                        checked = time.monotonic()
                        if soundcard.default_microphone().name != current:
                            LOGGER.info("audio: default microphone changed, following it")
                            break
        except Exception as error:
            LOGGER.warning("audio: microphone capture stopped (%s); trying again", error)
            stop.wait(1.0)
