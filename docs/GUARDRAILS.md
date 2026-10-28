# Guardrails

Hard-won rules. Read before modifying any tool or data file.

Every rule below cost a crash, a hang, or corrupted data.
"Why" is included so you can reason about edge cases
instead of memorizing.

---

## The unforgivable edits

### Never modify `tools/encode_quat_v4.py`

It is the verified reference implementation (validated
against the game binary, PDB, and real data). All other
tools are built AROUND it. Extend via new files that
import it.

### zlib level 6 or 7 only — never 9

Level-9 streams hang the game's decompressor (observed:
dirty-disc-style boot hang). Level 6 is vanilla's own
level; 7 for tighter fits.

### CAFF section-table and reloc-table compressed sizes in the header NEVER change

The loader computes data offsets from them; a changed
value shifts every section read → hang. Re-compress,
then pad with zeros to the original span — trailing
bytes after a zlib stream are ignored.

### The count table's trailing pad byte must survive

Size structure: `sizeB = nFULL + 1` (real counts + one
`00` pad). Evicting the pad (adding a FULL slot inside a
full table) made the game walk past the table → in-game
access violation reading `0x1087` (= a header pointer
minus 1, computed at runtime). This is why naive
SKIP→FULL crashes.

### ABSENT→FULL is forbidden

New base/count entries shift every table offset after
them → the game reads garbage pointers → access
violation (`guest AV 0x5C00001F`). Expansion must not
change table sizes: widen existing FULL counts (see
`c1_widen.py`), which only changes values in place.

### Per-frame bits ≤ bpf×8

Measured budgets: walk **765/768** (3 bits free);
fight_attack bpf=94 → **752 bits/frame cap**, death-roll
build used 751. Exceeding corrupts the trailer
boundary.

### Same-size is the safe zone; growth is surgery

Same-length inject (19800→19800, 6944→6944) is proven
end-to-end. Small CAFF growth: `inject_walk_blob.py
--allow-grow` (grows within the entry slot's padding;
proven in-game, +19 B case changed only the entry's size
field). Blob growth (bpf/frames): needs full container
sync — blob pointers, file-info size and all following
starts, and file-relative reloc targets. The
pointer-relocation crash was solved this way, but
**a loader hang on edited file-info/reloc tables remains
an OPEN ISSUE** — frame growth (longer animations) is
therefore blocked until it is understood.

### SKIP slots decode to `base × scale`, NOT the default

Proven from the game's own decoder (SDK v0.10.0 codegen,
`sub_8292D9C8` skip branch: reads base, multiplies by
scale, stores, consumes no bits). Defaults belong to
ABSENT components only. An encoder that treats SKIP as
0.0 decodes wrong on both sides of a comparison — pin
the base when converting a slot.

### Never deploy without offline verification

One build containing 104 invalid quaternions was loaded
in-game; it did not crash, but that is not a reliable
outcome. Required gates before any
deploy: `invalid quats == 0`, intended motion tracks its
target, all other channels match the original decode,
structure fields unchanged, blob size unchanged.

---

## Harness and process rules

- **Test with `tools/test_hang.py`**
  (`python tools/test_hang.py modified --pkg <pkg>`):
  deploys from your file, drives menu→garden, detects
  the garden, watches 15 s for crash/hang, prints a
  final `RESULT:` line, and reverts.
  Interpretation: `PASS (garden visible)` = strong pass;
  `FAIL — CRASH`/`HANG`/`TIMEOUT` = failure (one retry
  only for TIMEOUT).

- **Human eyes are the final arbiter** for any claim
  about how motion LOOKS (correctness, distortion, axis).
  Harness pixel/color signals have known false negatives;
  a harness FAIL with a human observing a correct garden
  is a sensor failure, not a data failure — and vice
  versa: never claim playback correctness from a
  load-only PASS.

- **Always revert and verify**: after any test, live
  `1150.pkg` must hash to your vanilla backup's hash;
  game processes killed; no crash dialogs left. Keep a
  pristine backup copy and never deploy directly from it.

- **Guard the environment, not just the PKG**: an
  external tool once replaced `retip.exe` and added a
  GPU-plugin config line; three test runs "failed" at
  boot before touching any of our data. Before a test
  session: confirm `retip.exe` hash against your recorded
  stock value and that no GPU-plugin line was prepended
  to `retip.toml`.

- **Deploy from copies, never in place**: build to a
  scratch file, copy over the live PKG, test, restore
  from backup.

---

## Encoder path repairs (fight_attack specifics)

`tools/fight_attack_spin.py` carries two fixes for
`encode_deltas(..., template_header=...)` quirks that
only bite non-walk animations — if you write a new
fight_attack encoder, copy them:

1. The template path hardcodes the walk's trailer offset
   (0xFC0); fight_attack's trailer sits at 0x3F62. Fix:
   rebuild the bitstream from the count/base tables at
   the animation's own bitstream_base (the C1 rebuild
   pattern).
2. Count-pad off-by-one: the encoder emits one fewer pad
   byte than the original `sizeB`; preserve the original's
   pad bytes or the bitstream lands one byte early.

---

## What is intentionally NOT here

Game assets, extracted models, animation blobs, and
decompiled code are never committed (`.gitignore`
enforces it). This repo ships tools and documentation
only.
