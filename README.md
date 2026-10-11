# square_impact

**Instant high-speed replays from the Square Omni launch monitor's own cameras.**

The Square Omni films every shot at ~500 fps with two internal cameras, but it
never shows you that footage. square_impact pulls the frames around impact
straight out of the camera board's memory over USB, a few seconds after each
shot, and shows them as a slow-motion clip. You can watch it in a desktop
viewer or as a pop-up overlay in OBS.

> Unofficial. Not affiliated with or endorsed by Square. It reads the device
> through its debug interface and **never writes anything to it** (see
> [Read-only by design](#read-only-by-design)). A firmware update could change
> any of the internals it relies on.

---

## Quick start

Requirements: Windows, Python 3 (standard library only, nothing to `pip install`),
and `adb` (`winget install Google.PlatformTools`).

1. Connect the Omni's **launch-camera USB port** (board `r1`) to the PC. It
   shows up as `Tina ADB` in Device Manager. Check with:
   ```bash
   python omni_frames.py status
   ```
2. Pick how you want to watch:

   **OBS overlay.** Each shot pops up, plays twice, then disappears:
   ```bash
   python omni_frames.py obs
   ```
   In OBS add a **Browser Source** with URL `http://localhost:8765/` and size
   **1280×192** (or any 20:3 size). Open `http://localhost:8765/replay` in any
   browser to re-show the last shot while you position it.

   **Desktop viewer.** Stays open and loads every new shot:
   ```bash
   python omni_frames.py live
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
python omni_frames.py obs   [--port 8765]        OBS overlay server (headless)
python omni_frames.py live                       desktop viewer, re-arms after every shot
python omni_frames.py shot                       capture the next shot once, then view it
python omni_frames.py view  [DIR]                view a saved clip (default: newest)
python omni_frames.py status                     board, frame layout, recent detections

python omni_frames.py grab  [--shot]             every buffer in the ring (slow; for digging)
python omni_frames.py sample [N] [--shot] [--range A:B]   every Nth buffer + contact sheet
python omni_frames.py impact [DIR]               find impact in a full-ring capture

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

## Known limits

- Developed and tested on the **launch board (`r1`) with soft putts and chips
  indoors**. Full-speed swings, where the ball leaves the frame within a few
  frames, and the spin board (`r2`) haven't been tested with the shot modes.
- Only one board is read at a time (one USB connection).
- Contact detection assumes the ball starts at rest and visible in the top
  camera's view.
- Depends on sqocam's log messages and memory layout. A firmware update may
  break it. `status`, `sample` and `grab` are the tools for re-checking.
