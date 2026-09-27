"""Small, deterministic formatting helpers shared by the composer and reply handler."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def num(n) -> str:
    """2410 -> '2,410' (Western grouping reads fine in WhatsApp for these ranges)."""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    if f.is_integer():
        return f"{int(f):,}"
    return f"{f:,.1f}"


def pct(x, signed: bool = False) -> str:
    """0.214 -> '21%'; signed=True -> '+21%' / '-21%'."""
    try:
        v = round(float(x) * 100)
    except (TypeError, ValueError):
        return str(x)
    if signed:
        return f"{v:+d}%"
    return f"{abs(v)}%"


def ctr(x) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(x)


def rupees(v) -> str:
    try:
        return f"₹{int(float(v)):,}"
    except (TypeError, ValueError):
        return f"₹{v}"


def parse_dt(s) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        if len(s) == 10:
            d = date.fromisoformat(s)
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def nice_date(s, with_day: bool = True) -> str:
    dt = parse_dt(s)
    if not dt:
        return str(s)
    base = f"{dt.day} {MONTHS[dt.month - 1]}"
    return f"{DAYS[dt.weekday()]} {base}" if with_day else base


def nice_time(s) -> str:
    dt = parse_dt(s)
    if not dt:
        return ""
    h, m = dt.hour, dt.minute
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}" if m else f"{h12}{suffix}"


def days_between(a: datetime | None, b: datetime | None) -> int | None:
    if not a or not b:
        return None
    return (b - a).days


def humanize(token: str) -> str:
    """'corporate_bulk_thali_package' -> 'corporate bulk thali package'."""
    out = re.sub(r"[_\-]+", " ", str(token)).strip()
    return re.sub(r"\b(\d+)(day|week|month)\b", r"\1-\2", out)


def first_sentence(text: str, limit: int = 220) -> str:
    if not text:
        return ""
    # split on sentence ends, but not after common abbreviations / initials ("Dr. R. Mehta", "vs.")
    parts = re.split(r"(?<!\bDr)(?<!\bMr)(?<!\bMs)(?<!\bvs)(?<!\bSt)(?<!\b[A-Z])(?<=[.!?])\s+", text.strip())
    s = parts[0]
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def clean(body: str) -> str:
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r" +([,.?!])", r"\1", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def price_in(title: str) -> int | None:
    m = re.search(r"₹\s?([\d,]+)", title or "")
    if not m:
        return None
    return int(m.group(1).replace(",", ""))
