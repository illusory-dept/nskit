#!/usr/bin/env python3
"""
nsdec.py
Encrypt and decrypt files using a fixed 0x84 XOR key.

Examples
========
  # Decrypt a .dat file (default cp932 -> UTF-8)
  ./nsdec.py -i nscript.dat

  # Decrypt a .dat file using shift_jis decoding
  ./nsdec.py -i nscript.dat --open-with-encoding shift_jis

  # Decrypt without converting to utf-8
  ./nsdec.py -i nscript.dat --no-conv

  # Encrypt a .txt file back into a .dat
  ./nsdec.py -i nscript.txt

  # In-place edit a .dat with no conversion (validates CP932 and opens VS Code)
  ./nsdec.py -i nscript.dat --edit-in-place

  # Use a custom editor command that waits for close (must block until exit)
  ./nsdec.py -i nscript.dat --edit-in-place --editor "code -w"
"""

import os
import argparse
import codecs
import sys
import tempfile
import subprocess
import shlex
import shutil

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


def decrypt_and_convert(src_path, out_path, encoding="cp932"):
    """
    XOR each byte with KEY, then decode from 'encoding' and write as UTF-8 text.
    Uses an incremental decoder to handle split multibyte sequences.
    """
    decoder = codecs.getincrementaldecoder(encoding)("replace")
    with (
        open(src_path, "rb") as src,
        open(out_path, "w", encoding="utf-8", errors="replace") as out,
    ):
        while True:
            chunk = src.read(BSIZE)
            if not chunk:
                break
            decoded_bytes = bytes(b ^ KEY for b in chunk)
            out.write(decoder.decode(decoded_bytes))
        out.write(decoder.decode(b"", final=True))


def decrypt_no_convert_with_cp932_check(src_path, out_path):
    """
    XOR each byte with KEY and write raw bytes (no text conversion).
    Simultaneously attempt to validate the XORed bytes as cp932 text using a strict decoder.
    Returns (is_valid_cp932, first_error_offset or None).
    """
    decoder = codecs.getincrementaldecoder("cp932")("strict")
    total_offset = 0
    first_error_offset = None
    is_valid = True

    with open(src_path, "rb") as src, open(out_path, "wb") as out:
        while True:
            chunk = src.read(BSIZE)
            if not chunk:
                break
            xored = bytes(b ^ KEY for b in chunk)
            out.write(xored)

            if is_valid:
                try:
                    # Validate incrementally; discard the decoded text
                    decoder.decode(xored, final=False)
                except UnicodeDecodeError as e:
                    is_valid = False
                    # e.start is the index in this chunk where the error occurred
                    first_error_offset = total_offset + e.start
            total_offset += len(chunk)

    if is_valid:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as e:
            is_valid = False
            first_error_offset = total_offset + e.start

    return is_valid, first_error_offset


def run_editor_and_reencrypt(original_dat_path, editor_cmd):
    """Decrypt .dat to a temp .txt (raw CP932 bytes), open editor, re-encrypt back in place."""
    base = os.path.splitext(os.path.basename(original_dat_path))[0]
    with tempfile.TemporaryDirectory(prefix="nsdec_") as td:
        tmp_txt = os.path.join(td, base + ".txt")
        ok, pos = decrypt_no_convert_with_cp932_check(original_dat_path, tmp_txt)
        if not ok:
            where = f"at byte offset {pos}" if pos is not None else "at unknown offset"
            print(
                f"[warning] Decrypted bytes are not valid CP932 {where}.\n"
                f"          In-place edit aborted. You can inspect: {tmp_txt}",
                file=sys.stderr,
            )
            return 2

        # Build editor command; must block until file is closed.
        if not editor_cmd:
            editor_cmd = "code -w"
        try:
            cmd = shlex.split(editor_cmd)
        except ValueError as e:
            print(f"Invalid --editor command: {e}", file=sys.stderr)
            return 2

        # Ensure the executable is available
        exe = shutil.which(cmd[0])
        if exe is None:
            print(f"Editor command not found: {cmd[0]}", file=sys.stderr)
            return 2

        # Launch editor and wait
        try:
            subprocess.run(cmd + [tmp_txt], check=True)
        except subprocess.CalledProcessError as e:
            print(
                f"Editor exited with non-zero status: {e.returncode}", file=sys.stderr
            )
            return e.returncode or 2
        except KeyboardInterrupt:
            print("[info] Edit canceled by user.")
            return 1

        # Re-encrypt edited text back into the original .dat atomically
        tmp_dat = os.path.join(td, base + ".dat")
        encrypt(tmp_txt, tmp_dat)
        try:
            # Keep a backup next to the original
            backup_path = original_dat_path + ".bak"
            try:
                shutil.copy2(original_dat_path, backup_path)
                print(f"[info] Backup written: {os.path.basename(backup_path)}")
            except Exception as e:
                print(f"[warning] Could not create backup: {e}", file=sys.stderr)
            os.replace(tmp_dat, original_dat_path)
        except Exception as e:
            print(f"[error] Failed to write updated .dat: {e}", file=sys.stderr)
            return 2

        print(f"{os.path.basename(original_dat_path)} updated via in-place edit.")
        return 0


def main():
    p = argparse.ArgumentParser(description="Encrypt/decrypt with 0x84-XOR")
    p.add_argument(
        "-i", "--input", required=True, help="Path to .dat (decrypt) or .txt (encrypt)"
    )
    p.add_argument(
        "-o",
        "--output",
        help=(
            "Output *file path*. If omitted: .dat→<base>.txt, .txt→<base>.dat. "
            "Supplying a directory is not allowed."
        ),
    )
    p.add_argument(
        "--no-conv",
        action="store_true",
        help=(
            "When decrypting .dat -> write XORed raw bytes with no text conversion. "
            "Also validates as cp932."
        ),
    )
    p.add_argument(
        "--open-with-encoding",
        metavar="ENCODING",
        help=(
            "When decrypting .dat -> decode XORed bytes using ENCODING to UTF-8 (default: cp932)"
        ),
    )
    p.add_argument(
        "--edit-in-place",
        "-e",
        action="store_true",
        help=(
            "In-place edit a .dat without converting encodings. Decrypts to a temp CP932 text file, "
            "validates CP932, opens editor, then re-encrypts back to the original .dat."
        ),
    )
    p.add_argument(
        "--editor",
        help=(
            "Editor command that waits for the file to close (default: 'code -w'). "
            "Must block until the editor exits."
        ),
    )
    args = p.parse_args()

    src = args.input
    if not os.path.isfile(src):
        p.error(f"File not found: {src}")

    base, ext = os.path.splitext(src)
    ext = ext.lower()

    # --edit-in-place: only valid for .dat inputs, ignores output and encoding flags
    if args.edit_in_place:
        if ext != ".dat":
            p.error("--edit-in-place works only with .dat inputs")
        if args.open_with_encoding:
            print(
                "[note] --edit-in-place ignores --open-with-encoding.", file=sys.stderr
            )
        if args.no_conv:
            print(
                "[note] --edit-in-place implies no conversion; --no-conv not needed.",
                file=sys.stderr,
            )
        rc = run_editor_and_reencrypt(src, args.editor)
        sys.exit(rc)

    # Validate output path semantics (if provided, it must be a file path, not a directory)
    if args.output and os.path.isdir(args.output):
        p.error("--output must be a file path, not a directory")

    # Determine output path when not editing in place
    if args.output:
        out = args.output
    elif ext == ".dat":
        out = base + ".txt"
    elif ext == ".txt":
        out = base + ".dat"
    else:
        p.error("Unsupported extension; must be .dat or .txt")

    if ext == ".dat":
        if args.no_conv:
            if args.open_with_encoding:
                print(
                    "[note] --no-conv specified; ignoring --open-with-encoding.",
                    file=sys.stderr,
                )
            ok, pos = decrypt_no_convert_with_cp932_check(src, out)
            if ok:
                print(
                    f"{os.path.basename(src)} decrypted (no conversion) -> {os.path.basename(out)} [valid cp932 text]"
                )
            else:
                where = (
                    f"at byte offset {pos}" if pos is not None else "at unknown offset"
                )
                print(
                    f"{os.path.basename(src)} decrypted (no conversion) -> {os.path.basename(out)} "
                    f"[warning: not valid cp932 {where}]",
                    file=sys.stderr,
                )
        else:
            enc = args.open_with_encoding or "cp932"
            # Validate encoding early for a clearer error
            try:
                codecs.lookup(enc)
            except LookupError:
                p.error(f"Unknown encoding for --open-with-encoding: {enc}")
            decrypt_and_convert(src, out, encoding=enc)
            print(
                f"{os.path.basename(src)} decrypted ({enc} -> UTF-8) -> {os.path.basename(out)}"
            )
    else:
        encrypt(src, out)
        print(f"{os.path.basename(src)} encrypted -> {os.path.basename(out)}")


if __name__ == "__main__":
    main()
