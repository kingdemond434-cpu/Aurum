"""External measurements that cannot lie about where they came from.

THE DEFECT THIS EXISTS TO MAKE IMPOSSIBLE

A recommendation pack shipped `local_fetchers/wgc/fetcher.py` containing this:

    response = requests.get(self.WGC_AISC_URL, timeout=30)
    # Parse the page for latest AISC figure
    data = {
        "last_updated": datetime.utcnow().isoformat(),
        "aisc_usd_per_oz": 1395,          # Update from WGC report
    }

The request is made and the response is never read. `1395` is a typed constant, stamped with the
current time, written to a cache with a 720-hour freshness window. Two sibling methods do the same
without making a request at all. Across that directory: eleven `utcnow()` stamps and four
hardcoded numbers annotated "# Update from ...".

Nothing downstream can tell. The value arrives from a function named `fetch_*`, in a package
named `local_fetchers`, carrying a timestamp newer than any real measurement would have. It is
absence wearing the costume of freshness — and it is aimed squarely at `supply_side.floor_context`,
which refuses when AISC is None. Wire that fetcher and the refusal never fires: the guard is not
defeated by an argument, it is defeated by a fabricated number that looks better than a real one.

SO A NUMBER IS NEVER RETURNED BARE

Every value crosses this boundary inside a `Measurement`, which cannot be constructed without
saying where it came from. `Provenance` has no default. A caller that wants the float must reach
through `.value`, and `.value` is None unless provenance is MEASURED or STALE — so the fabricated
case cannot be spelled at all, because there is no enum member for "I typed this".

    MEASURED   parsed from a live response, this run
    STALE      parsed previously, served from cache past its freshness window, age reported
    ABSENT     not obtained — and this is a real answer, not a zero

STALE IS SEPARATE FROM MEASURED ON PURPOSE. AISC moves quarterly, so a three-week-old figure is
perfectly usable and a caller should still know it is three weeks old. Collapsing the two is how a
cache silently becomes a source.

THE CACHE STORES PROVENANCE, NOT JUST A TIMESTAMP

The pack's cache wrote `{"fetched_at": ..., "data": ...}`. Age alone cannot distinguish a real
fetch from a placeholder written once, so a fabricated value stays fresh-looking forever and is
re-served on every read. Provenance is persisted with the value and downgraded to STALE on read —
never upgraded, and never invented.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

FEEDS_VERSION = "feeds-2026-08-20-a"

#: WGC publishes AISC quarterly. Past this a cached value is served as STALE rather than MEASURED
#: — still usable, and honestly labelled.
AISC_FRESH_HOURS = 24 * 45

#: Bounds outside which a parsed AISC is rejected as a mis-parse rather than believed. Aggregate
#: all-in sustaining cost has never been near these edges; a number outside them means the page
#: changed shape and the regex matched something else, which must fail loudly rather than quietly
#: become the desk's cost floor.
AISC_PLAUSIBLE = (400.0, 5000.0)


class Provenance(str, Enum):
    """Where a value came from. NO DEFAULT, and deliberately no member for 'hardcoded'."""

    MEASURED = "MEASURED"
    STALE = "STALE"
    ABSENT = "ABSENT"


@dataclass(frozen=True)
class Measurement:
    """A number that carries its own origin, or an honest absence.

    `value` is None for ABSENT. Callers that treat None as zero are making a different mistake
    this class cannot prevent, but they cannot make THIS one: there is no way to hand back a
    typed constant labelled MEASURED without writing the lie explicitly.
    """

    name: str
    value: Optional[float]
    provenance: Provenance
    source: str
    fetched_at: Optional[str] = None
    why: str = ""

    def __post_init__(self) -> None:
        if self.provenance is Provenance.ABSENT and self.value is not None:
            raise ValueError("an ABSENT measurement cannot carry a value")
        if self.provenance is not Provenance.ABSENT and self.value is None:
            raise ValueError(f"{self.provenance.value} requires a value")

    @property
    def usable(self) -> bool:
        return self.provenance is not Provenance.ABSENT

    @property
    def age_hours(self) -> Optional[float]:
        if not self.fetched_at:
            return None
        try:
            t = datetime.fromisoformat(self.fetched_at)
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0

    def render(self) -> str:
        if self.provenance is Provenance.ABSENT:
            return f"{self.name}: ABSENT — {self.why}"
        age = self.age_hours
        stamp = f", {age:.0f}h old" if age is not None else ""
        return (f"{self.name}: {self.value:,.2f} [{self.provenance.value}, "
                f"{self.source}{stamp}]")

    @classmethod
    def absent(cls, name: str, source: str, why: str) -> "Measurement":
        return cls(name, None, Provenance.ABSENT, source, None, why)


# --------------------------------------------------------------------------
# Cache — provenance persists, and is only ever downgraded
# --------------------------------------------------------------------------

def cache_write(path: Path, m: Measurement) -> None:
    """Persist a MEASURED value. Refuses anything else.

    Caching an ABSENT would create the thing this module exists to prevent: a stored non-answer
    that later reads back looking like data. Caching a STALE would let age reset itself on every
    process restart.
    """
    if m.provenance is not Provenance.MEASURED:
        raise ValueError(f"only MEASURED values are cached; got {m.provenance.value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": FEEDS_VERSION, "name": m.name, "value": m.value,
        "provenance": m.provenance.value, "source": m.source,
        "fetched_at": m.fetched_at, "why": m.why,
    }, indent=1), encoding="utf-8")


def cache_read(path: Path, name: str, source: str,
               fresh_hours: float) -> Measurement:
    """Read a cached value, DOWNGRADING to STALE past the freshness window.

    Never upgrades. A value read from disk was measured at the time written, not now, and the
    stamp is preserved rather than refreshed — refreshing it on read is precisely how a cache
    becomes a source.
    """
    if not path.exists():
        return Measurement.absent(name, source, f"no cached value at {path.name}")
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        val = float(d["value"])
        at = d.get("fetched_at")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return Measurement.absent(name, source,
                                  f"cache unreadable ({type(exc).__name__}) — treated as absent, "
                                  "never as a default")
    probe = Measurement(name, val, Provenance.MEASURED, source, at)
    age = probe.age_hours
    if age is None or age > fresh_hours:
        why = (f"served from cache, {age:.0f}h old against a {fresh_hours:.0f}h window"
               if age is not None else "cached with no usable timestamp")
        return Measurement(name, val, Provenance.STALE, source, at, why)
    return probe


# --------------------------------------------------------------------------
# AISC — the number `supply_side.floor_context` is waiting for
# --------------------------------------------------------------------------

#: Matches "$1,395/oz", "US$1395 per ounce", "AISC of 1,395". Deliberately several shapes, and
#: deliberately bounded afterwards: a regex that matches too much is how a page's phone number
#: becomes a cost floor.
_AISC_PATTERNS = (
    re.compile(r"AISC[^0-9$]{0,40}\$?\s?([0-9][0-9,]{2,6})(?:\.\d+)?", re.I),
    re.compile(r"all[- ]in sustaining cost[^0-9$]{0,60}\$?\s?([0-9][0-9,]{2,6})", re.I),
    re.compile(r"\$\s?([0-9][0-9,]{2,6})(?:\.\d+)?\s*(?:/|per\s+)o(?:z|unce)", re.I),
)

#: TRIED IN ORDER. The first entry came from the recommendation pack's fetcher and is a 404 —
#: found on the first real run, because that fetcher discards its response and could never have
#: noticed. The live page is `aisc-gold`; `production-costs` on the beta host is the same series
#: under an older path and is kept as a fallback for when the primary is restructured.
#:
#: A CHAIN, NOT A CONSTANT, because a data vendor's URL is not a stable interface. Each is tried
#: and the first that yields a plausible figure wins; if none do, the result is ABSENT naming
#: every URL attempted rather than a default.
WGC_AISC_URLS = (
    "https://www.gold.org/goldhub/data/aisc-gold",
    "https://beta.gold.org/goldhub/data/production-costs",
    "https://www.gold.org/about-gold/gold-supply/responsible-gold/all-in-costs",
)

#: Kept so existing callers and tests that reference a single URL still resolve.
WGC_AISC_URL = WGC_AISC_URLS[0]


def parse_aisc(text: str) -> Optional[float]:
    """First plausible AISC figure in a page, or None.

    PLAUSIBILITY IS PART OF PARSING, NOT A LATER CHECK. A page redesign that breaks the pattern
    will usually still match SOMETHING — a year, a tonnage, an index level — and a silently
    accepted 2026.0 would become the desk's cost floor with no error anywhere. Bounds are applied
    here so a mis-parse returns None and the caller reports ABSENT.
    """
    for pat in _AISC_PATTERNS:
        for m in pat.finditer(text):
            try:
                v = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if AISC_PLAUSIBLE[0] <= v <= AISC_PLAUSIBLE[1]:
                return v
    return None


def fetch_aisc(*, cache_path: Optional[Path] = None,
               getter: Optional[Callable[[str], str]] = None,
               fresh_hours: float = AISC_FRESH_HOURS) -> Measurement:
    """Aggregate all-in sustaining cost, USD/oz — MEASURED, STALE, or ABSENT. Never invented.

    `getter` takes a URL and returns page text; injected so the parse is testable without a
    network and so no import of `requests` is forced on callers that never fetch. Omitted, it
    uses `requests` if installed.

    THE FAILURE PATH RETURNS ABSENT, NOT A DEFAULT. Every branch below — no network library, a
    request that raised, a page that did not parse — produces ABSENT with the reason attached.
    `supply_side.floor_context` already handles None by refusing to characterise the floor, so
    the honest failure flows straight through to an honest prompt.
    """
    name, source = "aisc_usd_per_oz", "World Gold Council"

    if getter is None:
        try:
            import requests                                        # noqa: PLC0415

            def getter(url: str) -> str:                            # type: ignore[misc]
                r = requests.get(url, timeout=30,
                                 headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                return r.text
        except ImportError:
            fallback = (cache_read(cache_path, name, source, fresh_hours)
                        if cache_path else None)
            if fallback is not None and fallback.usable:
                return fallback
            return Measurement.absent(name, source,
                                      "requests is not installed and no cached value exists")

    val = None
    used = ""
    failures: list[str] = []
    for url in WGC_AISC_URLS:
        try:
            text = getter(url)
        except Exception as exc:                                    # noqa: BLE001
            failures.append(f"{url.split('/')[-1]}: {type(exc).__name__}")
            continue
        val = parse_aisc(text)
        if val is not None:
            used = url
            break
        failures.append(f"{url.split('/')[-1]}: fetched, no plausible figure")

    if val is None:
        if cache_path:
            cached = cache_read(cache_path, name, source, fresh_hours)
            if cached.usable:
                return cached
        return Measurement.absent(
            name, source,
            f"no plausible AISC from any known URL (bounds {AISC_PLAUSIBLE[0]:.0f}-"
            f"{AISC_PLAUSIBLE[1]:.0f}). Tried — {'; '.join(failures)}. If a page fetched but "
            "yielded nothing, the figure is probably rendered by a chart widget rather than "
            "present in the HTML, and no regex over the raw page will ever find it")

    m = Measurement(name, val, Provenance.MEASURED, source,
                    datetime.now(timezone.utc).isoformat(),
                    f"parsed from {used}")
    if cache_path:
        cache_write(cache_path, m)
    return m
