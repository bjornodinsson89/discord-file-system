"""Curated timezone lists for Discord picker flows."""

from __future__ import annotations

from zoneinfo import available_timezones


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _zones_for_prefix(*prefixes: str) -> list[str]:
    all_zones = available_timezones()
    matches: list[str] = []
    for zone in sorted(all_zones):
        if any(zone.startswith(prefix) for prefix in prefixes):
            matches.append(zone)
    return matches


REGION_LABELS: dict[str, str] = {
    "americas": "Americas",
    "europe": "Europe",
    "africa": "Africa",
    "asia": "Asia",
    "oceania": "Oceania",
    "utc_offsets": "UTC/Offsets",
}


_REGION_PRIORITY: dict[str, list[str]] = {
    "americas": [
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "America/Phoenix",
        "America/Toronto",
        "America/Vancouver",
        "America/Mexico_City",
        "America/Sao_Paulo",
        "America/Argentina/Buenos_Aires",
    ],
    "europe": [
        "Europe/London",
        "Europe/Dublin",
        "Europe/Paris",
        "Europe/Berlin",
        "Europe/Madrid",
        "Europe/Rome",
        "Europe/Amsterdam",
        "Europe/Warsaw",
        "Europe/Kiev",
        "Europe/Helsinki",
    ],
    "africa": [
        "Africa/Cairo",
        "Africa/Johannesburg",
        "Africa/Lagos",
        "Africa/Nairobi",
        "Africa/Casablanca",
        "Africa/Accra",
    ],
    "asia": [
        "Asia/Tokyo",
        "Asia/Seoul",
        "Asia/Shanghai",
        "Asia/Hong_Kong",
        "Asia/Singapore",
        "Asia/Kolkata",
        "Asia/Bangkok",
        "Asia/Dubai",
        "Asia/Jakarta",
        "Asia/Manila",
    ],
    "oceania": [
        "Australia/Sydney",
        "Australia/Melbourne",
        "Australia/Brisbane",
        "Australia/Perth",
        "Australia/Adelaide",
        "Pacific/Auckland",
        "Pacific/Fiji",
        "Pacific/Honolulu",
        "Pacific/Guam",
    ],
    "utc_offsets": [
        "UTC",
        "Etc/GMT+12",
        "Etc/GMT+11",
        "Etc/GMT+10",
        "Etc/GMT+9",
        "Etc/GMT+8",
        "Etc/GMT+7",
        "Etc/GMT+6",
        "Etc/GMT+5",
        "Etc/GMT+4",
        "Etc/GMT+3",
        "Etc/GMT+2",
        "Etc/GMT+1",
        "Etc/GMT",
        "Etc/GMT-1",
        "Etc/GMT-2",
        "Etc/GMT-3",
        "Etc/GMT-4",
        "Etc/GMT-5",
        "Etc/GMT-6",
        "Etc/GMT-7",
        "Etc/GMT-8",
        "Etc/GMT-9",
        "Etc/GMT-10",
        "Etc/GMT-11",
        "Etc/GMT-12",
        "Etc/GMT-13",
        "Etc/GMT-14",
    ],
}


REGION_MAP: dict[str, list[str]] = {
    "americas": _unique(_REGION_PRIORITY["americas"] + _zones_for_prefix("America/")),
    "europe": _unique(_REGION_PRIORITY["europe"] + _zones_for_prefix("Europe/")),
    "africa": _unique(_REGION_PRIORITY["africa"] + _zones_for_prefix("Africa/")),
    "asia": _unique(_REGION_PRIORITY["asia"] + _zones_for_prefix("Asia/")),
    "oceania": _unique(
        _REGION_PRIORITY["oceania"] + _zones_for_prefix("Australia/", "Pacific/", "Antarctica/")
    ),
    # NOTE: Etc/GMT signs are reversed by POSIX convention (Etc/GMT+5 == UTC-5).
    "utc_offsets": _unique(_REGION_PRIORITY["utc_offsets"]),
}


def get_region_timezones(region: str) -> list[str]:
    return list(REGION_MAP.get(region, []))
