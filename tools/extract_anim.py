#!/usr/bin/env python3
"""
extract_anim.py — Extract a raw animation blob from a Viva Piñata PKG archive.

Uses the CAFF V36 parser from splice_anim.py.

Usage:
    python extract_anim.py --pkg <path> --entry <index> --anim <name> --out <file>
    python extract_anim.py --pkg <path> --entry <index> --list
    python extract_anim.py --search <name_substring>

Examples:
    python extract_anim.py --pkg game/packages/1150.pkg --entry 10 --anim walk --out walk.bin
    python extract_anim.py --pkg game/packages/1150.pkg --entry 10 --list
    python extract_anim.py --search fight_attack
"""
import argparse
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from splice_anim import CaffV36


def parse_pkg_header(data):
    """Parse PKG header + entry table. Returns list of (id, offset, size)."""
    if len(data) < 8:
        return []
    entry_count = struct.unpack(">I", data[4:8])[0]
    entries = []
    for i in range(entry_count):
        off = 8 + i * 12
        if off + 12 > len(data):
            break
        eid, eoffset, esize = struct.unpack(">III", data[off:off + 12])
        entries.append((eid, eoffset, esize))
    return entries


def extract_caff_from_pkg(pkg_data, entry_index):
    """Extract raw CAFF bytes from a PKG entry."""
    entries = parse_pkg_header(pkg_data)
    if entry_index >= len(entries):
        return None
    _, eoffset, esize = entries[entry_index]
    return pkg_data[eoffset:eoffset + esize]


def search_all_pkgs(pkg_dir, anim_name):
    """Search all PKGs in a directory for an animation. Returns list of (pkg_path, entry, how)."""
    target = anim_name.encode()
    results = []
    pkg_files = sorted(
        [f for f in os.listdir(pkg_dir) if f.endswith(".pkg") and not f.endswith(".backup")],
        key=lambda x: int(x.split(".")[0]) if x.split(".")[0].isdigit() else 0,
    )
    for pkg_name in pkg_files:
        pkg_path = os.path.join(pkg_dir, pkg_name)
        try:
            with open(pkg_path, "rb") as f:
                data = f.read()
        except Exception:
            continue
        entries = parse_pkg_header(data)
        for j, (eid, eoffset, esize) in enumerate(entries):
            if eoffset + esize > len(data):
                continue
            caff = data[eoffset:eoffset + esize]
            if caff[:4] != b"CAFF":
                continue
            # Quick check: search raw CAFF
            if target in caff[:8192]:
                results.append((pkg_path, j, "raw"))
                continue
            # Check decompressed section table + section 0
            try:
                parsed = CaffV36(caff)
                for fi in parsed.file_infos:
                    if anim_name in fi["label"]:
                        results.append((pkg_path, j, "decompressed"))
                        break
            except Exception:
                pass
    return results


def main():
    parser = argparse.ArgumentParser(description="Extract animation from Viva Piñata PKG")
    parser.add_argument("--pkg", help="Path to PKG file")
    parser.add_argument("--entry", type=int, help="Entry index in PKG")
    parser.add_argument("--anim", help="Animation name substring (e.g. 'walk', 'fight_attack')")
    parser.add_argument("--out", help="Output file path")
    parser.add_argument("--search", help="Search all PKGs in VP_GAME_DIR for this animation")
    parser.add_argument("--list", action="store_true", help="List all animations in the PKG entry")
    args = parser.parse_args()

    if args.search:
        pkg_dir = os.environ.get("VP_GAME_DIR", "")
        if pkg_dir:
            pkg_dir = os.path.join(pkg_dir, "assets", "Beta", "packages")
        if not pkg_dir or not os.path.isdir(pkg_dir):
            print("ERROR: Set VP_GAME_DIR environment variable to search PKGs.")
            print("  Example: set VP_GAME_DIR=C:\\path\\to\\game")
            sys.exit(1)
        print(f"Searching PKGs in {pkg_dir} for '{args.search}'...")
        results = search_all_pkgs(pkg_dir, args.search)
        if not results:
            print("No matches found.")
        else:
            for pkg_path, entry, how in results:
                print(f"  {os.path.basename(pkg_path)} entry {entry} ({how})")
        return

    if not args.pkg or not os.path.exists(args.pkg):
        print("ERROR: --pkg is required (or use --search).")
        sys.exit(1)

    with open(args.pkg, "rb") as f:
        pkg_data = f.read()

    if args.entry is None:
        print("ERROR: --entry is required.")
        sys.exit(1)

    caff_data = extract_caff_from_pkg(pkg_data, args.entry)
    if not caff_data or caff_data[:4] != b"CAFF":
        print(f"ERROR: Entry {args.entry} is not a valid CAFF.")
        sys.exit(1)

    try:
        caff = CaffV36(caff_data)
    except Exception as e:
        print(f"ERROR: Cannot parse CAFF: {e}")
        sys.exit(1)

    if args.list:
        print(f"Animations in entry {args.entry}:")
        for fi in caff.file_infos:
            if "aid_anim_pinata_actor_animal_" in fi["label"]:
                parts = fi["label"].split(",")
                short = parts[0].replace("aid_anim_pinata_actor_animal_", "")
                print(f"  {short:20s}  offset=0x{fi['start']:06X}  size={fi['size']}")
        return

    if not args.anim:
        print("ERROR: --anim is required (or use --list).")
        sys.exit(1)

    # Find matching animation
    target_fi = None
    for fi in caff.file_infos:
        if args.anim in fi["label"] and "aid_anim" in fi["label"]:
            target_fi = fi
            break

    if not target_fi:
        print(f"ERROR: Animation '{args.anim}' not found in entry {args.entry}.")
        print("Available animations:")
        for fi in caff.file_infos:
            if "aid_anim_pinata_actor_animal_" in fi["label"]:
                parts = fi["label"].split(",")
                short = parts[0].replace("aid_anim_pinata_actor_animal_", "")
                print(f"  {short}")
        sys.exit(1)

    anim_bytes = caff.get_asset_data(args.anim)

    if not args.out:
        short = args.anim.replace("/", "_")
        args.out = f"{short}.bin"

    with open(args.out, "wb") as f:
        f.write(anim_bytes)

    print(f"Extracted: {target_fi['label'][:60]}")
    print(f"  Offset: 0x{target_fi['start']:X}, Size: {len(anim_bytes)} bytes")
    print(f"  Written: {args.out}")


if __name__ == "__main__":
    main()
