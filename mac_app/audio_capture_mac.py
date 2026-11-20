"""This Mac's microphone, as 48 kHz 16-bit stereo PCM, for the experimental audio stream
(core/audio_stream.py): a paired PC's apps hear it through a virtual cable there.

Through `sounddevice` and its bundled PortAudio, as playback is. macOS asks once whether Beamer may
use the microphone; the hardened runtime needs the com.apple.security.device.audio-input
entitlement to ask at all, and Info.plist's NSMicrophoneUsageDescription says why.

Part of the local audio experiment only: Beamer's Sound menu (sound_menu.py) opens it.
"""

from core import audio_stream

# BlackHole carries a PC's microphone into this Mac's apps; capturing it would send the PC its own
# voice back.
LOOP_WORD = "BlackHole"


def _inputs():
    import sounddevice
    return [device for device in sounddevice.query_devices() if device["max_input_channels"] > 0]


def input_name(device=None):
    """The microphone to capture: `device`, or the default input unless that is BlackHole, when it
    is the first real microphone instead. None when there is none."""
    import sounddevice
    if device:
        return sounddevice.query_devices(device, kind="input")["name"]
    try:
        default = sounddevice.query_devices(kind="input")["name"]
    except (ValueError, sounddevice.PortAudioError):
        default = None
    if default and LOOP_WORD.casefold() not in default.casefold():
        return default
    for candidate in _inputs():
        if LOOP_WORD.casefold() not in candidate["name"].casefold():
            return candidate["name"]
    return None


def mono_to_stereo(pcm):
    """16-bit mono PCM as 16-bit stereo, each sample on both sides; no numpy in the Mac app."""
    out = bytearray(len(pcm) * 2)
    out[0::4], out[1::4] = pcm[0::2], pcm[1::2]
    out[2::4], out[3::4] = pcm[0::2], pcm[1::2]
    return bytes(out)


def open_input(deliver, device):
    """A started input stream calling `deliver(pcm)` with each block, as stereo; close it with
    `.stop()` and `.close()`. Opened only by a caller that has just started PortAudio afresh, as the
    Sound menu does, so it sees the microphones as they are."""
    import sounddevice

    def callback(indata, frames, timing, status):
        deliver(mono_to_stereo(bytes(indata)))

    stream = sounddevice.RawInputStream(
        samplerate=audio_stream.SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=audio_stream.PACKET_FRAMES,
        latency="low",
        device=device,
        callback=callback,
    )
    stream.start()
    return stream
