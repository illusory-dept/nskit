import argparse
import sys
import time
import os
import subprocess
from nslex import Lexer, TK_CMD, TK_TEXT, TK_LABEL, TK_NL, TK_COMM

ASSETS = None
BGM_DIR = None
bgm_proc = None


def skip_ws(s, i):
    n = len(s)
    while i < n and s[i] in (" ", "\t"):
        i += 1
    return i


def prepass_decls(lx):
    pos, line, end = lx.i, lx.line, lx.end
    lx.seek(0)
    while True:
        t = lx.next()
        if t.kind == "eof":
            break
        if t.kind != "cmd":
            continue
        if t.text == "numalias":
            name = lx.read_ident()
            val = lx.read_int()
            if name is not None:
                lx.add_numalias(name, val)
        elif t.text == "dim":
            lx.declare_dim()
    lx.i, lx.line, lx.end = pos, line, end


def prepass_labels(lx):
    # trust lexer's label index
    labels = {}
    for li in lx.labels_all():
        # li.name has no leading '*', li.start is address of label body
        if getattr(li, "start", -1) >= 0 and getattr(li, "name", None):
            labels["*" + li.name] = (li.start, 0, 0)  # we don't need line/end
    return labels


def read_args(lx):
    args = []
    while True:
        s = lx.buf
        i0 = lx.i
        i = skip_ws(s, i0)
        if i >= lx.n:
            break
        ch = s[i]
        if ch in ("\n", ":", "~", ";", "*"):
            if ch == "*":
                lx.i = i
            break
        if ch in ('"', "$", "#", "(", "*"):
            val = lx.read_str()
            args.append(val)
        else:
            before = lx.i
            val = lx.read_int()
            if lx.i == before:
                break
            args.append(val)
        if lx.i == i0:
            break
    return args


def typewriter(text, cps=40, color=None):
    # enter during typing -> finish sentence up to next @ or \
    # press/hold 'f' -> fast forward
    delay_base = 1.0 / float(cps) if cps > 0 else 0
    out = sys.stdout
    i = 0
    n = len(text)
    fast = False

    import termios
    import tty
    import select

    class Raw:
        def __enter__(self):
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            return self

        def __exit__(self, *a):
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def key_pressed(timeout=0):
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(r)

    def read_key():
        try:
            return sys.stdin.read(1)
        except OSError:
            return "\n"

    def flush_to_wait(j):
        nxt1 = text.find("@", j)
        nxt2 = text.find("\\", j)
        cut = n
        if nxt1 != -1:
            cut = min(cut, nxt1)
        if nxt2 != -1:
            cut = min(cut, nxt2)
        if cut > j:
            out.write(text[j:cut])
            out.flush()
        return cut

    # ANSI color codes
    def color_seq(c):
        if c is None:
            return "", ""
        # Map c0..c9 to terminal colors. c0: default (no color)
        m = {
            0: ("", ""),
            1: ("\x1b[31m", "\x1b[0m"),  # red
            2: ("\x1b[32m", "\x1b[0m"),  # green
            3: ("\x1b[33m", "\x1b[0m"),  # yellow
            4: ("\x1b[34m", "\x1b[0m"),  # blue
            5: ("\x1b[35m", "\x1b[0m"),  # magenta
            6: ("\x1b[36m", "\x1b[0m"),  # cyan
            7: ("\x1b[37m", "\x1b[0m"),  # white
            8: ("\x1b[96m", "\x1b[0m"),  # bright cyan
            9: ("\x1b[95m", "\x1b[0m"),  # bright magenta
        }
        return m.get(c, ("", ""))

    prefix, reset = color_seq(color)
    color_active = False

    with Raw():
        while i < n:
            if key_pressed(0):
                k = read_key()
                if k in ("\r", "\n"):
                    i = flush_to_wait(i)
                    continue
                elif k and k.lower() == "f":
                    fast = True
                else:
                    fast = False

            ch = text[i]
            if ch == "@":
                out.flush()
                if color_active:
                    out.write(reset)
                    out.flush()
                    color_active = False
                try:
                    input()
                except EOFError:
                    pass
                i += 1
                # resume color after wait if more text pending
                if i < n and prefix:
                    out.write(prefix)
                    color_active = True
                continue
            if ch == "\\":
                out.write("\n")
                out.flush()
                if color_active:
                    out.write(reset)
                    out.flush()
                    color_active = False
                try:
                    input()
                except EOFError:
                    pass
                i += 1
                if i < n and prefix:
                    out.write(prefix)
                    color_active = True
                continue

            if prefix and not color_active:
                out.write(prefix)
                color_active = True
            out.write(ch)
            out.flush()
            if not fast and delay_base:
                time.sleep(delay_base)
            i += 1

    if color_active:
        out.write(reset)
        out.flush()


# --- bgm support via ffplay ---


def find_bgm_dir(base):
    # find folder named "bgm" case-insensitive inside base
    for root, dirs, files in os.walk(base):
        for d in dirs:
            if d.lower() == "bgm":
                return os.path.join(root, d)
    return None


def stop_bgm():
    global bgm_proc
    if bgm_proc and bgm_proc.poll() is None:
        try:
            bgm_proc.terminate()
        except Exception:
            pass
        bgm_proc = None


def play_bgm(name):
    global bgm_proc, BGM_DIR
    if not ASSETS:
        return  # skip silently when no assets
    if not BGM_DIR:
        BGM_DIR = find_bgm_dir(ASSETS)
        if not BGM_DIR:
            print("[bgm] bgm folder not found under assets. skipping.")
            return

    # normalize
    rel = name.replace("\\", os.sep).replace("/", os.sep)
    # try direct under assets
    cand = os.path.join(ASSETS, rel)
    if not os.path.isfile(cand):
        # try inside bgm dir
        cand = os.path.join(BGM_DIR, os.path.basename(rel))
        if not os.path.isfile(cand):
            # search recursively in bgm for filename (case-insensitive)
            target = os.path.basename(rel).lower()
            hit = None
            for root, dirs, files in os.walk(BGM_DIR):
                for f in files:
                    if f.lower() == target:
                        hit = os.path.join(root, f)
                        break
                if hit:
                    break
            cand = hit if hit else cand

    if not cand or not os.path.isfile(cand):
        print(f"[bgm] not found: {name}")
        return

    stop_bgm()
    try:
        bgm_proc = subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", cand],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[bgm] playing {os.path.relpath(cand, ASSETS)}")
    except FileNotFoundError:
        print("[bgm] ffplay not found. install ffmpeg.")
    except Exception as e:
        print(f"[bgm] failed: {e}")


# --- parsing helpers for if ---


def _peek_nonspace(lx):
    s = lx.buf
    n = lx.n
    i = lx.i
    while i < n and s[i] in (" ", "\t"):
        i += 1
    return s[i] if i < n else "\n", i


def skip_to_eol(lx):
    s = lx.buf
    n = lx.n
    i = lx.i
    while i < n and s[i] != "\n":
        i += 1
    lx.i = i


def parse_if_cond(lx):
    lhs = lx.read_int()
    ch, i = _peek_nonspace(lx)
    op = None
    j = i
    s = lx.buf
    n = lx.n
    if j < n and s[j] == "=":
        if j + 1 < n and s[j + 1] == "=":
            op = "=="
            j += 2
        else:
            op = "="
            j += 1
    elif j < n and s[j] == "!":
        if j + 1 < n and s[j + 1] == "=":
            op = "!="
            j += 2
    elif j < n and s[j] == ">":
        if j + 1 < n and s[j + 1] == "=":
            op = ">="
            j += 2
        else:
            op = ">"
            j += 1
    elif j < n and s[j] == "<":
        if j + 1 < n and s[j + 1] == "=":
            op = "<="
            j += 2
        else:
            op = "<"
            j += 1
    else:
        return lhs != 0
    lx.i = j
    rhs = lx.read_int()
    if op == "=" or op == "==":
        return lhs == rhs
    elif op == "!=":
        return lhs != rhs
    elif op == ">":
        return lhs > rhs
    elif op == "<":
        return lhs < rhs
    elif op == ">=":
        return lhs >= rhs
    elif op == "<=":
        return lhs <= rhs
    return False


def run(
    arc,
    label=None,
    cps=40,
    show_labels=False,
    show_comments=False,
    assets=None,
    use_color=False,
):
    global ASSETS, BGM_DIR
    ASSETS = assets
    BGM_DIR = None

    lx = Lexer()
    lx.expand_in_text = True  # todo!!!

    if lx.open(arc) != 0:
        print("[system] failed to open", arc, file=sys.stderr)
        return 1

    prepass_decls(lx)
    labels = prepass_labels(lx)
    callstack = []

    if not ASSETS:
        print(
            "[system] no asset folder provided. skipping commands that require asset."
        )

    if show_labels:
        print("[system] =====LABELS=====")
        for k, v in labels.items():
            print(k, end=" ")
        print()

    if label:
        tgt = label.lstrip("*")
        try:
            lx.jump_label(tgt)
            print(f"[system] label *{tgt} found.")
        except SystemExit:
            print("[system] label not found:", "*" + tgt, file=sys.stderr)

    print(f"[system] mode {lx.sw}x{lx.sh} vars {lx.var_rng} globals {lx.glob_border}")

    input("[system] Press enter to start")
    while True:
        t = lx.next()
        if t.kind == "eof":
            break

        if t.kind == TK_LABEL:
            if show_labels:
                print(f"[label] {t.text}")
            continue

        if t.kind == TK_COMM:
            if show_comments:
                sys.stdout.write(t.text)
            continue

        if t.kind == TK_NL:
            continue

        if t.kind == TK_TEXT:
            col = t.color if use_color else None
            typewriter(t.text, cps=cps, color=col)
            continue

        if t.kind == TK_CMD:
            name = t.text
            args = read_args(lx)
            pretty = " ".join(repr(a) if isinstance(a, str) else str(a) for a in args)
            print(f"[cmd] {name} {pretty}".rstrip())

            # control flow
            # control flow
            # control flow
            if name in ("goto", "jump"):
                if args and isinstance(args[0], str):
                    tgt = args[0].lstrip("*")
                    try:
                        lx.jump_label(tgt)
                        continue
                    except SystemExit:
                        print(f"[warn] label *{tgt} not found")
                continue

            if name == "gosub":
                if args and isinstance(args[0], str):
                    tgt = args[0].lstrip("*")
                    try:
                        callstack.append((lx.i, lx.line, lx.end))
                        lx.jump_label(tgt)
                        continue
                    except SystemExit:
                        print(f"[warn] label *{tgt} not found")
                continue
            if name == "return":
                if callstack:
                    lx.i, lx.line, lx.end = callstack.pop()
                else:
                    print("[warn] return with empty callstack")
                continue

            # if inline
            if name == "if":
                ok = parse_if_cond(lx)
                if not ok:
                    skip_to_eol(lx)
                    continue
                t2 = lx.next()
                if t2.kind == TK_CMD:
                    name2 = t2.text
                    args2 = read_args(lx)
                    pretty2 = " ".join(
                        repr(a) if isinstance(a, str) else str(a) for a in args2
                    )
                    print(f"[cmd] {name2} {pretty2}".rstrip())
                    # allow control
                    if name2 in ("goto", "jump"):
                        if args2 and isinstance(args2[0], str):
                            tgt = args2[0].lstrip("*")
                            try:
                                lx.jump_label(tgt)
                            except SystemExit:
                                print(f"[warn] label *{tgt} not found")
                        continue
                    if name2 == "gosub":
                        if args2 and isinstance(args2[0], str):
                            tgt = args2[0].lstrip("*")
                            try:
                                callstack.append((lx.i, lx.line, lx.end))
                                lx.jump_label(tgt)
                            except SystemExit:
                                print(f"[warn] label *{tgt} not found")
                        continue
                    if name2 == "return":
                        if callstack:
                            lx.i, lx.line, lx.end = callstack.pop()
                        continue
                continue

            # bgm
            if name == "bgm":
                if not ASSETS:
                    # skip silently; we already printed message at start
                    continue
                if args:
                    # first arg is path or filename
                    if isinstance(args[0], str) and args[0]:
                        play_bgm(args[0])
                continue
            if name == "bgmstop":
                stop_bgm()
                continue

            # waits
            if name == "wait":
                ms = args[0] if args else None
                if isinstance(ms, int) and ms > 0:
                    time.sleep(ms / 1000.0)
                else:
                    try:
                        input()
                    except EOFError:
                        pass
                continue
            if name in ("click", "wt"):
                try:
                    input()
                except EOFError:
                    pass
                continue

    stop_bgm()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Run nscli script with optional settings"
    )
    parser.add_argument(
        "-i", "--input", required=True, help="Path to the script directory"
    )
    parser.add_argument("-l", "--label", help="Entry point label")
    parser.add_argument(
        "--cps",
        type=int,
        default=int(os.environ.get("CPS", "40")),
        help="Characters per second (default from CPS env or 40)",
    )
    parser.add_argument("--show-labels", action="store_true", help="Show labels")
    parser.add_argument("--comments", action="store_true", help="Show comments")
    parser.add_argument("-a", "--assets", help="Assets directory")
    parser.add_argument(
        "-c",
        "--color",
        action="store_true",
        help="Use text color from script (Ponscripter ^~cX~)",
    )

    args = parser.parse_args()

    assets_path = os.path.abspath(args.assets) if args.assets else None

    return run(
        args.input,
        args.label,
        args.cps,
        args.show_labels,
        args.comments,
        assets_path,
        use_color=args.color,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nExiting on Ctrl-C")
        sys.exit(0)
    except EOFError:
        print("\nExiting on Ctrl-D")
        sys.exit(0)
