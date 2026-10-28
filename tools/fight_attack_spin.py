"""
fight_attack_spin.py — OFFLINE ONLY: no game, no deploy, no PKG edits.
Encode a clean 360 deg death-roll about encoder Z into crocodile fight_attack ch0
(BASE root): delta_new(t) = R_z(th(t)) PRE-multiplied, th(t)=2pi*t/167, w>=0 forced.
quat_mult(q1,q2)=Hamilton q1*q2 (q1 outer) -> q4.quat_mult(rz, d) = R_z pre-mult,
axis constant in the bind frame; th(0)=0 loop-safe with vanilla f166~=f0.

encode_quat_v4 template path has two fight_attack quirks (walk-sized templates
never hit them): (1) it stitches the template trailer at hardcoded 0xFC0, wiping
our bitstream for frames >= ~38 (fight_attack bitstream runs 528..16226);
(2) it emits one count-table pad byte while orig sizeB carries two, shifting the
bitstream to 527 while the decoder reads 528. Fix: C1-style rebuild-from-tables —
re-emit the bitstream at the ORIG bitstream_base with orig pad bytes preserved.
Widen path: if residual clamping appears, +N count VALUES on the clamped ch0
FULL quat slots (no inserts, bpf/sizeB unchanged, content bits <= budget),
N=1..6, else STOP. encode_quat_v4 = import only. Output: fight_attack_spin_z.bin.
"""
import os, sys, struct, math, hashlib, argparse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import encode_quat_v4 as q4

NCH, NFR = 27, 167
DEFAULT_INPUT  = os.path.join(HERE, "orig_fight_attack.bin")
DEFAULT_OUTPUT = "fight_attack_spin_z.bin"

def be16(d, o): return struct.unpack_from(">H", d, o)[0]
def be32(d, o): return struct.unpack_from(">I", d, o)[0]
def fields(d):
    hv = q4.QUAT_HEADER_OFFSET
    f = dict(len=len(d), scale_off=be32(d, hv + 4), default_off=be32(d, hv + 8),
             mode_off=be32(d, hv + 0xC), base_off=be32(d, hv + 0x10),
             bitcount_off=be32(d, hv + 0x14), sizeA=be16(d, hv + 0x18),
             sizeB=be16(d, hv + 0x1A), bpf=be16(d, hv + 0x1C),
             nch=be16(d, hv + 0x22), nfr=be16(d, hv + 0x24), mode_flag=d[hv + 0x26])
    f["bitstream_base"] = f["base_off"] + f["sizeA"] + f["sizeB"]
    f["trailer_start"] = f["bitstream_base"] + f["bpf"] * f["nfr"]
    return f

def main(argv=None):
    ap = argparse.ArgumentParser(description="fight_attack 360 death-roll encoder (offline)")
    ap.add_argument("--input", default=DEFAULT_INPUT,
                    help="original fight_attack animation blob "
                         "(default: orig_fight_attack.bin next to this script)")
    ap.add_argument("--output", default=DEFAULT_OUTPUT,
                    help="output blob (default: ./fight_attack_spin_z.bin in cwd)")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.input):
        sys.exit(f"file not found: {args.input} — place orig_fight_attack.bin next "
                 f"to this script or pass --input")
    orig = open(args.input, "rb").read()
    Fo = fields(orig)
    if not (len(orig) == 19800 and Fo["nch"] == NCH and Fo["nfr"] == NFR and Fo["mode_flag"] == 1):
        print(f"BLOCKER: unexpected orig {len(orig)}/{Fo}"); print("VERDICT: SPIN_BLOCKED"); return 2
    dec = q4.decode_anim_game_formula(orig)
    st, order, modes, obases, ocounts = q4._parse_orig_structure(orig, NCH)
    nfull = sum(1 for v in st.values() if v == "FULL")
    budget = Fo["bpf"] * 8
    full_pos = {k: i for i, k in enumerate(order)}          # order pos
    count_idx = {}; ci = 0                                   # FULL-only count index
    for k in order:
        if st[k] == "FULL": count_idx[k] = ci; ci += 1
    ch0_full = [k for k in order if k[0] == 0 and st[k] == "FULL"]

    # ---- STEP 1: baseline ------------------------------------------------
    q0 = dec["frames"][0][0][:4]; q166 = dec["frames"][166][0][:4]
    dot0 = abs(sum(a * b for a, b in zip(q0, q166)))
    print(f"== STEP 1: BASELINE ==")
    print(f"  ch0 f0 quat (xyzw): {tuple(round(v, 6) for v in q0)}")
    print(f"  vanilla f166-vs-f0 loop delta: dot={dot0:.6f} angle={math.degrees(2*math.acos(min(1, dot0))):.3f} deg")

    # ---- STEP 2: compose R_z(th) PRE-multiplied onto ch0 delta -----------
    comp = []
    for f in range(NFR):
        th = 2.0 * math.pi * f / NFR
        rz = (0.0, 0.0, math.sin(th / 2.0), math.cos(th / 2.0))
        n = q4.quat_mult(rz, dec["frames"][f][0][:4])       # rz outer = pre-mult
        if n[3] < 0: n = (-n[0], -n[1], -n[2], -n[3])       # w>=0 (decoder reconstructs +w)
        comp.append(n)
    deltas = {0: [(n[0], n[1], n[2]) for n in comp]}        # 3-tuples: pos/scale restored from template

    # ---- STEP 3: encode via the proven encode_deltas call shape ----------
    print(f"== STEP 3: encode_deltas(deltas, {NCH}, {NFR}, scales=orig, template_header=orig) ==")
    enc = q4.encode_deltas(deltas, NCH, NFR, scales=dec["scales"], template_header=orig)
    if len(enc) != 19800:
        print(f"BLOCKER: encoder produced {len(enc)} bytes, expected 19800")
        print("VERDICT: SPIN_BLOCKED"); return 2
    Fe = fields(enc)
    if enc[Fo["mode_off"]:Fo["base_off"]] != orig[Fo["mode_off"]:Fo["base_off"]]:
        print("BLOCKER: encoder rewrote mode bytes"); print("VERDICT: SPIN_BLOCKED"); return 2
    k = round(struct.unpack_from(">f", enc, Fo["scale_off"])[0] / struct.unpack_from(">f", orig, Fo["scale_off"])[0])
    ecounts = list(enc[Fo["bitcount_off"]:Fo["bitcount_off"] + nfull])
    print(f"  enc len=19800 ok; quat scale_k={k}; ch0 counts " +
          ", ".join(f"c{c}:{ocounts[count_idx[(0,c)]]}->{ecounts[count_idx[(0,c)]]}" for _, c in ch0_full))

    # ---- STEP 4: rebuild-from-tables bitstream emission (C1 pattern) -----
    def val(ch, ci, f):
        if ch == 0 and ci < 3: return comp[f][ci]
        return q4._orig_component_values(orig, ch, ci, NFR)[f]
    def emit(tuned):
        """Emit bitstream from enc tables + our values; widths = tuned counts."""
        eff = [struct.unpack_from(">f", enc, Fo["scale_off"] + 4 * i)[0] for i in range(9)]
        bases = [struct.unpack_from(">i", enc, Fo["base_off"] + 4 * i)[0] for i in range(Fo["sizeA"] // 4)]
        bw, oor, content = q4.BitWriter(), [], 0
        for f in range(NFR):
            bw.pad_to_bits(f * budget)
            for k_ in order:
                if st[k_] != "FULL": continue
                ch, ci = k_
                w = tuned[k_] + 1
                res = int(round(val(ch, ci, f) / eff[ci])) - bases[full_pos[k_]]
                if not 0 <= res < (1 << w): oor.append((f, ch, ci, res, w))
                bw.write(max(0, min(res, (1 << w) - 1)), w)
            content = sum(tuned[k_] + 1 for k_ in order if st[k_] == "FULL")
        # FULL word-padded stream: the last frame's final word legitimately
        # extends past bpf*nfr — bytes [trailer_start, +2) are bitstream
        # storage (orig stores its last residual bits there too); the fixed
        # trailer ('BITSTREAM32' marker) starts at trailer_start+2.
        return bw.to_bytes(), oor, content
    tuned = {k_: ecounts[count_idx[k_]] for k_ in order if st[k_] == "FULL"}  # all FULL slots
    # encoder wrote its own bitstream at bc_off + nfull + 1 (one pad byte) — cross-check
    # only its INTACT region: the 0xFC0 trailer stitch wiped enc bytes past 0xFC0.
    B0, oor0, _ = emit(dict(tuned))
    enc_bsb = Fo["bitcount_off"] + nfull + 1
    xlen = min(len(B0), 0xFC0 - enc_bsb)
    xdiff = sum(1 for a, b in zip(B0[:xlen], enc[enc_bsb:enc_bsb + xlen]) if a != b)
    print(f"  emission cross-check vs encoder intact region [527,0xFC0): {xdiff}/{xlen} bytes differ (gate 0)")
    if xdiff:
        print("BLOCKER: rebuild does not replicate encoder bitstream"); print("VERDICT: SPIN_BLOCKED"); return 2

    # ---- STEP 5: widen loop (Gate A) + assemble final blob ---------------
    tuned = {k_: ecounts[count_idx[k_]] for k_ in order if st[k_] == "FULL"}
    d2 = final = None; N_used = 0; bad = -1; content = 0
    for attempt in range(7):
        B, oor, content = emit(tuned)
        if content > budget:
            print(f"BLOCKER: widen N={attempt} needs {content} bits > budget {budget} "
                  f"(bpf/sizeB growth forbidden)"); print("VERDICT: SPIN_BLOCKED"); return 2
        final = bytearray(enc)
        final[Fo["bitcount_off"] + nfull:Fo["bitcount_off"] + Fo["sizeB"]] = \
            orig[Fo["bitcount_off"] + nfull:Fo["bitcount_off"] + Fo["sizeB"]]   # preserve orig pad bytes
        for k_ in ch0_full:                                                     # ship tuned count VALUES
            final[Fo["bitcount_off"] + count_idx[k_]] = tuned[k_]
        final[Fo["bitstream_base"]:Fo["bitstream_base"] + len(B)] = B           # bitstream at ORIG 528
        final = bytes(final)
        d2 = q4.decode_anim_game_formula(final)
        bad = sum(1 for fr in d2["frames"] for t in fr if abs(math.sqrt(sum(c * c for c in t[:4])) - 1.0) > 0.05)
        if bad == 0: break
        slots = {(ch, ci) for (_, ch, ci, _, _) in oor}
        if not oor or any(s not in ch0_full for s in slots):
            print(f"BLOCKER: {bad} invalid quats, clamping outside ch0 quat FULL slots: {sorted(slots)[:6]}")
            print("VERDICT: SPIN_BLOCKED"); return 2
        for s in slots: tuned[s] += 1
        N_used += 1
        print(f"  GateA invalid={bad} -> widen N={N_used} on {sorted(slots)}, content={content}/{budget}")
    print(f"== widen: N={N_used} ({'N=0 sufficed' if N_used == 0 else 'clamping cleared'}), content bits {content}/{budget} ==")

    # ---- STEP 6: gates ---------------------------------------------------
    fails = []
    print(f"== STEP 6: GATES ==")
    print(f"  GATE A invalid quats (mag dev > 0.05, all 27x167): {bad} -> {'PASS' if bad == 0 else 'FAIL'}")
    if bad: fails.append(f"A:{bad}")
    # r' = new * inv(orig) == R_z(th) exactly (post-mult recovers the axis in
    # the DELTA frame; no conjugation bias). Unwrap +-360 for the w>=0 flip.
    angs, mindot, prev = [], 1.0, 0.0
    for f in range(NFR):
        r = q4.quat_mult(d2["frames"][f][0][:4], q4.quat_conj(dec["frames"][f][0][:4]))
        a = math.degrees(2.0 * math.atan2(r[2], r[3]))
        while a - prev > 180.0: a -= 360.0
        while a < prev - 180.0: a += 360.0
        angs.append(a); prev = a
        sn = math.sqrt(r[0] ** 2 + r[1] ** 2 + r[2] ** 2)
        if sn > 1e-3 and 20.0 < a < 340.0:
            # double cover: axis SIGN is quat-negation-dependent (w>=0 force
            # flips wherever composed w crosses 0, ~theta 160 here); only the
            # |axis.Z| magnitude is decodable — deviation = 1 - |dot|.
            mindot = min(mindot, abs(r[2]) / sn)
    exp83, exp166 = 360.0 * 83 / NFR, 360.0 * 166 / NFR
    viol = sum(1 for i in range(NFR - 1) if angs[i + 1] < angs[i] - 0.5)
    print(f"  GATE B: f83 angle {angs[83]:.2f} (expect {exp83:.2f}+/-5) "
          f"| final f166 {angs[166]:.2f} (expect {exp166:.2f}) | monotonic violations {viol} (expect 0)")
    print(f"           max axis deviation: 1-|dot|(Z)={1 - mindot:.4f} (min |dot| with Z {mindot:.4f})")
    if abs(angs[83] - exp83) > 5.0: fails.append(f"B:f83 {angs[83]:.1f}")
    if abs(angs[166] - exp166) > 5.0: fails.append(f"B:f166 {angs[166]:.1f}")
    if viol: fails.append(f"B:mono {viol}")
    maxd, worst = 0.0, None
    for f in range(NFR):
        for ch in range(1, NCH):
            dd = max(abs(dec["frames"][f][ch][i] - d2["frames"][f][ch][i]) for i in range(10))
            if dd > maxd: maxd, worst = dd, (f, ch)
    print(f"  GATE C non-authored ch1-26 max decode diff {maxd:.3e}"
          + (f" (worst f{worst[0]} ch{worst[1]})" if worst else "")
          + f", gate < 1e-4 -> {'PASS' if maxd < 1e-4 else 'FAIL'}")
    if maxd >= 1e-4: fails.append(f"C:{maxd:.2e}@ch{worst[1]}")
    Ff = fields(final); exp = {k: Fo[k] for k in Fo if k != "len"}
    sd = [k for k in exp if Ff[k] != exp[k]]
    ts = Fo["trailer_start"]                                          # fixed trailer at ts+2
    tr = sum(1 for a, b in zip(final[ts + 2:], orig[ts + 2:]) if a != b)
    storage = final[ts:ts + 2] != orig[ts:ts + 2]                     # last-word storage bytes
    pad_ok = final[Fo["bitcount_off"] + nfull:Fo["bitcount_off"] + Fo["sizeB"]] == \
             orig[Fo["bitcount_off"] + nfull:Fo["bitcount_off"] + Fo["sizeB"]]
    print(f"  GATE D structure: len={Ff['len']} bpf={Ff['bpf']} trailer=0x{Ff['trailer_start']:X} "
          f"sizeA={Ff['sizeA']} sizeB={Ff['sizeB']} bsb={Ff['bitstream_base']} "
          f"(unchanged: {'YES' if not sd else 'NO -> ' + str(sd)})")
    print(f"           count table size {Fo['sizeB']}B preserved, pads {pad_ok}, "
          f"fixed-trailer[0x{ts+2:X}:] diffs {tr}, last-word storage bytes differ: {storage} "
          f"-> {'PASS' if not sd and pad_ok and tr == 0 else 'FAIL'}")
    if sd or not pad_ok or tr: fails.append(f"D:{sd}/{pad_ok}/{tr}")

    # ---- STEP 7: output --------------------------------------------------
    open(args.output, "wb").write(final)
    print(f"== OUTPUT ==\n  {args.output}\n  sha256: {hashlib.sha256(final).hexdigest().upper()}")
    if fails:
        print("VERDICT: SPIN_GATE_FAIL -> " + "; ".join(fails)); return 1
    print("VERDICT: SPIN_OFFLINE_PASS"); return 0

if __name__ == "__main__":
    sys.exit(main())
