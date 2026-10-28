# QUAT_BITSTREAM32 Format

The animation format used by *Viva Piñata: Trouble in Paradise*. All multi-byte values are **big-endian** (Xbox 360 / PowerPC byte order).

---

## Top-level layout

```
[animation asset header]
[format descriptor block]     ← contains "QUAT_BITSTREAM32" string
[QUAT_BITSTREAM32 header]     ← fixed-size header with offsets
[scale array]                 ← float32 × 9 components
[default values array]        ← float32 × 9 components
[mode bytes]                  ← 1+ bytes per channel
[base values]                 ← signed int16 BE
[bit count table]             ← nibble-packed 4-bit values
[bitstream]                   ← variable-length residual bits
[trailer]                     ← pose block, event tables, BITSTREAM32 markers
```

---

## Animation asset header

```
Offset  Size  Field
0x00    10    "animation\0"
0x0A    14    version string (e.g. "19.12.06.0036\0")
0x1C    4     float32 BE — duration in seconds
0x20    2     uint16 BE — channel count
0x22    2     uint16 BE — frame count
0x24    4     uint32 BE — offset to QUAT_BITSTREAM32 descriptor
```

---

## QUAT_BITSTREAM32 header

Located at the offset specified in the asset header (e.g. `0x60`).

```
Offset  Size  Field
+0x00   14    "QUAT_BITSTREAM32\0"
+0x10   4     uint32 BE — scale array offset
+0x14   4     uint32 BE — default values offset
+0x18   4     uint32 BE — mode bytes offset
+0x1C   4     uint32 BE — base values offset
+0x20   4     uint32 BE — bit count offset
+0x24   2     uint16 BE — sizeA (base table size in bytes)
+0x26   2     uint16 BE — sizeB (bit count table size in bytes)
+0x28   2     uint16 BE — bits_per_frame (bpf) in BYTES
+0x2A   2     uint16 BE — components per channel (always 9)
+0x2C   2     uint16 BE — unknown (typically 12)
+0x2E   2     uint16 BE — channel count
+0x30   2     uint16 BE — frame count
+0x32   1     uint8    — has_scale flag
```

**Bitstream start** = `base_values_offset + sizeA + sizeB`

**Trailer start** = `bitstream_start + bpf × frame_count`

---

## Decode formula

For each component that is present and not skipped:

```
raw = extract_bits(bitstream, bit_position, bit_count + 1)
value = (raw + base_value) * scale[component]
```

A SKIP component stores base × scale (no bits are read). An ABSENT component stores the default value. (SKIP semantics confirmed against the game's decoder, SDK v0.10.0.)

- `base_value` is a signed 16-bit big-endian integer.
- `bit_count` is a 4-bit nibble from the bit count table.
- `scale` is a float32 from the scale array (indexed by component 0–8).
- Quaternion W is reconstructed: `W = sqrt(max(0, 1 - X² - Y² - Z²))`

---

## Mode bytes

Each channel has 1+ mode bytes. The first mode byte controls components 0–2 (quat XYZ). Bits 1–0 select additional component groups:

| Bits 1–0 | Additional groups | Components covered |
|---|---|---|
| 0 | none | 0–2 only |
| 1 | +6 | 0–2, 6–8 (quat + scale) |
| 2 | +3 | 0–2, 3–5 (quat + position) |
| 3 | +3 and +6 | 0–2, 3–5, 6–8 (all) |

Each additional group has its own mode byte.

### Mode byte bit layout (per group of 3 components)

```
Bit 7 (0x80): component 0 present
Bit 6 (0x40): component 1 present
Bit 5 (0x20): component 2 present
Bit 4 (0x10): component 0 skip
Bit 3 (0x08): component 1 skip
Bit 2 (0x04): component 2 skip
Bits 1–0:     group selection (first mode byte only)
```

**Per component:**
- Present bit CLEAR → absent (use default, consume nothing)
- Present bit SET + Skip bit CLEAR → decode (read base + bit count + bitstream bits)
- Present bit SET + Skip bit SET → skip (consume base value only, advance base index, no bitstream bits; stores base × scale)

---

## Bit count table

Nibble-packed 4-bit values. Two components per byte.

```
nibble_offset = running counter (resets each frame)
byte_at = bit_count_offset + nibble_offset // 2

if nibble_offset is even:
    bit_count = byte_at & 0x0F
else:
    bit_count = (byte_at >> 4) & 0x0F
```

The actual number of bits read from the bitstream is `bit_count + 1`.

Count table: one count per FULL slot plus one trailing `00` pad byte (`sizeB = nFULL + 1`). The pad is structural — it must not be removed (evicting it crashes the game) — see `GUARDRAILS.md`.

---

## Bitstream reader

State: `byte_offset` (4-byte aligned), `bit_offset` (0–31 within current word).

**Per frame:**
```
packed = bitstream_start_bits + bpf × frame_index   # bpf in BYTES
byte_offset = packed & ~3
bit_offset  = (packed & 3) << 3
```

**Read operation:**
```
word1 = read_uint32_be(data, byte_offset)
word2 = read_uint32_be(data, byte_offset + 4)

combined = (word2 << (32 - bit_offset)) | (word1 >> bit_offset)
mask = (1 << (bit_count + 1)) - 1   # or 0xFFFFFFFF if bit_count == 31
raw = combined & mask

# Advance
new_pos = bit_offset + (bit_count + 1)
bit_offset = new_pos % 32
byte_offset += (new_pos // 32) * 4
```

---

## Output layout (per channel per frame)

40 bytes: 10 × float32.

| Index | Field | Default if absent |
|---|---|---|
| 0 | Quaternion X | 0.0 |
| 1 | Quaternion Y | 0.0 |
| 2 | Quaternion Z | 0.0 |
| 3 | Quaternion W (reconstructed) | 1.0 |
| 4 | Position X | 0.0 |
| 5 | Position Y | 0.0 |
| 6 | Position Z | 0.0 |
| 7 | Scale X | 1.0 |
| 8 | Scale Y | 1.0 |
| 9 | Scale Z | 1.0 |

---

## Base values

Always 16-bit signed big-endian, regardless of `has_scale` flag.

```
offset = base_values_offset + base_index × 2
base_value = sign_extend_16(read_uint16_be(data, offset))
base_index += 1
```

SKIP components advance `base_index` but do not consume bit count or bitstream bits.

---

## Trailer

The trailer begins at `bitstream_start + bpf × frame_count`. It contains:

- Pose block (rig-constant data)
- Event tables (animation-specific)
- `BITSTREAM32` marker strings

**The trailer offset is referenced by fields in the animation header.** Growing `bpf` pushes the bitstream past the trailer start, corrupting the last frames. This is why the encoder never grows `bpf` — it widens quat scales instead.

---

## Mode byte consumption

The mode byte index resets to 0 at the start of each frame. It advances by 1 for each mode byte consumed across all channels within that frame.

For 27 channels with mixed group selections, this typically produces 36 mode bytes per frame.

---

## Reference implementation

The working decoder and encoder are in `tools/encode_quat_v4.py`:

- `decode_anim_game_formula(data)` → dict with `frames`, `channel_count`, `frame_count`, `scales`, `defaults`
- `encode_deltas(delta_xyz, channel_count, frame_count, scales, template_header)` → encoded blob

Both have been verified against in-game behavior.
