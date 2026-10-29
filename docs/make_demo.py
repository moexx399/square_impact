"""Make docs/demo.gif, the README's example replay, from a saved shot.

    python docs/make_demo.py SHOT_FOLDER [--out docs/demo.gif]

The frames are exactly what the OBS overlay and viewers show for that shot with
the default settings (trimmed, enhanced, ball crop), played at the overlay's
speed with its pause on the impact frame, looping. Standard library only, so
it includes a small GIF encoder.
"""

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import square_impact as si  # noqa: E402

LEVELS = 64   # grey levels kept: plenty for the eye, and far smaller than 256 (the grain compresses badly)


def lzw(indices, min_bits):
    """GIF-flavoured LZW of a frame's palette indices -> data sub-blocks."""
    clear, end = 1 << min_bits, (1 << min_bits) + 1
    out, acc, nacc = bytearray(), 0, 0

    def emit(code, bits):
        nonlocal acc, nacc
        acc |= code << nacc
        nacc += bits
        while nacc >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nacc -= 8

    table = {bytes([i]): i for i in range(clear)}
    nxt, bits = end + 1, min_bits + 1
    emit(clear, bits)
    w = b""
    for i in indices:
        wc = w + bytes([i])
        if wc in table:
            w = wc
            continue
        emit(table[w], bits)
        if nxt < 4096:
            table[wc] = nxt
            if nxt == 1 << bits and bits < 12:
                bits += 1
            nxt += 1
        else:   # table full: start over
            emit(clear, bits)
            table = {bytes([k]): k for k in range(clear)}
            nxt, bits = end + 1, min_bits + 1
        w = bytes([i])
    if w:
        emit(table[w], bits)
    emit(end, bits)
    if nacc:
        out.append(acc & 0xFF)
    return b"".join(bytes([len(out[k:k + 255])]) + out[k:k + 255] for k in range(0, len(out), 255)) + b"\0"


def write_gif(path, w, h, frames, delays_cs):
    """Looping greyscale GIF: frames are 8-bit grey, delays in 1/100 s."""
    step = 256 // LEVELS
    palette = b"".join(bytes([min(255, k * step + step // 2)] * 3) for k in range(LEVELS))
    min_bits = max(2, (LEVELS - 1).bit_length())
    to_index = bytes(v // step for v in range(256))
    gif = bytearray(b"GIF89a" + struct.pack("<HHBBB", w, h, 0xF0 | (min_bits - 1), 0, 0) + palette)
    gif += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"   # loop forever
    for f, delay in zip(frames, delays_cs):
        gif += b"\x21\xf9\x04\x00" + struct.pack("<H", delay) + b"\x00\x00"
        gif += b"\x2c" + struct.pack("<HHHHB", 0, 0, w, h, 0) + bytes([min_bits])
        gif += lzw(f.translate(to_index), min_bits)
    gif += b"\x3b"
    with open(path, "wb") as fh:
        fh.write(gif)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shot", help="a saved shot folder (shots/YYYYMMDD_HHMMSS_shot_r1)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo.gif"))
    a = ap.parse_args()
    si.load_settings({})   # the defaults, not anyone's square_impact.json
    segs = si.display_segments(a.shot)
    seg = segs[0]          # the launch camera's swing
    frame_cs = round(100 / si.setting("fps"))
    delays = [frame_cs + (si.setting("hold") // 10 if i == seg["impact"] else 0)
              for i in range(len(seg["frames"]))]
    delays[-1] += si.setting("gap") // 10
    write_gif(a.out, seg["w"], seg["h"], seg["frames"], delays)
    print(f"wrote {a.out}: {seg['w']}x{seg['h']}, {len(seg['frames'])} frames, "
          f"{os.path.getsize(a.out) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
