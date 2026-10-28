# pinata-motion-r1

Animation extraction, modification, and injection for *Viva Piñata: Trouble in Paradise* (Xbox 360 / recompiled PC).

This tool reads the game's `QUAT_BITSTREAM32` animation format, lets you replace motion with custom animation data (from Blender or synthesized in code), and writes a modified game package that loads cleanly.

**No game assets are included.** You provide the files by extracting them from your own legally owned copy of the game.

---

## Status

| Capability | State |
|---|---|
| Extract animation from PKG | Working |
| Decode QUAT_BITSTREAM32 → floats | Working |
| Encode floats → bitstream (structure-preserving) | Working |
| Same-length re-authorization (decode → edit → re-encode → same-size inject) | Proven in-game (both wins) |
| Same-length motion replacement | Proven (walk + fight_attack) |
| Bit-count widening (in-budget expansion) | Proven in-game |
| Longer animations (frame growth) | Known-blocked (see `docs/GUARDRAILS.md`) |
| Blender export → inject | Working (walk path proven) |

---

## Results

Both win conditions are achieved: a **minimum win** (unmodified walk re-encoded with one bit-count widened: count 10→11 (bits 11→12) — blob differs from vanilla by exactly 1 byte, bit-identical playback confirmed in-game and by human review) and a **target win** (fight_attack 360° death-roll authored on the root channel, 0 invalid quaternions, non-authored channels bit-exact, played in-game end-to-end with correct roll axis).

See [`docs/RESULTS.md`](docs/RESULTS.md) for win details and reproduction steps, and [`docs/GUARDRAILS.md`](docs/GUARDRAILS.md) for the hard rules — read it before modifying anything.

---

## Requirements

- Python 3.10+
- A legally owned copy of *Viva Piñata: Trouble in Paradise* (Xbox 360 disc or recompiled PC build)
- Windows with Python 3.10+ and frida (pip install frida) for tools/test_hang.py — the game harness drives real windows and XInput input.
- Optional: Blender with the exporter add-on (see `docs/PIPELINE.md`)

---

## Quickstart

```bash
# 1. Extract an animation from your game package
python tools/extract_anim.py \
  --pkg /path/to/1150.pkg --entry 10 --anim walk --out walk_orig.bin

# 2. Decode it to inspect the motion
python -c "
import sys; sys.path.insert(0, 'tools')
from encode_quat_v4 import decode_anim_game_formula
d = decode_anim_game_formula(open('walk_orig.bin','rb').read())
print(d['channel_count'], 'channels x', d['frame_count'], 'frames')
"

# 3. Inject a modified animation (Blender JSON or programmatic deltas)
python tools/inject_animation.py my_animation.json walk \
  --pkg /path/to/1150.pkg.backup --entry 10 --output modified.pkg

# 4. Deploy and test
#    Copy modified.pkg over your game's 1150.pkg, launch, observe.
#    Or run the automated protocol (deploy → navigate → garden detection →
#    15s watch → revert):
python tools/test_hang.py modified --pkg <your.pkg>
```

Full walkthrough: [`docs/PIPELINE.md`](docs/PIPELINE.md)

---

## How it works

The game stores skeletal animation in a proprietary compressed format (`QUAT_BITSTREAM32`) inside `CAFF` containers packed into `.pkg` archives. Each animation stores per-bone quaternion deltas, positions, and scales using a bitstream with per-component bit widths.

This tool:

1. Parses the `CAFF` container and `PKG` archive structure.
2. Decodes the bitstream into float quaternions/positions/scales.
3. Computes deltas from the model's bind pose.
4. Re-encodes custom motion using the original animation's structure (mode bytes, bit widths, frame stride) so the game's decoder accepts it without modification.
5. Rebuilds the `CAFF` and `PKG` with correct checksums and alignment.

Deltas compose against the model's bind pose and can be axis-composed for spins (e.g. death-roll on a chosen world axis via pre-multiplication).

See [`docs/FORMAT.md`](docs/FORMAT.md) for the binary format specification.

---

## Limitations

The game's animations vary in how many components they store. Each clip has a fixed per-frame bit budget (`bpf × 8`): walk has generous headroom (765 of 768 bits used), while fight_attack is tight (751 of 752 bits used with the death-roll). That budget is the real constraint — but it is expandable: bit-count widening at frozen `bpf` is proven in-game and was used to fit both wins without growing the bitstream.

Longer animations (more frames) are **not** yet possible: growing the blob requires a full container sync (blob pointers, file-info sizes/starts, file-relative reloc targets), and a loader hang on those edits remains an open known-unknown.

See [`docs/GUARDRAILS.md`](docs/GUARDRAILS.md) for the hard rules and [`docs/PIPELINE.md`](docs/PIPELINE.md) § Constraints for details.

---

## Credits

This project builds on reverse-engineering work by others. See [`CREDITS.md`](CREDITS.md).

## Legal

See [`LEGAL.md`](LEGAL.md).

## License

Apache-2.0. See [`LICENSE`](LICENSE).
