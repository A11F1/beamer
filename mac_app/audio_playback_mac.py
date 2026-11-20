"""Plays the experimental audio stream (core/audio_stream.py) on this Mac's default output.

Through the `sounddevice` package (MIT), whose macOS wheel carries its own PortAudio, so nothing
else is installed. The device callback pulls from the JitterBuffer and never waits on the network;
Core Audio converts 48 kHz to whatever rate the output runs at.

Part of the local audio experiment only: Beamer's Sound menu (sound_menu.py) and
tools/audio_share.py import it when asked to receive.
"""

from core import audio_stream


def open_output(buffer, device=None):
    """An output stream that plays `buffer`, started; close it with `.stop()` and `.close()`."""
    import sounddevice

    def callback(outdata, frames, timing, status):
        outdata[:] = buffer.pull(len(outdata))

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


def output_name(device=None):
    import sounddevice
    return sounddevice.query_devices(device, kind="output")["name"]
