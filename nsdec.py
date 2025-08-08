#!/usr/bin/env python3
import os
import argparse
import codecs

BSIZE = 32  # read/write chunk size
KEY = 0x84  # XOR key


def encrypt(src_path, out_path):
    """XOR each byte with KEY and write raw bytes."""
    with open(src_path, "rb") as src, open(out_path, "wb") as out:
        while True:
            chunk = src.read(BSIZE)
            if not chunk:
                break
            out.write(bytes(b ^ KEY for b in chunk))


def decrypt_and_convert(src_path, out_path):
    """
    XOR each byte with KEY, then decode from Shift-JIS and write as UTF-8 text.
    Uses an incremental decoder to handle split multibyte sequences.
    """
    decoder = codecs.getincrementaldecoder("shift_jis")("replace")
    with (
        open(src_path, "rb") as src,
        open(out_path, "w", encoding="utf-8", errors="replace") as out,
    ):
        while True:
            chunk = src.read(BSIZE)
            if not chunk:
                break
            # undo XOR
            decoded_bytes = bytes(b ^ KEY for b in chunk)
            # incrementally decode and write
            out.write(decoder.decode(decoded_bytes))
        # flush any buffered state
        out.write(decoder.decode(b"", final=True))


def main():
    p = argparse.ArgumentParser(description="Encrypt/decrypt with 0x84-XOR")
    p.add_argument("-i", "--input-dir", help="Path to .dat (decrypt) or .txt (encrypt)")
    p.add_argument("-o", "--output-dir", help="Optional output path")
    args = p.parse_args()

    src = args.input_dir
    if not os.path.isfile(src):
        p.error(f"File not found: {src}")

    base, ext = os.path.splitext(src)
    ext = ext.lower()
    if args.output_dir:
        out = args.output_dir
    elif ext == ".dat":
        out = base + ".txt"
    elif ext == ".txt":
        out = base + ".dat"
    else:
        p.error("Unsupported extension; must be .dat or .txt")

    if ext == ".dat":
        decrypt_and_convert(src, out)
        print(
            f"{os.path.basename(src)} decrypted and converted to UTF-8 -> {os.path.basename(out)}"
        )
    else:
        encrypt(src, out)
        print(f"{os.path.basename(src)} encrypted -> {os.path.basename(out)}")


if __name__ == "__main__":
    main()
