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
import subprocess
import sys
import tempfile
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
        r"kill|pkill|killall|chmod|chown|ln|truncate|fw_setenv|uci|sysupgrade)\b",
        r"\bof=",
    ]

    def commands(self):
        lay = {"nbuf": 256, "stride": 0x40000, "w": 1280, "h": 192}
        yield si.session_cmd(lay, 0, 0, 18)
        yield si.session_cmd(lay, 1, 40, 31, trigger=False, f_fixed=250)
        yield si.shot_trigger("echo x")
        yield si.event_trigger("echo x")

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

    @unittest.skipUnless(shutil.which("sh"), "needs a POSIX sh")
    def test_event_trigger_parses_sqocam_log(self):
        """The trigger ignores lines from before arming and captures the event buffer."""
        lines = [
            "Jan 1 x sqocam: ##T 5.0 time : sqp_bfind_fire[99, 99] fire(1) bgn(1), end(2)",
            "Jan 1 x sqocam: ##T 5.4 sqp_bfind_shot_data return : 1 ",
            "Jan 1 x sqocam: ##T 99999.1 time : sqp_bfind_fire[34, 34] fire(1) bgn(32), end(49)",
            "Jan 1 x sqocam: ##T 99999.5 sqp_bfind_shot_data return : 1 ",
        ]
        cmd = si.event_trigger("echo EVENT=$F").replace(
            "U=$(cut -d. -f1 /proc/uptime)", "U=1000").replace(
            f"logread -f -e 'sqp_bfind_fire\\[|{si.SHOT_LINE}'", "printf '%s\\n' \"$@\"")
        out = subprocess.run(["sh", "-c", cmd, "sh", *lines], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "EVENT=34", out.stderr)


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

    def test_overlay_payload_orders_frames_and_stacks_cams(self):
        d = os.path.join(self.tmp, "20261011_140000_r1_shot_cam01")
        os.makedirs(d)
        w, h, idxs = 4, 2, [254, 255, 0, 1]   # wraps around the ring
        for b in idxs:
            for cam in (0, 1):
                si.write_pgm(os.path.join(d, f"cam{cam}_{b:03d}.pgm"), w, h,
                             bytes([b % 256 if cam == 0 else 200]) * (w * h))
        with open(os.path.join(d, "meta.json"), "w") as fh:
            json.dump({"w": w, "h": h, "idxs": idxs, "cams": [0, 1], "impact": 0}, fh)
        header, data = si.overlay_payload(d, "info")
        self.assertEqual((header["w"], header["h"], header["n"], header["impact"]), (4, 4, 4, 2))
        self.assertEqual(len(data), 4 * w * h * 2)
        # frame k = cam0 then cam1
        self.assertEqual(data[0], 254)
        self.assertEqual(data[w * h], 200)
        self.assertEqual(data[2 * w * h], 255)

    def test_png_writer(self):
        path = os.path.join(self.tmp, "t.png")
        si.write_png(path, 3, 2, bytes(range(6)))
        with open(path, "rb") as fh:
            png = fh.read()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
        self.assertEqual(zlib.decompress(idat), b"\x00\x00\x01\x02\x00\x03\x04\x05")


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


if __name__ == "__main__":
    unittest.main()
