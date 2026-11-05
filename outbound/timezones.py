"""Lead time zone from the phone's area code, the 8am–8pm send window, and next-morning times."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import phonenumbers
from phonenumbers import timezone as pn_timezone

from .timeutil import UTC

CENTRAL = ZoneInfo("America/Chicago")
# Area codes that straddle a zone line return 2-3 zones. More than that means the number
# isn't tied to a region (toll-free, invalid), so we fall back to the default.
_MAX_ZONES = 3


def zones_for_phone(phone: str | None, default: str = "America/Chicago") -> list[ZoneInfo]:
    names: list[str] = []
    if phone:
        try:
            number = phonenumbers.parse(phone, "US")
            names = [n for n in pn_timezone.time_zones_for_number(number) if n and not n.startswith("Etc/")]
        except phonenumbers.NumberParseException:
            names = []
    if not names or len(names) > _MAX_ZONES:
        return [ZoneInfo(default)]
    zones = []
    for name in names:
        try:
            zones.append(ZoneInfo(name))
        except Exception:  # unknown zone name on this system
            continue
    return zones or [ZoneInfo(default)]


def zone_label(zones: list[ZoneInfo]) -> str:
    return "/".join(z.key for z in zones)


def in_send_window(now: datetime, zones: list[ZoneInfo], start_hour: int = 8, end_hour: int = 20) -> bool:
    """True when it is between start and end hour in every zone the number could be in."""
    return all(start_hour <= now.astimezone(z).hour < end_hour for z in zones)


def anchor_zone(now: datetime, zones: list[ZoneInfo]) -> ZoneInfo:
    """Central for Eastern/Central leads; the lead's own zone when it is west of Central."""
    return min([CENTRAL, *zones], key=lambda z: now.astimezone(z).utcoffset())


def next_send_time(now: datetime, zones: list[ZoneInfo], start_hour: int = 8) -> datetime:
    """Next 8am Central, or next 8am local for Mountain/Pacific (and further west) leads."""
    zone = anchor_zone(now, zones)
    local = now.astimezone(zone)
    target = datetime.combine(local.date(), time(start_hour), tzinfo=zone)
    if target <= local:
        target = datetime.combine(local.date() + timedelta(days=1), time(start_hour), tzinfo=zone)
    return target.astimezone(UTC)
