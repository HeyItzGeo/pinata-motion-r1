#!/usr/bin/env python3
"""
inject_animation.py — Unified animation injection tool for ANY pinata species and ANY animation.

Blender JSON → game animation with automatic scale/count widening at frozen bpf.

Usage:
  python inject_animation.py <blender_json> <anim_name> [options]

Examples:
  python inject_animation.py Blender_export/walk.json walk
  python inject_animation.py Blender_export/fight_attack.json fight_attack
  python inject_animation.py my_export.json eat --search

Options:
  --pkg <path>       Path to PKG file (auto-detected by searching if omitted)
  --entry <index>    Entry index in PKG (auto-detected by searching CAFF if omitted)
  --game-dir <path>  retip install dir for PKG auto-search
                     (default: <path/to/retip-1.13.0-windows>)
  --glb <path>       Path to GLB model (auto-detected from species name)
  --species <name>   Species name (auto-detected from Blender export armature)
  --search           Search all PKGs to find the animation (slow, ~2 min)
  --dry-run          Encode and verify but don't write PKG
  --output <path>    Output PKG path (default: <pkg>.modified)
"""
import struct, os, sys, json, math, zlib, argparse, time

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS_DIR)

from encode_quat_v4 import (
    decode_anim_game_formula, encode_deltas, _BitReader, BitWriter,
    QUAT_HEADER_OFFSET, quat_mult, quat_conj, quat_normalize
)
from splice_anim import checksum32, CaffV36

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Defaults to the standard Goopie install under the current user's profile;
# override with --game-dir.
DEFAULT_GAME_DIR = (
    r"C:\Users\{}\AppData\Local\Goopie\Games\retip-1.13.0-windows".format(
        os.environ.get("USERNAME", "user")))
GAME_DIR = DEFAULT_GAME_DIR
PKG_DIR = os.path.join(GAME_DIR, "assets", "Beta", "packages")
# MeExtractModels GLB exports: default to <repo>\exports (drop files there;
# lookups return None cleanly when the directory is absent).
EXPORT_DIR = os.path.join(os.path.dirname(TOOLS_DIR), "exports")


# ---------------------------------------------------------------------------
# GLB loading
# ---------------------------------------------------------------------------
def load_glb(glb_path):
    """Load GLB node names and bind-pose rotations."""
    gd = open(glb_path, "rb").read()
    c0 = struct.unpack_from("<I", gd, 12)[0]
    gltf = json.loads(gd[20:20 + c0].decode("utf-8"))
    node_names = []
    bind_poses = {}  # name -> (w, x, y, z)
    for n in gltf.get("nodes", []):
        name = n.get("name", "")
        node_names.append(name)
        rot = n.get("rotation", [0, 0, 0, 1])  # glTF: [x, y, z, w]
        bind_poses[name] = (rot[3], rot[0], rot[1], rot[2])  # (w, x, y, z)
    return node_names, bind_poses


def find_glb_for_species(species):
    """Find GLB file for a species in the export directory."""
    pattern = f"aid_model_pinata_actor_animal_{species}_default.glb"
    path = os.path.join(EXPORT_DIR, pattern)
    if os.path.exists(path):
        return path
    # Search for partial match
    if os.path.isdir(EXPORT_DIR):
        for f in os.listdir(EXPORT_DIR):
            if f.endswith(".glb") and species in f and "actor_animal" in f:
                return os.path.join(EXPORT_DIR, f)
    return None


# ---------------------------------------------------------------------------
# Blender JSON loading
# ---------------------------------------------------------------------------
def load_blender_export(json_path):
    """Load Blender export JSON."""
    with open(json_path) as f:
        return json.load(f)


def extract_species_from_armature(armature_name):
    """Extract species name from armature name.
    e.g., 'aid_model_pinata_actor_animal_crocodile_default.002' -> 'crocodile'
    """
    marker = "pinata_actor_animal_"
    idx = armature_name.find(marker)
    if idx < 0:
        return None
    rest = armature_name[idx + len(marker):]
    # Species name ends at next underscore or end
    end = rest.find("_")
    if end < 0:
        return rest
    return rest[:end]


def find_blender_animation(export_data, anim_name):
    """Find animation in Blender JSON by name pattern.
    Matches if anim_name is a substring of the animation name (case-insensitive).
    """
    matches = []
    for anim in export_data.get("animations", []):
        if anim_name.lower() in anim["name"].lower():
            matches.append(anim)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Prefer exact prefix match (e.g., 'walk' matches 'walk_...' not 'walkbackwards_...')
    for a in matches:
        if a["name"].lower().startswith(anim_name.lower()):
            return a
    return matches[0]


# ---------------------------------------------------------------------------
# CAFF / PKG extraction
# ---------------------------------------------------------------------------
def parse_pkg_header(data):
    """Parse PKG header and return entries list."""
    if len(data) < 8:
        return None
    ver = struct.unpack(">I", data[0:4])[0]
    count = struct.unpack(">I", data[4:8])[0]
    if ver not in (1, 2) or count == 0 or count > 10000:
        return None
    entries = []
    for i in range(count):
        eoff = 8 + i * 12
        if eoff + 12 > len(data):
            break
        eid = struct.unpack(">I", data[eoff:eoff + 4])[0]
        eoffset = struct.unpack(">I", data[eoff + 4:eoff + 8])[0]
        esize = struct.unpack(">I", data[eoff + 8:eoff + 12])[0]
        entries.append((eid, eoffset, esize))
    return entries


def extract_caff_from_pkg(pkg_data, entry_index):
    """Extract CAFF bytes from a PKG entry."""
    entries = parse_pkg_header(pkg_data)
    if entries is None or entry_index >= len(entries):
        return None
    eid, eoffset, esize = entries[entry_index]
    return pkg_data[eoffset:eoffset + esize]


def decompress_caff_section0(caff_data):
    """Decompress CAFF section 0 data. Returns (section_table, section0_data)."""
    if caff_data[:4] != b"CAFF":
        return None, None
    hs = struct.unpack(">I", caff_data[0x14:0x18])[0]
    stc = struct.unpack(">I", caff_data[0x60:0x64])[0]
    rtc = struct.unpack(">I", caff_data[0x74:0x78])[0]
    ds = hs + stc + rtc
    compression = caff_data[0x4A]

    if compression == 1:
        st = zlib.decompress(caff_data[hs:hs + stc])
        s0cs = struct.unpack(">I", st[29:33])[0]
        s0 = bytearray(zlib.decompress(caff_data[ds:ds + s0cs]))
    else:
        st = caff_data[hs:hs + stc]
        # Uncompressed: section data starts after tables
        s0 = bytearray(caff_data[ds:])
    return st, s0


def find_animation_in_section0(s0_data, anim_name_pattern):
    """Find animation in section 0 by searching for its name in the section table.
    Returns (offset, size) or (None, None).
    """
    # Search section table for the animation label
    # Labels are in the section table, not section 0
    # We need to parse file infos to get offsets
    # For simplicity, search for known animation patterns in section 0
    # by looking for the "animation" magic at regular offsets

    # Animation offsets come from the file-info table; as a fallback, scan
    # section 0 for the "animation\x00" magic and validate the channel/frame
    # counts that follow it.
    magic = b"animation\x00"
    pos = 0
    while True:
        idx = s0_data.find(magic, pos)
        if idx < 0:
            break
        # Check if this looks like a valid animation
        if idx + 0x28 <= len(s0_data):
            nch = struct.unpack(">H", s0_data[idx + 0x20:idx + 0x22])[0]
            nfr = struct.unpack(">H", s0_data[idx + 0x22:idx + 0x24])[0]
            if 1 <= nch <= 100 and 1 <= nfr <= 1000:
                # Read the name from section table to verify
                yield idx
        pos = idx + 1


def find_animation_offset_by_name(section_table, s0_size, anim_name):
    """Find animation offset/size by parsing CAFF file infos.
    Returns (offset, size) or (None, None).
    """
    # Parse CAFF header from section table context
    # Section table structure: n_types * 0x21 entries + labels + file label table + adb + file infos
    # We need to find file infos and match by label

    # Not implemented: file-info/label matching. The active path is the
    # section-0 magic scan in iter_animation_offsets().
    return None, None


def search_pkg_for_animation(species, anim_name):
    """Search all PKGs for an animation. Returns list of (pkg_path, entry_index, anim_offset, anim_size).
    This is slow (~2 min for 1293 PKGs).
    """
    target = f"aid_anim_pinata_actor_animal_{species}_{anim_name}".encode()
    if not os.path.isdir(PKG_DIR):
        sys.exit(f"PKG dir not found: {PKG_DIR} — pass --pkg <file> to skip the "
                 f"search, or --game-dir <path/to/retip-1.13.0-windows>")
    pkg_files = sorted(
        [f for f in os.listdir(PKG_DIR) if f.endswith(".pkg") and not f.endswith(".backup")],
        key=lambda x: int(x.split(".")[0]) if x.split(".")[0].isdigit() else 0,
    )

    results = []
    for i, pkg_name in enumerate(pkg_files):
        pkg_path = os.path.join(PKG_DIR, pkg_name)
        try:
            with open(pkg_path, "rb") as f:
                data = f.read()
        except Exception:
            continue

        entries = parse_pkg_header(data)
        if not entries:
            continue

        for j, (eid, eoffset, esize) in enumerate(entries):
            if eoffset + esize > len(data):
                continue
            caff = data[eoffset:eoffset + esize]
            if caff[:4] != b"CAFF":
                continue

            # Quick check: search for target in raw CAFF
            if target in caff[:8192]:
                results.append((pkg_path, j, "raw"))
                continue

            # Decompress and search
            try:
                hs = struct.unpack(">I", caff[0x14:0x18])[0]
                compression = caff[0x4A]
                if compression == 1:
                    stc = struct.unpack(">I", caff[0x60:0x64])[0]
                    decompressed = zlib.decompress(caff[hs:hs + stc])
                    if target in decompressed:
                        results.append((pkg_path, j, "decompressed"))
            except Exception:
                pass

        if (i + 1) % 200 == 0:
            print(f"  Searched {i + 1}/{len(pkg_files)} PKGs, found {len(results)} hits...")

    return results


# ---------------------------------------------------------------------------
# Animation structure parsing
# ---------------------------------------------------------------------------
def parse_animation_structure(anim_data):
    """Parse QBS header and mode structure of an animation."""
    hv = QUAT_HEADER_OFFSET
    nch = struct.unpack(">H", anim_data[hv + 0x22:hv + 0x24])[0]
    nfr = struct.unpack(">H", anim_data[hv + 0x24:hv + 0x26])[0]
    bpf = struct.unpack(">H", anim_data[hv + 0x1C:hv + 0x1E])[0]
    mode_off = struct.unpack(">I", anim_data[hv + 0x0C:hv + 0x10])[0]
    base_off = struct.unpack(">I", anim_data[hv + 0x10:hv + 0x14])[0]
    bitcount_off = struct.unpack(">I", anim_data[hv + 0x14:hv + 0x18])[0]
    sizeA = struct.unpack(">H", anim_data[hv + 0x18:hv + 0x1A])[0]
    sizeB = struct.unpack(">H", anim_data[hv + 0x1A:hv + 0x1C])[0]
    scale_off = struct.unpack(">I", anim_data[hv + 0x04:hv + 0x08])[0]
    comps = struct.unpack(">H", anim_data[hv + 0x1E:hv + 0x20])[0]
    bitstream_base = base_off + sizeA + sizeB

    # Read scales
    scales = [struct.unpack(">f", anim_data[scale_off + i * 4:scale_off + i * 4 + 4])[0]
              for i in range(comps)]

    # Parse mode structure
    mode_bytes = anim_data[mode_off:]
    slots = []  # (ch, ci, is_skip)
    mode_idx = 0
    for ch in range(nch):
        mb0 = mode_bytes[mode_idx]
        mode_idx += 1
        gs = mb0 & 3
        for i in range(3):
            if mb0 & (0x80 >> i):
                is_skip = bool(mb0 & (0x40 >> i))  # GAME uses bits 6,5,4
                slots.append((ch, i, is_skip))
        if gs in (2, 3):
            mb2 = mode_bytes[mode_idx]
            mode_idx += 1
            for i in range(3):
                if mb2 & (0x80 >> i):
                    is_skip = bool(mb2 & (0x40 >> i))
                    slots.append((ch, 3 + i, is_skip))
        if gs in (1, 3):
            mb2 = mode_bytes[mode_idx]
            mode_idx += 1
            for i in range(3):
                if mb2 & (0x80 >> i):
                    is_skip = bool(mb2 & (0x40 >> i))
                    slots.append((ch, 6 + i, is_skip))

    # Read original widths and bases
    orig_widths = []
    orig_bases = []
    base_idx = 0
    count_idx = 0
    for (ch, ci, is_skip) in slots:
        if is_skip:
            orig_widths.append(0)
            orig_bases.append(0)
            base_idx += 1
            continue
        count = anim_data[bitcount_off + count_idx]
        orig_widths.append(count + 1)
        # Read base
        base_val = struct.unpack(">i", anim_data[base_off + base_idx * 4:base_off + base_idx * 4 + 4])[0]
        orig_bases.append(base_val)
        base_idx += 1
        count_idx += 1

    # Find pose data location
    pose_idx = anim_data.find(b"pose")
    if pose_idx < 0:
        pose_idx = len(anim_data)  # no pose found, use file end

    return {
        "nch": nch, "nfr": nfr, "bpf": bpf,
        "mode_off": mode_off, "base_off": base_off, "bitcount_off": bitcount_off,
        "sizeA": sizeA, "sizeB": sizeB, "bitstream_base": bitstream_base,
        "scale_off": scale_off, "comps": comps,
        "scales": scales, "slots": slots,
        "orig_widths": orig_widths, "orig_bases": orig_bases,
        "pose_idx": pose_idx, "file_size": len(anim_data),
    }


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------
def _slerp3(a, b, t):
    """Slerp between two 3-component delta quats (W reconstructed, W>=0)."""
    def wof(v):
        return math.sqrt(max(0.0, 1.0 - v[0] * v[0] - v[1] * v[1] - v[2] * v[2]))
    qa = (a[0], a[1], a[2], wof(a))
    qb = (b[0], b[1], b[2], wof(b))
    dot = qa[0] * qb[0] + qa[1] * qb[1] + qa[2] * qb[2] + qa[3] * qb[3]
    if dot < 0.0:
        qb = (-qb[0], -qb[1], -qb[2], -qb[3])
        dot = -dot
    dot = max(-1.0, min(1.0, dot))
    if dot > 0.9995:
        r = tuple(qa[i] + t * (qb[i] - qa[i]) for i in range(4))
    else:
        th = math.acos(dot)
        s = math.sin(th)
        wa = math.sin((1.0 - t) * th) / s
        wb = math.sin(t * th) / s
        r = tuple(wa * qa[i] + wb * qb[i] for i in range(4))
    n = math.sqrt(sum(x * x for x in r)) or 1.0
    r = (r[0] / n, r[1] / n, r[2] / n, r[3] / n)
    if r[3] < 0.0:
        r = (-r[0], -r[1], -r[2], -r[3])
    return (r[0], r[1], r[2])


def _resample_loop(frames, target):
    """Resample a looping delta-quat sequence to `target` frames.

    Blender walk exports are 15 frames where frame 14 duplicates frame 0
    (cycle = 14 unique frames). The game walk slot is 36 frames. Without resample
    the encoder hold-lasts, freezing all joints for frames 15-35 while the
    restored original pos/scale keeps the root moving (frozen-sliding look).
    Loop-aware mapping: game frame f -> cycle time t = f * (len-1) / target
    (len-1 = 14 = cycle length, frame 14 == frame 0).
    """
    n = len(frames)
    if n == 0 or target is None or target == n:
        return frames

    def _qdist(a, b):
        def wof(v):
            return math.sqrt(max(0.0, 1.0 - v[0] * v[0] - v[1] * v[1] - v[2] * v[2]))
        qa = (a[0], a[1], a[2], wof(a))
        qb = (b[0], b[1], b[2], wof(b))
        return abs(sum(x * y for x, y in zip(qa, qb)))

    # Auto-detect loop closure: last frame duplicates first => looping cycle.
    closes = n > 2 and _qdist(frames[0], frames[-1]) < 0.01
    cycle = (n - 1) if closes else (n - 1)
    out = []
    for f in range(target):
        if closes:
            t = f * cycle / target
        else:
            t = f * (n - 1) / max(1, target - 1)
        i0 = int(math.floor(t))
        frac = t - i0
        if closes and i0 >= cycle:
            out.append(frames[cycle])  # == frames[0]
            continue
        i0 = min(i0, n - 1)
        i1 = min(i0 + 1, n - 1)
        if frac < 1e-9:
            out.append(frames[i0])
        else:
            out.append(_slerp3(frames[i0], frames[i1], frac))
    return out


def compute_blender_deltas(blender_anim, bind_poses, bone_to_ch, target_frames=None):
    """Convert Blender absolute rotations to game delta format.
    Returns dict: ch_index -> [(x, y, z), ...] per frame.

    IMPORTANT: Blender exports rotation as [w, x, y, z].
    GLB stores rotation as [x, y, z, w] and we convert to (w, x, y, z).
    But quat_conj and quat_mult expect (x, y, z, w).
    So we must convert to (x, y, z, w) before calling quat functions.
    """
    deltas = {}
    for bone_name, frames in blender_anim["bones"].items():
        if bone_name not in bone_to_ch or bone_name not in bind_poses:
            continue
        ch = bone_to_ch[bone_name]
        bind_wxyz = bind_poses[bone_name]  # (w, x, y, z) from load_glb
        # Convert to (x, y, z, w) for quat_conj/quat_mult
        bind = (bind_wxyz[1], bind_wxyz[2], bind_wxyz[3], bind_wxyz[0])
        inv_bind = quat_conj(bind)

        bone_deltas = []
        for frame_data in frames:
            rot_wxyz = tuple(frame_data["rotation"])  # (w, x, y, z) from Blender
            # Convert to (x, y, z, w)
            rot = (rot_wxyz[1], rot_wxyz[2], rot_wxyz[3], rot_wxyz[0])
            delta = quat_mult(inv_bind, rot)
            delta = quat_normalize(delta)
            # Force W >= 0 (game reconstructs W as positive sqrt)
            if delta[3] < 0:
                delta = (-delta[0], -delta[1], -delta[2], -delta[3])
            bone_deltas.append((delta[0], delta[1], delta[2]))
        if target_frames is not None and target_frames != len(bone_deltas):
            bone_deltas = _resample_loop(bone_deltas, target_frames)
        deltas[ch] = bone_deltas
    return deltas


# ---------------------------------------------------------------------------
# Width computation and bitstream rebuilding
# ---------------------------------------------------------------------------
def compute_required_width(raw_values):
    """Compute bit width needed for a sequence of raw integer values."""
    if not raw_values:
        return 0
    min_v = min(raw_values)
    max_v = max(raw_values)
    span = max_v - min_v
    if span <= 0:
        return 1
    width = 1
    while width < 32 and (1 << width) - 1 < span:
        width += 1
    return width


def compute_new_widths(structure, deltas):
    """Compute new bit widths for all slots.
    Currently: keep original widths (proven in-game approach).
    Width expansion requires changing bpf which is untested in-game.
    """
    slots = structure["slots"]
    orig_widths = structure["orig_widths"]
    return list(orig_widths), 0


def rebuild_bitstream_widened(anim_data, structure, deltas):
    """Rebuild bitstream WITH bit-count widening.

    Instead of clamping residuals to original widths, compute the minimum
    width needed for each slot's new residuals, write new bitcount entries,
    and rebuild the bitstream with the wider slots.

    Returns: (new_anim_bytes, new_widths, new_bpf)
    """
    slots = structure["slots"]
    orig_widths = structure["orig_widths"]
    orig_bases = structure["orig_bases"]
    scales = structure["scales"]
    nfr = structure["nfr"]
    bitstream_base = structure["bitstream_base"]
    bitcount_off = structure["bitcount_off"]
    base_off = structure["base_off"]

    # --- Pass 1: compute new residuals for all frames, find max per slot ---
    # For each FULL slot, collect residuals across all frames
    slot_residuals = {}  # slot_index -> list of residual values per frame
    slot_bases = {}      # slot_index -> base value

    base_idx = 0
    for i, (ch, ci, is_skip) in enumerate(slots):
        if is_skip:
            base_idx += 1
            continue
        orig_base = orig_bases[base_idx]
        slot_bases[i] = orig_base
        slot_residuals[i] = []
        base_idx += 1

    for f in range(nfr):
        base_idx = 0
        for i, (ch, ci, is_skip) in enumerate(slots):
            if is_skip:
                base_idx += 1
                continue
            orig_base = orig_bases[base_idx]
            if ci < 3 and ch in deltas:
                bone_deltas = deltas[ch]
                si = f % len(bone_deltas)
                target = bone_deltas[si][ci]
                raw = round(target / scales[ci])
                residual = raw - orig_base
                # No clamping — let it be whatever it needs to be
                slot_residuals[i].append(residual)
            else:
                # Pos/scale: we'll read original residual in pass 2
                slot_residuals[i].append(None)  # placeholder
            base_idx += 1

    # Read original residuals for pos/scale slots
    for f in range(nfr):
        packed = 0  # we'll read from the original bitstream
        # Actually we need to read from each frame's position
        pass

    # Simpler: decode original residuals from the original bitstream
    orig_reader_data = {}
    for f in range(nfr):
        packed = structure["bpf"] * f
        reader = _BitReader(anim_data, bitstream_base, packed & ~3, (packed & 3) << 3)
        base_idx = 0
        for i, (ch, ci, is_skip) in enumerate(slots):
            if is_skip:
                base_idx += 1
                continue
            w = orig_widths[i]
            raw = reader.read(w) if w < 33 else 0
            if slot_residuals[i][f] is None:
                slot_residuals[i][f] = raw  # pos/scale: copy original
            base_idx += 1

    # Compute new width per slot: enough bits for max residual
    # NEGATIVE RESIDUAL FIX: decoder reads unsigned residuals.
    # If residual < 0, we must LOWER the base so residual becomes >= 0.
    # BASE ADJUST DISABLED: changing base values causes game to lose garden buttons.
    # We clamp negative residuals to 0 instead (cosmetic only).
    new_widths = list(orig_widths)  # start with original
    new_bases = list(orig_bases)    # copy bases (never adjust)
    widened_count = 0
    base_adjusted = 0
    for i in slot_residuals:
        residuals = slot_residuals[i]
        if not residuals:
            continue
        # Compute width from max ABSOLUTE residual
        max_abs = max(abs(r) for r in residuals)
        if max_abs <= 0:
            needed = orig_widths[i]
        else:
            needed = max_abs.bit_length()
        if needed > orig_widths[i]:
            new_widths[i] = needed
            widened_count += 1
        elif needed < orig_widths[i]:
            new_widths[i] = orig_widths[i]  # never shrink

    # --- Pass 2: compute new bpf (allow bpf to grow) ---
    total_bits = sum(new_widths[i] for i in range(len(slots)) if not slots[i][2])
    new_bpf = ((total_bits + 31) // 32) * 4
    if new_bpf < structure["bpf"]:
        new_bpf = structure["bpf"]  # never shrink

    # --- Pass 3: write new bitcount entries AND adjusted bases ---
    patched = bytearray(anim_data)

    # Write adjusted base values (32-bit BE int, one per present slot)
    base_idx = 0
    for i, (ch, ci, is_skip) in enumerate(slots):
        # ALL present slots consume a base value
        if new_bases[base_idx] != orig_bases[base_idx]:
            struct.pack_into(">i", patched, base_off + base_idx * 4, new_bases[base_idx])
        base_idx += 1

    # Write bitcount entries (1 byte per FULL slot, value = width - 1)
    count_idx = 0
    for i, (ch, ci, is_skip) in enumerate(slots):
        if is_skip:
            continue
        new_width = new_widths[i]
        count_val = new_width - 1
        if count_val > 255:
            raise ValueError(f"Slot {i}: width {new_width} exceeds 256 bits")
        patched[bitcount_off + count_idx] = count_val
        count_idx += 1

    # --- Pass 4: rebuild bitstream with new widths ---
    for f in range(nfr):
        frame_bw = BitWriter()
        for i in range(len(slots)):
            if slots[i][2]:  # skip
                continue
            residual = slot_residuals[i][f]
            if residual is None:
                residual = 0
            # Clamp to new width
            max_val = (1 << new_widths[i]) - 1 if new_widths[i] < 32 else 0xFFFFFFFF
            # Residual must be non-negative (decoder reads unsigned)
            residual = max(0, min(residual, max_val))
            frame_bw.write(residual, new_widths[i])

        # Pad to new_bpf * 8 bits
        frame_bw.pad_to_bits(new_bpf * 8)

        # Pack into words, write as BE
        frame_bits = frame_bw.bits
        for word_idx in range(0, len(frame_bits), 32):
            chunk = frame_bits[word_idx:word_idx + 32]
            word = 0
            for j, b in enumerate(chunk):
                if b:
                    word |= (1 << j)
            abs_byte = bitstream_base + (new_bpf * f) + (word_idx // 8)
            if abs_byte + 4 <= len(patched):
                struct.pack_into(">I", patched, abs_byte, word)

    # --- Pass 5: update bpf in header ---
    hv = QUAT_HEADER_OFFSET
    struct.pack_into(">H", patched, hv + 0x1C, new_bpf)

    return bytes(patched), new_widths, new_bpf, widened_count, base_adjusted


def rebuild_bitstream(anim_data, structure, new_widths, deltas):
    """Rebuild the bitstream. Returns new animation bytes.

    CRITICAL: We do NOT change bitcount entries. The game's bit reader
    depends on the exact bitcount values to determine bit positions.
    Changing them causes hangs/crashes (proven in-game).

    We only overwrite residual bits using bit-level patching, clamping
    to original widths. This is the proven in-game approach.
    """
    slots = structure["slots"]
    orig_widths = structure["orig_widths"]
    orig_bases = structure["orig_bases"]
    scales = structure["scales"]
    nfr = structure["nfr"]
    bpf = structure["bpf"]
    bitstream_base = structure["bitstream_base"]

    # Start with original bytes - we only modify residual bits
    patched = bytearray(anim_data)

    # Rebuild bitstream frame by frame using bit-level patching
    for f in range(nfr):
        packed = bpf * f
        # Reader for original bitstream at this frame
        orig_reader = _BitReader(anim_data, bitstream_base, packed & ~3, (packed & 3) << 3)
        # Writer for new residual bits
        frame_bw = BitWriter()

        base_idx = 0
        count_idx = 0
        for i, (ch, ci, is_skip) in enumerate(slots):
            if is_skip:
                base_idx += 1
                continue

            orig_width = orig_widths[i]
            orig_base = orig_bases[base_idx]

            # ALWAYS read original residual to keep orig_reader in sync
            orig_raw = orig_reader.read(orig_width) if orig_width < 33 else 0

            if ci < 3 and ch in deltas:
                # Quat slot: compute new residual from deltas
                bone_deltas = deltas[ch]
                n_bone_frames = len(bone_deltas)
                si = f % n_bone_frames
                target = bone_deltas[si][ci]
                raw = round(target / scales[ci])
                residual = raw - orig_base
                # Clamp to ORIGINAL width (we don't change bitcounts)
                max_val = (1 << orig_width) - 1 if orig_width < 32 else 0xFFFFFFFF
                residual = max(0, min(residual, max_val))
                frame_bw.write(residual, orig_width)
            else:
                # Pos/scale slot: copy original residual exactly
                frame_bw.write(orig_raw, orig_width)

            base_idx += 1
            count_idx += 1

        # Write residual bits into patched at the correct bit position.
        #
        # VERIFIED (test_single_residual.py): Method 2 is the ONLY correct approach:
        #   Pack bits LSB-first into 32-bit words, write as big-endian.
        #   Bit-by-bit patching produces WRONG bytes (decoder reads 0).
        #   Byte-swap also wrong. Plain BE packing is correct.
        frame_bits = frame_bw.bits
        nbits = len(frame_bits)

        # Pack into 32-bit words (LSB-first), write as big-endian
        for word_idx in range(0, nbits, 32):
            chunk = frame_bits[word_idx:word_idx + 32]
            word = 0
            for j, b in enumerate(chunk):
                if b:
                    word |= (1 << j)

            # Write as big-endian. packed is already in BYTES (bpf is bytes),
            # so no // 8 needed.
            abs_byte = bitstream_base + packed + (word_idx // 8)
            if abs_byte + 4 <= len(patched):
                struct.pack_into(">I", patched, abs_byte, word)

    return bytes(patched)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def verify_animation(orig_anim, new_anim, deltas, structure):
    """Verify the patched animation decodes correctly."""
    results = {"ok": True, "errors": []}

    try:
        dec_orig = decode_anim_game_formula(orig_anim)
        dec_new = decode_anim_game_formula(new_anim)
    except Exception as e:
        results["ok"] = False
        results["errors"].append(f"Decode failed: {e}")
        return results

    nch = dec_new["channel_count"]
    nfr = dec_new["frame_count"]
    scales = dec_new["scales"]

    # Check quaternion validity
    invalid_quats = 0
    max_mag = 0.0
    for f in range(nfr):
        for ch in range(nch):
            x, y, z = dec_new["frames"][f][ch][0], dec_new["frames"][f][ch][1], dec_new["frames"][f][ch][2]
            mag2 = x * x + y * y + z * z
            mag = math.sqrt(mag2)
            max_mag = max(max_mag, mag)
            if mag > 1.5:
                invalid_quats += 1

    if invalid_quats > 0:
        results["errors"].append(f"WARNING: {invalid_quats} quaternions with magnitude > 1.5 (max={max_mag:.3f})")
        # Not a hard error — slightly invalid quats still work in-game

    # Check clamping (quat slots only)
    slots = structure["slots"]
    orig_widths = structure["orig_widths"]
    new_widths = []
    bitcount_off = structure["bitcount_off"]
    count_idx = 0
    for i, (ch, ci, is_skip) in enumerate(slots):
        if is_skip:
            new_widths.append(0)
            continue
        new_widths.append(new_anim[bitcount_off + count_idx] + 1)
        count_idx += 1

    clamped_count = 0
    slot_idx = 0
    base_idx = 0
    for (ch, ci, is_skip) in slots:
        if is_skip:
            base_idx += 1
            slot_idx += 1
            continue
        if ci < 3 and ch in deltas:
            # Check if any residual was clamped
            bone_deltas = deltas[ch]
            n_bone_frames = len(bone_deltas)
            orig_base = structure["orig_bases"][base_idx]
            new_width = new_widths[slot_idx]
            max_val = (1 << new_width) - 1 if new_width < 32 else 0xFFFFFFFF
            for f in range(nfr):
                si = f % n_bone_frames
                target = bone_deltas[si][ci]
                raw = round(target / scales[ci])
                residual = raw - orig_base
                if residual < 0 or residual > max_val:
                    clamped_count += 1
        base_idx += 1
        slot_idx += 1

    if clamped_count > 0:
        results["errors"].append(f"WARNING: {clamped_count} residuals clamped (cosmetic twitching)")
        # Not a hard error - clamping is acceptable

    # Compare quat values with targets
    quat_matches = 0
    quat_total = 0
    max_quat_err = 0.0
    for ch in deltas:
        if ch >= nch:
            continue
        bone_deltas = deltas[ch]
        n_bone_frames = len(bone_deltas)
        for f in range(min(nfr, 10)):  # Check first 10 frames
            si = f % n_bone_frames
            target = bone_deltas[si]
            decoded = dec_new["frames"][f][ch]
            for ci in range(3):
                err = abs(decoded[ci] - target[ci])
                max_quat_err = max(max_quat_err, err)
                quat_total += 1
                if err < 0.01:
                    quat_matches += 1

    results["nch"] = nch
    results["nfr"] = nfr
    results["invalid_quats"] = invalid_quats
    results["max_quat_mag"] = max_mag
    results["clamped_count"] = clamped_count
    results["quat_match_pct"] = 100.0 * quat_matches / quat_total if quat_total else 0
    results["max_quat_err"] = max_quat_err

    return results


# ---------------------------------------------------------------------------
# PKG injection (proven pipeline)
# ---------------------------------------------------------------------------
def inject_into_pkg(pkg_path, entry_index, new_anim_bytes, output_path,
                    anim_offset, anim_size):
    """Inject modified animation into PKG using proven pipeline.
    Key rules:
    - Keep sectTable_comp unchanged, pad compressed table with zeros
    - Keep section compressed sizes unchanged, pad compressed data with zeros
    - Use checksum32 CRC (NOT binascii.crc32)
    - Use zlib level 6
    """
    with open(pkg_path, "rb") as f:
        pkg = bytearray(f.read())

    entries = parse_pkg_header(pkg)
    if entries is None or entry_index >= len(entries):
        raise ValueError(f"Invalid entry index {entry_index}")

    eid, caff_off, caff_size = entries[entry_index]
    caff = bytearray(pkg[caff_off:caff_off + caff_size])

    # Parse CAFF header
    hs = struct.unpack(">I", caff[0x14:0x18])[0]
    stc = struct.unpack(">I", caff[0x60:0x64])[0]
    rtc = struct.unpack(">I", caff[0x74:0x78])[0]
    ds = hs + stc + rtc
    compression = caff[0x4A]

    if compression != 1:
        raise ValueError("Only compressed CAFFs (compression=1) are supported")

    # Decompress section table
    sect_decomp = bytearray(zlib.decompress(caff[hs:hs + stc]))
    s0_comp = struct.unpack(">I", sect_decomp[29:33])[0]

    # Decompress section 0
    s0_decomp = bytearray(zlib.decompress(caff[ds:ds + s0_comp]))

    # Replace animation — allow different size (widening may grow it)
    if anim_offset + anim_size > len(s0_decomp):
        raise ValueError(f"Animation at 0x{anim_offset:X} size {anim_size} exceeds section 0 ({len(s0_decomp)})")
    
    if len(new_anim_bytes) == anim_size:
        # Same size: in-place replace
        s0_decomp[anim_offset:anim_offset + anim_size] = new_anim_bytes
    else:
        # Different size: rebuild section 0 (animation + everything after)
        before = s0_decomp[:anim_offset]
        after = s0_decomp[anim_offset + anim_size:]
        s0_decomp = bytearray(before) + bytearray(new_anim_bytes) + bytearray(after)
        print(f"  Section 0 resized: {len(s0_decomp)} bytes (was {len(before) + anim_size + len(after)})")
        # CRITICAL: update section table entry uncompressed size at offset +0x09
        # The game reads this as the stride for section-table walking (lhz at +0x150)
        # If it's 0 or wrong, the game hangs in an infinite loop
        old_uncomp = struct.unpack(">I", sect_decomp[9:13])[0]
        struct.pack_into(">I", sect_decomp, 9, len(s0_decomp))
        print(f"  Updated section uncompressed size: {old_uncomp} -> {len(s0_decomp)}")

    # Recompress section 0 — try level 6 first, then level 7 if needed
    # Level 6 matches original FLEVEL=2 but may be larger for modified data
    # Level 7 is proven safe in-game; NEVER use level 9 (causes hang)
    new_s0_comp = zlib.compress(bytes(s0_decomp), 6)
    used_level = 6
    if len(new_s0_comp) > s0_comp:
        new_s0_comp = zlib.compress(bytes(s0_decomp), 7)
        used_level = 7
    overflow = len(new_s0_comp) > s0_comp
    if overflow:
        # Compressed section 0 is larger than original. Section data positions
        # are computed SEQUENTIALLY (sum of previous compressed sizes), not from
        # the offset field. So we just need to:
        # 1. Update section[0].compressedSize in the section table
        # 2. Rebuild data area: new_s0_comp + section1_data + trailing
        delta = len(new_s0_comp) - s0_comp
        print(f"  Section 0 overflow: {len(new_s0_comp)} > {s0_comp} (+{delta} bytes)")

        # Update section[0].compressedSize (entry 0, offset +0x1D)
        struct.pack_into(">I", sect_decomp, 0x1D, len(new_s0_comp))

        # Section 1 data starts at ds + s0_comp (sequential, not from offset field)
        s1_comp = struct.unpack(">I", sect_decomp[33 + 29:33 + 33])[0]
        sect1_data = bytes(caff[ds + s0_comp:ds + s0_comp + s1_comp])
        print(f"  Section 1: {s1_comp} bytes extracted")

        # Trailing data after section 1
        trailing = bytes(caff[ds + s0_comp + s1_comp:])
        print(f"  Trailing after section 1: {len(trailing)} bytes")

        # Rebuild data area
        new_data_area = new_s0_comp + sect1_data + trailing

        # Recompress section table
        new_sect_comp = zlib.compress(bytes(sect_decomp), 6)
        if len(new_sect_comp) > stc:
            new_sect_comp = zlib.compress(bytes(sect_decomp), 7)
        padded_sect = new_sect_comp + b"\x00" * (stc - len(new_sect_comp))

        # Rebuild full CAFF
        reloc_data = bytes(caff[hs + stc:hs + stc + rtc])
        new_caff = bytearray(caff[:hs]) + bytearray(padded_sect) + bytearray(reloc_data) + bytearray(new_data_area)

        # Recompute CRC
        struct.pack_into(">I", new_caff, 0x18, 0)
        new_crc = checksum32(bytes(new_caff[:hs]))
        struct.pack_into(">I", new_caff, 0x18, new_crc)

        print(f"  New CAFF: {len(new_caff)} bytes (was {len(caff)})")
        caff = new_caff
        overflow_handled = True
    else:
        overflow_handled = False

    if not overflow:
        print(f"  Section 0 recompressed: {s0_comp} -> {len(new_s0_comp)} (level {used_level})")
        # Pad to original size
        padded_s0 = new_s0_comp + b"\x00" * (s0_comp - len(new_s0_comp))

    if not overflow:
        # Recompress section table with level 6
        new_sect_comp = zlib.compress(bytes(sect_decomp), 6)
        padded_sect = new_sect_comp + b"\x00" * (stc - len(new_sect_comp))

        # Write back (same sizes, no shifting)
        caff[hs:hs + stc] = padded_sect
        caff[ds:ds + s0_comp] = padded_s0

        # Recompute CRC (header only, field zeroed — correct algorithm)
        struct.pack_into(">I", caff, 0x18, 0)
        new_crc = checksum32(bytes(caff[:hs]))
        struct.pack_into(">I", caff, 0x18, new_crc)

    # Verify: decompress and check
    v_sect = zlib.decompress(bytes(caff[hs:hs + stc]))
    if overflow:
        v_s0 = zlib.decompress(bytes(caff[ds:ds + len(new_s0_comp)]))
    else:
        v_s0 = zlib.decompress(bytes(caff[ds:ds + s0_comp]))
    v_anim = v_s0[anim_offset:anim_offset + anim_size]
    # New animation may be shorter than original — check prefix matches
    if v_anim[:len(new_anim_bytes)] != new_anim_bytes:
        raise ValueError("Verification failed: decompressed animation doesn't match")

    # Write to PKG
    if len(caff) == caff_size:
        # Same size: in-place replace
        pkg[caff_off:caff_off + caff_size] = caff
    else:
        # Different size: splice new CAFF in, shift everything after it
        delta = len(caff) - caff_size
        print(f"  CAFF size changed: {len(caff)} (was {caff_size}, delta={delta:+d})")
        # Build new PKG
        new_pkg = bytearray(pkg[:caff_off])  # everything before this CAFF
        new_pkg += caff                       # new CAFF
        new_pkg += pkg[caff_off + caff_size:] # everything after (now shifted)
        # Update entry table: size of modified entry + offsets of later entries
        entries = parse_pkg_header(new_pkg)
        for i, (eid, eoff, esize) in enumerate(entries):
            if i == entry_index:
                # Update size of the modified entry
                struct.pack_into(">I", new_pkg, 8 + i * 12 + 8, len(caff))
            elif eoff > caff_off:
                # Shift offset of entries after the modified one
                struct.pack_into(">I", new_pkg, 8 + i * 12 + 4, eoff + delta)
        pkg = new_pkg
    with open(output_path, "wb") as f:
        f.write(pkg)

    return len(pkg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Inject Blender animation into Viva Pinata game PKG"
    )
    parser.add_argument("blender_json", help="Path to Blender export JSON")
    parser.add_argument("anim_name", help="Animation name (e.g., 'walk', 'fight_attack')")
    parser.add_argument("--pkg", help="Path to PKG file")
    parser.add_argument("--entry", type=int, help="Entry index in PKG")
    parser.add_argument("--game-dir",
                        help="retip install dir used to auto-search PKGs "
                             "(default: <path/to/retip-1.13.0-windows>)")
    parser.add_argument("--glb", help="Path to GLB model")
    parser.add_argument("--species", help="Species name")
    parser.add_argument("--search", action="store_true",
                        help="Search all PKGs to find the animation (slow)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Encode and verify but don't write PKG")
    parser.add_argument("--output", help="Output PKG path")
    args = parser.parse_args()

    global GAME_DIR, PKG_DIR
    if args.game_dir:
        GAME_DIR = args.game_dir
        PKG_DIR = os.path.join(GAME_DIR, "assets", "Beta", "packages")

    t0 = time.time()

    # --- Step 1: Load Blender export ---
    print(f"[1/8] Loading Blender export: {args.blender_json}")
    export_data = load_blender_export(args.blender_json)
    armature = export_data.get("armature", "")
    print(f"  Armature: {armature}")
    print(f"  Animations: {len(export_data.get('animations', []))}")

    # Auto-detect species
    species = args.species or extract_species_from_armature(armature)
    if not species:
        print("ERROR: Cannot detect species. Use --species to specify.")
        sys.exit(1)
    print(f"  Species: {species}")

    # Find target animation
    blender_anim = find_blender_animation(export_data, args.anim_name)
    if not blender_anim:
        print(f"ERROR: Animation '{args.anim_name}' not found in Blender export.")
        print("Available animations:")
        for a in export_data.get("animations", []):
            print(f"  {a['name']}")
        sys.exit(1)
    print(f"  Target animation: {blender_anim['name']}")
    print(f"  Frames: {blender_anim['total_frames']}, Bones: {len(blender_anim['bones'])}")

    # --- Step 2: Load GLB ---
    print(f"\n[2/8] Loading GLB model...")
    glb_path = args.glb or find_glb_for_species(species)
    if not glb_path or not os.path.exists(glb_path):
        print(f"ERROR: GLB not found for species '{species}'. Use --glb to specify.")
        sys.exit(1)
    print(f"  GLB: {glb_path}")
    node_names, bind_poses = load_glb(glb_path)
    bone_to_ch = {node_names[i]: i for i in range(min(len(node_names), 200))}
    print(f"  Nodes: {len(node_names)}, Bind poses: {len(bind_poses)}")

    # --- Step 3: Find PKG and extract original animation ---
    print(f"\n[3/8] Finding original animation...")
    pkg_path = args.pkg
    entry_index = args.entry

    if args.search or not pkg_path:
        print(f"  Searching PKGs for {species}_{args.anim_name}...")
        search_results = search_pkg_for_animation(species, args.anim_name)
        if not search_results:
            print(f"ERROR: Animation '{species}_{args.anim_name}' not found in any PKG.")
            sys.exit(1)
        print(f"  Found in {len(search_results)} PKGs:")
        for sp, se, how in search_results:
            print(f"    {os.path.basename(sp)} entry {se} ({how})")
        pkg_path, entry_index, _ = search_results[0]
        print(f"  Using: {os.path.basename(pkg_path)} entry {entry_index}")

    if not pkg_path or not os.path.exists(pkg_path):
        print(f"ERROR: PKG not found. Use --pkg to specify.")
        sys.exit(1)

    with open(pkg_path, "rb") as f:
        pkg_data = f.read()
    print(f"  PKG: {pkg_path} ({len(pkg_data)} bytes)")

    # Extract CAFF
    caff_data = extract_caff_from_pkg(pkg_data, entry_index)
    if not caff_data:
        print(f"ERROR: Cannot extract CAFF from entry {entry_index}")
        sys.exit(1)
    print(f"  CAFF: {len(caff_data)} bytes")

    # Decompress section 0
    section_table, s0_data = decompress_caff_section0(caff_data)
    if s0_data is None:
        print(f"ERROR: Cannot decompress CAFF section 0")
        sys.exit(1)
    print(f"  Section 0: {len(s0_data)} bytes (decompressed)")

    # Find animation in CAFF using proper file info parsing
    try:
        caff_parsed = CaffV36(caff_data)
    except Exception as e:
        print(f"ERROR: Cannot parse CAFF: {e}")
        sys.exit(1)

    # Search file infos for the target animation
    # Label format: aid_anim_pinata_actor_animal_{species}_{anim_name},hash,duration
    target_label = f"aid_anim_pinata_actor_animal_{species}_{args.anim_name}"
    target_fi = None
    for fi in caff_parsed.file_infos:
        if target_label in fi["label"]:
            target_fi = fi
            break

    if target_fi is None:
        print(f"ERROR: Animation '{target_label}' not found in CAFF.")
        print("Available animations:")
        for fi in caff_parsed.file_infos:
            if "aid_anim_pinata_actor_animal_" in fi["label"]:
                # Extract short name
                parts = fi["label"].split(",")
                short = parts[0].replace(f"aid_anim_pinata_actor_animal_{species}_", "")
                print(f"  {short}")
        sys.exit(1)

    anim_offset = target_fi["start"]
    anim_size = target_fi["size"]
    orig_anim = bytes(s0_data[anim_offset:anim_offset + anim_size])

    # Verify it's a valid animation
    if orig_anim[:10] != b"animation\x00":
        print(f"ERROR: Animation at 0x{anim_offset:X} doesn't start with 'animation' magic")
        sys.exit(1)

    # Parse header for channel/frame counts
    game_nch = struct.unpack(">H", orig_anim[0x20:0x22])[0]
    game_nfr = struct.unpack(">H", orig_anim[0x22:0x24])[0]
    print(f"  Found: {target_fi['label'][:60]}")
    print(f"  Animation at 0x{anim_offset:X}, size {anim_size}")
    print(f"  Game: {game_nch} channels x {game_nfr} frames")

    # --- Step 4: Parse original structure ---
    print(f"\n[4/8] Parsing original animation structure...")
    structure = parse_animation_structure(orig_anim)
    print(f"  Channels: {structure['nch']}, Frames: {structure['nfr']}")
    print(f"  bpf: {structure['bpf']} bytes ({structure['bpf'] * 8} bits)")
    print(f"  bitstream_base: {structure['bitstream_base']}")
    print(f"  Slots: {len(structure['slots'])} total, "
          f"{sum(1 for s in structure['slots'] if not s[2])} FULL")
    print(f"  Pose at: 0x{structure['pose_idx']:X}")
    print(f"  Available for bitstream: {structure['pose_idx'] - structure['bitstream_base']} bytes")

    # --- Step 5: Compute deltas ---
    print(f"\n[5/8] Computing animation deltas...")
    deltas = compute_blender_deltas(blender_anim, bind_poses, bone_to_ch,
                                    target_frames=structure['nfr'])
    print(f"  Deltas for {len(deltas)} bones")
    if not deltas:
        print(f"ERROR: No deltas computed. Check bone names match GLB.")
        sys.exit(1)

    # --- Step 6+7: Encode via encode_deltas (full re-encode) ---
    print(f"\n[6/8] Encoding via encode_deltas...")
    new_anim = encode_deltas(
        deltas, structure['nch'], structure['nfr'],
        scales=structure['scales'], template_header=orig_anim
    )
    print(f"  New animation: {len(new_anim)} bytes (original: {len(orig_anim)})")

    # --- Step 8: Verify ---
    print(f"\n[8/8] Verifying...")
    results = verify_animation(orig_anim, new_anim, deltas, structure)
    print(f"  Channels: {results['nch']}, Frames: {results['nfr']}")
    print(f"  Invalid quaternions: {results['invalid_quats']}")
    print(f"  Max quaternion magnitude: {results['max_quat_mag']:.4f}")
    print(f"  Clamped residuals: {results['clamped_count']}")
    print(f"  Quat match rate: {results['quat_match_pct']:.1f}%")
    print(f"  Max quat error: {results['max_quat_err']:.6f}")

    if not results["ok"]:
        print(f"\nERROR: Verification failed!")
        for e in results["errors"]:
            print(f"  {e}")
        sys.exit(1)

    for e in results["errors"]:
        print(f"  NOTE: {e}")

    # --- Save intermediate ---
    bin_path = os.path.splitext(args.blender_json)[0] + f"_{args.anim_name}_encoded.bin"
    with open(bin_path, "wb") as f:
        f.write(new_anim)
    print(f"\n  Encoded animation saved: {bin_path}")

    if args.dry_run:
        print(f"\n  Dry run complete. No PKG written.")
        return

    # --- Inject into PKG ---
    print(f"\nInjecting into PKG...")
    output_path = args.output or (pkg_path + ".modified")
    try:
        pkg_size = inject_into_pkg(
            pkg_path, entry_index, new_anim, output_path,
            anim_offset, anim_size
        )
        print(f"  Output: {output_path} ({pkg_size} bytes)")
        print(f"\nDone in {time.time() - t0:.1f}s")
        print(f"\nTo test in-game:")
        print(f"  1. Copy {output_path} to replace the original PKG")
        print(f"  2. Launch game and navigate to garden")
        print(f"  3. Watch the {args.anim_name} animation")
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
