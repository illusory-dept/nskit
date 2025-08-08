import os
import argparse
from pathlib import Path


def convert_file(src_path: str, dest_path: str) -> None:
    """
    Read a .txt file encoded in Shift-JIS and write it out as UTF-8.

    :param src_path: Path to the source .txt file (Shift-JIS encoded)
    :param dest_path: Path where the UTF-8 file will be written
    """
    # Ensure destination directory exists
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    # Read using Shift-JIS encoding
    with open(src_path, "r", encoding="shift_jis", errors="strict") as src_file:
        content = src_file.read()

    # Write using UTF-8 encoding
    with open(dest_path, "w", encoding="utf-8", newline="") as dest_file:
        dest_file.write(content)


def main():
    ap = argparse.ArgumentParser(
        description='Convert all Shift-JIS encoded .txt files in a folder to UTF-8 and store them in a subfolder named "utf8".'
    )
    ap.add_argument(
        "input_folder", help="Path to the folder containing Shift-JIS .txt files"
    )
    ap.add_argument(
        "-i",
        "--input-dir",
        type=Path,
        default=Path("."),
        help="Directory containing arc.nsa / arc#.nsa",
    )
    args = ap.parse_args()

    input_folder = os.path.abspath(args.input_folder)
    output_folder = os.path.join(input_folder, "utf8")

    for root, dirs, files in os.walk(input_folder):
        # Skip the output folder to avoid re-processing converted files
        if os.path.abspath(root).startswith(os.path.abspath(output_folder)):
            continue

        for filename in files:
            # Only process .txt files
            if not filename.lower().endswith(".txt"):
                continue

            src_file_path = os.path.join(root, filename)

            # Determine the relative path and destination path
            rel_path = os.path.relpath(src_file_path, input_folder)
            dest_file_path = os.path.join(output_folder, rel_path)

            try:
                convert_file(src_file_path, dest_file_path)
                print(f"Converted: {rel_path}")
            except Exception as e:
                print(f"Skipped: {rel_path} (error: {e})")


if __name__ == "__main__":
    main()
