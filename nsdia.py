#!/usr/bin/env python3
"""
nsdia.py

Extract dialog text from nscript.dat/0.txt/...

- Converts '\' waits to newlines and removes '@' waits from dialogue

Usage:
  nsdia.py -i PATH/TO/SCRIPT_DIR [-o OUTPUT.txt]
"""

import argparse
import os
import sys
from nslex import Lexer, TK_CMD, TK_TEXT, TK_LABEL, TK_NL, TK_COMM


def skip_to_eol(lx: Lexer) -> None:
    """Advance the lexer's index to the end of the current line."""
    s = lx.buf
    n = lx.n
    i = lx.i
    while i < n and s[i] != "\n":
        i += 1
    lx.i = i


def clean_dialogue_text(s: str) -> str:
    # Normalize newlines first
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\\", "\n").replace("@", "")
    return s


def extract_dialogue(script_dir: str, clean: bool) -> str:
    lx = Lexer()
    lx.expand_in_text = False
    if lx.open(script_dir) != 0:
        print(f"[error] failed to open: {script_dir}", file=sys.stderr)
        sys.exit(1)
    out_chunks = []
    last_ended_with_nl = True  # helps avoid accidental word joins across tokens
    while True:
        t = lx.next()
        if t.kind == "eof":
            break
        # Ignore labels, comments, explicit newlines, and any commands/control
        if t.kind == TK_LABEL or t.kind == TK_COMM or t.kind == TK_NL:
            continue
        if t.kind == TK_CMD:
            # Do not parse args or execute control flow
            skip_to_eol(lx)
            continue
        if t.kind == TK_TEXT:
            if clean:
                text = clean_dialogue_text(t.text)
            else:
                text = t.text
            if not text:
                continue
            # If the previous chunk didn't end with a newline and this one doesn't
            # start with one, add a soft separator to avoid gluing words together.
            if out_chunks and not last_ended_with_nl and not text.startswith("\n"):
                out_chunks.append("\n")
            out_chunks.append(text)
            last_ended_with_nl = text.endswith("\n")
            continue
        # Anything else: ignore
    # Join and normalize multiple blank lines lightly
    result = "".join(out_chunks)
    return result


def main():
    ap = argparse.ArgumentParser(
        description="Extract dialog text from nscript.dat/0.txt/..."
    )
    ap.add_argument("-i", "--input", required=True, help="Path to the script directory")
    ap.add_argument(
        "-o",
        "--output",
        help="Path to write the extracted dialogues (defaults to stdout)",
    )
    ap.add_argument("--no-clean", help="Keep \\ and @ in dialog", action="store_true")
    args = ap.parse_args()

    script_dir = os.path.abspath(args.input)
    dialogues = extract_dialogue(script_dir, args.no_clean)

    if args.output:
        out_path = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(dialogues)
    else:
        sys.stdout.write(dialogues)


if __name__ == "__main__":
    main()
