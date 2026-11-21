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
            # 10 ms a call, with 30 ms of room in the device: the callback is Python, sharing the
            # GIL with the network thread and the menu bar, and a callback that misses its turn
            # clicks without ever showing in the buffer's counts.
            blocksize=2 * audio_stream.PACKET_FRAMES,
            latency=0.03,
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


_core_foundation = None
_UTF8 = 0x08000100


def _audio():
    global _core_audio, _core_foundation
    if _core_audio is None:
        _core_audio = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    if _core_foundation is None:
        _core_foundation = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        _core_foundation.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        _core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    return _core_audio, _core_foundation


def _device_name(device_id):
    core_audio, core_foundation = _audio()
    address = _PropertyAddress(_code("lnam"), _code("glob"), 0)
    ref, size = ctypes.c_void_p(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    if core_audio.AudioObjectGetPropertyData(device_id, ctypes.byref(address), 0, None,
                                             ctypes.byref(size), ctypes.byref(ref)) != 0 or not ref.value:
        return None
    try:
        text = ctypes.create_string_buffer(512)
        if not core_foundation.CFStringGetCString(ref, text, len(text), _UTF8):
            return None
        return text.value.decode("utf-8")
    finally:
        core_foundation.CFRelease(ref)


def devices():
    """Every audio device Core Audio knows, as (id, name)."""
    core_audio, _ = _audio()
    address = _PropertyAddress(_code("dev#"), _code("glob"), 0)
    size = ctypes.c_uint32(0)
    if core_audio.AudioObjectGetPropertyDataSize(1, ctypes.byref(address), 0, None, ctypes.byref(size)) != 0:
        return []
    ids = (ctypes.c_uint32 * (size.value // ctypes.sizeof(ctypes.c_uint32)))()
    if core_audio.AudioObjectGetPropertyData(1, ctypes.byref(address), 0, None, ctypes.byref(size), ids) != 0:
        return []
    return [(device_id, _device_name(device_id)) for device_id in ids]


def default_output_name():
    current = default_output_id()
    return _device_name(current) if current is not None else None


def set_default_output(name):
    """Makes the device called `name` the Mac's default output, as choosing it in Control Centre
    does. True when it did."""
    core_audio, _ = _audio()
    match = next((device_id for device_id, device_name in devices() if device_name == name), None)
    if match is None:
        return False
    address = _PropertyAddress(_code("dOut"), _code("glob"), 0)
    value = ctypes.c_uint32(match)
    return core_audio.AudioObjectSetPropertyData(
        1, ctypes.byref(address), 0, None, ctypes.sizeof(value), ctypes.byref(value)) == 0
