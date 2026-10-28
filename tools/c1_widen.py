"""
c1_widen.py - Phase C1 (OFFLINE ONLY: no game, no deploy, no PKG).
Hypothesis: widening ONE existing FULL slot's count VALUE by +1 (num_bits=count+1
-> +1 bit/frame) with ZERO structural change - count table keeps 60 bytes with pad
00 at idx 59, nFULL 59, modes/bases unchanged, bpf 96, trailer 0xFC0, len 6944 -
decodes identically to the original (B2 crashed because its count INSERT evicted
the pad byte; C1 never touches table structure). Pattern adapted from an earlier
internal widen prototype: v4 template-path encode (structure tuple carries the
+1 count) + rebuild-from-
tables cross-check; final blob = original + tuned count byte + bitstream re-emitted
at the SAME 96-byte stride (positions >= content end copy through ORIGINAL bits).
encode_quat_v4 = READ-ONLY; do not use the earlier bpf-growing widen prototype
(grows bpf - forbidden).
Output: c1_widen_walk.bin + SHA256. Verdict: C1_OFFLINE_PASS|C1_BLOCKED|C1_GATE_FAIL
"""
import os, sys, struct, hashlib, argparse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import encode_quat_v4 as q4

DEFAULT_INPUT  = os.path.join(HERE, "orig_walk_anim.bin")
DEFAULT_OUTPUT = "c1_widen_walk.bin"
NCH, NFR = 27, 36
TRAILER, BSB = 0xFC0, 576
STRIDE, BITS_FR = 96, 768
TAIL_P = (765, 766, 767)

def be16(d, o): return struct.unpack_from(">H", d, o)[0]
def be32(d, o): return struct.unpack_from(">I", d, o)[0]

def fields(d):
    hv = q4.QUAT_HEADER_OFFSET
    f = dict(mode_off=be32(d, hv + 0x0C), base_off=be32(d, hv + 0x10),
             bitcount_off=be32(d, hv + 0x14), sizeA=be16(d, hv + 0x18),
             sizeB=be16(d, hv + 0x1A), bpf=be16(d, hv + 0x1C),
             scale_off=be32(d, hv + 0x04), nch=be16(d, hv + 0x22),
             nfr=be16(d, hv + 0x24), mode_flag=d[hv + 0x26])
    f["bitstream_base"] = f["base_off"] + f["sizeA"] + f["sizeB"]
    f["trailer_start"] = f["bitstream_base"] + f["bpf"] * f["nfr"]
    return f

def bit_at(data, frame_base, p):
    """Bit p of a frame (LSB-first within 32-bit BE words, v4 BitWriter order)."""
    return (data[frame_base + (p >> 5) * 4 + 3 - (p & 31) // 8] >> (p & 7)) & 1

def main(argv=None):
    ap = argparse.ArgumentParser(description="C1 count-value widen (offline, walk blob)")
    ap.add_argument("--input", default=DEFAULT_INPUT,
                    help="original walk animation blob "
                         "(default: orig_walk_anim.bin next to this script)")
    ap.add_argument("--output", default=DEFAULT_OUTPUT,
                    help="output blob (default: ./c1_widen_walk.bin in cwd)")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.input):
        sys.exit(f"file not found: {args.input} — place orig_walk_anim.bin next "
                 f"to this script or pass --input")
    orig = open(args.input, "rb").read()
    F = fields(orig)
    if F["mode_flag"] != 1 or len(orig) != 6944:
        print(f"BLOCKER: expected mode_flag=1 + len 6944, got {F['mode_flag']}/{len(orig)}")
        print("VERDICT: C1_BLOCKED"); return 2
    dec = q4.decode_anim_game_formula(orig)
    st, order, modes, obases, ocounts = q4._parse_orig_structure(orig, NCH)
    # ---- STEP 1: choose the LAST FULL quat slot ------------------------
    print("== STEP 1: CHOICE (LAST FULL quat slot, maximal order) ==")
    fullq = [(i, ch, ci) for i, (ch, ci) in enumerate(order) if ci < 3 and st[(ch, ci)] == "FULL"]
    pos, ch_c, ci_c = fullq[-1]
    ci_idx = sum(1 for k in order[:pos] if st[k] == "FULL")
    nfull = sum(1 for v in st.values() if v == "FULL")
    after_full = [k for k in order[pos + 1:] if st[k] == "FULL"]
    print(f"  census: FULL quat={len(fullq)}/{NCH * 3} nFULL(all)={nfull} slots={len(order)} "
          f"sizeB={F['sizeB']} sizeA={F['sizeA']}")
    print(f"  chosen: ch{ch_c}/c{ci_c} order_pos={pos}/{len(order) - 1} count_idx={ci_idx} "
          f"count {ocounts[ci_idx]}->{ocounts[ci_idx] + 1} width {ocounts[ci_idx] + 1}->{ocounts[ci_idx] + 2}")
    print(f"  slots after chosen: {len(order) - 1 - pos} -> "
          f"{'NOTHING AFTER (no shift past chosen)' if not after_full else 'FULL follows: ' + str(after_full)}")
    print(f"  count table {F['sizeB']}B pad00@59={ocounts[59] == 0} tail53-59: "
          + " ".join(f"{c:02X}" for c in ocounts[53:]))
    # ---- STEP 2: original frame tail bits 765..767 ---------------------
    print("== STEP 2: ORIGINAL FRAME TAIL BITS (positions 765,766,767) ==")
    tails = [[bit_at(orig, BSB + STRIDE * f, p) for p in TAIL_P] for f in range(NFR)]
    for f in range(NFR):
        print(f"  frame {f:2d}: {tails[f]}")
    nz = [f for f, t in enumerate(tails) if any(t)]
    uniform = len({tuple(t) for t in tails}) == 1
    print(f"  uniform across frames: {uniform}; all-zero: {not nz}; "
          f"frames with non-zero tail bits: {nz if nz else 'none'}")
    # ---- STEP 3: v4 template re-encode (structure tuple carries +1) ----
    print("== STEP 3: V4 TEMPLATE RE-ENCODE (tuple counts with chosen +1) ==")
    def dval(f, ch, ci):
        t = dec["frames"][f][ch]
        return t[ci] if ci < 3 else t[ci + 1]
    delta = {ch: [tuple(dval(f, ch, ci) for ci in range(9)) for f in range(NFR)] for ch in range(NCH)}
    scales = [struct.unpack_from(">f", orig, F["scale_off"] + 4 * i)[0] for i in range(9)]
    cnt2 = list(ocounts); cnt2[ci_idx] = ocounts[ci_idx] + 1
    enc = q4._encode_with_template_structure(delta, NCH, NFR, scales, orig,
                                             (st, order, bytes(modes), obases, tuple(cnt2)))
    e_ct = [i for i in range(F["sizeB"]) if enc[F["bitcount_off"] + i] != ocounts[i]]
    e_bd = sum(1 for i in range(F["base_off"], F["base_off"] + F["sizeA"]) if enc[i] != orig[i])
    e_md = sum(1 for i in range(F["mode_off"], F["base_off"]) if enc[i] != orig[i])
    e_hq = sum(1 for i in range(F["bitcount_off"]) if enc[i] != orig[i])
    e_bs = sum(1 for a, b in zip(enc[BSB:BSB + STRIDE * NFR], orig[BSB:BSB + STRIDE * NFR]) if a != b)
    print(f"  encoder blob len={len(enc)}; parity vs orig: [0,516)={e_hq} modes={e_md} "
          f"bases={e_bd} counts={e_ct} bitstream={e_bs}/3456")
    print("  API NOTE: encoder recomputes widths from data span (tuple count values are")
    print("  inert at the encoder stage - verifiable above: enc counts == orig despite +1).")
    print("  Count surgery is therefore applied at the rebuild-from-tables stage, the")
    print("  exact template-structure pattern: shipped count byte comes from tuple cnt2,")
    print("  bitstream is re-emitted from the shipped count table, encoder = cross-check.")
    # ---- STEP 4: frame emission at SAME 96-byte stride -----------------
    print("== STEP 4: FRAME EMISSION (stride 96, widths from tuned count table) ==")
    widths, fulls, c_i = {}, [], 0
    for i, (ch, ci) in enumerate(order):
        if st[(ch, ci)] == "FULL":
            widths[i] = cnt2[c_i] + 1
            fulls.append((ch, ci, widths[i], obases[i]))   # obases index == order pos
            c_i += 1
    content_bits = sum(w for _, _, w, _ in fulls)
    print(f"  per-frame content bits after: min={content_bits} max={content_bits} (gate == 766)")
    bw, blockers = q4.BitWriter(), []
    for f in range(NFR):
        bw.pad_to_bits(f * BITS_FR)
        for ch, ci, w, base in fulls:
            res = int(round(dval(f, ch, ci) / scales[ci])) - base
            if not 0 <= res < (1 << w):
                blockers.append(f"residual OOR f{f} ch{ch}/c{ci}: {res} w{w}")
            bw.write(max(0, min(res, (1 << w) - 1)), w)
        for p in range(content_bits, BITS_FR):        # copy-through ORIGINAL tail bits
            bw.write(bit_at(orig, BSB + STRIDE * f, p), 1)
    buf = bw.to_bytes()
    if blockers or len(buf) != STRIDE * NFR:
        print("BLOCKER: " + "; ".join(blockers[:5]) or f"buffer {len(buf)} != {STRIDE * NFR}")
        print("VERDICT: C1_BLOCKED"); return 2
    print(f"  chosen slot extension bit = original tail position {content_bits - 1} "
          f"(old pad slot); copy-through covers positions {content_bits}..{BITS_FR - 1}")
    print("  per-frame tail bits 765,766,767 (orig vs new):")
    for f in range(NFR):
        ot = [bit_at(orig, BSB + STRIDE * f, p) for p in TAIL_P]
        nt = [bit_at(buf, STRIDE * f, p) for p in TAIL_P]
        print(f"    frame {f:2d}: orig={ot} new={nt} {'same' if ot == nt else 'DIFF'}")
    xcheck = sum(1 for a, b in zip(buf, enc[BSB:BSB + len(buf)]) if a == b)
    print(f"  rebuild-from-tables vs encoder buffer cross-check: {xcheck}/{len(buf)} bytes agree")
    new = bytearray(orig)
    new[F["bitcount_off"] + ci_idx] = cnt2[ci_idx]
    new[BSB:BSB + len(buf)] = buf
    new = bytes(new)
    # ---- STEP 5: offline gates (plan 5.1 + structure) ------------------
    print("== STEP 5: OFFLINE VERIFY (plan 5.1 + structure gates) ==")
    d2 = q4.decode_anim_game_formula(new)
    bad = sum(1 for fr in d2["frames"] for t in fr if abs(sum(c * c for c in t[:4]) - 1.0) > 1e-4)
    qd = pd = sd = 0.0
    for f in range(NFR):
        for ch in range(NCH):
            a, b = dec["frames"][f][ch], d2["frames"][f][ch]
            qd = max(qd, *(abs(a[i] - b[i]) for i in range(4)))
            pd = max(pd, *(abs(a[i] - b[i]) for i in range(4, 7)))
            sd = max(sd, *(abs(a[i] - b[i]) for i in range(7, 10)))
    slot_d = max(abs(dec["frames"][f][ch_c][ci_c] - d2["frames"][f][ch_c][ci_c]) for f in range(NFR))
    fails = []
    print(f"  invalid quats: {bad} (gate == 0) -> {'PASS' if bad == 0 else 'FAIL'}")
    print(f"  max quat diff: {qd:.3e} (gate < 1e-4) -> {'PASS' if qd < 1e-4 else 'FAIL'}")
    print(f"  chosen slot ch{ch_c}/c{ci_c} OWN max quat diff: {slot_d:.3e} (printed separately)")
    print(f"  max pos diff: {pd:.3e} (gate == 0.0) -> {'PASS' if pd == 0.0 else 'FAIL'}")
    print(f"  max scale diff: {sd:.3e} (gate == 0.0) -> {'PASS' if sd == 0.0 else 'FAIL'}")
    F2 = fields(new)
    got = dict(F2); got["len"] = len(new)
    exp = dict(len=6944, bpf=96, trailer_start=0xFC0, sizeA=244, sizeB=60, mode_off=236,
               base_off=272, bitcount_off=516, bitstream_base=576)
    for k, v in exp.items():
        if got[k] != v: fails.append(f"{k} {got[k]} != {v}")
    print(f"  structure: len={got['len']} bpf={got['bpf']} trailer=0x{got['trailer_start']:X} "
          f"sizeA={got['sizeA']} sizeB={got['sizeB']} mode={got['mode_off']} base={got['base_off']} "
          f"bitcount={got['bitcount_off']} bitstream_base={got['bitstream_base']}")
    hq = sum(1 for i in range(516) if new[i] != orig[i])
    md = [i for i in range(236, 272) if new[i] != orig[i]]
    bd = sum(1 for i in range(272, 516) if new[i] != orig[i])
    ct = [(i, ocounts[i], new[516 + i]) for i in range(F["sizeB"]) if new[516 + i] != ocounts[i]]
    tr = sum(1 for i in range(TRAILER, len(orig)) if new[i] != orig[i])
    bsd = sum(1 for a, b in zip(new[BSB:BSB + STRIDE * NFR], orig[BSB:BSB + STRIDE * NFR]) if a != b)
    pf = [sum(1 for a, b in zip(new[BSB + STRIDE * f:BSB + STRIDE * f + STRIDE],
                                orig[BSB + STRIDE * f:BSB + STRIDE * f + STRIDE]) if a != b)
          for f in range(NFR)]
    nfull_new = sum(1 for v in q4._parse_orig_structure(new, NCH)[0].values() if v == "FULL")
    print(f"  census [0,516): {hq} (gate 0); modes: {md if md else 'none'} (gate none); "
          f"bases: {bd} (gate 0); nFULL {nfull}->{nfull_new} (gate 59)")
    print(f"  count table: {len(ct)}/60 differ (gate == 1); (idx,old,new)={ct}; "
          f"idx59={new[516 + 59]} (gate 00)")
    print(f"  trailer [0xFC0,EOF): {tr} (gate 0); bitstream: {bsd} bytes differ (print); "
          f"per-frame byte diffs: {pf}")
    if hq: fails.append(f"header+QBS[0,516) {hq} != 0")
    if md: fails.append(f"mode diffs at {md}")
    if bd: fails.append(f"bases diffs {bd} != 0")
    if len(ct) != 1 or (ct and ct[0][0] != ci_idx):
        fails.append(f"count diffs {ct} != exactly idx {ci_idx}")
    elif ct[0][2] != ct[0][1] + 1:
        fails.append(f"count not +1: {ct[0][1]}->{ct[0][2]}")
    if new[516 + 59] != 0: fails.append("pad 00 at idx59 evicted")
    if tr: fails.append(f"trailer {tr} != 0")
    if nfull_new != 59: fails.append(f"nFULL {nfull_new} != 59")
    if content_bits != 766: fails.append(f"content bits {content_bits} != 766")
    if bad or qd >= 1e-4 or pd != 0.0 or sd != 0.0: fails.append("decode gate (5.1)")
    if not nz and xcheck != len(buf): fails.append(f"xcheck {xcheck}/{len(buf)} (tail all-zero)")
    # ---- STEP 6: output + verdict -------------------------------------
    open(args.output, "wb").write(new)
    print(f"  output: {args.output}")
    print(f"  sha256: {hashlib.sha256(new).hexdigest().upper()}")
    if not fails:
        print("VERDICT: C1_OFFLINE_PASS"); return 0
    print("VERDICT: C1_GATE_FAIL -> " + "; ".join(fails)); return 1

if __name__ == "__main__":
    sys.exit(main())
