# Audio sharing: a local experiment

Plays what a Windows PC is playing on a paired Mac's speakers, from a **Sound** menu in Beamer
itself. A proof of concept kept in this fork: it is in the builds you make from this branch, not in
any release, and it changes nothing in Beamer's link, settings or wire version 6. Upstream is
designing audio of its own ([kalkman-code/beamer#23](https://github.com/kalkman-code/beamer/issues/23)),
so expect to throw this away when that lands.

## What it does

- **One fixed route.** The PC's sound goes to the Mac, whichever machine has the keyboard and mouse.
- **Uses your pairing.** Each end reads the pairing Beamer already has. The Mac answers only
  machines it is paired with. There is nothing new to pair.
- **Encrypted.** Every packet is ChaCha20-Poly1305, keyed afresh for each stream from the pair's
  token and an X25519 exchange, as the link is. The wire is described at the top of
  [core/audio_stream.py](../core/audio_stream.py).
- **Separate from input.** It runs on its own UDP port, 24823, so a late audio packet never holds up
  a keystroke.
- **Uncompressed.** 48 kHz 16-bit stereo, about 1.6 Mbit/s, in 5 ms packets. The Mac keeps 40 ms
  in hand by default.
- **Quiet on the PC.** While the Mac is playing, the PC's own output is muted, and unmuted again
  when streaming stops. See [The PC's own speakers](#the-pcs-own-speakers).

## The Sound menu

| On the PC: Beamer's tray menu, **Sound** | Does |
|---|---|
| The first line | What is happening: not sending, waiting for the Mac, or playing on it |
| Play this PC's sound on *Mac* | Starts sending to that Mac; choose it again to stop |
| Mute this PC while its sound plays elsewhere | See [The PC's own speakers](#the-pcs-own-speakers) |

| On the Mac: Beamer's menu-bar menu, **Sound** | Does |
|---|---|
| The first lines | What is playing, and the buffer's lost, late and gap counts |
| Play other machines' sound here | Turns receiving on and off. On by default |
| Buffer | How much is kept in hand: 40 ms on a wire, more on Wi-Fi |

Every machine paired with the Mac may play there. Both menus remember their choices in
`audio.json` beside Beamer's `settings.json`, and never change Beamer's own settings. Quitting
Beamer on the PC stops the sound and unmutes the PC. Anything that goes wrong is in Beamer's log
(**Open log folder** in the same menu), on lines starting `audio:`.

## Build Beamer locally

The Sound menu exists only in a Beamer built from this branch. Each build replaces the installed
Beamer on that machine, and the next release from upstream replaces it again, without the menu.

### Mac

Once, the tools the build script insists on, in Terminal (each asks for your password):

```sh
sudo installer -pkg python-3.12.10-macos11.pkg -target /    # python.org's, the version the build pins
sudo softwareupdate --install-rosetta --agree-to-license     # the build tests the Intel half too
```

Then, from the repository folder (about 25 minutes, most of it the build's own link test on each
architecture):

```sh
cd mac_app
BEAMER_SIGN_IDENTITY="<your signing identity>" ./build_dmg.sh
```

`security find-identity -v -p codesigning` lists your identities; an Apple Development one works.
Signed by a real identity, macOS keeps Beamer's Accessibility and Input Monitoring grants from one
local build to the next. It asks once for them again after the first, because the release is
signed by someone else. Without one, the script signs ad hoc, and macOS asks after every build.

Quit Beamer, replace `/Applications/Beamer.app` with `mac_app/dist/Beamer.app`, and open it. The
first time, macOS may ask whether Beamer may find devices on your local network. Allow it.

### Windows

From the repository folder, in PowerShell, with Python 3.12 from python.org on `PATH`:

```powershell
cd win_app
powershell -ExecutionPolicy Bypass -File build_win_app.ps1
```

It makes its own `.venv`, installs what Beamer needs, runs the Windows tests and writes
`dist\Beamer.exe`, unsigned. Quit the installed Beamer from its tray menu, then run
`dist\Beamer.exe`, or copy it over the installed one in `%LOCALAPPDATA%\Beamer`.

## The command line

The same, without Beamer's menus: for testing, and for seeing the stream's numbers. It uses the
Sound menu's port, so turn off **Play other machines' sound here** on the Mac first.

```sh
python3 -m venv .venv-audio
.venv-audio/bin/pip install -r tools/requirements-audio.txt
.venv-audio/bin/python tools/audio_share.py receive
```

```powershell
py -m venv .venv-audio
.venv-audio\Scripts\pip install -r tools\requirements-audio.txt
.venv-audio\Scripts\python tools\audio_share.py send
```

The sender picks the one Mac paired with the PC, at the address Beamer saved for it. Ctrl+C stops
either end, and the other end recovers on its own when it starts again.

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

The receiver prints a status line every five seconds; the Mac's Sound menu shows the first three:

| Field | Meaning | If it keeps rising |
|---|---|---|
| `lost` | Packets that never came, played as silence | Wi-Fi drops; try a wire |
| `late` | Packets that came after their turn | Choose a bigger Buffer |
| `underruns` (gaps) | Times the buffer ran dry and waited to refill | Choose a bigger Buffer |
| `dropped` | Packets cut because the PC's clock runs slightly fast against the Mac's | Expected now and then; a click each time |
| `rejected` | Packets that failed authentication | Should stay at 0 |

## The PC's own speakers

While the Mac is playing the sound, the PC mutes the output it captures, and says so on the Sound
menu's second line. It unmutes when streaming stops, or Beamer quits.

- **Your own mute is respected.** If the output was already muted, it stays muted afterwards. If
  you unmute it while streaming, it is not muted again until the next stream.
- **Nothing stays muted by accident.** If the PC crashes or loses power while muted, the next
  stream unmutes it first.
- **Some drivers can't do this.** On some sound drivers, muting the output also silences what
  loopback captures. The PC checks for this: if the sound stops entirely the moment it mutes, it
  unmutes and says so. Turn the volume down on the headset itself instead.
- **The output is chosen at start.** If you change the default output while streaming, capture
  follows the new one but the mute stays on the old one. Choose the Mac again after changing it.

## Known limits

- Windows to Mac only. Capture is WASAPI loopback (`win_app/audio_capture_win.py`); playback is
  PortAudio (`mac_app/audio_playback_mac.py`), on the Mac's default output when receiving starts.
- No compression and no clock-drift correction, beyond dropping packets.
- No microphone, and nothing follows the keyboard and mouse.
