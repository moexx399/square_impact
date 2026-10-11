# square_impact

[![ci](https://github.com/moexx399/square_impact/actions/workflows/ci.yml/badge.svg)](https://github.com/moexx399/square_impact/actions/workflows/ci.yml)
[![build-exe](https://github.com/moexx399/square_impact/actions/workflows/build-exe.yml/badge.svg)](https://github.com/moexx399/square_impact/actions/workflows/build-exe.yml)
[![release](https://img.shields.io/github/v/release/moexx399/square_impact?include_prereleases)](https://github.com/moexx399/square_impact/releases)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![platform: windows](https://img.shields.io/badge/platform-windows-lightgrey)

**Instant high-speed replays from the Square Omni launch monitor's own cameras.**

The Square Omni films every shot at ~500 fps with two internal cameras, but it
never shows you that footage. square_impact pulls the frames around impact
straight out of the camera board's memory over USB, a few seconds after each
shot, and shows them as a slow-motion clip. You can watch it in a desktop
viewer or as a pop-up overlay in OBS.

> **Use at your own risk. This software comes with no warranty of any kind.**
> It's unofficial and not affiliated with or endorsed by Square. It reads the
> device through its debug interface and **never writes anything to it** (see
> [Read-only by design](#read-only-by-design)), but it still talks to the
> hardware in ways Square doesn't support, and a firmware update could change
> any of the internals it relies on. See [Disclaimer](#disclaimer) and
> [License](#license).

**Contents:** [Quick start](#quick-start) · [OBS overlay](#obs-overlay-options) ·
[Commands](#commands) · [Output](#output) · [How it works](#how-it-works) ·
[Building the exe](#building-the-exe) · [Known limits](#known-limits) ·
[Contributing](#contributing) · [Changelog](CHANGELOG.md)

---

## Quick start

**Easiest:** download `square_impact-windows.zip` from the GitHub Releases page
(or from the latest `build-exe` workflow run). It contains a single
`square_impact.exe` with `adb` built in, so no Python or Android tools are
needed. Use it exactly like the commands below, replacing
`python square_impact.py` with `square_impact.exe`. Shots are saved in a `shots`
folder next to the exe. The bundled adb is unpacked once to
`%LOCALAPPDATA%\square_impact\adb\`.

**From source:** Windows, Python 3 (standard library only, nothing to `pip install`),
and `adb` (`winget install Google.PlatformTools`).

1. Connect the Omni's **launch-camera USB port** (board `r1`) to the PC. It
   shows up as `Tina ADB` in Device Manager. Check with:
   ```bash
   python square_impact.py status
   ```
2. Pick how you want to watch:

   **OBS overlay.** Each shot pops up, plays twice, then disappears:
   ```bash
   python square_impact.py obs
   ```
   In OBS add a **Browser Source** with URL `http://localhost:8765/` and size
   **1280×192** (or any 20:3 size). Open `http://localhost:8765/replay` in any
   browser to re-show the last shot while you position it.

   **Desktop viewer.** Stays open and loads every new shot:
   ```bash
   python square_impact.py live
   ```

3. Hit a ball. The clip shows up about 3–4 seconds later.

Run only one capture at a time (`obs`, `live`, `shot`, ...). Two compete for
the same USB bandwidth and can miss the window before the cameras restart.

## OBS overlay options

Add these to the Browser Source URL, e.g. `http://localhost:8765/?gain=1.5&info=1`:

| Option  | Default | Meaning |
|---------|---------|---------|
| `fps`   | 30      | Playback speed. A 61-frame clip at 30 fps is ~2 s, about 1/15 real time |
| `loops` | 2       | How many times the clip plays before hiding |
| `gap`   | 250     | Pause between plays (ms) |
| `fade`  | 300     | Fade in/out time (ms) |
| `gain`  | 1       | Brightness boost. The raw frames are dark; 1.5–2 usually looks better |
| `info`  | off     | `info=1` shows the Omni's own numbers (ball speed, launch, direction, spin) |

The page is transparent while idle. Reloading it in OBS doesn't replay old shots.
If a new shot arrives mid-playback, it replaces the current one.

## Commands

```text
python square_impact.py obs   [--port 8765]        OBS overlay server (headless)
python square_impact.py live                       desktop viewer, re-arms after every shot
python square_impact.py shot                       capture the next shot once, then view it
python square_impact.py view  [DIR]                view a saved clip (default: newest)
python square_impact.py status                     board, frame layout, recent detections

python square_impact.py grab  [--shot]             every buffer in the ring (slow; for digging)
python square_impact.py sample [N] [--shot] [--range A:B]   every Nth buffer + contact sheet
python square_impact.py impact [DIR]               find impact in a full-ring capture

common options:
  --cam top|bottom|both   which camera (default: top for obs/live/shot)
  --pad N                 frames kept before and after contact (default 30)
```

Viewer keys: `←`/`→` step a frame, `Space` play/pause. Buttons jump to impact
and save the current frame as PNG.

## Output

Each shot is saved to `shots/<date>_<time>_r1_shot_cam0/`:

- `cam0_<buffer>.pgm`: the raw frames (1280×192, 8-bit grayscale), contact ±30 frames
- `meta.json`: frame order, contact frame, time offsets, the Omni's event frame
- `sheet_cam0.png`: contact sheet of the whole clip

Only the **5 newest** capture folders are kept. Older ones are deleted
automatically (`KEEP_CAPTURES` at the top of the script).

---

## How it works

### The hardware

The Omni has two camera boards, each an Allwinner R818 running Tina Linux.
Each board has its own USB port exposing ADB with a root shell, and each runs
`/tmp/sqocam` driving two OV9281 global-shutter mono sensors:

| Board | Role | Frames | Rate | Ring buffer |
|-------|------|--------|------|-------------|
| `r1`  | launch | 1280×192 | ~500 fps | 256 frames per camera |
| `r2`  | spin   | 1280×384 | ~250 fps | 128 frames per camera |

On `r1`, camera 0 is the **top** camera (~33 cm up, tilted 26° down) and
camera 1 the **bottom** (~13 cm up, level). That comes from the factory
calibration in `/tmp/sqocam.cfg`.

### Getting frames out

- **Frames only exist in RAM.** They live in ION/CMA DMA buffers starting at
  physical address `0x68080000`, one slot per frame (slots are the next power
  of two above the frame size), camera 0 then camera 1. They're never written
  to disk, and the DMA mappings can't be read through `/proc/PID/mem`, so
  square_impact reads physical memory with `dd if=/dev/mem`.
- **A shot has ~5 s before it's gone.** After a shot, sqocam stops the
  cameras, analyses the shot, then restarts them about 5 s later, overwriting
  the ring. square_impact watches the log *on the device* (`logread -f`). The
  moment `sqp_bfind_shot_data return : 1` appears, it reads the event frame
  from the preceding `sqp_bfind_fire[F, F]` line and copies just the 71
  frames around it into `dd`'s memory. That takes a fraction of a second.
  Streaming them to the PC can then take as long as it needs.
- **Transfer speed.** This adbd only supports a terminal-style shell (no
  `exec-out`). The device's terminal processes newline-like bytes one at a
  time, and dark pixels (values 8–13) are exactly those bytes, which capped a
  single stream at ~2.3 MB/s. square_impact runs `stty -opost` on its own temporary
  session, uses several sessions in parallel (~10 MB/s total), and strips the
  carriage returns that adb.exe inserts on Windows. That cleanup is lossless
  and has been verified byte-for-byte.
- **Finding contact.** The resting ball is located on the per-pixel minimum
  of the first frames, which keeps still objects and removes the moving club.
  It's then tracked by its sub-pixel brightness centre: at rest it wanders
  under 0.5 px, and once struck it moves 3+ px per frame. Contact is the last
  frame before steady movement, typically 4–5 frames before the Omni's own
  event frame. Events with no ball in view are skipped, because sqocam
  reports those too.

### Read-only by design

Every command sent to the Omni only reads: `/dev/mem`, `/tmp/sqocam.cfg` and
`logread`. Nothing is pushed, written, staged in `/tmp`, or reconfigured. The
only setting touched is `stty -opost` on square_impact's own adb session terminal,
which disappears when that session closes.

## Performance

| | Before | Now |
|---|---|---|
| Shot to clip on screen | 35 s+ (often missed the shot) | ~3–4 s |
| Data pulled per shot | 64 MB (whole ring) | ~19 MB (71 frames) |
| Transfer rate | ~2.3 MB/s | ~10 MB/s |
| Finding contact | 0.3–0.9 s | 0.07 s |

## Building the exe

`.github/workflows/build-exe.yml` runs on every push and pull request
(`windows-latest`, Python 3.13). It:

1. downloads Google's latest Android SDK platform-tools and takes `adb.exe`,
   `AdbWinApi.dll`, `AdbWinUsbApi.dll` and `NOTICE.txt`;
2. builds a one-file `square_impact.exe` with PyInstaller, bundling those files;
3. smoke-tests it with `square_impact.exe version`, which runs the bundled adb
   and needs no device;
4. uploads `square_impact-windows.zip` (exe, README, LICENSE, adb license notice) as a
   workflow artifact, and attaches it to a GitHub Release when the push is a
   `v*` tag (`git tag v0.1.0 && git push --tags`).

To build locally, do the same by hand:

```bash
pip install pyinstaller
mkdir adb   # then copy adb.exe, AdbWinApi.dll, AdbWinUsbApi.dll, NOTICE.txt from platform-tools into it
pyinstaller --onefile --console --name square_impact --add-data "adb;adb" square_impact.py
```

adb is part of the Android SDK platform-tools (Apache 2.0). Its license notice
ships in the release zip as `adb-NOTICE.txt`.

## Known limits

- Developed and tested on the **launch board (`r1`) with soft putts and chips
  indoors**. Full-speed swings, where the ball leaves the frame within a few
  frames, and the spin board (`r2`) haven't been tested with the shot modes.
- Only one board is read at a time (one USB connection).
- Contact detection assumes the ball starts at rest and visible in the top
  camera's view.
- Depends on sqocam's log messages and memory layout. A firmware update may
  break it. `status`, `sample` and `grab` are the tools for re-checking.

Found something else? [Open an issue](https://github.com/moexx399/square_impact/issues/new/choose).

## Contributing

Contributions are welcome, including fixes, features, docs, and especially
**shot captures from setups and swings we haven't tested** (full swings, the
spin board, different lighting).

- [CONTRIBUTING.md](CONTRIBUTING.md): dev setup, the read-only rule, tests,
  changelog entries with [changie](https://changie.dev), and releasing.
- [docs/DEVICE_NOTES.md](docs/DEVICE_NOTES.md): everything known about the
  Omni's internals (boards, memory layout, log lines, timings, adb quirks).
- [CHANGELOG.md](CHANGELOG.md): what changed in each release.
- [Code of Conduct](CODE_OF_CONDUCT.md) · [Security policy](SECURITY.md)

Offline tests need no device: `python -m unittest discover -s tests -v`.

## Disclaimer

This software is provided **"as is", without warranty of any kind**, express or
implied. **You use it entirely at your own risk.** The authors are not
responsible for any damage to your Square Omni or other equipment, loss of
data, voided warranties, missed or incorrect shot data, or any other problem
that comes from using it.

square_impact is an independent project. It is not made, supported, or endorsed
by Square or its manufacturer, and "Square" and "Omni" are used only to say which
device it works with. It accesses the device through a debug interface that
isn't intended for end users. Using it may go against the device's terms of use
or affect its warranty, so check before you use it.

## License

Released under the [MIT License](LICENSE). You're free to use, copy, modify, and
share it, including commercially, as long as the copyright and license notice
are kept. The license includes the full no-warranty terms.

The bundled `adb` in the exe is part of the Android SDK platform-tools, licensed
separately under the Apache License 2.0. Its notice ships as `adb-NOTICE.txt`.
