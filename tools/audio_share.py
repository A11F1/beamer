"""Experimental: play one machine's sound on a paired machine's speakers, from the command line.

    On the Mac (plays):     python3 tools/audio_share.py receive
    On the PC (captures):   python tools/audio_share.py send

Both ends use the pairing Beamer already has: the sender finds the receiver's address and the
pair's token in its own settings.json, and the receiver answers only machines it is paired with.
It uses UDP port 24823, the same as the Sound menu in a local build of Beamer, so turn that
off while this runs. See docs/audio-experiment.md.

`send --source tone` plays a test tone instead of capturing, on any platform, so the path can be
checked without a Windows machine.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import audio_service, audio_stream  # noqa: E402
from core.audio_service import PairingError  # noqa: E402


def send(args):
    peers = audio_service.paired_peers(args.settings)
    peer = audio_service.choose_peer(peers, args.to)
    session = audio_service.SendSession(
        peer, peers, host=args.host, port=args.port, source=args.source, device=args.device,
        mute_local=args.source == audio_service.SOURCE_LOOPBACK and not args.keep_local,
        notice=lambda text: print(text, flush=True))
    session.start()
    print(f"Sending {session.what} to {session.name} at {session.host}:{args.port}. Ctrl+C stops.", flush=True)
    keep_alive = None
    if sys.platform == "win32":
        audio_service._win_app()
        import audio_local_mute
        keep_alive = audio_local_mute.on_console_close(session.restore)
    streaming = False
    try:
        last_report = time.monotonic()
        while True:
            time.sleep(0.25)
            session.tick()
            if session.streaming != streaming:
                streaming = session.streaming
                print(f"Streaming to {session.name}." if streaming else f"{session.name} stopped answering; waiting for it.",
                      flush=True)
            if streaming and time.monotonic() - last_report >= args.report:
                last_report = time.monotonic()
                print(f"  {session.sent} packets sent", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
        del keep_alive


def receive(args):
    peers = audio_service.paired_peers(args.settings)
    if args.from_:
        peers = [audio_service.choose_peer(peers, args.from_)]
    names = {entry["token"]: audio_service.label(entry, peers) for entry in peers}

    def change(name):
        print(f"Playing sound from {name}." if name else "Nothing playing.", flush=True)

    session = audio_service.ReceiveSession(names, port=args.port, device=args.device,
                                           latency_ms=args.latency_ms, on_change=change)
    print(f"Listening on UDP {session.port} for {', '.join(names.values())}; playing on {session.output_name} "
          f"with {session.latency_ms:.0f} ms of buffer. Ctrl+C stops.", flush=True)
    try:
        while True:
            time.sleep(args.report)
            if session.playing:
                stats = session.stats()
                print(f"  {stats['packets']} packets, buffer {stats['buffer']}, lost {stats['lost']}, "
                      f"late {stats['late']}, underruns {stats['underruns']}, dropped {stats['dropped']}, "
                      f"rejected {stats['rejected']}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Experimental: play one paired machine's sound on another.")
    parser.add_argument("--settings", default=audio_service.default_settings_path(),
                        help="Beamer's settings.json (default: this user's)")
    parser.add_argument("--port", type=int, default=audio_stream.AUDIO_PORT, help="UDP port (default: %(default)s)")
    parser.add_argument("--report", type=float, default=5.0, help="seconds between status lines")
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    sending = commands.add_parser("send", help="send this machine's sound (Windows) or a test tone")
    sending.add_argument("--to", help="the paired machine to play on (default: the one paired Mac)")
    sending.add_argument("--host", help="its address, if Beamer's saved one is wrong")
    sending.add_argument("--source", choices=(audio_service.SOURCE_LOOPBACK, audio_service.SOURCE_TONE),
                         default=audio_service.SOURCE_LOOPBACK)
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
    try:
        args.run(args)
    except PairingError as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
