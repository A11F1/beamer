"""Experimental: play one machine's sound on a paired machine's speakers.

    On the Mac (plays):     python3 tools/audio_share.py receive
    On the PC (captures):   python tools/audio_share.py send

Both ends use the pairing Beamer already has: the sender finds the receiver's address and the
pair's token in its own settings.json, and the receiver answers only machines it is paired with.
Beamer itself keeps running as usual; this uses UDP port 24823 beside it. See
docs/audio-experiment.md for setup, and core/audio_stream.py for the wire.

`send --source tone` plays a test tone instead of capturing, on any platform, so the path can be
checked without a Windows machine.
"""

import argparse
import json
import logging
import math
import os
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import audio_stream, peerlist, protocol  # noqa: E402

LOGGER = logging.getLogger("Beamer")


def default_settings_path():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Beamer" / "settings.json"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "Beamer" / "settings.json"
    return Path.home() / ".config" / "Beamer" / "settings.json"


def paired_peers(path):
    try:
        settings = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"Could not read Beamer's settings at {path}: {error}")
    peers = [entry for entry in settings.get("peers", []) if protocol.linkable(entry)]
    if not peers:
        raise SystemExit(f"No machine paired with Beamer 1.5 or later in {path}. Pair them in Beamer first.")
    return peers


def label(entry, peers):
    return peerlist.labels(peers).get(entry["token"]) or entry.get("name") or peerlist.UNNAMED


def choose_peer(peers, wanted):
    if wanted:
        matches = [entry for entry in peers if wanted.casefold() in (label(entry, peers).casefold(),
                                                                       str(entry.get("name", "")).casefold())]
        if not matches:
            names = ", ".join(label(entry, peers) for entry in peers)
            raise SystemExit(f"No paired machine is called {wanted!r}. Paired: {names}.")
        return matches[0]
    macs = [entry for entry in peers if entry.get("platform") == "macos" and entry.get("host")]
    if len(macs) == 1:
        return macs[0]
    reachable = [entry for entry in peers if entry.get("host")]
    if len(reachable) == 1:
        return reachable[0]
    names = ", ".join(label(entry, peers) for entry in peers)
    raise SystemExit(f"Several machines are paired ({names}); name one with --to.")


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


def send(args):
    peers = paired_peers(args.settings)
    peer = choose_peer(peers, args.to)
    host = args.host or peer.get("host")
    if not host:
        raise SystemExit(f"Beamer has no address for {label(peer, peers)}; give one with --host.")
    name = label(peer, peers)

    def state(streaming):
        print(f"Streaming to {name}." if streaming else f"{name} stopped answering; waiting for it.", flush=True)

    sender = audio_stream.AudioSender(peer["token"], host, args.port, on_state=state).start()
    stop = threading.Event()
    mute = None
    if args.source == "tone":
        source = threading.Thread(target=tone_source, args=(sender.send, stop), daemon=True)
        what = "a 440 Hz test tone"
    else:
        if sys.platform != "win32":
            raise SystemExit("Capturing this machine's sound is Windows-only for now; use --source tone to test.")
        sys.path.insert(0, str(ROOT / "win_app"))
        import audio_capture_win
        output = audio_capture_win.output_name(args.device)
        if not args.keep_local:
            mute = local_mute(output)

        def deliver(pcm):
            sender.send(pcm)
            if mute:
                mute.heard(audio_capture_win.peak(pcm))

        source = threading.Thread(target=audio_capture_win.capture, args=(deliver, stop, args.device), daemon=True)
        what = f"what {output} plays"
    source.start()
    print(f"Sending {what} to {name} at {host}:{args.port}. Ctrl+C stops.", flush=True)
    keep_alive = None
    if mute:
        import audio_local_mute
        keep_alive = audio_local_mute.on_console_close(mute.restore)
    try:
        last_report = time.monotonic()
        while True:
            time.sleep(0.25)
            if mute:
                mute.streaming(sender.streaming)
            if time.monotonic() - last_report >= args.report:
                last_report = time.monotonic()
                if sender.streaming:
                    print(f"  {sender.sent} packets sent", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if mute:
            mute.restore()
        sender.stop()
        del keep_alive


def local_mute(output):
    """A LocalMute for the output being captured, or None (with the reason shown) if it cannot be
    reached."""
    import audio_local_mute
    try:
        endpoint = audio_local_mute.WindowsEndpoint(output)
    except Exception as error:
        print(f"This PC's own output stays on: its mute could not be reached ({error}). "
              f"Install pycaw (tools/requirements-audio.txt), or pass --keep-local.", flush=True)
        return None
    words = {
        audio_local_mute.MUTED: "Muted {name} while the sound plays on the other machine.",
        audio_local_mute.UNMUTED: "Unmuted {name}.",
        audio_local_mute.RECOVERED: "Unmuted {name}, left muted by an earlier run.",
        audio_local_mute.GAVE_UP: ("Muting {name} silences the stream too on this device, so it stays unmuted. "
                                   "Turn it down on the headset itself instead, or pass --keep-local."),
    }
    return audio_local_mute.LocalMute(endpoint, marker=audio_local_mute.marker_path(),
                                      notice=lambda event, name: print(words[event].format(name=name), flush=True))


def receive(args):
    peers = paired_peers(args.settings)
    if args.from_:
        peers = [choose_peer(peers, args.from_)]
    names = {entry["token"]: label(entry, peers) for entry in peers}
    target = max(1, round(args.latency_ms / 1000 / audio_stream.PACKET_SECONDS))
    buffer = audio_stream.JitterBuffer(target=target, limit=max(target * 4, target + 20))

    def change(name):
        print(f"Playing sound from {name}." if name else "Nothing playing.", flush=True)

    receiver = audio_stream.AudioReceiver(names, buffer, port=args.port, on_change=change).start()
    sys.path.insert(0, str(ROOT / "mac_app"))
    import audio_playback_mac
    output = audio_playback_mac.open_output(buffer, args.device)
    print(f"Listening on UDP {receiver.port} for {', '.join(names.values())}; playing on "
          f"{audio_playback_mac.output_name(args.device)} with {target * audio_stream.PACKET_SECONDS * 1000:.0f} ms "
          f"of buffer. Ctrl+C stops.", flush=True)
    try:
        while True:
            time.sleep(args.report)
            if receiver.core.playing:
                core, jitter = receiver.core.stats, buffer.stats
                print(f"  {core['packets']} packets, buffer {buffer.depth}, lost {jitter['lost']}, "
                      f"late {jitter['late']}, underruns {jitter['underruns']}, dropped {jitter['dropped']}, "
                      f"rejected {core['rejected']}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        output.stop()
        output.close()
        receiver.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Experimental: play one paired machine's sound on another.")
    parser.add_argument("--settings", default=default_settings_path(), help="Beamer's settings.json (default: this user's)")
    parser.add_argument("--port", type=int, default=audio_stream.AUDIO_PORT, help="UDP port (default: %(default)s)")
    parser.add_argument("--report", type=float, default=5.0, help="seconds between status lines")
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    sending = commands.add_parser("send", help="send this machine's sound (Windows) or a test tone")
    sending.add_argument("--to", help="the paired machine to play on (default: the one paired Mac)")
    sending.add_argument("--host", help="its address, if Beamer's saved one is wrong")
    sending.add_argument("--source", choices=("loopback", "tone"), default="loopback")
    sending.add_argument("--device", help="capture this output device instead of the default (Windows)")
    sending.add_argument("--keep-local", action="store_true",
                         help="keep playing on this PC too, instead of muting it while streaming")
    sending.set_defaults(run=send)

    receiving = commands.add_parser("receive", help="play a paired machine's sound here (Mac)")
    receiving.add_argument("--from", dest="from_", help="answer only this paired machine")
    receiving.add_argument("--latency-ms", type=float, default=40.0,
                           help="buffer before playing; raise it on Wi-Fi (default: %(default)s)")
    receiving.add_argument("--device", help="play on this output device instead of the default")
    receiving.set_defaults(run=receive)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    args.run(args)


if __name__ == "__main__":
    main()
