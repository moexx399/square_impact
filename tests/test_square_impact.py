"""Offline tests for square_impact: no Omni, adb, or display needed.

Frames are synthesised (strobe-lit floor + sensor noise + a bright ball), so
these cover the image logic, the device command builders, and file handling.
Run with:  python -m unittest discover -s tests -v
"""

import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import square_impact as si  # noqa: E402

W, H = 1280, 192
REST = (166, 142)  # ball rest position in px, as on the real launch board


def _backgrounds(count=4, seed=1):
    """A few noisy floor frames (brighter towards the bottom, like the strobe)."""
    rng = random.Random(seed)
    base = bytearray()
    for y in range(H):
        base += bytes([20 + y // 4]) * W
    out = []
    for _ in range(count):
        noise = rng.randbytes(W * H)
        out.append(bytearray(min(255, b + (n & 7)) for b, n in zip(base, noise)))
    return out


BACKGROUNDS = _backgrounds()


def frame(t, ball=REST, brightness=230, level=0):
    """Frame number t with a ball (ellipse 28x12 px) at `ball`, or no ball if None."""
    f = bytearray(BACKGROUNDS[t % len(BACKGROUNDS)])
    if level:
        f = bytearray(min(255, v + level) for v in f)
    if ball is not None:
        bx, by = ball
        for y in range(by - 6, by + 7):
            for x in range(bx - 14, bx + 15):
                if 0 <= x < W and 0 <= y < H and ((x - bx) / 14) ** 2 + ((y - by) / 6) ** 2 <= 1:
                    f[y * W + x] = brightness
    return bytes(f)


def shot_frames(contact=35, n=71, speed=4, glow=False):
    """Ball at rest until `contact`, then rolling right `speed` px per frame.
    glow=True brightens the resting ball before contact, like light from the
    approaching club (what fooled the original block-based detector)."""
    frames = []
    for t in range(n):
        x = REST[0] + max(0, t - contact) * speed
        brightness = 230
        if glow and 10 <= t <= contact:
            brightness = 200 + min(55, (t - 10) * 3)
        frames.append(frame(t, (x, REST[1]), brightness))
    return frames


class ContactDetection(unittest.TestCase):
    def test_contact_is_last_frame_before_ball_moves(self):
        t, rest, contrast = si.find_impact_ordered(shot_frames(contact=35), W, H)
        self.assertEqual(t, 35)
        self.assertLessEqual(abs(rest[0] - REST[0]), 4)
        self.assertLessEqual(abs(rest[1] - REST[1]), 4)
        self.assertGreater(contrast, si.MIN_BALL_CONTRAST)

    def test_club_light_on_resting_ball_does_not_trigger(self):
        t, _, _ = si.find_impact_ordered(shot_frames(contact=35, glow=True), W, H)
        self.assertEqual(t, 35)

    def test_slow_putt_is_detected(self):
        t, _, _ = si.find_impact_ordered(shot_frames(contact=40, speed=3), W, H)
        self.assertEqual(t, 40)

    def fast_shot(self, launch, speed=100, n=50):
        """Full swing: the ball leaves its rest spot at time `launch` (in frames,
        between two frames) and flies right `speed` px per frame."""
        return [frame(t, (round(REST[0] + max(0.0, t - launch) * speed), REST[1])) for t in range(n)]

    def test_fast_ball_contact_is_the_frame_nearest_launch(self):
        # launched late in the 2 ms between frames 35 and 36: by frame 36 the club
        # has met the ball, so 36 is nearer contact than 35 (ball still waiting)
        t, _, _ = si.find_impact_ordered(self.fast_shot(35.8), W, H)
        self.assertEqual(t, 36)
        # launched early in that gap: frame 35 is nearer
        t, _, _ = si.find_impact_ordered(self.fast_shot(35.2), W, H)
        self.assertEqual(t, 35)

    def test_driver_ball_that_leaves_the_view_at_once_is_not_seen_late(self):
        # off a tee at driver speed the ball climbs out of the 192-px-tall view
        # within 2 frames, too soon to be "seen moving away for 3"; contact used
        # to be found 13-15 frames late, so the replay showed the ball on the tee
        def driver(launch, speed=300, climb=90):
            out = []
            for t in range(61):
                dt = max(0.0, t - launch)
                x, y = round(REST[0] + dt * speed), round(REST[1] - dt * climb)
                out.append(frame(t, (x, y) if y > -10 and x < W + 20 else None))
            return out
        for launch, want in ((30.2, 30), (30.7, 31)):
            t, _, _ = si.find_impact_ordered(driver(launch), W, H)
            self.assertEqual(t, want, launch)

    def test_ball_faster_than_the_tracking_window_is_not_seen_late(self):
        # 30 px/frame jumps out of the centroid window; this used to be detected
        # two frames late
        for launch, want in ((35.0, 35), (35.5, 35), (35.7, 36)):
            t, _, _ = si.find_impact_ordered(self.fast_shot(launch, speed=30), W, H)
            self.assertEqual(t, want, launch)

    def test_ball_that_never_moves(self):
        frames = [frame(t) for t in range(40)]
        t, _, contrast = si.find_impact_ordered(frames, W, H)
        self.assertIsNone(t)
        self.assertGreater(contrast, si.MIN_BALL_CONTRAST)

    def test_no_ball_has_low_contrast(self):
        frames = [frame(t, ball=None) for t in range(40)]
        _, _, contrast = si.find_impact_ordered(frames, W, H)
        self.assertLess(contrast, si.MIN_BALL_CONTRAST)


class RingSeam(unittest.TestCase):
    def test_oldest_frame_follows_biggest_jump(self):
        n, oldest = 32, 13
        # brightness ramps with age, so the only big jump is newest -> oldest
        ring = [frame(i, ball=None, level=((i - oldest) % n) * 2) for i in range(n)]
        self.assertEqual(si.ring_seam(ring, W), oldest)


class DeviceCommandsAreReadOnly(unittest.TestCase):
    """Guard for the project's core rule: nothing is ever written to the Omni."""

    WRITE_PATTERNS = [
        r"(?<!2)>(?!\s*/dev/null)",  # output redirection other than 2>/dev/null
        r"\b(rm|mv|cp|dd\s+of=|touch|mkdir|tee|mount|umount|setprop|insmod|rmmod|reboot|"
        r"pkill|killall|chmod|chown|ln|truncate|fw_setenv|uci|sysupgrade)\b",
        r"\bof=",
        # the log follower may only stop its own tail ($tp = the PID of the
        # `tail` it started with &) or check it's alive (kill -0 sends nothing),
        # never touch any other process
        r"\bkill\b(?!\s+(-0\s+)?\$tp\b)",
    ]

    def commands(self):
        lay = {"nbuf": 256, "stride": 0x40000, "w": 1280, "h": 192}
        yield si.watcher_cmd()                         # the only standing command on the Omni
        yield si.snapshot_cmd(lay, 0, 230, 71)         # r1 window, wrapping
        yield si.shot_trigger("echo x")                # legacy grab/sample --shot
        r2 = {"nbuf": 128, "stride": 0x80000, "w": 1280, "h": 384}
        yield si.snapshot_cmd(r2, 0, 10, 16)
        yield si.snapshot_cmd(r2, 1, 120, 20)          # wraps past the end of the ring
        yield si.log_trigger(si.SFIND_LINE, "echo x")

    def test_no_writes(self):
        for cmd in self.commands():
            # the only tty setting changed is our own session's output processing
            body = cmd.replace("stty -opost; ", "", 1)
            self.assertNotIn("stty", body, cmd)
            for pat in self.WRITE_PATTERNS:
                self.assertIsNone(re.search(pat, body), f"{pat!r} matched in: {cmd}")

    def test_reads_only_dev_mem(self):
        for cmd in self.commands():
            for src in re.findall(r"\bif=(\S+)", cmd):
                self.assertEqual(src, "/dev/mem")

    def test_watcher_parses_the_log_on_the_host(self):
        """Backlog lines (before the watcher started) are context only; after
        that, r1's fire + shot lines and r2's spin line become events."""
        import io
        lines = [
            "9000",                                                            # uptime at start
            "Jan 1 x sqocam: ##T 8990.0 time : sqp_bfind_fire[99, 99] fire(1) bgn(1), end(2)",
            "Jan 1 x sqocam: ##T 8990.4 sqp_bfind_shot_data return : 1 ",       # backlog: ignored
            "Jan 1 x ledmgr[1]: led_hb_set: [0] 1 -> 0",                        # not sqocam
            "Jan 1 x sqocam: ##T 9010.1 time : sqp_bfind_fire[34, 34] fire(1) bgn(32), end(49)",
            "Jan 1 x sqocam: ##T 9010.5 sqp_bfind_shot_data return : 1 ",
            "Jan 1 x sqocam: ##T 9011.0 sim_carry input bs(1.85) ba(2.50) bd(0.60) srate((97.80) x",
            "Jan 1 x sqocam: ##T 9020.0 sqp_sfind_main return : 1 ",
        ]
        w = si.LogWatcher.__new__(si.LogWatcher)
        w.p = type("P", (), {"stdout": io.BytesIO("".join(x + "\r\n" for x in lines).encode())})()
        w.events, w.lines = si.queue.Queue(), si.collections.deque(maxlen=100)
        w.started, w.last_line, w.fire = None, 0, None
        w._run()
        events = []
        while not w.events.empty():
            events.append(w.events.get())
        self.assertEqual([(e["kind"], e.get("F"), e["t"]) for e in events],
                         [("shot", 34, 9010.5), ("spin", None, 9020.0)])
        self.assertEqual(len(w.history()), 6)                      # sqocam lines, backlog included
        self.assertIn("ball speed 1.85 m/s", si.sqocam_text(w.history(), 9010.5))
        self.assertEqual(si.sqocam_text(w.history(), 9015.0), "")  # nothing logged after that

    @unittest.skipUnless(os.name == "posix" and all(shutil.which(t) for t in ("sh", "tail", "ls")),
                         "needs a POSIX sh/tail/ls (MSYS on Windows can't clean up the tail)")
    def test_log_follower_survives_rotation(self):
        """The Omni rotates /var/log/messages every ~25 min. A plain `tail -f`
        stays on the renamed file and never sees another shot (the 'hang')."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "messages").replace("\\", "/")
        with open(log, "w") as fh:
            fh.write("old line before arming\n")
        follow = si.FOLLOW_LOG.replace("/var/log/messages", log)
        p = subprocess.Popen(["sh", "-c", follow], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, start_new_session=True)

        def kill_tree():  # sh, its subshell and its tail
            import signal
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        self.addCleanup(kill_tree)
        lines = []
        threading.Thread(target=lambda: [lines.append(x.strip()) for x in p.stdout],
                         daemon=True).start()

        def wait_for(text, secs=8):
            end = time.time() + secs
            while time.time() < end and text not in lines:
                time.sleep(0.1)
            return text in lines

        time.sleep(1.5)
        with open(log, "a") as fh:
            fh.write("before rotation\n")
        self.assertTrue(wait_for("before rotation"))
        os.replace(log, log + ".0")                 # rotate like logrotate does
        with open(log, "w") as fh:
            fh.write("written right after rotation\n")
        self.assertTrue(wait_for("written right after rotation"), lines)
        with open(log, "a") as fh:
            fh.write("later line\n")
        self.assertTrue(wait_for("later line"), lines)
        self.assertNotIn("old line before arming", lines)


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._shots = si.SHOTS_DIR
        si.SHOTS_DIR = self.tmp

    def tearDown(self):
        si.SHOTS_DIR = self._shots
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prune_keeps_newest(self):
        names = [f"20261011_1400{i:02d}_r1_shot_cam0" for i in range(8)]
        for n in names:
            os.makedirs(os.path.join(self.tmp, n))
        si.prune_captures(keep=5)
        self.assertEqual(sorted(os.listdir(self.tmp)), names[-5:])

    def test_save_load_display_payload(self):
        """A two-board shot saves, reloads, and plays as r1 (trimmed, cropped,
        both cams stacked) then r2 (whole frame)."""
        from unittest import mock
        w, h = 4, 2
        r1 = {"board": "r1", "cams": [0, 1], "w": w, "h": h, "fps": 500.0,
              "idxs": [254, 255, 0, 1], "impact": 0,
              "offset_ms": [-4.0, -2.0, 0.0, 2.0],
              "frames": {0: [bytes([b]) * (w * h) for b in (254, 255, 0, 1)],
                         1: [bytes([200]) * (w * h)] * 4}}
        r2 = {"board": "r2", "cams": [0], "w": w, "h": h, "fps": 250.0,
              "idxs": [10, 11, 12], "impact": 11, "offset_ms": [-4.0, 0.0, 4.0],
              "frames": {0: [bytes(range(8)), bytes(range(8, 16)), bytes(range(16, 24))]}}
        with mock.patch.object(si, "DISPLAY_DENOISE", False), \
                mock.patch.object(si, "DISPLAY_ENHANCE", False), \
                mock.patch.object(si, "DISPLAY_CROP", "left"), \
                mock.patch.object(si, "DISPLAY_TRIM_FRONT", 0.25), \
                mock.patch.object(si, "DISPLAY_TRIM_BACK", 0.25):
            d = si.save_shot([r1, r2], "info")
            self.assertTrue(os.path.basename(d).endswith("_shot_r1+r2"))
            loaded = si.load_shot(d)
            self.assertEqual([s["board"] for s in loaded], ["r1", "r2"])
            self.assertEqual(loaded[0]["frames"][0][0], r1["frames"][0][0])
            header, data = si.overlay_payload(d, "info")
        g1, g2 = header["segments"]
        # r1: a quarter trimmed off each end -> buffers 255, 0; left half; cams stacked
        self.assertEqual((g1["w"], g1["h"], g1["n"], g1["impact"]), (2, 4, 2, 1))
        # r2: untouched
        self.assertEqual((g2["w"], g2["h"], g2["n"]), (4, 2, 3))
        r1_bytes = 2 * (2 * 4)
        self.assertEqual(len(data), r1_bytes + 3 * w * h)
        self.assertEqual(data[0], 255)                 # first r1 frame = buffer 255, cam0
        self.assertEqual(data[2 * h], 200)             # then cam1 below it
        self.assertEqual(data[r1_bytes:r1_bytes + 8], bytes(range(8)))

    def test_impact_snapshot_stacks_both_boards(self):
        """impact.png: r1's contact frame over r2's first lit frame, full size."""
        from unittest import mock
        import struct
        r1 = {"board": "r1", "cams": [0], "w": 6, "h": 2, "fps": 500.0, "idxs": [1, 2, 3],
              "impact": 2, "frames": {0: [bytes([10]) * 12, bytes([77]) * 12, bytes([10]) * 12]}}
        r2 = {"board": "r2", "cams": [0], "w": 4, "h": 3, "fps": 250.0, "idxs": [8, 9],
              "impact": 9, "frames": {0: [bytes(12), bytes([200]) * 12]}}
        with mock.patch.object(si, "DISPLAY_DENOISE", False), \
                mock.patch.object(si, "DISPLAY_ENHANCE", False):
            d = si.save_shot([r1, r2])
            path = si.write_impact_snapshot(d)
        with open(path, "rb") as fh:
            png = fh.read()
        w, h = struct.unpack(">II", png[16:24])
        self.assertEqual((w, h), (6, 2 + 6 + 3))                  # r1 rows, 6-px gap, r2 rows
        rows = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
        self.assertEqual(rows[1], 77)                             # r1's contact frame
        self.assertEqual(rows[(2 + 6) * 7 + 1], 200)              # r2's lit frame, padded to 6 wide
        self.assertEqual(rows[(2 + 6) * 7 + 1 + 4], 0)

    def test_loads_older_single_board_folders(self):
        d = os.path.join(self.tmp, "20261011_140000_r1_shot_cam0")
        os.makedirs(d)
        w, h = 4, 2
        for b in (5, 6):
            si.write_pgm(os.path.join(d, f"cam0_{b:03d}.pgm"), w, h, bytes([b]) * (w * h))
        with open(os.path.join(d, "meta.json"), "w") as fh:
            json.dump({"board": "r1", "w": w, "h": h, "idxs": [5, 6], "cams": [0], "impact": 6,
                       "offset_ms": [-2.0, 0.0], "fps": 500.0}, fh)
        (seg,) = si.load_shot(d)
        self.assertEqual((seg["board"], seg["impact"], seg["frames"][0][1][0]), ("r1", 6, 6))

    def test_png_writer(self):
        path = os.path.join(self.tmp, "t.png")
        si.write_png(path, 3, 2, bytes(range(6)))
        with open(path, "rb") as fh:
            png = fh.read()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
        self.assertEqual(zlib.decompress(idat), b"\x00\x00\x01\x02\x00\x03\x04\x05")


class Crop(unittest.TestCase):
    def test_left_half(self):
        w, h = 6, 2
        f = bytes(range(12))
        x0, x1 = si.crop_columns(w, "left")
        self.assertEqual((x0, x1), (0, 3))
        self.assertEqual(si.crop_frame(f, w, h, x0, x1), bytes([0, 1, 2, 6, 7, 8]))
        self.assertEqual(si.crop_frame(f, w, h, *si.crop_columns(w, "right")), bytes([3, 4, 5, 9, 10, 11]))
        self.assertIs(si.crop_frame(f, w, h, *si.crop_columns(w, "full")), f)


class BallCrop(unittest.TestCase):
    def test_window_is_centred_and_padded_past_the_edges(self):
        f = bytes(range(12))                                     # 6x2 frame
        self.assertEqual(si.crop_window(f, 6, 2, 3, 1, 4, 2), bytes([1, 2, 3, 4, 7, 8, 9, 10]))
        self.assertEqual(si.crop_window(f, 6, 2, 0, 1, 4, 2), bytes([0, 0, 0, 1, 0, 0, 6, 7]))
        self.assertEqual(si.crop_window(f, 6, 2, 6, 1, 4, 2), bytes([4, 5, 0, 0, 10, 11, 0, 0]))
        self.assertEqual(si.crop_window(f, 6, 2, -50, 1, 4, 2), bytes(8))

    def scene(self, ball=None, stripe=False, shaft=False):
        """Bright floor (like the grey-carpet room) with optional ball, a long flat
        floor highlight, and a tall thin club shaft."""
        f = bytearray(frame(0, ball=None))
        if stripe:   # long, flat and bigger than the ball
            for y in range(70, 76):
                for x in range(600, 900):
                    f[y * W + x] = 250
        if shaft:    # tall and thin
            for y in range(10, 150):
                for x in range(300, 306):
                    f[y * W + x] = 250
        if ball:
            f = bytearray(frame(0, ball=ball, brightness=250))
            if stripe or shaft:
                g = self.scene(None, stripe, shaft)
                f = bytearray(max(a, b) for a, b in zip(f, g))
        return bytes(f)

    def test_finds_the_ball_not_the_floor_stripe_or_shaft(self):
        x, y = si.ball_blob(self.scene(ball=(195, 134), stripe=True, shaft=True), W, H)
        self.assertLess(abs(x - 195), 4)
        self.assertLess(abs(y - 134), 4)
        self.assertIsNone(si.ball_blob(self.scene(stripe=True, shaft=True), W, H))

    def test_low_camera_sees_a_flat_ball_and_it_still_counts(self):
        # the bottom camera sees the resting ball ~4.5x wider than tall (72x16 px);
        # it used to be rejected as "too flat", and the crop snapped to the left
        f = bytearray(frame(0, ball=None))
        for y in range(126, 142):
            for x in range(300, 372):
                if ((x - 336) / 36) ** 2 + ((y - 134) / 8) ** 2 <= 1:
                    f[y * W + x] = 230
        x, y = si.ball_blob(bytes(f), W, H)
        self.assertLess(abs(x - 336), 4)
        self.assertLess(abs(y - 134), 4)

    def test_crop_starts_on_the_resting_ball_when_it_isnt_tracked_at_first(self):
        # opening frames: no ball the tracker accepts; later, a blob far to the left
        frames = [frame(0, ball=None)] * 3 + [frame(1, ball=(60, 120))] * 3
        self.assertLess(si.ball_path(frames, W, H)[0][0], 100)              # the old snap
        self.assertEqual(si.ball_path(frames, W, H, rest=(336, 134))[0], (336, 134))

    def test_path_fills_frames_without_a_ball(self):
        frames = [frame(0, ball=None), frame(1, ball=(400, 120)), frame(2, ball=None),
                  frame(3, ball=(500, 120)), frame(0, ball=None)]
        xs = [round(p[0]) for p in si.ball_path(frames, W, H)]
        # held before the first sighting, interpolated between, held after the
        # last (not panned off into black); then lightly smoothed
        self.assertTrue(xs[0] <= xs[1] <= xs[2] <= xs[3] <= xs[4], xs)
        for got, want in zip((xs[0], xs[2], xs[4]), (400, 450, 500)):
            self.assertLessEqual(abs(got - want), 2, xs)        # block-level precision


    def crop_centres(self, mode):
        """Where display_segments centres the window in each frame, for a ball the
        tracker saw moving right."""
        from unittest import mock
        seg = {"board": "r2", "cams": [0], "w": W, "h": H, "fps": 250.0,
               "idxs": [0, 1, 2], "impact": 1, "frames": {0: [bytes(W * H)] * 3}}
        centres = []
        with mock.patch.object(si, "load_shot", return_value=[seg]), \
                mock.patch.object(si, "ball_path", return_value=[(400, 120), (450, 118), (520, 115)]), \
                mock.patch.object(si, "crop_window",
                                  side_effect=lambda f, w, h, x, y, cw, ch: centres.append((x, y)) or f), \
                mock.patch.object(si, "DISPLAY_CROP", mode):
            si.display_segments("shot")
        return centres

    def test_default_crop_is_centred_on_the_start_and_holds_still(self):
        self.assertEqual(self.crop_centres("ball"), [(400, 120)] * 3)

    def test_follow_crop_pans_with_the_ball(self):
        self.assertEqual(self.crop_centres("follow"), [(400, 120), (450, 118), (520, 115)])


class Enhance(unittest.TestCase):
    def test_dark_clip_shadows_are_lifted_white_stays_white(self):
        dark = [bytes([8] * 50 + [14] * 100 + [40] * 40 + [255] * 10) * 10]   # median 14, like r1
        curve = si.tone_curve(dark)
        self.assertEqual(curve[8], 0)            # black point
        self.assertAlmostEqual(curve[14], si.TONE_MEDIAN, delta=3)
        self.assertGreater(curve[40], 100)       # the club's dark side becomes visible
        self.assertEqual(curve[255], 255)
        self.assertEqual(list(curve), sorted(curve))   # monotonic: nothing swaps brightness

    def test_bright_clip_is_never_darkened(self):
        bright = [bytes([20] * 10 + [120] * 180 + [250] * 10) * 10]
        curve = si.tone_curve(bright)
        self.assertTrue(all(curve[v] >= v - 20 for v in range(20, 256)))   # only the black point moves
        self.assertGreaterEqual(curve[120], 120 - 20)

    def test_moving_club_is_lifted_more_than_the_still_turf(self):
        w, h, n = 400, 3, 9
        turf = bytes([8, 10, 12, 14, 14, 16, 18, 20] * (w * h // 8))   # dark, median 14 like r1's floor
        frames = [turf] * n
        club = bytearray(turf)
        club[w + 3] = 60                       # one bright club edge passing over a 14
        frames[4] = bytes(club)
        still, moving = si.tone_curves(frames)
        self.assertLess(still[14], moving[14])
        out = si.enhance(frames, w, h, parallel=False)[4]
        flat = si.sharpen(turf, w, h)
        self.assertEqual(out[w], still[flat[w]])     # turf: the gentle curve
        self.assertEqual(out[w + 3], moving[si.sharpen(frames[4], w, h)[w + 3]])   # club: the full lift
        self.assertGreater(out[w + 3] / max(1, out[w + 4]), moving[60] / moving[14])   # stands out more

    def test_single_frame_without_background_counts_as_moving(self):
        f = bytes([8, 14, 40, 255] * 6)
        curves = si.tone_curves([f])
        self.assertEqual(si.enhance([f], 4, 6, curves, parallel=False)[0],
                         si.sharpen(f, 4, 6).translate(curves[1]))

    def test_sharpen_crisps_edges_but_leaves_grain(self):
        w, h = 8, 5
        grain = bytes([20, 22, 20, 21, 19, 20, 22, 20] * h)   # +-2 levels: under the threshold
        self.assertEqual(si.sharpen(grain, w, h), grain)
        edge = bytes(([20] * 4 + [80] * 4) * h)               # dark | bright
        out = si.sharpen(edge, w, h)
        self.assertLess(out[2 * w + 3], 20)                   # dark side of the edge gets darker
        self.assertGreater(out[2 * w + 4], 80)                # bright side brighter
        self.assertEqual(out[2 * w + 0], 20)                  # flat areas untouched
        self.assertEqual(out[2 * w + 7], 80)


class Settings(unittest.TestCase):
    """square_impact.json next to the app overrides the defaults; the command
    line overrides the file; the settings page saves to the file."""

    def setUp(self):
        from unittest import mock
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "square_impact.json")
        self.saved = {s["attr"]: getattr(si, s["attr"]) for s in si.SETTINGS if s["attr"]}
        self.patches = [mock.patch.object(si, "CONFIG_PATH", self.path),
                        mock.patch.dict(si.CONFIG, {"file": {}, "cli": {}, "warnings": []})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for attr, v in self.saved.items():   # settings are module globals: put them back
            setattr(si, attr, v)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, obj):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_schema_defaults_match_the_code(self):
        for s in si.SETTINGS:
            if s["attr"]:
                self.assertEqual(s["default"], self.saved[s["attr"]], s["key"])
            si.coerce_setting(s["key"], s["default"])   # every default passes its own limits

    def test_file_overrides_defaults_and_command_line_overrides_file(self):
        self.write({"crop": "full", "pad": 20, "sharpen_amount": 0.5})
        self.assertEqual(si.load_settings({"pad": 25}), [])
        self.assertEqual((si.DISPLAY_CROP, si.CAPTURE_PAD, si.SHARPEN_AMOUNT), ("full", 25, 0.5))
        self.assertEqual(si.setting("trim_front"), 0.2)   # untouched: the default

    def test_bad_entries_are_reported_and_ignored(self):
        self.write({"pad": "lots", "crop": "sideways", "tone_median": 999, "colour": "red", "denoise": True})
        warnings = si.load_settings()
        self.assertEqual(len(warnings), 4)
        self.assertEqual(si.CAPTURE_PAD, 30)
        self.assertTrue(si.DISPLAY_DENOISE)                # the valid one still applies
        self.write("{ not json")
        self.assertEqual(len(si.load_settings()), 1)
        self.assertEqual(si.DISPLAY_DENOISE, si.SETTING["denoise"]["default"])

    def test_save_validates_then_writes_and_applies(self):
        self.write({"comment": "kept", "pad": 20})
        self.assertIn("pad", si.save_settings({"pad": 500, "crop": "full"}))   # nothing written
        self.assertEqual(self.read(), {"comment": "kept", "pad": 20})
        self.assertEqual(si.save_settings({"pad": None, "crop": "full", "gain": "1.5"}), {})
        self.assertEqual(self.read(), {"comment": "kept", "crop": "full", "gain": 1.5})
        self.assertEqual(si.DISPLAY_CROP, "full")
        self.assertEqual(si.page_config()["gain"], 1.5)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_settings_page_and_api(self):
        import urllib.error
        import urllib.request
        ov = si.Overlay()
        srv = si.serve_overlay(ov, 0)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"   # (localhost tries IPv6 first: slow on Windows)

        def post(body, ctype="application/json", origin=None, host=None):
            req = urllib.request.Request(base + "/api/settings", data=json.dumps(body).encode(),
                                         headers={"Content-Type": ctype}, method="POST")
            if origin:
                req.add_header("Origin", origin)
            if host:
                req.add_header("Host", host)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read())
        try:
            with urllib.request.urlopen(base + "/settings", timeout=5) as r:
                self.assertIn(b"square_impact settings", r.read())
            with urllib.request.urlopen(base + "/api/settings", timeout=5) as r:
                state = json.loads(r.read())
            self.assertEqual({s["key"] for s in state["settings"]}, set(si.SETTING))
            # a form or page from another site can't change settings
            self.assertEqual(post({"crop": "full"}, ctype="text/plain")[0], 403)
            self.assertEqual(post({"crop": "full"}, origin="http://evil.example")[0], 403)
            self.assertEqual(post({"crop": "full"}, host=f"evil.example:{port}")[0], 403)
            self.assertFalse(os.path.exists(self.path))
            self.assertEqual(post({"pad": -1})[0], 400)
            code, state = post({"crop": "full", "fps": 12}, origin=base)
            self.assertEqual(code, 200)
            self.assertEqual(self.read(), {"crop": "full", "fps": 12})
            self.assertEqual(si.DISPLAY_CROP, "full")
            self.assertEqual(ov.config_seq, 1)               # open overlay pages reload
            with urllib.request.urlopen(base + "/", timeout=5) as r:
                self.assertIn(b'"fps": 12', r.read())        # the page's defaults
        finally:
            srv.shutdown()
            srv.server_close()


class Release(unittest.TestCase):
    def test_version_matches_pyproject_and_changie_can_stamp_it(self):
        """`changie batch` writes the version into both files by pattern; a line
        it can't match is silently skipped (0.14.0 shipped tagged with VERSION
        still 0.12.1 that way), and the release build refuses a mismatch."""
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
            project = re.search(r'^version = "(.*)"$', fh.read(), re.M)
        with open(os.path.join(root, "square_impact.py"), encoding="utf-8") as fh:
            code = re.search(r'^VERSION = "(.*)"$', fh.read(), re.M)
        self.assertTrue(code, 'square_impact.py needs a line exactly like VERSION = "1.2.3"')
        self.assertTrue(project, 'pyproject.toml needs a line exactly like version = "1.2.3"')
        self.assertEqual(code[1], si.VERSION)
        self.assertEqual(project[1], si.VERSION)


class Platforms(unittest.TestCase):
    def test_update_asset_names_match_what_the_pipeline_builds(self):
        """`update` downloads square_impact-<release_platform()>.zip; build-exe.yml
        must build exactly those platforms."""
        from unittest import mock
        wf = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "build-exe.yml")
        with open(wf, encoding="utf-8") as fh:
            built = set(re.findall(r"platform: ([\w-]+)", fh.read()))
        names = set()
        for windows, macos, machine in ((True, False, "AMD64"), (False, True, "arm64"),
                                        (False, True, "x86_64"), (False, False, "x86_64")):
            with mock.patch.object(si, "WINDOWS", windows), mock.patch.object(si, "MACOS", macos), \
                    mock.patch("platform.machine", return_value=machine):
                names.add(si.release_platform())
        self.assertEqual(names, {"windows", "macos-arm64", "macos-x64", "linux-x64"})
        self.assertEqual(built, names)

    def test_data_dir_per_platform(self):
        from unittest import mock
        with mock.patch.object(si, "WINDOWS", False), mock.patch.object(si, "MACOS", True):
            self.assertTrue(si.data_dir().endswith(os.path.join("Application Support", "square_impact")))
        with mock.patch.object(si, "WINDOWS", False), mock.patch.object(si, "MACOS", False), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg"}):
            self.assertEqual(si.data_dir(), os.path.join("/xdg", "square_impact"))


class SettingsInEveryMode(unittest.TestCase):
    def test_settings_server_starts_and_tolerates_a_taken_port(self):
        import contextlib
        import io
        import socket
        import urllib.request
        from unittest import mock
        with socket.socket() as s:   # a free port
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        out = io.StringIO()
        with mock.patch.object(si, "OVERLAY_PORT", port), contextlib.redirect_stdout(out):
            si.start_settings_server()
            si.start_settings_server()   # port now taken (as by a running obs): no crash
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/settings", timeout=5) as r:
                self.assertEqual(r.status, 200)
        self.assertIn(f"localhost:{port}/settings", out.getvalue())
        self.assertIn("already running", out.getvalue())


class SettingsPreview(unittest.TestCase):
    """The settings page previews a saved shot with unsaved values, without
    changing the running settings."""

    def setUp(self):
        from unittest import mock
        self.dir = tempfile.mkdtemp()
        r1 = {"board": "r1", "cams": [0], "w": W, "h": H, "fps": 500.0,
              "idxs": list(range(12)), "impact": 6,
              "frames": {0: shot_frames(contact=6, n=12, speed=20)}}
        self.patches = [mock.patch.object(si, "SHOTS_DIR", self.dir),
                        mock.patch.object(si, "KEEP_CAPTURES", 50)]
        for p in self.patches:
            p.start()
        self.name = os.path.basename(si.save_shot([r1], "info"))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_preview_renders_with_unsaved_values_and_leaves_settings_alone(self):
        self.assertEqual(si.saved_shots(), [self.name])
        header, data = si.preview_payload(self.name, {"crop": "full", "enhance": False, "fps": 99})
        seg = header["segments"][0]
        self.assertEqual((seg["w"], seg["h"]), (W, H))                  # full frame, not the ball crop
        self.assertEqual(len(data), seg["n"] * W * H)
        self.assertEqual((si.DISPLAY_CROP, si.DISPLAY_ENHANCE), ("ball", True))   # nothing changed
        header, _ = si.preview_payload(self.name, {"crop": None})       # None: the default
        self.assertEqual(header["segments"][0]["w"], si.BALL_CROP[0])

    def test_preview_rejects_unknown_shots_and_bad_values(self):
        for name, edits in (("../../etc", {}), ("nope", {}), (self.name, {"crop": "sideways"})):
            with self.assertRaises(ValueError):
                si.preview_payload(name, edits)


class TrimClip(unittest.TestCase):
    def clip_meta(self, n=61, contact=30):
        idxs = [(200 + i) % 256 for i in range(n)]   # wraps around the ring
        return {"idxs": idxs, "impact": idxs[contact],
                "offset_ms": [(i - contact) * 2.0 for i in range(n)],
                "motion": [[0] + [i % 7 for i in range(1, n)]]}

    def test_drops_first_20_percent(self):
        m = si.trim_clip(self.clip_meta(), front=0.2)
        self.assertEqual(len(m["idxs"]), 49)              # 12 of 61 frames dropped
        self.assertEqual(m["idxs"][0], 212)
        self.assertEqual(m["idxs"].index(m["impact"]), 18)
        self.assertEqual(m["offset_ms"][0], -36.0)
        self.assertEqual(len(m["motion"][0]), 49)
        self.assertEqual(m["motion"][0][0], 0)

    def test_drops_last_20_percent(self):
        m = si.trim_clip(self.clip_meta(), back=0.2)
        self.assertEqual(len(m["idxs"]), 49)
        self.assertEqual(m["idxs"][-1], (200 + 48) % 256)
        self.assertEqual(m["offset_ms"][-1], 36.0)
        self.assertEqual(len(m["motion"][0]), 49)

    def test_default_display_trim_both_ends(self):
        m = si.trim_clip(self.clip_meta(), 0.2, 0.2)
        self.assertEqual(len(m["idxs"]), 37)              # 18 before, contact, 18 after
        self.assertEqual(m["idxs"].index(m["impact"]), 18)
        self.assertEqual((m["offset_ms"][0], m["offset_ms"][-1]), (-36.0, 36.0))

    def test_never_trims_away_contact(self):
        m = si.trim_clip(self.clip_meta(), 0.9, 0.9)
        self.assertEqual(m["idxs"], [m["impact"]])

    def test_zero_keeps_everything(self):
        meta = self.clip_meta()
        self.assertIs(si.trim_clip(meta, 0, 0), meta)


class BoardLoopSelfHealing(unittest.TestCase):
    """A board's capture loop must never wait forever: it restarts a watcher
    whose adb session ended or went silent, and gives up on a board that's been
    unplugged (the manager restarts it)."""

    def run_loop(self, board, watchers, connected, clock=None):
        import queue
        from unittest import mock
        made = []
        stop = threading.Event()
        q = queue.Queue()

        class FakeWatcher:
            def __init__(self, serial):
                self.kind = watchers[len(made)]
                made.append(self)
                self.events = queue.Queue()
                if self.kind == "stop":
                    stop.set()

            def alive(self):
                return self.kind != "dead"

            def stalled(self):
                return self.kind == "silent"

            def close(self):
                pass

        patches = [mock.patch.object(si, "LogWatcher", FakeWatcher),
                   mock.patch.object(si, "omni_connected", side_effect=connected),
                   mock.patch.object(si.time, "sleep")]
        if clock:
            patches.append(mock.patch.object(si.time, "time", side_effect=clock))
        for p in patches:
            p.start()
        try:
            si.board_loop({"board": board, "serial": "x"}, [0], 30, q, stop)
        finally:
            for p in patches:
                p.stop()
        msgs = []
        while not q.empty():
            msgs.append(q.get())
        return made, msgs

    def test_restarts_a_dead_or_silent_watcher(self):
        for board in ("r1", "r2"):
            made, msgs = self.run_loop(board, ["dead", "silent", "stop"], connected=lambda *_: True)
            self.assertEqual(len(made), 3)                  # replaced twice, no hang
            self.assertEqual(msgs, [("armed", board)] * 3)

    def test_gives_up_on_an_unplugged_board(self):
        ticks = iter(range(0, 1000, 4))                     # each call is 4 s later
        made, msgs = self.run_loop("r2", [], connected=lambda *_: False, clock=lambda: next(ticks))
        self.assertEqual(made, [])
        self.assertEqual(msgs, [("gone", "r2")])


class SpinBoard(unittest.TestCase):
    LOG = [
        "Jan 1 x sqocam: ##T 7604.290 7604.289762 1 1 ( 1 2 7604.289135 ) ( 1 2 7604.289597 ) 0.000462",
        "Jan 1 x sqocam: ##T 7614.289 7614.289178 2501 69 ( 69 2502 7614.288517 ) ( 69 2502 x ) 0.0004",
        "Jan 1 x sqocam: ##T 7616.882 sqp_grab_stop() ",
        "Jan 1 x sqocam: ##T 7624.807 sqp_grab_stop() ",            # a later stop: ignored
    ]

    def test_newest_buffer_from_the_log(self):
        # the real test shot: frame 2501 at 7614.289, stop 2.593 s later -> frame 3149 -> buffer 77
        self.assertEqual(si.r2_newest_buffer(self.LOG, 7617.477, 250.0156, 128), 77)
        self.assertIsNone(si.r2_newest_buffer(self.LOG[2:3], 7617.477, 250.0156, 128))  # no counter
        self.assertIsNone(si.r2_newest_buffer(self.LOG[:2], 7617.477, 250.0156, 128))   # no stop

    def burst_frames(self, n, lit):
        dark = bytes([11]) * 2000
        bright = bytes([11]) * 1000 + bytes([230]) * 1000
        return [bright if i in lit else dark for i in range(n)]

    def lay(self):
        return {"board": "r2", "w": 50, "h": 40, "fps": 250.0}

    def test_lit_burst_from_the_predicted_window(self):
        bufs = [(100 + p) % 128 for p in range(64)]
        cap = {"frames": {0: self.burst_frames(64, range(30, 38))}, "buffers": bufs,
               "ordered": True, "t_shot": 0, "t_fetched": 1}
        seg, _ = si.r2_segment(self.lay(), cap)
        self.assertEqual(seg["idxs"], [bufs[p] for p in range(28, 40)])  # burst +-2
        self.assertEqual(seg["impact"], bufs[30])
        self.assertEqual(seg["offset_ms"][2], 0.0)

    def test_lit_burst_wrapping_the_whole_ring(self):
        lit = {126, 127, 0, 1, 2}
        cap = {"frames": {0: self.burst_frames(128, lit)}, "buffers": list(range(128)),
               "ordered": False, "t_shot": 0, "t_fetched": 1}
        seg, _ = si.r2_segment(self.lay(), cap)
        self.assertEqual(seg["idxs"], [124, 125, 126, 127, 0, 1, 2, 3, 4])
        self.assertEqual(seg["impact"], 126)

    def test_no_strobe_is_ignored(self):
        cap = {"frames": {0: self.burst_frames(64, [])}, "buffers": list(range(64)),
               "ordered": True, "t_shot": 0, "t_fetched": 1}
        with self.assertRaises(si.NoBall):
            si.r2_segment(self.lay(), cap)


class ShotManager(unittest.TestCase):
    """Captures from both boards of the same shot become one clip (r1 then r2);
    with one board connected, its captures are saved on their own."""

    def run_manager(self, boards):
        from unittest import mock
        saved, emitted = [], []
        stop = threading.Event()

        def fake_loop(lay, cams, pad, q, stop_ev):
            b = lay["board"]
            q.put(("armed", b))
            time.sleep(0.3 if b == "r1" else 0.6)        # r2 reports a little later
            q.put(("segment", b, {"board": b}, f"{b} ok", [], 100.0))
            stop_ev.wait()

        def fake_save(segs, info):
            saved.append([s["board"] for s in segs])
            return "dir"

        with mock.patch.object(si, "connected_boards", return_value={b: b for b in boards}), \
                mock.patch.object(si, "device_layout", side_effect=lambda s: {"board": s, "serial": s}), \
                mock.patch.object(si, "board_loop", fake_loop), \
                mock.patch.object(si, "save_shot", fake_save), \
                mock.patch.object(si, "write_impact_snapshot"), \
                mock.patch.object(si, "write_sheets"):
            out = si.shot_manager([0], 30, lambda *m: emitted.append(m), stop, once=True)
        stop.set()
        return out, saved, emitted

    def test_pairs_both_boards(self):
        out, saved, emitted = self.run_manager(["r2", "r1"])
        self.assertEqual(out, "dir")
        self.assertEqual(saved, [["r1", "r2"]])
        shot = [m for m in emitted if m[0] == "shot"][0]
        self.assertIn("r1 ok", shot[2])
        self.assertIn("r2 ok", shot[2])

    def test_single_board_saves_immediately(self):
        t = time.time()
        out, saved, _ = self.run_manager(["r2"])
        self.assertEqual(saved, [["r2"]])
        self.assertLess(time.time() - t, si.PAIR_WINDOW)   # didn't wait for a partner


class UpdateCheck(unittest.TestCase):
    """The network is always mocked here; tests never contact GitHub."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._cache, self._version = si.UPDATE_CACHE, si.VERSION
        si.UPDATE_CACHE = os.path.join(self.tmp, "update_check.json")
        si.VERSION = "0.2.0"

    def tearDown(self):
        si.UPDATE_CACHE, si.VERSION = self._cache, self._version
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_github(self, tag=None, error=None):
        """Patch urlopen to return a release with `tag`, or raise `error`."""
        import io
        import urllib.request
        from unittest import mock
        calls = []

        def urlopen(req, timeout=None):
            calls.append(req.full_url)
            if error:
                raise error
            body = json.dumps({"tag_name": tag, "html_url": f"https://example/{tag}"}).encode()
            return io.BytesIO(body)

        patcher = mock.patch.object(urllib.request, "urlopen", urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_parse_version(self):
        self.assertEqual(si.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(si.parse_version("0.10.0"), (0, 10, 0))
        self.assertEqual(si.parse_version("1.2.3-rc1"), (1, 2, 3))
        self.assertIsNone(si.parse_version("nightly"))
        self.assertIsNone(si.parse_version(None))

    def test_notice_only_when_newer(self):
        self.assertIn("0.2.0 -> 0.10.0", si.update_notice({"tag": "v0.10.0", "url": "u"}))
        self.assertIsNone(si.update_notice({"tag": "v0.2.0", "url": "u"}))
        self.assertIsNone(si.update_notice({"tag": "v0.0.2", "url": "u"}))   # older
        self.assertIsNone(si.update_notice({"tag": "latest", "url": "u"}))   # not a version
        self.assertIsNone(si.update_notice(None))

    def test_result_is_cached_for_a_day(self):
        calls = self.fake_github(tag="v0.3.0")
        self.assertEqual(si.latest_release()["tag"], "v0.3.0")
        self.assertEqual(si.latest_release()["tag"], "v0.3.0")
        self.assertEqual(len(calls), 1)                        # second call used the cache
        si.latest_release(use_cache=False)
        self.assertEqual(len(calls), 2)                        # forced refresh

    def test_offline_is_silent_and_not_cached(self):
        import urllib.error
        self.fake_github(error=urllib.error.URLError("no network"))
        self.assertIsNone(si.latest_release())
        self.assertFalse(os.path.exists(si.UPDATE_CACHE))

    def test_no_releases_yet(self):
        import urllib.error
        calls = self.fake_github(error=urllib.error.HTTPError(si.LATEST_RELEASE_API, 404, "x", {}, None))
        self.assertIsNone(si.latest_release())
        self.assertIsNone(si.latest_release())
        self.assertEqual(len(calls), 1)                        # "no releases" is cached too

    def test_only_the_exe_checks_by_default(self):
        from unittest import mock
        with mock.patch.object(si, "FROZEN", False):
            self.assertFalse(si.update_check_enabled(opt_out=False))
        with mock.patch.object(si, "FROZEN", True), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SQUARE_IMPACT_NO_UPDATE_CHECK", None)
            self.assertTrue(si.update_check_enabled(opt_out=False))
            self.assertFalse(si.update_check_enabled(opt_out=True))
            os.environ["SQUARE_IMPACT_NO_UPDATE_CHECK"] = "1"
            self.assertFalse(si.update_check_enabled(opt_out=False))


class Logo(unittest.TestCase):
    def test_icon_is_a_rounded_square(self):
        px = si.logo_rgba(32)
        alpha = lambda x, y: px[(y * 32 + x) * 4 + 3]
        self.assertEqual(len(px), 32 * 32 * 4)
        self.assertEqual(alpha(0, 0), 0)        # rounded corner is transparent
        self.assertEqual(alpha(16, 16), 255)    # body is opaque
        self.assertEqual(alpha(16, 0), 255)

    def test_png_decodes(self):
        png = si.logo_png(16)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(png[12:16], b"IHDR")
        self.assertEqual(png[16:26], bytes([0, 0, 0, 16, 0, 0, 0, 16, 8, 6]))  # 16x16 RGBA
        idat = png.index(b"IDAT")
        n = int.from_bytes(png[idat - 4:idat], "big")
        raw = zlib.decompress(png[idat + 4:idat + 4 + n])
        self.assertEqual(len(raw), 16 * (1 + 16 * 4))

    def test_ico_lists_every_size(self):
        ico = si.logo_ico()
        self.assertEqual(ico[:6], bytes([0, 0, 1, 0, len(si.ICON_SIZES), 0]))
        for k, size in enumerate(si.ICON_SIZES):
            w, _, _, _, _, bpp, n, off = struct.unpack_from("<BBBBHHII", ico, 6 + 16 * k)
            self.assertEqual(w, size % 256)     # 0 means 256
            self.assertEqual(bpp, 32)
            self.assertEqual(ico[off:off + n], si.logo_png(size))

    def test_console_logo_is_square(self):
        lines = si.logo_lines(14)
        self.assertEqual(len(lines), 7)                   # two pixel rows per text line
        for line in lines:
            self.assertEqual(len(re.sub(r"\033\[[0-9;]*m", "", line)), 14)

    def test_banner_is_plain_when_redirected(self):
        import contextlib
        import io
        buf = io.StringIO()   # not a tty: no logo, no escape codes
        with contextlib.redirect_stdout(buf):
            si.banner("live")
        self.assertIn("square_impact", buf.getvalue())
        self.assertNotIn("\033", buf.getvalue())
        self.assertNotIn("▀", buf.getvalue())

    def test_no_logo_on_a_terminal_without_colour(self):
        import contextlib
        import io
        from unittest import mock

        class Terminal(io.StringIO):
            encoding = "utf-8"

            def isatty(self):
                return True

        buf = Terminal()
        with contextlib.redirect_stdout(buf), mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            si.banner("live")
        self.assertIn("square_impact", buf.getvalue())
        self.assertNotIn("\033", buf.getvalue())
        self.assertNotIn("▀", buf.getvalue())

    def test_overlay_events_send_pings_the_page_can_see(self):
        """The page's watchdog reconnects after 15 s of silence, so /events must
        send named ping events (SSE comments never reach the page's script)."""
        import urllib.request
        from unittest import mock
        with mock.patch.object(si, "OVERLAY_PING", 0.2):
            srv = si.serve_overlay(si.Overlay(), 0)
            try:
                url = f"http://127.0.0.1:{srv.server_address[1]}/events"
                with urllib.request.urlopen(url, timeout=5) as r:
                    lines = [r.readline() for _ in range(5)]
            finally:
                srv.shutdown()
                srv.server_close()
        self.assertEqual(lines[0], b"retry: 2000\n")
        self.assertIn(b"event: ping\n", lines)

    def test_overlay_serves_favicon(self):
        import urllib.request
        srv = si.serve_overlay(si.Overlay(), 0)   # port 0: any free port
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            with urllib.request.urlopen(base + "/") as r:
                self.assertIn(b'rel="icon"', r.read())
            with urllib.request.urlopen(base + "/favicon.png") as r:
                self.assertEqual(r.headers["Content-Type"], "image/png")
                self.assertEqual(r.read(), si.logo_png(32))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
