import os
import sys
import argparse
from typing import Optional

# flags
END_NONE = 0
END_COMMA = 1 << 0
END_1BYTE = 1 << 1

# ops
PLUS, MINUS, MULT, DIV, MOD, INV = 1, 2, 3, 4, 5, 0

# token kinds
TK_EOF = "eof"
TK_NL = "nl"
TK_TEXT = "text"
TK_CMD = "cmd"
TK_LABEL = "label"
TK_MARK = "mark"  # '~' ':' etc
TK_COMM = "comment"

# var kinds
VK_NONE = 0
VK_INT = 1
VK_STR = 2
VK_ARR = 3


def skip_ws(s, i):
    n = len(s)
    while i < n and s[i] in (" ", "\t"):
        i += 1
    return i


def is_a(c):
    return "A" <= c <= "Z" or "a" <= c <= "z"


def is_d(c):
    return "0" <= c <= "9"


def is_id0(c):
    return is_a(c) or c == "_"


def is_id(c):
    return is_a(c) or is_d(c) or c == "_"


class Enc:
    CP932 = 932
    UTF8 = 65001

    def __init__(self):
        self.code = self.CP932
        self.text_marker = 0

    def set(self, code):
        self.code = code

    def get(self):
        return self.code

    def bytes(self, b):
        if isinstance(b, str):
            b = ord(b)
        if self.code == self.UTF8:
            return 1
        return 2 if b & 0x80 else 1

    def utf16(self, s):
        if not s:
            return 0
        if isinstance(s, bytes):
            s = s.decode("utf-8", "ignore")
        return ord(s[0])


class Var:
    def __init__(self):
        self.num = 0
        self.str = None
        self.lim = False
        self.lo = 0
        self.hi = 0

    def reset(self, hard=False):
        self.num = 0
        if hard:
            self.str = None
        self.lim = False
        self.lo = 0
        self.hi = 0


class Array:
    def __init__(self):
        self.no = 0
        self.num_dim = 0
        self.dim = [0] * 20
        self.data = []
        self.next: Optional[Array] = None


class Label:
    def __init__(self):
        self.name = ""
        self.hdr = 0
        self.start = 0
        self.sline = 0
        self.lines = 0


class Token:
    __slots__ = ("kind", "text", "pos", "line", "end", "wait_at", "color")

    def __init__(self, kind, text, pos, line, end=END_NONE, wait_at=None, color=None):
        self.kind = kind
        self.text = text
        self.pos = pos
        self.line = line
        self.end = end
        self.wait_at = wait_at
        self.color = color  # For TK_TEXT, Ponscripter color (c0..c9)

    def __repr__(self):
        return (
            f"Token({self.kind},{self.text!r},pos={self.pos},line={self.line},"
            f"end={self.end},wait={self.wait_at},color={self.color})"
        )


class Lexer:
    def __init__(self):
        self.expand_in_text = False
        self.enc = Enc()
        self.arc = ""
        self.buf = ""
        self.n = 0
        self.i = 0
        self.line = 0
        self.end = END_NONE
        self.labels = []
        self.num_labels = 0
        self.vars = []
        self.ext = {}
        self.var_rng = 0
        self.glob_border = 0
        self.sw, self.sh = 640, 480
        self.lang = 1
        self.click = None
        self.wait_at = None
        self.text_mode = False
        self.english = False
        self.arr_root = Array()
        self.arr_cur = None
        self.keytbl = bytes(range(256))
        self.keytbl_on = False
        self.num_alias = {}
        # Ponscripter detection/state
        self.pons = False
        self.cur_color = None  # current active color id (0-9)

    def add_numalias(self, name, val):
        self.num_alias[name.lower()] = int(val)

    def read_ident(self):
        s = self.buf
        n = self.n
        i = skip_ws(s, self.i)
        if i < n and is_id0(s[i]):
            j = i
            while j < n and is_id(s[j]):
                j += 1
            name = s[i:j].lower()
            self.i = self._eat_comma(j)
            return name
        return None

    # public api
    def open(self, path):
        if self._load(path) < 0:
            return -1
        self._read_cfg()
        self.vars = [Var() for _ in range(self.var_rng)]
        # Smart detection for Ponscripter syntax: look for markers like ^@^ or ^~cX~
        if ("^@^" in self.buf) or ("^~c" in self.buf.lower()):
            self.pons = True
        self._index_labels()
        self.seek(0)
        return 0

    def next(self):
        t = self._next_token()
        return t

    def peek(self):
        pos = (self.i, self.line, self.end)
        t = self._next_token()
        self.i, self.line, self.end = pos
        return t

    def seek(self, pos):
        self.i = pos
        self.line = self._line_at(pos)
        self.end = END_NONE

    def jump_label(self, name):
        li = self._find_label(name)
        self.seek(li.start)
        return li

    def read_int(self):
        i = self.i
        val, j = self._expr(i)
        self.i = self._eat_comma(j)
        return val

    def read_str(self):
        i = self.i
        parts = []
        while True:
            s, j = self._str_at(i)
            parts.append(s)
            j = self._eat_comma(j)
            if j >= self.n or self.buf[j] != "+":
                i = j
                break
            i = j + 1
        self.i = i
        return "".join(parts)

    def read_label_name(self):
        i = skip_ws(self.buf, self.i)
        s = self.buf
        n = self.n
        out = []
        if i < n and s[i] == "*":
            out.append("*")
            i += 1
            i = skip_ws(s, i)
            while i < n and is_id(s[i]):
                out.append(s[i].lower())
                i += 1
        self.i = self._eat_comma(i)
        return "".join(out) or None

    # state info
    def mode(self):
        return self.sw, self.sh

    def var_range(self):
        return self.var_rng, self.glob_border

    def labels_all(self):
        return self.labels[: self.num_labels]

    # arrays
    def declare_dim(self):
        i = self.i
        av, j = self._array_at(i)
        node = Array()
        node.no = av.no
        node.num_dim = av.num_dim
        for k in range(av.num_dim):
            node.dim[k] = av.dim[k] + 1
        size = 1
        for k in range(av.num_dim):
            size *= node.dim[k]
        node.data = [0] * size
        if self.arr_cur:
            self.arr_cur.next = node
            self.arr_cur = node
        else:
            self.arr_root = self.arr_cur = node
        self.i = j

    # variables
    def set_num(self, no, val):
        v = self._vd(no)
        if v.lim:
            if val < v.lo:
                val = v.lo
            if val > v.hi:
                val = v.hi
        v.num = val

    def set_str(self, no, s):
        self._vd(no).str = s

    # internals
    def _vd(self, no):
        if 0 <= no < self.var_rng:
            return self.vars[no]
        if no not in self.ext:
            self.ext[no] = Var()
        return self.ext[no]

    def _err(self, msg):
        p = self.i
        ln = self._line_at(p)
        sys.stderr.write(f"error: {msg} at pos {p} line {ln}\n")
        raise SystemExit(1)

    # loader
    def _load(self, path):
        self.arc = path
        tries = [
            ("0.txt", 0, None),
            ("0.utf", 0, "utf8"),
            ("00.txt", 0, None),
            ("nscr_sec.dat", 2, None),
            ("nscript.___", 3, None),
            ("nscript.dat", 1, None),
            ("pscript.dat", 1, "utf8"),
        ]
        fp = None
        mode = 0
        ext = ".txt"
        found_path = None
        for name, m, enc in tries:
            p = os.path.join(path, name)
            if os.path.exists(p):
                fp = open(p, "rb")
                print("Opening", p)
                mode = m
                found_path = p
                if enc == "utf8":
                    self.enc.set(self.enc.UTF8)
                break
        if fp is None:
            sys.stderr.write("load: no script container\n")
            return -1
        bufs = []

        def read_one(fh, em):
            magic = [0x79, 0x57, 0x0D, 0x80, 0x04]
            mc = 0
            out = bytearray()
            nl = True
            cr = False
            newlab = False
            while True:
                chunk = fh.read(4096)
                if not chunk:
                    if cr:
                        out.append(0x0A)
                    break
                for b in chunk:
                    if em == 1:
                        b ^= 0x84
                    elif em == 2:
                        b = (b ^ magic[mc]) & 0xFF
                        mc = (mc + 1) % 5
                    elif em == 3:
                        b = (self.keytbl[b] ^ 0x84) & 0xFF
                    if cr and b != 0x0A:
                        out.append(0x0A)
                        nl = True
                        cr = False
                    if b == ord("*") and nl and not newlab:
                        self.num_labels += 1
                        newlab = True
                    else:
                        newlab = False
                    if b == 0x0D:
                        cr = True
                        continue
                    if b == 0x0A:
                        out.append(0x0A)
                        nl = True
                        cr = False
                    else:
                        out.append(b)
                        if b not in (ord(" "), ord("\t")):
                            nl = False
            out.append(0x0A)
            return out

        if mode > 0:
            bufs.append(read_one(fp, mode))
            fp.close()
        else:
            # Plain text series (e.g., 1.txt, 01.txt or 1.utf, 01.utf).
            # Derive extension from the discovered entry (0.txt/0.utf/etc.).
            # If no numbered files exist, fall back to loading the discovered file itself.
            fp.close()
            if found_path is not None:
                _, found_ext = os.path.splitext(found_path)
                if found_ext:
                    ext = found_ext
            for i in range(1, 100):
                for pat in (f"{i}{ext}", f"{i:02d}{ext}"):
                    p2 = os.path.join(path, pat)
                    if os.path.exists(p2):
                        with open(p2, "rb") as fh:
                            bufs.append(read_one(fh, 0))
            # Fallback: if no segments were found, load the discovered file directly
            if not bufs and found_path is not None:
                with open(found_path, "rb") as fh:
                    bufs.append(read_one(fh, 0))
        data = b"".join(bufs)
        if self.enc.get() == self.enc.UTF8:
            self.buf = data.decode("utf-8", "ignore")
        else:
            try:
                self.buf = data.decode("cp932", "ignore")
            except UnicodeDecodeError:
                self.buf = data.decode("latin1", "ignore")
        self.n = len(self.buf)
        return 0

    # config
    def _read_cfg(self):
        self.var_rng = 4096
        self.glob_border = 200
        s = self.buf
        n = self.n
        i = 0
        while i < n and s[i] != ";":
            i += 1
        while i < n and s[i] != "\n":
            i += 1
        if i >= n:
            return
        i += 1
        i = skip_ws(s, i)
        cfg = False
        if i < n and s[i] == "$":
            cfg = True
            i += 1

        def num(j):
            j = skip_ws(s, j)
            v = 0
            while j < n and s[j].isdigit():
                v = v * 10 + (ord(s[j]) - 48)
                j += 1
            return v, j

        while i < n and s[i] != "\n":
            i = skip_ws(s, i)
            if s.startswith("mode", i):
                i += 4
                if s.startswith("800", i):
                    self.sw, self.sh = 800, 600
                    i += 3
                elif s.startswith("400", i):
                    self.sw, self.sh = 400, 300
                    i += 3
                elif s.startswith("320", i):
                    self.sw, self.sh = 320, 240
                    i += 3
                elif s.startswith("w720", i):
                    self.sw, self.sh = 1280, 720
                    i += 4
                else:
                    break
            elif s[i] in "gG" or s.startswith("value", i):
                i = i + 1 if s[i] in "gG" else i + 5
                self.glob_border, i = num(i)
            elif s[i] in "vV":
                i += 1
                self.var_rng, i = num(i)
            elif s[i] in "sS":
                i += 1
                w, i = num(i)
                while i < n and s[i] in ", \t":
                    i += 1
                h, i = num(i)
                self.sw, self.sh = w, h
            elif s[i] in "lL":
                i += 1
                _, i = num(i)
            elif s[i] != ",":
                break
            i = skip_ws(s, i)
            if not cfg and s[i] != ",":
                break
            if s[i] == ",":
                i += 1

    # label index
    def _index_labels(self):
        s = self.buf
        n = self.n
        i = 0
        line = 0
        labs = []
        cur = None
        while i < n:
            i = skip_ws(s, i)
            if i < n and s[i] == "*":
                while i + 1 < n and s[i + 1] == "*":
                    i += 1
                name = self._read_label_name(i)
                li = Label()
                li.name = name[1:]
                li.hdr = i
                li.lines = 1
                li.sline = line
                j = self._after_label(i)
                if j < n and s[j] == "\n":
                    j += 1
                    line += 1
                j = skip_ws(s, j)
                li.start = j
                labs.append(li)
                i = j
                cur = li
            else:
                if cur:
                    cur.lines += 1
                while i < n and s[i] != "\n":
                    i += 1
                if i < n:
                    i += 1
                    line += 1
        sent = Label()
        sent.start = -1
        labs.append(sent)
        self.labels = labs
        self.num_labels = len(labs) - 1

    def _read_label_name(self, i):
        s = self.buf
        n = self.n
        j = i
        out = ["*"]
        j += 1
        j = skip_ws(s, j)
        while j < n and is_id(s[j]):
            out.append(s[j].lower())
            j += 1
        return "".join(out)

    def _after_label(self, i):
        # move to end of label token
        s = self.buf
        n = self.n
        j = i + 1
        j = skip_ws(s, j)
        while j < n:
            c = s[j]
            if c.isalpha() or c.isdigit() or c == "_":
                j += 1
            else:
                break
        return j

    def _line_at(self, pos):
        s = self.buf
        ln = 0
        for k in range(0, min(pos, len(s))):
            if s[k] == "\n":
                ln += 1
        return ln

    def _find_label(self, name):
        name = name.lower()
        for i in range(self.num_labels - 1, -1, -1):
            if self.labels[i].name == name:
                return self.labels[i]
        self._err(f'label "{name}" not found')

    # tokenization
    def _next_token(self):
        s = self.buf
        n = self.n
        i = self.i
        self.end = END_NONE
        self.wait_at = None
        if i >= n:
            return Token(TK_EOF, "", i, self.line)
        i = skip_ws(s, i)
        if i >= n:
            self.i = i
            return Token(TK_EOF, "", i, self.line)
        ch = s[i]
        # comment or lang gate
        # in _next_token, comment branch
        if (
            ch == ";"
            or (s.startswith("langjp", i) and self.lang == 0)
            or (s.startswith("langen", i) and self.lang == 1)
        ):
            start = i
            start_line = self.line
            b = []
            while i < n:
                c = s[i]
                b.append(c)
                i += 1
                if c == "\n":
                    self.line += 1
                    break
            self.i = i
            return Token(TK_COMM, "".join(b), start, start_line, self.end)
        # label head
        if ch == "*":
            name = self._read_label_name(i)
            j = self._after_label(i)
            self.i = self._eat_comma(j)
            return Token(TK_LABEL, name, i, self.line, self.end)
        # newline or marks
        if ch in ("~", ":"):
            self.i = i + 1
            return Token(TK_MARK, ch, i, self.line)
        if ch == "\n":
            self.i = i + 1
            self.line += 1
            return Token(TK_NL, "\n", i, self.line - 1)
        # command (ascii only)
        if is_id0(ch):
            b = []
            i0 = i
            while i < n and is_id(s[i]):
                b.append(s[i].lower())
                i += 1
            self.i = self._eat_comma(i)
            return Token(TK_CMD, "".join(b), i0, self.line, self.end)
        # text
        b = []
        tok_color = self.cur_color
        eng = False
        while i < n:
            c = s[i]
            nb = self.enc.bytes(c)

            if nb >= 2:
                b.append(c)
                i += 1
                if i < n:
                    b.append(s[i])
                    i += 1
                self._maybe_wait(b, i)
                continue

            # Ponscripter inline controls (apply in both expand and non-expand modes)
            if self.pons and nb == 1 and c == "^":
                # ^@^ -> wait control
                if i + 3 <= n and s[i : i + 3] == "^@^":
                    # Emit a regular '@' so downstream UIs keep wait behavior
                    b.append("@")
                    i += 3
                    if self.wait_at is None:
                        self.wait_at = i
                    continue
                # ^~cX~ -> color set (X is 0..9)
                if (
                    i + 5 <= n
                    and s[i + 1] == "~"
                    and s[i + 2].lower() == "c"
                    and s[i + 3].isdigit()
                    and s[i + 4] == "~"
                ):
                    col = int(s[i + 3])
                    self.cur_color = col
                    if not b:
                        tok_color = col
                    i += 5
                    continue
                # Unrecognized caret sequence -> treat '^' literally
                b.append("^")
                i += 1
                continue

            if not self.expand_in_text:
                if c == ";":  # start comment -> stop text token
                    break
                if c == "\n" or c == "\0":
                    break
                b.append(c)
                i += 1
                if self.wait_at is None and c in ("@", "\\"):
                    self.wait_at = i
                continue

            # expand mode
            if not eng and c in ("%", "?"):
                v, j = self._int_at(i)
                for ch2 in str(v):
                    b.append(ch2)
                i = j
                i = skip_ws(s, i)
                continue
            if not eng and c == "$":
                j = i + 1
                no, j = self._int_raw(j)
                v = self._vd(no).str or ""
                b.append(v)
                i = j
                i = skip_ws(s, i)
                continue

            if self.enc.get() == self.enc.UTF8 and ord(c) == self.enc.text_marker:
                eng = not eng
                i += 1
                continue
            if c == "\n" or c == "\0":
                break
            b.append(c)
            i += 1
            if self.wait_at is None and not eng and c in ("@", "\\"):
                self.wait_at = i
            if i < n and s[i] in (";"):
                break
        self.i = self._eat_comma(i)
        self.text_mode = True
        return Token(TK_TEXT, "".join(b), i, self.line, self.end, self.wait_at, tok_color)

    def _maybe_wait(self, old_buf, i):
        if self.wait_at is None and self.click:
            # simple 1-char clickstr
            if i > 0 and self.buf[i - 1] == self.click[0]:
                self.wait_at = i

    # comma handling
    def _eat_comma(self, i):
        s = self.buf
        n = self.n
        i = skip_ws(s, i)
        if i < n and s[i] == ",":
            self.end |= END_COMMA
            i += 1
            i = skip_ws(s, i)
        return i

    # int parsing
    def _int_raw(self, i):
        s = self.buf
        n = self.n
        i = skip_ws(s, i)
        neg = False
        if i < n and s[i] in ("+", "-"):
            neg = s[i] == "-"
            i += 1
        # try identifier
        if i < n and is_id0(s[i]):
            j = i
            while j < n and is_id(s[j]):
                j += 1
            name = s[i:j].lower()
            if name in self.num_alias:
                v = self.num_alias[name]
                return (-v if neg else v), j
            # unknown -> backtrack, yield 0
            return 0, i - (1 if neg else 0)
        # digits
        j = i
        ok = False
        v = 0
        while j < n and s[j].isdigit():
            ok = True
            v = v * 10 + (ord(s[j]) - 48)
            j += 1
        if not ok:
            return 0, i - (1 if neg else 0)
        return (-v if neg else v), j

    def _int_at(self, i):
        s = self.buf
        # n = self.n
        i = skip_ws(s, i)
        if s[i] == "%":
            no, j = self._int_raw(i + 1)
            return self._vd(no).num, j
        if s[i] == "?":
            av, j = self._array_at(i)
            node = self._arr_node(av.no)
            idx = self._arr_idx(node, av, 0)
            return node.data[idx], j
        return self._int_raw(i)

    def _expr(self, i):
        s = self.buf
        n = self.n

        def read_num(j):
            j = skip_ws(s, j)
            neg = False
            if j < n and s[j] == "-":
                neg = True
                j += 1
                j = skip_ws(s, j)
            if j < n and s[j] == "(":
                v, k = self._expr(j + 1)
                k = skip_ws(s, k)
                if k >= n or s[k] != ")":
                    self._err("missing )")
                k += 1
                return (-v if neg else v), k
            v, k = self._int_at(j)
            return (-v if neg else v), k

        def next_op(j):
            j = skip_ws(s, j)
            if j >= n:
                return INV, j
            if s[j] == "+":
                return PLUS, j + 1
            if s[j] == "-":
                return MINUS, j + 1
            if s[j] == "*":
                return MULT, j + 1
            if s[j] == "/":
                return DIV, j + 1
            if s.startswith("mod", j):
                return MOD, j + 3
            return INV, j

        a, i = read_num(i)
        op, i = next_op(i)
        if op == INV:
            return a, i
        b, i = read_num(i)
        while True:
            op2, i2 = next_op(i)
            if op2 == INV:
                break
            c, i = read_num(i2)
            hi = op2 in (MULT, DIV, MOD)
            lo = op in (MULT, DIV, MOD)
            if (not lo) and hi:
                b = self._calc(b, op2, c)
            else:
                a = self._calc(a, op, b)
                op = op2
                b = c
        return self._calc(a, op, b), i

    def _calc(self, a, op, b):
        if op == PLUS:
            r = a + b
        elif op == MINUS:
            r = a - b
        elif op == MULT:
            r = a * b
        elif op == DIV:
            r = int(a / b) if b else 0
        elif op == MOD:
            r = a % b if b else 0
        else:
            r = a
        return r

    # strings
    def _str_at(self, i):
        s = self.buf
        n = self.n
        i = skip_ws(s, i)
        if s[i] == "(":
            frag, j = self._str_at(i + 1)
            j = skip_ws(s, j)
            if j >= n or s[j] != ")":
                self._err("parseStr: missing )")
            return frag, j + 1
        if s[i] == "$":
            no, j = self._int_raw(i + 1)
            return self._vd(no).str or "", j
        if s[i] == '"':
            j = i + 1
            b = []
            while j < n and s[j] != '"' and s[j] != "\n":
                b.append(s[j])
                j += 1
            if j < n and s[j] == '"':
                j += 1
            return "".join(b), j
        if s[i] == "#":
            return s[i : i + 7], i + 7
        if s[i] == "*":
            j = i + 1
            j = skip_ws(s, j)
            b = ["*"]
            while j < n:
                c = s[j]
                if c.isalpha() or c.isdigit() or c == "_":
                    b.append(c.lower())
                    j += 1
                else:
                    break
            return "".join(b), j
        # alias not implemented -> empty
        return "", i

    # arrays
    def _array_at(self, i):
        s = self.buf
        n = self.n
        i = skip_ws(s, i)
        assert s[i] == "?"
        i += 1
        no, i = self._int_raw(i)
        av = Array()
        av.no = no
        av.num_dim = 0
        while i < n and s[i] == "[":
            i += 1
            val, i = self._expr(i)
            av.dim[av.num_dim] = val
            av.num_dim += 1
            i = skip_ws(s, i)
            if i >= n or s[i] != "]":
                self._err("parseArray: missing ]")
            i += 1
        return av, i

    def _arr_node(self, no):
        av = self.arr_root
        while av:
            if av.no == no:
                return av
            av = av.next
        self._err("array not declared")

    def _arr_idx(self, decl, req, off):
        dim = 0
        for k in range(decl.num_dim):
            if decl.dim[k] <= req.dim[k]:
                self._err("dim overflow")
            dim = dim * decl.dim[k] + req.dim[k]
        if decl.dim[decl.num_dim - 1] <= req.dim[decl.num_dim - 1] + off:
            self._err("offset overflow")
        return dim + off


# cli
def main():
    parser = argparse.ArgumentParser(description="Optional settings")
    parser.add_argument(
        "-i", "--input", required=True, help="Path to the script directory"
    )
    parser.add_argument("-l", "--label", help="Label to jump to")

    args = parser.parse_args()
    arc = args.input
    lab = args.label
    lx = Lexer()
    if lx.open(arc) != 0:
        return 1
    print(f"mode {lx.sw}x{lx.sh} vars {lx.var_rng} globals {lx.glob_border}")
    if lab:
        lx.jump_label(lab)
    k = 0
    while True:
        t = lx.next()
        print(t)
        if t.kind == TK_EOF:
            break
        k += 1
        if lab and k > 40:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
