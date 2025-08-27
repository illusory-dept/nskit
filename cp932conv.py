#!/usr/bin/env python3
"""
cp932conv.py
Convert text files from a given single-byte encoding (default: CP932) to UTF-8.

USAGE
=====
File mode (verify encoding, then convert):
    python cp932_to_utf8_refactor.py -i /path/to/file.txt

    # choose a different output path
    python cp932_to_utf8_refactor.py -i /path/to/file.txt -o /path/to/out/name.utf8.txt

    # send to an output directory (filename becomes <basename>.utf8.txt)
    python cp932_to_utf8_refactor.py -i /path/to/file.txt -o /path/to/output_dir

Directory mode (non-recursive; confirm or skip with --yes):
    python cp932_to_utf8_refactor.py -i /path/to/folder
    python cp932_to_utf8_refactor.py -i /path/to/folder --yes

    # only convert .txt (default); change the extension filter:
    python cp932_to_utf8_refactor.py -i /path/to/folder --extension .csv
    python cp932_to_utf8_refactor.py -i /path/to/folder --extension csv
    # process all files regardless of extension:
    python cp932_to_utf8_refactor.py -i /path/to/folder --extension ""

    # specify output directory (replaces default "utf8 [<timestamp>]")
    python cp932_to_utf8_refactor.py -i /path/to/folder -o /path/to/output_dir --yes

Try a different source encoding (default: cp932):
    python cp932_to_utf8_refactor.py -i file.txt --encoding euc_jp

Behavior
========
- If -i/--input points to a *file*: verify it can be decoded using the chosen encoding
  (default cp932). If decodable, write UTF-8 to <basename>.utf8.txt (or to -o if provided).
  If not decodable, skip and print a message.
- If -i/--input points to a *directory*: confirm in CLI unless --yes is provided.
  Attempts to convert every regular file directly inside that directory (no subfolders),
  filtered by --extension (default ".txt"; empty string disables filtering). Results are
  saved to a folder named "utf8 [YYYYmmdd-HHMMSS]" inside the input directory, unless
  -o/--output is provided to override the destination folder.
- For each file:
    * If it decodes with the chosen encoding, save as <basename>.utf8.txt and print a success line.
    * If it does not decode, skip and print a skip message.

Notes
=====
- "cp932" and "shift_jis" are often interchangeable; this script defaults to "cp932".
- Newlines are normalized by Python's text I/O; output uses UTF-8 with default newline handling.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple


def try_decode(path: Path, encoding: str) -> Tuple[bool, Optional[str]]:
    """Attempt to read file *strictly* with the given encoding.

    Returns (True, text) on success, or (False, None) on failure.
    """
    try:
        with path.open("r", encoding=encoding, errors="strict") as f:
            return True, f.read()
    except UnicodeDecodeError:
        return False, None
    except Exception:
        # Propagate non-decoding errors (e.g., permission) to caller
        raise


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def output_name_for(src: Path, out_arg: Optional[Path]) -> Path:
    """Resolve the destination path for *file mode*.

    - If out_arg is a file path (has a suffix or points to non-existent but endswith .txt), use it.
    - If out_arg is an existing directory, place <basename>.utf8.txt inside it.
    - If out_arg is None, use src.parent/<basename>.utf8.txt.
    """
    default_name = f"{src.stem}.utf8.txt"
    if out_arg is None:
        return src.with_name(default_name)

    if out_arg.exists() and out_arg.is_dir():
        return out_arg / default_name

    # If user provided a path that looks like a file (has suffix) or doesn't exist yet,
    # honor it as a file destination.
    return out_arg


def convert_text_to_utf8(text: str, dest: Path) -> None:
    ensure_parent_dir(dest)
    with dest.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def convert_single_file(src: Path, encoding: str, out_arg: Optional[Path]) -> None:
    ok, text = try_decode(src, encoding)
    if not ok:
        print(f"Skipped (not {encoding}): {src}")
        return

    dest = output_name_for(src, out_arg)
    convert_text_to_utf8(text or "", dest)
    print(f"Converted: {src} -> {dest}")


def timestamped_utf8_dir(base_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return base_dir / f"utf8 [{stamp}]"


def normalize_extension(ext: Optional[str]) -> Optional[str]:
    """Normalize an extension filter.

    - None => default behavior handled by caller
    - "" or whitespace => no filtering (process all files)
    - otherwise ensure it starts with '.' and is lowercase
    """
    if ext is None:
        return None
    ext = ext.strip()
    if ext == "":
        return ""
    if not ext.startswith("."):
        ext = "." + ext
    return ext.lower()


def convert_directory(
    dir_path: Path,
    encoding: str,
    out_dir_arg: Optional[Path],
    assume_yes: bool,
    ext_filter: Optional[str],
) -> None:
    # Confirm unless --yes provided
    if not assume_yes:
        reply = (
            input(
                "specified folder as input. would you like to try to convert all files in this directory? (non-recursive) [y/N]: "
            )
            .strip()
            .lower()
        )
        if reply not in {"y", "yes"}:
            print("Aborted by user.")
            return

    # Determine destination root
    dest_root = out_dir_arg if out_dir_arg else timestamped_utf8_dir(dir_path)
    dest_root.mkdir(parents=True, exist_ok=True)

    # Normalize extension filter (default to '.txt')
    ext_norm = normalize_extension(ext_filter)
    if ext_norm is None:
        ext_norm = ".txt"

    any_converted = False
    total_seen = 0
    for p in sorted(dir_path.iterdir()):
        if not p.is_file():
            continue
        if ext_norm != "":  # if not disabled
            if p.suffix.lower() != ext_norm:
                continue
        total_seen += 1
        ok, text = try_decode(p, encoding)
        if not ok:
            print(f"Skipped (not {encoding}): {p.name}")
            continue
        dest = dest_root / f"{p.stem}.utf8.txt"
        convert_text_to_utf8(text or "", dest)
        print(f"Converted: {p.name} -> {dest.relative_to(dest_root.parent)}")
        any_converted = True

    if total_seen == 0:
        if ext_norm == "":
            print("No regular files found.")
        else:
            print(f"No files with extension '{ext_norm}' found.")
    elif not any_converted:
        print("No files were converted.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify and convert CP932/Shift-JIS (or another specified encoding) text files to UTF-8. "
            "Works on a single file or a non-recursive directory."
        )
    )
    parser.add_argument(
        "-i",
        "--input",
        required=True,
        type=Path,
        help="File or folder path to process.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=(
            "Output destination. For file mode: a file path or an existing directory. "
            "For folder mode: an output directory (replaces the default 'utf8 [<timestamp>]' folder)."
        ),
    )
    parser.add_argument(
        "--encoding",
        default="cp932",
        help="Source encoding to try (default: cp932).",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Do not prompt for confirmation in folder mode (assume 'yes').",
    )
    parser.add_argument(
        "--extension",
        default=".txt",
        help=(
            "When input is a directory, only process files with this extension (default: .txt). "
            "Pass an empty string to process all files. Leading dot is optional."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    input_path: Path = args.input
    output_path: Optional[Path] = args.output
    encoding: str = args.encoding
    assume_yes: bool = args.yes
    ext_filter: Optional[str] = args.extension

    if not input_path.exists():
        print(f"Input path does not exist: {input_path}", file=sys.stderr)
        return 1

    if input_path.is_file():
        # In file mode, always attempt the given file regardless of its extension.
        convert_single_file(input_path, encoding, output_path)
        return 0

    if input_path.is_dir():
        if output_path is not None and output_path.exists() and output_path.is_file():
            print(
                f"-o/--output must be a directory when input is a directory: {output_path}",
                file=sys.stderr,
            )
            return 1
        convert_directory(input_path, encoding, output_path, assume_yes, ext_filter)
        return 0

    print(f"Unsupported input type: {input_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
