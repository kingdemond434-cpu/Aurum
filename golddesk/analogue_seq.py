"""A sequence specialist that needs no weights: the desk's own history as the model.

`specialists.SequenceSpecialist` takes a `predict_fn` and deliberately bundles
nothing, so it has been UNAVAILABLE since it was written. The obvious fix is to
download a pretrained candlestick foundation model. The free fix is better for
this desk, and this is it.

WHY ANALOGUE MATCHING RATHER THAN A DOWNLOADED TRANSFORMER

A pretrained sequence model is a compressed statement about the markets it was
trained on — 45 exchanges, mostly equities and crypto, at timeframes chosen by
somebody else. Gold's session structure is the entire strategy here, and a model
that never saw a London fix has no representation of it. Fine-tuning would fix
that and costs GPU hours plus a fine-tuning corpus the desk does not have.

Nearest-neighbour matching over the desk's OWN normalised history is orthogonal
to the engineered features in exactly the way a specialist is supposed to be —
it reads shape, not indicators — costs nothing, needs no weights, and its
prediction is inspectable: you can look at the twenty historical bars it matched
and judge whether they resemble today. A transformer's answer cannot be argued
with; this one can.

THE THREE WAYS ANALOGUE MATCHING CHEATS, AND WHAT IS DONE ABOUT EACH

1. IT MATCHES ITSELF. The nearest neighbour of a window is the window, and the
   next-nearest are its overlapping siblings — which contain the very bars being
   predicted. `_eligible` enforces a gap of at least the window length plus the
   horizon between a query and any neighbour, so a match cannot share a single
   bar with the thing it is forecasting.

2. IT MATCHES THE LEVEL, NOT THE SHAPE. Raw prices make 2026's gold match only
   2026, because everything else is at a different level. Windows are z-scored
   within themselves, so a 2019 pattern at $1,500 can match a 2026 pattern at
   $4,300 when the SHAPE agrees — which is the only thing that should transfer.

3. IT ANSWERS ANYWAY. With no close analogue the mean of the k nearest is still
   a number, and it is noise wearing a decimal point. A distance floor makes the
   specialist return UNAVAILABLE rather than a confident average over
   twenty things that look nothing like today.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

ANALOGUE_VERSION = "analogue-2026-08-18-a"

#: Neighbours averaged. Small enough that one good match still shows through,
#: large enough that a single coincidence does not carry the answer.
DEFAULT_K = 20

#: Mean normalised distance beyond which the neighbours are not analogues. A
#: z-scored window of length L has expected squared distance ~2L against an
#: unrelated window, so a normalised distance near 1.0 IS unrelated.
MAX_MEAN_DISTANCE = 0.85

#: History windows required before the specialist will answer at all.
MIN_HISTORY = 500


def _z(window: np.ndarray) -> Optional[np.ndarray]:
    """Standardise a window within itself. Level out, shape in."""
    sd = window.std()
    if sd <= 0:
        return None                    # a flat window has no shape to match
    return (window - window.mean()) / sd


@dataclass
class AnalogueModel:
    """The desk's own closes, indexed as overlapping normalised windows."""
    window: int = 24
    horizon: int = 4
    k: int = DEFAULT_K
    max_distance: float = MAX_MEAN_DISTANCE
    _shapes: Optional[np.ndarray] = None
    _outcomes: Optional[np.ndarray] = None
    _ends: Optional[np.ndarray] = None

    def fit(self, closes: Sequence[float]) -> "AnalogueModel":
        """Build the library. Every window keeps the index of its LAST bar, so
        eligibility can be judged by distance in time rather than by position in
        an array that has already been filtered."""
        c = np.asarray(closes, dtype=float)
        c = c[np.isfinite(c)]
        shapes, outs, ends = [], [], []
        for s in range(len(c) - self.window - self.horizon + 1):
            w = c[s:s + self.window]
            z = _z(w)
            if z is None:
                continue
            end = s + self.window - 1
            fwd = c[end + self.horizon] - c[end]
            # Outcome in units of the window's OWN volatility, so a match from a
            # calm 2019 and one from a violent 2026 contribute comparable
            # numbers. An outcome in dollars would let high-volatility eras
            # dominate the average for reasons unrelated to shape.
            outs.append(fwd / w.std())
            shapes.append(z)
            ends.append(end)
        if shapes:
            self._shapes = np.vstack(shapes)
            self._outcomes = np.asarray(outs)
            self._ends = np.asarray(ends)
        return self

    @property
    def n(self) -> int:
        return 0 if self._shapes is None else len(self._shapes)

    def _eligible(self, query_end: int) -> np.ndarray:
        """Neighbours that share no bar with the query or its forward window.

        THE LEAK THIS EXISTS TO STOP. The nearest neighbour of a window is the
        window itself, and the next-nearest are its overlapping siblings, which
        contain the exact bars being predicted. Without this the specialist
        scores beautifully and has learned nothing.
        """
        gap = self.window + self.horizon
        return np.abs(self._ends - query_end) >= gap

    def predict(self, recent_closes: Sequence[float],
                query_end: Optional[int] = None) -> tuple:
        """(signed strength in [-1, 1], why). Returns (None, why) when unsure."""
        if self.n < MIN_HISTORY:
            return None, (f"{self.n} historical windows, {MIN_HISTORY} required. "
                          f"An analogue library this thin has no analogues in it.")
        c = np.asarray(recent_closes, dtype=float)
        if len(c) < self.window:
            return None, f"{len(c)} bars supplied, {self.window} required"
        q = _z(c[-self.window:])
        if q is None:
            return None, "the query window is flat; there is no shape to match"

        mask = (self._eligible(query_end) if query_end is not None
                else np.ones(self.n, dtype=bool))
        if mask.sum() < self.k:
            return None, (f"only {int(mask.sum())} neighbours are far enough from "
                          f"the query to be independent of it")

        d = np.sqrt(((self._shapes[mask] - q) ** 2).sum(axis=1) / self.window)
        outs = self._outcomes[mask]
        idx = np.argsort(d)[:self.k]
        mean_d = float(d[idx].mean())
        if mean_d > self.max_distance:
            # THE REFUSAL. With no close analogue the mean of k neighbours is
            # still a number, and it is noise wearing a decimal point.
            return None, (f"nearest {self.k} analogues average distance "
                          f"{mean_d:.2f} > {self.max_distance}: nothing in the "
                          f"desk's history looks like now, so there is no "
                          f"analogue read to give.")

        # Inverse-distance weighting: a genuinely close match should count for
        # more than the twentieth-nearest, and a flat average throws that away.
        w = 1.0 / (d[idx] + 1e-9)
        raw = float((outs[idx] * w).sum() / w.sum())
        # Squash to [-1, 1]. The magnitude is in volatility units and can be
        # large; a specialist is only entitled to a bounded opinion.
        strength = math.tanh(raw)
        agree = float(np.mean(np.sign(outs[idx]) == np.sign(raw)))
        return strength, (f"{self.k} analogues at mean distance {mean_d:.2f}, "
                          f"{agree:.0%} agreeing on direction, forward move "
                          f"{raw:+.2f} in window-volatility units")


def build_specialist(closes: Sequence[float], window: int = 24,
                     horizon: int = 4, k: int = DEFAULT_K):
    """A `SequenceSpecialist` backed by the desk's own history. No weights.

    Returns the specialist ready to hand to a `Council`. Because the model is
    the desk's own past, it needs nothing downloaded, nothing licensed, and no
    GPU — and its answer can be inspected by looking at what it matched.
    """
    from golddesk.specialists import SequenceSpecialist
    model = AnalogueModel(window=window, horizon=horizon, k=k).fit(closes)

    def predict_fn(bars):
        # `bars` arrives oldest-first as [open, high, low, close] rows.
        closes_in = [row[3] for row in bars]
        strength, why = model.predict(closes_in)
        if strength is None:
            # Raising is how SequenceSpecialist learns this is UNAVAILABLE
            # rather than FLAT — and the distinction is the whole reason
            # UnavailableSpecialist exists.
            raise RuntimeError(why)
        return strength

    return SequenceSpecialist(name="analogue", predict_fn=predict_fn,
                              horizon_bars=horizon, min_bars=window)
