# Square Omni device notes

What we know about the Omni's camera boards, found by reading them over ADB.
Everything here was observed on one unit (camera firmware `SQOVER:1.5.8`,
Tina build `tina.ubuntu.20260406`). Other firmware may differ. Please update
this file when you learn something new.

## Boards

| | `r1` | `r2` |
|---|---|---|
| Role | launch cameras | spin cameras |
| USB device | `Tina ADB`, VID `18D1` PID `D002` (WinUSB) | same |
| SoC / OS | Allwinner R818 (4× Cortex-A53), Tina Linux 3.5.1, kernel 4.9 | same |
| Sensors | 2× OV9281 (I²C `twi2` 0x60 / 0x70), 1280×800 global shutter, mono | same |
| Capture | 1280×192 @ ~500 fps (`LM_*` config) | 1280×384 @ ~250 fps (`SP_*` config) |
| Ring buffer | 256 frames per camera | 128 frames per camera |

- **Shell:** the ADB shell is root, BusyBox 1.27.2.
- **Capture program:** `/tmp/sqocam`, copied from `/mnt/UDISK/sqocam` along
  with its config `/tmp/sqocam.cfg`.
- **Main board:** strings in sqocam show a main board deploying it to the
  camera boards over SSH (`172.16.20.2`). MQTT is used for commands and results.
- **Camera positions:** factory calibration (intrinsics, distortion,
  extrinsics) is in `sqocam.cfg`. On `r1`, camera 0 sits ~329 mm up, tilted
  ~26° down (the **top** camera); camera 1 sits ~129 mm up and level.

## Where the frames are

- **Buffers:** sqocam allocates ION buffers from the CMA pool
  (`cma=384M`, `0x68000000`–`0x80000000`). Frames start at physical
  **`0x68080000`** (the first 512 KB is an unrelated allocation): all of
  camera 0's buffers, then all of camera 1's.
- **Slot size:** each frame sits in a slot of the next power of two at or
  above its size: 245 760 B → 256 KB slots on `r1`, 491 520 B → 512 KB on `r2`.
- **Pixel format:** 8-bit grayscale, row-major, no header.
- **Reading them:** `/proc/<pid>/mem` can't read the dmabuf mappings (EIO).
  `/dev/mem` can, because `CONFIG_STRICT_DEVMEM` is off. Reads run at ~20 MB/s
  per core.
- **Lifetime:** buffers are never written to disk unless sqocam's
  `DATA_EXPORT=1`, which is off by default and changing it means writing to
  the device, so we don't.

## Shot timeline (`r1`)

sqocam logs to logd (`logread`), with uptime stamps `##T <seconds>`. Logged
lines for a typical shot, with times relative to the cameras stopping:

| t | Log line | Meaning |
|---|---|---|
| −0.2 s | `time : sqp_bfind_fire[F, F] fire(1) bgn(..), end(..)` | event detected at ring buffer **F** |
| | `fire or move ok.` | |
| 0 | `sqp_bfind_fire_detect stop : .. idx(N) ..` | cameras stopping; N = buffer at stop |
| +0.1 s | `mycam_stop() done`, `mycam_close_buffer(..)` | buffers released (contents stay intact) |
| +0.4 s | `sqp_bfind_shot_data return : 1` | **accepted as a shot** (square_impact's trigger) |
| +0.9 s | `sim_carry input bs(..) ba(..) bd(..) srate((..) ..` | ball speed, launch, direction, spin |
| +5 s | `sqp_grab_start(1)` | cameras restart: **ring overwritten** |

- **Rejected events:** these log `sqp_bfind_fire_detect return : 5` and no
  `shot_data` line.
- **Contact frame:** the moment of impact is 4–5 frames before **F**.
- **Before the shot:** `ball ready ok.` / `sqp_grab_start(0)` marks the
  switch to 500 fps shot mode once a ball is placed.
- **Log file vs logd:** `/tmp/log/messages` is rotated (`messages.0`) and
  flooded by `ledmgr`, so follow logd with `logread -f -e ...` instead of
  tailing the file.
- **Frame counter:** the VIN driver status (`/sys/kernel/debug/mpp/vi`, `frame => cnt`)
  keeps counting after the shot line, so it **can't** be used to find the
  newest buffer.

## ADB transport quirks

- **No `exec-out`, `shell -T` or `reverse`.** The device only allocates a
  pty, so binary output passes through the tty layer.
- **The pty inserts CRs.** Its `ONLCR` adds a `\r` before every `\n`.
  `adb.exe` on Windows adds another when its stdout is a pipe. square_impact
  echoes a marker line first to learn the exact sequence, then strips it,
  which is lossless.
- **The tty layer is slow on dark frames.** It processes newline-like bytes
  one at a time, and dark pixels (8–13) are exactly those, capping one stream
  at ~2.3 MB/s. `stty -opost` on the session pty raises that to ~6 MB/s, and
  several parallel sessions reach ~10 MB/s in total.
- **Missing tools:** BusyBox has `dd` (with `iflag=skip_bytes`), `gzip`,
  `hexdump`, `zstd`, and a client-only `nc`. It has no `base64`, `od` or `xxd`.
  `gzip`/`zstd` compress the noisy frames poorly (~0.6) and slower than the
  link, so raw transfer wins.
