# Credits

This project would not exist without prior reverse-engineering work by the community. Full acknowledgment:

---

## CAFFeinated

**Author:** OlieGamerTV  
**Repository:** https://github.com/OlieGamerTV/CAFFeinated  
**License:** MIT

CAFFeinated is a C++ tool for reading Rare's `CAFF` (Common Asset File Format) containers used across Xbox and Xbox 360 titles. This project reimplements (in Python) the following, informed by CAFFeinated's published format research:

- `checksum32` — Rare's header CRC algorithm (from `ReaderUtils.h`)
- `CAFF` V36 header layout (from `Bundle.h` / `Bundle.cpp`)
- `PKG` archive structure (from `PinataPKG.cpp`)

All reimplementations are original Python code. No CAFFeinated source is redistributed here.

---

## ReXGlue SDK / TiP-Recomp

**Author:** Tom Clay  
**Repository:** https://github.com/rexglue/rexglue-sdk  
**License:** BSD-3-Clause

The recompiled PC build of *Viva Piñata: Trouble in Paradise* (`retip.exe`) and its generated decompilation were used as **reference material** for understanding the game's animation decoder (`animQuatBitstreamDecompressor`). No ReXGlue or TiP-Recomp source code is redistributed in this repository.

---

## PinataCentral

**Author:** SolarCookies  
**Repository:** `github.com/SolarCookies/PinataCentral` — no longer public  
**Announcement:** https://www.reddit.com/r/VivaPinata/comments/1jlmv07/introducing_pinatacentral_a_research_tool_for/  
**License:** MIT (verified from the project's LICENSE file)

PinataCentral's documentation of the `PKG` and `CAFF` formats informed this project's container parsing. No PinataCentral source is redistributed here.

---

## Mumbo's Motors / XephyrCraft

The `checksum32` algorithm was originally documented by MojoBojo / XephyrCraft via the Mumbo's Motors project. Credit for the original reverse-engineering of Rare's checksum.

---

## Note on decompiled game code

The recompiled game source (`retip_recomp.*.cpp`) referenced during development is derived from the game binary and is **not redistributed** in this repository. It was used as a read-only reference for format understanding. The Python decoder and encoder in this repository are original implementations.
