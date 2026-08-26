"""
Verify the MarkdownV2 escaper handles all 18 special characters correctly.

Telegram MarkdownV2 spec — characters that MUST be escaped in literal text:
  backslash _ * [ ] ( ) ~ backtick > # + - = | { } . !

Characters NOT special in MarkdownV2 (must pass through unchanged):
  @ $ % ^ & : " < ? / , ; '

Run:
  python scripts/test_markdownv2_escaper.py
"""
from __future__ import annotations

import re
import sys

# ── Copy the two functions under test directly ────────────────────────────────
# (avoids pulling in httpx / config / supabase at import time)

_MV2_PLAIN_RE = re.compile(r'[\\\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!]')

_PROTECTED_RE = re.compile(
    r"(```[\s\S]*?```"
    r"|`[^`\n]+`"
    r"|\*\*[^*\n]+\*\*"
    r"|\*[^*\n]+\*"
    r"|__[^_\n]+__"
    r"|_[^_\n]+_"
    r"|\|\|[^|\n]+\|\|"
    r"|\[[^\]]+\]\([^)]+\))",
    re.DOTALL,
)


def _escape_plain(text: str) -> str:
    return _MV2_PLAIN_RE.sub(lambda c: "\\" + c.group(), text)


def _escape_markdownv2(text: str) -> str:
    result: list[str] = []
    last = 0
    for m in _PROTECTED_RE.finditer(text):
        start, end = m.start(), m.end()
        result.append(_escape_plain(text[last:start]))
        seg = m.group()
        if seg.startswith("**") and seg.endswith("**"):
            seg = "*" + seg[2:-2] + "*"
        result.append(seg)
        last = end
    result.append(_escape_plain(text[last:]))
    return "".join(result)


# ── Test harness ──────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0


def check(label: str, got: str, expected: str) -> None:
    global PASS, FAIL
    if got == expected:
        PASS += 1
        print(f"  OK    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")
        print(f"        expected: {expected!r}")
        print(f"        got:      {got!r}")


# ── 1. Every MarkdownV2 special character in plain text ───────────────────────
print("\n── Plain-text escaping (all 18 specials) ──")

check("backslash",       _escape_plain("\\"),    "\\\\")
check("underscore _",   _escape_plain("_"),      r"\_")
check("asterisk *",     _escape_plain("*"),      r"\*")
check("open bracket [", _escape_plain("["),      r"\[")
check("close bracket ]",_escape_plain("]"),      r"\]")
check("open paren (",   _escape_plain("("),      r"\(")
check("close paren )",  _escape_plain(")"),      r"\)")
check("tilde ~",        _escape_plain("~"),      r"\~")
check("backtick",       _escape_plain("`"),      r"\`")
check("greater-than >", _escape_plain(">"),      r"\>")
check("hash #",         _escape_plain("#"),      r"\#")
check("plus +",         _escape_plain("+"),      r"\+")
check("minus -",        _escape_plain("-"),      r"\-")
check("equals =",       _escape_plain("="),      r"\=")
check("pipe |",         _escape_plain("|"),      r"\|")
check("open brace {",   _escape_plain("{"),      r"\{")
check("close brace }",  _escape_plain("}"),      r"\}")
check("period .",       _escape_plain("."),      r"\.")
check("exclamation !",  _escape_plain("!"),      r"\!")

# ── 2. Non-special characters must NOT be escaped ─────────────────────────────
print("\n── Non-special characters (pass through unchanged) ──")

for ch in list("@$%^&:\"<?/,;'"):
    check(f"non-special {ch!r}", _escape_plain(ch), ch)

# ── 3. Backslash is escaped first — no double-escaping ───────────────────────
print("\n── Backslash-first ordering (no double-escape) ──")

check("single backslash a\\b",     _escape_markdownv2("a\\b"),    "a\\\\b")
check("two backslashes a\\\\b",    _escape_markdownv2("a\\\\b"),  "a\\\\\\\\b")
check("backslash then dot",        _escape_markdownv2("\\."),      "\\\\\\.")

# ── 4. Protected segments — interior NOT escaped ──────────────────────────────
print("\n── Protected segments (interior unchanged) ──")

check("inline code",          _escape_markdownv2("`hello_world`"),            "`hello_world`")
check("fenced code block",    _escape_markdownv2("```\nhello_world!\n```"),    "```\nhello_world!\n```")
check("*bold* unchanged",     _escape_markdownv2("*hello world*"),             "*hello world*")
check("**bold** => *bold*",   _escape_markdownv2("**hello world**"),           "*hello world*")
check("_italic_ unchanged",   _escape_markdownv2("_hello world_"),             "_hello world_")
check("__underline__",        _escape_markdownv2("__hello world__"),           "__hello world__")
check("||spoiler||",          _escape_markdownv2("||hello world||"),           "||hello world||")
check("[text](url)",          _escape_markdownv2("[click](https://t.me)"),      "[click](https://t.me)")

# ── 5. Mixed: plain text with specials adjacent to protected segments ──────────
print("\n── Mixed content ──")

check(
    "price with dot",
    _escape_markdownv2("Price: 12.50"),
    r"Price: 12\.50",
)
check(
    "bold label + plain special",
    _escape_markdownv2("**Total**: 100.00"),
    r"*Total*: 100\.00",
)
check(
    "underscore in plain text (customer_name)",
    _escape_markdownv2("customer_name"),
    r"customer\_name",
)
check(
    "square brackets in plain text",
    _escape_markdownv2("see [note]"),
    r"see \[note\]",
)
check(
    "pipe in plain text (table row)",
    _escape_markdownv2("a | b | c"),
    r"a \| b \| c",
)
check(
    "exclamation at end",
    _escape_markdownv2("Done!"),
    r"Done\!",
)
check(
    "parens + percent (% not special)",
    _escape_markdownv2("Rate: 18% (GST)"),
    r"Rate: 18% \(GST\)",
)
check(
    "hash in bill number",
    _escape_markdownv2("Bill #001"),
    r"Bill \#001",
)
check(
    "dash in range",
    _escape_markdownv2("10-20 kg"),
    r"10\-20 kg",
)
check(
    "equals in expression",
    _escape_markdownv2("3 + 4 = 7"),
    r"3 \+ 4 \= 7",
)
check(
    "tilde strikethrough in plain",
    _escape_markdownv2("~discount~"),
    r"\~discount\~",
)
check(
    "backtick in plain text",
    _escape_markdownv2("use `x`"),
    "use `x`",           # backtick is recognised as inline code → protected
)
check(
    "at-sign (not special)",
    _escape_markdownv2("@shop"),
    "@shop",
)
check(
    "real invoice line",
    _escape_markdownv2("Surf Excel 1kg - Rs.85.00 (+18% GST)"),
    r"Surf Excel 1kg \- Rs\.85\.00 \(\+18% GST\)",
)
check(
    "bold + underscore adjacent",
    _escape_markdownv2("**Item_name**: rice_bag"),
    r"*Item_name*: rice\_bag",  # inside bold: unchanged; outside: escaped
)

# ── 6. Edge cases ─────────────────────────────────────────────────────────────
print("\n--- Edge cases ---")

check("empty string",   _escape_markdownv2(""),          "")
check("no specials",    _escape_markdownv2("hello world"), "hello world")
check("rupee symbol",   _escape_markdownv2("Rs.100"),    r"Rs\.100")
check("newline passes", _escape_markdownv2("a\nb"),      "a\nb")
check("digits only",    _escape_markdownv2("12345"),     "12345")

# ── Summary ───────────────────────────────────────────────────────────────────
total = PASS + FAIL
print(f"\n{total} tests — {PASS} passed, {FAIL} failed")
if FAIL:
    sys.exit(1)
