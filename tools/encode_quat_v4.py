"""
QUAT_BITSTREAM32 Encoder v4 — VERIFIED FORMAT (game-binary + PDB + real-data confirmed).

Format facts this encoder implements (all verified 2026-10-16):
  - Formula: value = (unsigned_raw + base_A) * scale_B   [NO centering subtraction]
  - Table A: ONE int32 BE base value per component SLOT, reused every frame
  - Bit-widths: ONE byte per slot, reused every frame  (count = width - 1)
  - Mode bytes: fixed for the whole animation (36 bytes for 27 channels typical)
  - Per-frame: base_idx/count_idx/mode_idx RESET to 0; only bit position advances
  - base = min(raw_signed across all frames); width covers (max - min)
  - Frame stride: ceil(total_bits / 32) * 32  (32-bit word aligned)
  - Bitstream: LSB-first bits packed into big-endian 32-bit words
  - Quaternion W never stored: W = sqrt(max(0, 1 - X^2 - Y^2 - Z^2)), always >= 0
  - Animation stores DELTA rotations from bind pose; GLB = bind x delta
  - mode_flag at hv+0x26 = 1 (32-bit bases, byte bit-counts)

Usage:
  python encode_quat_v4.py                 # roundtrip test vs real crocodile_eat
  from encode_quat_v4 import encode_deltas  # as a library
"""
import struct
import math
import os
import sys

# ---------------------------------------------------------------------------
# Constants from real game data (crocodile_eat)
# ---------------------------------------------------------------------------
SCALE_QUAT = 1.0 / 32768.0   # comps 0-2
SCALE_POS  = 1.0 / 65536.0   # comps 3-5
SCALE_SCL  = 1.0 / 8192.0    # comps 6-8
SCALES = [SCALE_QUAT] * 3 + [SCALE_POS] * 3 + [SCALE_SCL] * 3
DEFAULTS = [0.0] * 6 + [1.0] * 3
COMPS_PER_CH = 9
MODE_FLAG_32BIT = 1

ANIM_HEADER_SIZE = 0x60
QUAT_STR_OFFSET = 0x60
QUAT_HEADER_OFFSET = 0x7C   # hv; QBS descriptor fields read relative to here

# QBS descriptor field offsets (relative to .data start; hv = 0x7C)
# +0x00 f32 1.0 | +0x04 scale_off | +0x08 default_off | +0x0C mode_off
# +0x10 base_off | +0x14 bitcount_off | +0x18 sizeA u16 | +0x1A sizeB u16
# +0x1C bits_per_frame u16 | +0x1E comps_per_ch u16 | +0x20 var12 u16
# +0x22 channel_count u16 | +0x24 frame_count u16 | +0x26 mode_flag u8


# ---------------------------------------------------------------------------
# Quaternion helpers
# ---------------------------------------------------------------------------
def quat_mult(q1, q2):
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return (
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
    )


def quat_conj(q):
    return (-q[0], -q[1], -q[2], q[3])


def quat_normalize(q):
    n = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])
    if n < 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


# ---------------------------------------------------------------------------
# Bit writer: LSB-first bits into big-endian 32-bit words
# (matches decoder: combined = (word2 << (32-bitoff)) | (word1 >> bitoff))
# ---------------------------------------------------------------------------
class BitWriter:
    def __init__(self):
        self.bits = []

    def write(self, value, num_bits):
        """Write num_bits of value, LSB first."""
        for i in range(num_bits):
            self.bits.append((value >> i) & 1)

    def pad_to_bits(self, total_bits):
        while len(self.bits) < total_bits:
            self.bits.append(0)

    def to_bytes(self):
        # Pad to 32-bit word boundary
        while len(self.bits) % 32 != 0:
            self.bits.append(0)
        out = bytearray()
        for i in range(0, len(self.bits), 32):
            word = 0
            for j in range(32):
                word |= self.bits[i + j] << j
            out.extend(struct.pack(">I", word))
        return bytes(out)


# ---------------------------------------------------------------------------
# Core encoder: delta components -> QUAT_BITSTREAM32 .data blob
# ---------------------------------------------------------------------------
def _parse_orig_structure(template_data, channel_count):
    """Parse original animation's COMPLETE slot structure from template.
    Returns: (slot_states, mode_bytes, orig_bases, orig_counts)
    slot_states: dict {(ch, ci): "FULL"|"SKIP"|None} for every component
    """
    def be16(o): return struct.unpack_from(">H", template_data, o)[0]
    def be32(o): return struct.unpack_from(">I", template_data, o)[0]

    hv = QUAT_HEADER_OFFSET
    mode_off = be32(hv + 0x0C)
    base_off = be32(hv + 0x10)
    bc_off = be32(hv + 0x14)
    sizeA = be16(hv + 0x18)
    sizeB = be16(hv + 0x1A)
    mode_flag = template_data[hv + 0x26]

    modes = bytes(template_data[mode_off:base_off])
    bases = []
    if mode_flag == 0:
        for i in range(sizeA // 2):
            bases.append(struct.unpack_from(">h", template_data, base_off + i * 2)[0])
    else:
        for i in range(sizeA // 4):
            bases.append(struct.unpack_from(">i", template_data, base_off + i * 4)[0])
    counts = list(template_data[bc_off:bc_off + sizeB])

    slot_states = {}
    slot_order = []
    ch = 0
    idx = 0
    base_idx = 0
    count_idx = 0
    while ch < channel_count and idx < len(modes):
        mb0 = modes[idx]
        gs = mb0 & 3
        present = [(mb0 >> 7) & 1, (mb0 >> 6) & 1, (mb0 >> 5) & 1]
        skip = [(mb0 >> 4) & 1, (mb0 >> 3) & 1, (mb0 >> 2) & 1]
        for ci in range(3):
            if present[ci]:
                state = "SKIP" if skip[ci] else "FULL"
                slot_states[(ch, ci)] = state
                slot_order.append((ch, ci))
                base_idx += 1
                if state == "FULL":
                    count_idx += 1
        idx += 1
        if gs in (1, 2, 3):
            mb = modes[idx] if idx < len(modes) else 0
            p = [(mb >> 7) & 1, (mb >> 6) & 1, (mb >> 5) & 1]
            s = [(mb >> 4) & 1, (mb >> 3) & 1, (mb >> 2) & 1]
            coff = 6 if gs == 1 else 3
            for ci in range(3):
                if p[ci]:
                    state = "SKIP" if s[ci] else "FULL"
                    slot_states[(ch, ci + coff)] = state
                    slot_order.append((ch, ci + coff))
                    base_idx += 1
                    if state == "FULL":
                        count_idx += 1
            idx += 1
        if gs == 3:
            mb = modes[idx] if idx < len(modes) else 0
            p = [(mb >> 7) & 1, (mb >> 6) & 1, (mb >> 5) & 1]
            s = [(mb >> 4) & 1, (mb >> 3) & 1, (mb >> 2) & 1]
            for ci in range(3):
                if p[ci]:
                    state = "SKIP" if s[ci] else "FULL"
                    slot_states[(ch, ci + 6)] = state
                    slot_order.append((ch, ci + 6))
                    base_idx += 1
                    if state == "FULL":
                        count_idx += 1
            idx += 1
        ch += 1
    return slot_states, slot_order, modes, bases, counts


def _parse_orig_skip_slots(template_data, channel_count):
    """Parse original animation's mode bytes to find (ch, ci) pairs that are SKIP
    (present+skip bits set). Also returns their base values.
    Returns: dict {(ch, ci): base_value} for all SKIP slots."""
    def be16(o): return struct.unpack_from(">H", template_data, o)[0]
    def be32(o): return struct.unpack_from(">I", template_data, o)[0]

    hv = QUAT_HEADER_OFFSET
    mode_off = be32(hv + 0x0C)
    base_off = be32(hv + 0x10)
    sizeA = be16(hv + 0x18)
    mode_flag = template_data[hv + 0x26]

    modes = template_data[mode_off:base_off]
    bases = []
    if mode_flag == 0:
        for i in range(sizeA // 2):
            bases.append(struct.unpack_from(">h", template_data, base_off + i * 2)[0])
    else:
        for i in range(sizeA // 4):
            bases.append(struct.unpack_from(">i", template_data, base_off + i * 4)[0])

    skip_slots = {}
    ch = 0
    idx = 0
    base_idx = 0
    while ch < channel_count and idx < len(modes):
        mb0 = modes[idx]
        gs = mb0 & 3
        present = [(mb0 >> 7) & 1, (mb0 >> 6) & 1, (mb0 >> 5) & 1]
        skip = [(mb0 >> 4) & 1, (mb0 >> 3) & 1, (mb0 >> 2) & 1]
        for ci in range(3):
            if present[ci]:
                if skip[ci]:
                    b = bases[base_idx] if base_idx < len(bases) else 0
                    skip_slots[(ch, ci)] = b
                base_idx += 1
        idx += 1
        if gs in (1, 2, 3):
            mb = modes[idx] if idx < len(modes) else 0
            p = [(mb >> 7) & 1, (mb >> 6) & 1, (mb >> 5) & 1]
            s = [(mb >> 4) & 1, (mb >> 3) & 1, (mb >> 2) & 1]
            coff = 6 if gs == 1 else 3
            for ci in range(3):
                if p[ci]:
                    if s[ci]:
                        b = bases[base_idx] if base_idx < len(bases) else 0
                        skip_slots[(ch, ci + coff)] = b
                    base_idx += 1
            idx += 1
        if gs == 3:
            mb = modes[idx] if idx < len(modes) else 0
            p = [(mb >> 7) & 1, (mb >> 6) & 1, (mb >> 5) & 1]
            s = [(mb >> 4) & 1, (mb >> 3) & 1, (mb >> 2) & 1]
            for ci in range(3):
                if p[ci]:
                    if s[ci]:
                        b = bases[base_idx] if base_idx < len(bases) else 0
                        skip_slots[(ch, ci + 6)] = b
                    base_idx += 1
            idx += 1
        ch += 1
    return skip_slots


def encode_deltas(delta_xyz, channel_count, frame_count, scales=None, template_header=None):
    """
    Encode per-channel per-frame delta components.

    delta_xyz: dict {channel_index: [(c0, c1, c2), ...] or [(c0..c8), ...]}
               Length-3 tuples = quat XYZ only (pos/scale absent).
               Length-9 tuples = all components (quat XYZ, pos XYZ, scale XYZ).
               Missing channels or frames are treated as absent (default 0).
    template_header: optional bytes of the original animation (full .data blob).
               When provided, the header region (0x00-0x7B) is copied from the
               template so that metadata offsets (pose blocks, events, etc.)
               are preserved. Only duration/channel_count/frame_count/QBS fields
               are overwritten with our values. Also preserves SKIP-vs-ABSENT
                classification for all-default components.
    Returns: bytes of the animation .data section (header + QBS + tables + bitstream).
    """
    if scales is None:
        scales = SCALES

    # Parse original SKIP slots from the template to preserve
    # SKIP-vs-ABSENT classification for all-default components.
    orig_skip_slots = {}
    orig_structure = None
    if template_header is not None:
        try:
            orig_skip_slots = _parse_orig_skip_slots(template_header, channel_count)
            orig_structure = _parse_orig_structure(template_header, channel_count)
        except Exception:
            orig_skip_slots = {}
            orig_structure = None

    # When a template is provided, preserve the original's COMPLETE
    # slot structure (mode bytes, FULL/SKIP classification, base count, widths).
    # Only re-encode the bitstream residuals from the new input data.
    # This prevents the game from reading wrong offsets and crashing.
    if orig_structure is not None:
        return _encode_with_template_structure(
            delta_xyz, channel_count, frame_count, scales,
            template_header, orig_structure
        )

    # --- Pass 1: decide per-channel mode + per-slot states ---
    # Each channel can have 9 components: quat XYZ (0-2), pos XYZ (3-5), scale XYZ (6-8).
    # Group selection: 0=quat only, 1=+scale, 2=+pos, 3=+pos+scale.
    # Components absent if all frames are ~0 (or ~1 for scale defaults).

    ch_slots = {}   # ch -> list of (comp_index, state, base, width, raw_seq)
    mode_bytes = bytearray()
    slot_order = []  # (ch, comp_index) in write order

    for ch in range(channel_count):
        frames_data = delta_xyz.get(ch, [])
        # Detect component count from first frame tuple
        ncomps_input = len(frames_data[0]) if frames_data else 3

        # Determine per-component statistics across frames
        comp_stats = []
        for ci in range(COMPS_PER_CH):
            # Components beyond input length are absent
            if ci >= ncomps_input:
                comp_stats.append(None)
                continue
            # Scale components: default is 1.0, so check deviation from 1.0
            if ci >= 6:
                default_val = 1.0
            else:
                default_val = 0.0
            vals = []
            for f in range(frame_count):
                if f < len(frames_data):
                    v = frames_data[f][ci]
                else:
                    v = default_val
                vals.append(v)
            # Present if any value deviates from default
            if all(abs(v - default_val) < 1e-7 for v in vals):
                # If the original had this slot as SKIP, preserve
                # that classification instead of marking absent.
                if (ch, ci) in orig_skip_slots:
                    orig_base = orig_skip_slots[(ch, ci)]
                    comp_stats.append({"state": "SKIP", "base": orig_base, "seq": None})
                else:
                    comp_stats.append(None)  # absent
                continue
            # Constant? -> SKIP (store base only)
            first = vals[0]
            if all(abs(v - first) < 1e-7 for v in vals):
                raw_const = round(first / scales[ci])
                comp_stats.append({"state": "SKIP", "base": raw_const, "seq": None})
                continue
            # FULL: raw_signed sequence
            seq = [round(v / scales[ci]) for v in vals]
            base = min(seq)
            width = 1
            span = max(seq) - base
            while (1 << width) - 1 < span:
                width += 1
                if width > 32:
                    width = 32
                    break
            comp_stats.append({"state": "FULL", "base": base, "width": width, "seq": seq})

        # Build group selection for this channel
        g0 = comp_stats[0:3]   # quat xyz
        g3 = comp_stats[3:6]   # pos xyz
        g6 = comp_stats[6:9]   # scale xyz

        def group_active(g):
            return any(s is not None for s in g)

        active0 = group_active(g0)
        active3 = group_active(g3)
        active6 = group_active(g6)

        if active3 and active6:
            group_sel = 3
        elif active3:
            group_sel = 2
        elif active6:
            group_sel = 1
        else:
            group_sel = 0

        def make_group_byte(g):
            mb = 0
            for i, s in enumerate(g):
                if s is None:
                    continue
                mb |= (0x80 >> i)  # present bit
                if s["state"] == "SKIP":
                    mb |= (0x10 >> i)  # skip bit (bit 4/3/2 for c0/c1/c2)
            return mb

        mb0 = make_group_byte(g0)
        mb0 = (mb0 & ~0x03) | group_sel
        mode_bytes.append(mb0)
        if group_sel in (1, 2, 3):
            mode_bytes.append(make_group_byte(g3 if group_sel in (2, 3) else g6))
        if group_sel == 3:
            mode_bytes.append(make_group_byte(g6))

        ch_slots[ch] = comp_stats
        # Slot write order: group0 comps, then group3 or group6 per selection
        for ci in range(3):
            if g0[ci] is not None:
                slot_order.append((ch, ci))
        if group_sel in (2, 3):
            for ci in range(3, 6):
                if g3[ci - 3] is not None:
                    slot_order.append((ch, ci))
        if group_sel in (1, 3):
            for ci in range(6, 9):
                if g6[ci - 6] is not None:
                    slot_order.append((ch, ci))

    # --- Pass 2: collect Table A + bit-width stream in slot order ---
    # Decoder semantics (verified vs real data sizeB=n_slots):
    #   base_idx AND count_idx BOTH advance on every slot (FULL and SKIP).
    #   SKIP just doesn't USE the count (no bits read).
    # So counts are PER-SLOT: one byte each, SKIP slots store 0 (unused).
    bases = []
    counts = []          # one entry per slot (FULL and SKIP)
    total_bits_per_frame = 0
    for (ch, ci) in slot_order:
        s = ch_slots[ch][ci]
        bases.append(int(s["base"]))
        if s["state"] == "FULL":
            counts.append(s["width"] - 1)
            total_bits_per_frame += s["width"]
        # SKIP slots get NO count byte (matches the game decoder)

    # Trailing pad byte: the original always has one.
    counts.append(0)

    n_slots = len(slot_order)
    bits_per_frame = ((total_bits_per_frame + 31) // 32) * 32
    bw = BitWriter()
    for f in range(frame_count):
        frame_start = f * bits_per_frame
        bw.pad_to_bits(frame_start)
        for (ch, ci) in slot_order:
            s = ch_slots[ch][ci]
            if s["state"] != "FULL":
                continue
            raw = s["seq"][f] if f < len(s["seq"]) else (s["seq"][-1] if s["seq"] else s["base"])
            residual = raw - s["base"]
            bw.write(residual, s["width"])
        bw.pad_to_bits((f + 1) * bits_per_frame)

    # Trailing padding: the bit reader reads TWO 32-bit words (8-byte
    # lookahead) from its current position. Without spare bytes after the
    # last residual, the final reads of the last frame hit the EOF guard
    # and return 0. Real game files carry kilobytes of trailing padding.
    bitstream = bw.to_bytes() + b"\x00" * 16

    # --- Assemble file layout ---
    mode_off = 236
    # Layout: header(0x60) + quat_str(0x1C) + qbs_header(to 0x7C+0x28=0xA4)
    # Scales at 164, defaults at 200, modes at 236, bases at 272...
    # Scales: 9 floats = 36 bytes -> 164..199
    # Defaults: 9 floats = 36 bytes -> 200..235
    # Modes: len(mode_bytes) -> 236..
    # Bases: n_slots * 4 -> base_off..
    # Counts: n_slots bytes -> bitcount_off..
    # Bitstream -> bitstream_base..
    base_off = mode_off + len(mode_bytes)
    # Align base_off to 4 bytes
    if base_off % 4:
        base_off += 4 - (base_off % 4)
    bitcount_off = base_off + n_slots * 4
    bitstream_base = bitcount_off + len(counts)  # counts, not n_slots
    sizeA = n_slots * 4
    sizeB = len(counts)  # actual count bytes written
    total_size = bitstream_base + len(bitstream)
    data = bytearray(total_size)

    # Animation header
    if template_header is not None and len(template_header) >= 0x7C:
        # Copy template header region (0x00-0x7B) to preserve metadata offsets
        # (pose blocks, event data, duration, etc.) that the game follows at runtime.
        data[0:0x7C] = template_header[0:0x7C]
        # Do NOT overwrite duration — trust the template's value
        struct.pack_into(">H", data, 0x20, channel_count)
        struct.pack_into(">H", data, 0x22, frame_count)
        struct.pack_into(">I", data, 0x24, QUAT_STR_OFFSET)
    else:
        data[0:10] = b"animation\x00"
        data[10:24] = b"19.12.06.0036\x00"
        duration = frame_count / 30.0
        struct.pack_into(">f", data, 0x1C, duration)
        struct.pack_into(">H", data, 0x20, channel_count)
        struct.pack_into(">H", data, 0x22, frame_count)
        struct.pack_into(">I", data, 0x24, QUAT_STR_OFFSET)

    # QUAT_BITSTREAM32 string
    data[0x60:0x71] = b"QUAT_BITSTREAM32\x00"

    # QBS descriptor (at hv = 0x7C)
    hv = QUAT_HEADER_OFFSET
    struct.pack_into(">f", data, hv + 0x00, 1.0)
    struct.pack_into(">I", data, hv + 0x04, 164)          # scale_off
    struct.pack_into(">I", data, hv + 0x08, 200)          # default_off
    struct.pack_into(">I", data, hv + 0x0C, mode_off)     # mode_off
    struct.pack_into(">I", data, hv + 0x10, base_off)     # base_off
    struct.pack_into(">I", data, hv + 0x14, bitcount_off) # bitcount_off
    var36 = sizeA  # per PDB: baseStreamSize
    var35 = sizeB  # per PDB: bitCountStreamSize
    struct.pack_into(">H", data, hv + 0x18, var36)
    struct.pack_into(">H", data, hv + 0x1A, var35)
    struct.pack_into(">H", data, hv + 0x1C, bits_per_frame // 8)  # bytes
    struct.pack_into(">H", data, hv + 0x1E, COMPS_PER_CH)
    struct.pack_into(">H", data, hv + 0x20, 12)
    struct.pack_into(">H", data, hv + 0x22, channel_count)
    struct.pack_into(">H", data, hv + 0x24, frame_count)
    data[hv + 0x26] = MODE_FLAG_32BIT

    # Scales (at 164)
    for i in range(COMPS_PER_CH):
        struct.pack_into(">f", data, 164 + i * 4, scales[i])

    # Defaults (at 200)
    for i in range(COMPS_PER_CH):
        struct.pack_into(">f", data, 200 + i * 4, DEFAULTS[i])

    # Mode bytes (at mode_off)
    data[mode_off:mode_off + len(mode_bytes)] = mode_bytes

    # Table A bases (int32 BE)
    for i, b in enumerate(bases):
        struct.pack_into(">i", data, base_off + i * 4, b)

    # Bit-width counts (1 byte each, count = width - 1)
    for i, c in enumerate(counts):
        data[bitcount_off + i] = c & 0xFF

    # Bitstream
    data[bitstream_base:bitstream_base + len(bitstream)] = bitstream

    # Append the trailer from the template (pose block, event tables, etc.)
    # The game follows header offsets (0x1088, 0x18FC, 0x1A54) into this region.
    # Without it, the game reads garbage pointers and crashes.
    # PAD to 0xFC0 first so the trailer lands at the original offsets.
    TRAILER_OFFSET = 0xFC0
    if template_header is not None and len(template_header) > TRAILER_OFFSET:
        if len(data) < TRAILER_OFFSET:
            data = data + b"\x00" * (TRAILER_OFFSET - len(data))
        data = data[:TRAILER_OFFSET] + template_header[TRAILER_OFFSET:]

    return bytes(data)


# ---------------------------------------------------------------------------
# Game-formula decoder (EXACT: per-frame reset, no centering) for roundtrip
# ---------------------------------------------------------------------------
class _BitReader:
    def __init__(self, data, base, byte_off=0, bit_off=0):
        self.data = data
        self.base = base
        self.byte_off = byte_off
        self.bit_off = bit_off

    def read(self, num_bits):
        off = self.base + self.byte_off
        if off + 8 > len(self.data):
            return 0
        word1 = struct.unpack_from(">I", self.data, off)[0]
        word2 = struct.unpack_from(">I", self.data, off + 4)[0]
        if self.bit_off == 0:
            combined = word1
        else:
            combined = ((word2 << (32 - self.bit_off)) | (word1 >> self.bit_off)) & 0xFFFFFFFF
        mask = 0xFFFFFFFF if num_bits >= 32 else (1 << num_bits) - 1
        result = combined & mask
        total = self.bit_off + num_bits
        self.bit_off = total & 0x1F
        self.byte_off += (total >> 3) & ~3
        return result


# ---------------------------------------------------------------------------
# Original-template component recovery cache
# ---------------------------------------------------------------------------
# When the new input (Blender) does not provide a component that the original
# animation stored as FULL (Blender exports quat XYZ only; pos/scale are
# missing), the ORIGINAL animation's decoded values are used instead of zeros.
_orig_template_cache = {}


def _orig_component_values(template_header, ch, ci, frame_count):
    """Return a list of frame_count original decoded values for (ch, ci).

    Returns None if the original cannot be decoded (caller falls back to 0.0).
    Decoded tuple layout: (x, y, z, w, pX, pY, pZ, sX, sY, sZ)
      ci 0-2 -> quat XYZ (index ci)
      ci 3-8 -> pos/scale (index ci+1, because reconstructed W sits at 3)
    """
    try:
        dec = _orig_template_cache.get(template_header)
        if dec is None:
            dec = decode_anim_game_formula(template_header)
            _orig_template_cache[template_header] = dec
        idx = ci if ci < 3 else ci + 1
        orig_frames = dec["frames"]
        if not orig_frames or ch >= len(orig_frames[0]):
            return None
        vals = []
        for f in range(frame_count):
            of = orig_frames[f] if f < len(orig_frames) else orig_frames[-1]
            vals.append(of[ch][idx])
        return vals
    except Exception:
        return None


def _encode_with_template_structure(delta_xyz, channel_count, frame_count, scales,
                                     template_header, orig_structure):
    """Encode using the ORIGINAL animation's slot structure.
    
    Preserves: mode bytes, FULL/SKIP classification, base count, count widths,
    bits_per_frame, and all QBS header offsets from the template.
    Only re-encodes: bitstream residuals from the new input data.
    
    This prevents the game from reading wrong offsets when the input animation
    has different value distributions than the original (e.g. Blender 15-frame
    walk vs original 36-frame walk).
    """
    slot_states, slot_order, orig_modes, orig_bases, orig_counts = orig_structure
    mode_flag = template_header[QUAT_HEADER_OFFSET + 0x26]
    hv = QUAT_HEADER_OFFSET

    # Collect per-slot VALUES first (independent of scales).
    # FULL slots whose component the input doesn't provide are restored from
    # the original template (Blender exports quat XYZ only).
    slot_vals = {}
    for (ch, ci) in slot_order:
        state = slot_states[(ch, ci)]
        if state != "FULL":
            continue
        vals = []
        frames_data = delta_xyz.get(ch, [])
        ncomps = 0
        for fr in frames_data:
            if len(fr) > ncomps:
                ncomps = len(fr)
        provides = ci < ncomps
        if provides:
            for f in range(frame_count):
                if f < len(frames_data) and ci < len(frames_data[f]):
                    vals.append(frames_data[f][ci])
                elif frames_data and ci < len(frames_data[-1]):
                    vals.append(frames_data[-1][ci])  # hold last frame
                else:
                    vals.append(0.0)
        else:
            orig_vals = _orig_component_values(template_header, ch, ci, frame_count)
            if orig_vals is not None:
                vals = orig_vals
            else:
                vals = [0.0] * frame_count
        slot_vals[(ch, ci)] = vals

    # HARD bit budget. The original bitstream fills its region
    # EXACTLY (bitstream_base + bpf*frame_count == 0xFC0), and the header
    # references 0xFC0 (pose/event trailer). Growing bpf overruns the trailer
    # -> frames at the end decode garbage -> crash/hang. Therefore bpf is
    # FIXED at the template's value and the total residual bits per frame
    # MUST fit in bpf_orig*8. When Blender's quat spans need more bits than
    # the original, the quat SCALE is widened (K = 2, 4, ...): raws shrink by
    # K, quantization error stays <= K*scale/2 (invisible at these scales),
    # and the decoded values are otherwise preserved. SKIP bases on quat
    # components are divided by K to stay consistent with the new scale.
    bpf_orig = struct.unpack_from(">H", template_header, hv + 0x1C)[0]
    budget_bits = bpf_orig * 8
    scale_k = 1

    def _compute_raws_widths(k):
        eff = list(scales)
        if k != 1:
            for ci in range(3):
                eff[ci] = scales[ci] * k
        widths = {}
        raws_map = {}
        for (ch, ci) in slot_order:
            if slot_states[(ch, ci)] != "FULL":
                continue
            raws = [int(round(v / eff[ci])) for v in slot_vals[(ch, ci)]]
            raws_map[(ch, ci)] = raws
            span = max(raws) - min(raws)
            w = 1
            while (1 << w) <= span:
                w += 1
            widths[(ch, ci)] = min(w, 32)
        total = sum(widths.values())
        return widths, raws_map, eff, total

    slot_widths, slot_raws, eff_scales, total_bits = _compute_raws_widths(scale_k)
    while total_bits > budget_bits and scale_k < 64:
        scale_k *= 2
        slot_widths, slot_raws, eff_scales, total_bits = _compute_raws_widths(scale_k)

    if total_bits > budget_bits:
        # Last resort: shrink the widest slots down to the budget and clamp
        # residuals at write time (never exceeds budget).
        cap = budget_bits
        for key in sorted(slot_widths, key=lambda kk: -slot_widths[kk]):
            if total_bits <= cap:
                break
            overflow = total_bits - cap
            room = slot_widths[key] - 1
            if room > 0:
                take = min(room, overflow)
                slot_widths[key] -= take
                total_bits -= take
        print(f"  [encode] WARNING: budget clamp active (k={scale_k}, "
              f"total={total_bits}/{budget_bits})")

    if scale_k != 1:
        print(f"  [encode] quat scale widened x{scale_k} to fit bit budget "
              f"({total_bits}/{budget_bits} bits)")

    # Build bitstream using original's slot order and FULL/SKIP classification.
    # bpf is ALWAYS the template's (fixed stride) — see budget note above.
    bits_per_frame = budget_bits
    bw = BitWriter()
    for f in range(frame_count):
        frame_start = f * bits_per_frame
        bw.pad_to_bits(frame_start)
        for (ch, ci) in slot_order:
            if slot_states[(ch, ci)] != "FULL":
                continue
            width = slot_widths[(ch, ci)]
            raws = slot_raws[(ch, ci)]
            raw = raws[f] if f < len(raws) else (raws[-1] if raws else 0)
            # Use base = min(raws) so residual >= 0
            base = min(raws)
            residual = raw - base
            limit = (1 << width) - 1
            if residual < 0:
                residual = 0
            elif residual > limit:
                residual = limit  # clamp (only when budget cap kicked in)
            bw.write(residual, width)
    bitstream = bw.to_bytes() + b"\x00" * 16

    # Compute base values: base = min(raw) for FULL, original for SKIP
    bases = []
    counts = []
    for (ch, ci) in slot_order:
        state = slot_states[(ch, ci)]
        if state == "FULL":
            raws = slot_raws[(ch, ci)]
            bases.append(min(raws))
            counts.append(slot_widths[(ch, ci)] - 1)
        else:
            # SKIP: keep original's base (scaled down if quat scale widened)
            idx = len(bases)
            ob = orig_bases[idx] if idx < len(orig_bases) else 0
            if scale_k != 1 and ci < 3:
                ob = int(round(ob / scale_k))
            bases.append(ob)

    # Trailing pad byte
    counts.append(0)

    n_slots = len(slot_order)
    sizeA = n_slots * 4
    sizeB = len(counts)

    # Copy template header (0x00-0x7B) including all metadata offsets
    hv = QUAT_HEADER_OFFSET
    total_size = hv + 0x28 + sizeA + sizeB + len(bitstream)
    TRAILER_OFFSET = 0xFC0
    if len(template_header) > TRAILER_OFFSET:
        total_size = max(total_size, TRAILER_OFFSET + len(template_header[TRAILER_OFFSET:]))

    data = bytearray(total_size)
    data[0:0x7C] = template_header[0:0x7C]

    # Copy the ENTIRE QBS header region from template (hv to hv+0x28)
    # This preserves ALL offsets and metadata exactly
    qbs_end = hv + 0x28
    data[hv:qbs_end] = template_header[hv:qbs_end]

    # Only update fields that MUST change
    # frame_count in the animation header (offset 0x22, NOT QBS)
    struct.pack_into(">H", data, 0x22, frame_count)
    # NOTE: bpf is NEVER grown: the bitstream region ends at
    # the trailer (0xFC0) by construction; growth overruns pose/event data.

    # Read back the offsets we need
    scale_off = struct.unpack_from(">I", data, hv + 0x04)[0]
    default_off = struct.unpack_from(">I", data, hv + 0x08)[0]
    mode_off = struct.unpack_from(">I", data, hv + 0x0C)[0]
    base_off = struct.unpack_from(">I", data, hv + 0x10)[0]
    bitcount_off = struct.unpack_from(">I", data, hv + 0x14)[0]

    # Copy defaults from template; write scales (quat scales may be widened)
    if scale_k != 1:
        for i in range(9):
            struct.pack_into(">f", data, scale_off + i * 4, eff_scales[i])
    else:
        data[scale_off:scale_off + 36] = template_header[scale_off:scale_off + 36]
    data[default_off:default_off + 36] = template_header[default_off:default_off + 36]

    # Write mode bytes from ORIGINAL (preserved structure)
    data[mode_off:mode_off + len(orig_modes)] = orig_modes

    # Write bases
    base_size = sizeA // 4
    for i in range(base_size):
        val = bases[i] if i < len(bases) else 0
        if mode_flag == 0:
            struct.pack_into(">h", data, base_off + i * 2, max(-32768, min(32767, val)))
        else:
            struct.pack_into(">i", data, base_off + i * 4, val)

    # Write counts
    for i, c in enumerate(counts):
        data[bitcount_off + i] = c & 0xFF

    # Write bitstream
    bitstream_base = bitcount_off + sizeB
    data[bitstream_base:bitstream_base + len(bitstream)] = bitstream

    # Pad to 0xFC0 and append template trailer
    if len(template_header) > TRAILER_OFFSET:
        if len(data) < TRAILER_OFFSET:
            data = data + b"\x00" * (TRAILER_OFFSET - len(data))
        data = data[:TRAILER_OFFSET] + template_header[TRAILER_OFFSET:]

    return bytes(data)


def decode_anim_game_formula(data):
    """Decode using the GAME's exact formula: per-frame reset, (raw+base)*scale."""
    def be16(o):
        return struct.unpack_from(">H", data, o)[0]

    def be32(o):
        return struct.unpack_from(">I", data, o)[0]

    hv = QUAT_HEADER_OFFSET
    scale_off = be32(hv + 0x04)
    default_off = be32(hv + 0x08)
    mode_off = be32(hv + 0x0C)
    base_off = be32(hv + 0x10)
    bitcount_off = be32(hv + 0x14)
    sizeA = be16(hv + 0x18)
    sizeB = be16(hv + 0x1A)
    bits_per_frame_bytes = be16(hv + 0x1C)
    comps = be16(hv + 0x1E)
    nch = be16(hv + 0x22)
    nfr = be16(hv + 0x24)
    mode_flag = data[hv + 0x26]

    bitstream_base = base_off + sizeA + sizeB
    scales = [struct.unpack_from(">f", data, scale_off + i * 4)[0] for i in range(comps)]
    defaults = [struct.unpack_from(">f", data, default_off + i * 4)[0] for i in range(comps)]

    def read_base(idx):
        if mode_flag == 0:
            off = base_off + idx * 2
            return struct.unpack_from(">h", data, off)[0] if off + 2 <= len(data) else 0
        off = base_off + idx * 4
        return struct.unpack_from(">i", data, off)[0] if off + 4 <= len(data) else 0

    def read_count(idx):
        if mode_flag == 0:
            off = bitcount_off + (idx >> 1)
            if off >= len(data):
                return 0
            shift = (idx & 1) << 2
            return (data[off] >> shift) & 0xF
        off = bitcount_off + idx
        return data[off] if off < len(data) else 0

    frames = []
    for frame in range(nfr):
        packed = bits_per_frame_bytes * frame
        reader = _BitReader(data, bitstream_base, packed & ~3, (packed & 3) << 3)
        base_idx = 0
        count_idx = 0
        mode_idx = 0
        frame_out = []
        for ch in range(nch):
            o = list(defaults)
            mb = data[mode_off + mode_idx]
            mode_idx += 1
            gs = mb & 3

            def do_group(mbx, coff):
                nonlocal base_idx, count_idx
                for i in range(3):
                    mask = 0x80 >> i
                    if mbx & mask:
                        if (mbx & (mask >> 3)) == 0:
                            b = read_base(base_idx)
                            base_idx += 1
                            c = read_count(count_idx)
                            count_idx += 1
                            nb = c + 1
                            raw = reader.read(nb) if nb < 33 else 0
                            o[coff + i] = (raw + b) * scales[coff + i]
                        else:
                            base_idx += 1

            do_group(mb, 0)
            if gs == 1:
                mb2 = data[mode_off + mode_idx]; mode_idx += 1
                do_group(mb2, 6)
            elif gs == 2:
                mb2 = data[mode_off + mode_idx]; mode_idx += 1
                do_group(mb2, 3)
            elif gs == 3:
                mb2 = data[mode_off + mode_idx]; mode_idx += 1
                do_group(mb2, 3)
                mb3 = data[mode_off + mode_idx]; mode_idx += 1
                do_group(mb3, 6)

            # Reconstruct W
            x, y, z = o[0], o[1], o[2]
            w = math.sqrt(max(0.0, 1.0 - x * x - y * y - z * z))
            frame_out.append((x, y, z, w, o[3], o[4], o[5], o[6], o[7], o[8]))
        frames.append(frame_out)
    return {
        "channel_count": nch,
        "frame_count": nfr,
        "scales": scales,
        "defaults": defaults,
        "frames": frames,
    }


# ---------------------------------------------------------------------------
# GLB loading helper
# ---------------------------------------------------------------------------
def load_glb_animation(glb_path, anim_index=5):
    """Load bind rest rotations + per-frame absolute rotations from a GLB."""
    gd = open(glb_path, "rb").read()
    c0 = struct.unpack_from("<I", gd, 12)[0]
    gltf = json.loads(gd[20 : 20 + c0].decode("utf-8"))
    bindata = gd[20 + c0 + 8 :]
    nodes = gltf["nodes"]
    bind = {i: tuple(n.get("rotation", [0, 0, 0, 1])) for i, n in enumerate(nodes)}
    anim = gltf["animations"][anim_index]
    rots = {}
    for ch in anim["channels"]:
        if ch["target"]["path"] != "rotation":
            continue
        ni = ch["target"]["node"]
        s = anim["samplers"][ch["sampler"]]
        a = gltf["accessors"][s["output"]]
        bv = gltf["bufferViews"][a["bufferView"]]
        off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        count = a["count"]
        rots[ni] = [struct.unpack_from("<ffff", bindata, off + f * 16) for f in range(count)]
    frame_count = max(len(v) for v in rots.values()) if rots else 0
    return bind, rots, frame_count


import json


# ---------------------------------------------------------------------------
# Roundtrip test
# ---------------------------------------------------------------------------
def run_roundtrip_test():
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(
        tools_dir,
        "aid_anim_pinata_actor_animal_crocodile_eat_data.bin",
    )
    glb_path = os.path.join(
        os.path.dirname(tools_dir), "notes", "EXPORT", "MeExtractModels",
        "export", "aid_model_pinata_actor_animal_crocodile_default.glb",
    )

    print("=" * 72)
    print("QUAT_BITSTREAM32 ENCODER v4 — ROUNDTRIP TEST")
    print("=" * 72)

    # --- Test A: re-encode the REAL game data's own decoded deltas ---
    print("\n[A] Real-data roundtrip (decode crocodile_eat -> re-encode -> decode)")
    real = open(data_path, "rb").read()
    dec = decode_anim_game_formula(real)
    nch, nfr = dec["channel_count"], dec["frame_count"]
    print(f"  Decoded real: {nch} ch x {nfr} frames (game formula, per-frame reset)")

    # Build delta_xyz from decoded frames (quat XYZ only)
    delta_xyz = {}
    for ch in range(nch):
        frames = []
        for f in range(nfr):
            x, y, z = dec["frames"][f][ch][0], dec["frames"][f][ch][1], dec["frames"][f][ch][2]
            frames.append((x, y, z))
        delta_xyz[ch] = frames

    encoded = encode_deltas(delta_xyz, nch, nfr)
    print(f"  Re-encoded: {len(encoded)} bytes (original {len(real)} bytes)")

    dec2 = decode_anim_game_formula(encoded)
    max_err = 0.0
    worst = None
    for f in range(nfr):
        for ch in range(nch):
            for ci in range(3):
                a = dec["frames"][f][ch][ci]
                b = dec2["frames"][f][ch][ci]
                err = abs(a - b)
                if err > max_err:
                    max_err = err
                    worst = (f, ch, ci, a, b)
    print(f"  Max component error: {max_err:.9f}")
    if worst:
        print(f"    worst: frame {worst[0]} ch {worst[1]} comp {worst[2]}: "
              f"{worst[3]:.6f} -> {worst[4]:.6f}")
    status_a = "PASS" if max_err < 1e-4 else "FAIL"
    print(f"  RESULT [{status_a}]: encode(decode(real)) ~= decode(real)")

    # --- Test B: GLB -> deltas -> encode -> decode -> compose vs GLB ---
    print("\n[B] GLB ground-truth roundtrip (bind x decoded_delta vs GLB)")
    if not os.path.exists(glb_path):
        print("  GLB not found, skipping Test B")
        return status_a

    bind, rots, glb_frames = load_glb_animation(glb_path, anim_index=5)
    frame_count = min(nfr, glb_frames)
    print(f"  GLB anim 5: {len(rots)} channels, {glb_frames} frames; using {frame_count}")

    # delta = conj(bind) * abs, negate to ensure W >= 0 for XYZ storage
    glb_delta = {}
    for ch, abs_list in rots.items():
        b = bind.get(ch, (0, 0, 0, 1))
        inv_b = quat_conj(quat_normalize(b))
        frames = []
        for f in range(frame_count):
            a = quat_normalize(abs_list[f])
            d = quat_mult(inv_b, a)
            if d[3] < 0:  # keep W >= 0 so stored XYZ match decoder's reconstruction
                d = (-d[0], -d[1], -d[2], -d[3])
            frames.append((d[0], d[1], d[2]))
        glb_delta[ch] = frames

    enc_glb = encode_deltas(glb_delta, nch, frame_count)
    dec_glb = decode_anim_game_formula(enc_glb)

    matches = 0
    total = 0
    max_err_b = 0.0
    for ch, abs_list in rots.items():
        if ch >= nch:
            continue
        b = bind.get(ch, (0, 0, 0, 1))
        for f in range(frame_count):
            dx, dy, dz = (
                dec_glb["frames"][f][ch][0],
                dec_glb["frames"][f][ch][1],
                dec_glb["frames"][f][ch][2],
            )
            dw = math.sqrt(max(0.0, 1.0 - dx * dx - dy * dy - dz * dz))
            composed = quat_mult(quat_normalize(b), (dx, dy, dz, dw))
            glb_q = abs_list[f]
            err = min(
                max(abs(composed[i] - glb_q[i]) for i in range(4)),
                max(abs(composed[i] + glb_q[i]) for i in range(4)),
            )
            total += 1
            if err < 0.01:
                matches += 1
            max_err_b = max(max_err_b, err)
    pct = 100.0 * matches / total if total else 0
    print(f"  Max error: {max_err_b:.6f}")
    print(f"  Channels x frames with err < 0.01: {matches}/{total} ({pct:.1f}%)")
    status_b = "PASS" if pct > 95.0 else "FAIL"
    print(f"  RESULT [{status_b}]: bind x decode(encode(GLB deltas)) ~= GLB")

    # --- Test C: verify against the REAL file's own composition ---
    print("\n[C] Structural checks on encoded file")
    enc = enc_glb
    def be16(o, buf=enc): return struct.unpack_from(">H", buf, o)[0]
    def be32(o, buf=enc): return struct.unpack_from(">I", buf, o)[0]
    hv = 0x7C
    print(f"  mode_flag={enc[hv+0x26]} (expect 1)")
    print(f"  channel_count={be16(hv+0x22)} frame_count={be16(hv+0x24)}")
    print(f"  bits_per_frame_bytes={be16(hv+0x1C)}")
    print(f"  sizeA={be16(hv+0x18)} sizeB={be16(hv+0x1A)}")
    bstream_base = be32(hv + 0x10) + be16(hv + 0x18) + be16(hv + 0x1A)
    print(f"  bitstream_base={bstream_base} file_size={len(enc)}")
    # Verify stride alignment
    bpf_bits = be16(hv + 0x1C) * 8
    print(f"  stride bits={bpf_bits} (must be multiple of 32: {bpf_bits % 32 == 0})")

    print("\n" + "=" * 72)
    print(f"OVERALL: Test A [{status_a}]  Test B [{status_b}]")
    print("=" * 72)
    return status_a if status_b == "SKIP" else (status_a if status_a == "FAIL" else status_b)


if __name__ == "__main__":
    run_roundtrip_test()
