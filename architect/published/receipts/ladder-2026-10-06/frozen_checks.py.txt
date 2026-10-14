"""Frozen rule checks for the receipt reader, 2026-10-06. Do not edit.

These are guard_not_total_copy and guard_digits, with only the helpers they use: the
checks frozen for the 2026-10-06 change in docs/bench/prereg-nebius.md, "Changes after
publication". From "import re" to the end, this file is byte for byte the frozen file.
Only the lines above differ, so this file's own sha256 is not the one the plan records.
The sha256 of each function's source (inspect.getsource, UTF-8) is the plan's:

    guard_not_total_copy  026ee79d7a94d7b635dd38f2bdc0b2f39c6d73a1eaebc4197090498044bc8e33
    guard_digits          b2bf2c7f2995072023141c7afc885b5ac3299c4c4ae3346aab9ebc6612f047bf

Architect runs the same checks from the receipt reader's spec, digits first and then the
copy check (architect/presets/checks.py, declared in
examples/bench/eval-2026-10-06/ladder/spec.json). tests/test_checks.py runs both on the
same receipts and compares the answers.
"""
# ruff: noqa: I001
import re


AMOUNT = re.compile(r"\d[\d.,]*")


def digits(tok):
    return int(re.sub(r"\D", "", tok)) if re.search(r"\d", tok) else None


def amounts(line):
    return [t for t in AMOUNT.findall(line)]


SUB_WORDS = re.compile(
    r"sub\W*_?\W*total|subtotal|items?\s*total|harga\s*jual|\bdpp\b|net\s*total|^\W*items?\b",
    re.I,
)
GRAND = re.compile(r"grand\s*total|grandtotal", re.I)
TOTALW = re.compile(r"total", re.I)


def subtotal_lines(text):
    lines = text.split("\n")
    out = []
    for i, ln in enumerate(lines):
        if SUB_WORDS.search(ln):
            out.append(ln)
        elif TOTALW.search(ln) and not GRAND.search(ln) and any(
            GRAND.search(later) for later in lines[i + 1:]
        ):
            out.append(ln)
        elif not re.search(r"[A-Za-z]", ln) and amounts(ln):
            out.append(ln)  # a bare amount line
    return out


def guard_not_total_copy(text, out):
    """Weaker: subtotal equal to total is kept only when a subtotal-worded line prints it."""
    v = out.get("subtotal")
    if v is None or v != out.get("total"):
        return out
    ok = any(digits(t) == v for ln in subtotal_lines(text) for t in amounts(ln))
    return out if ok else dict(out, subtotal=None)


DEC = re.compile(r"^(.*\d)[.,](\d\d)$")


def guard_digits(text, out):
    """Rule: amounts keep every digit ("226,500.00" is 22650000)."""
    toks = [t for ln in text.split("\n") for t in amounts(ln)]
    full = {digits(t) for t in toks}
    out = dict(out)
    for f in ("total", "subtotal", "tax"):
        v = out.get(f)
        if v is None or v in full:
            continue
        for t in toks:
            m = DEC.match(t)
            if m and digits(m.group(1)) == v:
                out[f] = digits(t)
                break
    return out
