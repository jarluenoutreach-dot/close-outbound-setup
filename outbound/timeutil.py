"""Timestamp helpers. Everything stored in the database is UTC ISO-8601 with a fixed format,
so string comparison in SQL matches time order."""
from __future__ import annotations

import re
from datetime import datetime, timezone

UTC = timezone.utc
_FRACTION = re.compile(r"\.(\d+)")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_dt(value) -> datetime | None:
    """Parse Close / database timestamps (handles 'Z', any fraction length, date-only)."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text[:10])
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
