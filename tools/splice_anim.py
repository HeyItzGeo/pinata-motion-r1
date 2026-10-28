"""
CAFF Animation Splicer — replace the animation .data section inside a CAFF V36 container.

Reads an existing animation CAFF, replaces the QUAT_BITSTREAM .data payload with a new
encoded animation blob (from encode_quat_v4.encode_deltas()), updates all offsets/sizes/CRC,
and writes the modified CAFF.

CAFF V36 layout (from CAFFeinated Bundle.h/Bundle.cpp + MeExtractModels decompile):
  Header 0x78 bytes, all multi-byte fields BIG-ENDIAN (byteswapFlags=1)
  Section table: numSectionTypes x 0x21-byte entries + labels
  File label table: totalSize + per-asset offsets + null-terminated strings
  ADB string (optional): length + data
  File infos: numSections x 0x0E-byte entries (asset, start, size, sectionType, align)
  Relocation table
  Section data (concatenated blocks; file infos describe offsets within)

checksum32: Rare's header CRC (from CAFFeinated ReaderUtils.h, credit MojoBojo/XephyrCraft).

Usage:
  python splice_anim.py <input_caff> <new_data.bin> <output_caff>
  python splice_anim.py --test   # self-test with crocodile_eat
"""
import struct
import sys
import os


def checksum32(data: bytes) -> int:
    """Rare's checksum32 — from CAFFeinated ReaderUtils.h (Mumbo's Motors / XephyrCraft)."""
    r11 = 0
    for byte in data:
        r8 = byte
        r10 = r11 << 4
        if r8 & 0x80:
            r11 = 0xFFFFFF80 | r8
        else:
            r11 = r8
        r11 = (r11 + r10) & 0xFFFFFFFF
        r10 = r11 & 0xF0000000
        if r10 != 0:
            r8 = r10 >> 24
            r10 = (r8 | r10) & 0xFFFFFFFF
            r11 = (r10 ^ r11) & 0xFFFFFFFF
    return r11


def be32(data, off):
    return struct.unpack_from(">I", data, off)[0]


def be32s(data, off):
    return struct.unpack_from(">i", data, off)[0]


def be16(data, off):
    return struct.unpack_from(">H", data, off)[0]


class CaffV36:
    """Parsed CAFF V36 container."""

    def __init__(self, data: bytes):
        if data[0:4] != b"CAFF":
            raise ValueError("Not a CAFF file (missing magic)")
        self.raw = data
        self._parse_header()
        if self.compression == 1:
            self._decompress_tables()
        self._parse_section_table()
        self._parse_file_labels()
        self._parse_adb()
        self._parse_file_infos()
        self._parse_reloc()
        self._parse_section_data()

    def _parse_header(self):
        d = self.raw
        self.header_size = be32(d, 0x14)
        self.header_hash = be32(d, 0x18)
        self.num_assets = be32(d, 0x1C)
        self.num_sections = be32(d, 0x20)
        self.num_byteswap_groups = be32(d, 0x24)
        self.num_byteswaps = be32(d, 0x28)
        self.num_reloc_groups = be32(d, 0x2C)
        self.num_relocs = be32(d, 0x30)
        self.num_mapped_bs_groups = be32(d, 0x34)
        self.num_mapped_bs = be32(d, 0x38)
        self.num_mapped_reloc_groups = be32(d, 0x3C)
        self.num_mapped_relocs = be32(d, 0x40)
        self.num_pool_items = be32(d, 0x44)
        self.byteswap_flags = d[0x48]
        self.num_section_types = d[0x49]
        self.compression = d[0x4A]
        self.num_pools = d[0x4B]
        self.sect_names_buf_len = be32(d, 0x4C)
        self.sect_table_size = be32(d, 0x50)
        self.sect_table_base = be32(d, 0x54)
        self.sect_table_overlap = be32(d, 0x58)
        self.sect_table_load_size = be32(d, 0x5C)
        self.sect_table_comp_size = be32(d, 0x60)
        self.reloc_table_size = be32(d, 0x64)
        self.reloc_table_base = be32(d, 0x68)
        self.reloc_table_overlap = be32(d, 0x6C)
        self.reloc_table_load_size = be32(d, 0x70)
        self.reloc_table_comp_size = be32(d, 0x74)

    def _decompress_tables(self):
        """Decompress section table and reloc table when compression=1.
        Layout after header: compressed_sect_table + compressed_reloc_table + section_data.
        sect_table_load_size = decompressed size of section table.
        reloc_table_load_size = decompressed size of reloc table.
        """
        import zlib
        d = self.raw
        hs = self.header_size

        comp_sect_size = self.sect_table_comp_size if self.sect_table_comp_size else self.sect_table_size
        comp_reloc_size = self.reloc_table_comp_size if self.reloc_table_comp_size else self.reloc_table_size
        load_sect_size = self.sect_table_load_size if self.sect_table_load_size else self.sect_table_size
        load_reloc_size = self.reloc_table_load_size if self.reloc_table_load_size else self.reloc_table_size

        # Compressed section table starts right after header
        comp_sect_data = d[hs:hs + comp_sect_size]
        # Compressed reloc table follows
        comp_reloc_start = hs + comp_sect_size
        comp_reloc_data = d[comp_reloc_start:comp_reloc_start + comp_reloc_size]

        # Decompress
        decomp_sect = zlib.decompress(comp_sect_data)
        decomp_reloc = zlib.decompress(comp_reloc_data) if comp_reloc_size > 0 else b""

        # Section data starts after compressed tables
        section_data_start = hs + comp_sect_size + comp_reloc_size
        section_data = d[section_data_start:]

        # Reconstruct: header + decompressed_sect_table + decompressed_reloc_table + section_data
        # Update size fields so downstream parsers see uncompressed sizes
        self.raw = bytearray(d[:hs] + decomp_sect + decomp_reloc + section_data)
        # Point size fields at decompressed values
        struct.pack_into(">I", self.raw, 0x50, len(decomp_sect))   # sect_table_size
        struct.pack_into(">I", self.raw, 0x5C, len(decomp_sect))   # sect_table_load_size
        struct.pack_into(">I", self.raw, 0x60, len(decomp_sect))   # sect_table_comp_size
        struct.pack_into(">I", self.raw, 0x64, len(decomp_reloc))  # reloc_table_size
        struct.pack_into(">I", self.raw, 0x70, len(decomp_reloc))  # reloc_table_load_size
        struct.pack_into(">I", self.raw, 0x74, len(decomp_reloc))  # reloc_table_comp_size
        self.raw = bytes(self.raw)

        # Re-read header fields from updated raw
        self.sect_table_size = len(decomp_sect)
        self.sect_table_load_size = len(decomp_sect)
        self.sect_table_comp_size = len(decomp_sect)
        self.reloc_table_size = len(decomp_reloc)
        self.reloc_table_load_size = len(decomp_reloc)
        self.reloc_table_comp_size = len(decomp_reloc)

    def _parse_section_table(self):
        d = self.raw
        hs = self.header_size
        self.section_entries_raw = []
        self.section_labels = []
        for i in range(self.num_section_types):
            pos = hs + i * 0x21
            entry = d[pos:pos + 0x21]
            self.section_entries_raw.append(entry)
        label_base = hs + 0x21 * self.num_section_types
        for i in range(self.num_section_types):
            e = self.section_entries_raw[i]
            str_off = struct.unpack_from(">i", e, 0)[0]
            ls = label_base + str_off
            le = d.index(b"\x00", ls)
            self.section_labels.append(d[ls:le].decode("ascii"))

    def _parse_file_labels(self):
        d = self.raw
        fl_base = self.header_size + 0x21 * self.num_section_types + self.sect_names_buf_len
        self.file_label_table_offset = fl_base
        self.total_label_table_size = be32s(d, fl_base)
        self.file_label_offsets = []
        for i in range(self.num_assets):
            self.file_label_offsets.append(be32s(d, fl_base + 4 + i * 4))
        str_base = fl_base + 4 + self.num_assets * 4
        self.file_labels = []
        for i in range(self.num_assets):
            ls = str_base + self.file_label_offsets[i]
            le = d.index(b"\x00", ls)
            self.file_labels.append(d[ls:le].decode("ascii"))

    def _parse_adb(self):
        d = self.raw
        # Layout: totalLabelTableSize(4) + numAssets*4 offsets + label strings + adbLen(4) + adb
        self.adb_offset = (self.file_label_table_offset + 4
                           + self.num_assets * 4 + self.total_label_table_size)
        self.adb_len = be32s(d, self.adb_offset)
        if self.adb_len > 0:
            self.adb_data = d[self.adb_offset + 4:self.adb_offset + 4 + self.adb_len]
        else:
            self.adb_data = b""

    def _parse_file_infos(self):
        d = self.raw
        fi_base = self.adb_offset + 4 + max(0, self.adb_len)
        self.file_infos = []
        for i in range(self.num_sections):
            pos = fi_base + i * 0x0E
            asset = be32s(d, pos)
            start = be32s(d, pos + 4)
            size = be32s(d, pos + 8)
            section_type = d[pos + 0x0C]
            start_align = d[pos + 0x0D]
            label = self.file_labels[asset - 1] if 0 < asset <= len(self.file_labels) else f"asset_{asset}"
            self.file_infos.append({
                "asset": asset,
                "start": start,
                "size": size,
                "section_type": section_type,
                "start_alignment": start_align,
                "label": label,
            })

    def _parse_reloc(self):
        d = self.raw
        # Reloc table starts after header + section table
        self.reloc_data_start = self.header_size + self.sect_table_size
        self.reloc_raw = d[self.reloc_data_start:self.reloc_data_start + self.reloc_table_size]

    def _parse_section_data(self):
        import zlib
        d = self.raw
        self.data_start = self.header_size + self.sect_table_size + self.reloc_table_size
        # Section data block sizes
        self.section_block_sizes = []
        self.section_comp_sizes = []
        for e in self.section_entries_raw:
            size = struct.unpack_from(">i", e, 9)[0]  # uncompressed size
            comp = struct.unpack_from(">i", e, 0x1D)[0] if len(e) >= 0x21 else size  # compressed size
            self.section_block_sizes.append(size)
            self.section_comp_sizes.append(comp if comp > 0 else size)

        if self.compression == 1:
            # Each section is independently zlib-compressed on disk
            decomp_sections = []
            pos = self.data_start
            for i in range(len(self.section_entries_raw)):
                comp_size = self.section_comp_sizes[i]
                uncomp_size = self.section_block_sizes[i]
                raw = d[pos:pos + comp_size]
                try:
                    dec = zlib.decompress(raw)
                except:
                    dec = raw  # not compressed, use as-is
                decomp_sections.append(dec)
                pos += comp_size
            self.section_data_raw = b"".join(decomp_sections)
        else:
            self.section_data_raw = d[self.data_start:self.data_start + sum(self.section_block_sizes)]

    def get_asset_data(self, asset_name_substr: str) -> bytes:
        """Extract the data for the first file info whose label contains the substring."""
        for fi in self.file_infos:
            if asset_name_substr in fi["label"]:
                return self.section_data_raw[fi["start"]:fi["start"] + fi["size"]]
        raise ValueError(f"No file info matching '{asset_name_substr}'")

    def replace_asset_data(self, asset_name_substr: str, new_data: bytes) -> "CaffV36":
        """Return a new CaffV36 with the matched asset's data replaced by new_data."""
        target_fi = None
        for fi in self.file_infos:
            if asset_name_substr in fi["label"]:
                target_fi = fi
                break
        if target_fi is None:
            raise ValueError(f"No file info matching '{asset_name_substr}'")

        # Rebuild section data: keep all other file infos' data, replace target
        new_section_data = bytearray()
        for fi in self.file_infos:
            if fi is target_fi:
                new_section_data.extend(new_data)
                new_size = len(new_data)
                new_start = len(new_section_data) - new_size
            else:
                old_data = self.section_data_raw[fi["start"]:fi["start"] + fi["size"]]
                new_start = len(new_section_data)
                new_section_data.extend(old_data)
                new_size = fi["size"]
            fi["start"] = new_start
            fi["size"] = new_size

        # Preserve trailing padding from original section data
        old_total = sum(fi["size"] for fi in self.file_infos)
        old_padding = len(self.section_data_raw) - old_total
        if old_padding > 0:
            new_section_data.extend(b"\x00" * old_padding)

        # Update section entry sizes (only the first section type holds .data)
        # For uncompressed: size = compressedSize = len(new_section_data)
        for i in range(self.num_section_types):
            e = bytearray(self.section_entries_raw[i])
            struct.pack_into(">i", e, 9, len(new_section_data))       # size
            struct.pack_into(">i", e, 0x1D, len(new_section_data))    # compressedSize
            self.section_entries_raw[i] = bytes(e)

        self.section_data_raw = bytes(new_section_data)
        # Do NOT recalculate sect_table_size — the original may have padding
        # bytes that we must preserve to keep reloc/data offsets stable.
        # The build() method pads the section table to self.sect_table_size.
        return self

    def _calc_sect_table_size(self) -> int:
        """Calculate the section table size (entries + labels + file labels + adb + file infos)."""
        size = 0
        size += self.num_section_types * 0x21  # section entries
        size += self.sect_names_buf_len          # section labels
        size += 4 + self.num_assets * 4 + self.total_label_table_size  # file label table
        size += 4 + max(0, self.adb_len)         # ADB
        size += self.num_sections * 0x0E         # file infos
        return size

    def build(self) -> bytes:
        """Rebuild the complete CAFF file."""
        # --- Build section table blob ---
        st = bytearray()
        # Section entries
        for e in self.section_entries_raw:
            st.extend(e)
        # Section labels
        for label in self.section_labels:
            st.extend(label.encode("ascii") + b"\x00")
        # File label table
        st.extend(struct.pack(">i", self.total_label_table_size))
        for lo in self.file_label_offsets:
            st.extend(struct.pack(">i", lo))
        for label in self.file_labels:
            st.extend(label.encode("ascii") + b"\x00")
        # ADB
        st.extend(struct.pack(">i", self.adb_len))
        if self.adb_len > 0:
            st.extend(self.adb_data)
        # File infos
        for fi in self.file_infos:
            st.extend(struct.pack(">i", fi["asset"]))
            st.extend(struct.pack(">i", fi["start"]))
            st.extend(struct.pack(">i", fi["size"]))
            st.append(fi["section_type"] & 0xFF)
            st.append(fi["start_alignment"] & 0xFF)

        # Verify section table size matches header
        # Original may have padding; preserve the header's sect_table_size
        actual_st_size = len(st)
        target_st_size = self.sect_table_size
        if actual_st_size < target_st_size:
            # Pad to original size (preserves alignment padding)
            st.extend(b"\x00" * (target_st_size - actual_st_size))
            actual_st_size = target_st_size
        elif actual_st_size > target_st_size:
            # Section table grew; update header
            target_st_size = actual_st_size

        # --- Build header ---
        hdr = bytearray(self.header_size)
        hdr[0:4] = b"CAFF"
        hdr[4:17] = b"07.08.06.0036"
        struct.pack_into(">i", hdr, 0x14, self.header_size)
        struct.pack_into(">I", hdr, 0x18, 0)  # placeholder for CRC
        struct.pack_into(">i", hdr, 0x1C, self.num_assets)
        struct.pack_into(">i", hdr, 0x20, self.num_sections)
        struct.pack_into(">i", hdr, 0x24, self.num_byteswap_groups)
        struct.pack_into(">i", hdr, 0x28, self.num_byteswaps)
        struct.pack_into(">i", hdr, 0x2C, self.num_reloc_groups)
        struct.pack_into(">i", hdr, 0x30, self.num_relocs)
        struct.pack_into(">i", hdr, 0x34, self.num_mapped_bs_groups)
        struct.pack_into(">i", hdr, 0x38, self.num_mapped_bs)
        struct.pack_into(">i", hdr, 0x3C, self.num_mapped_reloc_groups)
        struct.pack_into(">i", hdr, 0x40, self.num_mapped_relocs)
        struct.pack_into(">i", hdr, 0x44, self.num_pool_items)
        hdr[0x48] = self.byteswap_flags
        hdr[0x49] = self.num_section_types
        hdr[0x4A] = self.compression
        hdr[0x4B] = self.num_pools
        struct.pack_into(">i", hdr, 0x4C, self.sect_names_buf_len)
        # sectTable (size, base, overlap, loadSize, compressedSize)
        struct.pack_into(">i", hdr, 0x50, target_st_size if self.compression == 0 else self.sect_table_comp_size)
        struct.pack_into(">i", hdr, 0x54, self.sect_table_base)
        struct.pack_into(">i", hdr, 0x58, self.sect_table_overlap)
        struct.pack_into(">i", hdr, 0x5C, self.sect_table_load_size)
        struct.pack_into(">i", hdr, 0x60, target_st_size if self.compression == 0 else self.sect_table_comp_size)
        # relocTable
        struct.pack_into(">i", hdr, 0x64, self.reloc_table_size)
        struct.pack_into(">i", hdr, 0x68, self.reloc_table_base)
        struct.pack_into(">i", hdr, 0x6C, self.reloc_table_overlap)
        struct.pack_into(">i", hdr, 0x70, self.reloc_table_load_size)
        struct.pack_into(">i", hdr, 0x74, self.reloc_table_comp_size)

        # Compute CRC over entire header (with CRC field = 0)
        crc = checksum32(bytes(hdr))
        struct.pack_into(">I", hdr, 0x18, crc)

        # --- Assemble ---
        out = bytearray()
        out.extend(hdr)
        out.extend(st)
        out.extend(self.reloc_raw)
        out.extend(self.section_data_raw)
        return bytes(out)


def splice_caff(input_caff_path: str, new_data_path: str, output_caff_path: str,
                asset_substr: str = "aid_anim") -> dict:
    """
    Splice a new animation .data blob into an existing animation CAFF.

    Returns a summary dict.
    """
    caff_data = open(input_caff_path, "rb").read()
    new_data = open(new_data_path, "rb").read()

    caff = CaffV36(caff_data)
    old_fi = None
    for fi in caff.file_infos:
        if asset_substr in fi["label"]:
            old_fi = fi
            break
    if old_fi is None:
        raise ValueError(f"No file info matching '{asset_substr}' in {input_caff_path}")

    old_size = old_fi["size"]
    caff.replace_asset_data(asset_substr, new_data)
    out = caff.build()
    open(output_caff_path, "wb").write(out)

    return {
        "input_size": len(caff_data),
        "output_size": len(out),
        "old_anim_size": old_size,
        "new_anim_size": len(new_data),
        "asset": old_fi["label"],
        "output_path": output_caff_path,
    }


def verify_caff(caff_path: str) -> dict:
    """Verify a CAFF file's header CRC and structural consistency."""
    data = open(caff_path, "rb").read()
    caff = CaffV36(data)

    # Verify CRC
    hdr = bytearray(data[:caff.header_size])
    stored_crc = be32(hdr, 0x18)
    struct.pack_into(">I", hdr, 0x18, 0)
    calc_crc = checksum32(bytes(hdr))
    crc_ok = stored_crc == calc_crc

    # Verify section data fits
    data_fits = caff.data_start + sum(caff.section_block_sizes) <= len(data)

    # Verify file infos fit within section data
    section_size = sum(caff.section_block_sizes)
    fi_ok = all(fi["start"] + fi["size"] <= section_size for fi in caff.file_infos)

    return {
        "path": caff_path,
        "file_size": len(data),
        "header_crc_stored": f"0x{stored_crc:08X}",
        "header_crc_calc": f"0x{calc_crc:08X}",
        "crc_ok": crc_ok,
        "data_fits": data_fits,
        "file_infos_ok": fi_ok,
        "num_assets": caff.num_assets,
        "num_sections": caff.num_sections,
        "file_labels": caff.file_labels,
        "file_infos": [(fi["label"], fi["start"], fi["size"]) for fi in caff.file_infos],
    }


def run_test():
    """Self-test: splice crocodile_eat CAFF with re-encoded data."""
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    caff_path = os.path.join(tools_dir, "aid_anim_pinata_actor_animal_crocodile_eat_full.bin")
    data_path = os.path.join(tools_dir, "aid_anim_pinata_actor_animal_crocodile_eat_data.bin")

    print("=" * 72)
    print("CAFF ANIMATION SPLICER — SELF TEST")
    print("=" * 72)

    if not os.path.exists(caff_path):
        print(f"FAIL: missing {caff_path}")
        return False

    # Parse and verify original
    print("\n[1] Verify original CAFF")
    v = verify_caff(caff_path)
    print(f"  file_size={v['file_size']} crc_stored={v['header_crc_stored']} "
          f"crc_calc={v['header_crc_calc']} ok={v['crc_ok']}")
    print(f"  assets={v['num_assets']} sections={v['num_sections']}")
    for label, start, size in v["file_infos"]:
        print(f"  file_info: '{label}' start={start} size={size}")

    if not v["crc_ok"]:
        print("  WARNING: original CRC mismatch (may be expected for some tools)")

    # Splice with same data (no-op test)
    print("\n[2] Splice with original animation data (no-op roundtrip)")
    # Extract just the animation portion (not the full .data section)
    caff_orig = CaffV36(open(caff_path, "rb").read())
    anim_data = caff_orig.get_asset_data("aid_anim")
    anim_tmp = os.path.join(tools_dir, "_test_anim_only.bin")
    open(anim_tmp, "wb").write(anim_data)
    out_path = os.path.join(tools_dir, "_test_splice_noop.bin")
    result = splice_caff(caff_path, anim_tmp, out_path, asset_substr="aid_anim")
    print(f"  old_anim_size={result['old_anim_size']} new_anim_size={result['new_anim_size']}")
    print(f"  output_size={result['output_size']} (input was {result['input_size']})")

    v2 = verify_caff(out_path)
    print(f"  output CRC: stored={v2['header_crc_stored']} calc={v2['header_crc_calc']} ok={v2['crc_ok']}")
    print(f"  data_fits={v2['data_fits']} file_infos_ok={v2['file_infos_ok']}")

    # Verify animation data is identical
    c2 = CaffV36(open(out_path, "rb").read())
    spliced = c2.get_asset_data("aid_anim")
    data_match = spliced == anim_data
    print(f"  animation data preserved: {data_match}")

    # Splice with DIFFERENT size data (encode_quat_v4 output)
    print("\n[3] Splice with different-size data (encode_quat_v4 output)")
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
    print(f"  new encoded blob: {len(new_blob)} bytes (original anim: {len(anim_data)})")

    out2_path = os.path.join(tools_dir, "_test_splice_new.bin")
    result2 = splice_caff(caff_path, data_path, out2_path, asset_substr="aid_anim")
    # Actually splice with the new blob
    caff2 = CaffV36(open(caff_path, "rb").read())
    caff2.replace_asset_data("aid_anim", new_blob)
    out2 = caff2.build()
    open(out2_path, "wb").write(out2)

    v3 = verify_caff(out2_path)
    print(f"  output_size={len(out2)} crc_ok={v3['crc_ok']} data_fits={v3['data_fits']} "
          f"file_infos_ok={v3['file_infos_ok']}")
    for label, start, size in v3["file_infos"]:
        print(f"  file_info: '{label}' start={start} size={size}")

    # Verify the new animation decodes correctly
    c3 = CaffV36(out2)
    new_anim = c3.get_asset_data("aid_anim")
    dec2 = decode_anim_game_formula(new_anim)
    max_err = 0.0
    for f in range(min(nfr, dec2["frame_count"])):
        for ch in range(min(nch, dec2["channel_count"])):
            for ci in range(3):
                err = abs(dec["frames"][f][ch][ci] - dec2["frames"][f][ch][ci])
                max_err = max(max_err, err)
    print(f"  decode of spliced animation: max_err={max_err:.9f}")

    # Cleanup
    for p in [out_path, out2_path, anim_tmp]:
        if os.path.exists(p):
            os.remove(p)

    status = "PASS" if (v3["crc_ok"] and v3["data_fits"] and v3["file_infos_ok"]
                        and max_err < 1e-4 and data_match) else "FAIL"
    print(f"\n  RESULT [{status}]")
    return status == "PASS"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        ok = run_test()
        sys.exit(0 if ok else 1)
    elif len(sys.argv) >= 4:
        result = splice_caff(sys.argv[1], sys.argv[2], sys.argv[3],
                             asset_substr=sys.argv[4] if len(sys.argv) > 4 else "aid_anim")
        print(f"Spliced: {result['asset']}")
        print(f"  {result['old_anim_size']} -> {result['new_anim_size']} bytes")
        print(f"  CAFF: {result['input_size']} -> {result['output_size']} bytes")
        v = verify_caff(sys.argv[3])
        print(f"  CRC ok={v['crc_ok']} data_fits={v['data_fits']} file_infos_ok={v['file_infos_ok']}")
    else:
        print(__doc__)
