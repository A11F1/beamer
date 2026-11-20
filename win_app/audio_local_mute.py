"""Mutes this PC's own output while its sound plays on another machine (the audio experiment,
docs/audio-experiment.md), and puts it back after.

Whether loopback still hears a muted output depends on the device: a USB headset's mute is usually
in its own hardware, after the point loopback reads from, so the stream carries on; a driver that
mutes in software hands loopback silence too, and muting would silence the stream. So after muting
it watches: sound in the moment before the mute and nothing but digital zero for two seconds after
means this device cannot be muted that way, and it unmutes and says so.

Never fights the user: an output already muted is left muted after, and one unmuted by hand while
streaming is not muted again until the next stream. A marker file records a mute this made, so one
left behind by a closed window or a crash is undone the next time the sender starts.

`LocalMute` is pure and tested with a fake; `WindowsEndpoint` is the pycaw adapter.
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

LOGGER = logging.getLogger("Beamer")

# Sound this soon before the mute, then digital zero this long after, means the mute reached loopback.
LOUD_BEFORE_SECONDS = 0.5
SILENT_AFTER_SECONDS = 2.0
# Blocks captured just before the mute can still arrive just after it.
SILENCE_STARTS_WITHIN = 0.5
# A 16-bit peak above this is sound, not noise.
LOUD_PEAK = 64

COINIT_MULTITHREADED = 0x0

MUTED = "muted"
UNMUTED = "unmuted"
RECOVERED = "recovered"
GAVE_UP = "gave_up"


class LocalMute:
    """`endpoint` has `name`, `muted()` and `set_muted(bool)`. Call `streaming(bool)` whenever the
    sender's state may have changed, `heard(peak, now)` with each captured block's peak sample, and
    `restore()` on the way out. `notice(event, name)` is told what happened to the output called
    `name`, one of MUTED, UNMUTED, RECOVERED (unmuted, left muted by an earlier run) and GAVE_UP
    (unmuted for good, as muting silences the stream too); the caller words it."""

    def __init__(self, endpoint, marker=None, notice=None, clock=time.monotonic):
        self._endpoint = endpoint
        self._marker = Path(marker) if marker else None
        self._notice = notice or (lambda event, name: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._mine = False
        self._muted_at = None
        self._loud_at = None
        self._silent_since = None
        self._given_up = False
        self._recover()

    def _recover(self):
        if not self._marker or not self._marker.exists():
            return
        try:
            left = self._marker.read_text(encoding="utf-8").strip()
        except OSError:
            left = ""
        if left == self._endpoint.name and self._endpoint.muted():
            self._endpoint.set_muted(False)
            self._notice(RECOVERED, self._endpoint.name)
        self._forget()

    def streaming(self, on):
        with self._lock:
            if on and not self._mine and self._muted_at is None and not self._given_up:
                if self._endpoint.muted():
                    # Muted already, by the user: stays theirs.
                    self._muted_at = self._clock()
                    return
                self._endpoint.set_muted(True)
                self._mine = True
                self._muted_at = self._clock()
                self._silent_since = None
                self._remember()
                self._notice(MUTED, self._endpoint.name)
            elif not on and self._muted_at is not None:
                self._release(UNMUTED)

    def heard(self, peak, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            if peak > LOUD_PEAK:
                self._silent_since = None
                if self._muted_at is None or not self._mine:
                    self._loud_at = now
                return
            if not self._mine or self._muted_at is None:
                return
            if self._silent_since is None:
                self._silent_since = now
            was_loud = self._loud_at is not None and self._muted_at - self._loud_at <= LOUD_BEFORE_SECONDS
            if (was_loud and peak == 0 and self._silent_since - self._muted_at < SILENCE_STARTS_WITHIN
                    and now - self._silent_since >= SILENT_AFTER_SECONDS):
                self._given_up = True
                self._release(GAVE_UP)

    def restore(self):
        with self._lock:
            if self._muted_at is not None:
                self._release(UNMUTED)

    def _release(self, event):
        if self._mine:
            # Unmuted by hand meanwhile: nothing to put back, nothing to say.
            if self._endpoint.muted():
                self._endpoint.set_muted(False)
                self._notice(event, self._endpoint.name)
            self._forget()
        self._mine = False
        self._muted_at = None
        self._silent_since = None

    def _remember(self):
        if self._marker:
            try:
                self._marker.parent.mkdir(parents=True, exist_ok=True)
                self._marker.write_text(self._endpoint.name, encoding="utf-8")
            except OSError as error:
                LOGGER.debug("audio: mute marker not written: %s", error)

    def _forget(self):
        if self._marker:
            try:
                self._marker.unlink()
            except OSError:
                pass


class WindowsEndpoint:
    """An output device's mute, through pycaw (MIT) and comtypes. Each call initialises COM on its
    own thread, so it may be used from the main thread and from a console-close handler alike."""

    def __init__(self, name=None):
        # soundcard has already made this thread's COM apartment multithreaded, and comtypes
        # initialises COM when first imported, single-threaded unless told otherwise, which fails
        # with RPC_E_CHANGED_MODE. So it is told: multithreaded everywhere, as soundcard is.
        sys.coinit_flags = COINIT_MULTITHREADED
        from pycaw.pycaw import AudioUtilities

        self._com()
        if name:
            devices = [device for device in AudioUtilities.GetAllDevices() if device.FriendlyName == name]
            if not devices:
                raise LookupError(f"no output device called {name!r}")
            device = devices[0]
        else:
            device = AudioUtilities.GetSpeakers()
        self.name = getattr(device, "FriendlyName", None) or name or "the default output"
        self._id = self._device_id(device)

    @staticmethod
    def _com():
        import comtypes
        try:
            comtypes.CoInitializeEx(COINIT_MULTITHREADED)
        except OSError:
            pass

    @staticmethod
    def _device_id(device):
        value = getattr(device, "id", None)
        if value is None and hasattr(device, "GetId"):
            value = device.GetId()
        return value

    def _volume(self):
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        self._com()
        enumerator = AudioUtilities.GetDeviceEnumerator()
        device = enumerator.GetDevice(self._id) if self._id else enumerator.GetDefaultAudioEndpoint(0, 1)
        interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return interface.QueryInterface(IAudioEndpointVolume)

    def muted(self):
        return bool(self._volume().GetMute())

    def set_muted(self, value):
        self._volume().SetMute(1 if value else 0, None)


def marker_path():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Beamer" / "audio-muted-output.txt"


def on_console_close(callback):
    """Runs `callback` when the console window is closed, logged off or shut down, as well as on
    Ctrl+C; Windows gives a closing console a few seconds. Returns the handler, which the caller
    keeps alive."""
    import ctypes

    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    def handler(event):
        try:
            callback()
        except Exception as error:
            LOGGER.debug("audio: restore on close failed: %s", error)
        return False

    handler = handler_type(handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
    return handler
