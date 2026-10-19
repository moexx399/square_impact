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
    square_impact.exe update [--force]            # replace the exe with the latest GitHub release

Only the newest KEEP_CAPTURES (5) capture folders in shots/ are kept.
"""

import argparse
import collections
import functools
import glob
import json
import math
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
_CHILDREN = []   # adb clients we started (each holds a shell session on the Omni)


def spawn(args, **kw):
    """Popen that's stopped when square_impact exits (see stop_children_at_exit)."""
    p = subprocess.Popen(args, **kw)
    _CHILDREN.append(p)
    return p


def stop_children_at_exit():
    """Make sure no adb client (and so no shell session on the Omni) outlives us.

    On Windows a program's child processes keep running after it exits, so a
    closed window used to leave adb clients holding sessions open on the Omni.
    The process joins a job object that kills its children when it ends, however
    it ends; atexit also stops them on a normal exit. The shared adb server is
    started first so it isn't one of them."""
    import atexit
    subprocess.run([ADB, "start-server"], capture_output=True)

    def stop_all():
        for c in _CHILDREN:
            if c.poll() is None:
                c.kill()
    atexit.register(stop_all)
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    info.Basic.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    handle = wintypes.HANDLE(job)
    ok = (job and k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info))
          and k32.AssignProcessToJobObject(handle, wintypes.HANDLE(k32.GetCurrentProcess())))
    if ok:
        globals()["_JOB"] = job   # the handle must stay open for our whole lifetime


SERIAL = None  # default device for single-board commands (set when several are connected)


def adb_args(serial, *args):
    """adb command line, addressed to one device when a serial is given."""
    serial = serial or SERIAL
    return [ADB] + (["-s", serial] if serial else []) + list(args)


def shell(cmd, serial=None):
    out = subprocess.run(adb_args(serial, "shell", cmd), capture_output=True, check=True).stdout
    return out.decode("latin-1").replace("\r", "")


BOARD_NAMES = {"r1": "launch", "r2": "spin"}
_board_of_serial = {}  # serial -> hostname, cached (a serial's board never changes)


def connected_boards():
    """{'r1': serial, 'r2': serial} for the Omni camera boards adb sees right now.
    Each board has its own USB port, so either or both can be connected."""
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    boards = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        serial = parts[0]
        if serial not in _board_of_serial:
            try:
                _board_of_serial[serial] = shell("hostname", serial).strip()
            except (OSError, subprocess.CalledProcessError):
                continue
        if _board_of_serial[serial] in BOARD_MODE:
            boards[_board_of_serial[serial]] = serial
    return boards


# Follow the device log by *name*, surviving rotation. On the Omni, `logread`
# is only a wrapper script around `tail -f /var/log/messages`, and that file is
# rotated every ~25 min (ledmgr floods it). BusyBox tail -f keeps reading the
# renamed old file, so after the first rotation no shot was ever seen again.
# This restarts tail whenever the file's inode changes, reading the new file
# from its start so lines written during the switch aren't lost. When nobody is
# reading any more, tail dies on its next write (the log is written every
# second) and the follower exits too (`kill -0` only tests that tail is alive).
# The only process it ever stops is its own tail.
FOLLOW_LOG = (
    "( f=/var/log/messages; n=0; while :; do ino=$(ls -i $f 2>/dev/null); "
    "tail -n $n -f $f 2>/dev/null & tp=$!; n=+1; "
    'while [ "$(ls -i $f 2>/dev/null)" = "$ino" ] && kill -0 $tp 2>/dev/null; '
    "do sleep 2; done; "
    "kill -0 $tp 2>/dev/null || exit; sleep 1; kill $tp; done )"
)


def shot_trigger(dump):
    """Wrap `dump` so it runs the moment r1 logs the next shot. sqocam stamps
    its lines with uptime ("##T 2609.890"), so lines from before arming are ignored."""
    return log_trigger(SHOT_LINE, dump)


class Stream:
    """One device command whose stdout is: anything, then MARKER + header + EOL,
    then a binary payload. Runs in a background thread.

    Raw bytes come back over the adb pty with CRs inserted before every LF (by
    the pty unless `stty -opost`, and by adb.exe on Windows). The marker line
    ends the same way, so it tells us the exact insertion; since every LF
    carries it, stripping it is lossless."""

    def __init__(self, cmd, need, serial=None):
        self.need = need            # payload bytes, or callable(header) -> bytes
        self.header = self.payload = self.error = None
        self.t_marker = None
        self.p = spawn(adb_args(serial, "shell", cmd), stdout=subprocess.PIPE)
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


def device_layout(serial=None):
    """Frame geometry for a board (default: the SERIAL device), from its sqocam.cfg."""
    board = shell("hostname", serial).strip()
    mode = BOARD_MODE.get(board)
    if not mode:
        sys.exit(f"unknown board '{board}' (expected r1 or r2)")
    cfg = dict(re.findall(r"^(\w+)\s*=\s*(\S+)", shell("cat /tmp/sqocam.cfg", serial), re.M))
    w, h = int(cfg[f"{mode}_CAM_W"]), int(cfg[f"{mode}_CAM_H"])
    stride = 1 << (w * h - 1).bit_length()
    assert (w * h) % DD_BS == 0 and stride % DD_BS == 0
    return {"board": board, "mode": mode, "w": w, "h": h, "stride": stride,
            "fps": float(cfg[f"{mode}_CAM_RFPS"]), "nbuf": int(cfg[f"{mode}_CAM_NBUF"]),
            "serial": serial or SERIAL}


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
# Contact (the frame nearest the ball's launch) lands 4-5 frames before F, so the
# window fetched around F is skewed earlier.
FETCH_BEFORE = 40   # frames fetched before sqocam's event buffer F
FETCH_AFTER = 30    # frames fetched after F
CONTACT_BEFORE_F = 4  # fallback when the ball isn't seen moving
MIN_BALL_CONTRAST = 40  # resting ball scores 70-92 vs floor; empty floor ~16


# ---------------------------------------------------------------- host-side log watching

# The Omni does as little as possible: per board, one adb session follows its log
# (FOLLOW_LOG: a tail plus a rotation check every 2 s) and streams every line to
# the host, which does all the parsing. When a shot is logged the host works out
# which ring buffers it needs and reads exactly those with plain `dd` sessions.
LOG_BACKLOG = 300    # earlier lines sent when a watcher starts (context: r2's frame counter)
LOG_STALL = 40       # seconds without any log line = stalled follower (sqocam logs every 15 s)
SFIND_LINE = "sqp_sfind_main return : 1"


def log_uptime(line):
    """sqocam's uptime stamp ("##T 7617.477 ...") as a float, or None."""
    m = re.search(r"##T (\d+\.\d+)", line)
    return float(m[1]) if m else None


def watcher_cmd():
    """The only standing command on the Omni: print the uptime, then follow the
    log (with LOG_BACKLOG earlier lines as context)."""
    return ("stty -opost; cut -d. -f1 /proc/uptime; "
            + FOLLOW_LOG.replace("n=0;", f"n={LOG_BACKLOG};"))


class LogWatcher:
    """One board's log, followed on the host. Events (dicts) go to .events:
       r1: {"kind": "shot", "F": event buffer, "t": uptime} on 'shot_data return : 1'
       r2: {"kind": "spin", "t": uptime} on 'sqp_sfind_main return : 1'
    Lines logged before the watcher started (the backlog) are kept as context
    but never fire events."""

    def __init__(self, serial):
        self.p = spawn(adb_args(serial, "shell", watcher_cmd()), stdout=subprocess.PIPE)
        self.events = queue.Queue()
        self.lines = collections.deque(maxlen=4000)   # (uptime, line), sqocam lines only
        self.started = None
        self.last_line = time.time()
        self.fire = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        for raw in self.p.stdout:
            line = raw.decode("latin-1").strip()
            self.last_line = time.time()
            if self.started is None:
                if line.isdigit():
                    self.started = int(line)
                continue
            if "sqocam" not in line:
                continue
            t = log_uptime(line)
            self.lines.append((t, line))
            if t is None or t < self.started:
                continue          # backlog: context only
            if "fire[" in line:
                m = re.search(r"fire\[(\d+),", line)
                if m:
                    self.fire = int(m[1])
            elif SHOT_LINE in line and self.fire is not None:
                self.events.put({"kind": "shot", "F": self.fire, "t": t, "at": time.time()})
                self.fire = None
            elif SFIND_LINE in line:
                self.events.put({"kind": "spin", "t": t, "at": time.time()})

    def alive(self):
        return self.thread.is_alive() and self.p.poll() is None

    def stalled(self):
        return time.time() - self.last_line > LOG_STALL

    def history(self):
        return [line for _, line in list(self.lines)]

    def close(self):
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait()


def sqocam_text(lines, since=0.0):
    """The Omni's own measurements (r1's 'sim_carry input' line) logged at or
    after uptime `since`, as text; '' if there aren't any."""
    for line in reversed(lines):
        if "sim_carry input" in line and (log_uptime(line) or 0) >= since:
            vals = dict(re.findall(r"(\w+)\(\(?(-?[\d.]+)\)", line))
            return (f"sqocam: ball speed {vals.get('bs', '?')} m/s, launch {vals.get('ba', '?')} deg, "
                    f"direction {vals.get('bd', '?')} deg, spin {vals.get('srate', '?')} rpm")
    return ""


def log_trigger(pattern, dump):
    """Device-side trigger, used only by the legacy `grab/sample --shot`: run
    `dump` the moment a log line containing `pattern` is written after arming.
    $T is that line's uptime stamp ("7617.477")."""
    return ("U=$(cut -d. -f1 /proc/uptime); "
            f"{FOLLOW_LOG} | while read l; do "
            f'case "$l" in *"{pattern}"*) ;; *) continue;; esac; '
            'T=${l##*##T }; T=${T%% *}; t=${T%%.*}; '
            f'if [ "$t" -ge "$U" ] 2>/dev/null; then {dump}; exit; fi; done')


def snapshot_cmd(lay, cam, start, n):
    """Device command: MARKER line, then buffers start..start+n-1 (wrapping
    around the ring) of one camera, both spans read in parallel. Just `dd`."""
    nb, st = lay["nbuf"], lay["stride"]
    base = FRAME_BASE + cam * nb * st

    def snap(b, k):
        return f"dd if=/dev/mem bs={k * st} count=1 iflag=skip_bytes skip={base + b * st} 2>/dev/null"
    start %= nb
    first = min(n, nb - start)
    body = snap(start, first) if first == n else f"{snap(0, n - first)} | ({snap(start, first)}; cat)"
    return f"stty -opost; echo {MARKER.decode()}; {body}"


def fetch_window(lay, cams, start, n, parts=SESSIONS):
    """Read ring buffers start..start+n-1 (wrapping) of each camera with
    parallel adb sessions. Returns ({cam: [frames in order]}, [buffers])."""
    nb, st, fb = lay["nbuf"], lay["stride"], lay["w"] * lay["h"]
    bounds = [n * k // parts for k in range(parts + 1)]
    jobs = [(cam, o0, o1 - o0, Stream(snapshot_cmd(lay, cam, start + o0, o1 - o0),
                                      (o1 - o0) * st, lay.get("serial")))
            for cam in cams for o0, o1 in zip(bounds, bounds[1:])]
    frames = {cam: [None] * n for cam in cams}
    for cam, o0, k, s in jobs:
        _, data = s.result()
        for j in range(k):
            frames[cam][o0 + j] = data[j * st:j * st + fb]
    return frames, [(start + p) % nb for p in range(n)]


def capture_r1(lay, cams, event):
    """r1: the FETCH_BEFORE/FETCH_AFTER window around sqocam's event buffer F."""
    F = event["F"]
    frames, buffers = fetch_window(lay, cams, F - FETCH_BEFORE, FETCH_BEFORE + 1 + FETCH_AFTER)
    return {"frames": frames, "event": F, "buffers": buffers,
            "t_shot": event["at"], "t_fetched": time.time()}


# ---------------------------------------------------------------- r2 (spin board)

# r2's IR strobe only fires for a short burst after impact (~8 frames at 250 fps),
# so its "clip" is a sharp, clean close-up of the ball leaving the club. Per shot
# it logs (see docs/DEVICE_NOTES.md):
#   <t> <t_us> <frame#> <buffer> ( ...        frame counter, every 2500 frames + at start
#   sqp_grab_stop()                           cameras stop
#   sqp_sfind_main return : 1                 spin analysis done = a real shot
# and keeps the ring for 6-20 s. buffer = frame# % nbuf, so the counter line plus
# the stop time give the newest buffer; the lit burst sits ~44-51 frames before it.
R2_LIT_BEFORE_NEWEST = 48   # centre of the lit burst, in frames before the newest buffer
R2_WINDOW = 64              # frames fetched around it (the whole ring if unknown)
R2_LIT_LEVEL = 150          # a pixel this bright is strobe-lit (the unlit scene sits ~11)
R2_LIT_MIN = 4              # lit pixels (in a 1-in-53 sample) for a frame to count as lit
R2_PAD = 2                  # unlit frames kept either side of the burst


def r2_newest_buffer(log_lines, t_shot, fps, nbuf):
    """Newest ring buffer when r2 stopped for the shot logged at `t_shot`, from
    its frame-counter lines (buffer = frame# % nbuf) and the stop time. None if
    the log doesn't have what's needed."""
    stop = counter = None
    for line in log_lines:
        m = re.search(r"##T ([\d.]+) sqp_grab_stop", line)
        if m and float(m[1]) <= t_shot:
            stop = float(m[1])
    if stop is None:
        return None
    for line in log_lines:
        m = re.search(r"##T ([\d.]+) [\d.]+ (\d+) (\d+) \( ", line)
        if m and float(m[1]) <= stop:
            counter = (float(m[1]), int(m[2]))
    if counter is None:
        return None
    t0, frame0 = counter
    return (frame0 + round((stop - t0) * fps)) % nbuf


def lit_frames(frames):
    """Positions of the strobe-lit frames."""
    return [i for i, f in enumerate(frames)
            if sum(1 for v in f[::53] if v > R2_LIT_LEVEL) >= R2_LIT_MIN]


def capture_r2(lay, cams, event, log_lines):
    """r2: locate the lit burst from the log (host-side), then read that window."""
    nb = lay["nbuf"]
    newest = r2_newest_buffer(log_lines, event["t"], lay["fps"], nb)
    if newest is None:   # fall back to the whole ring, in buffer order
        start, n, ordered = 0, nb, False
    else:
        start, n, ordered = newest - R2_LIT_BEFORE_NEWEST - R2_WINDOW // 2, R2_WINDOW, True
    frames, buffers = fetch_window(lay, cams, start, n)
    return {"frames": frames, "buffers": buffers, "ordered": ordered, "newest": newest,
            "t_shot": event["at"], "t_fetched": time.time()}


def r2_segment(lay, cap):
    """The strobe-lit burst (+-R2_PAD frames) of an r2 capture, as a segment."""
    cams = list(cap["frames"])
    frames, buffers = cap["frames"][cams[0]], cap["buffers"]
    lit = lit_frames(frames)
    if not lit:
        raise NoBall("r2: no strobe-lit frames (no ball in flight)")
    n = len(frames)
    if cap["ordered"]:
        order = list(range(n))
    else:   # whole ring in buffer order: start right after the biggest gap between lit frames
        gaps = [((lit[(i + 1) % len(lit)] - lit[i]) % n, i) for i in range(len(lit))]
        _, i = max(gaps)
        first = lit[(i + 1) % len(lit)]
        order = [(first - R2_PAD + p) % n for p in range(n)]
        lit = [order.index(x) for x in lit]
    lo, hi = max(0, min(lit) - R2_PAD), min(n, max(lit) + R2_PAD + 1)
    pos = order[lo:hi]
    first_lit = min(lit) - lo
    seg = {"board": lay["board"], "cams": cams, "w": lay["w"], "h": lay["h"], "fps": lay["fps"],
           "idxs": [buffers[p] for p in pos], "impact": buffers[pos[first_lit]],
           "offset_ms": [round((k - first_lit) * 1000 / lay["fps"], 1) for k in range(len(pos))],
           "frames": {cam: [cap["frames"][cam][p] for p in pos] for cam in cams},
           "lit": len(lit)}
    summary = (f"r2: {len(lit)} lit frames (buffers {seg['idxs'][0]}..{seg['idxs'][-1]}); "
               f"fetched {n} frames/cam in {cap['t_fetched'] - cap['t_shot']:.1f}s")
    return seg, summary


class NoBall(Exception):
    """sqocam reported a shot, but there's no ball in view around it."""


def r1_segment(lay, cap, pad):
    """Find contact in an r1 capture, reject events with no ball in view, and
    return the contact +-pad frames as a segment."""
    w, h, n = lay["w"], lay["h"], len(cap["buffers"])
    cams = list(cap["frames"])
    t_imp, rest, contrast = find_impact_ordered(cap["frames"][cams[0]], w, h)
    if contrast < MIN_BALL_CONTRAST:
        raise NoBall(f"r1: ignored sqocam event at buffer {cap['event']}: no ball in view "
                     f"(contrast {contrast:.0f} < {MIN_BALL_CONTRAST})")
    notes = []
    if t_imp is None:
        t_imp = FETCH_BEFORE - CONTACT_BEFORE_F  # ball never tracked moving
        notes.append("ball never seen moving; contact estimated from sqocam's event frame")
    lo, hi = max(0, t_imp - pad), min(n, t_imp + pad + 1)
    if hi - lo < 2 * pad + 1:
        notes.append(f"impact {t_imp - FETCH_BEFORE:+d} frames from sqocam's event; "
                     f"clip cut to {hi - lo} frames")
    seg = {"board": lay["board"], "cams": cams, "w": w, "h": h, "fps": lay["fps"],
           "idxs": cap["buffers"][lo:hi], "impact": cap["buffers"][t_imp], "pad": pad,
           "offset_ms": [round((p - t_imp) * 1000 / lay["fps"], 1) for p in range(lo, hi)],
           "frames": {cam: cap["frames"][cam][lo:hi] for cam in cams},
           "sqocam_event": cap["event"], "ball_rest": rest, "ball_contrast": round(contrast)}
    summary = (f"r1: contact at buffer {seg['impact']} (sqocam event {cap['event']}); "
               f"fetched {n} frames/cam in {cap['t_fetched'] - cap['t_shot']:.1f}s")
    return seg, summary, notes


# ---------------------------------------------------------------- shot folders

def save_shot(segments, info=""):
    """Write one shot (one or more board segments, in play order) to shots/."""
    boards = [s["board"] for s in segments]
    out_dir = os.path.join(SHOTS_DIR, time.strftime("%Y%m%d_%H%M%S") + "_shot_" + "+".join(boards))
    base, k = out_dir, 1
    while os.path.exists(out_dir):
        k += 1
        out_dir = f"{base}_{k}"
    os.makedirs(out_dir)
    metas = []
    for s in segments:
        for cam in s["cams"]:
            for b, f in zip(s["idxs"], s["frames"][cam]):
                write_pgm(os.path.join(out_dir, f"{s['board']}_cam{cam}_{b:03d}.pgm"), s["w"], s["h"], f)
        metas.append({k: v for k, v in s.items() if k != "frames"})
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump({"shot": True, "boards": boards, "segments": metas, "sqocam": info}, fh)
    prune_captures()
    return out_dir


def load_shot(shot_dir):
    """Segments of a saved capture, frames included. Also reads the older
    single-board folders (meta with idxs/cams at the top, files camN_BBB.pgm)."""
    with open(os.path.join(shot_dir, "meta.json")) as fh:
        meta = json.load(fh)
    if "segments" in meta:
        metas, prefix = meta["segments"], "{board}_"
    else:
        metas = [{**meta, "board": meta.get("board", "r1"), "cams": meta.get("cams", [0, 1])}]
        prefix = ""
    segs = []
    for m in metas:
        w, h = m["w"], m["h"]
        frames = {}
        for cam in m["cams"]:
            frames[cam] = []
            for b in m["idxs"]:
                name = prefix.format(board=m["board"]) + f"cam{cam}_{b:03d}.pgm"
                with open(os.path.join(shot_dir, name), "rb") as fh:
                    frames[cam].append(fh.read()[-w * h:])
        segs.append({**m, "frames": frames})
    return segs


def prune_captures(keep=KEEP_CAPTURES):
    """Delete all but the newest `keep` capture folders in shots/ (folder names
    start with a YYYYMMDD_HHMMSS timestamp, so name order is time order)."""
    dirs = sorted(d for d in glob.glob(os.path.join(SHOTS_DIR, "*")) if os.path.isdir(d))
    for d in dirs[:-keep]:
        shutil.rmtree(d, ignore_errors=True)


def write_impact_snapshot(out_dir):
    """impact.png: a still of the moment of impact, full frame, one per board
    stacked (launch camera's contact frame, denoised, over the spin camera's
    first strobe-lit frame). Returns its path, or None if no board has one."""
    rows, width = [], 0
    for s in load_shot(out_dir):
        if s.get("impact") not in s["idxs"]:
            continue
        cam = s["cams"][0]
        frames, i = s["frames"][cam], s["idxs"].index(s["impact"])
        if s["board"] == "r1" and DISPLAY_DENOISE:   # average with the frames around contact
            lo = max(0, i - 3)
            f = denoise(frames[lo:i + 4], parallel=False)[i - lo]
        else:
            f = frames[i]
        if s["board"] == "r1" and DISPLAY_ENHANCE:   # as in the overlay and viewers,
            # with the whole clip telling what's still and what's moving
            f = enhance([f], s["w"], s["h"], tone_curves(frames), still_background(frames),
                        parallel=False)[0]
        rows.append((s["w"], s["h"], f))
        width = max(width, s["w"])
    if not rows:
        return None
    gap = 6
    px = bytearray()
    for k, (w, h, f) in enumerate(rows):
        if k:
            px += b"\xff" * (width * gap)
        for y in range(h):
            px += f[y * w:(y + 1) * w] + bytes(width - w)
    path = os.path.join(out_dir, "impact.png")
    write_png(path, width, len(px) // width, bytes(px))
    return path


def write_sheets(out_dir):
    """Contact sheet per board segment and camera, full resolution halved."""
    for s in load_shot(out_dir):
        for cam in s["cams"]:
            name = f"sheet_{s['board']}_cam{cam}.png"
            grid_sheet(os.path.join(out_dir, name), s["frames"][cam], s["w"], s["h"])


# ---------------------------------------------------------------- image output

# Crop for display (OBS overlay, viewers); saved clips stay full-frame.
# "ball" (default): a BALL_CROP window centred on where the ball is at the start
# of the clip, then held still, so the club and ball move through it;
# "follow": the same window panning to keep the ball in the middle of every frame;
# the others are fixed horizontal slices of the launch camera's frame.
CROPS = {"ball": (0.0, 1.0), "follow": (0.0, 1.0), "full": (0.0, 1.0),
         "left": (0.0, 0.5), "right": (0.5, 1.0)}
DISPLAY_CROP = "ball"  # set from --crop
BALL_CROP = (640, 192)  # 10:3 on both boards, so the OBS source never changes size
# The start of a clip is mostly the club still on its way in, and the end the
# ball rolling away; drop these fractions of the frames before showing it
# (saved clips keep every frame).
DISPLAY_TRIM_FRONT = 0.2  # set from --trim-front
DISPLAY_TRIM_BACK = 0.2   # set from --trim-back


def trim_clip(meta, front=0.0, back=0.0):
    """Copy of a clip's meta without its first `front` and last `back`
    fractions of frames (both of the full clip length), never cutting away the
    contact frame. Trims the per-frame lists that go with them."""
    idxs = meta["idxs"]
    n = len(idxs)
    lo = round(n * max(0.0, min(front, 1.0)))
    hi = n - round(n * max(0.0, min(back, 1.0)))
    if meta.get("impact") in idxs:
        imp = idxs.index(meta["impact"])
        lo, hi = min(lo, imp), max(hi, imp + 1)
    if (lo, hi) == (0, n):
        return meta
    out = dict(meta, idxs=idxs[lo:hi])
    if "offset_ms" in meta:
        out["offset_ms"] = meta["offset_ms"][lo:hi]
    if "motion" in meta:
        out["motion"] = [[0] + m[lo + 1:hi] for m in meta["motion"]]
    return out


def crop_columns(w, crop):
    a, b = CROPS[crop]
    return int(w * a), int(w * b)


def crop_window(f, w, h, cx, cy, cw, ch):
    """A cw x ch window of a w x h frame centred on (cx, cy). Horizontally the
    centre is exact: past the frame's left/right edge the window is filled with
    black, so the ball stays in the middle even near an edge of the camera's view.
    Vertically it's kept inside the frame (no black bars above/below)."""
    ch = min(ch, h)
    x0 = round(cx - cw / 2)
    y0 = max(0, min(h - ch, round(cy - ch / 2)))
    a, b = max(0, x0), min(w, x0 + cw)            # part of the window inside the frame
    left, right = bytes(max(0, a - x0)), bytes(max(0, x0 + cw - max(a, b)))
    if a >= b:
        return bytes(cw * ch)
    return b"".join(left + f[(y0 + y) * w + a:(y0 + y) * w + b] + right for y in range(ch))


BLOB_CONTRAST = 60   # a block this much brighter than its row's median is "bright"
BLOB_MIN_BLOCKS = 4  # smallest blob (in 4x4 blocks) that can be the ball


def ball_blob(f, w, h, with_size=False):
    """Centre (x, y) of the ball in one frame, or None if it isn't visible
    (with_size: (x, y, blocks)).

    Blocks much brighter than the typical brightness of their row are grouped
    into connected blobs; the ball is the largest one that's about as wide as it
    is tall or wider (club highlights are thin lines or small marks). Works on
    both the strobe-lit dark scene and the grey floor of brighter rooms."""
    B = block_means(f, w, h)
    bh, bw = len(B), len(B[0])
    hot = []
    for row in B:
        thr = sorted(row)[bw // 2] + BLOB_CONTRAST
        hot.append([v > thr for v in row])
    seen = [[False] * bw for _ in range(bh)]
    best, best_score = None, 0
    for y0 in range(bh):
        for x0 in range(bw):
            if not hot[y0][x0] or seen[y0][x0]:
                continue
            stack, cells = [(y0, x0)], []
            seen[y0][x0] = True
            while stack:
                y, x = stack.pop()
                cells.append((y, x))
                for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                    if 0 <= ny < bh and 0 <= nx < bw and hot[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((ny, nx))
            if len(cells) < BLOB_MIN_BLOCKS:
                continue
            ys, xs = [c[0] for c in cells], [c[1] for c in cells]
            width, height = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            # the ball reads as an ellipse ~1-3.5x wider than tall; club shafts are
            # tall and thin, floor markers and reflections long and flat: not the ball
            if not 0.8 * height <= width <= 4 * height:
                continue
            score = len(cells)
            if score > best_score:
                wsum = sum(B[y][x] for y, x in cells)
                best_score = score
                best = (sum((x * BLOCK + 2) * B[y][x] for y, x in cells) / wsum,
                        sum((y * BLOCK + 2) * B[y][x] for y, x in cells) / wsum, len(cells))
    return best if best is None or with_size else best[:2]


def ball_path(frames, w, h, board=None):
    """Where the ball is in each frame, for the crop to follow, smoothed.
    Frames where it isn't visible (r2's unlit frames, or after it leaves the
    view) are filled in: interpolated between known positions, and held at the
    first/last position before/after them (rather than panning off into black)."""
    if not frames:
        return []
    found = [ball_blob(f, w, h, with_size=True) for f in frames]
    sizes = sorted(b[2] for b in found if b)
    if not sizes:
        return [(w / 2, h / 2)] * len(frames)
    # the ball is about the same size all clip; a much smaller blob (once the ball
    # has left the view: the club's marker or a glint) isn't it
    typical = sizes[len(sizes) // 2]
    pos = [(b[0], b[1]) if b and b[2] >= 0.4 * typical else None for b in found]
    known = [i for i, p in enumerate(pos) if p]
    if not known:
        return [(w / 2, h / 2)] * len(frames)
    filled = []
    for i in range(len(pos)):
        if pos[i]:
            filled.append(pos[i])
        elif i < known[0]:
            filled.append(pos[known[0]])
        elif i > known[-1]:
            filled.append(pos[known[-1]])   # it has left the view: stay where it was last seen
        else:
            a = max(k for k in known if k < i)
            b = min(k for k in known if k > i)
            r = (i - a) / (b - a)
            filled.append(tuple(pos[a][k] + (pos[b][k] - pos[a][k]) * r for k in (0, 1)))
    n = len(filled)   # light, centred smoothing: steadies the pan without lagging the ball
    return [tuple(sum(p[k] for p in filled[max(0, i - 1):i + 2]) / len(filled[max(0, i - 1):i + 2])
                  for k in (0, 1)) for i in range(n)]


def crop_frame(f, w, h, x0, x1):
    """Keep columns x0..x1 of an 8-bit w*h frame."""
    if (x0, x1) == (0, w):
        return f
    return b"".join(f[y * w + x0:y * w + x1] for y in range(h))


def write_pgm(path, w, h, f):
    with open(path, "wb") as fh:
        fh.write(b"P5\n%d %d\n255\n" % (w, h) + f)


def png_bytes(w, h, px, channels=1):
    """Minimal 8-bit PNG encoder (stdlib only): grayscale, RGB or RGBA."""
    row = w * channels
    raw = b"".join(b"\x00" + px[y * row:(y + 1) * row] for y in range(h))
    chunk = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))
    ctype = {1: 0, 3: 2, 4: 6}[channels]
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def write_png(path, w, h, px):
    with open(path, "wb") as fh:
        fh.write(png_bytes(w, h, px))


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
    rest that wanders < 0.5 px, once struck it moves 3+ px per frame (100+ on a
    full swing, when it's picked up again from a whole-frame search). The ball
    has left its spot at the first frame it's >= 2 px from rest and moving
    steadily away for 3 frames; contact_frame() then times the launch from the
    ball's flight and returns the frame nearest it. (Block-level tracking was
    not enough: light from the approaching club shifted the ball's block by one
    38 ms before contact.)"""
    first = [block_means(f, w, h) for f in frames[:8]]
    floor_min = [[min(b[y][x] for b in first) for x in range(len(first[0][0]))]
                 for y in range(len(first[0]))]
    (bx, by), contrast = find_ball(floor_min)
    rest_px = [bx * BLOCK + 2, by * BLOCK + 2]

    c = ball_centroid(frames[0], w, h, *rest_px)
    origin = c
    away = lambda p: ((p[0] - origin[0]) ** 2 + (p[1] - origin[1]) ** 2) ** 0.5
    rest_blob = ball_blob(frames[0], w, h, with_size=True)
    dist = []
    for f in frames:
        c = ball_centroid(f, w, h, int(c[0]), int(c[1]))
        # a struck ball can jump further in one frame than the centroid window
        # reaches; then pick it up again from a search of the whole frame
        b = rest_blob and ball_blob(f, w, h, with_size=True)
        if b and b[2] >= 0.5 * rest_blob[2] and away(b) > away(c) + 6:
            c = ball_centroid(f, w, h, int(b[0]), int(b[1]))
        dist.append(away(c))
    for t in range(1, len(frames) - 2):
        if dist[t] >= 2 and dist[t] < dist[t + 1] < dist[t + 2]:
            return contact_frame(frames, w, h, origin, t), rest_px, contrast
    return None, rest_px, contrast


CONTACT_LEAD = 0.1   # frames: club-ball contact starts ~0.2 ms before the launch extrapolated from flight


def contact_frame(frames, w, h, origin, moved):
    """The frame nearest the moment of contact, given `moved`, the first frame
    the ball is seen off its rest spot.

    Contact falls somewhere in the 2 ms between moved-1 (ball still at rest)
    and `moved`, and a full swing sends the ball 100+ px per frame, so neither
    frame is reliably the closer one. The ball's flight (tracked over the whole
    frame) is extrapolated back to its rest spot to time the launch to a
    fraction of a frame, and the nearest frame is picked. Falls back to moved-1
    when the flight can't be tracked."""
    rest = ball_blob(frames[moved - 1], w, h, with_size=True)
    pts = []
    for t in range(moved, min(len(frames), moved + 6)):
        b = ball_blob(frames[t], w, h, with_size=True)
        if not b or not rest or b[2] < 0.5 * rest[2]:   # gone, or a club glint
            continue
        c = ball_centroid(frames[t], w, h, int(b[0]), int(b[1]))
        d = ((c[0] - origin[0]) ** 2 + (c[1] - origin[1]) ** 2) ** 0.5
        if pts and d <= pts[-1][1]:
            continue   # the ball only moves away; anything else is a mistracked frame
        pts.append((t, d))
    if len(pts) < 2:
        return moved - 1
    mt = sum(p[0] for p in pts) / len(pts)
    md = sum(p[1] for p in pts) / len(pts)
    slope = sum((t - mt) * (d - md) for t, d in pts) / sum((t - mt) ** 2 for t, _ in pts)
    if slope < 1:
        return moved - 1
    launch = mt - md / slope - CONTACT_LEAD
    return max(moved - 2, min(moved, round(launch)))


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
    boards = connected_boards()
    if not boards:
        print("no Omni camera board connected (plug in the launch and/or spin USB port)")
        return
    for b, serial in sorted(boards.items()):
        lay = device_layout(serial)
        print(f"{b} ({BOARD_NAMES[b]}, {lay['mode']}): {lay['w']}x{lay['h']} @ {lay['fps']:.0f} fps, "
              f"{lay['nbuf']} buffers/camera, slot stride {lay['stride']:#x}, adb {serial}")
        print(shell(f"grep sqocam /var/log/messages | grep -E 'grab_st|{SHOT_LINE}|{SFIND_LINE}' | "
                    "sed 's/^.*##T /  /' | tail -4", serial))


# ---------------------------------------------------------------- display pipeline

DISPLAY_DENOISE = True  # set from --no-denoise


_POOL = None


def process_pool():
    """Worker processes for denoising (pure-Python pixel loops; ~1.5 s for a
    clip on one core). Started once, in the background, by obs/live."""
    global _POOL
    if _POOL is None:
        import concurrent.futures
        _POOL = concurrent.futures.ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1))
    return _POOL


def warm_up_pool():
    """Start the worker processes now so the first shot doesn't pay for it."""
    threading.Thread(target=lambda: list(process_pool().map(abs, range(8))), daemon=True).start()


def denoise(frames, radius=3, thr=14, parallel=True):
    """Motion-aware temporal denoise for r1's grainy frames: each pixel is
    averaged with the same pixel in up to `radius` frames either side, but only
    those within `thr` grey levels of it. The still floor gets averaged (noise
    5.8 -> 2.6 grey levels on real shots) while the moving club and ball, whose
    pixels change by far more, are left sharp. Split across worker processes."""
    n = len(frames)
    if parallel and n >= 8:
        try:
            pool = process_pool()
            k = min(8, n)
            bounds = [n * i // k for i in range(k + 1)]
            jobs = [pool.submit(_denoise_span, frames[max(0, lo - radius):min(n, hi + radius)],
                                lo - max(0, lo - radius), hi - lo, radius, thr)
                    for lo, hi in zip(bounds, bounds[1:])]
            return [f for j in jobs for f in j.result()]
        except (OSError, RuntimeError):   # no worker processes available: do it here
            pass
    return _denoise_span(frames, 0, n, radius, thr)


def _denoise_span(frames, first, count, radius, thr):
    """Denoise frames[first:first+count] using the neighbours present in `frames`."""
    out = []
    for t in range(first, first + count):
        cur = frames[t]
        nbrs = [frames[k] for k in range(max(0, t - radius), min(len(frames), t + radius + 1)) if k != t]
        o = bytearray(len(cur))
        for i, v in enumerate(cur):
            s, n = v, 1
            for f in nbrs:
                u = f[i]
                if -thr < u - v < thr:
                    s += u
                    n += 1
            o[i] = (s + n // 2) // n
        out.append(bytes(o))
    return out


# The launch camera's scene is very dark (median ~14/255 on real shots; only the
# ball and the club's edges are bright), so the club's shaft and the room barely
# show. The overlay and viewers lift the shadows and sharpen edges. The club
# itself only shows as its bright edges (its dark body is as dark as the floor),
# so lifting everything alike would bring the turf up to the club: what moves
# gets the full lift, the still scene a gentler one.
DISPLAY_ENHANCE = True  # set from --no-enhance
TONE_MEDIAN = 50        # the curve for moving things puts a clip's median brightness here,
TONE_MEDIAN_STILL = 28  # the one for the still scene (turf, room) here...
TONE_GAMMA_MIN = 0.4    # ...but neither lifts harder than this
MOTION_LO, MOTION_HI = 4, 14   # grey levels off the still background: still below, moving above
SHARPEN_AMOUNT = 1.2    # unsharp mask strength
SHARPEN_THRESHOLD = 3   # grey levels: smaller differences are grain, left alone


def tone_curve(frames, median=TONE_MEDIAN):
    """Lookup table that lifts a dark clip's shadows: from the black point (the
    1st percentile) a gamma curve that puts the clip's median at `median`. White
    stays white, so the ball keeps its shading rather than clipping. It never
    darkens: a clip that's bright already (gamma would be > 1) only gets its
    black point set."""
    sample = sorted(v for f in frames for v in f[::97])
    black, mid = sample[len(sample) // 100], sample[len(sample) // 2]
    if black >= 250:
        return bytes(range(256))
    span = 255 - black
    if mid - black < 1:
        gamma = TONE_GAMMA_MIN
    else:
        gamma = min(1.0, max(TONE_GAMMA_MIN, math.log(median / 255) / math.log((mid - black) / span)))
    return bytes(round(255 * max(0.0, (v - black) / span) ** gamma) for v in range(256))


def tone_curves(frames):
    """(still, moving) lookup tables for a clip: see TONE_MEDIAN_STILL."""
    return tone_curve(frames, TONE_MEDIAN_STILL), tone_curve(frames, TONE_MEDIAN)


def still_background(frames):
    """Per-pixel median over a clip: the scene with the club and the flying
    ball gone (each is at any one spot for only a few frames)."""
    n = len(frames)
    return bytes(sorted(px)[n // 2] for px in zip(*frames))


def enhance(frames, w, h, curves=None, background=None, parallel=True):
    """Sharpen edges, then apply the tone curves: each pixel gets the still
    curve where it matches the clip's still background and the moving one where
    it differs by MOTION_HI or more, blended between. (Sharpening first, so its
    grain threshold is in the sensor's grey levels.) `background` defaults to
    the clip's own; without one (fewer than 3 frames) everything counts as moving."""
    still, moving = curves or tone_curves(frames)
    n = len(frames)
    if background is None and n >= 3:
        background = still_background(frames)
    if parallel and n >= 8:
        try:
            pool = process_pool()
            k = min(8, n)
            bounds = [n * i // k for i in range(k + 1)]
            jobs = [pool.submit(_enhance_span, frames[lo:hi], w, h, background, still, moving)
                    for lo, hi in zip(bounds, bounds[1:])]
            return [f for j in jobs for f in j.result()]
        except (OSError, RuntimeError):   # no worker processes available: do it here
            pass
    return _enhance_span(frames, w, h, background, still, moving)


def _enhance_span(frames, w, h, background, still, moving):
    out = []
    for f in frames:
        s = sharpen(f, w, h)
        if background is None:
            out.append(s.translate(moving))
            continue
        o = bytearray(len(s))
        span = MOTION_HI - MOTION_LO
        for i, (v, raw, b) in enumerate(zip(s, f, background)):
            d = raw - b if raw > b else b - raw   # motion judged on the unsharpened pixel
            if d <= MOTION_LO:
                o[i] = still[v]
            elif d >= MOTION_HI:
                o[i] = moving[v]
            else:
                o[i] = still[v] + (moving[v] - still[v]) * (d - MOTION_LO) // span
        out.append(bytes(o))
    return out


def sharpen(f, w, h, amount=SHARPEN_AMOUNT, thr=SHARPEN_THRESHOLD):
    """Unsharp mask with a threshold: each pixel is pushed away from its 3x3
    neighbourhood mean, but only where it differs from it by more than `thr`,
    so the club's, shaft's and ball's edges get crisper while flat areas'
    grain isn't amplified."""
    if w < 3 or h < 3:
        return f
    sums = []   # horizontal 3-pixel sums per row, edges repeated
    for y in range(h):
        r = f[y * w:(y + 1) * w]
        s = [r[0] * 2 + r[1]]
        s += [r[x - 1] + r[x] + r[x + 1] for x in range(1, w - 1)]
        s.append(r[w - 2] + r[w - 1] * 2)
        sums.append(s)
    out = bytearray(f)
    for y in range(h):
        a, b, c = sums[max(0, y - 1)], sums[y], sums[min(h - 1, y + 1)]
        o = y * w
        for x in range(w):
            v = f[o + x]
            d = v - (a[x] + b[x] + c[x]) / 9
            if d > thr or d < -thr:
                out[o + x] = min(255, max(0, round(v + amount * d)))
    return bytes(out)


def display_segments(shot_dir):
    """A saved shot as shown in the overlay and viewers: per board segment, the
    frames trimmed (r1), denoised and enhanced (r1), cropped (--crop; by default
    a still window centred on the ball's starting position) and with cameras
    stacked. Saved files are never changed."""
    out = []
    for s in load_shot(shot_dir):
        idxs = s["idxs"]
        lo, hi = 0, len(idxs)
        r1 = s["board"] == "r1"
        if r1 and s.get("impact") in idxs:
            t = trim_clip(s, DISPLAY_TRIM_FRONT, DISPLAY_TRIM_BACK)
            lo = idxs.index(t["idxs"][0])
            hi = lo + len(t["idxs"])
        w, h = s["w"], s["h"]
        on_ball = DISPLAY_CROP in ("ball", "follow") and "impact" in s   # raw grab/sample stay whole
        raws = [s["frames"][cam][lo:hi] for cam in s["cams"]]
        # before cropping: denoise needs aligned frames, and sharpening the crop's
        # black padding would draw a line along its edge
        shown = [denoise(raw) if r1 and DISPLAY_DENOISE else raw for raw in raws]
        if r1 and DISPLAY_ENHANCE:
            curves = tone_curves([f for frames in shown for f in frames])   # one pair: cameras match
            shown = [enhance(frames, w, h, curves) for frames in shown]
        per_cam = []
        for raw, frames in zip(raws, shown):
            if on_ball:
                cw, ch = BALL_CROP
                path = ball_path(raw, w, h, s["board"])
                if DISPLAY_CROP == "ball":
                    path = [path[0]] * len(path)   # where the clip starts, then hold still
                frames = [crop_window(f, w, h, x, y, cw, ch) for f, (x, y) in zip(frames, path)]
            else:
                x0, x1 = crop_columns(w, DISPLAY_CROP if r1 and not on_ball else "full")
                cw, ch = x1 - x0, h
                frames = [crop_frame(f, w, h, x0, x1) for f in frames]
            per_cam.append(frames)
        offs = s.get("offset_ms")
        out.append({"board": s["board"], "w": cw, "h": ch * len(s["cams"]),
                    "frames": [b"".join(fs) for fs in zip(*per_cam)],
                    "idxs": idxs[lo:hi], "offset_ms": offs[lo:hi] if offs else None,
                    "impact": idxs[lo:hi].index(s["impact"]) if s.get("impact") in idxs[lo:hi] else None,
                    "fps": s.get("fps")})
    return out


# ---------------------------------------------------------------- logo

def logo_rgba(size):
    """The app icon as RGBA pixels, drawn from code so there's no image file to
    ship: a golf ball and two fading strobe exposures inside orange camera
    viewfinder corners, on a dark rounded square. Edges are antialiased from
    signed distances, so every size is drawn natively rather than scaled."""
    def box(x, y, cx, cy, hw, hh, r=0.0):
        qx, qy = abs(x - cx) - hw + r, abs(y - cy) - hh + r
        return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r

    def cover(d):  # distance (in icon widths) -> pixel coverage
        return min(1.0, max(0.0, 0.5 - d * size))

    def over(c, col, k):  # paint colour col over pixel c with coverage k
        for n in range(3):
            c[n] += (col[n] - c[n]) * k

    m, arm, t = 0.15, 0.17, 0.05      # viewfinder margin, arm length, thickness
    corners = []
    for ex, dx in ((m, 1), (1 - m, -1)):
        for ey, dy in ((m, 1), (1 - m, -1)):
            corners += [(ex + dx * arm / 2, ey + dy * t / 2, arm / 2, t / 2),
                        (ex + dx * t / 2, ey + dy * arm / 2, t / 2, arm / 2)]
    by, br, bx = 0.5, 0.19, 0.62      # ball centre y, radius, centre x
    ghosts = [(0.30, 0.22), (0.45, 0.45)]   # earlier exposures: x, opacity
    dimples = [(bx + 0.075 * i, by + 0.075 * j) for i, j in
               [(-1, -1), (0, -1.15), (1, -1), (-1.15, 0), (0, 0), (1.15, 0), (-1, 1), (0, 1.15), (1, 1)]]
    out = bytearray()
    for j in range(size):
        y = (j + 0.5) / size
        for i in range(size):
            x = (i + 0.5) / size
            alpha = cover(box(x, y, 0.5, 0.5, 0.5, 0.5, 0.22))
            if not alpha:
                out += b"\0\0\0\0"
                continue
            c = [0x16 + (0x0a - 0x16) * y, 0x2c + (0x14 - 0x2c) * y, 0x48 + (0x24 - 0x48) * y]
            over(c, (0xff, 0x8a, 0x1f), cover(min(box(x, y, *b) for b in corners)))
            for gx, opacity in ghosts:
                over(c, (0xbf, 0xd4, 0xea), cover(math.hypot(x - gx, y - by) - br) * opacity)
            k = cover(math.hypot(x - bx, y - by) - br)
            if k:
                shade = min(1.0, math.hypot(x - bx + 0.07, y - by + 0.07) / (br * 1.6))
                over(c, [255 + (v - 255) * shade for v in (0xb4, 0xc0, 0xcf)], k)
                if size >= 48:  # just noise at small sizes
                    for dx, dy in dimples:
                        over(c, (0x9a, 0xa8, 0xb8), cover(math.hypot(x - dx, y - dy) - 0.022) * 0.55)
            out += bytes(round(v) for v in c) + bytes([round(255 * alpha)])
    return bytes(out)


@functools.cache
def logo_png(size):
    return png_bytes(size, size, logo_rgba(size), 4)


ICON_SIZES = (16, 24, 32, 48, 64, 256)


def logo_ico(sizes=ICON_SIZES):
    """Windows .ico with a PNG image per size (the exe's icon, built in CI)."""
    pngs = [logo_png(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = len(head) + 16 * len(pngs)
    entries = b""
    for s, png in zip(sizes, pngs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    return head + entries + b"".join(pngs)


def set_app_id():
    """Without its own app ID, Windows groups our windows under python.exe and
    shows Python's icon in the taskbar. Must run before the first window is
    created. The ID only lives in this process; nothing is registered."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("square_impact")
        except (AttributeError, OSError):
            pass


def set_window_icon(root):
    """Title bar, Alt+Tab and taskbar icon for a Tk window."""
    import tkinter as tk
    root._icons = [tk.PhotoImage(data=logo_png(s), format="PNG") for s in (64, 48, 32, 16)]
    root.iconphoto(True, *root._icons)


# ---------------------------------------------------------------- viewer

class Viewer:
    """Tk window playing one shot (its board segments in sequence); load()
    swaps in another (used by live)."""

    def __init__(self, title="Omni frames"):
        import tkinter as tk
        self.tk = tk
        set_app_id()
        self.root = tk.Tk()
        self.root.title(title)
        set_window_icon(self.root)
        self.root.configure(bg="#111")
        self.segs, self.play, self.dir, self.imgs = [], [], None, {}
        self.state = {"i": 0, "play": False}
        self.label = tk.Label(self.root, bg="#111")
        self.label.grid(row=0, column=0, padx=6, pady=6)
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
        self.segs = display_segments(shot_dir)
        self.play = [(si, fi) for si, s in enumerate(self.segs) for fi in range(len(s["frames"]))]
        self.dir, self.imgs = shot_dir, {}
        self.scale.configure(to=max(0, len(self.play) - 1))
        self.root.title(f"Omni frames - {os.path.basename(shot_dir)}")
        self.goto_impact()

    def image(self, si, fi):
        if (si, fi) not in self.imgs:
            s = self.segs[si]
            pgm = b"P5\n%d %d\n255\n" % (s["w"], s["h"]) + s["frames"][fi]
            self.imgs[(si, fi)] = self.tk.PhotoImage(data=pgm, format="PPM")
        return self.imgs[(si, fi)]

    def show(self, i):
        if not self.play:
            return
        self.state["i"] = i % len(self.play)
        si, fi = self.play[self.state["i"]]
        s = self.segs[si]
        self.label.configure(image=self.image(si, fi))
        ref = "contact" if s["board"] == "r1" else "first lit frame"
        when = (f"t = {s['offset_ms'][fi]:+6.1f} ms from {ref}"
                + ("  <<< IMPACT" if fi == s["impact"] and s["board"] == "r1" else "")
                if s["offset_ms"] else f"buffer {s['idxs'][fi]}")
        self.info.configure(text=f"{s['board']} ({BOARD_NAMES.get(s['board'], '')})  "
                                 f"frame {fi + 1}/{len(s['frames'])}   {when}")
        if self.scale.get() != self.state["i"]:
            self.scale.set(self.state["i"])

    def goto_impact(self):
        for i, (si, fi) in enumerate(self.play):
            if fi == self.segs[si]["impact"]:
                return self.show(i)
        self.show(0)

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
        if not self.play:
            return
        si, fi = self.play[self.state["i"]]
        s = self.segs[si]
        out = os.path.join(self.dir, f"{s['board']}_frame_{fi + 1:03d}.png")
        self.image(si, fi).write(out, format="png")
        self.set_status(f"saved {out}")


def view(shot_dir=None):
    v = Viewer()
    v.load(shot_dir or latest_capture())
    v.root.mainloop()


# ---------------------------------------------------------------- capture loops

PAIR_WINDOW = 8.0   # seconds to wait for the other board's capture of the same shot
SQOCAM_RESULT_WAIT = 1.5  # r1 logs its measurements ~0.5 s after the shot line


def omni_connected(serial=None):
    """True if adb sees that board right now (asks the host's adb server only)."""
    try:
        out = subprocess.run(adb_args(serial, "get-state"), capture_output=True, text=True, timeout=5)
        return out.stdout.strip() == "device"
    except (OSError, subprocess.TimeoutExpired):
        return False


def board_loop(lay, cams, pad, out_q, stop):
    """Capture loop for one board (r1 launch or r2 spin), driven by a LogWatcher.
    Reports to out_q: ("armed"|"fetching", board),
    ("segment", board, seg, summary, notes, t_shot), ("ignored"|"error", board, text),
    ("gone", board).

    Self-healing: the watcher is restarted if its adb session ends or goes
    silent for LOG_STALL seconds (sqocam logs every 15 s, so silence = stalled),
    and the loop exits if the board is gone for 10 s (the manager starts a new
    one when it comes back)."""
    board, serial = lay["board"], lay["serial"]
    gone_since, watcher = None, None
    try:
        while not stop.is_set():
            if watcher is None or not watcher.alive() or watcher.stalled():
                if watcher:
                    watcher.close()
                    watcher = None
                if not omni_connected(serial):
                    gone_since = gone_since or time.time()
                    if time.time() - gone_since > 10:
                        out_q.put(("gone", board))
                        return
                    time.sleep(2)
                    continue
                gone_since = None
                watcher = LogWatcher(serial)
                out_q.put(("armed", board))
            try:
                event = watcher.events.get(timeout=1)
            except queue.Empty:
                continue
            out_q.put(("fetching", board))
            try:
                if board == "r2":
                    seg, summary = r2_segment(lay, capture_r2(lay, cams, event, watcher.history()))
                    notes = []
                else:
                    cap = capture_r1(lay, cams, event)
                    seg, summary, notes = r1_segment(lay, cap, pad)
                    end = time.time() + SQOCAM_RESULT_WAIT
                    while not sqocam_text(watcher.history(), event["t"]) and time.time() < end:
                        time.sleep(0.1)
                    seg["sqocam"] = sqocam_text(watcher.history(), event["t"])
                out_q.put(("segment", board, seg, summary, notes, event["at"]))
            except NoBall as e:
                out_q.put(("ignored", board, str(e)))
            except Exception as e:  # noqa: BLE001 - keep the loop alive, report the error
                if stop.is_set():
                    break
                if omni_connected(serial):
                    traceback.print_exc()
                    out_q.put(("error", board, str(e)))
            out_q.put(("armed", board))
    finally:
        if watcher:
            watcher.close()


def shot_manager(cams, pad, emit, stop, once=False):
    """Capture from whichever camera boards are plugged in. Runs a board_loop
    per connected board (r1 launch, r2 spin), re-checking every few seconds as
    cables come and go, and pairs their captures of the same shot into one
    clip: r1's swing, then r2's close-up of the ball leaving the club.

    Reports through emit(kind, ...): ("status", text) and
    ("shot", out_dir, summary, t_shot, sqocam_text). With once=True, returns
    the folder of the first shot."""
    q = queue.Queue()
    workers, state, serials = {}, {}, {}
    pending, first_at, last_scan = [], None, 0.0

    shown = [None]

    def report():   # only when it changes, so the console isn't flooded
        text = ("   ".join(f"{b} {BOARD_NAMES[b]}: {state.get(b, '')}" for b in sorted(workers))
                or "waiting for the Omni: connect the launch (r1) and/or spin (r2) USB port...")
        if text != shown[0]:
            shown[0] = text
            emit("status", text)

    def finish():
        segs = [p[1] for p in sorted(pending, key=lambda p: p[0])]   # r1 before r2
        info = next((s.get("sqocam") for s in segs if s.get("sqocam")), "")
        out_dir = save_shot(segs, info)
        summary = "  |  ".join(p[2] + "".join("; " + n for n in p[3]) for p in pending)
        emit("shot", out_dir, summary, min(p[4] for p in pending), info)

        def extras():   # not on the critical path: the clip is already showing
            write_impact_snapshot(out_dir)
            emit("snapshot", out_dir)
            write_sheets(out_dir)
        threading.Thread(target=extras, daemon=True).start()
        return out_dir

    while not stop.is_set():
        if time.time() - last_scan > 5:
            last_scan = time.time()
            for b, serial in connected_boards().items():
                if b not in workers or not workers[b].is_alive():
                    lay = device_layout(serial)
                    serials[b] = serial
                    workers[b] = threading.Thread(target=board_loop, args=(lay, cams, pad, q, stop),
                                                  daemon=True)
                    workers[b].start()
                    state[b] = "starting"
            for b in [b for b, t in workers.items() if not t.is_alive()]:
                del workers[b]
                state.pop(b, None)
            report()
        if pending and ({p[0] for p in pending} >= set(workers) or time.time() - first_at > PAIR_WINDOW):
            out_dir = finish()
            pending, first_at = [], None
            if once:
                return out_dir
        try:
            msg = q.get(timeout=0.2)
        except queue.Empty:
            continue
        kind, b = msg[0], msg[1]
        if kind == "armed":
            state[b] = "armed"
        elif kind == "fetching":
            state[b] = "shot! fetching"
        elif kind == "segment":
            pending.append((b, msg[2], msg[3], msg[4], msg[5]))
            first_at = first_at or time.time()
            state[b] = "captured"
        elif kind == "ignored":
            state[b] = "ignored (no ball in view)"
            print(msg[2], flush=True)
        elif kind == "error":
            state[b] = f"error: {msg[2]}"
        elif kind == "gone":
            workers.pop(b, None)
            state.pop(b, None)
        report()
    return None


def shot_once(cams, pad):
    stop = threading.Event()

    def emit(kind, *a):
        if kind == "status":
            print(a[0], flush=True)
        elif kind == "snapshot":
            print(f"impact snapshot: {os.path.join(a[0], 'impact.png')}", flush=True)
        else:
            print(f"{a[1]}\nsaved {a[0]}", flush=True)
    try:
        return shot_manager(cams, pad, emit, stop, once=True)
    finally:
        stop.set()


def live(cams, pad):
    """Viewer that re-arms after every shot and shows each one as it lands."""
    v = Viewer("Omni live")
    q = queue.Queue()
    stop = threading.Event()
    shown = {"last": [], "state": ""}

    def emit(*msg):
        print(*msg[1:3], flush=True)
        q.put(msg)

    def pump():
        try:
            while True:
                msg = q.get_nowait()
                if msg[0] == "status":
                    shown["state"] = msg[1]
                elif msg[0] == "snapshot":
                    pass
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
        stop.set()
        v.root.destroy()

    v.root.protocol("WM_DELETE_WINDOW", close)
    if DISPLAY_DENOISE or DISPLAY_ENHANCE:
        warm_up_pool()
    threading.Thread(target=shot_manager, args=(cams, pad, emit, stop), daemon=True).start()
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
<link rel="icon" type="image/png" href="/favicon.png">
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
// URL options: fps (launch-camera playback speed, default 30), fps2 (spin-camera
// close-up, default 10), hold (ms to freeze on the impact frame, 700; 0 = no
// freeze), loops (2), gap (ms between loops, 250), fade (ms, 300),
// gain (brightness multiplier, 1), info=1 (show sqocam's numbers)
const P = new URLSearchParams(location.search);
const FPS = +P.get('fps') || 30, FPS2 = +P.get('fps2') || 10, LOOPS = +P.get('loops') || 2,
      GAP = +(P.get('gap') ?? 250), FADE = +P.get('fade') || 300, GAIN = +P.get('gain') || 1,
      INFO = P.get('info') === '1', HOLD = +(P.get('hold') ?? 700);
const QUIET_MS = 15000;   // no event or ping for this long (pings come every 5 s): reconnect
document.documentElement.style.setProperty('--fade', FADE + 'ms');
const wrap = document.getElementById('wrap'), cv = document.getElementById('c'),
      ctx = cv.getContext('2d'), infoEl = document.getElementById('info');
const lut = new Uint32Array(256);
for (let i = 0; i < 256; i++) { const v = Math.min(255, Math.round(i * GAIN));
  lut[i] = 0xff000000 | (v << 16) | (v << 8) | v; }
let token = 0;

async function show(s) {
  const my = ++token;
  let buf;
  try {
    const r = await fetch('/shot/' + s.id + '.bin', { cache: 'no-store' });
    if (!r.ok) return;                           // replaced by a newer shot already
    buf = new Uint8Array(await r.arrayBuffer());
  } catch (err) { return; }                      // square_impact went away; the next shot will play
  if (my !== token) return;                      // a newer shot arrived meanwhile
  // One timeline over all segments: the launch camera's swing, then the spin
  // camera's close-up (each with its own size and playback speed).
  const tl = []; let off = 0, t = 0;
  for (const g of s.segments) {
    const ms = 1000 / (g.board === 'r2' ? FPS2 : FPS);
    for (let f = 0; f < g.n; f++) {
      tl.push({ g, o: off + f * g.w * g.h, start: t });
      t += ms + (f === g.impact ? HOLD : 0);    // snapshot: freeze on the moment of impact
    }
    off += g.n * g.w * g.h;
  }
  const loopMs = t + GAP;
  let img = null, px = null;
  const draw = e => {
    if (cv.width !== e.g.w || cv.height !== e.g.h || !img) {
      cv.width = e.g.w; cv.height = e.g.h;
      img = ctx.createImageData(e.g.w, e.g.h); px = new Uint32Array(img.data.buffer);
    }
    const size = e.g.w * e.g.h;
    for (let i = 0; i < size; i++) px[i] = lut[buf[e.o + i]];
    ctx.putImageData(img, 0, 0);
  };
  draw(tl[0]);
  infoEl.textContent = INFO ? (s.info || '') : '';
  wrap.classList.add('on');
  // Timer-driven from wall-clock time (not requestAnimationFrame, which stalls
  // when the page isn't being painted), so the hide always happens on schedule.
  const t0 = performance.now() + FADE;           // start playing once faded in
  let last = null;
  const tick = setInterval(() => {
    if (my !== token) return clearInterval(tick);
    const el = Math.max(0, performance.now() - t0), loop = Math.floor(el / loopMs);
    if (loop >= LOOPS) { clearInterval(tick); wrap.classList.remove('on'); return; }
    const lt = el - loop * loopMs;
    let e = tl[0];
    for (const x of tl) { if (x.start <= lt) e = x; else break; }
    if (e !== last) { draw(e); last = e; }
  }, 8);
}

// New shots arrive over server-sent events. The browser reconnects by itself
// after most drops, but not all (an error reply while square_impact restarts
// closes the stream for good, and a stalled connection never errors), so a
// watchdog reopens it when it's closed or has gone quiet: the server sends a
// ping every few seconds.
let es = null, heard = 0;
function connect() {
  if (es) es.close();
  heard = performance.now();
  es = new EventSource('/events');
  es.onopen = () => { heard = performance.now(); };
  es.addEventListener('ping', () => { heard = performance.now(); });
  es.onmessage = e => { heard = performance.now(); show(JSON.parse(e.data)); };
}
connect();
setInterval(() => {
  if (es.readyState === EventSource.CLOSED || performance.now() - heard > QUIET_MS) connect();
}, 2000);
</script></body></html>
"""


OVERLAY_PING = 5   # seconds between keep-alive pings on /events (the page reconnects after 15 s without)


class Overlay:
    """Latest shot, served to the OBS browser source: frames as raw 8-bit gray,
    announced over server-sent events."""

    def __init__(self):
        self.cond = threading.Condition()
        self.seq = 0          # bumps on every publish; clients play when it changes
        self.shots = {}       # seq -> (header dict, frame bytes); last few kept
        self.current = None

    shot_dir = None   # folder of the latest shot, for /impact.png

    def publish(self, header, data, announce=True, shot_dir=None):
        with self.cond:
            if shot_dir:
                self.shot_dir = shot_dir
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
    """(header, bytes) for a saved shot as displayed (see display_segments):
    each segment's frames in play order, segments one after another."""
    segs = display_segments(out_dir)
    header = {"segments": [{"board": s["board"], "w": s["w"], "h": s["h"], "n": len(s["frames"]),
                            "impact": s["impact"]} for s in segs],
              "info": info, "name": os.path.basename(out_dir)}
    return header, b"".join(b"".join(s["frames"]) for s in segs)


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
            if path in ("/favicon.png", "/favicon.ico"):
                return self.send(200, "image/png", logo_png(32))
            if path == "/impact.png":   # still of the latest shot's moment of impact
                snap = ov.shot_dir and os.path.join(ov.shot_dir, "impact.png")
                if not snap or not os.path.exists(snap):
                    return self.send(404, "text/plain", b"no snapshot yet\n")
                with open(snap, "rb") as fh:
                    return self.send(200, "image/png", fh.read())
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
                    self.wfile.write(b"retry: 2000\n\n")   # browser's reconnect delay after a drop
                    self.wfile.flush()
                    while True:
                        with ov.cond:
                            ov.cond.wait_for(lambda s=seen: ov.seq != s, timeout=OVERLAY_PING)
                            fresh = ov.seq != seen
                            seen = ov.seq
                            header = ov.shots[seen][0] if fresh else None
                        # a named event, not an SSE comment: the page's watchdog has to see it
                        msg = f"data: {json.dumps(header)}\n\n" if fresh else "event: ping\ndata: \n\n"
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
    """Headless capture from the connected boards, feeding an OBS browser-source overlay."""
    ov = Overlay()
    serve_overlay(ov, port)
    if DISPLAY_DENOISE or DISPLAY_ENHANCE:
        warm_up_pool()
    try:  # last saved shot, so /replay works for positioning before any new shot
        last = latest_capture()
        ov.publish(*overlay_payload(last), announce=False, shot_dir=last)
    except (SystemExit, OSError, ValueError, KeyError):
        pass
    url = f"http://localhost:{port}/"
    print(f"OBS overlay: add a Browser Source with URL {url}\n"
          f"  size 1280x384 or 640x192 (10:3; with --crop full on the launch camera, 20:3)\n"
          f"  options: {url}?fps=30&fps2=10&loops=2&gain=1.5&info=1\n"
          f"  re-show the last shot (for positioning): {url}replay\n"
          f"  still of the latest impact: {url}impact.png\n"
          f"Ctrl+C to stop.", flush=True)
    stop = threading.Event()

    def emit(kind, *a):
        if kind == "status":
            print(a[0], flush=True)
        elif kind == "snapshot":
            print(f"impact snapshot: {os.path.join(a[0], 'impact.png')}", flush=True)
        else:
            out_dir, summary, t_shot, info = a
            ov.publish(*overlay_payload(out_dir, info), shot_dir=out_dir)
            print(f"{summary}; on overlay {time.time() - t_shot:.1f}s after shot", flush=True)
            if info:
                print(info, flush=True)

    try:
        shot_manager(cams, pad, emit, stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


# ---------------------------------------------------------------- main

VERSION = "0.12.1"
MODE_TEXT = {
    "obs": "OBS overlay", "live": "live viewer", "shot": "single shot", "view": "viewer",
    "status": "status", "sample": "sample", "grab": "full grab", "impact": "impact finder",
}


def colour_available():
    """True when stdout is a terminal that will show colour: not redirected, not
    NO_COLOR (https://no-color.org), not TERM=dumb, and on Windows a console
    that accepts escape-code processing (older consoles would print the codes
    as text)."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        out, mode = k32.GetStdHandle(-11), wintypes.DWORD()   # STD_OUTPUT_HANDLE
        if not k32.GetConsoleMode(out, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(out, mode.value | 0x0004))   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (AttributeError, OSError):
        return False


def paint(code, s):
    """ANSI colour when the terminal shows it, plain text otherwise."""
    return f"\033[{code}m{s}\033[0m" if colour_available() else s


def logo_lines(size=14):
    """The logo as terminal text: each '▀' cell is two pixels, the top one in the
    foreground colour and the bottom one in the background colour, so it comes
    out square (cells are about twice as tall as wide). 24-bit colour."""
    px = logo_rgba(size)
    pixel = lambda x, y: px[(y * size + x) * 4:(y * size + x) * 4 + 4]
    rgb = lambda c: ";".join(map(str, c))
    lines = []
    for y in range(0, size, 2):
        line = ""
        for x in range(size):
            (*top, ta), (*bot, ba) = pixel(x, y), pixel(x, y + 1)
            if ta < 128 and ba < 128:   # rounded corners: leave the terminal's own background
                line += "\033[0m "
            elif ta < 128:
                line += f"\033[0;38;2;{rgb(bot)}m▄"
            elif ba < 128:
                line += f"\033[0;38;2;{rgb(top)}m▀"
            else:
                line += f"\033[38;2;{rgb(top)};48;2;{rgb(bot)}m▀"
        lines.append(line + "\033[0m")
    return lines


def fancy_terminal():
    """The logo is drawn in colour, so it's only shown when colour is available
    and the console can print the block characters."""
    if not colour_available():
        return False
    try:
        "▀▄╭".encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def banner(cmd):
    """Startup header: the logo (colour terminals only) with the title box beside it."""
    try:
        sys.stdout.reconfigure(errors="replace")  # never crash on a non-UTF-8 console
    except (AttributeError, ValueError):
        pass
    title = f"square_impact  v{VERSION}"
    tagline = "instant high-speed replays from the Square Omni"
    width = max(len(title), len(tagline)) + 4
    edge = paint("2", "│")
    text = [paint("2", "╭" + "─" * width + "╮"),
            f"{edge}  {paint('1;92', title)}{' ' * (width - len(title) - 2)}{edge}",
            f"{edge}  {tagline}{' ' * (width - len(tagline) - 2)}{edge}",
            paint("2", "╰" + "─" * width + "╯"),
            paint("2", f"  mode: {MODE_TEXT.get(cmd, cmd)}  ·  read-only  ·  Ctrl+C to stop")]
    if fancy_terminal():
        logo = logo_lines()
        top = (len(logo) - len(text)) // 2   # centre the text beside the logo
        text = [""] * top + text + [""] * (len(logo) - len(text) - top)
        text = [f"  {art}  {line}" for art, line in zip(logo, text)]
    print("\n".join(text))
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
        return (f"Update available: {VERSION} -> {release['tag'].lstrip('v')}   "
                f"run 'square_impact.exe update' or see {release['url']}")
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


# ---------------------------------------------------------------- self-update

RELEASE_ASSET = "square_impact-windows.zip"
EXE_NAME = "square_impact.exe"


def cleanup_old_exe():
    """Remove the exe left behind by the last `update` (it was still running then)."""
    if FROZEN:
        try:
            os.remove(sys.executable + ".old")
        except OSError:
            pass


def self_update(force=False):
    """Replace this exe with the latest GitHub release's.

    Windows can't overwrite a running exe but can rename it, so: download and
    verify the new one, test-run it, rename the running exe to .old, put the new
    one in its place (rolling back on any failure). The .old file is removed the
    next time the program starts."""
    import hashlib
    import tempfile
    import urllib.request
    import zipfile

    if not FROZEN:
        sys.exit("running from source: update with `git pull` (self-update is for square_impact.exe)")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"square_impact/{VERSION}"}
    try:
        with urllib.request.urlopen(urllib.request.Request(LATEST_RELEASE_API, headers=headers),
                                    timeout=15) as resp:
            release = json.load(resp)
    except (OSError, ValueError) as e:
        sys.exit(f"couldn't reach GitHub to check for updates: {e}")
    tag = release.get("tag_name", "")
    new, ours = parse_version(tag), parse_version(VERSION)
    if not new:
        sys.exit(f"latest release tag {tag!r} isn't a version; not updating")
    if new <= ours and not force:
        print(f"already up to date: {VERSION} (latest release {tag})")
        return
    asset = next((a for a in release.get("assets", []) if a.get("name") == RELEASE_ASSET), None)
    if not asset:
        sys.exit(f"release {tag} has no {RELEASE_ASSET}; download it manually: {RELEASES_PAGE}")
    url = asset["browser_download_url"]
    if not url.startswith(f"https://github.com/{REPO}/releases/download/"):
        sys.exit(f"unexpected download URL {url}; not updating")

    print(f"updating {VERSION} -> {tag.lstrip('v')}")
    work = tempfile.mkdtemp(prefix="square_impact_update_")
    try:
        zpath = os.path.join(work, RELEASE_ASSET)
        size = asset.get("size") or 0
        dl = urllib.request.Request(url, headers={"User-Agent": headers["User-Agent"]})
        with urllib.request.urlopen(dl, timeout=60) as resp, open(zpath, "wb") as out:
            got = 0
            while chunk := resp.read(1 << 16):
                out.write(chunk)
                got += len(chunk)
                if size:
                    print(f"\r  downloading {got / 1e6:5.1f} / {size / 1e6:.1f} MB", end="", flush=True)
        print()
        digest = asset.get("digest") or ""  # GitHub publishes "sha256:<hex>" for release assets
        if digest.startswith("sha256:"):
            with open(zpath, "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
            if actual != digest.split(":", 1)[1]:
                sys.exit("download failed its SHA-256 check; not updating")
            print("  SHA-256 verified")
        with zipfile.ZipFile(zpath) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(EXE_NAME)), None)
            if not name:
                sys.exit(f"{RELEASE_ASSET} doesn't contain {EXE_NAME}; not updating")
            new_exe = os.path.join(work, EXE_NAME)
            with z.open(name) as src, open(new_exe, "wb") as dst:
                shutil.copyfileobj(src, dst)
        check = subprocess.run([new_exe, "--help"], capture_output=True, timeout=60)
        if check.returncode != 0:
            sys.exit("the downloaded exe failed to start; not updating")

        current = sys.executable
        old = current + ".old"
        try:
            os.remove(old)
        except OSError:
            pass
        os.replace(current, old)          # allowed for a running exe
        try:
            shutil.copy2(new_exe, current)
        except OSError:
            os.replace(old, current)      # roll back
            raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"updated to {tag.lstrip('v')}: {current}\n"
          f"  restart square_impact to use it. Release notes: {release.get('html_url', RELEASES_PAGE)}")


def main():
    import multiprocessing
    multiprocessing.freeze_support()   # denoise worker processes inside the packaged exe
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["obs", "live", "shot", "status", "sample", "grab", "view",
                                    "impact", "version", "update"])
    ap.add_argument("arg", nargs="?", help="sample: step N; view/impact: capture dir")
    ap.add_argument("--shot", action="store_true", help="sample/grab: wait for the next shot")
    ap.add_argument("--range", metavar="A:B", help="sample: only buffers A..B-1 (e.g. 120:150)")
    ap.add_argument("--cam", choices=["0", "1", "top", "bottom", "both"], default=None,
                    help="0/top (upper, tilted down) or 1/bottom (lower, level); "
                         "default top for live/shot, both otherwise")
    ap.add_argument("--pad", type=int, default=30, help="frames kept before/after impact (30)")
    ap.add_argument("--port", type=int, default=8765, help="obs: overlay web server port (8765)")
    ap.add_argument("--trim-front", type=float, default=0.2, metavar="FRACTION",
                    help="drop this fraction of each clip's start in the overlay and viewers, "
                         "where the club is still on its way in (default 0.2; 0 shows all; "
                         "saved clips keep every frame)")
    ap.add_argument("--trim-back", type=float, default=0.2, metavar="FRACTION",
                    help="drop this fraction of each clip's end in the overlay and viewers, "
                         "where the ball is rolling away (default 0.2; 0 shows all)")
    ap.add_argument("--crop", choices=list(CROPS), default="ball",
                    help="what the OBS overlay and viewers show: ball = a 640x192 window centred "
                         "on the ball at the start of the clip, then held still (default); follow = "
                         "the same window panning with the ball; left/right/full = fixed parts "
                         "of the launch camera's frame. Saved clips keep the full frame")
    ap.add_argument("--no-denoise", action="store_true",
                    help="show the launch camera's frames as captured, without the "
                         "frame-averaging denoise")
    ap.add_argument("--no-enhance", action="store_true",
                    help="show the launch camera's frames at their captured brightness, without "
                         "the shadow lift and edge sharpening")
    ap.add_argument("--board", choices=list(BOARD_MODE),
                    help="grab/sample: which board when both are connected (default r1)")
    ap.add_argument("--no-view", action="store_true", help="don't open the viewer afterwards")
    ap.add_argument("--no-update-check", action="store_true",
                    help="don't check GitHub for a newer release (exe only; also "
                         "SQUARE_IMPACT_NO_UPDATE_CHECK=1)")
    ap.add_argument("--force", action="store_true",
                    help="update: reinstall the latest release even if not newer")
    a = ap.parse_args()
    cleanup_old_exe()
    if a.cmd == "version":
        return version_info()
    if a.cmd == "update":
        return self_update(a.force)
    banner(a.cmd)
    upd = start_update_check() if update_check_enabled(a.no_update_check) else None
    try:
        return run(a)
    finally:
        if upd:
            upd.join(timeout=2)  # let a short command (status, view) still show the notice


def run(a):
    global ADB, SERIAL, DISPLAY_CROP, DISPLAY_TRIM_FRONT, DISPLAY_TRIM_BACK, DISPLAY_DENOISE, DISPLAY_ENHANCE
    DISPLAY_CROP = a.crop
    DISPLAY_TRIM_FRONT, DISPLAY_TRIM_BACK = a.trim_front, a.trim_back
    DISPLAY_DENOISE = not a.no_denoise
    DISPLAY_ENHANCE = not a.no_enhance
    if a.cmd == "view":
        return view(a.arg)
    if a.cmd == "impact":
        d = impact(a.arg, a.pad)
        return None if a.no_view else view(d)
    ADB = find_adb()
    stop_children_at_exit()
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
    boards = connected_boards()   # grab/sample talk to a single board
    if a.board and a.board not in boards:
        sys.exit(f"{a.board} isn't connected (connected: {', '.join(boards) or 'none'})")
    SERIAL = boards.get(a.board) or boards.get("r1") or next(iter(boards.values()), None)
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
