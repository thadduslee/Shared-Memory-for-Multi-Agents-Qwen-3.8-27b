"""Text utilities: normalisation, verbatim checks, tokens, value extraction. No domain vocabulary."""

from __future__ import annotations

import re
import unicodedata

_QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}
_WORD = re.compile(r"[a-z0-9]+(?:['.\-][a-z0-9]+)*")

STOP = frozenset("""a an the and or but if then else of to in on at by for with from as is are was were be been being
it its this that these those i me my we our you your he him his she her they them their there here what which who whom
whose when where why how do does did done not no yes so than too very can could should would will shall may might must
have has had having just also only about into over under again further once all any both each few more most other some
such own same up down out off per via am pm ok okay please thanks thank hi hello""".split())


def clean(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s or ""))
    for a, b in _QUOTES.items():
        s = s.replace(a, b)
    return s


def norm(s: str) -> str:
    """Lowercase, unify punctuation to spaces, collapse whitespace. For verbatim comparison."""
    s = clean(s).lower()
    s = re.sub(r"[^a-z0-9$%@#/:.,+\-']+", " ", s)
    s = re.sub(r"[.,:;'\-]+(\s|$)", r" ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_verbatim(quote: str, source: str) -> bool:
    q, s = norm(quote), norm(source)
    return bool(q) and q in s


def locate(quote: str, source: str) -> tuple[int, int] | None:
    """Character span of `quote` in `source`, tolerant of case and whitespace."""
    q = re.escape(clean(quote).strip()).replace(r"\ ", r"\s+")
    if not q:
        return None
    m = re.search(q, clean(source), flags=re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def tokens(s: str, *, keep_stop: bool = False) -> list[str]:
    out = _WORD.findall(clean(s).lower())
    return out if keep_stop else [t for t in out if t not in STOP and len(t) > 1]


_VALUE_PATTERNS = [
    re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),                       # phone-like
    re.compile(r"\b\d{1,2}:\d{2}\s?(?:[ap]\.?m\.?)?", re.I),               # times
    re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?"),
    re.compile(r"\b\d+(?:,\d{3})*(?:\.\d+)?\s?(?:%|[a-z]{1,4}\b)", re.I),  # quantities with units
    re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b"),  # codes
    re.compile(r"\b\d[\d,]{2,}\b"),
]


def values(s: str) -> list[str]:
    """Exact value-like substrings (numbers, times, dates, codes). Used by the completeness guard and masking."""
    s = clean(s)
    spans: list[tuple[int, int, str]] = []
    for pat in _VALUE_PATTERNS:
        for m in pat.finditer(s):
            v = m.group(0).strip(" ,.")
            a = m.start() + m.group(0).find(v)
            b = a + len(v)
            if not v or any(a < y and b > x for x, y, _ in spans):
                continue
            spans.append((a, b, v))
    return [v for _a, _b, v in sorted(spans)]


def mask(text: str, vals: list[str], token: str = "[removed]") -> str:
    out = text
    for v in sorted(set(vals), key=len, reverse=True):
        if not v:
            continue
        out = re.sub(r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])", token, out, flags=re.IGNORECASE)
    return out


def ngrams(s: str, max_n: int = 6) -> set[str]:
    toks = _WORD.findall(clean(s).lower())
    out: set[str] = set()
    for n in range(1, max_n + 1):
        for i in range(0, max(0, len(toks) - n + 1)):
            out.add(" ".join(toks[i:i + n]))
    return out


def value_key(v: str) -> str:
    return " ".join(_WORD.findall(clean(v).lower()))


def slugify(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", clean(s).lower()).strip("_")
    return s[:max_len]
