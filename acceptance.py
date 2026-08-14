"""END-TO-END SHADOW LIFECYCLE ACCEPTANCE TEST.

Proves each arrow of the production chain executes inside the REAL LiveDesk,
not in a module demonstration:

  feed(MT5 adapter) -> bar path -> watcher -> analyst(real AnthropicAnalyst
  code path) -> compiler -> router -> risk -> Telegram ENTRY -> TradeObserver
  driven by ticks -> economic wake -> contextual management via provider
  choose_option -> stop/partial/runner -> Telegram update -> intrabar-resolved
  EXIT -> conditional re-entry -> ledger -> adaptation candidate

WHAT IS REAL AND WHAT IS NOT — read this before believing any number below.

  REAL: every line of golddesk executed here is the shipped code. The MT5
        adapter contract (feed.Mt5Client), AnthropicAnalyst.read() including
        base64 image encoding, cache_control, output_config and schema
        validation, the compiler, router, risk gate, observer, management
        engine, ledger and adaptation cycle.

  REAL: the D1 bars, which are broker XAUUSD history.

  NOT REAL: the HTTP boundary. There is no ANTHROPIC_API_KEY in this
        container, so `client.messages.create` is replaced by a capturing
        double that returns a schema-valid response. Everything up to and
        including request construction is exercised; the model's judgment is
        not. NOTHING here is evidence about Claude's trading ability.

  NOT REAL: the intrabar tick path. No M1 or tick history could be fetched
        (every market-data host returns 403 CONNECT). The tick sequence used
        to drive the observer is synthetic and exists solely to prove the wire
        is connected. It is not market data and produces no evidence.

Run: python3 acceptance.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.adapt import Adapter
from golddesk.analyst import Thresholds
from golddesk.features import Bar, atr, classify, swings
from golddesk.hypothesis import HypothesisBook
from golddesk.ledger import Ledger
from golddesk.live import LiveDesk, Resolution, Vision
from golddesk.notify import Sink
from golddesk.observer import resolve_intrabar
from golddesk.policies import ContextualChooser, HeuristicChooser, PassiveChooser
from golddesk.policy_state import PolicyState
from golddesk.providers import AnthropicAnalyst
from golddesk.runner import ParquetBarSource

PARQUET = "/root/.claude/uploads/353d9479-657d-5787-9c73-4a674604017c/c3041b3a-XAUUSD_D1.parquet"
OUT = Path("acceptance_out")

TRACE: list[str] = []


def arrow(name: str, detail: str) -> None:
    TRACE.append(f"{name}|{detail}")
    print(f"  [{len(TRACE):>2}] {name:<34} {detail}")


# ---------------------------------------------------------------------------
# The HTTP boundary double. Everything ABOVE it is the shipped code path.
# ---------------------------------------------------------------------------

class _Usage:
    input_tokens = 1234
    cache_read_input_tokens = 900
    output_tokens = 210


class _Block:
    def __init__(self, text): self.type, self.text = "text", text


class _Resp:
    def __init__(self, text):
        self.stop_reason, self.content, self.usage = "end_turn", [_Block(text)], _Usage()


class CapturingMessages:
    """Stands in for anthropic.Anthropic().messages — records what was sent."""

    def __init__(self, outer): self.outer = outer

    def create(self, **kw):
        self.outer.calls.append(kw)
        content = kw["messages"][0]["content"]
        fmt = kw["output_config"]["format"]["schema"]
        if "option_id" in fmt.get("properties", {}):
            # Management choice. The double reads the rendered option list and
            # prefers capture-preserving actions over EXIT, so the trace walks
            # the whole lifecycle instead of terminating on the first wake.
            # This is a scripted preference, NOT a model judgement.
            ids = fmt["properties"]["option_id"]["enum"]
            self.outer.last_options = ids
            body = kw["messages"][0]["content"]
            order = ("PARTIAL", "PROTECT", "TRAIL", "HOLD", "EXIT")
            pick = ids[0]
            for want in order:
                hit = [ln for ln in body.splitlines() if want in ln
                       and any(i in ln for i in ids)]
                if hit:
                    pick = next(i for i in ids if i in hit[0])
                    break
            return _Resp(json.dumps({"option_id": pick, "because": "acceptance double"}))
        n_img = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
        self.outer.images_seen.append(n_img)
        return _Resp(json.dumps({
            "setup": "SWING_REVERSAL", "direction": self.outer.direction,
            "entry_ref": self.outer.entry_ref, "stop_ref": self.outer.stop_ref,
            "tp1_ref": self.outer.tp1_ref, "tp2_ref": self.outer.tp2_ref,
            "mechanism_name": "acceptance-swing-reclaim", "confidence": 4,
            "read": "Acceptance double — not a model judgement.",
            "why": "Wiring proof only.", "why_not": "This is not a real read.",
            "invalidation": "n/a"}))


class CapturingClient:
    def __init__(self):
        self.calls, self.images_seen, self.last_options = [], [], []
        self.direction = "LONG"
        self.entry_ref = self.stop_ref = self.tp1_ref = self.tp2_ref = "NONE"
        self.messages = CapturingMessages(self)


class RecordingSink(Sink):
    def __init__(self): self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    print(__doc__.split("Run:")[0])
    print("=" * 78)
    print("EXECUTABLE TRACE")
    print("=" * 78)

    # -- 1. feed: the real MT5 adapter contract ---------------------------
    src = ParquetBarSource(PARQUET, timeframe="D1")
    bars = src.bars()
    arrow("feed.ParquetBarSource",
          f"{len(bars)} D1 bars {bars[0].ts:%Y-%m-%d}..{bars[-1].ts:%Y-%m-%d} (REAL broker data)")

    sw = swings(bars)
    atrs = atr(bars)
    arrow("features.swings/atr", f"{len(sw)} swings, ATR[-1]={atrs[-1]:.2f}")

    # -- 2. the desk, with all three management arms registered -----------
    client = CapturingClient()
    provider = AnthropicAnalyst(model="claude-opus-5", client=client)
    ledger = Ledger(OUT / "ledger.jsonl")
    sink = RecordingSink()
    book = HypothesisBook(OUT / "hypotheses.json")
    pstate = PolicyState(OUT / "policy_state.json",
                         defaults={"management_chooser": HeuristicChooser.name,
                                   "reentry_policy": "reentry-2026-08-14-a"})
    desk = LiveDesk(provider, ledger, sink, shadow=True,
                    vision=Vision.NUMERIC_PLUS_CHARTS,
                    policy_state=pstate, book=book,
                    shadow_management=True, shadow_contextual=True,
                    thresholds=Thresholds(fallback_min_rr=1.2))
    arrow("LiveDesk.__init__",
          f"vision={desk.vision.value} arms={sorted(desk.choosers)} "
          f"mgmt={pstate.active('management_chooser')}")
    assert "contextual-v1" in desk.choosers, "contextual arm not registered"

    # Put the contextual arm live for this trace so the model-in-the-loop path
    # is the one exercised. This is a WIRING SELECTION, explicitly not an
    # evidence-based promotion — the warrant says so, and adaptation would
    # overwrite it the moment a paired comparison said otherwise.
    pstate.bind("management_chooser", ContextualChooser.name,
                "ACCEPTANCE WIRING ONLY — no evidence, not a promotion",
                {"acceptance": True}, ttl_days=1)
    arrow("policy bound for trace",
          f"management_chooser -> {pstate.active('management_chooser')} "
          f"(warrant explicitly marks it as non-evidential)")

    # -- 3. drive bars until an entry occurs ------------------------------
    # The double must name levels that exist in the brief it is about to be
    # shown, exactly as a real analyst would — it cites ids, never prices.
    from golddesk.runner import build_brief
    i = 0
    for i in range(260, len(bars) - 70):
        st = classify(bars, i, sw, atrs)
        if st is None:
            continue
        b = bars[i]
        peek = build_brief(bars, i, st, sw, b.close - 0.21, b.close + 0.21, 1.0,
                           None, ("D1 acceptance",), timeframe="D1")
        if not peek.levels:
            continue
        lo = min(peek.levels, key=lambda l: l.price)
        hi = max(peek.levels, key=lambda l: l.price)
        client.direction = "LONG"
        client.entry_ref, client.stop_ref = "MARKET", lo.id
        client.tp1_ref = client.tp2_ref = hi.id
        desk.on_bar(bars, i, sw, atrs, None,
                    (b.close - 0.21, b.close + 0.21, 1.0), ("D1 acceptance",))
        if desk.open is not None:
            break

    if desk.open is None:
        print("\n  NO ENTRY PRODUCED — the double's refs never compiled. "
              "Trace incomplete.")
        return 1

    t = desk.open
    arrow("watcher->analyst->compiler",
          f"reads={desk.stats.reads} states={desk.stats.states} at bar {i}")
    arrow("AnthropicAnalyst.read (REAL code)",
          f"request built: system+cache_control={'cache_control' in client.calls[0]['system'][0]}, "
          f"schema={'json_schema' == client.calls[0]['output_config']['format']['type']}, "
          f"images={client.images_seen[-1]}")
    assert client.images_seen[-1] >= 1, "multimodal declared but no image sent"
    arrow("MULTIMODAL VERIFIED",
          f"vision={desk.vision.value} and {client.images_seen[-1]} image block(s) "
          f"actually in the request payload")
    arrow("risk_check->ENTRY",
          f"{t.signal.direction} entry={t.signal.entry:.2f} stop={t.signal.stop:.2f} "
          f"tp2={t.signal.tp2:.2f} rr={t.signal.rr_tp2:.2f}")
    arrow("Telegram ENTRY",
          f"{len([s for s in sink.sent if 'ENTRY' in s])} notification(s), "
          f"shadow-tagged={sink.sent[-1].startswith('[SHADOW]')}")
    arrow("TradeObserver instantiated",
          f"entry={t.observer.entry:.2f} stop={t.observer.stop:.2f} "
          f"target={t.observer.target:.2f} risk={t.observer.risk_price:.2f}")

    # -- 4. tick path: SYNTHETIC path, real observer + real management ----
    pos = t.position
    risk = pos.risk_price
    base = pos.entry
    ts = pos.opened_utc
    # a favourable excursion, then a retracement — enough to make the observer
    # fire MFE_EXTENSION and GIVEBACK, which is what wakes management
    path = ([base + risk * k * 0.15 for k in range(1, 21)]      # run to ~+3R
            + [base + risk * (3.0 - k * 0.12) for k in range(1, 15)])
    wakes, actions = 0, []
    for n, px in enumerate(path):
        ts = ts + timedelta(minutes=1)
        r = desk.on_tick(px, ts, bar_closed=(n % 15 == 14))
        if r:
            actions.append(r)
            if r.startswith("WAKE"):
                wakes += 1
        if desk.open is None:
            break
    arrow("on_tick -> observer",
          f"{desk.stats.ticks} ticks consumed, MFE={t.observer.mfe_r:+.2f}R "
          f"path_points={len(t.observer.path)}")
    assert desk.stats.ticks > 0, "observer never received a tick"
    arrow("economic wake fired",
          f"{desk.stats.observer_wakes} wake(s), "
          f"triggers={[a for a in actions if a.startswith('WAKE')][:2]}")
    assert desk.stats.observer_wakes > 0, "observer produced no economic wake"
    arrow("management reconsideration",
          f"{desk.stats.mgmt_reconsiderations} step(s), "
          f"stop_moves={desk.stats.stop_moves} partials={desk.stats.partials}")

    mgmt = t.mgmt_log if desk.open else []
    if not mgmt and desk.open is None:
        mgmt = json.loads((OUT / "ledger.jsonl").read_text().splitlines()[-1]).get("management", [])
    tickborne = [m for m in mgmt if m["source"].startswith("observer")]
    arrow("wake -> mgmt (causal link)",
          f"{len(tickborne)}/{len(mgmt)} management steps originated from an "
          f"observer wake, not a bar close")
    assert tickborne, "management never ran off a tick wake"
    withshadow = [m for m in mgmt if m["shadow"]]
    arrow("paired shadow arms",
          f"{len(withshadow)} step(s) recorded alternatives on the identical "
          f"option set: e.g. {withshadow[0]['shadow'] if withshadow else '-'}")
    contextual = [m for m in mgmt if m["active_policy"] == "contextual-v1"]
    arrow("ContextualChooser executed",
          f"{len(contextual)} step(s) decided by the model-in-the-loop arm; "
          f"provider.choose_option constrained to {len(client.last_options)} "
          f"legal ids, no price field exists in its schema")
    assert contextual, "contextual arm never decided a step"
    assert client.last_options, "choose_option never called"
    arrow("legality guaranteed",
          f"every chosen id came from the enumerated set: "
          f"{all(m['chosen'] in m['options'] for m in mgmt if m['chosen'])}")

    # -- 5. exit ----------------------------------------------------------
    if desk.open is not None:
        for n in range(40):
            ts = ts + timedelta(minutes=1)
            if desk.on_tick(pos.current_stop - 0.01 if pos.long
                            else pos.current_stop + 0.01, ts):
                break
    arrow("intrabar-resolved EXIT",
          f"exits={desk.stats.exits} tick_resolved={desk.stats.exits_tick_resolved} "
          f"m1={desk.stats.exits_m1_resolved} assumed={desk.stats.exits_assumed}")
    assert desk.stats.exits_tick_resolved > 0, "no exit resolved at tick ordering"
    arrow("Telegram EXIT",
          f"{len([s for s in sink.sent if 'EXIT' in s])} exit notification(s)")

    # -- 6. re-entry ------------------------------------------------------
    arrow("prior trade recorded",
          f"dir={desk.prior.direction} exit={desk.prior.exit_reason} "
          f"realised={desk.prior.realised_r:+.2f}R mfe={desk.prior.mfe_r:+.2f}R")
    rp = desk.active_reentry()
    st_now = desk._last_state
    v = rp.evaluate(desk.prior, st_now, ts)
    arrow("ReentryPolicy consulted",
          f"{rp.version} -> allowed={v.allowed}: {v.reason[:60]}")

    # -- 7. ledger --------------------------------------------------------
    rows = ledger.read_all()
    closed = [r for r in rows if r.get("kind") == "TRADE_CLOSED"]
    arrow("ledger",
          f"{len(rows)} rows, {len(closed)} closed trade(s) with full management trace")
    assert closed and closed[0]["management"], "close row carries no management trace"
    arrow("ledger records resolution",
          f"resolution={closed[0]['resolution']} forgone_r={closed[0]['forgone_r']:+.2f} "
          f"observations={closed[0]['observations']}")

    # -- 8. adaptation ----------------------------------------------------
    from golddesk.opportunity import build_cohorts, resolved_outcomes
    outs = resolved_outcomes(rows)
    coh = build_cohorts(rows)
    arrow("learning loop sees outcomes",
          f"resolved_outcomes={len(outs)} cohorts={len(coh)} "
          f"(was structurally 0 before this revision)")
    assert outs and coh, "learning loop still reading an empty set"

    adapter = Adapter(OUT / "adapt.jsonl", policy_state=pstate, book=book)
    sealed = adapter.discover(rows, [{"setup": "SWING_REVERSAL"}], min_n=1)
    arrow("hypothesis sealed (zero authority)",
          f"{len(sealed)} sealed; enforcing now = {len(book.enforcing())}")
    # A synthetic paired result set, to prove the BINDING path executes.
    paired = {"management_chooser": {
        "heuristic-v1": [0.10] * 60,
        "passive": [0.35] * 60}}
    rep = adapter.run(rows, policy_results=paired)
    arrow("adaptation cycle",
          f"changes={len(rep.changes)} refused={len(rep.refused)} "
          f"hyp_moves={len(rep.hypothesis_moves)}")
    now_active = pstate.active("management_chooser")
    arrow("POLICY ACTUALLY BOUND",
          f"management_chooser: heuristic-v1 -> {now_active} "
          f"(was an audit-only no-op before this revision)")
    assert now_active == "passive", "adaptation did not change the active policy"

    reloaded = PolicyState(OUT / "policy_state.json",
                           defaults={"management_chooser": HeuristicChooser.name})
    arrow("DURABLE ACROSS RESTART",
          f"fresh PolicyState from disk reports {reloaded.active('management_chooser')!r}")
    assert reloaded.active("management_chooser") == "passive"

    undone = adapter.revert_last()
    arrow("REVERSIBLE",
          f"{len(undone)} change(s) undone; active now "
          f"{pstate.active('management_chooser')!r}")

    b0 = pstate.binding("management_chooser")
    arrow("DECAY AWARE",
          f"warrant carries expiry; expiring within 365d = "
          f"{len(pstate.expiring_within(365))}")

    print("=" * 78)
    print(f"ALL {len(TRACE)} ARROWS EXECUTED")
    (OUT / "trace.txt").write_text("\n".join(TRACE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
