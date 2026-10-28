# Pipeline

Extract → decode → modify → encode → inject → play.

---

## 1. Extract an animation

```bash
python tools/extract_anim.py \
  --pkg /path/to/1150.pkg --entry 10 --anim walk --out walk_orig.bin
```

- `--pkg` — path to a `.pkg` file from your game install
- `--entry` — index in the PKG entry table (crocodile animations = 10)
- `--anim` — animation name substring (`walk`, `fight_attack`, `run`, `eat`, etc.)
- `--out` — output file path for the raw animation blob

To find which PKG contains a given animation:
```bash
python tools/extract_anim.py --search <name_substring>
```

---

## 2. Decode to inspect

```python
import sys; sys.path.insert(0, 'tools')
from encode_quat_v4 import decode_anim_game_formula

d = decode_anim_game_formula(open('walk_orig.bin', 'rb').read())
print(d['channel_count'], 'channels x', d['frame_count'], 'frames')

# d['frames'][frame][channel] = (qx, qy, qz, qw, px, py, pz, sx, sy, sz)
# Quaternion W is reconstructed (not stored): W = sqrt(max(0, 1 - X² - Y² - Z²))
```

---

## 3. Modify the motion

### Option A: Blender

Install `tools/vp_anim_exporter.py` as a Blender add-on. Export the active action to JSON. The injector handles bind-pose delta computation and frame-count resampling automatically.

### Option B: Programmatic

```python
import math
from encode_quat_v4 import decode_anim_game_formula

orig = decode_anim_game_formula(open('walk_orig.bin', 'rb').read())
nch, nfr = orig['channel_count'], orig['frame_count']

# Example: spin channel 0 through 360° over the clip
deltas = {}
for ch in range(nch):
    deltas[ch] = []
    for f in range(nfr):
        if ch == 0:
            ang = 2 * math.pi * f / nfr
            deltas[ch].append((0.0, 0.0, math.sin(ang / 2)))  # Z-axis delta quat
        else:
            q = orig['frames'][f][ch]
            deltas[ch].append((q[0], q[1], q[2]))  # keep original
```

---

## 4. Encode + inject

```bash
python tools/inject_animation.py my_animation.json walk \
  --pkg /path/to/1150.pkg.backup \
  --entry 10 \
  --output modified.pkg
```

This:
1. Loads the Blender JSON or programmatic deltas
2. Extracts the original animation from the PKG
3. Encodes custom motion using the original's structure (mode bytes, bit widths, bpf preserved)
4. Rebuilds CAFF + PKG with correct checksums and alignment
5. Verifies the result decodes with 0 invalid quaternions

**Always inject into a copy of the original PKG, never the live file directly.**

For same-size injection of a fight_attack blob (label/size match the target clip's file-info entry):

```bash
python tools/inject_walk_blob.py --blob my.bin \
  --label aid_anim_pinata_actor_animal_crocodile_fight_attack \
  --size 19800 --pkg /path/to/vanilla.pkg --output /path/to/out.pkg
```

---

## 5. Deploy

```bash
# Deploy
cp modified.pkg /path/to/game/packages/1150.pkg

# Revert
cp /path/to/game/packages/1150.pkg.backup /path/to/game/packages/1150.pkg
```

Launch the game, load your garden, observe.

---

## Offline verification (run before every game test)

```python
import sys, math; sys.path.insert(0, 'tools')
from encode_quat_v4 import decode_anim_game_formula

orig = decode_anim_game_formula(open('walk_orig.bin', 'rb').read())
new  = decode_anim_game_formula(open('walk_walk_encoded.bin', 'rb').read())

nch, nfr = orig['channel_count'], orig['frame_count']
bad = sum(1 for f in range(nfr) for ch in range(nch)
          if abs(math.sqrt(sum(new['frames'][f][ch][i]**2 for i in range(4))) - 1.0) > 0.05)
pos = max(abs(orig['frames'][f][ch][i] - new['frames'][f][ch][i])
          for f in range(nfr) for ch in range(nch) for i in range(4, 10))

print('invalid quats:', bad, '  max pos/scale diff:', pos)
# Want: bad == 0, pos == 0.0
```

---

## Constraints

These will crash the game if violated. All are enforced by the encoder where possible.

| Constraint | Detail |
|---|---|
| Never grow `bpf` | The bitstream region ends at a fixed trailer offset. Growing it overruns the trailer → last frames decode as garbage → hang. |
| Never use zlib level 9 | The game's decompressor cannot handle it. Levels 6 and 7 are proven. |
| Preserve mode bytes / structure | Reclassifying slots shifts base/bitcount offsets → garbage pointers → crash. |
| Bit budget | Residual bits per frame must be ≤ `bpf × 8`. The encoder widens quat scales to fit. |
| Only FULL slots carry custom motion | SKIP/ABSENT slots are constant by design. See below. |
| CAFF section-table compressed size must not change | Changing it shifts the data start offset → hang. Pad with zeros instead. |
| PKG entry size and subsequent offsets | Must be updated when the CAFF grows. The injector handles this. |
| CAFF blobs are 2048-aligned | Terminated with `0xFF` + zero pad. The injector handles this. |
| Count table trailing pad byte (`sizeB = nFULL + 1`) | Never evict the trailing `00` pad. A SKIP→FULL count INSERT that shifts the pad crashed in-game (AV on a raw pointer). |
| SKIP slots decode to `base × scale`, not default | ABSENT = default. Treating SKIP as ABSENT produces wrong values. |
| ABSENT→FULL forbidden | Reclassifying ABSENT as FULL shifts the base and count tables → crash. |
| All components' per-frame bits ≤ `bpf × 8` | walk 765/768; fight_attack bpf 94 = 752 cap (death-roll measured 751). |
| Small CAFF growth: `inject_walk_blob.py --allow-grow` | In-slot growth proven in-game (+19 B case: only entry size changes; FI/reloc byte-identical). |
| Frame growth | Needs full container sync (blob ptrs, FI size/starts, file-relative reloc targets); loader hang on FI/reloc edits is open — see [`GUARDRAILS.md`](GUARDRAILS.md). |

---

## FULL slots (the per-animation bit budget)

Each animation stores only the components the original animators keyed. Those are **FULL** slots. Everything else is **SKIP** (frozen) or **ABSENT** (default).

| Animation | FULL slots (quat) | Per-frame budget | Result with large motion |
|---|---|---|---|
| `walk` | 59 of 81 | 765 of 768 bits used | Clean. Motion fits. |
| `fight_attack` | 20 of 81 | 751 of 752 bits used (bpf 94) | Large motion needed bit-count widening to fit; proven in-game. |

**Symptoms of slot crush:** character grows/shrinks, stretches, spins on the wrong axis, quaternions with magnitude > 1.0.

**Mitigations:**
1. Author milder motion for low-slot clips.
2. Pick a target clip with more FULL slots (`walk` / `run` / `eat` are generous).
3. Expand the structure — widen bit counts at frozen `bpf` (proven path; see `tools/c1_widen.py` and [`RESULTS.md`](RESULTS.md)); multi-slot widening was used for the death-roll (ch0/c1 12→14 bits). Frame-count growth is blocked — see [`GUARDRAILS.md`](GUARDRAILS.md).

---

## Proven result

Both wins are verified end-to-end in-game:
- **Minimum win** — unmodified walk re-encoded with one bit-count widened: count 10→11 (bits 11→12); blob differs from vanilla by exactly 1 byte; bit-identical playback confirmed in-game and by human review.
- **Target win** — fight_attack 360° death-roll authored on the root channel; 0 invalid quaternions; non-authored channels bit-exact; played in-game end-to-end with correct roll axis.

See [`RESULTS.md`](RESULTS.md) for details and reproduction steps, [`GUARDRAILS.md`](GUARDRAILS.md) for hard rules, and `tools/test_hang.py` for the automated deploy→navigate→garden→15s-watch→revert protocol.

See `examples/synthesize_spin.py` for a pure-code example.
