#!/usr/bin/env python3
"""
test_roundtrip.py — Decode → re-encode → verify fidelity.

Usage:
    python tests/test_roundtrip.py <original.bin> [<reencoded.bin>]

If only one file is given, decodes it and reports structure.
If two files are given, compares them for semantic equivalence.
"""

import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))

from encode_quat_v4 import decode_anim_game_formula


def quat_magnitude(frame_channel):
    return math.sqrt(sum(frame_channel[i] ** 2 for i in range(4)))


def test_decode(path: str):
    data = open(path, 'rb').read()
    d = decode_anim_game_formula(data)
    nch, nfr = d['channel_count'], d['frame_count']

    bad = 0
    max_mag = 0.0
    for f in range(nfr):
        for ch in range(nch):
            mag = quat_magnitude(d['frames'][f][ch])
            max_mag = max(max_mag, mag)
            if abs(mag - 1.0) > 0.05:
                bad += 1

    print(f"File: {path}")
    print(f"  Size: {len(data)} bytes")
    print(f"  Channels: {nch}, Frames: {nfr}")
    print(f"  Invalid quaternions: {bad}")
    print(f"  Max quat magnitude: {max_mag:.4f}")
    return bad == 0


def test_compare(orig_path: str, new_path: str):
    orig = decode_anim_game_formula(open(orig_path, 'rb').read())
    new = decode_anim_game_formula(open(new_path, 'rb').read())

    nch_o, nfr_o = orig['channel_count'], orig['frame_count']
    nch_n, nfr_n = new['channel_count'], new['frame_count']

    print(f"Original: {nch_o} ch x {nfr_o} fr")
    print(f"New:      {nch_n} ch x {nfr_n} fr")

    if nch_o != nch_n or nfr_o != nfr_n:
        print("  MISMATCH: channel/frame counts differ")
        return False

    # Quaternion validity
    bad = sum(
        1 for f in range(nfr_n) for ch in range(nch_n)
        if abs(quat_magnitude(new['frames'][f][ch]) - 1.0) > 0.05
    )

    # Position/scale preservation (should be exact if using template path)
    pos_max = max(
        abs(orig['frames'][f][ch][i] - new['frames'][f][ch][i])
        for f in range(nfr_o) for ch in range(nch_o) for i in range(4, 10)
    )

    # Quaternion differences (motion)
    quat_diff_channels = 0
    for ch in range(nch_o):
        moved = max(
            abs(orig['frames'][f][ch][i] - new['frames'][f][ch][i])
            for f in range(nfr_o) for i in range(3)
        )
        if moved > 1e-3:
            quat_diff_channels += 1

    print(f"  Invalid quaternions in new: {bad}")
    print(f"  Max pos/scale diff: {pos_max:.6f}")
    print(f"  Channels with quat motion: {quat_diff_channels}/{nch_o}")

    ok = bad == 0 and pos_max < 1e-5
    print(f"  Result: {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    orig_path = sys.argv[1]

    if len(sys.argv) >= 3:
        new_path = sys.argv[2]
        ok = test_compare(orig_path, new_path)
    else:
        ok = test_decode(orig_path)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
