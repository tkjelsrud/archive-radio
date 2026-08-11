"""Shared helpers used by more than one pipeline stage."""

import re

PLAUSIBLE_YEAR_RANGE = (2000, 2030)
YEAR_PREFIX_RE = re.compile(r"^(\d{4})")


def extract_year(name):
    """Return a plausible 4-digit year prefix from a name, or None.

    Only the leading digits matter (§5a) — everything after is free text.
    A match outside PLAUSIBLE_YEAR_RANGE (e.g. a typo like "2926-6 Camera
    Angle") is treated as no match, not a bogus year.
    """
    match = YEAR_PREFIX_RE.match(name)
    if not match:
        return None
    year = int(match.group(1))
    if PLAUSIBLE_YEAR_RANGE[0] <= year <= PLAUSIBLE_YEAR_RANGE[1]:
        return year
    return None
