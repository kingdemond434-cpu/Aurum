"""The daily cycle. What makes Aurum compound instead of merely exist.

    python3 aurum_cycle.py                  # run today's cycle
    python3 aurum_cycle.py --dry            # run, send nothing
    python3 aurum_cycle.py --force          # run again even if today is stamped

WHY THIS FILE EXISTS

Eleven modules were built and tested and eight of them were imported by nothing.
That is not a codebase with capabilities, it is a codebase with plans — the
desk's own `live.py` says so in its docstring, and then the same thing happened
one directory over. A tested module on no execution path changes no decision and
produces no evidence; it is indistinguishable from a design document that
happens to run.

So this is the executable path for everything that is supposed to happen daily,
and its rule is simple: every step either does its work or says precisely why it
could not, and a step that fails DOES NOT ABORT THE REST. A cycle that stops at
the first problem loses the six things that would have worked, which is how a
desk goes dark for a week over one bad fetch.

WHAT "SELF-IMPROVING" HONESTLY MEANS HERE

It does not mean the desk edits itself. Nothing in this cycle may change a
threshold, loosen a gate, or promote anything — those all live behind the
promotion gate on forward evidence, and a loop that could widen its own limits
would, because looser gates produce more signals and more signals feel like
progress.

What compounds is the RECORD: one more day of resolved outcomes, one more day of
refusals with their forgone value attached, findings from the quant desk queued
as claims Aurum can be wrong about, a trial census that grows so every
significance number gets harder rather than easier, and a size recommendation
re-solved from the ledger as it stands today. The desk gets smarter by
accumulating things it cannot argue with.

THE STAMP RECORDS THE ATTEMPT, NOT THE SUCCESS

If the stamp were written only on a clean run, a cycle that failed one step
would re-run every step on the next invocation — re-sending notifications,
re-queueing findings, and double-counting anything not perfectly idempotent. The
date is stamped because the cycle RAN.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

BASE = Path(__file__).parent
STATE_DIR = BASE / "state"
LEDGER = STATE_DIR / "ledger.jsonl"
CYCLE_STATE = STATE_DIR / "cycle_state.json"
REPORT_DIR = BASE / "reports"
LOG = STATE_DIR / "cycle.log"

CYCLE_VERSION = "cycle-2026-08-18-a"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _rows(path: Path | None = None, limit: int = 100_000) -> list:
    """Ledger rows. A torn final line costs one row, never the whole cycle.

    The default is resolved AT CALL TIME rather than written as `path=LEDGER`.
    A module constant captured in a default argument is frozen at import, so the
    parameter only looks configurable: relocating the desk — or a test pointing
    at a fixture — would silently keep reading the original path and report an
    empty ledger for a file full of rows. That is exactly what happened here.
    """
    path = path if path is not None else LEDGER
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-limit:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _resolved_r(rows: list) -> list:
    return [float(r["realised_r"]) for r in rows
            if isinstance(r.get("realised_r"), (int, float))]


# --------------------------------------------------------------- the steps

def step_evidence(ctx: dict) -> str:
    """How much forward evidence exists. Everything downstream is scaled by it."""
    rows = _rows()
    ctx["rows"] = rows
    rs = _resolved_r(rows)
    ctx["r_multiples"] = rs
    refusals = [r for r in rows if str(r.get("kind", "")).upper()
                in ("NO_SETUP", "REFUSED", "REFUSAL", "VETO", "BLOCKED")]
    ctx["refusals"] = refusals
    if not rows:
        return ("LEDGER EMPTY. No forward evidence exists yet, so every number "
                "below is a null and not a result. This is the honest state of a "
                "desk that has not yet run, not a finding about gold.")
    return (f"{len(rows)} ledger rows | {len(rs)} resolved | "
            f"{len(refusals)} refusals recorded")


def step_growth(ctx: dict) -> str:
    """Re-solve the size from the ledger as it stands TODAY.

    Re-solved every day rather than configured once: the drawdown estimate, the
    measured breadth and therefore the heat budget all move as evidence accrues,
    and a size fixed at the moment of deployment stops tracking the book it was
    derived from.
    """
    from golddesk.growth import recommend
    rs = ctx.get("r_multiples") or []
    if not rs:
        return ("no resolved R-multiples, so no size is supported. That is not a "
                "zero-risk book — it is a book nobody has watched long enough to "
                "solve a size from.")
    rec = recommend(rs, rows=ctx.get("rows") or [])
    ctx["growth"] = rec
    return rec.render()


def step_attribution(ctx: dict) -> str:
    """What moved gold, and whether knowing it changes a decision."""
    from golddesk.attribution import ATTRIBUTION_VERSION
    drivers = ctx.get("driver_series")
    if not drivers:
        # No fitted decomposition without a history of driver moves, but the
        # LIVE READING is available for free and is worth reporting on its own:
        # "gold is up with the dollar up and real yields up" is a genuinely
        # unusual configuration whether or not a regression is fitted yet.
        try:
            from golddesk.drivers_free import build_drivers, coverage_note
            pts = build_drivers()
        except Exception as e:                        # noqa: BLE001
            return (f"{ATTRIBUTION_VERSION}: driver fetch failed "
                    f"({type(e).__name__}: {e}). UNAVAILABLE, which is not the "
                    f"same as 'nothing was driving gold'.")
        ctx["drivers_today"] = pts
        obs = sum(1 for p in pts.values() if p.observed)
        head = coverage_note(pts)
        if obs == 0:
            return (head + "\n\n  Nothing observed. UNAVAILABLE, which is not the "
                           "same as 'nothing was driving gold'.")
        return (head + f"\n\n  Today's reading only. A fitted decomposition needs "
                       f"a history of driver moves aligned to gold's; supply "
                       f"`driver_series` in the context to enable it.")
    from golddesk.attribution import report as attrib_report
    from golddesk.attribution import rolling_attribution
    y, x, keys = drivers["y"], drivers["x"], drivers["keys"]
    attrs = rolling_attribution(y, x, keys)
    ctx["attribution"] = attrs
    return attrib_report(attrs)


def step_census(ctx: dict) -> str:
    """The trial count, and what it does to every significance number.

    Runs daily and NOT only before a promotion, because the census only counts
    what has been registered, and a run registered a week after it happened is a
    run somebody had to remember.
    """
    from golddesk.linkage import LinkedRegistry, render
    reg = LinkedRegistry.load(STATE_DIR / "linkage.json")
    ctx["registry"] = reg
    ok, why = reg.audit()
    out = render(reg)
    if not ok:
        out += f"\n\n  AUDIT FAILED: {why}"
    rs = ctx.get("r_multiples") or []
    if len(rs) >= 30:
        from golddesk.deflation import census_from_registry, deflated_sharpe
        census = census_from_registry(reg)
        d = deflated_sharpe(rs, census)
        ctx["deflated"] = d
        out += "\n\n" + d.render()
    else:
        out += (f"\n\n  deflated Sharpe not computed: {len(rs)} resolved trades, "
                f"30 required. A Sharpe over fewer is a number, not evidence.")
    return out


def step_absorb(ctx: dict) -> str:
    """Process anything the quant desk queued for Aurum.

    The inbox is a file the other desk writes; nothing here reaches across to
    it, because a cycle that pulled from another repository would fail in a way
    neither desk owns.
    """
    from golddesk.absorb import Absorber, Finding
    inbox = BASE / "inbox" / "quant_findings.jsonl"

    # PULL FIRST, IF AND ONLY IF QUANT IS ACTUALLY HERE. Set AURUM_QUANT_ROOT to
    # the quant repository root and the two desks stay in step with no manual
    # step at all. Every failure below is swallowed on purpose: the whole point
    # of keeping the pull separate from the queue-and-seal is that a missing,
    # moved or half-written quant checkout must degrade to "no new findings"
    # rather than take down Aurum's nightly cycle. Absorption is a nice-to-have
    # on any given night; the cycle is not.
    qroot = os.environ.get("AURUM_QUANT_ROOT", "").strip()
    if qroot:
        # UPDATE THE CHECKOUT BEFORE SCANNING IT. Without this, "absorbs as
        # quant grows" is a lie unless a human remembers to `git pull` the
        # checkout by hand between cycles — the scan would silently keep
        # reading whatever snapshot was on disk the day it was cloned. Same
        # swallow-on-failure rule as the rest of this function: a detached
        # HEAD, a dirty tree, or no network degrades to "scan what's already
        # there", never to a broken cycle.
        if (Path(qroot) / ".git").is_dir():
            try:
                pull = subprocess.run(
                    ["git", "-C", qroot, "pull", "--ff-only"],
                    capture_output=True, text=True, timeout=120)
                if pull.returncode == 0:
                    log(f"  quant checkout updated: {pull.stdout.strip() or '(already current)'}")
                else:
                    log(f"  quant pull failed ({pull.returncode}): "
                        f"{pull.stderr.strip()[:200]}; scanning checkout as-is")
            except Exception as e:                               # noqa: BLE001
                log(f"  quant pull skipped ({type(e).__name__}: {e}); "
                    f"scanning checkout as-is")
        try:
            from golddesk.absorb_auto import to_inbox
            res = to_inbox(Path(qroot), inbox)
            log(f"  pulled {res['written']} gold-relevant finding(s) from "
                f"{qroot}, dropped {res['dropped_not_relevant']} as not gold")
        except Exception as e:                               # noqa: BLE001
            log(f"  quant pull skipped ({type(e).__name__}: {e}); "
                f"processing whatever is already in the inbox")
    else:
        log("  AURUM_QUANT_ROOT not set — inbox-only mode, nothing pulled")

    ab = Absorber.load(STATE_DIR / "absorption.json")
    ctx["absorber"] = ab
    n_new = 0
    for row in _rows(inbox):
        try:
            f = Finding(**row)
        except TypeError as e:
            log(f"  malformed finding skipped: {e}")
            continue
        before = ab.already_decided(f)
        ab.queue(f)
        if before is None:
            n_new += 1
    ab.save(STATE_DIR / "absorption.json")
    return f"{n_new} new finding(s) this cycle\n" + ab.report()


def step_channel(ctx: dict) -> str:
    """Is the desk's only product actually reaching anybody?"""
    try:
        st = json.loads((STATE_DIR / "service_state.json").read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return ("no service checkpoint — the desk has not run, so channel health "
                "is UNKNOWN rather than healthy.")
    h = st.get("notification_health") or {}
    if not h:
        return ("checkpoint carries no notification_health: this desk predates "
                "delivery tracking, so health is UNKNOWN rather than healthy.")
    if h.get("healthy") is False:
        return (f"SIGNAL CHANNEL DOWN — {h.get('consecutive_failures')} consecutive "
                f"failures, {h.get('sent', 0)} delivered ever. The desk's only "
                f"product is reaching nobody.")
    return (f"channel ok — {h.get('sent', 0)} delivered, {h.get('failed', 0)} failed, "
            f"last ok {h.get('last_ok_at')}")


def step_mining(ctx: dict) -> str:
    """Ingest any new mirrored copy-trade history and re-read the provider.

    Runs daily because the mandate is daily and because a statement exported
    weekly is a week of behaviour reconstructed from memory. Ingestion is
    idempotent on the broker's deal ticket, so pointing it at an overlapping
    export costs nothing.
    """
    from golddesk.ingest import IngestError, IngestLog, ingest_file
    inbox = BASE / "inbox" / "copytrade"
    if not inbox.exists():
        return (f"no {inbox} directory: nothing to mine. Drop MT5 statements or "
                f"CSV exports there and set the server offset in "
                f"state/ingest_offset.txt (broker time is UTC+2/+3 and the "
                f"statement does not say which).")
    off_file = STATE_DIR / "ingest_offset.txt"
    if not off_file.exists():
        return (f"{inbox} has files but {off_file} is missing. The server offset "
                f"has no safe default: parsing broker time as UTC shifts every "
                f"trade two or three hours and misaligns every session inference "
                f"while every timestamp still looks ordinary. Write the offset "
                f"(e.g. 3) to that file.")
    try:
        offset = float(off_file.read_text(encoding='utf-8').strip())
    except ValueError:
        return f"{off_file} is not a number; refusing to guess an offset."

    log_path = STATE_DIR / "copytrade_deals.json"
    ilog = IngestLog.load(log_path)
    notes, total_new = [], 0
    for f in sorted(inbox.iterdir()):
        if f.suffix.lower() not in (".html", ".htm", ".csv"):
            continue
        try:
            ilog, n, note = ingest_file(f, server_offset_hours=offset, log=ilog)
        except IngestError as e:
            notes.append(f"{f.name}: REFUSED — {e}")
            continue
        total_new += n
        notes.append(f"{f.name}: {note}")
    ilog.save(log_path)

    trades, unmatched = ilog.trades()
    ctx["copytrades"] = trades
    out = [f"{total_new} new deal(s) across {len(notes)} file(s); "
           f"{len(trades)} paired trades, {len(unmatched)} unmatched"]
    out += [f"  {n}" for n in notes[:8]]
    if not trades:
        return "\n".join(out + ["  nothing paired yet — no provider analysis."])
    from golddesk.reverse import report as reverse_report
    return "\n".join(out) + "\n\n" + reverse_report(trades)


def step_regime(ctx: dict) -> str:
    """The learned regime challenger against the incumbent rule labels.

    Only run once there is enough series to fit on. A three-state HMM on two
    hundred bars is a description of two hundred bars.
    """
    from golddesk.regime_hmm import MIN_TRAIN, contest, render
    series = ctx.get("regime_series")
    if not series:
        return (f"no return series supplied. The contest needs bars plus the "
                f"incumbent's own labels on the SAME bars; wire a bar source "
                f"into the cycle to enable it. Not run is not the same as "
                f"'the incumbent won'.")
    r, inc, fwd = series["returns"], series["incumbent"], series["forward"]
    if len(r) < MIN_TRAIN * 3:
        return (f"{len(r)} bars, {MIN_TRAIN * 3} needed before a contest means "
                f"anything. A three-state HMM on fewer is a description of the "
                f"sample, not a challenger.")
    out = contest(r, inc, fwd)
    ctx["regime_contest"] = out
    return render(out)


def step_decay(ctx: dict) -> str:
    """Is any sleeve's edge decayed, or is it having a bad month?

    Runs over EVERY sleeve including the armed book, which promoter.py
    explicitly does not manage. A sleeve carrying real capital that nobody
    monitors is the gap this closes.
    """
    from golddesk.decay import assess, book_health, detection_latency
    rows = ctx.get("rows") or []
    by_mech: dict = {}
    for r in rows:
        m, v = r.get("mechanism"), r.get("realised_r")
        if m and isinstance(v, (int, float)):
            by_mech.setdefault(m, []).append(float(v))
    if not by_mech:
        return ("no resolved trades per mechanism, so nothing can be assessed for "
                "decay. UNMONITORED is not the same as healthy.")
    baselines = ctx.get("baselines") or {}
    states = []
    for m, rs in sorted(by_mech.items()):
        base = baselines.get(m)
        if base is None:
            # A sleeve with no recorded warrant cannot be judged against one.
            # Its own mean would ask "did it change", which every series does.
            base = sum(rs) / len(rs)
        states.append(assess(m, rs, baseline_exp_r=base))
    ctx["decay_states"] = states
    h = book_health(states, ready_replacements=ctx.get("ready_replacements", 0))
    lat = {s.sleeve: detection_latency(s.baseline_exp_r) for s in states}
    tail = "\n".join(
        f"    {k:<24}{'n/a' if v is None else format(v, '.0f') + ' trades to prove a halving'}"
        for k, v in sorted(lat.items()))
    return h.render() + "\n\n  DETECTION LATENCY\n" + tail + (
        "\n    A thin edge cannot be protected by monitoring: by the time decay "
        "is provable it has been paid for.")


def step_missed_money(ctx: dict) -> str:
    """What the refusals actually cost -- counting only money that was GETTABLE.

    A refusal's forward path says what price did next; it does NOT say the desk could have
    participated. missed_money.py exists precisely to separate those, because conflating them
    inflates every "this gate cost you R" number in one direction -- upward -- which is the
    direction that argues for removing gates. It was written, tested, and run by nothing: its
    `__main__` block defaults to `backtest_out/ledger-A-test*.jsonl`, so the LIVE ledger was
    never its subject. Wired here because a desk that grades only what it took cannot see its
    false negatives, and refusals are the majority of what this analyst produces.
    """
    import missed_money

    if not LEDGER.exists():
        return "no ledger yet -- UNMEASURED, not zero missed money."
    rows = missed_money.load([LEDGER])
    if not rows:
        return ("ledger present but no rows this run -- UNMEASURED. A refusal that has not "
                "resolved forward yet is pending, not free.")
    return missed_money.report(rows)


def step_mgmt_counterfactual(ctx: dict) -> str:
    """What the OTHER management policies would have produced on the identical path.

    Management is roughly half of realised R, and this desk runs `heuristic` while shadowing
    the alternatives -- but shadowing only records what each policy would have CHOSEN. What
    those choices would have PRODUCED needs the excursion path, which the ledger persists, and
    that half was computed by a script nothing scheduled. No market re-simulation is involved:
    the path is what happened, and only the desk's response to it changes.
    """
    import mgmt_counterfactual

    if not LEDGER.exists():
        return "no ledger yet -- UNMEASURED, not a verdict that heuristic is best."
    rows = mgmt_counterfactual.load([LEDGER])
    if not rows:
        return ("no management traces carrying a persisted path yet -- UNMEASURED. The arms "
                "cannot be compared until closed positions carry their excursions.")
    return mgmt_counterfactual.report(rows)


def step_levers(ctx: dict) -> str:
    """Where does the next unit of effort buy the most growth?"""
    from golddesk.levers import analyse
    states = ctx.get("decay_states") or []
    rs = ctx.get("r_multiples") or []
    n = len(states)
    if n < 2 or len(rs) < 30:
        return (f"{n} sleeve(s) and {len(rs)} resolved trades. The lever ranking "
                f"needs at least two sleeves and thirty trades before it "
                f"describes this book rather than the prior.")
    mu = sum(rs) / len(rs)
    rho = ctx.get("rho", 0.165)
    tpy = ctx.get("trades_per_year") or len(rs)
    return analyse(n_sleeves=n, mu=mu, n_per_year=tpy, rho=rho).render()


def step_entries(ctx: dict) -> str:
    """What are the mined provider's entries NEAR, against a matched null?"""
    from golddesk.entry_classifier import classify, report as cls_report
    trades = ctx.get("copytrades") or []
    bars = ctx.get("m5_bars") or []
    if not trades:
        return "no mined copy-trades this cycle; nothing to classify."
    if not bars:
        return (f"{len(trades)} mined trades but no M5 bar series in context. "
                f"The classifier aligns entries to bars and cannot run without "
                f"them — supply `m5_bars` to enable it. Not run is not the same "
                f"as 'his entries cluster on nothing'.")
    hits = classify([t.open_utc for t in trades], bars)
    ctx["entry_hits"] = hits
    return cls_report(hits)


def step_shadow(ctx: dict) -> str:
    """What each shadow cell earned yesterday, from bars the desk already has.

    Runs BEFORE intake so the returns are in ctx when the pipeline reads them.
    Needs a bar loader and a cost function in ctx; without them it says so
    rather than posting an empty day, because "no data" and "no fills" advance
    the shadow clock differently and only one of them is evidence.
    """
    from datetime import date, timedelta
    from golddesk.promotion import Status, load as load_book
    from golddesk.shadow_eval import evaluate, render
    from pathlib import Path
    loader = ctx.get("bar_loader")
    fams = ctx.get("families_module")
    windows = ctx.get("session_windows") or {}
    costs = ctx.get("costs_for")
    runner = ctx.get("run_backtest")
    if not all((loader, fams, costs, runner)):
        return ("shadow evaluation SKIPPED: needs bar_loader, families_module, "
                "costs_for and run_backtest in context. Not run is not the same "
                "as no fills — an absent evaluator must not advance any shadow "
                "clock, so nothing is posted.")
    book = load_book(Path(ctx.get("pipeline_path", "state/pipeline.json")))
    cells = [c.cell for c in book
             if c.status in (Status.SHADOW, Status.LIVE)]
    if not cells:
        return "no cell is shadowing or live yet; nothing to evaluate."
    day = ctx.get("as_of") or (date.today() - timedelta(days=1))
    if isinstance(day, str):
        day = date.fromisoformat(day)
    r, t, notes = evaluate(cells, day, loader, fams, windows, costs, runner)
    ctx["forward_returns"], ctx["forward_trades"] = r, t
    return render(r, t, notes, day)


def step_intake(ctx: dict) -> str:
    """Hunt output -> shadow book, with nobody asked.

    Runs every morning against a candidate file that mostly does not change.
    The idempotence is load-bearing: re-registering a known cell would reset its
    shadow clock daily and nothing would ever accrue enough forward days to be
    promoted — a pipeline that looks busy and can never finish.
    """
    from golddesk.intake import budget_note, run as intake_run
    from pathlib import Path
    # FORWARD EVIDENCE, PRODUCED RATHER THAN ASSUMED. ctx["forward_returns"] was
    # read here and written by nothing, so every candidate sat at zero fills
    # for ever — a pipeline that reports cleanly every morning and can never
    # promote. step_shadow fills it now; an empty dict still means "no fills
    # today", which correctly advances no clock.
    book, text = intake_run(
        book_path=Path(ctx.get("pipeline_path", "state/pipeline.json")),
        returns=ctx.get("forward_returns") or {},
        trades=ctx.get("forward_trades") or {},
        day=ctx.get("as_of"))
    ctx["pipeline"] = book
    rs = ctx.get("r_multiples") or []
    if len(rs) >= 30:
        text += "\n\n" + budget_note(book, rs,
                                     tolerance=ctx.get("tolerance", 0.35))
    return text


STEPS = (
    ("evidence", step_evidence),
    ("shadow", step_shadow),
    ("intake", step_intake),
    ("channel", step_channel),
    ("growth", step_growth),
    ("attribution", step_attribution),
    ("regime", step_regime),
    ("census", step_census),
    ("decay", step_decay),
    # AFTER decay, BEFORE levers, and the position is load-bearing. `levers` asks where the
    # next unit of effort buys the most growth, and it cannot rank honestly while the cost of
    # the desk's REFUSALS and the value of its management arms are both invisible -- the two
    # numbers most likely to move that ranking. Both scripts existed and were run by nothing.
    ("missed_money", step_missed_money),
    ("mgmt_counterfactual", step_mgmt_counterfactual),
    ("levers", step_levers),
    ("mining", step_mining),
    ("entries", step_entries),
    ("absorb", step_absorb),
)


# ----------------------------------------------------------------- the runner

def run_step(name: str, fn, ctx: dict) -> tuple:
    """One step. Never raises — a failing step must not cost the other five."""
    try:
        return True, fn(ctx)
    except Exception as e:                            # noqa: BLE001
        log(f"{name}: FAILED -- {type(e).__name__}: {e}")
        log(traceback.format_exc())
        return False, f"FAILED: {type(e).__name__}: {e}"


def run(force: bool = False, dry: bool = False) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {}
    if CYCLE_STATE.exists():
        try:
            state = json.loads(CYCLE_STATE.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            state = {}
    if state.get("last_run") == today and not force:
        log(f"cycle already ran {today}; --force to repeat")
        return 0

    log(f"daily cycle {today} starting ({CYCLE_VERSION})")
    ctx: dict = {}
    sections, failed = [], []
    for name, fn in STEPS:
        ok, text = run_step(name, fn, ctx)
        if not ok:
            failed.append(name)
        sections.append((name, text))
        log(f"{name}: {'ok' if ok else 'FAILED'}")

    body = "\n\n".join(f"== {n.upper()} ==\n{t}" for n, t in sections)
    head = (f"AURUM DAILY CYCLE {today}  ({CYCLE_VERSION})\n"
            + (f"FAILED STEPS: {', '.join(failed)}\n" if failed else ""))
    report = head + "\n" + body

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"cycle-{today}.md").write_text(report, encoding="utf-8")

    if not dry:
        try:
            from golddesk.notify import build_sink
            sink = build_sink(BASE / "secrets")
            # Telegram caps a message; the file holds the whole thing and the
            # message carries the headline plus where to read the rest.
            summary = head + "\n" + "\n".join(
                f"{n}: {t.splitlines()[0] if t else ''}" for n, t in sections)
            sink.send(summary[:3500])
        except Exception as e:                        # noqa: BLE001
            log(f"report send failed (non-fatal): {type(e).__name__}: {e}")

    # THE ATTEMPT IS STAMPED, NOT THE SUCCESS. Stamping only on a clean run
    # means one failing step re-runs every step next invocation — re-sending
    # notifications and re-queueing findings.
    #
    # A DRY RUN IS A REHEARSAL AND MUST NOT CONSUME THE DAY. It stamped, so the
    # documented sequence — `--dry` to read the output, then the real run —
    # blocked itself on the second command with "cycle already ran". The
    # operator had to discover that and reach for --force, which defeats the
    # point of offering a rehearsal at all.
    if dry:
        log("dry run: nothing sent, day NOT stamped -- the real run is still "
            "available without --force")
    else:
        state["last_run"] = today
        state["last_failed_steps"] = failed
        state["version"] = CYCLE_VERSION
        CYCLE_STATE.write_text(json.dumps(state, indent=2))

    log(f"daily cycle {today} done"
        + (f" -- FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Aurum daily cycle")
    ap.add_argument("--force", action="store_true", help="run again today")
    ap.add_argument("--dry", action="store_true", help="run, send nothing")
    a = ap.parse_args()
    return run(force=a.force, dry=a.dry)


if __name__ == "__main__":
    raise SystemExit(main())
