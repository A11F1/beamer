# Audio sharing: a local experiment

Plays what a Windows PC is playing on a paired Mac's speakers. A proof of concept kept in this
fork, run from source beside the installed Beamer. It is not in the release builds, and it changes
nothing in Beamer's link, settings or wire version 6. Upstream is designing audio of its own
([kalkman-code/beamer#23](https://github.com/kalkman-code/beamer/issues/23)), so expect to throw this
away when that lands.

## What it does

- **One fixed route.** The PC's sound goes to the Mac, whichever machine has the keyboard and mouse.
- **Uses your pairing.** Each end reads the pairing Beamer already has from its own `settings.json`.
  The Mac answers only machines it is paired with. There is nothing new to pair.
- **Encrypted.** Every packet is ChaCha20-Poly1305, keyed afresh for each stream from the pair's
  token and an X25519 exchange, as the link is. The wire is described at the top of
  [core/audio_stream.py](../core/audio_stream.py).
- **Separate from input.** It runs on its own UDP port, 24823, so a late audio packet never holds up
  a keystroke.
- **Uncompressed.** 48 kHz 16-bit stereo, about 1.6 Mbit/s, in 5 ms packets. The Mac keeps 40 ms
  in hand by default.
- **Quiet on the PC.** While the Mac is playing, the PC's own output is muted, and unmuted again
  when streaming stops. See [The PC's own speakers](#the-pcs-own-speakers).

## Set it up

You need the repository on both machines, and Python 3.9 or later.

On the Mac:

```sh
python3 -m venv .venv-audio
.venv-audio/bin/pip install -r tools/requirements-audio.txt
.venv-audio/bin/python tools/audio_share.py receive
```

The first time, macOS may ask whether Python may accept incoming network connections. Allow it.

On the PC, in PowerShell:

```powershell
py -m venv .venv-audio
.venv-audio\Scripts\pip install -r tools\requirements-audio.txt
.venv-audio\Scripts\python tools\audio_share.py send
```

The sender picks the one Mac paired with the PC, at the address Beamer saved for it. Play something
on the PC and it comes out of the Mac within a second. Ctrl+C stops either end, and the other end
recovers on its own when it starts again.

## Options

| Command | Option | Use |
|---|---|---|
| `send` | `--to NAME` | Choose the machine when several are paired |
| `send` | `--host ADDRESS` | Use this address if the saved one is wrong |
| `send` | `--device NAME` | Capture an output other than the default |
| `send` | `--source tone` | Send a test tone instead; works on any platform |
| `send` | `--keep-local` | Keep playing on the PC too, instead of muting it |
| `receive` | `--latency-ms 80` | Keep more in hand, for Wi-Fi |
| `receive` | `--from NAME` | Answer only this machine |
| `receive` | `--device NAME` | Play on an output other than the default |
| either | `--settings PATH` | Use another `settings.json` (a portable install) |

## Reading the status line

The receiver prints a status line every five seconds:

| Field | Meaning | If it keeps rising |
|---|---|---|
| `lost` | Packets that never came, played as silence | Wi-Fi drops; try a wire |
| `late` | Packets that came after their turn | Raise `--latency-ms` |
| `underruns` | Times the buffer ran dry and waited to refill | Raise `--latency-ms` |
| `dropped` | Packets cut because the PC's clock runs slightly fast against the Mac's | Expected now and then; a click each time |
| `rejected` | Packets that failed authentication | Should stay at 0 |

## The PC's own speakers

While the Mac is playing the sound, the sender mutes the output it captures. It prints `Muted ...`
when it does, and `Unmuted ...` when streaming stops or the sender is closed.

- **Your own mute is respected.** If the output was already muted, it stays muted afterwards. If
  you unmute it while streaming, it is not muted again until the next stream.
- **Nothing stays muted by accident.** Closing the window also unmutes. If the PC crashes or loses
  power while muted, the next `send` unmutes it.
- **Some drivers can't do this.** On some sound drivers, muting the output also silences what
  loopback captures. The sender checks for this: if the sound stops entirely the moment it mutes,
  it unmutes and says so. Turn the volume down on the headset itself instead.
- **The output is chosen at start.** If you change the default output while streaming, the sender
  follows the new one for capture but keeps muting the old one. Restart the sender after changing
  it.

## Known limits

- Windows to Mac only. Capture is WASAPI loopback (`win_app/audio_capture_win.py`); playback is
  PortAudio (`mac_app/audio_playback_mac.py`).
- No compression and no clock-drift correction, beyond dropping packets.
- No microphone, and nothing follows the keyboard and mouse.
