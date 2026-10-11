"""square_impact: instant high-speed replays from the Square Omni's own cameras.

Grabs and shows the frames held in RAM by the Square Omni's camera boards.
See README.md.

The Omni has two camera boards (Allwinner R818, Tina Linux), each exposing ADB
on its own USB port and each running /tmp/sqocam with two OV9281 mono sensors:
    r1 - launch cameras  (LM_* config: 1280x192 @ ~500 fps, 256 buffers/camera)
    r2 - spin cameras    (SP_* config: 1280x384 @ ~250 fps, 128 buffers/camera)
Frames live only in ION/CMA ring buffers, never on disk, and the dmabuf
mappings can't be read via /proc/PID/mem, so we read physical RAM with
`dd if=/dev/mem`.

After a shot sqocam stops grabbing, logs "sqp_bfind_shot_data return : 1",
and restarts grabbing ~5 s later, overwriting the ring. Shot captures wait for
that log line *on the device* and snapshot immediately to fit inside the window.

Fast shot path (`live` / `shot`):
  * sqocam logs the ring buffer of each detected event ("sqp_bfind_fire[F, F]")
    just before "shot_data return : 1"; on that line, parallel sessions
    snapshot only the window around F (71 frames) into dd's memory;
  * the parallel adb sessions stream ~10 MB/s total vs ~2.3 for one;
  * `stty -opost` on each session's own pty skips the tty layer's per-byte
    newline processing, which was the bottleneck on dark frames (pixel values
    8-13 are tab/newline/CR). It only affects that transient session pty;
  * contact is found from the images (4-5 frames before F), and
    events with no ball in view are ignored (sqocam also fires on those);
  * sqocam's own measurements (ball speed, launch, spin) come from its log.
The VIN driver's frame counter is NOT used: it kept moving after the shot was
logged, so it doesn't identify the newest buffer.

STRICTLY READ-ONLY: device commands only read (/dev/mem, /tmp/sqocam.cfg,
logread). Nothing is written, pushed, or staged on the device.

Usage:
    python square_impact.py obs [--port 8765]     # OBS browser-source overlay: each shot pops up,
                                                  # plays twice, disappears (http://localhost:8765/)
    python square_impact.py live [--cam top|bottom|both] [--pad N]
                                                  # viewer that captures every shot, re-arming itself
    python square_impact.py shot                  # capture the next shot once, then view it
    python square_impact.py status                # board, layout, recent shots
    python square_impact.py sample [N] [--shot] [--range A:B]
                                                  # every Nth buffer (default 10) + contact sheet
    python square_impact.py grab [--shot]         # every buffer; with --shot, then finds impact
    python square_impact.py impact [DIR] [--pad N]  # find impact in a full-ring capture
    python square_impact.py view [DIR]            # view a capture (default: newest)
    python square_impact.py version               # version and the adb in use

Only the newest KEEP_CAPTURES (5) capture folders in shots/ are kept.
"""

import argparse
import glob
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import zlib

# Layout found on both boards: ION/CMA base 0x68000000, the first 512 KB is an
# unrelated allocation, then 2 x NBUF frame buffers (cam0 then cam1), each in a
# slot of the next power of two >= the frame size (1280x384 -> 512 KB,
# 1280x192 -> 256 KB).
FRAME_BASE = 0x68080000
DD_BS = 0x4000  # divides every frame size and slot stride seen so far
MARKER = b"@@FRAMES@@"
SHOT_LINE = "shot_data return : 1"
BOARD_MODE = {"r1": "LM", "r2": "SP"}
SNAPSHOT_MIN = 40   # above this many frames per camera, snapshot the span in one dd
SESSIONS = 4        # parallel adb sessions per camera for shot captures

# Packaged as a one-file exe (PyInstaller), __file__ is a temp folder deleted
# on exit, so user data lives next to the exe instead.
FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
SHOTS_DIR = os.path.join(HERE, "shots")
KEEP_CAPTURES = 5   # older capture folders in shots/ are deleted after each save


# ---------------------------------------------------------------- adb helpers

def find_adb():
    if FROZEN:
        return bundled_adb()
    adb = shutil.which("adb")
    if adb:
        return adb
    pattern = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools*\platform-tools\adb.exe")
    hits = glob.glob(pattern)
    if hits:
        return hits[0]
    sys.exit("adb not found on PATH")


def bundled_adb():
    """The exe ships adb.exe + its DLLs. adb starts a server that keeps running
    after we exit, so it must not run from PyInstaller's temp folder (deleted on
    exit): copy it once to %LOCALAPPDATA%\\square_impact\\adb and run it there."""
    src = os.path.join(sys._MEIPASS, "adb")
    dst = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "square_impact", "adb")
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if not os.path.exists(d) or os.path.getsize(d) != os.path.getsize(s):
            try:
                shutil.copy2(s, d)
            except PermissionError:
                pass  # in use by a running adb server; the existing copy works
    return os.path.join(dst, "adb.exe")


def version_info():
    print(f"square_impact {VERSION}")
    release = latest_release(use_cache=False)  # always a fresh check here
    if release is None:
        print("    couldn't check for updates (offline, or no releases published yet)")
    else:
        print("    " + (update_notice(release) or f"up to date (latest release {release['tag']})"))
    adb = find_adb()
    out = subprocess.run([adb, "version"], capture_output=True, text=True)
    print(f"adb: {adb}")
    print("    " + "\n    ".join(out.stdout.strip().splitlines()[:2]))
    if out.returncode:
        sys.exit("adb failed to run")


ADB = None


def shell(cmd):
    out = subprocess.run([ADB, "shell", cmd], capture_output=True, check=True).stdout
    return out.decode("latin-1").replace("\r", "")


def shot_trigger(dump):
    """Wrap `dump` so it runs the moment the next shot is logged. Follows logd
    via logread (the /tmp/log/messages file gets rotated); sqocam stamps lines
    with uptime ("##T 2609.890"), so lines from before arming are ignored."""
    return ("U=$(cut -d. -f1 /proc/uptime); "
            f"logread -f -e '{SHOT_LINE}' | while read l; do "
            't=${l##*##T }; t=${t%%.*}; '
            f'if [ "$t" -ge "$U" ] 2>/dev/null; then {dump}; exit; fi; done')


class Stream:
    """One device command whose stdout is: anything, then MARKER + header + EOL,
    then a binary payload. Runs in a background thread.

    Raw bytes come back over the adb pty with CRs inserted before every LF (by
    the pty unless `stty -opost`, and by adb.exe on Windows). The marker line
    ends the same way, so it tells us the exact insertion; since every LF
    carries it, stripping it is lossless."""

    def __init__(self, cmd, need):
        self.need = need            # payload bytes, or callable(header) -> bytes
        self.header = self.payload = self.error = None
        self.t_marker = None
        self.p = subprocess.Popen([ADB, "shell", cmd], stdout=subprocess.PIPE)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        buf = bytearray()
        start = eol = None
        lfs = scanned = need = 0
        try:
            while True:
                chunk = self.p.stdout.read1(1 << 20)
                if not chunk:
                    break
                buf += chunk
                if start is None:
                    m = buf.find(MARKER)
                    nl = buf.find(b"\n", m) if m >= 0 else -1
                    if nl < 0:
                        continue
                    line = bytes(buf[m + len(MARKER):nl + 1])
                    body = line.rstrip(b"\r\n")
                    eol = line[len(body):]
                    self.header = body.decode("latin-1").strip()
                    self.t_marker = time.time()
                    need = self.need(self.header) if callable(self.need) else self.need
                    start = scanned = nl + 1
                lfs += buf.count(b"\n", scanned)
                scanned = len(buf)
                if len(buf) - start - lfs * (len(eol) - 1) >= need:
                    break  # all in; with a shot trigger the device side lingers otherwise
        except Exception as e:  # noqa: BLE001 - reported by the caller
            self.error = e
        finally:
            self.kill()
        if start is None:
            self.error = self.error or RuntimeError("no data received from device")
            return
        data = bytes(buf[start:]).replace(eol, b"\n")
        if len(data) < need:
            self.error = RuntimeError(f"short read: {len(data)} of {need} bytes")
        self.payload = data[:need]

    def kill(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait()

    def result(self):
        self.thread.join()
        if self.error:
            raise self.error
        return self.header, self.payload


def device_layout():
    """Frame geometry for whichever board is connected, from its sqocam.cfg."""
    board = shell("hostname").strip()
    mode = BOARD_MODE.get(board)
    if not mode:
        sys.exit(f"unknown board '{board}' (expected r1 or r2)")
    cfg = dict(re.findall(r"^(\w+)\s*=\s*(\S+)", shell("cat /tmp/sqocam.cfg"), re.M))
    w, h = int(cfg[f"{mode}_CAM_W"]), int(cfg[f"{mode}_CAM_H"])
    stride = 1 << (w * h - 1).bit_length()
    assert (w * h) % DD_BS == 0 and stride % DD_BS == 0
    return {"board": board, "mode": mode, "w": w, "h": h, "stride": stride,
            "fps": float(cfg[f"{mode}_CAM_RFPS"]), "nbuf": int(cfg[f"{mode}_CAM_NBUF"])}


def read_frames(lay, idxs, cams=(0, 1), wait_shot=False):
    """Read buffers `idxs` of each camera in `cams`. Returns one frame list per camera."""
    frame, stride, nbuf = lay["w"] * lay["h"], lay["stride"], lay["nbuf"]
    lo, hi = min(idxs), max(idxs)
    span = (hi - lo) * stride + frame
    snapshot = len(idxs) > SNAPSHOT_MIN
    if snapshot:
        # One dd per camera with a block size covering every requested slot: the
        # whole span is read into dd's own memory (/dev/mem runs ~20 MB/s) and
        # only then trickles out over adb. Two cameras are read in parallel: the
        # second dd buffers its span while the first writes, `cat` forwards it.
        def snap(cam):
            addr = FRAME_BASE + (cam * nbuf + lo) * stride
            return f"dd if=/dev/mem bs={span} count=1 iflag=skip_bytes skip={addr} 2>/dev/null"
        body = snap(cams[0])
        for cam in cams[1:]:
            body = f"{snap(cam)} | ({body}; cat)"
        dump = f"echo {MARKER.decode()}; {body}"
        need = len(cams) * span
    else:
        skips = [(FRAME_BASE + (cam * nbuf + i) * stride) // DD_BS for cam in cams for i in idxs]
        dump = (f"echo {MARKER.decode()}; for s in {' '.join(map(str, skips))}; do "
                f"dd if=/dev/mem bs={DD_BS} skip=$s count={frame // DD_BS} 2>/dev/null; done")
        need = len(skips) * frame
    cmd = "stty -opost; " + (shot_trigger(dump) if wait_shot else dump)

    s = Stream(cmd, need)
    _, data = s.result()
    print(f"  {len(idxs) * len(cams)} frames in {time.time() - s.t_marker:.1f}s"
          + (" (snapshot)" if snapshot else ""))
    if snapshot:
        return [[data[k * span + (i - lo) * stride:][:frame] for i in idxs]
                for k in range(len(cams))]
    n = len(idxs)
    return [[data[(k * n + j) * frame:][:frame] for j in range(n)] for k in range(len(cams))]


# ---------------------------------------------------------------- fast shot capture

# sqocam logs, per detection (uptime-stamped "##T <s>"):
#   time : sqp_bfind_fire[F, F] fire(1) bgn(..), end(..)   F = buffer of the event
#   sqp_bfind_fire_detect stop : ... idx(N) ...            cameras stopping
#   sqp_bfind_shot_data return : 1                         accepted as a shot
#   sim_carry input bs(..) ba(..) bd(..) srate((..) ...    its measurements
# Contact (last frame before the ball moves) lands 4-5 frames before F, so the
# window fetched around F is skewed earlier.
FETCH_BEFORE = 40   # frames fetched before sqocam's event buffer F
FETCH_AFTER = 30    # frames fetched after F
CONTACT_BEFORE_F = 4  # fallback when the ball isn't seen moving
MIN_BALL_CONTRAST = 40  # resting ball scores 70-92 vs floor; empty floor ~16


def event_trigger(dump):
    """Like shot_trigger, but also captures sqocam's event buffer into $F from
    the 'sqp_bfind_fire[F, F]' line that precedes 'shot_data return : 1'."""
    return ("U=$(cut -d. -f1 /proc/uptime); F=; "
            "logread -f -e 'sqp_bfind_fire\\[|" + SHOT_LINE + "' | while read l; do "
            't=${l##*##T }; t=${t%%.*}; [ "$t" -ge "$U" ] 2>/dev/null || continue; '
            'case "$l" in '
            '*"fire["*) F=${l#*fire[[]}; F=${F%%,*};; '
            '*"' + SHOT_LINE + '"*) if [ -n "$F" ]; then ' + dump + '; exit; fi;; '
            'esac; done')


def session_cmd(lay, cam, o0, n, trigger=True, f_fixed=None):
    """Device command for one parallel session: on the next shot, snapshot
    window positions [o0, o0+n) of the window that starts FETCH_BEFORE frames
    before sqocam's event buffer F, wrapping around the ring (two spans are read
    in parallel and emitted in time order). The header line carries F."""
    nb, st = lay["nbuf"], lay["stride"]
    base = FRAME_BASE + cam * nb * st
    dump = (
        (f"F={f_fixed}; " if f_fixed is not None else "")
        + f"S=$(( (F - {FETCH_BEFORE} + {o0} + 4 * {nb}) % {nb} )); "
        f"snap() {{ dd if=/dev/mem bs=$(($2 * {st})) count=1 iflag=skip_bytes "
        f"skip=$(({base} + $1 * {st})) 2>/dev/null; }}; "
        f"echo {MARKER.decode()} $F; "
        f"if [ $((S + {n})) -le {nb} ]; then snap $S {n}; "
        f"else snap 0 $((S + {n} - {nb})) | (snap $S $(({nb} - S)); cat); fi"
    )
    return "stty -opost; " + (event_trigger(dump) if trigger else dump)


class ShotCapture:
    """Arm parallel sessions for the next shot; each snapshots its share of the
    window around sqocam's event buffer the moment the shot is logged (a few MB
    each, read in a fraction of a second, well inside the ~5 s before the
    cameras restart). result() blocks until everything is fetched."""

    def __init__(self, lay, cams, parts=SESSIONS, f_fixed=None):
        self.lay, self.cams = lay, cams
        self.n = FETCH_BEFORE + 1 + FETCH_AFTER
        bounds = [self.n * k // parts for k in range(parts + 1)]
        self.parts = [(cam, o0, o1 - o0,
                       Stream(session_cmd(lay, cam, o0, o1 - o0, f_fixed is None, f_fixed),
                              (o1 - o0) * lay["stride"]))
                      for cam in cams for o0, o1 in zip(bounds, bounds[1:])]

    def cancel(self):
        for *_, s in self.parts:
            s.kill()

    def wait_shot(self):
        """Block until the shot is detected (first marker seen) or a session dies."""
        while True:
            for *_, s in self.parts:
                if s.t_marker or not s.thread.is_alive():
                    return
            time.sleep(0.01)

    def result(self):
        """Frames per camera in time order, with their buffer numbers."""
        st, fb, nb = self.lay["stride"], self.lay["w"] * self.lay["h"], self.lay["nbuf"]
        frames = {cam: [None] * self.n for cam in self.cams}
        events = set()
        for cam, o0, n, s in self.parts:
            header, data = s.result()
            events.add(int(header))
            for j in range(n):
                frames[cam][o0 + j] = data[j * st:j * st + fb]
        if len(events) > 1:
            raise RuntimeError(f"sessions saw different events {sorted(events)}")
        F = events.pop()
        t_shot = min(s.t_marker for *_, s in self.parts)
        return {"frames": frames, "event": F,
                "buffers": [(F - FETCH_BEFORE + p) % nb for p in range(self.n)],
                "t_shot": t_shot, "t_fetched": time.time()}


class NoBall(Exception):
    """sqocam reported a shot, but there's no ball in view around it."""


def sqocam_result():
    """sqocam's own measurements for the latest shot, if logged yet."""
    line = shell("logread -e 'sim_carry input' | tail -1")
    vals = dict(re.findall(r"(\w+)\(\(?(-?[\d.]+)\)", line))
    if not vals:
        return ""
    return (f"sqocam: ball speed {vals.get('bs', '?')} m/s, launch {vals.get('ba', '?')} deg, "
            f"direction {vals.get('bd', '?')} deg, spin {vals.get('srate', '?')} rpm")


def process_shot(lay, cap, pad):
    """Find the impact in the fetched window, reject events with no ball in
    view, and save the +-pad window as a viewable capture.
    Returns (out_dir, summary, notes)."""
    w, h, n = lay["w"], lay["h"], len(cap["buffers"])
    cams = list(cap["frames"])
    t_imp, rest, contrast = find_impact_ordered(cap["frames"][cams[0]], w, h)
    if contrast < MIN_BALL_CONTRAST:
        raise NoBall(f"ignored sqocam event at buffer {cap['event']}: no ball in view "
                     f"(contrast {contrast:.0f} < {MIN_BALL_CONTRAST})")
    notes = []
    if t_imp is None:
        t_imp = FETCH_BEFORE - CONTACT_BEFORE_F  # ball never tracked moving
        notes.append("ball never seen moving; contact estimated from sqocam's event frame")

    lo, hi = max(0, t_imp - pad), min(n, t_imp + pad + 1)
    if hi - lo < 2 * pad + 1:
        notes.append(f"impact {t_imp - FETCH_BEFORE:+d} frames from sqocam's event; "
                     f"clip cut to {hi - lo} frames")
    window = cap["buffers"][lo:hi]
    imp_buf = cap["buffers"][t_imp]
    out_dir = os.path.join(SHOTS_DIR, time.strftime("%Y%m%d_%H%M%S") +
                           f"_{lay['board']}_shot_cam{''.join(map(str, cams))}")
    os.makedirs(out_dir, exist_ok=True)
    for cam in cams:
        for b, f in zip(window, cap["frames"][cam][lo:hi]):
            write_pgm(os.path.join(out_dir, f"cam{cam}_{b:03d}.pgm"), w, h, f)
    meta = {**lay, "idxs": window, "cams": cams, "shot": True, "impact": imp_buf, "pad": pad,
            "offset_ms": [round((p - t_imp) * 1000 / lay["fps"], 1) for p in range(lo, hi)],
            "motion": [motion(cap["frames"][cam][lo:hi], w) for cam in cams],
            "sqocam_event": cap["event"], "ball_rest": rest, "ball_contrast": round(contrast)}
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh)
    fetch = cap["t_fetched"] - cap["t_shot"]
    mb = len(cams) * n * lay["stride"] / 1e6
    summary = (f"impact at buffer {imp_buf} (sqocam event {cap['event']}); "
               f"fetched {n} frames/cam ({mb:.0f} MB) in {fetch:.1f}s")
    prune_captures()
    return out_dir, summary, notes


def prune_captures(keep=KEEP_CAPTURES):
    """Delete all but the newest `keep` capture folders in shots/ (folder names
    start with a YYYYMMDD_HHMMSS timestamp, so name order is time order)."""
    dirs = sorted(d for d in glob.glob(os.path.join(SHOTS_DIR, "*")) if os.path.isdir(d))
    for d in dirs[:-keep]:
        shutil.rmtree(d, ignore_errors=True)


def write_sheets(out_dir):
    with open(os.path.join(out_dir, "meta.json")) as fh:
        meta = json.load(fh)
    w, h = meta["w"], meta["h"]
    for cam in meta["cams"]:
        frames = []
        for b in meta["idxs"]:
            with open(os.path.join(out_dir, f"cam{cam}_{b:03d}.pgm"), "rb") as fh:
                frames.append(fh.read()[-w * h:])
        grid_sheet(os.path.join(out_dir, f"sheet_cam{cam}.png"), frames, w, h)


# ---------------------------------------------------------------- image output

def write_pgm(path, w, h, f):
    with open(path, "wb") as fh:
        fh.write(b"P5\n%d %d\n255\n" % (w, h) + f)


def write_png(path, w, h, px):
    """Minimal 8-bit grayscale PNG writer (stdlib only)."""
    raw = b"".join(b"\x00" + px[y * w:(y + 1) * w] for y in range(h))
    chunk = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def half(f, w, h):
    """2x2 box downscale."""
    out = bytearray()
    for y in range(0, h - 1, 2):
        a, b = f[y * w:(y + 1) * w], f[(y + 1) * w:(y + 2) * w]
        out += bytes((a[x] + a[x + 1] + b[x] + b[x + 1]) >> 2 for x in range(0, w - 1, 2))
    return bytes(out)


def contact_sheet(path, columns, w, h, gap=6):
    """One row per buffer, one column per camera, half resolution."""
    hw, hh = w // 2, h // 2
    sw = hw * len(columns) + gap * (len(columns) - 1)
    white = b"\xff"
    sheet = bytearray()
    for row in zip(*columns):
        small = [half(f, w, h) for f in row]
        for y in range(hh):
            sheet += (white * gap).join(s[y * hw:(y + 1) * hw] for s in small)
        sheet += white * (sw * gap)
    write_png(path, sw, len(columns[0]) * (hh + gap), bytes(sheet))


def grid_sheet(path, frames, w, h, cols=3, gap=4):
    """Frames at half size, left to right then top to bottom."""
    hw, hh = w // 2, h // 2
    small = [half(f, w, h) for f in frames]
    small += [bytes(hw * hh)] * (-len(small) % cols)
    sw = cols * hw + (cols - 1) * gap
    white = b"\xff"
    px = bytearray()
    for r in range(len(small) // cols):
        for y in range(hh):
            px += (white * gap).join(s[y * hw:(y + 1) * hw] for s in small[r * cols:(r + 1) * cols])
        px += white * (sw * gap)
    write_png(path, sw, len(small) // cols * (hh + gap), bytes(px))


def motion(frames, w):
    """Mean abs difference from the previous frame, on a sparse pixel sample."""
    sig = [f[::w // 4 + 3] for f in frames]
    return [0] + [sum(abs(x - y) for x, y in zip(sig[i - 1], sig[i])) // len(sig[i])
                  for i in range(1, len(sig))]


def capture(idxs, cams, wait_shot, tag):
    lay = device_layout()
    w, h = lay["w"], lay["h"]
    print(f"{lay['board']} ({lay['mode']}): {w}x{h} @ {lay['fps']:.0f} fps, "
          f"{len(idxs)} of {lay['nbuf']} buffers, cam {'+'.join(map(str, cams))}")
    if wait_shot:
        print("waiting for a shot (Ctrl+C to cancel)...")
    columns = read_frames(lay, idxs, cams, wait_shot)

    out_dir = os.path.join(SHOTS_DIR, time.strftime("%Y%m%d_%H%M%S") + f"_{lay['board']}_{tag}")
    base, n = out_dir, 1
    while os.path.exists(out_dir):  # e.g. two captures of the same shot
        n += 1
        out_dir = f"{base}_{n}"
    os.makedirs(out_dir)
    for cam, frames in zip(cams, columns):
        for i, f in zip(idxs, frames):
            write_pgm(os.path.join(out_dir, f"cam{cam}_{i:03d}.pgm"), w, h, f)
    if len(idxs) <= SNAPSHOT_MIN:
        contact_sheet(os.path.join(out_dir, "sheet.png"), columns, w, h)
    meta = {**lay, "idxs": idxs, "cams": list(cams), "shot": wait_shot,
            "motion": [motion(frames, w) for frames in columns]}
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh)
    print(f"saved {out_dir}")
    prune_captures()
    return out_dir


# ---------------------------------------------------------------- impact finder

BLOCK = 4  # ball search works on 4x4 block means


def block_means(f, w, h):
    out = []
    for by in range(h // BLOCK):
        colsum = [0] * w
        for k in range(BLOCK):
            colsum = [a + b for a, b in zip(colsum, f[(by * BLOCK + k) * w:(by * BLOCK + k + 1) * w])]
        out.append([sum(colsum[bx * BLOCK:(bx + 1) * BLOCK]) // (BLOCK * BLOCK)
                    for bx in range(w // BLOCK)])
    return out


def find_ball(B, near=None, reach=(12, 3)):
    """Ball = the 3x3-block blob brightest relative to the floor beside it.
    Plain brightness doesn't work: the strobe-lit floor and club highlights
    saturate too. `near` (block coords) limits the search to around the last
    known position, so the club passing through the frame can't steal it."""
    bw, bh = len(B[0]), len(B)
    xs, ys = range(14, bw - 14), range(1, bh - 1)
    if near:
        xs = range(max(14, near[0] - reach[0]), min(bw - 14, near[0] + reach[0] + 1))
        ys = range(max(1, near[1] - reach[1]), min(bh - 1, near[1] + reach[1] + 1))
    best, pos = -1e9, None
    for by in ys:
        pre = [0]
        for v in B[by]:
            pre.append(pre[-1] + v)
        for bx in xs:
            blob = sum(B[y][x] for y in (by - 1, by, by + 1) for x in (bx - 1, bx, bx + 1)) / 9
            floor = (pre[bx - 4] - pre[bx - 14] + pre[bx + 14] - pre[bx + 4]) / 20
            if blob - floor > best:
                best, pos = blob - floor, (bx, by)
    return pos, best


def find_impact_ordered(frames, w, h):
    """Frames in time order -> (contact frame or None, ball rest position in px,
    rest ball contrast vs the floor: ~70-92 for a ball, ~16 for none).

    The ball at rest is found on the per-block minimum of the first 8 frames,
    which keeps static objects (the ball) and drops moving ones (club
    highlights). It is then tracked by its sub-pixel brightness centroid: at
    rest that wanders < 0.5 px, once struck it moves 3+ px per frame. Contact =
    the last frame before the ball is >= 2 px from rest and moving steadily
    away for 3 frames. (Block-level tracking was not enough: light from the
    approaching club shifted the ball's block by one 38 ms before contact.)"""
    first = [block_means(f, w, h) for f in frames[:8]]
    floor_min = [[min(b[y][x] for b in first) for x in range(len(first[0][0]))]
                 for y in range(len(first[0]))]
    (bx, by), contrast = find_ball(floor_min)
    rest_px = [bx * BLOCK + 2, by * BLOCK + 2]

    c = ball_centroid(frames[0], w, h, *rest_px)
    origin = c
    dist = []
    for f in frames:
        c = ball_centroid(f, w, h, int(c[0]), int(c[1]))
        dist.append(((c[0] - origin[0]) ** 2 + (c[1] - origin[1]) ** 2) ** 0.5)
    for t in range(1, len(frames) - 2):
        if dist[t] >= 2 and dist[t] < dist[t + 1] < dist[t + 2]:
            return t - 1, rest_px, contrast
    return None, rest_px, contrast


def ball_centroid(f, w, h, cx, cy, rw=28, rh=14):
    """Brightness-weighted centroid of the pixels well above the local floor
    in a window around (cx, cy): the ball's position to a fraction of a pixel."""
    x0, x1, y0, y1 = max(0, cx - rw), min(w, cx + rw), max(0, cy - rh), min(h, cy + rh)
    vals = sorted(v for y in range(y0, y1) for v in f[y * w + x0:y * w + x1])
    floor, top = vals[len(vals) // 2], vals[-max(1, len(vals) // 50)]
    thr = floor + (top - floor) / 2
    sx = sy = sw = 0
    for y in range(y0, y1):
        row = f[y * w + x0:y * w + x1]
        for i, v in enumerate(row):
            if v > thr:
                sx += (v - thr) * (x0 + i)
                sy += (v - thr) * y
                sw += v - thr
    return (sx / sw, sy / sw) if sw else (cx, cy)


def ring_seam(frames, w):
    """Index of the oldest frame in a ring = right after the biggest jump."""
    sig = [f[::w // 4 + 3] for f in frames]
    jump = [sum(abs(a - b) for a, b in zip(sig[i - 1], sig[i])) for i in range(len(frames))]
    return max(range(len(frames)), key=jump.__getitem__)


def impact(shot_dir=None, pad=30):
    """Find the impact in a full-ring capture (from grab/sample 1) and save the
    `pad` frames before and after it, in time order, as its own capture."""
    shot_dir = shot_dir or latest_capture()
    with open(os.path.join(shot_dir, "meta.json")) as fh:
        meta = json.load(fh)
    w, h, nbuf = meta["w"], meta["h"], meta["nbuf"]
    cams = meta.get("cams", [0, 1])
    if sorted(meta["idxs"]) != list(range(nbuf)):
        sys.exit("impact needs a full-ring capture (grab, or sample 1 without --range)")

    def load(cam, i):
        with open(os.path.join(shot_dir, f"cam{cam}_{i:03d}.pgm"), "rb") as fh:
            return fh.read()[-w * h:]

    t0 = time.time()
    ring = [load(cams[0], i) for i in range(nbuf)]
    oldest = ring_seam(ring, w)
    order = [(oldest + t) % nbuf for t in range(nbuf)]
    t_hit, rest, _ = find_impact_ordered([ring[i] for i in order], w, h)
    if t_hit is None:
        sys.exit(f"no impact found: ball at rest {rest} never moves "
                 f"(oldest buffer {oldest}); was this a shot capture?")
    window = order[max(0, t_hit - pad):t_hit + pad + 1]
    print(f"oldest buffer {oldest}, ball at rest {rest}, "
          f"impact at buffer {order[t_hit]} ({time.time() - t0:.1f}s)")

    out_dir = os.path.join(shot_dir, f"impact_{order[t_hit]:03d}")
    os.makedirs(out_dir, exist_ok=True)
    columns = []
    for cam in cams:
        frames = [load(cam, i) for i in window]
        columns.append(frames)
        for i, f in zip(window, frames):
            write_pgm(os.path.join(out_dir, f"cam{cam}_{i:03d}.pgm"), w, h, f)
    sub = {k: meta[k] for k in ("board", "mode", "w", "h", "stride", "fps", "nbuf")}
    sub.update(idxs=window, cams=cams, shot=True, impact=order[t_hit], pad=pad,
               offset_ms=[round((order.index(i) - t_hit) * 1000 / meta["fps"], 1) for i in window],
               motion=[motion(frames, w) for frames in columns])
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(sub, fh)
    write_sheets(out_dir)
    print(f"saved {len(window)} frames (buffers {window[0]}..{window[-1]}"
          f"{', wrapping' if window[-1] < window[0] else ''}) -> {out_dir}")
    return out_dir


def latest_capture():
    dirs = sorted(glob.glob(os.path.join(SHOTS_DIR, "*", "meta.json")) +
                  glob.glob(os.path.join(SHOTS_DIR, "*", "impact_*", "meta.json")),
                  key=os.path.getmtime)
    if not dirs:
        sys.exit("no captures yet")
    return os.path.dirname(dirs[-1])


def status():
    lay = device_layout()
    print(f"{lay['board']} ({lay['mode']}): {lay['w']}x{lay['h']} @ {lay['fps']:.0f} fps, "
          f"{lay['nbuf']} buffers/camera, slot stride {lay['stride']:#x}")
    print(shell(f"logread | grep -E 'grab_st|{SHOT_LINE}' | tail -4"))


# ---------------------------------------------------------------- viewer

class Viewer:
    """Tk window showing one capture; load() swaps in another (used by live)."""

    def __init__(self, title="Omni frames"):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg="#111")
        self.meta, self.dir, self.labels, self.imgs = None, None, [], {}
        self.state = {"i": 0, "play": False}
        self.frame = tk.Frame(self.root, bg="#111")
        self.frame.grid(row=0, column=0, padx=6, pady=3)
        self.info = tk.Label(self.root, fg="#ddd", bg="#111", font=("Consolas", 10), anchor="w")
        self.info.grid(row=1, column=0, sticky="we", padx=6)
        self.scale = tk.Scale(self.root, from_=0, to=1, orient="horizontal", length=1000,
                              showvalue=False, command=lambda v: self.show(int(v)), bg="#111",
                              fg="#ddd", highlightthickness=0)
        self.scale.grid(row=2, column=0, padx=6, sticky="we")
        bar = tk.Frame(self.root, bg="#111")
        bar.grid(row=3, column=0, pady=4)
        for text, fn in [("<<", lambda: self.step(-10)), ("<", lambda: self.step(-1)),
                         ("Play/Pause", self.toggle), (">", lambda: self.step(1)),
                         (">>", lambda: self.step(10)), ("Impact", self.goto_impact),
                         ("Save PNG", self.save_png)]:
            tk.Button(bar, text=text, command=fn, width=10).pack(side="left", padx=2)
        self.speed = tk.Scale(bar, from_=1, to=60, orient="horizontal", label="playback fps",
                              length=140, bg="#111", fg="#ddd", highlightthickness=0)
        self.speed.set(15)
        self.speed.pack(side="left", padx=8)
        self.status = tk.Label(self.root, fg="#8cf", bg="#111", font=("Consolas", 10), anchor="w",
                               justify="left")
        self.status.grid(row=4, column=0, sticky="we", padx=6, pady=(0, 4))
        self.root.bind("<Left>", lambda e: self.step(-1))
        self.root.bind("<Right>", lambda e: self.step(1))
        self.root.bind("<space>", lambda e: self.toggle())

    def set_status(self, text):
        self.status.configure(text=text)

    def load(self, shot_dir):
        with open(os.path.join(shot_dir, "meta.json")) as fh:
            self.meta = json.load(fh)
        self.dir, self.imgs = shot_dir, {}
        cams = self.meta.get("cams", [0, 1])
        for lbl in self.labels:
            lbl.destroy()
        self.labels = [self.tk.Label(self.frame, bg="#111") for _ in cams]
        for row, lbl in enumerate(self.labels):
            lbl.grid(row=row, column=0, pady=3)
        self.scale.configure(to=len(self.meta["idxs"]) - 1)
        self.root.title(f"Omni frames - {os.path.basename(shot_dir)}")
        self.goto_impact()

    def image(self, cam, idx):
        key = (cam, idx)
        if key not in self.imgs:
            self.imgs[key] = self.tk.PhotoImage(
                file=os.path.join(self.dir, f"cam{cam}_{idx:03d}.pgm"))
        return self.imgs[key]

    def show(self, i):
        if not self.meta:
            return
        meta, n = self.meta, len(self.meta["idxs"])
        self.state["i"] = i % n
        idx = meta["idxs"][self.state["i"]]
        cams = meta.get("cams", [0, 1])
        for lbl, cam in zip(self.labels, cams):
            lbl.configure(image=self.image(cam, idx))
        when = (f"t = {meta['offset_ms'][self.state['i']]:+6.1f} ms from impact"
                + ("  <<< IMPACT" if idx == meta["impact"] else "")
                if "offset_ms" in meta else f"buffer spacing = {1000.0 / meta['fps']:.2f} ms")
        self.info.configure(text=f"buffer {idx:3d}  ({self.state['i'] + 1}/{n})   {when}")
        if self.scale.get() != self.state["i"]:
            self.scale.set(self.state["i"])

    def goto_impact(self):
        if self.meta:
            imp = self.meta.get("impact")
            self.show(self.meta["idxs"].index(imp) if imp in self.meta["idxs"] else 0)

    def step(self, d):
        self.state["play"] = False
        self.show(self.state["i"] + d)

    def tick(self):
        if self.state["play"]:
            self.show(self.state["i"] + 1)
            self.root.after(int(1000 / self.speed.get()), self.tick)

    def toggle(self):
        self.state["play"] = not self.state["play"]
        if self.state["play"]:
            self.tick()

    def save_png(self):
        if not self.meta:
            return
        idx = self.meta["idxs"][self.state["i"]]
        out = os.path.join(self.dir, f"buffer_{idx:03d}")
        for cam in self.meta.get("cams", [0, 1]):
            self.image(cam, idx).write(f"{out}_cam{cam}.png", format="png")
        self.set_status(f"saved {out}_cam*.png")


def view(shot_dir=None):
    v = Viewer()
    v.load(shot_dir or latest_capture())
    v.root.mainloop()


def shot_once(cams, pad):
    lay = device_layout()
    cap = ShotCapture(lay, cams)
    print(f"{lay['board']}: armed; hit a shot (Ctrl+C to cancel)...")
    try:
        cap.wait_shot()
        print("shot detected, fetching...")
        res = cap.result()
    finally:
        cap.cancel()
    try:
        out_dir, summary, notes = process_shot(lay, res, pad)
    except NoBall as e:
        sys.exit(str(e))
    print(summary, sqocam_result(), *notes, f"saved {out_dir}", sep="\n")
    return out_dir


def shot_loop(lay, cams, pad, emit, current):
    """Arm, wait for a shot, fetch, process; repeat until current["stop"].
    Reports through emit(kind, ...): ("status", text) and
    ("shot", out_dir, summary, t_shot, sqocam_text)."""
    while not current["stop"]:
        try:
            cap = current["cap"] = ShotCapture(lay, cams)
            emit("status", "armed: hit a shot")
            cap.wait_shot()
            if current["stop"]:
                break
            emit("status", "shot! fetching frames...")
            res = cap.result()
            cap.cancel()
            out_dir, summary, notes = process_shot(lay, res, pad)
            info = sqocam_result()  # logged ~0.5 s after the shot; long done by now
            emit("shot", out_dir, summary + "".join("   | " + n for n in notes),
                 res["t_shot"], info)
            write_sheets(out_dir)  # after display; not on the critical path
        except NoBall as e:
            emit("status", f"{e}; re-armed")
        except Exception as e:  # noqa: BLE001 - keep the loop alive, show the error
            if current["stop"]:
                break
            traceback.print_exc()
            emit("status", f"error: {e}; re-arming")
            time.sleep(1)
        finally:
            if current["cap"]:
                current["cap"].cancel()


def live(cams, pad):
    """Viewer that re-arms after every shot and shows each one as it lands."""
    lay = device_layout()
    v = Viewer(f"Omni live - {lay['board']}")
    q = queue.Queue()
    current = {"cap": None, "stop": False}
    shown = {"last": [], "state": ""}

    def emit(*msg):
        print(*msg[1:3], flush=True)
        q.put(msg)

    def worker():
        shot_loop(lay, cams, pad, emit, current)

    def pump():
        try:
            while True:
                msg = q.get_nowait()
                if msg[0] == "status":
                    shown["state"] = msg[1]
                else:
                    _, out_dir, summary, t_shot, info = msg
                    v.load(out_dir)
                    shown["last"] = [f"last shot: {summary}; on screen "
                                     f"{time.time() - t_shot:.1f}s after shot"] + ([info] if info else [])
                v.set_status("\n".join(shown["last"] + [shown["state"]]))
        except queue.Empty:
            pass
        v.root.after(50, pump)

    def close():
        current["stop"] = True
        if current["cap"]:
            current["cap"].cancel()
        v.root.destroy()

    v.root.protocol("WM_DELETE_WINDOW", close)
    threading.Thread(target=worker, daemon=True).start()
    if os.path.isdir(SHOTS_DIR) and glob.glob(os.path.join(SHOTS_DIR, "*", "meta.json")):
        try:
            v.load(latest_capture())
        except (OSError, ValueError, KeyError):
            pass
    pump()
    v.root.mainloop()


# ---------------------------------------------------------------- OBS overlay

OVERLAY_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Omni shot overlay</title>
<style>
  html, body { margin: 0; background: transparent; overflow: hidden; }
  #wrap { position: relative; opacity: 0; transform: translateY(12px);
          transition: opacity var(--fade), transform var(--fade); }
  #wrap.on { opacity: 1; transform: none; }
  canvas { display: block; width: 100vw; height: auto; }
  #info { font: 600 2.2vw system-ui, sans-serif; color: #fff; text-shadow: 0 1px 4px #000;
          padding: .4em .6em; position: absolute; left: 0; bottom: 0; }
</style></head>
<body><div id="wrap"><canvas id="c"></canvas><div id="info"></div></div>
<script>
// URL options: fps (playback speed, default 30), loops (2), gap (ms between loops, 250),
// fade (ms, 300), gain (brightness multiplier, 1), info=1 (show sqocam's numbers)
const P = new URLSearchParams(location.search);
const FPS = +P.get('fps') || 30, LOOPS = +P.get('loops') || 2, GAP = +(P.get('gap') ?? 250),
      FADE = +P.get('fade') || 300, GAIN = +P.get('gain') || 1, INFO = P.get('info') === '1';
document.documentElement.style.setProperty('--fade', FADE + 'ms');
const wrap = document.getElementById('wrap'), cv = document.getElementById('c'),
      ctx = cv.getContext('2d'), infoEl = document.getElementById('info');
const lut = new Uint32Array(256);
for (let i = 0; i < 256; i++) { const v = Math.min(255, Math.round(i * GAIN));
  lut[i] = 0xff000000 | (v << 16) | (v << 8) | v; }
let token = 0;

async function show(s) {
  const my = ++token;
  const buf = new Uint8Array(await (await fetch('/shot/' + s.id + '.bin')).arrayBuffer());
  if (my !== token) return;                      // a newer shot arrived meanwhile
  cv.width = s.w; cv.height = s.h;
  const img = ctx.createImageData(s.w, s.h), px = new Uint32Array(img.data.buffer);
  const size = s.w * s.h, frameMs = 1000 / FPS, loopMs = s.n * frameMs + GAP;
  const draw = f => { const o = f * size; for (let i = 0; i < size; i++) px[i] = lut[buf[o + i]];
                      ctx.putImageData(img, 0, 0); };
  draw(0);
  infoEl.textContent = INFO ? (s.info || '') : '';
  wrap.classList.add('on');
  // Timer-driven from wall-clock time (not requestAnimationFrame, which stalls
  // when the page isn't being painted), so the hide always happens on schedule.
  const t0 = performance.now() + FADE;           // start playing once faded in
  let last = -1;
  const tick = setInterval(() => {
    if (my !== token) return clearInterval(tick);
    const t = Math.max(0, performance.now() - t0), loop = Math.floor(t / loopMs);
    if (loop >= LOOPS) { clearInterval(tick); wrap.classList.remove('on'); return; }
    const f = Math.min(s.n - 1, Math.floor((t - loop * loopMs) / frameMs));
    if (f !== last) { draw(f); last = f; }
  }, Math.max(4, frameMs / 2));
}

const es = new EventSource('/events');
es.onmessage = e => show(JSON.parse(e.data));
</script></body></html>
"""


class Overlay:
    """Latest shot, served to the OBS browser source: frames as raw 8-bit gray
    (all cameras stacked vertically per frame), announced over server-sent events."""

    def __init__(self):
        self.cond = threading.Condition()
        self.seq = 0          # bumps on every publish; clients play when it changes
        self.shots = {}       # seq -> (header dict, frame bytes); last few kept
        self.current = None

    def publish(self, header, data, announce=True):
        with self.cond:
            if announce:
                self.seq += 1
            seq = self.seq
            header = {**header, "id": seq}
            self.shots[seq] = (header, data)
            for old in [k for k in self.shots if k < seq - 3]:
                del self.shots[old]
            self.current = seq
            self.cond.notify_all()

    def replay(self):
        with self.cond:
            if self.current is None:
                return False
            header, data = self.shots[self.current]
        self.publish(header, data)
        return True


def overlay_payload(out_dir, info=""):
    """(header, bytes) for a saved clip: frames in time order, cams stacked."""
    with open(os.path.join(out_dir, "meta.json")) as fh:
        meta = json.load(fh)
    w, h, cams = meta["w"], meta["h"], meta.get("cams", [0, 1])
    parts = []
    for b in meta["idxs"]:
        for cam in cams:
            with open(os.path.join(out_dir, f"cam{cam}_{b:03d}.pgm"), "rb") as fh:
                parts.append(fh.read()[-w * h:])
    header = {"w": w, "h": h * len(cams), "n": len(meta["idxs"]),
              "impact": meta["idxs"].index(meta["impact"]) if meta.get("impact") in meta["idxs"] else 0,
              "info": info, "name": os.path.basename(out_dir)}
    return header, b"".join(parts)


def serve_overlay(ov, port):
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self.send(200, "text/html; charset=utf-8", OVERLAY_PAGE.encode())
            if path == "/replay":
                ok = ov.replay()
                return self.send(200 if ok else 404, "text/plain",
                                 b"replaying last shot\n" if ok else b"no shot yet\n")
            m = re.fullmatch(r"/shot/(\d+)\.bin", path)
            if m:
                with ov.cond:
                    shot = ov.shots.get(int(m[1]))
                if not shot:
                    return self.send(404, "text/plain", b"gone\n")
                return self.send(200, "application/octet-stream", shot[1])
            if path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with ov.cond:
                    seen = ov.seq  # don't replay the last shot when OBS (re)loads the page
                try:
                    while True:
                        with ov.cond:
                            ov.cond.wait_for(lambda s=seen: ov.seq != s, timeout=15)
                            fresh = ov.seq != seen
                            seen = ov.seq
                            header = ov.shots[seen][0] if fresh else None
                        msg = f"data: {json.dumps(header)}\n\n" if fresh else ": ping\n\n"
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                except OSError:
                    return  # client went away
            return self.send(404, "text/plain", b"not found\n")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def obs(cams, pad, port):
    """Headless shot loop feeding an OBS browser-source overlay."""
    lay = device_layout()
    ov = Overlay()
    serve_overlay(ov, port)
    try:  # last saved shot, so /replay works for positioning before any new shot
        ov.publish(*overlay_payload(latest_capture()), announce=False)
    except (SystemExit, OSError, ValueError, KeyError):
        pass
    url = f"http://localhost:{port}/"
    print(f"OBS overlay: add a Browser Source with URL {url}\n"
          f"  size {lay['w']}x{lay['h'] * len(cams)} (or any size with that aspect ratio)\n"
          f"  options: {url}?fps=30&loops=2&gain=1.5&info=1\n"
          f"  re-show the last shot (for positioning): {url}replay\n"
          f"Ctrl+C to stop.", flush=True)
    current = {"cap": None, "stop": False}

    def emit(kind, *a):
        if kind == "status":
            print(a[0], flush=True)
        else:
            out_dir, summary, t_shot, info = a
            ov.publish(*overlay_payload(out_dir, info))
            print(f"{summary}; on overlay {time.time() - t_shot:.1f}s after shot", flush=True)
            if info:
                print(info, flush=True)

    try:
        shot_loop(lay, cams, pad, emit, current)
    except KeyboardInterrupt:
        pass
    finally:
        current["stop"] = True
        if current["cap"]:
            current["cap"].cancel()


# ---------------------------------------------------------------- main

VERSION = "0.3.0"
MODE_TEXT = {
    "obs": "OBS overlay", "live": "live viewer", "shot": "single shot", "view": "viewer",
    "status": "status", "sample": "sample", "grab": "full grab", "impact": "impact finder",
}


def paint(code, s):
    """ANSI colour on a real terminal, plain text when redirected."""
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def banner(cmd):
    """Startup header."""
    if os.name == "nt":
        os.system("")  # turns on ANSI escape handling in the classic Windows console
    try:
        sys.stdout.reconfigure(errors="replace")  # never crash on a non-UTF-8 console
    except (AttributeError, ValueError):
        pass
    title = f"◉  square_impact  v{VERSION}"
    tagline = "   instant high-speed replays from the Square Omni"
    width = max(len(title), len(tagline)) + 4
    edge = paint("2", "│")
    print(paint("2", "╭" + "─" * width + "╮"))
    print(f"{edge}  {paint('1;92', title)}{' ' * (width - len(title) - 2)}{edge}")
    print(f"{edge}  {tagline}{' ' * (width - len(tagline) - 2)}{edge}")
    print(paint("2", "╰" + "─" * width + "╯"))
    print(paint("2", f"  mode: {MODE_TEXT.get(cmd, cmd)}  ·  read-only  ·  Ctrl+C to stop"))
    print(flush=True)


# ---------------------------------------------------------------- update check

REPO = "moexx399/square_impact"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
UPDATE_CHECK_EVERY = 24 * 3600  # seconds between checks (GitHub allows 60 requests/hour)
UPDATE_CACHE = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "square_impact",
                            "update_check.json")


def parse_version(s):
    """'v1.2.3' / '1.2.3' / '1.2.3-rc1' -> (1, 2, 3); None if it isn't a version."""
    m = re.match(r"\s*v?(\d+)\.(\d+)\.(\d+)", s or "")
    return tuple(int(x) for x in m.groups()) if m else None


def latest_release(use_cache=True, timeout=3):
    """{'tag', 'url'} of the newest published GitHub release, or None if it can't
    be found out (offline, rate-limited, no releases yet). Cached for a day."""
    if use_cache:
        try:
            with open(UPDATE_CACHE) as fh:
                cached = json.load(fh)
            if time.time() - cached["checked"] < UPDATE_CHECK_EVERY:
                return cached if cached.get("tag") else None
        except (OSError, ValueError, KeyError, TypeError):
            pass
    import urllib.request
    req = urllib.request.Request(LATEST_RELEASE_API, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"square_impact/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        result = {"checked": time.time(), "tag": data.get("tag_name", ""),
                  "url": data.get("html_url") or RELEASES_PAGE}
    except OSError as e:  # URLError/HTTPError/timeouts are all OSErrors
        if getattr(e, "code", None) != 404:
            return None   # offline etc.: try again next run
        result = {"checked": time.time(), "tag": "", "url": RELEASES_PAGE}  # no releases yet
    except ValueError:
        return None
    try:
        os.makedirs(os.path.dirname(UPDATE_CACHE), exist_ok=True)
        with open(UPDATE_CACHE, "w") as fh:
            json.dump(result, fh)
    except OSError:
        pass
    return result if result["tag"] else None


def update_notice(release):
    """One-line notice if `release` is newer than this version, else None."""
    if not release:
        return None
    new, ours = parse_version(release["tag"]), parse_version(VERSION)
    if new and ours and new > ours:
        return f"Update available: {VERSION} -> {release['tag'].lstrip('v')}   {release['url']}"
    return None


def update_check_enabled(opt_out):
    """On for the packaged exe (source users update with git), unless opted out."""
    return FROZEN and not opt_out and not os.environ.get("SQUARE_IMPACT_NO_UPDATE_CHECK")


def start_update_check():
    """Check in the background so startup is never delayed; print if newer."""
    def run():
        notice = update_notice(latest_release())
        if notice:
            print(paint("1;93", f"  ⬆ {notice}"), flush=True)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["obs", "live", "shot", "status", "sample", "grab", "view",
                                    "impact", "version"])
    ap.add_argument("arg", nargs="?", help="sample: step N; view/impact: capture dir")
    ap.add_argument("--shot", action="store_true", help="sample/grab: wait for the next shot")
    ap.add_argument("--range", metavar="A:B", help="sample: only buffers A..B-1 (e.g. 120:150)")
    ap.add_argument("--cam", choices=["0", "1", "top", "bottom", "both"], default=None,
                    help="0/top (upper, tilted down) or 1/bottom (lower, level); "
                         "default top for live/shot, both otherwise")
    ap.add_argument("--pad", type=int, default=30, help="frames kept before/after impact (30)")
    ap.add_argument("--port", type=int, default=8765, help="obs: overlay web server port (8765)")
    ap.add_argument("--no-view", action="store_true", help="don't open the viewer afterwards")
    ap.add_argument("--no-update-check", action="store_true",
                    help="don't check GitHub for a newer release (exe only; also "
                         "SQUARE_IMPACT_NO_UPDATE_CHECK=1)")
    a = ap.parse_args()
    if a.cmd == "version":
        return version_info()
    banner(a.cmd)
    upd = start_update_check() if update_check_enabled(a.no_update_check) else None
    try:
        return run(a)
    finally:
        if upd:
            upd.join(timeout=2)  # let a short command (status, view) still show the notice


def run(a):
    global ADB
    if a.cmd == "view":
        return view(a.arg)
    if a.cmd == "impact":
        d = impact(a.arg, a.pad)
        return None if a.no_view else view(d)
    ADB = find_adb()
    cam_map = {"0": [0], "top": [0], "1": [1], "bottom": [1], "both": [0, 1]}
    if a.cmd in ("obs", "live", "shot"):
        cams = cam_map[a.cam or "top"]
        if a.cmd == "obs":
            return obs(cams, a.pad, a.port)
        if a.cmd == "live":
            return live(cams, a.pad)
        d = shot_once(cams, a.pad)
        return None if a.no_view else view(d)
    if a.cmd == "status":
        return status()
    cams = cam_map[a.cam or "both"]
    nbuf = device_layout()["nbuf"]
    lo, hi = (int(x) for x in a.range.split(":")) if a.range else (0, nbuf)
    cam_tag = "" if len(cams) == 2 else f"_cam{cams[0]}"
    if a.cmd == "sample":
        step = int(a.arg or 10)
        tag = f"every{step}" + (f"_{lo}-{hi}" if a.range else "") + cam_tag
        d = capture(list(range(lo, min(hi, nbuf), step)), cams, a.shot, tag)
    else:
        d = capture(list(range(nbuf)), cams, a.shot, "all" + cam_tag)
    if a.shot and not a.range and (a.cmd == "grab" or step == 1):
        d = impact(d, a.pad)  # full ring of a shot: cut out the frames around impact
    if not a.no_view:
        view(d)


if __name__ == "__main__":
    main()
