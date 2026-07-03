"""Tests for gm.delivery.leadcard (Phase D1 weekly WhatsApp trend card).

Pure card assembly (week math, headline branches, the delta cascade,
clipping) runs everywhere; DB-backed tests (add_lead, week_stats,
build_card_text, the send_lead_card handler) skip cleanly when DATABASE_URL
is unset. ZERO network anywhere — WhatsApp sends go through a fake client.
"""

import datetime as dt
import os
from types import SimpleNamespace

import pytest

from gm.delivery import leadcard

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

MON = dt.date(2026, 6, 22)  # a Monday
PREV_MON = MON - dt.timedelta(days=7)


def _stats(booked=0, prev=0, by_source=None, trend=None):
    if trend is None:
        trend = "up" if booked > prev else ("down" if booked < prev else "flat")
    return {
        "booked": booked,
        "prev_week": prev,
        "by_source": by_source or {},
        "trend": trend,
        "week_start": MON,
    }


class _StubConn:
    """Scripted conn: each execute() pops the next result. A list result
    feeds fetchall(); a dict/None result feeds fetchone()."""

    def __init__(self, results):
        self._results = list(results)

    def execute(self, *_args, **_kwargs):
        r = self._results.pop(0)
        return SimpleNamespace(
            fetchone=lambda: (r[0] if r else None) if isinstance(r, list) else r,
            fetchall=lambda: r if isinstance(r, list) else ([r] if r else []),
        )


class FakeWaba:
    def __init__(self):
        self.sent = []

    def send_text(self, to, body):
        self.sent.append((to, body))
        return {"messages": [{"id": "wamid.fake"}]}


def _no_module(name):
    raise ImportError(f"stubbed away: {name}")


# --- week math (pure) ------------------------------------------------------------------


def test_week_start_for_monday_snap():
    assert leadcard.week_start_for(MON) == MON  # Monday stays
    assert leadcard.week_start_for(MON + dt.timedelta(days=2)) == MON  # Wednesday
    assert leadcard.week_start_for(MON + dt.timedelta(days=6)) == MON  # Sunday
    assert leadcard.week_start_for(MON + dt.timedelta(days=7)) == MON + dt.timedelta(days=7)


def test_clip():
    assert leadcard._clip("short", 10) == "short"
    clipped = leadcard._clip("x" * 300, 250)
    assert len(clipped) == 250
    assert clipped.endswith("…")


# --- headline branches (pure, every empty state) ----------------------------------------


def test_headline_up():
    assert leadcard._headline(_stats(7, 4)) == "Booked consults this week: 7 (▲ from 4)"


def test_headline_down():
    assert leadcard._headline(_stats(3, 9)) == "Booked consults this week: 3 (▼ from 9)"


def test_headline_flat():
    assert leadcard._headline(_stats(4, 4)) == "Booked consults this week: 4 (= 4 last week)"


def test_headline_up_from_zero():
    assert leadcard._headline(_stats(2, 0)) == "Booked consults this week: 2 (▲ from 0)"


def test_headline_zero_this_week_honest():
    assert (
        leadcard._headline(_stats(0, 5))
        == "No booked consults logged this week (▼ from 5)."
    )


def test_headline_zero_both_weeks_honest():
    assert (
        leadcard._headline(_stats(0, 0))
        == "No booked consults logged this week (none the week before either)."
    )


# --- delta cascade: rank tracker (concurrent module, stubbed) ---------------------------


def test_rank_delta_absent_module(monkeypatch):
    monkeypatch.setattr(leadcard, "_import_module", _no_module)
    assert leadcard._rank_delta_line(None, "s", since=MON, until=MON) is None


def test_rank_delta_tracker_error_degrades(monkeypatch):
    def boom(conn, site_id, *, since, until):
        raise RuntimeError("half-built")

    monkeypatch.setattr(
        leadcard, "_import_module",
        lambda name: SimpleNamespace(rank_movement=boom),
    )
    assert leadcard._rank_delta_line(None, "s", since=MON, until=MON) is None


def test_rank_delta_picks_best_improvement(monkeypatch):
    moves = [
        {"query": "small move", "first_rank": 5, "last_rank": 4},
        {"query": "best clinic dubai", "first_rank": 9, "last_rank": 4},
        {"query": "worse", "first_rank": 3, "last_rank": 8},
        "garbage-row",
    ]
    monkeypatch.setattr(
        leadcard, "_import_module",
        lambda name: SimpleNamespace(
            rank_movement=lambda conn, site_id, *, since, until: moves
        ),
    )
    line = leadcard._rank_delta_line(None, "s", since=MON, until=MON)
    assert line == "Google: 'best clinic dubai' moved #9 → #4 this week."


def test_rank_delta_newly_ranked(monkeypatch):
    moves = [{"query_norm": "new query", "first_rank": None, "last_rank": 8}]
    monkeypatch.setattr(
        leadcard, "_import_module",
        lambda name: SimpleNamespace(
            rank_movement=lambda conn, site_id, *, since, until: moves
        ),
    )
    line = leadcard._rank_delta_line(None, "s", since=MON, until=MON)
    assert line == "Google: 'new query' entered the results at #8 this week."


def test_rank_delta_no_improvement_returns_none(monkeypatch):
    moves = [{"query": "q", "first_rank": 4, "last_rank": 4}]
    monkeypatch.setattr(
        leadcard, "_import_module",
        lambda name: SimpleNamespace(
            rank_movement=lambda conn, site_id, *, since, until: moves
        ),
    )
    assert leadcard._rank_delta_line(None, "s", since=MON, until=MON) is None


# --- delta cascade: resolved finding + receipt fallbacks (stub conn) ---------------------


def test_resolved_finding_line_picks_most_recent(monkeypatch):
    monkeypatch.setattr(leadcard, "_CHECK_NAMES", {})  # registry names pinned off
    conn = _StubConn([
        [
            {"findings_diff": {"skipped": True, "note": "no audit pair"}},
            {"findings_diff": {"resolved": ["A1", "B2"], "regressed": []}},
        ]
    ])
    line = leadcard._resolved_finding_line(conn, "s")
    assert line is not None
    assert "A1" in line
    assert "resolved in the latest audit" in line
    assert "(+1 more)" in line


def test_resolved_finding_line_none_when_nothing_resolved():
    conn = _StubConn([[{"findings_diff": {"resolved": [], "regressed": ["C3"]}}]])
    assert leadcard._resolved_finding_line(conn, "s") is None
    assert leadcard._resolved_finding_line(_StubConn([[]]), "s") is None


def test_receipt_delta_line_up_down_steady():
    def payload(change, first=60, last=72):
        return {"payload": {
            "period": "2026-06",
            "audits": {"movement": {
                "first": {"score": first}, "last": {"score": last}, "change": change,
            }},
        }}

    assert (
        leadcard._receipt_delta_line(_StubConn([payload(12.0)]), "s")
        == "Site score up 12 (60 → 72) in 2026-06."
    )
    assert (
        leadcard._receipt_delta_line(_StubConn([payload(-5.0, 70, 65)]), "s")
        == "Site score down 5 (70 → 65) in 2026-06."
    )
    assert (
        leadcard._receipt_delta_line(_StubConn([payload(0.0, 70, 70)]), "s")
        == "Site score held steady (70 → 70) through 2026-06."
    )


def test_receipt_delta_line_honest_absences():
    # no site_deltas row at all
    assert leadcard._receipt_delta_line(_StubConn([None]), "s") is None
    # a row whose movement has no change value -> no invented number
    row = {"payload": {"audits": {"movement": {"first": None, "last": None, "change": None}}}}
    assert leadcard._receipt_delta_line(_StubConn([row]), "s") is None


def test_delta_line_final_honest_fallback(monkeypatch):
    monkeypatch.setattr(leadcard, "_import_module", _no_module)
    conn = _StubConn([[], None])  # no content_deltas, no site_deltas
    line = leadcard._delta_line(conn, "s", since=MON, until=MON)
    assert line == "No movement data yet — the next audit or rank check will fill this in."


# --- next action (stub conn) --------------------------------------------------------------


def test_next_action_empty_queue():
    assert (
        leadcard._next_action_line(_StubConn([[]]), "s")
        == "Next: queue is clear — no open opportunities right now."
    )


def test_next_action_prefers_highest_est_gain():
    rows = [
        {"kind": "decay", "target": {"page": "https://ex.com/a"},
         "at_stake": {"est_clicks_gain": 40}},
        {"kind": "striking_distance", "target": {"query": "best clinic dubai"},
         "at_stake": {"est_clicks_gain": 120}},
        {"kind": "ctr_outlier", "target": {"page": "https://ex.com/b"},
         "at_stake": {"est_clicks_gain": "not-a-number"}},
    ]
    line = leadcard._next_action_line(_StubConn([rows]), "s")
    assert line == (
        "Next: Push a striking-distance query — 'best clinic dubai'"
        " (~120 clicks/mo at stake)"
    )


def test_next_action_without_gain_stays_honest():
    rows = [{"kind": "decay", "target": {"page": "https://ex.com/a"}, "at_stake": {}}]
    line = leadcard._next_action_line(_StubConn([rows]), "s")
    assert line == "Next: Rescue a decaying page — 'https://ex.com/a'"
    assert "clicks" not in line  # no invented estimate


# --- full card assembly (stub conn, pure golden) --------------------------------------------


def test_build_card_golden_four_lines(monkeypatch):
    monkeypatch.setattr(leadcard, "_import_module", _no_module)
    monkeypatch.setattr(leadcard, "_CHECK_NAMES", {})  # registry names pinned off
    conn = _StubConn([
        [{"source": "whatsapp", "cur": 5, "prev": 4}, {"source": "manual", "cur": 2, "prev": 0}],
        [{"findings_diff": {"resolved": ["A1"]}}],
        [{"kind": "striking_distance", "target": {"query": "laser hair removal dubai"},
          "at_stake": {"est_clicks_gain": 120}}],
    ])
    card = leadcard.build_card_text(conn, "s", week_start=MON)
    lines = card.split("\n")
    assert len(lines) == 4
    assert lines[0] == "Booked consults this week: 7 (▲ from 4)"
    assert lines[1].startswith("Fixed: A1")
    assert lines[2] == (
        "Next: Push a striking-distance query — 'laser hair removal dubai'"
        " (~120 clicks/mo at stake)"
    )
    assert lines[3] == leadcard.FOOTER
    assert len(card) <= leadcard.MAX_CARD_CHARS


def test_build_card_empty_states_and_cap(monkeypatch):
    monkeypatch.setattr(leadcard, "_import_module", _no_module)
    conn = _StubConn([[], [], None, []])
    card = leadcard.build_card_text(conn, "s", week_start=MON)
    lines = card.split("\n")
    assert lines[0] == "No booked consults logged this week (none the week before either)."
    assert lines[1] == "No movement data yet — the next audit or rank check will fill this in."
    assert lines[2] == "Next: queue is clear — no open opportunities right now."
    assert lines[3] == leadcard.FOOTER
    assert len(card) <= leadcard.MAX_CARD_CHARS


def test_build_card_hostile_lengths_capped(monkeypatch):
    monkeypatch.setattr(leadcard, "_import_module", _no_module)
    conn = _StubConn([
        [{"source": "manual", "cur": 1, "prev": 0}],
        [{"findings_diff": {"resolved": ["X" * 900]}}],
        [{"kind": "striking_distance", "target": {"query": "q" * 3000},
          "at_stake": {"est_clicks_gain": 5}}],
    ])
    card = leadcard.build_card_text(conn, "s", week_start=MON)
    assert len(card) <= leadcard.MAX_CARD_CHARS
    assert card.endswith(leadcard.FOOTER)  # the footer survives clipping


# --- waba client construction (env + lazy import failures) ----------------------------------


def test_build_waba_client_missing_env(monkeypatch):
    monkeypatch.delenv("WABA_TOKEN", raising=False)
    monkeypatch.delenv("WABA_PHONE_NUMBER_ID", raising=False)
    with pytest.raises(RuntimeError, match="WABA_TOKEN, WABA_PHONE_NUMBER_ID"):
        leadcard._build_waba_client()


def test_build_waba_client_missing_module(monkeypatch):
    monkeypatch.setenv("WABA_TOKEN", "t")
    monkeypatch.setenv("WABA_PHONE_NUMBER_ID", "p")
    monkeypatch.setattr(leadcard, "_import_module", _no_module)
    with pytest.raises(RuntimeError, match="gm.delivery.whatsapp is not available"):
        leadcard._build_waba_client()


# --- DB fixtures -----------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute(
            "truncate orgs, sites, booked_leads, connections, cost_events, jobs,"
            " queue_items, content_items, content_deltas, site_deltas"
            " restart identity cascade"
        )
        yield c


def _org(conn) -> str:
    return conn.execute("insert into orgs (name) values ('t') returning id").fetchone()["id"]


def _site(conn, org, domain="ex.com") -> str:
    return conn.execute(
        "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
        (org, domain),
    ).fetchone()["id"]


def _at(day: dt.date, hour=10) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour), tzinfo=dt.UTC)


def _ctx(conn, org, site, payload=None):
    return SimpleNamespace(
        job=SimpleNamespace(id=None, org_id=org, site_id=site, payload=payload or {}),
        conn=conn,
    )


def _wa_connection(conn, org, site, meta):
    from psycopg.types.json import Jsonb

    conn.execute(
        "insert into connections (org_id, site_id, kind, meta) values (%s, %s, 'whatsapp', %s)",
        (org, site, Jsonb(meta)),
    )


# --- DB tests --------------------------------------------------------------------------------


@requires_db
def test_add_lead_and_week_stats_monday_weeks(conn):
    org = _org(conn)
    site = _site(conn, org)
    # this week (MON..SUN): 3 whatsapp + 1 manual
    for day in (MON, MON + dt.timedelta(days=2), MON + dt.timedelta(days=6)):
        add_id = leadcard.add_lead(
            conn, org_id=org, site_id=site, source="whatsapp", occurred_at=_at(day)
        )
        assert add_id
    leadcard.add_lead(conn, org_id=org, site_id=site, source="manual",
                      occurred_at=_at(MON + dt.timedelta(days=3)))
    # previous week: 2; two weeks ago: 1 (must not count); next week: 1 (must not count)
    leadcard.add_lead(conn, org_id=org, site_id=site, occurred_at=_at(PREV_MON))
    leadcard.add_lead(conn, org_id=org, site_id=site,
                      occurred_at=_at(PREV_MON + dt.timedelta(days=6)))
    leadcard.add_lead(conn, org_id=org, site_id=site,
                      occurred_at=_at(PREV_MON - dt.timedelta(days=1)))
    leadcard.add_lead(conn, org_id=org, site_id=site,
                      occurred_at=_at(MON + dt.timedelta(days=7)))

    stats = leadcard.week_stats(conn, site, week_start=MON)
    assert stats["booked"] == 4
    assert stats["prev_week"] == 2
    assert stats["by_source"] == {"manual": 1, "whatsapp": 3}
    assert stats["trend"] == "up"
    assert stats["week_start"] == MON

    # any day of the week snaps to its Monday
    midweek = leadcard.week_stats(conn, site, week_start=MON + dt.timedelta(days=4))
    assert midweek["booked"] == 4
    assert midweek["week_start"] == MON

    prev = leadcard.week_stats(conn, site, week_start=PREV_MON)
    assert (prev["booked"], prev["prev_week"], prev["trend"]) == (2, 1, "up")


@requires_db
def test_add_lead_rejects_unknown_source(conn):
    with pytest.raises(ValueError, match="source must be one of"):
        leadcard.add_lead(conn, org_id="x", site_id="y", source="carrier_pigeon")


@requires_db
def test_add_lead_attribution_round_trip(conn):
    org = _org(conn)
    site = _site(conn, org)
    lead_id = leadcard.add_lead(
        conn, org_id=org, site_id=site, source="whatsapp",
        attribution={"referral": {"source_id": "ad1"}, "body_excerpt": "hi"},
        notes="from webhook",
    )
    row = conn.execute(
        "select * from booked_leads where id = %s", (lead_id,)
    ).fetchone()
    assert row["attribution"]["referral"] == {"source_id": "ad1"}
    assert row["notes"] == "from webhook"
    assert row["occurred_at"] is not None  # defaulted, not NULL


@requires_db
def test_build_card_db_empty_site_all_honest(conn, monkeypatch):
    monkeypatch.setattr(leadcard, "_import_module", _no_module)
    org = _org(conn)
    site = _site(conn, org)
    card = leadcard.build_card_text(conn, site, week_start=MON)
    lines = card.split("\n")
    assert lines[0].startswith("No booked consults logged this week")
    assert lines[1].startswith("No movement data yet")
    assert lines[2].startswith("Next: queue is clear")
    assert lines[3] == leadcard.FOOTER
    assert len(card) <= leadcard.MAX_CARD_CHARS
    assert org  # silences unused warning; org row backs the site


@requires_db
def test_build_card_db_full_stack(conn, monkeypatch):
    from psycopg.types.json import Jsonb

    monkeypatch.setattr(leadcard, "_import_module", _no_module)
    org = _org(conn)
    site = _site(conn, org)
    for i in range(7):
        leadcard.add_lead(conn, org_id=org, site_id=site, source="whatsapp",
                          occurred_at=_at(MON + dt.timedelta(days=i % 5)))
    for i in range(4):
        leadcard.add_lead(conn, org_id=org, site_id=site,
                          occurred_at=_at(PREV_MON + dt.timedelta(days=i)))
    conn.execute(
        "insert into site_deltas (org_id, site_id, period, payload) values (%s, %s, %s, %s)",
        (org, site, "2026-06", Jsonb({
            "period": "2026-06",
            "audits": {"movement": {"first": {"score": 60}, "last": {"score": 72},
                                    "change": 12.0}},
        })),
    )
    conn.execute(
        "insert into queue_items (org_id, site_id, kind, target, target_hash, at_stake)"
        " values (%s, %s, 'striking_distance', %s, 'h1', %s)",
        (org, site, Jsonb({"query": "best clinic dubai"}),
         Jsonb({"est_clicks_gain": 120, "basis": "provisional"})),
    )
    card = leadcard.build_card_text(conn, site, week_start=MON)
    lines = card.split("\n")
    assert lines[0] == "Booked consults this week: 7 (▲ from 4)"
    assert lines[1] == "Site score up 12 (60 → 72) in 2026-06."
    assert lines[2] == (
        "Next: Push a striking-distance query — 'best clinic dubai'"
        " (~120 clicks/mo at stake)"
    )
    assert lines[3] == leadcard.FOOTER


@requires_db
def test_build_card_db_resolved_finding_beats_receipt(conn, monkeypatch):
    from psycopg.types.json import Jsonb

    monkeypatch.setattr(leadcard, "_import_module", _no_module)
    org = _org(conn)
    site = _site(conn, org)
    conn.execute(
        "insert into site_deltas (org_id, site_id, period, payload) values (%s, %s, %s, %s)",
        (org, site, "2026-06",
         Jsonb({"period": "2026-06",
                "audits": {"movement": {"first": {"score": 60}, "last": {"score": 72},
                                        "change": 12.0}}})),
    )
    item = conn.execute(
        "insert into content_items (org_id, site_id, kind, status)"
        " values (%s, %s, 'refresh', 'measured') returning id",
        (org, site),
    ).fetchone()["id"]
    conn.execute(
        "insert into content_deltas (org_id, content_item_id, window_start, window_end,"
        " findings_diff) values (%s, %s, %s, %s, %s)",
        (org, item, MON, MON + dt.timedelta(days=27),
         Jsonb({"resolved": ["B1"], "regressed": []})),
    )
    card = leadcard.build_card_text(conn, site, week_start=MON)
    assert "resolved in the latest audit" in card.split("\n")[1]
    assert "Site score" not in card


@requires_db
def test_handler_sends_card_and_records_zero_cost(conn):
    org = _org(conn)
    site = _site(conn, org)
    _wa_connection(conn, org, site,
                   {"recipient_wa_id": "971500000001", "phone_number_id": "pn1"})
    fake = FakeWaba()
    ctx = _ctx(conn, org, site, payload={"week_start": MON.isoformat()})
    leadcard.handle_send_lead_card(ctx, waba_client=fake)

    assert len(fake.sent) == 1
    to, body = fake.sent[0]
    assert to == "971500000001"
    assert body.endswith(leadcard.FOOTER)
    assert len(body) <= leadcard.MAX_CARD_CHARS

    cost = conn.execute(
        "select * from cost_events where provider = 'waba' and purpose = 'lead_card'"
    ).fetchall()
    assert len(cost) == 1
    assert float(cost[0]["cost_cents"]) == 0.0
    assert str(cost[0]["org_id"]) == str(org)
    assert cost[0]["units"]["messages"] == 1
    assert cost[0]["units"]["week_start"] == MON.isoformat()
    # privacy: the recipient wa_id never lands in the audit trail
    assert "971500000001" not in str(cost[0]["units"])


@requires_db
def test_handler_missing_connection_is_a_clear_failure(conn):
    org = _org(conn)
    site = _site(conn, org)
    with pytest.raises(ValueError, match="no whatsapp connection"):
        leadcard.handle_send_lead_card(_ctx(conn, org, site), waba_client=FakeWaba())


@requires_db
def test_handler_missing_recipient_is_a_clear_failure(conn):
    org = _org(conn)
    site = _site(conn, org)
    _wa_connection(conn, org, site, {"phone_number_id": "pn1"})  # no recipient_wa_id
    with pytest.raises(ValueError, match="recipient_wa_id"):
        leadcard.handle_send_lead_card(_ctx(conn, org, site), waba_client=FakeWaba())


@requires_db
def test_handler_bad_week_start_payload(conn):
    org = _org(conn)
    site = _site(conn, org)
    _wa_connection(conn, org, site, {"recipient_wa_id": "9715", "phone_number_id": "pn1"})
    ctx = _ctx(conn, org, site, payload={"week_start": "next tuesday"})
    with pytest.raises(ValueError, match="invalid week_start"):
        leadcard.handle_send_lead_card(ctx, waba_client=FakeWaba())
