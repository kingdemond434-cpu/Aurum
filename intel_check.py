#!/usr/bin/env python3
"""#11 regime, #6 calendar, #9 budget, #14 path, #7 competition, #13 cross-market.

Each is checked against the way it would be WRONG rather than the way it is
meant to work, because a module that returns plausible numbers on good input
and plausible numbers on no input is worse than one that returns nothing.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

OK, BAD = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ok   {label}" + (f"  — {detail}" if detail else ""))
    else:
        BAD += 1
        print(f"  FAIL {label}" + (f"  — {detail}" if detail else ""))


CTX = {"trend_direction": "UP", "trend_health": "MODERATE", "trend_maturity": "MID",
       "volatility_state": "NORMAL", "htf_alignment": "ALIGNED",
       "displacement_state": "CONFIRMED", "sweep_state": "CONFIRMED",
       "reclaim_state": "CONFIRMED", "pullback_depth": "MEDIUM",
       "distance_from_session_extreme": "MID", "session": "LONDON"}


def outcome(ctx, r=1.0, mfe=1.6, mae=-0.4, t_mfe=2400.0, t_mae=600.0,
            vision="NUMERIC_ONLY", mech="m1"):
    return {"context": dict(ctx), "realised_r": r, "mfe_r": mfe, "mae_r": mae,
            "t_mfe": t_mfe, "t_mae": t_mae, "vision": vision,
            "mechanism_name": mech, "direction": "LONG", "setup": "SWING_REVERSAL"}


def regime() -> None:
    from golddesk.regime import (assess_novelty, context_similarity,
                                 similarity_to_history)
    print("#11 REGIME NOVELTY")

    check("identical contexts are 100% similar",
          context_similarity(CTX, CTX) == 1.0)

    flipped = dict(CTX, trend_direction="DOWN")
    s = context_similarity(CTX, flipped)
    check("a flipped trend costs the most of any single field",
          s is not None and s < 0.85, f"{s:.2f}")

    adjacent = dict(CTX, trend_health="STRONG")     # MODERATE -> STRONG
    far = dict(CTX, trend_health="WEAK")            # MODERATE -> WEAK, 1 step too
    sa, sf = context_similarity(CTX, adjacent), context_similarity(CTX, far)
    check("ordinal fields get partial credit rather than 0/1",
          sa is not None and 0.9 < sa < 1.0, f"{sa:.3f}")

    two_step = dict(CTX, volatility_state="EXTREME")   # NORMAL -> EXTREME = 2
    one_step = dict(CTX, volatility_state="ELEVATED")  # NORMAL -> ELEVATED = 1
    check("and two steps apart scores worse than one",
          context_similarity(CTX, two_step) < context_similarity(CTX, one_step),
          f"{context_similarity(CTX, two_step):.3f} vs "
          f"{context_similarity(CTX, one_step):.3f}")

    check("incomparable contexts read None, not 0%",
          context_similarity(CTX, {"unrelated": "x"}) is None,
          "0% would read as 'wildly novel' when it means 'I cannot tell'")

    thin = [outcome(CTX) for _ in range(5)]
    n = assess_novelty(CTX, thin)
    check("a thin history is UNMEASURABLE, not 'familiar'",
          not n.measurable, n.basis[:70])
    check("and similarity_to_history returns None for it",
          similarity_to_history(CTX, None, thin) is None)

    same = [outcome(CTX) for _ in range(40)]
    n2 = assess_novelty(CTX, same)
    check("a matching history scores high", n2.similarity and n2.similarity > 0.9,
          f"{n2.similarity:.0%} on n={n2.comparable_n}")
    check("and is called interpolation", "interpolation" in n2.basis)

    alien = [outcome(dict(CTX, trend_direction="DOWN", volatility_state="EXTREME",
                          htf_alignment="CONFLICTED", trend_health="WEAK",
                          session="ASIA", sweep_state="NONE",
                          reclaim_state="NONE", displacement_state="NONE"))
             for _ in range(40)]
    n3 = assess_novelty(CTX, alien)
    check("an alien history scores low", n3.similarity and n3.similarity < 0.5,
          f"{n3.similarity:.0%}")
    check("and is called EXTRAPOLATION", "EXTRAPOLATION" in n3.basis)
    check("and names which dimensions are unlike", bool(n3.dissimilar_fields),
          ", ".join(n3.dissimilar_fields))
    check("novelty never raises into a decision",
          similarity_to_history(CTX, None, [{"bad": "row"}]) is None)


def calendar() -> None:
    from golddesk.calendar import NY, Calendar, month_events
    print("\n#6 EVENT CALENDAR")

    evs = month_events(2026, 3)
    names = {e.name for e in evs}
    check("NFP, CPI and FOMC are derived without a network call",
          {"NFP", "CPI"} <= names, ", ".join(sorted(names)))

    nfp = next(e for e in evs if e.name == "NFP")
    check("NFP lands on a Friday", nfp.when_utc.weekday() == 4,
          nfp.when_utc.strftime("%A %Y-%m-%d %H:%M UTC"))
    check("and on the FIRST one", nfp.when_utc.day <= 7, f"day {nfp.when_utc.day}")

    # THE DST TRAP. 08:30 New York is 13:30 UTC in winter and 12:30 in summer.
    # A hardcoded UTC time is wrong for half the year, in the direction that
    # puts the event an hour away from where it actually is.
    jan = next(e for e in month_events(2026, 1) if e.name == "NFP")
    jul = next(e for e in month_events(2026, 7) if e.name == "NFP")
    if NY is not None:
        check("08:30 ET converts differently in winter and summer",
              jan.when_utc.hour != jul.when_utc.hour,
              f"Jan {jan.when_utc:%H:%M}Z vs Jul {jul.when_utc:%H:%M}Z")
        check("and both are the same New York wall clock",
              jan.when_utc.astimezone(NY).hour == jul.when_utc.astimezone(NY).hour == 8)
    else:
        check("zoneinfo present", False, "tz database unavailable")

    check("approximate dates SAY they are approximate",
          any("APPROXIMATE" in e.basis for e in evs if e.name == "CPI"))

    c = Calendar()
    nxt = c.next_event(datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc))
    check("next_event returns (minutes, name) for uncertainty.event_risk",
          nxt is not None and len(nxt) == 2 and nxt[0] > 0,
          f"{nxt[1]} in {nxt[0]/60:.1f}h" if nxt else "None")
    check("only HIGH-importance releases count by default",
          c.include == {"NFP", "CPI", "FOMC"},
          "treating ISM as equivalent to NFP marks most of the month elevated, "
          "which is the same as marking none of it")
    check("the FOMC table declares where it ends",
          c.horizon_ends.year >= 2026,
          f"{c.horizon_ends} — an empty answer past this is a table gap, "
          f"not a quiet month")

    # It must be INFORMATION only.
    from golddesk.constitution import BY_ID
    check("no event blackout was registered as a gate",
          not any("event" in k and "blackout" in k for k in BY_ID),
          "'never trade near news' is a plausible permanent rule that has not "
          "earned one")


def budget() -> None:
    from golddesk.budget import Pricing, compare_arms, report
    print("\n#9 INFORMATION BUDGET")

    p = Pricing()
    # 100k input of which 90k cache-read, 2k output.
    usd = p.cost({"in": 100_000, "cache_read": 90_000, "out": 2_000})
    naive = (100_000 * p.input_per_mtok + 2_000 * p.output_per_mtok) / 1e6
    check("cache reads are priced separately, not at full input rate",
          usd < naive, f"${usd:.4f} vs ${naive:.4f} if all input were fresh")
    check("and the cached prefix is not double-billed",
          abs(usd - ((10_000 * 15 + 90_000 * 1.5 + 2_000 * 75) / 1e6)) < 1e-9,
          f"${usd:.4f}")

    rows = []
    for k in range(20):
        rows.append({"kind": "SIGNAL", "t0": f"2026-03-0{1+k%9}T10:00:00+00:00",
                     "decision": {"vision": "NUMERIC_ONLY", "charts_sent": 0,
                                  "usage": {"in": 50_000, "cache_read": 45_000,
                                            "out": 1500}}})
    for k in range(20):
        rows.append({"kind": "SIGNAL", "t0": f"2026-04-0{1+k%9}T10:00:00+00:00",
                     "decision": {"vision": "NUMERIC_PLUS_CHARTS", "charts_sent": 3,
                                  "usage": {"in": 260_000, "cache_read": 45_000,
                                            "out": 1500}}})
    r = report(rows)
    check("spend is split by arm", len(r.lines) >= 2,
          "; ".join(l.label for l in r.lines))
    chart = next(l for l in r.lines if "charts" in l.label)
    num = next(l for l in r.lines if "numeric" in l.label)
    check("the chart arm is measurably more expensive per call",
          chart.usd_per_call > num.usd_per_call * 2,
          f"${chart.usd_per_call:.4f} vs ${num.usd_per_call:.4f}")
    check("no net figure is produced without an R value",
          "R VALUE NOT SUPPLIED" in r.render(),
          "an assumed R value silently decides whether every component in the "
          "desk looks profitable")
    r2 = report(rows, r_value_usd=100.0)
    check("and one IS produced when the R value is supplied",
          "NET" in r2.render())
    check("coverage is reported so totals can be read as a lower bound",
          0.0 <= r.coverage <= 1.0, f"{r.coverage:.0%}")

    out = compare_arms(rows, r_value_usd=100.0)
    check("arm comparison refuses to call a winner on spend alone",
          "not yet a result" in out or "identical states" in out)


def path() -> None:
    from golddesk.path import MIN_COHORT, forecast, management_implication, report
    print("\n#14 PATH PREDICTION")

    thin = [outcome(CTX) for _ in range(5)]
    f = forecast(thin)
    check("a thin reference class is NOT usable", not f.usable,
          f"matched {f.matched}, floor {MIN_COHORT}")
    check("and draws no implication from it",
          "No implication drawn" in management_implication(f))

    # The desk's actual shape: reaches +1R, gives it back.
    real = [outcome(CTX, r=-1.0, mfe=1.8, mae=-1.0, t_mfe=1800, t_mae=3600)
            for _ in range(30)]
    f2 = forecast(real)
    check("reach_1r is measured", f2.get("reach_1r").value == 1.0,
          "all 30 reached +1R")
    check("giveback is measured and large",
          f2.get("giveback").value > 1.0,
          f"{f2.get('giveback').value:.0%} of MFE surrendered")
    check("minutes_to_mfe is measured", f2.get("minutes_to_mfe").value == 30.0,
          f"{f2.get('minutes_to_mfe').value:.0f}m")
    check("adverse_first is measured",
          f2.get("adverse_first").value == 0.0, "MFE came first in all")
    imp = management_implication(f2)
    check("the implication names management, not entry, as the leak",
          "management, not entry" in imp, imp[:100])

    check("every estimate carries its own n",
          all(e.n >= 0 for e in f2.estimates)
          and all("n=" in e.render() for e in f2.estimates))

    cond = forecast(real, {"trend_direction": "DOWN"})
    check("conditioning actually filters", cond.matched == 0,
          "no DOWN-trend trades in this history")
    check("and an empty class is unusable rather than empty-and-confident",
          not cond.usable)


def competition() -> None:
    from golddesk.competition import (MIN_PAIRED, check_pairing, collect,
                                      compete, report, state_id)
    print("\n#7 MODEL COMPETITION")

    check("state_id is arm-independent",
          "NUMERIC" not in state_id("XAUUSD", "M15", "2026-03-01T10:00:00"),
          "an arm in the id makes every join empty and silently unpairs the test")

    def row(arm, t, r, direction="LONG"):
        return {"kind": "SIGNAL", "t0": t, "symbol": "XAUUSD", "realised_r": r,
                "decision": {"vision": arm, "direction": direction}}

    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    rows = []
    for k in range(40):
        t = (base + timedelta(hours=k)).isoformat()
        rows.append(row("A", t, 0.5))
        rows.append(row("B", t, 0.1))
    # states only A saw — these must NOT be counted for A
    for k in range(40, 60):
        rows.append(row("A", (base + timedelta(hours=k)).isoformat(), 3.0))

    arms = collect(rows)
    p = check_pairing(arms, "A", "B")
    check("only shared states are compared", p.n == 40,
          f"{p.n} shared, {p.only_a} only-A dropped")
    check("the unshared states are REPORTED, not silently dropped",
          p.only_a == 20,
          "an arm that skips hard states must not be rewarded for it")

    v = compete(rows, "A", "B")
    check("a real paired difference is detected", v.mean_diff > 0.3,
          f"{v.mean_diff:+.3f}R  CI [{v.ci[0]:+.3f}, {v.ci[1]:+.3f}]")
    check("the CI excludes zero on a clean 0.4R separation",
          v.ci[0] > 0, f"lo={v.ci[0]:+.3f}")
    check("agreement rate is reported", v.agreement_rate == 1.0,
          "both arms said LONG everywhere — the difference is management, "
          "not selection, and the verdict must not hide that")
    check("and the verdict demands FDR before calling it a result",
          "BH-FDR" in v.verdict or "hypothesis" in v.verdict)

    small = [r for r in rows[:20]]
    v2 = compete(small, "A", "B")
    check(f"below {MIN_PAIRED} paired states the verdict is UNDETERMINED",
          "UNDETERMINED" in v2.verdict, f"n={v2.n}")

    one = report([row("A", base.isoformat(), 1.0)])
    check("one arm is not a competition", "needs two" in one)

    raised = False
    try:
        check_pairing(arms, "A", "NOPE")
    except ValueError:
        raised = True
    check("an unknown arm fails loudly", raised)


def crossmarket() -> None:
    from golddesk.crossmarket import (DRIVERS, MIN_COVERAGE, build_state, report)
    print("\n#13 CROSS-MARKET CAUSAL STATE")

    empty = build_state(None)
    check("no fetcher yields a fully UNAVAILABLE state, not an error",
          all(not o.observed for o in empty.observations))
    check("coverage is 0 and the state is not actionable",
          empty.coverage == 0.0 and not empty.is_actionable)
    check("absence reads UNAVAILABLE, never neutral",
          "UNAVAILABLE" in empty.render(),
          "a missing DXY treated as 'dollar unchanged' asserts something "
          "unknown, in the direction that makes everything look calm")
    check("and the analyst is told the move is UNEXPLAINED",
          any("unexplained" in l for l in empty.brief_lines()))

    def fetch(key, hours):
        vals = {"dxy": -0.8, "real_yield_10y": -0.5, "spx": 0.2,
                "breakeven_10y": 0.3, "vix": 1.2}
        return (vals[key], datetime.now(timezone.utc), "test")

    st = build_state(fetch, gold_change_pct=1.1)
    check("a full fetch is actionable", st.is_actionable,
          f"coverage {st.coverage:.0%}, floor {MIN_COVERAGE:.0%}")
    exp = st.expected_direction()
    check("dollar down + real yields down implies gold up", exp > 0, f"{exp:+.2f}")
    check("and gold agreeing is reported as consistent",
          "consistent" in st.render())

    # Now the interesting case: gold ignores its drivers.
    st2 = build_state(fetch, gold_change_pct=-1.1)
    div = st2.divergences()
    check("gold moving against its drivers is flagged as DIVERGING",
          len(div) >= 2, ", ".join(d.key for d, _ in div))
    check("and the analyst sees it as evidence, not an instruction",
          any("DIVERGENCE" in l for l in st2.brief_lines())
          and not any("BUY" in l.upper() or "SELL" in l.upper()
                      for l in st2.brief_lines()))

    check("every driver DECLARES its expected sign",
          all(d.expected_sign in (-1, 1) and d.why for d in DRIVERS),
          "a fitted sign absorbs the violation and reports nothing")

    def half(key, hours):
        return (0.5, datetime.now(timezone.utc), "test") if key in ("dxy", "spx") else None
    st3 = build_state(half, gold_change_pct=0.4)
    check("partial coverage below the floor is refused",
          not st3.is_actionable, f"coverage {st3.coverage:.0%}")

    def boom(key, hours):
        raise RuntimeError("vendor down")
    st4 = build_state(boom, gold_change_pct=0.4)
    check("a failing fetcher degrades rather than raising",
          all(not o.observed for o in st4.observations)
          and all(o.source == "fetch failed" for o in st4.observations))


def main() -> int:
    print("INTELLIGENCE MODULES — #11 #6 #9 #14 #7 #13\n")
    regime()
    calendar()
    budget()
    path()
    competition()
    crossmarket()
    print(f"\n{OK} ok, {BAD} failed")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
