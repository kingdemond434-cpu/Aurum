"""Specialists — orthogonal readers of the same state, and what one is worth.

A learned sequence model over candles (Kronos and its kin) is a genuinely
different representation from the desk's engineered features and from an LLM
reading structure in prose. That is the argument for having one. It is not an
argument for letting it decide anything, and this module is built around the
difference.

TWO THINGS THIS REFUSES TO DO

NO VOTING, NO AVERAGING. The obvious design is a council of specialists whose
outputs are averaged or majority-voted into a consensus. It is wrong here for a
specific reason: the specialists are CORRELATED — they read the same bars,
through different lenses, and their errors are not independent. Averaging
correlated readers manufactures confidence out of agreement that was structural
rather than evidential, and the resulting number is more confident and no more
accurate. Worse, it destroys the only genuinely useful output: DISAGREEMENT.
Two readers that diverge on the same state have located something interesting,
and a consensus is precisely the operation that erases it. `Council.read()`
returns every read, reports agreement as a measurement, and has no method that
collapses them.

NO BUNDLED WEIGHTS, NO NEW DEPENDENCY. A specialist is a callable the operator
supplies. Importing a stack imports its assumptions, its failure modes, its
maintenance burden and its unreproduced performance claims. The seam takes a
`predict_fn`; whether that is a local checkpoint, a hosted endpoint or a lookup
table is not this module's business, and the absence of one is a first-class
state rather than an error.

WHAT A SPECIALIST IS ACTUALLY WORTH, AND WHY ACCURACY DOES NOT ANSWER IT

The tempting measurement is accuracy, or IC, or hit rate. All three can be
excellent while the specialist is worth exactly zero, because a specialist earns
its keep only where it CHANGES A DECISION. A reader that is right 70% of the
time and agrees with the desk every time it speaks has added nothing; the desk
would have taken those trades anyway.

So `marginal_value()` measures the only thing that matters: on states where the
desk's decision differed with the specialist and without it, what did those
CHANGED decisions pay, net of cost? Two consequences fall out of that framing
and both are implemented:

  THE UNCHANGED STATES ARE EXCLUDED, not counted as ties. Including them adds
  identical outcomes to both arms, which cannot move the difference but does
  inflate the sample and shrink the standard error — a smaller p-value computed
  from observations that carried no information about the question. That is a
  significance result manufactured out of agreement.

  THE COST IS CHARGED TO THE CHANGE. A specialist that flips a decision incurs
  a spread and possibly a slippage the desk would not otherwise have paid. A
  specialist whose changes are right 55% of the time and cost more than 0.05R
  each is a losing specialist, and only a net measurement says so.

The verdict this most often returns is NO STANDING: not enough changed
decisions to say anything. That is the correct answer for a new specialist and
it is designed to be reachable, because the alternative — promoting a reader on
the strength of its accuracy — is how a desk acquires an expensive oracle that
has never once altered an outcome.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Optional, Protocol, Sequence

SPECIALIST_VERSION = "spec-2026-08-30-a"

#: Changed decisions below this and there is no verdict. Not a p-value threshold
#: — a floor on the sample the question is actually asked over.
MIN_CHANGED = 25

#: Charged to every decision the specialist flips: the round-trip the desk would
#: not otherwise have paid. Stated in R and overridable, never silently zero.
DEFAULT_CHANGE_COST_R = 0.05


@dataclass(frozen=True)
class DeskRole:
    """A named seat with one narrow responsibility.

    The names make the desk legible to an operator; they do not create agents,
    authority, or votes.  A seat without an implementation remains explicitly
    UNAVAILABLE rather than being filled by a decorative heuristic.
    """
    key: str
    name: str
    responsibility: str


DESK_ROLES: tuple[DeskRole, ...] = (
    DeskRole("atlas", "Atlas", "macro context"),
    DeskRole("lumen", "Lumen", "technical setup scouting"),
    DeskRole("apollo", "Apollo", "events and session context"),
    DeskRole("argus", "Argus", "visual structure"),
    DeskRole("chronos", "Chronos", "sequence and path dependence"),
    DeskRole("orion", "Orion", "COMEX and positioning context"),
    DeskRole("mnemosyne", "Mnemosyne", "analogues and decision memory"),
    DeskRole("hephaestus", "Hephaestus", "data and operational health"),
)

ROLE_BY_KEY = {r.key: r for r in DESK_ROLES}


@dataclass(frozen=True)
class SpecialistRead:
    """One specialist's view. Deliberately coarse.

    A learned model will happily emit a probability to four decimals. Carrying
    that precision through implies a calibration nobody has demonstrated on this
    desk's data, and precision is what sizing reads. Direction plus a bounded
    strength is what a specialist has actually earned the right to say.
    """
    name: str
    direction: str                   # LONG | SHORT | FLAT
    strength: float = 0.0            # 0..1, bounded on construction
    horizon_bars: int = 1
    why: str = ""
    available: bool = True
    meta: dict = field(default_factory=dict)
    role: str = ""
    #: Probability that the horizon return is positive. Optional because most
    #: seats have not earned calibrated probabilities; scorecards derive a
    #: deliberately coarse value from direction/strength when it is absent.
    probability_up: Optional[float] = None

    def __post_init__(self):
        if self.direction not in ("LONG", "SHORT", "FLAT"):
            raise ValueError(f"direction {self.direction!r} is not LONG/SHORT/FLAT")
        object.__setattr__(self, "strength", max(0.0, min(1.0, float(self.strength))))
        if self.horizon_bars < 1:
            raise ValueError("horizon_bars must be positive")
        if self.probability_up is not None:
            p = float(self.probability_up)
            if not math.isfinite(p):
                raise ValueError("probability_up must be finite")
            object.__setattr__(self, "probability_up", max(0.0, min(1.0, p)))

    @property
    def signed(self) -> float:
        return {"LONG": 1.0, "SHORT": -1.0, "FLAT": 0.0}[self.direction] * self.strength

    @property
    def p_up(self) -> float:
        """A scoreable probability without pretending to extra precision."""
        if self.probability_up is not None:
            return self.probability_up
        return max(0.0, min(1.0, 0.5 + self.signed / 2.0))

    def render(self) -> str:
        if not self.available:
            return f"    {self.name:<22} UNAVAILABLE — {self.why or 'no reason given'}"
        return (f"    {self.name:<22} {self.direction:<5} "
                f"strength {self.strength:.2f}  {self.why[:52]}")


class Specialist(Protocol):
    name: str

    def read(self, snapshot) -> SpecialistRead: ...


@dataclass
class SeatSpecialist:
    """Give an injected implementation the desk seat's stable identity."""
    name: str
    role: str
    implementation: Specialist

    def read(self, snapshot) -> SpecialistRead:
        read = self.implementation.read(snapshot)
        meta = dict(read.meta)
        meta.setdefault("implementation", str(getattr(
            self.implementation, "name", type(self.implementation).__name__)))
        return replace(read, name=self.name, role=self.role, meta=meta)


@dataclass
class UnavailableSpecialist:
    """The honest absence. A specialist with no model is not a FLAT opinion.

    This distinction is the entire reason the class exists. A missing sequence
    model returning FLAT would be read downstream as "the sequence model sees
    nothing here" — an observation — when the truth is that nobody asked
    anything. One of those is evidence about gold and the other is evidence
    about the deployment.
    """
    name: str
    reason: str = "no model configured"
    role: str = ""

    def read(self, snapshot) -> SpecialistRead:
        return SpecialistRead(self.name, "FLAT", 0.0, why=self.reason,
                              available=False, role=self.role)


@dataclass
class SequenceSpecialist:
    """The Kronos-shaped seam. Weights are the operator's, never bundled.

    `predict_fn` receives the closed-bar sequence pulled from a CausalSnapshot
    and returns either a signed float in [-1, 1] or a (direction, strength)
    pair. Anything it raises is caught and becomes UNAVAILABLE: a specialist
    that can throw into the decision path is a specialist that can halt the
    desk, and no reader is worth that.
    """
    name: str = "sequence"
    predict_fn: Optional[Callable[[Sequence[Sequence[float]]], object]] = None
    horizon_bars: int = 4
    key_prefix: str = "m15"
    min_bars: int = 20
    role: str = "sequence and path dependence"

    def _bars(self, snapshot) -> list[list[float]]:
        """Closed bars out of the snapshot, oldest first.

        Read through the snapshot rather than from a feed on purpose: the
        snapshot has already refused anything from the future, so a specialist
        physically cannot be handed the bar it is predicting. A specialist with
        its own data path is a specialist with its own lookahead.
        """
        n = snapshot.get(f"{self.key_prefix}.n_closed") or 0
        rows = []
        for i in range(int(n) - 1, -1, -1):          # index 0 is most recent
            row = [snapshot.get(f"{self.key_prefix}.{i}.{f}")
                   for f in ("open", "high", "low", "close")]
            if any(v is None for v in row):
                continue
            rows.append([float(v) for v in row])
        return rows

    def read(self, snapshot) -> SpecialistRead:
        if self.predict_fn is None:
            return SpecialistRead(self.name, "FLAT", 0.0,
                                  why="no predict_fn supplied — weights are the "
                                      "operator's, never bundled here",
                                  available=False)
        bars = self._bars(snapshot)
        if len(bars) < self.min_bars:
            return SpecialistRead(
                self.name, "FLAT", 0.0,
                why=f"{len(bars)} closed bars, {self.min_bars} required",
                available=False)
        try:
            out = self.predict_fn(bars)
        except Exception as e:                       # noqa: BLE001
            return SpecialistRead(self.name, "FLAT", 0.0,
                                  why=f"model raised {type(e).__name__}: {e}",
                                  available=False)
        if isinstance(out, tuple) and len(out) == 2:
            d, s = out
            return SpecialistRead(self.name, str(d).upper(), float(s),
                                  self.horizon_bars, "learned sequence read")
        try:
            v = float(out)
        except (TypeError, ValueError):
            return SpecialistRead(self.name, "FLAT", 0.0,
                                  why=f"model returned {type(out).__name__}, "
                                      f"expected a float or (direction, strength)",
                                  available=False)
        if not math.isfinite(v):
            return SpecialistRead(self.name, "FLAT", 0.0,
                                  why="model returned a non-finite value",
                                  available=False)
        d = "FLAT" if abs(v) < 1e-9 else ("LONG" if v > 0 else "SHORT")
        return SpecialistRead(self.name, d, abs(v), self.horizon_bars,
                              "learned sequence read")


@dataclass
class ContextSensorSpecialist:
    """A bounded read of deterministic context already in the snapshot.

    These are shadow sensors, not strategies: they never emit entries, levels,
    stops, targets, or vetoes. Their only purpose is to create a permanent,
    scoreable alternate representation on the exact same frozen state.
    """
    name: str
    mode: str
    role: str
    horizon_bars: int = 1

    def read(self, snapshot) -> SpecialistRead:
        if self.mode == "technical":
            direction = str(snapshot.get("context.trend_direction") or "NONE")
            health = str(snapshot.get("context.trend_health") or "WEAK")
            displacement = str(snapshot.get("context.displacement_state") or "NONE")
            if direction not in ("UP", "DOWN"):
                return SpecialistRead(self.name, "FLAT", 0.15, self.horizon_bars,
                                      "no measured directional structure", role=self.role)
            strength = {"WEAK": 0.25, "MODERATE": 0.45, "STRONG": 0.65}.get(
                health, 0.30)
            if displacement == "CONFIRMED":
                strength = min(0.85, strength + 0.15)
            return SpecialistRead(
                self.name, "LONG" if direction == "UP" else "SHORT", strength,
                self.horizon_bars,
                f"measured trend {direction}/{health}, displacement {displacement}",
                role=self.role)

        if self.mode == "session":
            session = snapshot.get("session")
            if not session:
                return SpecialistRead(self.name, "FLAT", 0.0, self.horizon_bars,
                                      "session absent from snapshot", available=False,
                                      role=self.role)
            return SpecialistRead(self.name, "FLAT", 0.0, self.horizon_bars,
                                  f"session={session}; context only, no direction",
                                  role=self.role, probability_up=0.5)

        if self.mode == "macro":
            keys = [k for k in snapshot.keys() if k.startswith("macro.")
                    and k not in {"macro.age_hours"}]
            if not keys:
                return SpecialistRead(self.name, "FLAT", 0.0, self.horizon_bars,
                                      "no fresh structured macro observations",
                                      available=False, role=self.role)
            detail = ", ".join(f"{k[6:]}={snapshot.get(k):+.3f}" for k in keys
                               if isinstance(snapshot.get(k), (int, float)))
            return SpecialistRead(self.name, "FLAT", 0.0, self.horizon_bars,
                                  (detail or "fresh macro observed")[:180],
                                  role=self.role, probability_up=0.5)

        if self.mode == "health":
            tick_age = snapshot.get("tick_age_s")
            frames = snapshot.get("sensor.live_frame_count")
            if not isinstance(tick_age, (int, float)) or frames != 5:
                return SpecialistRead(
                    self.name, "FLAT", 0.0, self.horizon_bars,
                    f"sensor incomplete: tick_age={tick_age}, live_frames={frames}",
                    available=False, role=self.role)
            return SpecialistRead(
                self.name, "FLAT", 0.0, self.horizon_bars,
                f"quote age {tick_age:.1f}s; all five live frames present",
                role=self.role, probability_up=0.5)

        return SpecialistRead(self.name, "FLAT", 0.0, self.horizon_bars,
                              f"unknown sensor mode {self.mode}", available=False,
                              role=self.role)


def built_in_sensor_specialists() -> dict[str, Specialist]:
    """Real local sensors available without pretending missing feeds exist.

    Argus, Orion and Mnemosyne remain unavailable until their actual visual,
    COMEX and structured-memory inputs are placed in the frozen snapshot.
    """
    def sequence_path(rows):
        closes = [r[3] for r in rows]
        if len(closes) < 20:
            return 0.0
        recent = closes[-8:]
        scale = max(max(recent) - min(recent), 1e-9)
        return max(-0.8, min(0.8, (recent[-1] - recent[0]) / scale * 0.65))

    return {
        "atlas": ContextSensorSpecialist("atlas-sensor", "macro", "macro context", 4),
        "lumen": ContextSensorSpecialist(
            "structure-sensor", "technical", "technical setup scouting", 1),
        "apollo": ContextSensorSpecialist(
            "session-sensor", "session", "events and session context", 1),
        "chronos": SequenceSpecialist(
            name="closed-path-sensor", predict_fn=sequence_path, horizon_bars=4,
            key_prefix="entry", min_bars=20,
            role="sequence and path dependence"),
        "hephaestus": ContextSensorSpecialist(
            "health-sensor", "health", "data and operational health", 1),
    }


# ------------------------------------------------------------------ the council

@dataclass
class Council:
    """Every specialist's read on one state. NO consensus method, by design."""
    specialists: list = field(default_factory=list)

    def read(self, snapshot) -> list[SpecialistRead]:
        """Read every seat independently against one immutable state.

        Failure isolation belongs here, not in individual implementations.  A
        third-party specialist cannot be trusted to catch its own exception or
        to return a valid SpecialistRead, and one broken seat must never blind
        the other seven or stop the desk.
        """
        state_id, content_hash = snapshot.state_id, snapshot.content_hash
        reads: list[SpecialistRead] = []
        for specialist in self.specialists:
            name = str(getattr(specialist, "name", type(specialist).__name__))
            role = str(getattr(specialist, "role", ""))
            try:
                read = specialist.read(snapshot)
                if not isinstance(read, SpecialistRead):
                    raise TypeError(
                        f"returned {type(read).__name__}, expected SpecialistRead")
                if snapshot.state_id != state_id or snapshot.content_hash != content_hash:
                    raise RuntimeError("mutated the frozen causal snapshot")
                meta = dict(read.meta)
                meta.update({"state_id": state_id, "content_hash": content_hash})
                reads.append(replace(read, name=name, role=read.role or role,
                                     meta=meta))
            except Exception as e:                       # noqa: BLE001
                reads.append(SpecialistRead(
                    name, "FLAT", 0.0, available=False, role=role,
                    why=f"isolated {type(e).__name__}: {e}",
                    meta={"state_id": state_id, "content_hash": content_hash,
                          "failure_isolated": True}))
        return reads

    def report(self, snapshot) -> dict:
        reads = self.read(snapshot)
        live = [r for r in reads if r.available]
        dirs = {r.direction for r in live if r.direction != "FLAT"}
        return {
            "version": SPECIALIST_VERSION,
            "state_id": snapshot.state_id,
            "content_hash": snapshot.content_hash,
            "reads": reads,
            "available": len(live),
            "unavailable": [r.name for r in reads if not r.available],
            # Reported as a MEASUREMENT, never used to weight anything. Two
            # specialists reading the same bars agree structurally, and treating
            # that agreement as corroboration is the error this class exists to
            # avoid.
            "agreement": ("NONE" if not live else
                          "SPLIT" if len(dirs) > 1 else
                          "UNANIMOUS" if dirs else "ALL_FLAT"),
            "note": ("Disagreement is information and is preserved. There is no "
                     "consensus here on purpose: these readers share their "
                     "inputs, so averaging them manufactures confidence out of "
                     "structural agreement and erases the divergence that was "
                     "the only interesting output."),
        }


def build_desk_council(
    overrides: Optional[Mapping[str, Specialist]] = None,
) -> Council:
    """Build all eight named seats, leaving unconfigured ones unavailable.

    `overrides` is keyed by the stable lower-case role key.  Supplying an
    implementation is configuration, not promotion: its verdicts are still
    shadowed and must earn standing from paired outcomes before the analyst is
    shown them.
    """
    supplied = dict(overrides or {})
    unknown = set(supplied) - set(ROLE_BY_KEY)
    if unknown:
        raise ValueError(f"unknown specialist roles: {sorted(unknown)}")
    seats = []
    for role in DESK_ROLES:
        specialist = supplied.get(role.key)
        if specialist is None:
            specialist = UnavailableSpecialist(
                role.name, "no implementation configured for this seat",
                role.responsibility)
        else:
            specialist = SeatSpecialist(role.name, role.responsibility,
                                         specialist)
        seats.append(specialist)
    return Council(seats)


# -------------------------------------------------- what is a specialist worth?

@dataclass
class MarginalValue:
    """The only question that matters: did it change anything, and did that pay?"""
    specialist: str
    n_states: int
    n_changed: int
    net_r: Optional[float]
    mean_r_per_change: Optional[float]
    t_stat: Optional[float]
    cost_r: float
    verdict: str
    why: str

    @property
    def has_standing(self) -> bool:
        return self.verdict == "POSITIVE"

    def render(self) -> str:
        head = (f"MARGINAL VALUE — {self.specialist}\n"
                f"  states seen        {self.n_states}\n"
                f"  decisions CHANGED  {self.n_changed}"
                f"  ({100 * self.n_changed / self.n_states:.1f}%)"
                if self.n_states else f"MARGINAL VALUE — {self.specialist}\n  no states")
        if self.n_changed and self.net_r is not None:
            head += (f"\n  net over changes   {self.net_r:+.2f}R "
                     f"({self.mean_r_per_change:+.3f}R each, cost "
                     f"{self.cost_r:.2f}R charged per change)")
            if self.t_stat is not None:
                head += f"\n  t                  {self.t_stat:+.2f}"
        return f"{head}\n  {self.verdict}: {self.why}"


def marginal_value(specialist: str,
                   with_spec: Sequence[str],
                   without_spec: Sequence[str],
                   realised_r: Sequence[float],
                   counterfactual_r: Sequence[float],
                   cost_r: float = DEFAULT_CHANGE_COST_R,
                   min_changed: int = MIN_CHANGED) -> MarginalValue:
    """Paired, on identical states, over the CHANGED decisions only.

    `realised_r[i]`        what the with-specialist decision paid
    `counterfactual_r[i]`  what the without-specialist decision would have paid

    Unchanged states are excluded rather than counted as ties. Including them
    adds identical outcomes to both arms: the difference cannot move, but the
    sample inflates and the standard error shrinks. That is a smaller p-value
    computed from observations carrying no information about the question — a
    significance result manufactured out of agreement.
    """
    n = min(len(with_spec), len(without_spec), len(realised_r),
            len(counterfactual_r))
    changed = [i for i in range(n) if with_spec[i] != without_spec[i]]
    if not changed:
        return MarginalValue(specialist, n, 0, None, None, None, cost_r,
                             "NO STANDING",
                             "the specialist never changed a decision. Whatever "
                             "its accuracy, it added nothing the desk did not "
                             "already do.")
    deltas = [float(realised_r[i]) - float(counterfactual_r[i]) - cost_r
              for i in changed
              if math.isfinite(realised_r[i]) and math.isfinite(counterfactual_r[i])]
    if len(deltas) < min_changed:
        return MarginalValue(specialist, n, len(deltas),
                             sum(deltas) if deltas else None,
                             (sum(deltas) / len(deltas)) if deltas else None,
                             None, cost_r, "NO STANDING",
                             f"{len(deltas)} changed decisions, {min_changed} "
                             f"required. Too few to say anything either way — "
                             f"which is the right answer for a new specialist.")
    net = sum(deltas)
    mean = net / len(deltas)
    sd = (sum((d - mean) ** 2 for d in deltas) / (len(deltas) - 1)) ** 0.5
    # ZERO VARIANCE IS THE STRONGEST EVIDENCE, NOT THE WEAKEST, and the previous
    # `t = 0.0 if sd == 0` said the opposite: a specialist that improved every
    # single decision by an identical amount was filed as "indistinguishable
    # from noise".
    #
    # It was also non-deterministic. With every delta identical, whether the
    # summation leaves a 1-ULP residue decides whether sd is a tiny positive
    # number or exactly zero — so the same input returned POSITIVE on one
    # interpreter and UNPROVEN on another. A verdict must not turn on rounding.
    # NEGLIGIBLE, not merely zero. Testing `sd <= 0` still leaves the answer to
    # the interpreter: with identical deltas one machine's summation leaves a
    # 1-ULP residue (sd ~1e-16, t ~4e15) and another's cancels exactly (sd = 0,
    # t = 0). Both describe the same deterministic sample and must reach the
    # same verdict, so the comparison is against the scale of the data rather
    # than against zero.
    scale = max(abs(mean), 1e-12)
    if sd <= 1e-9 * scale:
        t = math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
    else:
        t = mean / (sd / math.sqrt(len(deltas)))
    if mean <= 0:
        return MarginalValue(specialist, n, len(deltas), net, mean, t, cost_r,
                             "NEGATIVE",
                             "its changes cost more than they earned, net of the "
                             f"{cost_r:.2f}R charged per flip. A specialist that "
                             "changes decisions for the worse is worse than none.")
    if abs(t) < 2.0:
        return MarginalValue(specialist, n, len(deltas), net, mean, t, cost_r,
                             "UNPROVEN",
                             f"positive but indistinguishable from noise "
                             f"(t={t:+.2f}). Keep it in shadow and accumulate.")
    return MarginalValue(specialist, n, len(deltas), net, mean, t, cost_r,
                         "POSITIVE",
                         "it changed decisions and the changes paid after cost. "
                         "One test, uncorrected for the desk's accumulated "
                         "multiplicity — seal it before it sizes anything.")
