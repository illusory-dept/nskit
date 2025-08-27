#!/usr/bin/env python3
"""
nsaout.py - arc.nsa extractor

Usage:
  # basic extraction
  python nsa_extract.py -i /path/to/game -o ./arc

  # scan more numbered volumes
  python nsa_extract.py -i . --max-volumes 200

  # header quirks
  python nsa_extract.py -i . --hdr-skip 1                 # skip N bytes before object count
  python nsa_extract.py -i . --objcount-fallback          # if first count==0, read another u16

  # triage a single entry + peek bytes
  python nsa_extract.py -i . -o ./arc --only-index 1 --hexdump 128

  # SPB conversion modes
  python nsa_extract.py -i . -o ./arc --spb-mode auto     # default: try convert, bail on timeout -> keep original
  python nsa_extract.py -i . -o ./arc --spb-mode convert  # always attempt (if plausible)
  python nsa_extract.py -i . -o ./arc --spb-mode copy     # never convert SPB; keep original bytes

  # cap per-image SPB work time (milliseconds); on timeout keep original
  python nsa_extract.py -i . -o ./arc --spb-timeout-ms 1500

  # bypass plausibility guard (advanced; may try more SPB variants)
  python nsa_extract.py -i . -o ./arc --spb-skip-plausibility

  # bypass expanded_size vs expected BMP size check (advanced)
  python nsa_extract.py -i . -o ./arc --spb-skip-sizecheck

Desc:
  - Finds arc.nsa and arc#.nsa and extracts contents.
  - .bmp entries:
      - If bytes start with 'BM' -> written as-is.
      - If LZSS magic (0xA153) at 0 or +2 -> decompress to BMP.
      - Else, attempts SPB -> 24-bit BMP only if plausible; otherwise keeps original.
  - .nbz entries: strips 4-byte header and uses Python bz2 to also write a sibling .wav.

"""

from pathlib import Path
import argparse
import io
import os
import struct
import sys
import bz2
import binascii
import time
from dataclasses import dataclass
from typing import Tuple, Optional

# -------------------------------
# Utilities
# -------------------------------


def read_cstring(f: io.BufferedReader) -> bytes:
    """Read a NUL-terminated byte string."""
    out = bytearray()
    extend = out.extend
    read = f.read
    while True:
        b = read(1)
        if not b or b == b"\x00":
            break
        extend(b)
    return bytes(out)


def u16be(b: bytes) -> int:
    return struct.unpack(">H", b)[0]


def u32be(b: bytes) -> int:
    return struct.unpack(">I", b)[0]


class BitReader:
    """MSB-first bit reader from a bytes-like object (optimized)."""

    __slots__ = ("data", "pos", "bitbuf", "rem", "_len")

    def __init__(self, data: bytes, start: int = 0, end: int | None = None):
        mv = memoryview(data)
        self.data = mv[start : end if end is not None else len(mv)]
        self.pos = 0
        self.bitbuf = 0
        self.rem = 0
        self._len = len(self.data)

    def get_bits(self, n: int) -> int:
        if n == 0:
            return 0
        rem = self.rem
        buf = self.bitbuf
        pos = self.pos
        data = self.data
        dlen = self._len
        v = 0
        while n > 0:
            if rem == 0:
                if pos >= dlen:
                    self.rem = rem
                    self.bitbuf = buf
                    self.pos = pos
                    raise EOFError("BitReader: out of data")
                buf = (buf << 8) | data[pos]
                pos += 1
                rem = 8
            take = n if n <= rem else rem
            v = (v << take) | ((buf >> (rem - take)) & ((1 << take) - 1))
            rem -= take
            n -= take
        self.rem = rem
        self.bitbuf = buf
        self.pos = pos
        return v

    def get_u8(self) -> int:
        if self.rem == 0:
            if self.pos >= self._len:
                raise EOFError("BitReader: out of data")
            b = self.data[self.pos]
            self.pos += 1
            return b
        return self.get_bits(8)


# -------------------------------
# Data structures
# -------------------------------


@dataclass
class NSAEntry:
    name: str
    compression_flag: int
    rel_offset: int
    stored_size: int
    expanded_size: int


@dataclass
class NSAHeader:
    object_count: int
    base_offset: int


# -------------------------------
# Parsing (technical flags)
# -------------------------------


def parse_header(
    f: io.BufferedReader, hdr_skip: int, objcount_fallback: bool
) -> NSAHeader:
    """
    Parse the NSA header with technical knobs:
      - hdr_skip: skip N bytes before reading object_count
      - objcount_fallback: if first read returns 0, read another u16
    """
    if hdr_skip:
        _ = f.read(hdr_skip)

    obj_raw = f.read(2)
    if len(obj_raw) != 2:
        raise EOFError("Unexpected EOF reading object_count")
    obj = u16be(obj_raw)

    if objcount_fallback and obj == 0:
        obj_raw2 = f.read(2)
        if len(obj_raw2) != 2:
            raise EOFError("Unexpected EOF reading object_count (fallback)")
        obj = u16be(obj_raw2)

    base_raw = f.read(4)
    if len(base_raw) != 4:
        raise EOFError("Unexpected EOF reading base offset")
    base = u32be(base_raw)
    return NSAHeader(object_count=obj, base_offset=base)


def parse_entries(f: io.BufferedReader, count: int) -> list[NSAEntry]:
    entries: list[NSAEntry] = []
    for _ in range(count):
        name = read_cstring(f).decode("shift_jis", errors="replace")
        comp_b = f.read(1)
        if len(comp_b) != 1:
            raise EOFError("Unexpected EOF reading directory compression flag")
        compression_flag = comp_b[0]
        rel_offset = u32be(f.read(4))
        stored = u32be(f.read(4))
        expanded = u32be(f.read(4))
        entries.append(NSAEntry(name, compression_flag, rel_offset, stored, expanded))
    return entries


# -------------------------------
# Decompression / conversion
# -------------------------------


def lzss_decompress(data: bytes, out_size: int, start_offset: int = 0) -> bytes:
    """
    LZSS variant:
      - 1-bit flags; 1 -> literal(8), 0 -> backref(offset 8, length (4)+2)
      - 256 ring buffer, initial zeros, pos = 256 - 17
      - Stops after out_size bytes or EOF.
    """
    br = BitReader(data, start=start_offset)
    RING = bytearray(256)
    bufpos = 256 - 17
    out = bytearray()
    app = out.append
    try:
        gb = br.get_bits
        g8 = br.get_u8
        while len(out) < out_size:
            if gb(1):
                ch = g8()
                RING[bufpos] = ch
                bufpos = (bufpos + 1) & 0xFF
                app(ch)
            else:
                offset = g8()
                count = gb(4) + 2
                for i in range(count):
                    ch = RING[(offset + i) & 0xFF]
                    RING[bufpos] = ch
                    bufpos = (bufpos + 1) & 0xFF
                    app(ch)
                    if len(out) >= out_size:
                        break
    except EOFError:
        pass
    return bytes(out)


# --- SPB plausibility, caps, and timeout support ---

MAX_W = 8192
MAX_H = 8192
MAX_PIXELS = 4096 * 4096  # 16MP cap: avoids pathological allocations


def spb_plausible(raw: bytes) -> tuple[bool, int, int]:
    if len(raw) < 4:
        return (False, 0, 0)
    w = (raw[0] << 8) | raw[1]
    h = (raw[2] << 8) | raw[3]
    if not (1 <= w <= MAX_W and 1 <= h <= MAX_H):
        return (False, w, h)
    if w * h > MAX_PIXELS:
        return (False, w, h)
    return (True, w, h)


def expected_24bpp_bmp_size(w: int, h: int) -> int:
    row = w * 3
    pad = (4 - (row % 4)) % 4
    return 14 + 40 + h * (row + pad)


def spb_to_bmp(
    spb: bytes,
    timeout_ms: int | None = 1500,
    scan: str = "zigzag",  # or "linear"
    plane: str = "bgr",  # or "rgb"
) -> bytes:
    """
    Convert SPB-like delta-coded image to a 24-bit BMP.
    Layout: u16_be width, u16_be height, then bit-coded plane data.
    """
    if len(spb) < 4:
        raise ValueError("SPB too short")
    width = (spb[0] << 8) | spb[1]
    height = (spb[2] << 8) | spb[3]
    if not (1 <= width <= MAX_W and 1 <= height <= MAX_H):
        raise ValueError(f"Invalid SPB size {width}x{height}")
    pix_count = width * height
    if pix_count <= 0 or pix_count > MAX_PIXELS:
        raise ValueError(f"Invalid SPB pixel count {pix_count}")

    tmp = bytearray(pix_count)  # scratch for one plane
    rgb_data = bytearray(pix_count * 3)  # B,G,R interleaved

    src = BitReader(spb, start=4)
    get_bits = src.get_bits
    get_u8 = src.get_u8

    # timeout setup
    deadline = None
    if timeout_ms is not None and timeout_ms > 0:
        deadline = time.perf_counter() + (timeout_ms / 1000.0)

    # decode planes in requested order so bytes line up with BMP BGR/RGB
    if plane.lower() == "rgb":
        plane_order = (0, 1, 2)
    else:
        plane_order = (2, 1, 0)

    for plane_idx in plane_order:
        dest_i = 0
        try:
            ch = get_u8()
        except EOFError:
            ch = 0
        tmp[0] = ch
        dest_i = 1

        try:
            ttmp = tmp
            check_counter = 0
            while dest_i < pix_count:
                # periodic timeout check (every ~16k pixels)
                check_counter += 1
                if deadline is not None and (check_counter & 0x3FFF) == 0:
                    if time.perf_counter() > deadline:
                        raise TimeoutError("SPB decode timeout")

                nbit = get_bits(3)
                if nbit == 0:
                    remaining = pix_count - dest_i
                    run = 4 if remaining >= 4 else remaining
                    ttmp[dest_i : dest_i + run] = bytes((ch,)) * run
                    dest_i += run
                    continue
                mask = get_bits(1) + 1 if nbit == 7 else nbit + 2
                for _ in range(4):
                    if mask == 8:
                        ch = get_u8()
                    else:
                        t = get_bits(mask)
                        if t & 1:
                            ch = (ch + ((t >> 1) + 1)) & 0xFF
                        else:
                            ch = (ch - (t >> 1)) & 0xFF
                    if dest_i >= pix_count:
                        break
                    ttmp[dest_i] = ch
                    dest_i += 1
        except EOFError:
            if dest_i < pix_count:
                tmp[dest_i:pix_count] = bytes((ch,)) * (pix_count - dest_i)

        # map decoded plane into rgb_data according to scan pattern
        rgb = rgb_data
        stride3 = width * 3
        p = 0
        if scan == "linear":
            # straight left-to-right, top-to-bottom
            for y in range(height):
                base = y * stride3
                q = base + plane_idx
                for x in range(width):
                    rgb[q] = tmp[p]
                    p += 1
                    q += 3
        else:
            # zigzag (forward row then reverse row)
            q = plane_idx
            half_rows = height // 2
            for _ in range(half_rows):
                # forward row
                qq = q
                endp = p + width
                while p < endp:
                    rgb[qq] = tmp[p]
                    p += 1
                    qq += 3
                q = qq + stride3
                # reverse row
                qq -= 3
                endp = p + width
                while p < endp:
                    rgb[qq] = tmp[p]
                    p += 1
                    qq -= 3
                q = qq + 3 + stride3

            if height & 1:
                qq = q
                endp = p + width
                while p < endp:
                    rgb[qq] = tmp[p]
                    p += 1
                    qq += 3

    # bottom-up BMP with row padding
    row_bytes = width * 3
    pad = (4 - (row_bytes % 4)) % 4
    dst_row_len = row_bytes + pad
    pixel_data = bytearray(dst_row_len * height)
    src_mv = memoryview(rgb_data)
    dst_mv = memoryview(pixel_data)
    for y in range(height):
        src_off = (height - 1 - y) * row_bytes
        dst_off = y * dst_row_len
        dst_mv[dst_off : dst_off + row_bytes] = src_mv[src_off : src_off + row_bytes]
        # padding already zero

    # headers
    file_size = 14 + 40 + len(pixel_data)
    header = bytearray(14 + 40)
    header[0:2] = b"BM"
    struct.pack_into("<I", header, 2, file_size)
    struct.pack_into("<I", header, 10, 14 + 40)  # pixel data offset
    struct.pack_into("<I", header, 14, 40)  # DIB header size
    struct.pack_into("<i", header, 18, width)
    struct.pack_into("<i", header, 22, height)
    struct.pack_into("<H", header, 26, 1)  # planes
    struct.pack_into("<H", header, 28, 24)  # bpp
    return bytes(header + pixel_data)


def detect_and_process_bmp(
    raw: bytes,
    expanded_size: int,
    spb_mode: str,
    spb_timeout_ms: Optional[int],
    spb_skip_plausibility: bool = False,
    spb_skip_sizecheck: bool = False,
    spb_scan: str = "zigzag",
    spb_plane: str = "bgr",
) -> Tuple[Optional[bytes], str]:
    """
    Decide how to handle a .bmp entry.

    Returns:
      (result_bytes, status)
        - result_bytes: bytes to write, or None if we should SKIP writing
        - status: string reason, one of:
            "raw_bmp"                 - already a BMP, kept as-is
            "lzss_decompressed"       - LZSS -> BMP ok
            "bz2_decompressed"        - BZip2 -> BMP ok
            "spb_converted"           - SPB -> BMP ok
            "spb_skip_implausible"    - SPB header implausible or too big
            "spb_skip_mismatch"       - expanded_size not consistent with 24bpp BMP
            "spb_skip_policy"         - user policy ("copy")
            "spb_skip_timeout"        - hit timeout
            "spb_skip_error"          - decode error
    """
    # Case 1: already BMP
    if len(raw) >= 2 and raw[:2] == b"BM":
        return (None, "raw_bmp")  # signal "write original" to caller

    # Case 1.5: BZip2-compressed BMP (some archives store 4-byte size then BZh)
    for bz_off in (0, 4):
        if len(raw) >= bz_off + 3 and raw[bz_off : bz_off + 3] == b"BZh":
            try:
                decomp = bz2.decompress(raw[bz_off:])
                if len(decomp) >= 2 and decomp[:2] == b"BM":
                    return (decomp, "bz2_decompressed")
            except Exception:
                pass

    # Case 2: LZSS -> BMP (search for magic within first 16 bytes)
    lz_off = -1
    scan_max = min(16, len(raw) - 1)
    for off in range(0, scan_max):
        if raw[off : off + 2] == b"\xa1\x53":
            lz_off = off
            break
    if lz_off >= 0:
        try:
            out = lzss_decompress(raw, out_size=expanded_size, start_offset=lz_off)
            if len(out) >= 2 and out[:2] == b"BM":
                return (out, "lzss_decompressed")
            # If it doesn't look like BMP, treat as error to try SPB path
        except Exception:
            pass

    # Case 3: SPB?
    plausible, w, h = spb_plausible(raw)
    if not plausible:
        if not spb_skip_plausibility:
            return (None, "spb_skip_implausible")
        # proceed anyway; try using header width/height if present
        if len(raw) >= 4:
            w = (raw[0] << 8) | raw[1]
            h = (raw[2] << 8) | raw[3]

    if not spb_skip_sizecheck and expanded_size and expanded_size > 0:
        expected = expected_24bpp_bmp_size(w, h)
        if abs(expected - expanded_size) > 8:
            return (None, "spb_skip_mismatch")

    if spb_mode == "copy":
        return (None, "spb_skip_policy")

    # Try SPB -> BMP with timeout
    try:
        out = spb_to_bmp(
            raw,
            timeout_ms=(
                spb_timeout_ms if spb_timeout_ms and spb_timeout_ms > 0 else 1500
            ),
            scan=spb_scan,
            plane=spb_plane,
        )
        return (out, "spb_converted")
    except TimeoutError:
        return (None, "spb_skip_timeout")
    except Exception:
        return (None, "spb_skip_error")


# -------------------------------
# Extraction logic
# -------------------------------


def ensure_parent(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


def write_bytes(path: Path, b: bytes):
    ensure_parent(path)
    with open(path, "wb") as wf:
        wf.write(b)


def extract_entry_data(f: io.BufferedReader, base: int, e: NSAEntry) -> bytes:
    f.seek(base + e.rel_offset)
    data = f.read(e.stored_size)
    if len(data) != e.stored_size:
        raise EOFError(f"Unexpected EOF reading data for {e.name}")
    return data


def hexdump_preview(label: str, b: bytes, n: int = 128):
    print(f"[hexdump] {label} len={len(b)} first {min(n, len(b))} bytes:")
    h = binascii.hexlify(b[:n]).decode("ascii")
    print(" ".join(h[i : i + 2] for i in range(0, len(h), 2)))


def process_file_bytes(
    name: str,
    data: bytes,
    expanded_size: int,
    compression_flag: Optional[int],
    spb_mode: str,
    spb_timeout_ms: Optional[int],
    spb_skip_plausibility: bool = False,
    spb_skip_sizecheck: bool = False,
    spb_scan: str = "zigzag",
    spb_plane: str = "bgr",
) -> Tuple[Optional[bytes], bool, str]:
    """
    Returns (bytes_to_write, should_write, status_text).

    For .bmp:
      - Writes only if we have a real BMP (raw), LZSS->BMP, or SPB->BMP conversion.
      - Otherwise: should_write=False and a SKIP reason is returned (we do NOT write junk).
    For .nbz and others: write as before.
    """
    ext = Path(name).suffix.lower().lstrip(".")
    # Use compression flag (arc.md) as a helper
    flag = compression_flag or 0

    # NBZ audio: prefer to return decompressed WAV bytes
    if ext == "nbz":
        # Preserve legacy behavior for .nbz: write payload and let side-effect create .wav
        payload = data[4:] if len(data) >= 4 else data
        return (payload, True, "nbz_payload")
    if flag == 4:
        # Flagged NBZ but not named .nbz: return decompressed WAV bytes if possible
        for bz_off in (0, 4):
            if len(data) >= bz_off + 3 and data[bz_off : bz_off + 3] == b"BZh":
                try:
                    wav = bz2.decompress(data[bz_off:])
                    return (wav, True, "nbz_decompressed")
                except Exception:
                    pass
        payload = data[4:] if len(data) >= 4 else data
        return (payload, True, "nbz_payload")

    if ext == "bmp":
        # If flagged LZSS, try that path first; expect BMP output
        if flag == 2:
            try:
                lz_off = -1
                scan_max = min(16, len(data) - 1)
                for off in range(0, scan_max):
                    if data[off : off + 2] == b"\xa1\x53":
                        lz_off = off
                        break
                if lz_off >= 0:
                    out = lzss_decompress(
                        data, out_size=expanded_size, start_offset=lz_off
                    )
                    if len(out) >= 2 and out[:2] == b"BM":
                        return (out, True, "lzss_decompressed_flag")
            except Exception:
                pass

        # If flagged SPB, try SPB conversion first
        if flag == 1:
            plausible, w, h = spb_plausible(data)
            try:
                if plausible or spb_skip_plausibility:
                    if (
                        not spb_skip_sizecheck
                        and expanded_size
                        and expanded_size > 0
                        and len(data) >= 4
                    ):
                        expected = expected_24bpp_bmp_size(w, h)
                        if abs(expected - expanded_size) <= 8:
                            out = spb_to_bmp(
                                data,
                                timeout_ms=(
                                    spb_timeout_ms
                                    if spb_timeout_ms and spb_timeout_ms > 0
                                    else 1500
                                ),
                                scan=spb_scan,
                                plane=spb_plane,
                            )
                            return (out, True, "spb_converted_flag")
                    else:
                        out = spb_to_bmp(
                            data,
                            timeout_ms=(
                                spb_timeout_ms
                                if spb_timeout_ms and spb_timeout_ms > 0
                                else 1500
                            ),
                            scan=spb_scan,
                            plane=spb_plane,
                        )
                        return (out, True, "spb_converted_flag")
            except Exception:
                pass

        transformed, status = detect_and_process_bmp(
            data,
            expanded_size,
            # downstream heuristics still apply; flag already tried above
            spb_mode,
            spb_timeout_ms,
            spb_skip_plausibility,
            spb_skip_sizecheck,
            spb_scan,
            spb_plane,
        )

        if status == "raw_bmp":
            # already BMP -> keep original bytes
            return (data, True, status)

        if (
            status in ("lzss_decompressed", "bz2_decompressed", "spb_converted")
            and transformed is not None
        ):
            return (transformed, True, status)

        # All other statuses -> do not write this entry
        return (None, False, status)

    # default: passthrough
    return (data, True, "passthrough")


def _safe_reason(reason: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in reason)


def _skip_dest_path(save_root: Path, rel_name: str, reason: str) -> Path:
    rel = rel_name.replace("\\", os.sep).replace("/", os.sep)
    p = Path(rel)
    tag = _safe_reason(reason)
    newname = f"{p.stem}.skip-{tag}.bin"
    return save_root / p.parent / newname


def save_skip_bytes(save_root: Path, rel_name: str, raw: bytes, reason: str) -> Path:
    dest = _skip_dest_path(save_root, rel_name, reason)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as wf:
        wf.write(raw)
    return dest


def postprocess_side_effects(name: str, out_path: Path):
    """
    Sibling .wav next to .nbz using Python's bz2
    """
    if out_path.suffix.lower() == ".nbz":
        wav_path = out_path.with_suffix(".wav")
        try:
            wav = bz2.decompress(out_path.read_bytes())
            write_bytes(wav_path, wav)
        except Exception:
            # Ignore if not valid bzip2 or other error
            pass


def extract_volume(
    vol_path: Path,
    out_root: Path,
    hdr_skip: int,
    objcount_fallback: bool,
    only_index: Optional[int],
    hexdump_n: Optional[int],
    spb_mode: str,
    spb_timeout_ms: Optional[int],
    spb_skip_plausibility: bool = False,
    spb_skip_sizecheck: bool = False,
    spb_scan: str = "zigzag",
    spb_plane: str = "bgr",
    save_skips_dir: Optional[Path] = None,
):
    with open(vol_path, "rb") as f:
        header = parse_header(f, hdr_skip, objcount_fallback)
        entries = parse_entries(f, header.object_count)

        print(
            f"[{vol_path.name}] objects={header.object_count} base=0x{header.base_offset:08X}"
        )
        for i, e in enumerate(entries):
            if only_index is not None and i != only_index:
                continue
            try:
                raw = extract_entry_data(f, header.base_offset, e)
                if hexdump_n is not None:
                    print(
                        f"[debug] idx={i} name={e.name} stored={e.stored_size} expanded={e.expanded_size}"
                    )
                    hexdump_preview(e.name, raw, hexdump_n)

                out_bytes, should_write, status = process_file_bytes(
                    e.name,
                    raw,
                    e.expanded_size,
                    e.compression_flag,
                    spb_mode,
                    spb_timeout_ms,
                    spb_skip_plausibility=spb_skip_plausibility,
                    spb_skip_sizecheck=spb_skip_sizecheck,
                    spb_scan=spb_scan,
                    spb_plane=spb_plane,
                )

                if should_write and out_bytes is not None:
                    out_path = out_root / e.name.replace("\\", os.sep).replace(
                        "/", os.sep
                    )
                    write_bytes(out_path, out_bytes)
                    postprocess_side_effects(e.name, out_path)
                    print(
                        f"  #{i:04d} {e.name} [{status}] flag={e.compression_flag} off=0x{header.base_offset + e.rel_offset:08X} "
                        f"stored=0x{e.stored_size:08X} expanded=0x{e.expanded_size:08X}"
                    )
                else:
                    saved_msg = ""
                    if save_skips_dir is not None:
                        dest = save_skip_bytes(save_skips_dir, e.name, raw, status)
                        saved_msg = f" -> saved: {dest}"
                    print(
                        f"  #{i:04d} {e.name} SKIPPED ({status}) flag={e.compression_flag} off=0x{header.base_offset + e.rel_offset:08X} "
                        f"stored=0x{e.stored_size:08X} expanded=0x{e.expanded_size:08X}{saved_msg}"
                    )

            except Exception as ex:
                print(f"  ! #{i:04d} {e.name}: {ex}")

            if only_index is not None:
                break


# -------------------------------
# CLI
# -------------------------------


def build_volume_list(
    root: Path, prefix: str = "arc", ext: str = ".nsa", max_count: int = 100
) -> list[Path]:
    vols: list[Path] = []
    p0 = root / f"{prefix}{ext}"
    if p0.exists():
        vols.append(p0)
    for n in range(max_count):
        pn = root / f"{prefix}{n}{ext}"
        if pn.exists():
            vols.append(pn)
    return vols


def main(argv=None):
    ap = argparse.ArgumentParser(description="NSA archive extractor")
    ap.add_argument(
        "-i",
        "--input-dir",
        type=Path,
        default=Path("."),
        help="Directory containing arc.nsa / arc#.nsa",
    )
    ap.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("arc"),
        help="Output root directory",
    )
    ap.add_argument(
        "--max-volumes",
        type=int,
        default=100,
        help="Max numbered volumes to scan (arc0..arcN)",
    )

    ap.add_argument(
        "--hdr-skip",
        type=int,
        default=0,
        help="Skip N bytes before reading object_count",
    )
    ap.add_argument(
        "--objcount-fallback",
        action="store_true",
        help="If initial object_count is 0, read another u16 for object_count",
    )

    # Debug/triage
    ap.add_argument(
        "--only-index",
        type=int,
        default=None,
        help="Process only this entry index in each volume (debug)",
    )
    ap.add_argument(
        "--hexdump",
        type=int,
        default=None,
        help="Print first N bytes of the raw entry (debug)",
    )

    # SPB behavior
    ap.add_argument(
        "--spb-mode",
        choices=["auto", "convert", "copy"],
        default="auto",
        help="SPB conversion policy: auto (default), convert (force), copy (keep original)",
    )
    ap.add_argument(
        "--spb-timeout-ms",
        type=int,
        default=1500,
        help="Per-image SPB decode time budget in ms (auto/convert modes). 0 = unlimited.",
    )
    ap.add_argument(
        "--spb-skip-plausibility",
        action="store_true",
        help="Attempt SPB->BMP even if header looks implausible (advanced)",
    )
    ap.add_argument(
        "--spb-skip-sizecheck",
        action="store_true",
        help="Ignore expanded_size vs expected 24bpp BMP mismatch (advanced)",
    )
    ap.add_argument(
        "--spb-scan",
        choices=["zigzag", "linear"],
        default="zigzag",
        help="SPB pixel order mapping: zigzag (default) or linear",
    )
    ap.add_argument(
        "--spb-plane",
        choices=["bgr", "rgb"],
        default="bgr",
        help="SPB plane order: bgr (default) or rgb",
    )
    ap.add_argument(
        "--save-skips-dir",
        type=Path,
        default=None,
        help="If set, save original bytes of SKIPPED entries here as *.skip-<reason>.bin",
    )

    args = ap.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    vols = build_volume_list(
        args.input_dir, prefix="arc", ext=".nsa", max_count=args.max_volumes
    )
    if not vols:
        print(
            "No NSA volumes found (expected arc.nsa or arc0.nsa, etc.).",
            file=sys.stderr,
        )
        return 2

    for vol in vols:
        try:
            extract_volume(
                vol,
                args.output_dir,
                hdr_skip=args.hdr_skip,
                objcount_fallback=args.objcount_fallback,
                only_index=args.only_index,
                hexdump_n=args.hexdump,
                spb_mode=args.spb_mode,
                spb_timeout_ms=(
                    None if args.spb_timeout_ms == 0 else args.spb_timeout_ms
                ),
                spb_skip_plausibility=args.spb_skip_plausibility,
                spb_skip_sizecheck=args.spb_skip_sizecheck,
                spb_scan=args.spb_scan,
                spb_plane=args.spb_plane,
                save_skips_dir=args.save_skips_dir,
            )
        except Exception as ex:
            print(f"[{vol.name}] ERROR: {ex}", file=sys.stderr)

    print("Finished extracting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
