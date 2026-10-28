#!/usr/bin/env python3
"""inject_walk_blob.py — inject a SAME-SIZE animation blob into a PKG, fully offline.

PKG entry 10 = crocodile CAFF. Target asset resolved by file-info label
(default: aid_anim_pinata_actor_animal_crocodile_walk, size 6944; --label/--size
select e.g. aid_anim_pinata_actor_animal_crocodile_fight_attack, 19800).
Same-size guarantee: every file-info, section offset and header field stays valid by
construction; only section-0 bytes + compressed streams + CAFF CRC change.

--allow-grow: when the patched section-0 stream no longer fits its
on-disk slot even at zlib level 7, grow the CAFF IN PLACE by consuming the PKG
entry-slot padding (next entry must stay put). Blob itself stays same-size, so the
FI table / reloc table / s0 decompressed layout are untouched; only
sect[0].compressedSize, the s0 stream length and the CAFF CRC change.
Hard constraints: sect/reloc table header comp-size spans frozen (pad streams with
zeros), zlib levels 6-7 ONLY, CAFF must still end before the next entry's data
start (0x136000 for entry 10), bytes [next_entry_start, EOF) byte-identical.

Reused plumbing (proven in-game):
  inject_animation.extract_caff_from_pkg  (inject_animation.py:144)
  inject_animation.inject_into_pkg        (inject_animation.py:855)  — decompress s0,
      in-place patch, zlib level 6->7, PAD compressed output to original sizes
      (sect[0].compressedSize UNCHANGED), checksum32 CRC, in-place PKG write
  splice_anim.CaffV36                     (splice_anim.py:59)        — file-info lookup
  splice_anim.checksum32                  (splice_anim.py:28)        — CRC revalidation

Usage:
  python inject_walk_blob.py --blob orig_walk_anim.bin --pkg vanilla_1150.pkg --output out.pkg
  python inject_walk_blob.py --blob orig_fight_attack.bin \\
      --label aid_anim_pinata_actor_animal_crocodile_fight_attack --size 19800 \\
      --pkg vanilla_1150.pkg --output out.pkg
  python inject_walk_blob.py --blob fight_attack_spin_z.bin ... --allow-grow
"""
import argparse, os, struct, sys, zlib

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from inject_animation import extract_caff_from_pkg, inject_into_pkg, parse_pkg_header
from splice_anim import CaffV36, checksum32

ENTRY = 10
DEFAULT_LABEL = "aid_anim_pinata_actor_animal_crocodile_walk"
DEFAULT_SIZE = 6944
# Entry-10 slot geometry in vanilla_1150.pkg (EE35818D): CAFF @ 0xD9000, next
# entry data starts at 0x136000. FF000000 terminator sits at old CAFF end;
# the gap in between is zero pad that the grow path reuses.
NEXT_ENTRY_START = 0x136000
TERM = b"\xFF\x00\x00\x00"


def caff_parts(caff):
    """Return (hs, ds, s0_comp_size, decompressed_sect_table, decompressed_s0)."""
    hs = struct.unpack_from(">I", caff, 0x14)[0]
    stc = struct.unpack_from(">I", caff, 0x60)[0]
    rtc = struct.unpack_from(">I", caff, 0x74)[0]
    ds = hs + stc + rtc
    st = zlib.decompress(bytes(caff[hs:hs + stc]))
    s0cs = struct.unpack_from(">I", st, 29)[0]
    s0 = zlib.decompress(bytes(caff[ds:ds + s0cs]))
    return hs, ds, s0cs, st, s0

def crc_of(caff, hs):
    hdr = bytearray(caff[:hs]); struct.pack_into(">I", hdr, 0x18, 0)
    return checksum32(bytes(hdr))


def gate(name, ok, detail):
    print(f"  [{'PASS' if ok else 'FAIL'}] ({name}) {detail}")
    return ok


def verify(pkg_in, pkg_out, blob, start, size):
    print("\nBuilt-in verification gates:")
    ok = True
    entries_in = struct.unpack_from(">I", pkg_in, 4)[0]
    ent_tbl = 8 + entries_in * 12
    off_in, sz_in = struct.unpack_from(">II", pkg_in, 8 + ENTRY * 12 + 4)
    off_out, sz_out = struct.unpack_from(">II", pkg_out, 8 + ENTRY * 12 + 4)
    caff_in, caff_out = pkg_in[off_in:off_in + sz_in], pkg_out[off_out:off_out + sz_out]
    hs_i, ds_i, s0cs_i, st_i, s0_i = caff_parts(caff_in)
    hs_o, ds_o, s0cs_o, st_o, s0_o = caff_parts(caff_out)

    re_ext = s0_o[start:start + size]
    ok &= gate("a", re_ext == blob, f"re-extracted blob == input blob: {re_ext == blob}")
    outside = [i for i, (x, y) in enumerate(zip(s0_i, s0_o)) if x != y
               if not (start <= i < start + size)]
    in_count = sum(1 for i, (x, y) in enumerate(zip(s0_i, s0_o)) if x != y
                   if start <= i < start + size)
    ok &= gate("b", not outside, f"s0 diffs: {in_count} inside asset [{start}..{start+size}), "
               f"{len(outside)} outside")
    ok &= gate("c", st_i == st_o, f"file-info/section table decompressed byte-identical: {st_i == st_o}")
    hdr_diff = [i for i, (x, y) in enumerate(zip(caff_in[:hs_i], caff_out[:hs_o])) if x != y]
    ok &= gate("d", hs_i == hs_o and set(hdr_diff) <= {0x18, 0x19, 0x1A, 0x1B},
               f"CAFF header diffs {['0x%X' % i for i in hdr_diff]} subset of CRC 0x18-0x1B (empty=identity)")
    ok &= gate("e", st_i == st_o, f"section table (decompressed) byte-identical: {st_i == st_o}")
    tbl_same = pkg_in[:ent_tbl] == pkg_out[:ent_tbl]
    ok &= gate("f", tbl_same, f"PKG entry table byte-identical: {tbl_same}")
    out_same = (pkg_in[:off_in] == pkg_out[:off_out]
                and pkg_in[off_in + sz_in:] == pkg_out[off_out + sz_out:])
    ok &= gate("g", out_same, f"bytes outside CAFF byte-identical: {out_same}")
    valid = crc_of(caff_out, hs_o) == struct.unpack_from(">I", caff_out, 0x18)[0]
    ok &= gate("h", valid, f"CAFF CRC validates: stored=0x{struct.unpack_from('>I', caff_out, 0x18)[0]:08X} "
               f"calc=0x{crc_of(caff_out, hs_o):08X}")
    ok &= gate("i", sz_in == sz_out == len(caff_in) == len(caff_out),
               f"entry-10 size in={sz_in} out={sz_out} (CAFF len {len(caff_in)}->{len(caff_out)})")
    return ok


# ---------------------------------------------------------------------------
# --allow-grow: in-place CAFF growth inside the PKG entry-slot padding
# ---------------------------------------------------------------------------

def grow_inject(pkg_path, output_path, blob, start, size):
    """Inject same-size blob, allowing the compressed s0 stream to grow into the
    entry-slot padding. Returns (delta, new_caff_len, old_s0cs, new_s0cs, info).

    Rules:
      - zlib level 6-7 only; pick the smaller of L6/L7 for s0 (L7 wins ties)
      - sect[0].compressedSize += delta; sect[1].offset += delta ONLY if the
        vanilla field equals the old s0 compressed size (brief's stated
        invariant); otherwise the field is unused (vanilla stores 0 and both
        CaffV36 and the game compute section positions SEQUENTIALLY) and is
        left untouched
      - FI table, reloc table, all other section-entry fields, and the header
        comp-size spans (stc @+0x60, rtc @+0x74) NEVER change
      - reloc table copied VERBATIM (compressed bytes) — content unchanged
      - sect table recompressed (content changed) and zero-padded to span stc
      - new CAFF end must be <= NEXT_ENTRY_START - 4 (room for FF000000
        terminator) — hard STOP otherwise
      - PKG written in place: only entry[10].size changes in the entry table;
        bytes [NEXT_ENTRY_START, EOF) byte-identical to vanilla
    """
    pkg_in = open(pkg_path, "rb").read()
    entries = parse_pkg_header(pkg_in)
    if entries is None or ENTRY >= len(entries):
        sys.exit("FAIL: invalid PKG / entry index")
    eid, off, sz = entries[ENTRY]
    caff = bytes(pkg_in[off:off + sz])
    hs = struct.unpack_from(">I", caff, 0x14)[0]
    stc = struct.unpack_from(">I", caff, 0x60)[0]
    rtc = struct.unpack_from(">I", caff, 0x74)[0]
    nst = caff[0x49]                      # num section entries (NOT header+0x20)
    names_len = struct.unpack_from(">I", caff, 0x4C)[0]
    fi_tbl_off = nst * 0x21 + names_len   # FI table inside decompressed sect table
    ds = hs + stc + rtc

    st = bytearray(zlib.decompress(bytes(caff[hs:hs + stc])))
    s0cs = struct.unpack_from(">I", st, 29)[0]
    s0 = bytearray(zlib.decompress(bytes(caff[ds:ds + s0cs])))
    s1cs = struct.unpack_from(">I", st, 0x21 + 0x1D)[0] if nst >= 2 else 0
    if start + size > len(s0):
        sys.exit(f"FAIL: asset [{start}..{start+size}) exceeds s0 ({len(s0)})")

    s0[start:start + size] = blob

    # Recompress s0 at L6 and L7, take the smaller (tie -> L7). NEVER L8/L9.
    c6 = zlib.compress(bytes(s0), 6)
    c7 = zlib.compress(bytes(s0), 7)
    new_s0 = c7 if len(c7) <= len(c6) else c6
    used = 7 if new_s0 is c7 else 6
    delta = len(new_s0) - s0cs
    print(f"  s0 recompressed: {s0cs} -> {len(new_s0)} (L6={len(c6)} L7={len(c7)}, "
          f"level {used}, delta {delta:+d})")
    if delta <= 0:
        print("  NOTE: compressed s0 fits the original slot; grow path still used "
              "(stream written at true length, header sizes follow actual output).")

    # Update section entry 0 compressedSize (+0x1D) and (conditionally) entry 1 offset (+0x11)
    struct.pack_into(">I", st, 29, s0cs + delta)
    off1_note = "n/a"
    if nst >= 2:
        old_off1 = struct.unpack_from(">I", st, 0x21 + 0x11)[0]
        if old_off1 == s0cs:
            struct.pack_into(">I", st, 0x21 + 0x11, old_off1 + delta)
            off1_note = f"{old_off1} -> {old_off1 + delta} (matched old s0cs)"
        else:
            # Vanilla field is 0 (not s0cs): offsets are SEQUENTIAL, field unused.
            off1_note = (f"left at {old_off1} (vanilla field != old s0cs {s0cs}; "
                         f"section positions computed sequentially)")

    # Recompress section table (content changed). Must fit frozen span stc.
    st_cands = [(7, zlib.compress(bytes(st), 7)), (6, zlib.compress(bytes(st), 6))]
    st_cands.sort(key=lambda p: (len(p[1]), -p[0]))      # smaller first, tie -> L7
    if len(st_cands[0][1]) > stc:
        sys.exit(f"FAIL: recompressed section table {len(st_cands[0][1])} > frozen span {stc}")
    st_comp = st_cands[0][1]
    st_padded = st_comp + b"\x00" * (stc - len(st_comp))
    print(f"  sect table recompressed: {stc} span <- {len(st_comp)} B stream (level {st_cands[0][0]})")

    # Reloc table: content unchanged -> copy compressed bytes verbatim.
    reloc = bytes(caff[hs + stc:hs + stc + rtc])

    # s1 stream copied verbatim (decompressed s1 unchanged, compressed size unchanged).
    s1 = bytes(caff[ds + s0cs:ds + s0cs + s1cs])

    # Header: only CRC (0x18) recomputed; stc @+0x60 / rtc @+0x74 stay frozen.
    hdr = bytearray(caff[:hs])
    struct.pack_into(">I", hdr, 0x18, 0)
    crc = checksum32(bytes(hdr))
    struct.pack_into(">I", hdr, 0x18, crc)

    new_caff = bytes(hdr) + bytes(st_padded) + reloc + bytes(new_s0) + s1
    new_end = off + len(new_caff)
    print(f"  CAFF: {len(caff)} -> {len(new_caff)} B (end 0x{off+len(caff):X} -> 0x{new_end:X}); "
          f"sect[1].offset {off1_note}")
    if new_end + len(TERM) > NEXT_ENTRY_START:
        sys.exit(f"FAIL: grown CAFF end 0x{new_end:X} + terminator exceeds next-entry "
                 f"start 0x{NEXT_ENTRY_START:X} — hard STOP (no entry shifts allowed)")
    if bytes(pkg_in[off + sz:off + sz + 4]) != TERM:
        sys.exit(f"FAIL: vanilla terminator not at old CAFF end 0x{off+sz:X} — geometry "
                 f"assumption broken; refusing in-place write")

    # Build output PKG: vanilla bytes, ONLY entry[10].size updated.
    out = bytearray(pkg_in)
    struct.pack_into(">I", out, 8 + ENTRY * 12 + 8, len(new_caff))
    out[off:off + len(new_caff)] = new_caff
    out[new_end:new_end + 4] = TERM
    # [new_end+4, 0x136000) is already vanilla zero pad; assert, don't touch.
    if any(out[new_end + 4:NEXT_ENTRY_START]):
        sys.exit("FAIL: pad region after new terminator is not zero in output")
    open(output_path, "wb").write(bytes(out))
    info = dict(off=off, old_sz=sz, new_sz=len(new_caff), delta=delta,
                s0cs=s0cs, new_s0cs=len(new_s0), stc=stc, rtc=rtc, hs=hs,
                nst=nst, fi_tbl_off=fi_tbl_off, s1cs=s1cs, new_end=new_end,
                crc=crc)
    return delta, len(new_caff), s0cs, len(new_s0), info


def verify_grow(pkg_in, pkg_out, blob, start, size, info):
    """Gates (a)-(l) for the --allow-grow path."""
    print("\nBuilt-in verification gates (--allow-grow):")
    ok = True
    off, old_sz, new_sz, delta = info["off"], info["old_sz"], info["new_sz"], info["delta"]
    hs, stc, rtc, nst = info["hs"], info["stc"], info["rtc"], info["nst"]
    fi_off = info["fi_tbl_off"]
    s0cs, s0cs_n, s1cs = info["s0cs"], info["new_s0cs"], info["s1cs"]

    caff_in = bytes(pkg_in[off:off + old_sz])
    off_out = struct.unpack_from(">I", pkg_out, 8 + ENTRY * 12 + 4)[0]
    sz_out = struct.unpack_from(">I", pkg_out, 8 + ENTRY * 12 + 8)[0]
    caff_out = bytes(pkg_out[off_out:off_out + sz_out])
    hs_o = struct.unpack_from(">I", caff_out, 0x14)[0]
    stc_o = struct.unpack_from(">I", caff_out, 0x60)[0]
    rtc_o = struct.unpack_from(">I", caff_out, 0x74)[0]
    ds_o = hs_o + stc_o + rtc_o

    st_i = zlib.decompress(bytes(caff_in[hs:hs + stc]))
    st_o = zlib.decompress(bytes(caff_out[hs_o:hs_o + stc_o]))
    s0_i = zlib.decompress(bytes(caff_in[hs + stc + rtc:hs + stc + rtc + s0cs]))
    s0_o = zlib.decompress(bytes(caff_out[ds_o:ds_o + s0cs_n]))
    reloc_i = bytes(caff_in[hs + stc:hs + stc + rtc])
    reloc_o = bytes(caff_out[hs_o + stc_o:hs_o + stc_o + rtc_o])
    s1_i = bytes(caff_in[hs + stc + rtc + s0cs:hs + stc + rtc + s0cs + s1cs])
    s1_o = bytes(caff_out[ds_o + s0cs_n:ds_o + s0cs_n + s1cs])

    re_ext = s0_o[start:start + size]
    ok &= gate("a", re_ext == blob,
               f"re-extracted blob == input blob: {re_ext == blob} "
               f"(sha256 {__import__('hashlib').sha256(re_ext).hexdigest()[:16]}...)")
    outside = [i for i, (x, y) in enumerate(zip(s0_i, s0_o)) if x != y
               if not (start <= i < start + size)]
    in_count = sum(1 for i, (x, y) in enumerate(zip(s0_i, s0_o)) if x != y
                   if start <= i < start + size)
    ok &= gate("b", len(s0_i) == len(s0_o) and not outside,
               f"s0 decomp len {len(s0_i)}->{len(s0_o)}; diffs: {in_count} inside "
               f"asset [{start}..{start+size}), {len(outside)} outside")

    ok &= gate("c", st_i[fi_off:] == st_o[fi_off:],
               f"file-info table (decompressed, from +{fi_off}) byte-identical: "
               f"{st_i[fi_off:] == st_o[fi_off:]}")

    hdr_diff = [i for i, (x, y) in enumerate(zip(caff_in[:hs], caff_out[:hs_o])) if x != y]
    ok &= gate("d", hs == hs_o and set(hdr_diff) <= {0x18, 0x19, 0x1A, 0x1B},
               f"CAFF header diffs {['0x%X' % i for i in hdr_diff]} subset of CRC 0x18-0x1B; "
               f"header size {hs}->{hs_o}")

    # Section-entry field diffs: only sect[0].compressedSize (+0x1D, bytes 29-32)
    # and possibly sect[1].offset (+0x11 of entry 1, bytes 44-47) may differ.
    allowed = set(range(29, 33)) | set(range(0x21 + 0x11, 0x21 + 0x11 + 4))
    sed = [i for i, (x, y) in enumerate(zip(st_i[:nst * 0x21], st_o[:nst * 0x21])) if x != y]
    ok &= gate("e", set(sed) <= allowed
               and struct.unpack_from(">I", st_o, 29)[0] == s0cs + delta
               and struct.unpack_from(">I", st_i, 29)[0] == s0cs,
               f"section-entry diffs {['+%d' % i for i in sed] or '(none)'} subset of "
               f"{{+29..32 sect0.compSize, +44..47 sect1.offset}}; "
               f"sect0.compSize {s0cs}->{struct.unpack_from('>I', st_o, 29)[0]} "
               f"(delta {delta:+d}); sect1.offset "
               f"{struct.unpack_from('>I', st_i, 0x21+0x11)[0]}->"
               f"{struct.unpack_from('>I', st_o, 0x21+0x11)[0]}")

    # (f) entry table: ONLY entry[10].size differs (offsets unchanged, no shifts)
    n_ent = struct.unpack_from(">I", pkg_in, 4)[0]
    ent_tbl = 8 + n_ent * 12
    tbl_diffs = [i for i in range(ent_tbl) if pkg_in[i] != pkg_out[i]]
    bad = [i for i in tbl_diffs
           if not (8 + ENTRY * 12 + 8 <= i < 8 + ENTRY * 12 + 12)]
    ok &= gate("f", not bad and struct.unpack_from(">I", pkg_out, 8 + ENTRY * 12 + 8)[0]
               == old_sz + delta and struct.unpack_from(">I", pkg_out, 8 + ENTRY * 12 + 4)[0] == off,
               f"entry table: only entry[{ENTRY}].size differs "
               f"({old_sz}->{struct.unpack_from('>I', pkg_out, 8+ENTRY*12+8)[0]}, "
               f"expected {old_sz+delta}); other diffs: {bad or '(none)'}; "
               f"entry[{ENTRY}].offset unchanged ({off})")

    # Bytes between PKG entry table and CAFF start must be identical (the entry
    # table itself is allowed the single size-field change covered by gate f).
    n_ent = struct.unpack_from(">I", pkg_in, 4)[0]
    tbl_end = 8 + n_ent * 12
    ok &= gate("g", pkg_in[tbl_end:off] == pkg_out[tbl_end:off_out] and off == off_out,
               f"PKG bytes [entry-table-end..CAFF-start) byte-identical and CAFF "
               f"start unmoved: {pkg_in[tbl_end:off] == pkg_out[tbl_end:off_out]} "
               f"(CAFF @ 0x{off:X})")

    calc_crc = crc_of(caff_out, hs_o)
    ok &= gate("h", calc_crc == struct.unpack_from(">I", caff_out, 0x18)[0],
               f"CAFF CRC validates: stored=0x{struct.unpack_from('>I', caff_out, 0x18)[0]:08X} "
               f"calc=0x{calc_crc:08X}")

    ok &= gate("i", sz_out == len(caff_out) == old_sz + delta,
               f"entry-10 size {old_sz} -> {sz_out} (CAFF len {len(caff_out)}, "
               f"delta {delta:+d})")

    # (j) tail: [0x136000, EOF) byte-identical to vanilla
    ok &= gate("j", pkg_in[NEXT_ENTRY_START:] == pkg_out[NEXT_ENTRY_START:],
               f"PKG bytes [0x{NEXT_ENTRY_START:X}, EOF) byte-identical: "
               f"{pkg_in[NEXT_ENTRY_START:] == pkg_out[NEXT_ENTRY_START:]} "
               f"({len(pkg_in) - NEXT_ENTRY_START} B)")

    # (k) s1 data at its new offset equals vanilla s1 compressed bytes
    ok &= gate("k", s1_o == s1_i and len(s1_o) == s1cs,
               f"s1 stream at new offset equals vanilla s1 ({s1cs} B): {s1_o == s1_i}")

    # (l) CAFF fully decompresses, data_start math consistent. The sect-table
    # stream may fill the frozen span exactly (no zero pad) — that's fine; what
    # matters is the span itself never changes and every stream decompresses.
    try:
        full_ok = (len(st_o) == len(st_i) == 8504
                   and len(s0_o) == len(s0_i)
                   and len(zlib.decompress(bytes(caff_out[ds_o + s0cs_n:ds_o + s0cs_n + s1cs]))) == 8192
                   and ds_o == hs_o + 2069 + 9143 == hs + stc + rtc
                   and stc_o == stc and rtc_o == rtc)
    except Exception as e:
        full_ok = False
        print(f"    decompress error: {e}")
    ok &= gate("l", full_ok,
               f"full decompress OK (st {len(st_o)}, s0 {len(s0_o)}, s1 8192); "
               f"data_start {ds_o} == header {hs_o}+2069+9143; spans frozen "
               f"stc={stc_o} rtc={rtc_o}")

    # Terminator + pad geometry
    term_ok = (bytes(pkg_out[info['new_end']:info['new_end'] + 4]) == TERM
               and not any(pkg_out[info['new_end'] + 4:NEXT_ENTRY_START]))
    ok &= gate("m", term_ok,
               f"FF000000 terminator at new CAFF end 0x{info['new_end']:X}, "
               f"zero pad to 0x{NEXT_ENTRY_START:X}: {term_ok}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--blob", required=True, help="same-size animation blob (.bin)")
    ap.add_argument("--pkg", required=True, help="input PKG")
    ap.add_argument("--output", required=True, help="output PKG")
    ap.add_argument("--label", default=DEFAULT_LABEL,
                    help=f"file-info asset label (default: {DEFAULT_LABEL})")
    ap.add_argument("--size", type=int, default=DEFAULT_SIZE,
                    help=f"required blob/asset size in bytes (default: {DEFAULT_SIZE})")
    ap.add_argument("--allow-grow", action="store_true",
                    help="allow the compressed section-0 stream to grow into the PKG "
                         "entry-slot padding (in-place CAFF growth; next entry must "
                         "stay put). Default off = same-size inject_into_pkg path.")
    args = ap.parse_args()

    blob = open(args.blob, "rb").read()
    if len(blob) != args.size:
        sys.exit(f"FAIL: blob is {len(blob)} bytes, need exactly {args.size} "
                 f"(same-size rule for label '{args.label}')")
    pkg_in = open(args.pkg, "rb").read()
    caff = extract_caff_from_pkg(pkg_in, ENTRY)
    if not caff:
        sys.exit(f"FAIL: cannot extract CAFF from PKG entry {ENTRY}")
    # Match the label proper (before the ",id,version" suffix); substring match would
    # also hit e.g. '..._walkbackwards' when looking for '..._walk'.
    matches = [f for f in CaffV36(caff).file_infos
               if f["label"].split(",")[0] == args.label]
    if not matches:
        sys.exit(f"FAIL: no file-info matching '{args.label}' in CAFF entry {ENTRY}")
    if len(matches) > 1:
        sys.exit(f"FAIL: label '{args.label}' is ambiguous in CAFF entry {ENTRY}: "
                 f"{len(matches)} matches")
    fi = matches[0]
    if fi["size"] != args.size:
        sys.exit(f"FAIL: asset '{args.label}' size {fi['size']} != {args.size}")
    print(f"PKG entry {ENTRY}: CAFF {len(caff)} B; asset '{fi['label']}' "
          f"start={fi['start']} size={fi['size']}; blob {len(blob)} B")

    if args.allow_grow:
        print("--allow-grow: in-place CAFF growth mode (entry-slot padding)")
        delta, new_caff_len, s0cs, s0cs_n, info = grow_inject(
            args.pkg, args.output, blob, fi["start"], fi["size"])
        pkg_out = open(args.output, "rb").read()
        if not verify_grow(pkg_in, pkg_out, blob, fi["start"], fi["size"], info):
            sys.exit("RESULT [FAIL] — see gates above")
        print(f"RESULT [PASS] — all gates (a)-(m) passed; "
              f"CAFF {info['old_sz']} -> {new_caff_len} (delta {delta:+d}), "
              f"s0 stream {s0cs} -> {s0cs_n}")
        return

    inject_into_pkg(args.pkg, ENTRY, blob, args.output, fi["start"], fi["size"])
    pkg_out = open(args.output, "rb").read()
    if not verify(pkg_in, pkg_out, blob, fi["start"], fi["size"]):
        sys.exit("RESULT [FAIL] — see gates above")
    print("RESULT [PASS] — all gates (a)-(i) passed")


if __name__ == "__main__":
    main()
