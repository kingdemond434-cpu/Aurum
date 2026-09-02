"""Has the pipe from quant actually been carrying anything?

THE FAILURE THIS CLOSES. Absorption ran nightly and could be entirely dark
without a single artifact saying so. The pull happened only when
AURUM_QUANT_ROOT was set; when it was not, one line went to a log nobody reads
and the daily report said:

    0 new finding(s) this cycle

That sentence is the same whether quant published nothing this week or the
checkout has not existed since the box was rebuilt. One of those is fine and
the other means the desk stopped learning from the other desk months ago, and
NOTHING in the record could tell them apart. It is the desk's most repeated
defect wearing its most ordinary disguise: absence resolving to a clean answer.

WHAT MAKES THIS A CHECK THAT CAN ACTUALLY CLEAR

Three checks in this repo have gone BROKEN and stayed BROKEN after their defect
was fixed, and all three made the same mistake: they read a stored timestamp as
evidence about the present. So this one grades the LAST RECORDED CYCLE, which
the cycle writes every single night including the nights when nothing was
reachable. Fix the checkout, run a cycle, and the next line reads OK — there is
no window to wait out and no clock to age past.

The distinction that matters is between REACHABLE and PRODUCTIVE:

    dark        no checkout was reachable. A defect, always, and immediately.
    quiet       the checkout was scanned and produced no new findings. Not a
                defect at all — quant is entitled to a quiet week, and calling
                that a fault is how a monitor teaches its operator to ignore it.

Only the first escalates. A monitor that cries about the second is furniture
inside a month.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

ABSORB_HEALTH_VERSION = "absorbhealth-2026-08-29-a"

#: Consecutive dark cycles before the report escalates from a note to a defect.
#: One is a box being rebooted mid-cycle; two nights running is a broken pipe.
DARK_CYCLES_BEFORE_DEFECT = 2

#: Cycles kept in the artifact. Enough to see a pattern, small enough that the
#: file stays readable by a human at a glance.
KEEP = 30


@dataclass(frozen=True)
class Health:
    ok: bool
    dark_streak: int
    last_root: Optional[str]
    last_basis: str
    last_scan_day: Optional[str]        # last cycle that actually scanned a checkout
    last_finding_day: Optional[str]     # last cycle that queued something new
    cycles: int

    @property
    def state(self) -> str:
        if self.dark_streak >= DARK_CYCLES_BEFORE_DEFECT:
            return "DARK"
        if self.dark_streak:
            return "DARK-ONCE"
        return "OK"

    def render(self) -> str:
        if self.state == "DARK":
            return (f"ABSORPTION DARK for {self.dark_streak} consecutive cycle(s) "
                    f"({self.last_basis}) — the desk has not read anything from "
                    f"quant since {self.last_scan_day or 'never'}. This is a "
                    f"DEFECT, not a quiet week: nothing was reachable to be quiet.")
        if self.state == "DARK-ONCE":
            return (f"absorption dark once ({self.last_basis}); last successful "
                    f"scan {self.last_scan_day or 'never'}. One cycle is a reboot; "
                    f"two in a row is a broken pipe and will be reported as one.")
        return (f"absorption OK — scanned {self.last_root} ({self.last_basis}); "
                f"last new finding {self.last_finding_day or 'none yet'}. A scan "
                f"that finds nothing is quant being quiet, which is not a fault.")

    def to_dict(self) -> dict:
        return {"ok": self.ok, "state": self.state, "dark_streak": self.dark_streak,
                "last_root": self.last_root, "last_basis": self.last_basis,
                "last_scan_day": self.last_scan_day,
                "last_finding_day": self.last_finding_day, "cycles": self.cycles}


def load(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def health_of(art: dict) -> Health:
    """Grade the artifact. Pure — the same input always grades the same way."""
    cycles = art.get("cycles") or []
    streak = 0
    for c in reversed(cycles):
        if c.get("scanned"):
            break
        streak += 1
    last_scan = next((c.get("day") for c in reversed(cycles) if c.get("scanned")),
                     None)
    last_find = next((c.get("day") for c in reversed(cycles)
                      if (c.get("n_new") or 0) > 0), None)
    last = cycles[-1] if cycles else {}
    return Health(ok=streak < DARK_CYCLES_BEFORE_DEFECT, dark_streak=streak,
                  last_root=last.get("root"), last_basis=str(last.get("basis") or
                                                             "never-run"),
                  last_scan_day=last_scan, last_finding_day=last_find,
                  cycles=len(cycles))


def record(path: Path, *, day: str, root: Optional[str], basis: str,
           scanned: bool, n_new: int) -> Health:
    """Append this cycle and grade the result. Idempotent within a day.

    Re-running the cycle with --force rewrites today's entry rather than adding
    a second one; otherwise three re-runs on one morning would read as a
    three-cycle dark streak, and the operator would be paged for pressing a key.
    """
    p = Path(path)
    art = load(p)
    cycles: list[dict[str, Any]] = list(art.get("cycles") or [])
    entry = {"day": day, "root": root, "basis": basis, "scanned": bool(scanned),
             "n_new": int(n_new)}
    if cycles and cycles[-1].get("day") == day:
        cycles[-1] = entry
    else:
        cycles.append(entry)
    cycles = cycles[-KEEP:]
    out = {"version": ABSORB_HEALTH_VERSION, "cycles": cycles}
    h = health_of(out)
    out["health"] = h.to_dict()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return h


def check(path: Path) -> Health:
    """Grade whatever is on disk, for a watchdog that did not run the cycle."""
    return health_of(load(path))
