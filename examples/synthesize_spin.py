#!/usr/bin/env python3
"""
synthesize_spin.py — Pure-code example: synthesize a 360-degree spin
and encode it without any game files or Blender.

This demonstrates the encoder API with a minimal synthetic case.
For a real injection, see docs/PIPELINE.md.
"""

import math
import struct
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))

from encode_quat_v4 import encode_deltas


def make_minimal_template(n_channels: int, n_frames: int, bpf: int = 96) -> bytes:
    """
    Build a minimal QUAT_BITSTREAM32 template header for demonstration.
    In a real injection, you would extract this from the original animation.
    """
    # This is a simplified stub. Real templates come from extract_anim.py.
    # The encoder needs: header with mode bytes, base/bitcount tables, trailer.
    # For a full working example, use the real pipeline:
    #   extract_anim.py → modify → inject_animation.py
    raise NotImplementedError(
        "This example demonstrates delta synthesis only.\n"
        "For a real injection, use the full pipeline in docs/PIPELINE.md:\n"
        "  1. extract_anim.py to get the original template\n"
        "  2. synthesize or author your deltas\n"
        "  3. inject_animation.py to encode and inject"
    )


def synthesize_spin_deltas(n_channels: int, n_frames: int):
    """
    Generate delta quaternions for a 360-degree spin on channel 0,
    with all other channels held at identity (no rotation).
    """
    deltas = {}
    for ch in range(n_channels):
        deltas[ch] = []
        for f in range(n_frames):
            if ch == 0:
                # Full 360-degree rotation around Z over the clip
                angle = 2.0 * math.pi * f / n_frames
                # Delta quaternion XYZ (W is reconstructed): (0, 0, sin(angle/2))
                deltas[ch].append((0.0, 0.0, math.sin(angle / 2.0)))
            else:
                # Identity delta (no rotation)
                deltas[ch].append((0.0, 0.0, 0.0))
    return deltas


def demo():
    n_channels = 27
    n_frames = 36
    deltas = synthesize_spin_deltas(n_channels, n_frames)

    print(f"Synthesized {n_channels} channels x {n_frames} frames")
    print(f"Channel 0 spins 360 degrees; all others held at identity")
    print(f"Frame 0, ch0 delta: {deltas[0][0]}")
    print(f"Frame {n_frames//4}, ch0 delta: {deltas[0][n_frames//4]}")
    print(f"Frame {n_frames//2}, ch0 delta: {deltas[0][n_frames//2]}")
    print()
    print("To encode this into a real animation, you need a template")
    print("extracted from the game. See docs/PIPELINE.md for the full workflow.")


if __name__ == "__main__":
    demo()
