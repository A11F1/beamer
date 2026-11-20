"""Plays the experimental audio stream (core/audio_stream.py) on this Mac's default output.

Through the `sounddevice` package (MIT), whose macOS wheel carries its own PortAudio, so nothing
else is installed. The device callback pulls from the JitterBuffer and never waits on the network;
Core Audio converts 48 kHz to whatever rate the output runs at.

Part of the local audio experiment only: Beamer's Sound menu (sound_menu.py) and
tools/audio_share.py import it when asked to receive.
"""

import ctypes

from core import audio_stream


def open_output(buffer, device=None):
    """An output stream that plays `buffer`, started; close it with `.stop()` and `.close()`.

    PortAudio reads the Mac's devices once, when it starts, and never again: after an output comes
    or goes (headphones, a display, waking from sleep) its list is stale, and opening fails with an
    internal error (-9986). Restarting it (`refresh_devices`) frees every stream it has open, and
    touching a freed stream can crash, so this never restarts it itself: its callers close every
    stream they hold, restart PortAudio once, and open them again (the Sound menu's
    `_reopen_streams`)."""
    import sounddevice

    def callback(outdata, frames, timing, status):
        outdata[:] = buffer.pull(len(outdata))

    def started():
        stream = sounddevice.RawOutputStream(
            samplerate=audio_stream.SAMPLE_RATE,
            channels=audio_stream.CHANNELS,
            dtype="int16",
            blocksize=audio_stream.PACKET_FRAMES,
            latency="low",
            device=device,
            callback=callback,
        )
        stream.start()
        return stream

    return started()


def refresh_devices():
    """Starts PortAudio afresh, so it sees the outputs as they are now."""
    import sounddevice
    sounddevice._terminate()
    sounddevice._initialize()


def find_output(word):
    """The name of the first output whose name holds `word`, such as "BlackHole", or None."""
    import sounddevice
    for device in sounddevice.query_devices():
        if device["max_output_channels"] > 0 and word.casefold() in device["name"].casefold():
            return device["name"]
    return None


def output_name(device=None):
    import sounddevice
    return sounddevice.query_devices(device, kind="output")["name"]


class _PropertyAddress(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


def _code(text):
    return int.from_bytes(text.encode("ascii"), "big")


_core_audio = None


def default_output_id():
    """The Mac's default output device as Core Audio numbers it, or None if it cannot say.

    Read straight from Core Audio, because PortAudio's idea of the default is fixed when it starts:
    plugging headphones into the jack makes External Headphones the default, and only this sees it."""
    global _core_audio
    try:
        if _core_audio is None:
            _core_audio = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        address = _PropertyAddress(_code("dOut"), _code("glob"), 0)
        device, size = ctypes.c_uint32(0), ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
        status = _core_audio.AudioObjectGetPropertyData(
            1, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(device))
    except OSError:
        return None
    return device.value if status == 0 else None
