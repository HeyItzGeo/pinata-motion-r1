"""
Checksum32 from CAFFeinated ReaderUtils.h (line 223).
Credit: MojoBojo/XephyrCraft via Mumbo's Motors.
"""
import struct

def checksum32(data: bytes) -> int:
    r11 = 0
    for i in range(len(data)):
        # data[i] is signed char in C
        byte_val = data[i]
        if byte_val >= 128:
            r8 = byte_val | 0xFFFFFF00  # sign extend to 32-bit
        else:
            r8 = byte_val
        
        r10 = (r11 << 4) & 0xFFFFFFFF
        
        if r8 & 0x80:
            r11 = (0xFFFFFF80 | r8) & 0xFFFFFFFF
        else:
            r11 = r8
        
        r11 = (r11 + r10) & 0xFFFFFFFF
        r10 = r11 & 0xF0000000
        
        if r10 != 0:
            r8 = r10 >> 24
            r10 = (r8 | r10) & 0xFFFFFFFF
            r11 = r10 ^ r11
    
    return r11

if __name__ == "__main__":
    # Original demo (was hardcoded to a local 1150.pkg.backup): verify the
    # entry-10 CAFF header CRC. Path is now a CLI argument.
    import argparse, os, sys
    ap = argparse.ArgumentParser(
        description="Verify checksum32 against the entry-10 CAFF header of a 1150 PKG")
    ap.add_argument("pkg",
                    help="path to a 1150 PKG (e.g. <path/to>/1150.pkg.backup)")
    ap.add_argument("--entry", type=int, default=10,
                    help="PKG entry index (default: 10)")
    ap.add_argument("--caff-size", type=int, default=380237,
                    help="CAFF byte length to slice from the entry (default: 380237)")
    args = ap.parse_args()
    if not os.path.isfile(args.pkg):
        sys.exit(f"file not found: {args.pkg} — pass the path to a 1150 PKG")
    PKG = args.pkg
    with open(PKG, 'rb') as f:
        pkg = f.read()

    entry_off = 8 + args.entry * 12
    caff_offset = struct.unpack('>I', pkg[entry_off+4:entry_off+8])[0]
    caff = pkg[caff_offset:caff_offset+args.caff_size]

    header = caff[:0x78]
    original_crc = struct.unpack('>I', caff[0x18:0x1C])[0]
    computed_crc = checksum32(header)

    print(f"Original CRC in file: 0x{original_crc:08X}")
    print(f"CRC (info, field not zeroed): 0x{computed_crc:08X}")
    print(f"MATCH: {original_crc == computed_crc}")

    # Also check: does it match when we zero the CRC field?
    header_zeroed = bytearray(header)
    struct.pack_into('>I', header_zeroed, 0x18, 0)
    crc_zeroed = checksum32(bytes(header_zeroed))
    print(f"\nWith CRC field zeroed: 0x{crc_zeroed:08X}")
    print(f"Matches original: {crc_zeroed == original_crc}")
