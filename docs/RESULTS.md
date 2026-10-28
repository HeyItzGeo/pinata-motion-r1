# Results

Everything below was verified in-game on ReTiP 1.13. Both mission win conditions are complete. Commands are runnable from the repo root; you supply your own game files.

---

## Prerequisites

- Extract the originals first (see PIPELINE §1), place them as tools/orig_walk_anim.bin and tools/orig_fight_attack.bin, or pass --input.
- A vanilla backup of the target PKG from your game install (`<vanilla.pkg>` below). See `PIPELINE.md` §5 for deploy/revert.
- Python 3 with the repo's `tools/` on the path (commands run from the repo root).
- For in-game checks: a garden save that triggers the target animation, plus `tools/test_hang.py` (brightness probe) for automated load verification.

Always inject into a copy. Revert by copying the backup over the live PKG.

---

## 1. What was proved

### 1.1 Minimum win: bit-widening at frozen bpf

The game accepts a wider bit-count on an existing FULL slot with zero structural change.

The walk test build differed from vanilla by **exactly one byte** (count table entry `10 → 11`). Decode was bit-identical (max quat/pos/scale diff 0.0). In-game: strong PASS (`garden visible`) with a clean 15-second post-load watch, and human confirmation that the walk played identically.

```bash
python tools/c1_widen.py
python tools/inject_walk_blob.py --blob <widen output> --pkg <vanilla.pkg> --output <out.pkg>
python tools/test_hang.py modified --pkg <out.pkg>
```

`c1_widen.py` widens the last FULL slot, so nothing after it shifts.

### 1.2 Target win: authored 360° death-roll in fight_attack

A full 360° death-roll was authored on channel 0 (the BASE root bone) over the clip's 167 frames and played end-to-end in-game with no crash, no distortion, and a correct roll axis (user-verified).

Offline gates on the death-roll build:

| Gate | Result |
|---|---|
| Invalid quaternions | 0 |
| Spin at midpoint / end | 179° / 358° |
| Axis alignment \|dot\| | 1.0000 |
| Other 26 channels | bit-exact vs original |

```bash
python tools/fight_attack_spin.py
python tools/inject_walk_blob.py --blob <spin output> \
  --label aid_anim_pinata_actor_animal_crocodile_fight_attack \
  --size 19800 --pkg <vanilla.pkg> --output <out.pkg>
python tools/test_hang.py modified --pkg <out.pkg>
```

`fight_attack_spin.py` axis-composes the spin onto the decoded original and applies the fight_attack template repairs.

### 1.3 Same-length re-authorization (the general capability)

Decode any animation → edit deltas → structure-preserving re-encode → same-size inject.

Proven on both animation shapes: walk (27ch × 36fr) and fight_attack (27ch × 167fr). The game does not require original motion — only original structure.

---

## 2. The axis table (ch0 / BASE root, from the model bind pose)

The BASE bone's bind rotation is 150.28° about +X, so a rotation composed in delta space maps to a fixed world axis. Compose pre-multiplied:

```text
delta_new(t) = R_axis(θ(t)) · delta_orig(t)
```

Post-multiplying makes the axis wobble with existing motion. Force w ≥ 0 after composition. Constant θ across the clip = constant world axis; θ(t) = 2π·t/frames gives a full loop-safe revolution.

| Encoder axis | World motion |
|---|---|
| **X** | Forward somersault (axis-exact through the hips) |
| **Y** | Top-spin / pirouette (tilted ~28.7° toward the nose) |
| **Z** | **Death-roll** — the signature crocodile move (tilted ~28.7° toward the tail) |

---

## 3. Capacity (measured)

| Animation | bpf | Bits/frame budget | Used | Headroom |
|---|---|---|---|---|
| `walk` | 96 | 768 | 765 | 3 bits/frame |
| `fight_attack` | 94 | 752 | 751 (death-roll build) | ~1 bit/frame at that motion |

Expansion within budget = widening existing FULL counts (proven, see 1.1). Walk also has exactly ONE SKIP quat slot. fight_attack has 20 FULL slots vs walk's 59 — but SKIP→FULL inserts are blocked by the pad-byte rule (see GUARDRAILS).

---

## 4. What was refuted (do not retry)

| Approach | Outcome |
|---|---|
| ABSENT→FULL conversion | Shifts base/count table offsets → access violation (`guest AV 0x5C00001F`), twice. |
| Growing bpf without full container sync | The original loader crash (`guest AV 0x11D4`). With complete container sync the crash itself is solved (zero exceptions), but an unidentified loader hang on edited file-info/reloc tables remains — **frame growth stays blocked** (see GUARDRAILS). |
| Structure-inserting SKIP→FULL without pad accounting | In-game access violation at runtime value `0x1087`. |
| Trusting a passing load as data validation | One build with 104 invalid quaternions loaded in-game without crashing. A passing load does not validate the data. |

---

## 5. Proven in-game (one line each)

- Widened-bit walk (1-byte diff) — identical playback, machine + human verified.
- Death-roll fight_attack — full custom clip, machine + human verified.
- Same-size inject through the full chain (blob → CAFF → PKG → game) with container bytes outside the animation region untouched.
- Small CAFF growth within an entry slot (`--allow-grow`, +19 B) — loaded and played.

---

## 6. Open problems (known-unknowns, scoped)

1. **Longer animations** (more frames): blocked by the file-info/reloc loader hang above; the pointer-relocation half is solved.
2. The 648-byte trailing table in the reloc section was never identified (unused in practice).
3. Alternate decoder dispatch paths exist (mode-byte routing) but are never exercised by these animations.

---

## See also

- `docs/PIPELINE.md` — extract → decode → modify → encode → inject → play.
- `docs/FORMAT.md` — QUAT_BITSTREAM32 on-disk layout.
- `examples/synthesize_spin.py` — pure-code spin example.
