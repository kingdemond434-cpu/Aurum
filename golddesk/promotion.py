"""Candidates in on the raw threshold; capital out on forward evidence only.

THE POLICY THIS FILE IMPLEMENTS, AND WHY IT IS NOT A WEAKENING

The desk used to let the deflated Sharpe VETO a cell: search three thousand
parameter points, raise the bar by E[max of N], and arm whatever cleared it.
Nothing ever cleared it, which is not conservatism, it is a desk that can never
adopt anything. A screening statistic used as a final gate has no power to
separate "noise" from "real but small", and it throws away the second along with
the first.

So the rule here is:

    RAW THRESHOLD  ->  admits to SHADOW.       No multiplicity haircut, no veto.
    FORWARD RESULT ->  admits to LIVE CAPITAL. The only gate that decides.

That is strictly MORE permissive at the door and strictly LESS permissive at the
till, and the second half is what makes the first half safe. Out-of-sample days
were not in the search, so a multiplicity artefact cannot survive them: a cell
that looked good because three thousand were tried reverts within weeks, and the
decay monitor retires it having cost nothing but time.

DEFLATION IS KEPT, AND DEMOTED FROM JUDGE TO PRIORITISER

The multiplicity information is real and throwing it away would be its own
error. It just is not a verdict. Here it sets QUEUE ORDER: when shadow slots or
live capital are scarce, the candidate with the stronger deflated Sharpe goes
first. Same population admitted, better ordering within it. A cell with a weak
DSR is not refused, it waits.

WHAT THIS FILE WILL NOT DO

It will not mark anything LIVE on in-sample evidence, whatever its Sharpe. The
word "survivor" is reserved for a cell that has survived forward days it could
not have been fitted to. That is not a stylistic preference: a status field is
read by code that sizes positions, and a cell labelled LIVE gets real lots.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Sequence

PROMOTION_VERSION = "promotion-2026-08-18-a"


class Status(str, Enum):
    """Where a cell sits. The ONLY value that authorises real lots is LIVE."""
    CANDIDATE = "CANDIDATE"      # passed the raw threshold, not yet shadowing
    SHADOW = "SHADOW"            # accruing forward days, no capital
    LIVE = "LIVE"                # forward-validated, sized by the risk layer
    RETIRED = "RETIRED"          # decayed or failed forward; never re-armed silently
    REJECTED = "REJECTED"        # failed even the raw threshold


#: THE ORIGINAL THRESHOLD, deliberately un-inflated. A cell needs a positive
#: in-sample Sharpe and a probabilistic Sharpe clearing this against a ZERO
#: benchmark — the same bar the desk used before multiplicity entered the
#: picture. This is the gate the principal asked to be judged on and it is the
#: gate applied, exactly.
RAW_PSR_THRESHOLD = 0.95

#: Forward days a shadow cell must accrue before it can be considered at all.
#: Not a statistical bar — a sample-size floor. Below this the forward mean is
#: dominated by whichever few days happened to land in it.
MIN_SHADOW_DAYS = 60

#: Forward evidence required to promote. The t-statistic is computed on days the
#: cell could not have been fitted to, which is the entire point.
MIN_FORWARD_T = 1.5

#: Forward days before a LIVE cell is re-examined against its shadow record.
REVIEW_EVERY_DAYS = 20


@dataclass
class Candidate:
    """One searched cell, and everything known about it.

    `dsr` is carried but never gates: it orders the queue. Storing it on the
    record means a future reader can see what the multiplicity cost was, rather
    than discovering that the question was never asked.
    """
    cell: str
    in_sample_sharpe: float
    psr_raw: float                       # against a zero benchmark — the gate
    dsr_deflated: Optional[float] = None  # against E[max of N] — diagnostic only
    n_trials_searched: int = 1
    status: Status = Status.CANDIDATE
    registered_at: str = ""
    shadow_days: int = 0
    forward_r: list = field(default_factory=list)
    #: Dates parallel to forward_r, when the caller supplies them. Needed to
    #: align two sleeves for the marginal-growth test — a bare list of returns
    #: cannot say which days two sleeves shared.
    forward_days_idx: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    # ------------------------------------------------------------- forward stats
    @property
    def forward_mean(self) -> Optional[float]:
        return statistics.fmean(self.forward_r) if self.forward_r else None

    @property
    def forward_t(self) -> Optional[float]:
        """t of the forward mean against zero. None below two observations.

        None rather than 0.0 deliberately: a t-statistic that cannot be computed
        is not a t-statistic of zero, and returning a number here would let a
        one-day cell compare equal to a flat hundred-day one.
        """
        n = len(self.forward_r)
        if n < 2:
            return None
        sd = statistics.stdev(self.forward_r)
        if sd <= 0:
            return None
        return statistics.fmean(self.forward_r) / (sd / math.sqrt(n))

    @property
    def queue_priority(self) -> float:
        """Higher goes first. Deflated Sharpe when known, else raw PSR.

        THIS IS WHERE MULTIPLICITY DOES ITS WORK. It cannot exclude a candidate;
        it can only decide who is served first when slots are finite, which is
        the honest use of a statistic that measures confidence rather than truth.
        """
        return float(self.dsr_deflated if self.dsr_deflated is not None
                     else self.psr_raw)


def screen(cell: str, in_sample_sharpe: float, psr_raw: float,
           dsr_deflated: Optional[float] = None,
           n_trials_searched: int = 1) -> Candidate:
    """Apply the RAW threshold. Deflation is recorded, not applied.

    A cell clearing the un-inflated bar becomes a CANDIDATE however many other
    cells were searched. That is the policy, and the deflated figure travels
    with it so nothing is hidden from whoever reads the record later.
    """
    c = Candidate(cell=cell, in_sample_sharpe=float(in_sample_sharpe),
                  psr_raw=float(psr_raw), dsr_deflated=dsr_deflated,
                  n_trials_searched=int(n_trials_searched),
                  registered_at=datetime.now(timezone.utc).isoformat())
    if in_sample_sharpe > 0 and psr_raw >= RAW_PSR_THRESHOLD:
        c.status = Status.CANDIDATE
        note = (f"admitted on the raw threshold (PSR {psr_raw:.4f} >= "
                f"{RAW_PSR_THRESHOLD}), no multiplicity haircut applied")
        if dsr_deflated is not None and dsr_deflated < RAW_PSR_THRESHOLD:
            note += (f"; deflated Sharpe at N={n_trials_searched} is "
                     f"{dsr_deflated:.4f} and would have refused it — recorded "
                     f"for queue order, not applied as a veto")
        c.notes.append(note)
    else:
        c.status = Status.REJECTED
        c.notes.append(f"below the raw threshold: sharpe "
                       f"{in_sample_sharpe:+.3f}, PSR {psr_raw:.4f}")
    return c


def to_shadow(c: Candidate) -> Candidate:
    """Move a CANDIDATE into shadow. Costs nothing but time, so nothing gates it."""
    if c.status is not Status.CANDIDATE:
        c.notes.append(f"cannot shadow from {c.status.value}")
        return c
    c.status = Status.SHADOW
    c.notes.append("shadowing — accruing forward days, no capital at risk")
    return c


def observe(c: Candidate, r: float, day: Optional[str] = None) -> Candidate:
    """Record one forward day. The only kind of evidence that promotes.

    `day` is optional but wanted: marginal-growth ranking needs to know WHICH
    days two sleeves shared, and a bare list of returns cannot say. Without
    dates the growth test falls back to positional alignment, which is only
    correct when every sleeve traded every day — so a caller that omits dates
    gets the weaker test rather than a silently wrong one.
    """
    if c.status not in (Status.SHADOW, Status.LIVE):
        return c
    if not math.isfinite(r):
        return c
    c.forward_r.append(float(r))
    if day is not None:
        c.forward_days_idx.append(str(day))
    c.shadow_days = len(c.forward_r)
    return c


def _aligned(a: Candidate, b_series: dict) -> tuple:
    """A candidate's forward returns aligned to a dated portfolio series.

    Returns (portfolio_on_shared_days, candidate_on_shared_days). Empty when the
    candidate carries no dates — absence of alignment must not be papered over
    with positional zipping of two differently-scheduled series.
    """
    if not a.forward_days_idx or len(a.forward_days_idx) != len(a.forward_r):
        return [], []
    shared = [d for d in a.forward_days_idx if d in b_series]
    if not shared:
        return [], []
    idx = {d: r for d, r in zip(a.forward_days_idx, a.forward_r)}
    return [b_series[d] for d in shared], [idx[d] for d in shared]


def _normal_sf(t: float) -> float:
    """One-sided tail of the standard normal. A p-value from a t, near enough
    at the sample sizes here (60+ days), and it avoids a scipy dependency."""
    return 0.5 * math.erfc(t / math.sqrt(2.0))


def consider_promotion(c: Candidate, min_days: int = MIN_SHADOW_DAYS,
                       min_t: float = MIN_FORWARD_T) -> Candidate:
    """Promote ONE cell on forward evidence. Prefer promote_book() for a book.

    THE FORWARD GATE IS ITSELF A MULTIPLE TEST, which this single-cell version
    cannot see. At t >= 1.5 a pure-noise cell promotes about 6.7% of the time,
    so a shadow book of 77 candidates promotes roughly five random-number
    generators — and this module's own test caught exactly that, with a
    seeded-noise cell reaching LIVE at t=+2.14 before review() retired it.
    Retiring it afterwards is not good enough: it carried capital in between.

    Left in place because a single cell genuinely is a single test, and callers
    that shadow one thing at a time are not committing the error.

    The in-sample Sharpe plays no part here and must not: it is the number the
    search maximised, so using it again would count the same evidence twice.
    """
    if c.status is not Status.SHADOW:
        return c
    if c.shadow_days < min_days:
        return c
    t = c.forward_t
    if t is None:
        return c
    if t >= min_t and (c.forward_mean or 0.0) > 0:
        c.status = Status.LIVE
        c.notes.append(f"PROMOTED on {c.shadow_days} forward days, "
                       f"mean {c.forward_mean:+.4f}R, t={t:+.2f} — evidence the "
                       f"search could not have fitted")
    return c


#: Share of promoted cells permitted to be false discoveries. FDR rather than
#: family-wise error on purpose: Bonferroni across 77 shadow cells demands
#: t >= 3.2 and would refuse genuine edges to avoid any single mistake, which is
#: the wrong trade for a book that wants several sleeves. Bounding the
#: PROPORTION of duds keeps the queue moving while capping the damage.
FALSE_DISCOVERY_RATE = 0.10


#: Drawdown the marginal-growth comparison is solved to. Both books are sized to
#: the SAME tolerance before their growth is compared, so the winner is the one
#: with the better edge rather than the one handed more leverage.
GROWTH_TOLERANCE = 0.35


def _log_growth(q: float, v: Sequence[float]) -> float:
    tot = 0.0
    for r in v:
        x = 1.0 + q * r
        if x <= 0:
            return float("-inf")
        tot += math.log(x)
    return tot / len(v) if v else 0.0


def _solve_q(v: Sequence[float], tolerance: float) -> float:
    """Largest q whose worst drawdown stays inside `tolerance`. Half-edge."""
    if len(v) < 30:
        return 0.0
    shift = 0.5 * (sum(v) / len(v))
    s = [r - shift for r in v]
    if sum(s) / len(s) <= 0:
        return 0.0

    def dd(q: float) -> float:
        eq = peak = 1.0
        worst = 0.0
        for r in s:
            eq *= (1.0 + q * r)
            if eq <= 0:
                return 1.0
            peak = max(peak, eq)
            worst = max(worst, 1.0 - eq / peak)
        return worst

    lo, hi = 0.0, 0.60
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if dd(mid) > tolerance:
            hi = mid
        else:
            lo = mid
    return lo


def marginal_growth(candidate: Candidate, live_series: dict,
                    n_live: int = 1,
                    tolerance: float = GROWTH_TOLERANCE) -> Optional[float]:
    """Change in the book's LOG GROWTH from adding this sleeve. None if unknowable.

    THE QUESTION A FORWARD t-STATISTIC DOES NOT ASK

    A significant forward edge says the sleeve makes money on its own. It does
    not say the BOOK grows faster for holding it, and those come apart exactly
    when the sleeve is correlated with what is already held: adding a real edge
    at rho 0.7 raises risk more than return and lowers geometric growth. The
    admission condition SR_new > SR_book x rho is the closed form of the same
    idea, and this is its direct measurement on forward days.

    Both books are solved to the SAME drawdown tolerance before comparison, so
    the answer is about edge quality and not about which was handed more
    leverage. Growth is E[ln(1+qR)] — the rate the account actually compounds at
    — because the arithmetic mean is what makes a book look good while it
    shrinks.
    """
    port, cand = _aligned(candidate, live_series)
    if len(port) < 30:
        return None
    before = [p for p in port]
    # THE NEW SLEEVE'S WEIGHT IS 1/(N+1), NOT ONE HALF.
    #
    # A 50/50 blend is not "add a sleeve", it is "rewrite the book around it",
    # and this module's own test caught the difference: two mutually redundant
    # candidates BOTH cleared the gate, because at 50% weight each was really
    # being scored on how far it dragged the book toward its own higher mean
    # rather than on what it added. Weighting the addition correctly makes the
    # measurement marginal, which is the only thing the answer is useful for.
    n = max(int(n_live), 1)
    w_new = 1.0 / (n + 1)
    after = [p * (1.0 - w_new) + c * w_new for p, c in zip(port, cand)]
    q0, q1 = _solve_q(before, tolerance), _solve_q(after, tolerance)
    if q0 <= 0 or q1 <= 0:
        return None
    s0 = 0.5 * (sum(before) / len(before))
    s1 = 0.5 * (sum(after) / len(after))
    g0 = _log_growth(q0, [r - s0 for r in before])
    g1 = _log_growth(q1, [r - s1 for r in after])
    if not (math.isfinite(g0) and math.isfinite(g1)):
        return None
    return g1 - g0


def live_series_of(book: Sequence[Candidate]) -> dict:
    """Dated daily series of the current LIVE book, equal-weighted by day."""
    return series_of([c for c in book if c.status is Status.LIVE])


def series_of(cells: Sequence[Candidate]) -> dict:
    """Dated daily series of ANY set of cells, equal-weighted per day.

    Weighted by the sleeves that actually traded each day, not by the full
    roster: a sleeve that sat out contributes nothing rather than a zero, so a
    quiet day is not scored as a break-even day for everyone.
    """
    acc: dict = {}
    for c in cells:
        if len(c.forward_days_idx) != len(c.forward_r):
            continue
        for d, r in zip(c.forward_days_idx, c.forward_r):
            acc.setdefault(d, []).append(r)
    return {d: sum(v) / len(v) for d, v in acc.items()}


def book_growth(series: dict, tolerance: float = GROWTH_TOLERANCE) -> Optional[float]:
    """E[ln(1+qR)] of a dated book at the q its own drawdown tolerance allows.

    THE OBJECTIVE THE WHOLE PIPELINE MAXIMISES. Log growth, not arithmetic
    return, because the account compounds geometrically and the two come apart
    exactly where it matters — a book can have a rising arithmetic mean and a
    falling geometric one, and that book shrinks while its statistics improve.

    Solved to a fixed drawdown so two books are always compared at the same
    risk. Comparing at the same SIZE would just report which was handed more
    leverage.
    """
    if len(series) < 30:
        return None
    v = [series[d] for d in sorted(series)]
    q = _solve_q(v, tolerance)
    if q <= 0:
        return None
    shift = 0.5 * (sum(v) / len(v))
    g = _log_growth(q, [r - shift for r in v])
    return g if math.isfinite(g) else None


#: Correlation above which two sleeves are the SAME BET wearing two names.
#: Matches deflation.CLONE_RHO. Below it, two correlated sleeves genuinely
#: diversify a little; above it they do not, and only the better may be held.
CLONE_RHO = 0.90


def _corr(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    n = len(a)
    if n < 30:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    if saa <= 0 or sbb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(saa * sbb)


def clone_of(candidate: Candidate, live: Sequence[Candidate]) -> Optional[Candidate]:
    """The live sleeve this candidate duplicates, if any.

    WITHOUT THIS THE GROWTH TEST CANNOT SEE A DUPLICATE. The book series is
    equal-weighted per day, so adding a second copy of a strong sleeve shifts
    the average toward it and the log growth genuinely RISES — the measurement
    cannot distinguish "a new source of return" from "more weight on the one
    already held". This module's own test caught two near-identical candidates
    both clearing the gate at +0.0236 and +0.0095 a day.

    Correlation answers directly what the weighting cannot. Above CLONE_RHO the
    only legal move is REPLACE, so the book keeps the better of the two rather
    than both.
    """
    idx = {d: r for d, r in zip(candidate.forward_days_idx, candidate.forward_r)}
    if not idx:
        return None
    for c in live:
        shared = [d for d in c.forward_days_idx if d in idx]
        if len(shared) < 30:
            continue
        cmap = {d: r for d, r in zip(c.forward_days_idx, c.forward_r)}
        r = _corr([cmap[d] for d in shared], [idx[d] for d in shared])
        if r is not None and r >= CLONE_RHO:
            return c
    return None


@dataclass
class Action:
    """What to do with one shadow candidate, and what it is worth.

    `gain` is the improvement in the book's log growth per day. Positive means
    the book compounds faster for acting; ADD and REPLACE are both permitted and
    the larger gain wins, because "can these coexist" is a question about
    correlation rather than about seniority.
    """
    kind: str                    # "ADD" | "REPLACE" | "HOLD"
    candidate: Candidate
    gain: float = 0.0
    victim: Optional[Candidate] = None
    why: str = ""
    #: Growth could not be scored at all — an empty book, or too few shared
    #: days. NOT the same as a gain of zero, and conflating them is a real bug:
    #: the greedy loop filters on gain > 0, so a "not applicable" action worth
    #: 0.0 was silently dropped and the FIRST sleeve could never reach live.
    #: Not-measurable must fall through to the significance test, never to a
    #: refusal.
    forced: bool = False


def best_action(candidate: Candidate, book: Sequence[Candidate],
                tolerance: float = GROWTH_TOLERANCE) -> Action:
    """ADD, REPLACE or HOLD — whichever maximises the book's log growth.

    REPLACEMENT IS A FIRST-CLASS MOVE. A new sleeve that cannot be added because
    it duplicates an incumbent may still be BETTER than that incumbent, and a
    pipeline that can only append will hold the worse of two correlated edges
    forever purely because it arrived first. Every live sleeve is tested as a
    swap target and the best move wins.
    """
    live = [c for c in book if c.status is Status.LIVE]
    if not live:
        return Action("ADD", candidate, 0.0, None,
                      "empty book: nothing to be correlated with, so growth is "
                      "not applicable rather than failed", forced=True)
    now = book_growth(live_series_of(book), tolerance)
    if now is None:
        return Action("ADD", candidate, 0.0, None,
                      "live book has too few shared days to score; admitted on "
                      "significance alone", forced=True)
    best = Action("HOLD", candidate, 0.0, None, "")
    twin = clone_of(candidate, live)
    g_add = None if twin is not None else book_growth(
        series_of(live + [candidate]), tolerance)
    if twin is None and g_add is None:
        return Action("ADD", candidate, 0.0, None,
                      "candidate shares too few days with the book to score "
                      "growth; admitted on significance alone", forced=True)
    if g_add is not None and g_add - now > best.gain:
        best = Action("ADD", candidate, g_add - now, None,
                      f"adding raises book log growth {now:+.6f} -> {g_add:+.6f}")
    for victim in live:
        keep = [c for c in live if c is not victim]
        g_sw = book_growth(series_of(keep + [candidate]), tolerance)
        if g_sw is None:
            continue
        if g_sw - now > best.gain:
            best = Action("REPLACE", candidate, g_sw - now, victim,
                          f"replacing {victim.cell} raises book log growth "
                          f"{now:+.6f} -> {g_sw:+.6f}; the two cannot both pay "
                          f"because they overlap, and this one pays more")
    if best.kind == "HOLD":
        best.why = (
            (f"duplicates {twin.cell} at rho >= {CLONE_RHO} and does not beat it, "
             f"so it cannot be added and is not worth swapping in. "
             if twin is not None else "")
            + f"neither adding nor replacing raises book log growth from "
              f"{now:+.6f}. A real edge that does not compound the book is a "
              f"correlated edge, not a new one.")
    return best


def promote_book(book: Sequence[Candidate], min_days: int = MIN_SHADOW_DAYS,
                 fdr: float = FALSE_DISCOVERY_RATE,
                 floor_t: float = MIN_FORWARD_T,
                 require_growth: bool = True,
                 tolerance: float = GROWTH_TOLERANCE) -> list:
    """Promote across the whole shadow book at a controlled false-discovery rate.

    Benjamini-Hochberg over every cell eligible on days: sort the one-sided
    p-values, find the largest k with p_(k) <= k*fdr/m, promote that many. The
    threshold therefore TIGHTENS as more cells shadow concurrently, which is the
    property the single-cell version lacks and the reason noise was reaching
    LIVE.

    `floor_t` still applies underneath, so a book of two cells cannot promote on
    a t of 0.3 merely because two is a small family.
    """
    eligible = [c for c in book
                if c.status is Status.SHADOW and c.shadow_days >= min_days
                and c.forward_t is not None and (c.forward_mean or 0.0) > 0]
    m = len(eligible)
    if m == 0:
        return []
    scored = sorted(((_normal_sf(c.forward_t), c) for c in eligible),
                    key=lambda pc: pc[0])
    cutoff = 0
    for i, (p, _) in enumerate(scored, start=1):
        if p <= i * fdr / m:
            cutoff = i
    # TWO GATES, AND THEY ASK DIFFERENT QUESTIONS.
    #
    #   BH on the forward t  ->  is this sleeve's edge REAL?
    #   marginal log growth  ->  does the BOOK compound faster for holding it?
    #
    # A sleeve can pass the first and fail the second: a genuine edge at rho 0.7
    # against the existing book adds more risk than return and LOWERS geometric
    # growth. Promoting on significance alone would keep adding real edges until
    # the book stopped growing, which is the failure the five-versus-twelve
    # comparison already demonstrated on historical data.
    passed_stat = [(p, c) for p, c in scored[:cutoff] if c.forward_t >= floor_t]
    if not require_growth:
        out = []
        for p, c in passed_stat:
            c.status = Status.LIVE
            c.notes.append(f"PROMOTED on significance alone (t={c.forward_t:+.2f},"
                           f" p={p:.4f}); growth gate disabled by the caller")
            out.append(c)
        return out

    # GREEDY MAXIMISATION OF BOOK LOG GROWTH, RE-SCORED AFTER EVERY MOVE.
    #
    # Deploy as many edges as the book will carry, take every gain however
    # small, and take none that shrink it. Each pass scores every remaining
    # candidate as ADD or REPLACE against the CURRENT book, executes the single
    # best positive move, and starts again — because one promotion changes what
    # every other candidate is worth. Scoring once and executing a whole list
    # would let two mutually redundant sleeves both clear the same stale
    # baseline, which this module's own test caught happening.
    # A list of pairs, not a dict: Candidate is a mutable dataclass and
    # therefore unhashable, and making it hashable to suit a lookup here would
    # invite identity bugs everywhere else it is stored.
    pending = list(passed_stat)                 # [(p, candidate), ...]
    promoted: list = []
    while pending:
        moves = [(best_action(c, book, tolerance), p) for p, c in pending]
        moves = [(a, p) for a, p in moves
                 if a.kind != "HOLD" and (a.gain > 0 or a.forced)]
        if not moves:
            break
        moves.sort(key=lambda ap: -ap[0].gain)
        act, p = moves[0]
        c = act.candidate
        if act.kind == "REPLACE" and act.victim is not None:
            act.victim.status = Status.RETIRED
            act.victim.notes.append(
                f"REPLACED by {c.cell}: {act.why}. Retired for growth, not for "
                f"decay — it may still have a positive edge of its own.")
        c.status = Status.LIVE
        c.notes.append(
            f"PROMOTED ({act.kind}) on {c.shadow_days} forward days, mean "
            f"{c.forward_mean:+.4f}R, t={c.forward_t:+.2f}, p={p:.4f} — cleared "
            f"Benjamini-Hochberg at FDR {fdr:.0%} against {m} concurrent shadow "
            f"cells. {act.why}. Book log growth +{act.gain:.6f}/day.")
        promoted.append(c)
        pending = [(pp, cc) for pp, cc in pending if cc is not c]

    for _, c in pending:
        a = best_action(c, book, tolerance)
        c.notes.append(f"HELD IN SHADOW: {a.why}")
    return promoted


def review(c: Candidate, lookback: int = REVIEW_EVERY_DAYS) -> Candidate:
    """Retire a LIVE cell whose recent forward record has turned over.

    Deliberately crude and deliberately one-directional: this retires, it never
    re-arms. A cell that recovers goes back through shadow like anything else,
    because "it came back" is exactly what a noise cell looks like half the time.
    """
    if c.status is not Status.LIVE or len(c.forward_r) < lookback + 10:
        return c
    recent = c.forward_r[-lookback:]
    if statistics.fmean(recent) <= 0:
        c.status = Status.RETIRED
        c.notes.append(f"RETIRED: last {lookback} forward days average "
                       f"{statistics.fmean(recent):+.4f}R")
    return c


def queue(cands: Iterable[Candidate], slots: Optional[int] = None) -> list:
    """Candidates in service order. Every one is served eventually if slots allow.

    Sorted by deflated Sharpe where known — the multiplicity-aware figure decides
    WHO FIRST, never who at all.
    """
    ordered = sorted([c for c in cands if c.status is Status.CANDIDATE],
                     key=lambda c: -c.queue_priority)
    return ordered if slots is None else ordered[:slots]


def save(cands: Sequence[Candidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{**asdict(c), "status": c.status.value}
                                for c in cands], indent=1), "utf-8")


def load(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for row in json.loads(path.read_text("utf-8")):
        row["status"] = Status(row.get("status", "CANDIDATE"))
        out.append(Candidate(**row))
    return out


def report(cands: Sequence[Candidate]) -> str:
    by = {s: [c for c in cands if c.status is s] for s in Status}
    lines = [f"PROMOTION PIPELINE  ({PROMOTION_VERSION})",
             f"  raw threshold PSR >= {RAW_PSR_THRESHOLD}, no multiplicity veto",
             f"  live requires {MIN_SHADOW_DAYS}+ forward days at t >= "
             f"{MIN_FORWARD_T}", ""]
    for s in Status:
        lines.append(f"  {s.value:<10} {len(by[s]):>4}")
    live = by[Status.LIVE]
    if live:
        lines += ["", "  LIVE (forward-validated):"]
        for c in sorted(live, key=lambda c: -(c.forward_t or 0)):
            lines.append(f"    {c.cell:<40} {c.shadow_days:>4}d  "
                         f"mean {c.forward_mean:+.4f}R  t={c.forward_t:+.2f}")
    shadow = by[Status.SHADOW]
    if shadow:
        lines += ["", "  SHADOW (no capital):"]
        for c in sorted(shadow, key=lambda c: -c.queue_priority)[:12]:
            t = c.forward_t
            lines.append(f"    {c.cell:<40} {c.shadow_days:>4}d  "
                         + (f"t={t:+.2f}" if t is not None else "t=—"))
    return "\n".join(lines)
