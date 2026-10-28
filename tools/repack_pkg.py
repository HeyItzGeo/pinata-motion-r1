"""
PKG Repacker — replace one CAFF entry inside a Viva Pinata PKG archive.

PKG format (from CAFFeinated PinataPKG.cpp + MeExtractModels FUN_14003a900):
  Header:   8 bytes — version (BE u32, 1 or 2) + entryCount (BE u32)
  Entries:  entryCount x 12 bytes — id (BE u32) + offset (BE u32) + size (BE u32)
  EOF:      4 bytes — 0xFF (LE int32: FF 00 00 00)
  Padding:  zeros to next 2048-byte boundary
  CAFFs:    each followed by 4-byte EOF (FF 00 00 00) + zero padding to 2048

Usage:
  python repack_pkg.py <input.pkg> <entry_index> <new_caff.bin> <output.pkg>
  python repack_pkg.py --test
"""
import struct
import sys
import os

ALIGN = 2048
EOF_MARKER = b"\xFF\x00\x00\x00"  # LE int32(0xFF)


def read_pkg(path: str) -> dict:
    """Parse a PKG file into header + entries + raw CAFF blobs."""
    data = open(path, "rb").read()
    if len(data) < 8:
        raise ValueError("PKG too small")

    version = struct.unpack_from(">I", data, 0)[0]
    entry_count = struct.unpack_from(">I", data, 4)[0]
    if version not in (1, 2):
        raise ValueError(f"Unknown PKG version {version}")
    if not (1 <= entry_count <= 999999):
        raise ValueError(f"Invalid entry count {entry_count}")

    entries = []
    for i in range(entry_count):
        pos = 8 + i * 12
        eid = struct.unpack_from(">I", data, pos)[0]
        offset = struct.unpack_from(">I", data, pos + 4)[0]
        size = struct.unpack_from(">I", data, pos + 8)[0]
        entries.append({"id": eid, "offset": offset, "size": size})

    # Extract raw CAFF blobs
    caffs = []
    for e in entries:
        if e["offset"] + e["size"] > len(data):
            raise ValueError(f"Entry at {e['offset']}+{e['size']} exceeds file size {len(data)}")
        caffs.append(data[e["offset"]:e["offset"] + e["size"]])

    return {
        "version": version,
        "entry_count": entry_count,
        "entries": entries,
        "caffs": caffs,
        "raw_size": len(data),
    }


def write_pkg(version: int, entries: list, caffs: list, out_path: str) -> int:
    """
    Write a PKG file with the given entries and CAFF blobs.
    entries: list of dicts with 'id' keys (offset/size recalculated).
    Returns: output file size.
    """
    # Calculate offsets: header + entry table + EOF + padding to ALIGN
    header_block = 8 + len(entries) * 12 + 4  # version+count + entries + EOF
    first_caff_offset = ((header_block + ALIGN - 1) // ALIGN) * ALIGN

    # Build entry table with new offsets/sizes
    out_entries = []
    current_offset = first_caff_offset
    for i, (entry, caff) in enumerate(zip(entries, caffs)):
        out_entries.append({
            "id": entry["id"],
            "offset": current_offset,
            "size": len(caff),
        })
        # CAFF + EOF + padding to ALIGN
        remain = len(caff) % ALIGN
        pad = (ALIGN - remain) - 4 if remain != 0 else ALIGN - 4
        current_offset += len(caff) + 4 + pad

    # Write
    with open(out_path, "wb") as f:
        # Header
        f.write(struct.pack(">I", version))
        f.write(struct.pack(">I", len(entries)))
        # Entry table
        for e in out_entries:
            f.write(struct.pack(">I", e["id"]))
            f.write(struct.pack(">I", e["offset"]))
            f.write(struct.pack(">I", e["size"]))
        # EOF + padding to first CAFF
        f.write(EOF_MARKER)
        header_end = 8 + len(entries) * 12 + 4
        pad = first_caff_offset - header_end
        if pad > 0:
            f.write(b"\x00" * pad)
        # CAFF blobs
        for caff in caffs:
            f.write(caff)
            f.write(EOF_MARKER)
            remain = len(caff) % ALIGN
            pad = (ALIGN - remain) - 4 if remain != 0 else ALIGN - 4
            if pad > 0:
                f.write(b"\x00" * pad)

    return os.path.getsize(out_path)


def replace_caff(pkg_path: str, entry_index: int, new_caff_path: str,
                 out_path: str) -> dict:
    """Replace one CAFF entry in a PKG and write the result."""
    pkg = read_pkg(pkg_path)
    new_caff = open(new_caff_path, "rb").read()

    if not (0 <= entry_index < pkg["entry_count"]):
        raise ValueError(f"Entry index {entry_index} out of range (0..{pkg['entry_count']-1})")

    old_caff = pkg["caffs"][entry_index]
    pkg["caffs"][entry_index] = new_caff

    out_size = write_pkg(pkg["version"], pkg["entries"], pkg["caffs"], out_path)

    return {
        "input_size": pkg["raw_size"],
        "output_size": out_size,
        "entry_index": entry_index,
        "old_caff_size": len(old_caff),
        "new_caff_size": len(new_caff),
        "entry_count": pkg["entry_count"],
        "output_path": out_path,
    }


def verify_pkg(path: str) -> dict:
    """Verify a PKG file's structure."""
    pkg = read_pkg(path)
    issues = []

    for i, e in enumerate(pkg["entries"]):
        if e["offset"] + e["size"] > pkg["raw_size"]:
            issues.append(f"entry {i}: offset {e['offset']}+{e['size']} exceeds file")
        # Check CAFF magic
        caff = pkg["caffs"][i]
        if caff[0:4] != b"CAFF":
            issues.append(f"entry {i}: missing CAFF magic at offset {e['offset']}")
        # Check alignment
        if e["offset"] % ALIGN != 0:
            issues.append(f"entry {i}: offset {e['offset']} not {ALIGN}-aligned")

    return {
        "path": path,
        "file_size": pkg["raw_size"],
        "version": pkg["version"],
        "entry_count": pkg["entry_count"],
        "entries": [(e["id"], e["offset"], e["size"]) for e in pkg["entries"]],
        "issues": issues,
        "ok": len(issues) == 0,
    }


def run_test():
    """Self-test: build a synthetic PKG, replace an entry, verify."""
    import tempfile
    from splice_anim import CaffV36, splice_caff

    tools_dir = os.path.dirname(os.path.abspath(__file__))
    caff_path = os.path.join(tools_dir, "aid_anim_pinata_actor_animal_crocodile_eat_full.bin")
    data_path = os.path.join(tools_dir, "aid_anim_pinata_actor_animal_crocodile_eat_data.bin")

    print("=" * 72)
    print("PKG REPACKER — SELF TEST")
    print("=" * 72)

    if not os.path.exists(caff_path):
        print(f"FAIL: missing {caff_path}")
        return False

    caff_data = open(caff_path, "rb").read()

    # [1] Build a synthetic 3-entry PKG containing our CAFF
    print("\n[1] Build synthetic PKG with 3 CAFF entries")
    tmpdir = tempfile.mkdtemp()
    pkg_path = os.path.join(tmpdir, "test.pkg")
    # Use the same CAFF for all 3 entries (id=100,101,102)
    entries = [{"id": 100}, {"id": 101}, {"id": 102}]
    caffs = [caff_data, caff_data, caff_data]
    out_size = write_pkg(2, entries, caffs, pkg_path)
    print(f"  Built PKG: {out_size} bytes, 3 entries")

    v = verify_pkg(pkg_path)
    print(f"  Verify: ok={v['ok']} issues={v['issues']}")
    for eid, off, sz in v["entries"]:
        print(f"    id={eid} offset={off} size={sz}")

    # [2] Read it back
    print("\n[2] Read back PKG")
    pkg = read_pkg(pkg_path)
    print(f"  version={pkg['version']} entries={pkg['entry_count']}")
    for i, e in enumerate(pkg["entries"]):
        magic = pkg["caffs"][i][0:4]
        print(f"    [{i}] id={e['id']} offset={e['offset']} size={e['size']} magic={magic}")

    # [3] Replace entry 1 with a spliced CAFF (encode_quat_v4 output)
    print("\n[3] Replace entry 1 with spliced CAFF")
    from encode_quat_v4 import encode_deltas, decode_anim_game_formula
    real = open(data_path, "rb").read()
    dec = decode_anim_game_formula(real)
    nch, nfr = dec["channel_count"], dec["frame_count"]
    delta_xyz = {}
    for ch in range(nch):
        frames = []
        for f in range(nfr):
            frames.append((dec["frames"][f][ch][0], dec["frames"][f][ch][1], dec["frames"][f][ch][2]))
        delta_xyz[ch] = frames
    new_blob = encode_deltas(delta_xyz, nch, nfr)

    # Splice the new blob into a copy of the original CAFF
    caff_orig = CaffV36(caff_data)
    caff_orig.replace_asset_data("aid_anim", new_blob)
    spliced_caff = caff_orig.build()

    new_pkg_path = os.path.join(tmpdir, "test_replaced.pkg")
    result = replace_caff(pkg_path, 1, caff_path.replace("_full.bin", "_full.bin"), new_pkg_path)
    # Actually need to write the spliced CAFF first
    spliced_path = os.path.join(tmpdir, "spliced.bin")
    open(spliced_path, "wb").write(spliced_caff)
    result = replace_caff(pkg_path, 1, spliced_path, new_pkg_path)
    print(f"  old_caff_size={result['old_caff_size']} new_caff_size={result['new_caff_size']}")
    print(f"  PKG: {result['input_size']} -> {result['output_size']} bytes")

    # [4] Verify replaced PKG
    print("\n[4] Verify replaced PKG")
    v2 = verify_pkg(new_pkg_path)
    print(f"  ok={v2['ok']} issues={v2['issues']}")
    for eid, off, sz in v2["entries"]:
        print(f"    id={eid} offset={off} size={sz}")

    # [5] Read back and verify the replaced entry decodes
    print("\n[5] Decode replaced entry")
    pkg2 = read_pkg(new_pkg_path)
    entry1_caff = pkg2["caffs"][1]
    print(f"  Entry 1 CAFF: {len(entry1_caff)} bytes, magic={entry1_caff[0:4]}")

    # Extract animation from spliced CAFF and decode
    c3 = CaffV36(entry1_caff)
    anim = c3.get_asset_data("aid_anim")
    dec2 = decode_anim_game_formula(anim)
    max_err = 0.0
    for f in range(min(nfr, dec2["frame_count"])):
        for ch in range(min(nch, dec2["channel_count"])):
            for ci in range(3):
                err = abs(dec["frames"][f][ch][ci] - dec2["frames"][f][ch][ci])
                max_err = max(max_err, err)
    print(f"  Animation decode: {dec2['channel_count']}ch x {dec2['frame_count']}f, max_err={max_err:.9f}")

    # Verify entries 0 and 2 are unchanged
    entry0_same = pkg2["caffs"][0] == caff_data
    entry2_same = pkg2["caffs"][2] == caff_data
    print(f"  Entry 0 unchanged: {entry0_same}")
    print(f"  Entry 2 unchanged: {entry2_same}")

    # Cleanup
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    status = "PASS" if (v["ok"] and v2["ok"] and max_err < 1e-4
                        and entry0_same and entry2_same) else "FAIL"
    print(f"\n  RESULT [{status}]")
    return status == "PASS"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        ok = run_test()
        sys.exit(0 if ok else 1)
    elif len(sys.argv) >= 5:
        result = replace_caff(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4])
        print(f"Replaced entry {result['entry_index']}:")
        print(f"  CAFF: {result['old_caff_size']} -> {result['new_caff_size']} bytes")
        print(f"  PKG: {result['input_size']} -> {result['output_size']} bytes")
        v = verify_pkg(sys.argv[4])
        print(f"  Verify: ok={v['ok']} issues={v['issues']}")
    else:
        print(__doc__)
