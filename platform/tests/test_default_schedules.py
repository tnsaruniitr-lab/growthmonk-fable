"""Phase D3 WP-E tests: default schedules (gm/core/schedules.py) + cli.py wiring.

ZERO network — WP-G's ads modules are consumed strictly by contract signature and
faked here via a sys.modules stand-in (never imported at module load, so these
tests pass whether or not gm/intel/ads_ingest.py exists yet). CLI commands run
through typer's CliRunner against the real database; `cli._org` is monkeypatched
to the per-test org. DB tests skip without DATABASE_URL.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
import types
import uuid
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from gm import cli
from gm.core import schedules

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

runner = CliRunner()

UTC = dt.UTC
DEFAULT_TYPES = [jt for jt, _ in schedules.DEFAULT_SCHEDULES]


# --- fixtures -------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        yield c


@pytest.fixture()
def org(conn, monkeypatch):
    org_id = conn.execute(
        "insert into orgs (name) values (%s) returning id", (f"wpe-{uuid.uuid4().hex[:8]}",)
    ).fetchone()["id"]
    monkeypatch.setattr(cli, "_org", lambda _conn: {"id": org_id, "name": "wpe"})
    return org_id


@pytest.fixture()
def site(conn, org):
    domain = f"client-{uuid.uuid4().hex[:8]}.ae"
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
        (org, domain),
    ).fetchone()["id"]
    return {"org_id": org, "site_id": site_id, "domain": domain}


def make_site(conn, org, *, control=False):
    domain = f"client-{uuid.uuid4().hex[:8]}.ae"
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm, is_control) values (%s, %s, %s) returning id",
        (org, domain, control),
    ).fetchone()["id"]
    return {"org_id": org, "site_id": site_id, "domain": domain}


def add_connection(conn, site, kind, status="ok", meta=None):
    conn.execute(
        "insert into connections (org_id, site_id, kind, status, meta) values (%s,%s,%s,%s,%s)"
        " on conflict (site_id, kind) do update set status=excluded.status, meta=excluded.meta",
        (site["org_id"], site["site_id"], kind, status, Jsonb(meta or {})),
    )


def schedule_rows(conn, site):
    return {
        r["job_type"]: r
        for r in conn.execute(
            "select * from schedules where site_id = %s", (site["site_id"],)
        ).fetchall()
    }


def fake_ads_ingest(monkeypatch, calls):
    """Install a gm.intel.ads_ingest stand-in matching the WP-G contract signatures."""
    mod = types.ModuleType("gm.intel.ads_ingest")

    def pull_ads_daily(conn, *, org_id, site_id, readers=None, days=7, today=None):
        calls.append(("pull", str(site_id), days))
        return {"note": "no ads connections"}

    def handle_pull_ads_daily(ctx):
        calls.append(("handle", ctx))

    mod.pull_ads_daily = pull_ads_daily
    mod.handle_pull_ads_daily = handle_pull_ads_daily
    monkeypatch.setitem(sys.modules, "gm.intel.ads_ingest", mod)
    return mod


class FakeCtx:
    """Just enough of jobs.JobContext for handle_assemble_receipt_monthly."""

    def __init__(self, conn, site_id, *, org_id=None, created_at, payload=None):
        self.conn = conn
        self.job = SimpleNamespace(
            id=1, site_id=site_id, org_id=org_id, payload=payload or {}, created_at=created_at
        )

    def heartbeat(self):
        pass


# --- cadence constants + first_run_at goldens (pure) ----------------------------------------


def test_cadence_constants_are_the_contract_values():
    assert (schedules.DAILY, schedules.WEEKLY, schedules.MONTHLY) == (1440, 10080, 43200)
    assert schedules.DEFAULT_SCHEDULES == (
        ("track_serps", 10080),
        ("keyword_gap", 43200),
        ("assemble_receipt_monthly", 43200),
        ("refresh_competitor_profiles", 43200),
    )
    assert schedules.CONDITIONAL_SCHEDULES == {
        "send_lead_card": (("whatsapp",), 10080),
        "pull_ads_daily": (("google_ads", "meta_ads"), 1440),
    }
    # cli.py sources its monthly cadence from the single copy
    assert cli.MONTHLY_MINUTES is schedules.MONTHLY


def test_first_run_at_most_jobs_start_now():
    today = dt.date(2026, 7, 4)  # a Saturday
    for job_type in ("track_serps", "keyword_gap", "refresh_competitor_profiles",
                     "pull_ads_daily"):
        assert schedules.first_run_at(job_type, today=today) == dt.datetime(
            2026, 7, 4, tzinfo=UTC
        )


def test_first_run_at_send_lead_card_next_monday_0600_utc():
    # Saturday -> the coming Monday
    assert schedules.first_run_at("send_lead_card", today=dt.date(2026, 7, 4)) == dt.datetime(
        2026, 7, 6, 6, 0, tzinfo=UTC
    )
    # Sunday -> tomorrow
    assert schedules.first_run_at("send_lead_card", today=dt.date(2026, 7, 5)) == dt.datetime(
        2026, 7, 6, 6, 0, tzinfo=UTC
    )
    # a Monday -> STRICTLY the next Monday (first card covers a full tracked week)
    assert schedules.first_run_at("send_lead_card", today=dt.date(2026, 7, 6)) == dt.datetime(
        2026, 7, 13, 6, 0, tzinfo=UTC
    )


def test_first_run_at_receipt_first_of_next_month_0600_utc():
    assert schedules.first_run_at(
        "assemble_receipt_monthly", today=dt.date(2026, 7, 4)
    ) == dt.datetime(2026, 8, 1, 6, 0, tzinfo=UTC)
    # December rolls the year
    assert schedules.first_run_at(
        "assemble_receipt_monthly", today=dt.date(2026, 12, 15)
    ) == dt.datetime(2027, 1, 1, 6, 0, tzinfo=UTC)
    # 1st of a month still waits for NEXT month (never a partial month's receipt)
    assert schedules.first_run_at(
        "assemble_receipt_monthly", today=dt.date(2026, 7, 1)
    ) == dt.datetime(2026, 8, 1, 6, 0, tzinfo=UTC)


def test_first_run_at_defaults_to_real_now_tz_aware():
    before = dt.datetime.now(UTC)
    got = schedules.first_run_at("track_serps")
    after = dt.datetime.now(UTC)
    assert got.tzinfo is not None and before <= got <= after
    card = schedules.first_run_at("send_lead_card")
    assert card.tzinfo is not None and card > after and card.weekday() == 0


# --- ensure_default_schedules ----------------------------------------------------------------


@requires_db
def test_ensure_creates_defaults_and_skips_conditionals(conn, site):
    result = schedules.ensure_default_schedules(
        conn, org_id=site["org_id"], site_id=site["site_id"], today=dt.date(2026, 7, 4)
    )
    assert result["created"] == DEFAULT_TYPES
    assert result["existing"] == []
    assert result["skipped"] == {
        "send_lead_card": "no whatsapp connection",
        "pull_ads_daily": "no google_ads/meta_ads connection",
    }
    rows = schedule_rows(conn, site)
    assert set(rows) == set(DEFAULT_TYPES)
    for job_type, every in schedules.DEFAULT_SCHEDULES:
        row = rows[job_type]
        assert row["every_minutes"] == every
        assert row["payload"] == {}
        assert row["org_id"] == site["org_id"]
        assert row["enabled"] is True
    # first-run goldens land in next_run_at
    assert rows["track_serps"]["next_run_at"] == dt.datetime(2026, 7, 4, tzinfo=UTC)
    assert rows["assemble_receipt_monthly"]["next_run_at"] == dt.datetime(
        2026, 8, 1, 6, 0, tzinfo=UTC
    )


@requires_db
def test_ensure_is_idempotent_and_never_mutates_tuned_rows(conn, site):
    schedules.ensure_default_schedules(conn, org_id=site["org_id"], site_id=site["site_id"])
    # operator tunes one row and disables another — both must survive re-invocation
    conn.execute(
        "update schedules set every_minutes = 999, payload = %s"
        " where site_id = %s and job_type = 'track_serps'",
        (Jsonb({"tuned": True}), site["site_id"]),
    )
    conn.execute(
        "update schedules set enabled = false"
        " where site_id = %s and job_type = 'keyword_gap'",
        (site["site_id"],),
    )
    result = schedules.ensure_default_schedules(
        conn, org_id=site["org_id"], site_id=site["site_id"]
    )
    assert result["created"] == []
    assert result["existing"] == DEFAULT_TYPES
    rows = schedule_rows(conn, site)
    assert len(rows) == len(DEFAULT_TYPES)  # no duplicates, ever
    assert rows["track_serps"]["every_minutes"] == 999
    assert rows["track_serps"]["payload"] == {"tuned": True}
    assert rows["keyword_gap"]["enabled"] is False  # disabled counts as existing


@requires_db
def test_conditional_matrix(conn, site):
    # whatsapp ok -> send_lead_card appears; ads still skipped
    add_connection(conn, site, "whatsapp")
    result = schedules.ensure_default_schedules(
        conn, org_id=site["org_id"], site_id=site["site_id"]
    )
    assert "send_lead_card" in result["created"]
    assert result["skipped"] == {"pull_ads_daily": "no google_ads/meta_ads connection"}
    assert schedule_rows(conn, site)["send_lead_card"]["every_minutes"] == schedules.WEEKLY

    # a BROKEN ads connection does not qualify
    add_connection(conn, site, "google_ads", status="broken")
    result = schedules.ensure_default_schedules(
        conn, org_id=site["org_id"], site_id=site["site_id"]
    )
    assert result["created"] == []
    assert result["skipped"] == {"pull_ads_daily": "no google_ads/meta_ads connection"}

    # status back to ok -> the DAILY schedule appears
    add_connection(conn, site, "google_ads", status="ok")
    result = schedules.ensure_default_schedules(
        conn, org_id=site["org_id"], site_id=site["site_id"]
    )
    assert result["created"] == ["pull_ads_daily"]
    assert schedule_rows(conn, site)["pull_ads_daily"]["every_minutes"] == schedules.DAILY


@requires_db
def test_conditional_any_of_kinds_meta_ads_alone_qualifies(conn, org):
    site = make_site(conn, org)
    add_connection(conn, site, "meta_ads", meta={"act_id": "123"})
    result = schedules.ensure_default_schedules(
        conn, org_id=site["org_id"], site_id=site["site_id"]
    )
    assert "pull_ads_daily" in result["created"]
    assert "send_lead_card" in result["skipped"]


# --- handle_assemble_receipt_monthly ---------------------------------------------------------


@requires_db
def test_monthly_handler_derives_prior_month_and_idempotency_key(conn, site):
    created_at = dt.datetime(2026, 7, 15, 9, 30, tzinfo=UTC)
    ctx = FakeCtx(conn, site["site_id"], org_id=site["org_id"], created_at=created_at)
    schedules.handle_assemble_receipt_monthly(ctx)
    job = conn.execute(
        "select * from jobs where site_id = %s and type = 'assemble_receipt'",
        (site["site_id"],),
    ).fetchone()
    assert job["payload"] == {"period": "2026-06"}
    assert job["idempotency_key"] == f"receipt:{site['site_id']}:2026-06"
    assert job["org_id"] == site["org_id"]

    # retry of the SAME job row (same created_at) dedupes: still exactly one
    schedules.handle_assemble_receipt_monthly(ctx)
    n = conn.execute(
        "select count(*) as n from jobs where site_id = %s and type = 'assemble_receipt'",
        (site["site_id"],),
    ).fetchone()["n"]
    assert n == 1

    # the NEXT month's tick enqueues the next period
    ctx2 = FakeCtx(
        conn, site["site_id"], org_id=site["org_id"],
        created_at=dt.datetime(2026, 8, 1, 6, 0, tzinfo=UTC),
    )
    schedules.handle_assemble_receipt_monthly(ctx2)
    periods = [
        r["payload"]["period"]
        for r in conn.execute(
            "select payload from jobs where site_id = %s and type = 'assemble_receipt'"
            " order by id",
            (site["site_id"],),
        ).fetchall()
    ]
    assert periods == ["2026-06", "2026-07"]


@requires_db
def test_monthly_handler_january_rolls_year_and_resolves_org(conn, site):
    # org_id absent from the job -> resolved from sites (handle_keyword_gap's pattern)
    ctx = FakeCtx(
        conn, site["site_id"], org_id=None,
        created_at=dt.datetime(2026, 1, 3, 6, 0, tzinfo=UTC),
    )
    schedules.handle_assemble_receipt_monthly(ctx)
    job = conn.execute(
        "select * from jobs where site_id = %s and type = 'assemble_receipt'",
        (site["site_id"],),
    ).fetchone()
    assert job["payload"] == {"period": "2025-12"}
    assert job["org_id"] == site["org_id"]


def test_monthly_handler_requires_site_id():
    ctx = FakeCtx(None, None, created_at=dt.datetime(2026, 7, 1, tzinfo=UTC))
    with pytest.raises(RuntimeError, match="site_id"):
        schedules.handle_assemble_receipt_monthly(ctx)


# --- backfill_default_schedules ---------------------------------------------------------------


@requires_db
def test_backfill_covers_non_control_sites_and_dry_run_writes_nothing(conn, org):
    a = make_site(conn, org)
    b = make_site(conn, org)
    ctrl = make_site(conn, org, control=True)

    report = schedules.backfill_default_schedules(conn, org_id=org, dry_run=True)
    assert set(report["sites"]) == {a["domain"], b["domain"]}  # control excluded
    assert report["sites"][a["domain"]]["created"] == DEFAULT_TYPES
    n = conn.execute(
        "select count(*) as n from schedules where org_id = %s", (org,)
    ).fetchone()["n"]
    assert n == 0  # dry run reports only

    report = schedules.backfill_default_schedules(conn, org_id=org)
    assert report["sites"][a["domain"]]["created"] == DEFAULT_TYPES
    assert report["sites"][b["domain"]]["created"] == DEFAULT_TYPES
    assert schedule_rows(conn, a).keys() == set(DEFAULT_TYPES)
    assert schedule_rows(conn, ctrl) == {}

    # second pass: everything existing, nothing duplicated
    report = schedules.backfill_default_schedules(conn, org_id=org)
    assert report["sites"][a["domain"]] == {
        "created": [], "existing": DEFAULT_TYPES,
        "skipped": {
            "send_lead_card": "no whatsapp connection",
            "pull_ads_daily": "no google_ads/meta_ads connection",
        },
    }


# --- CLI: gm site add ------------------------------------------------------------------------


@requires_db
def test_site_add_wires_default_schedules_by_default(conn, org):
    domain = f"client-{uuid.uuid4().hex[:8]}.ae"
    result = runner.invoke(cli.app, ["site", "add", domain])
    assert result.exit_code == 0, result.output
    assert "schedules created: " + ", ".join(DEFAULT_TYPES) in result.output
    assert "send_lead_card — no whatsapp connection" in result.output
    assert "pull_ads_daily — no google_ads/meta_ads connection" in result.output
    site_id = result.output.splitlines()[0].strip()
    rows = schedule_rows(conn, {"site_id": site_id})
    assert set(rows) == set(DEFAULT_TYPES)

    # double add: same site, nothing duplicated, honest "existing" echo
    result = runner.invoke(cli.app, ["site", "add", domain])
    assert result.exit_code == 0, result.output
    assert "schedules created: none" in result.output
    assert "existing (untouched): " + ", ".join(DEFAULT_TYPES) in result.output
    assert len(schedule_rows(conn, {"site_id": site_id})) == len(DEFAULT_TYPES)


@requires_db
def test_site_add_no_schedules_opts_out(conn, org):
    domain = f"client-{uuid.uuid4().hex[:8]}.ae"
    result = runner.invoke(cli.app, ["site", "add", domain, "--no-schedules"])
    assert result.exit_code == 0, result.output
    assert "default schedules skipped (--no-schedules)" in result.output
    site_id = result.output.splitlines()[0].strip()
    assert schedule_rows(conn, {"site_id": site_id}) == {}


@requires_db
def test_site_add_control_gets_no_schedules(conn, org):
    domain = f"ctrl-{uuid.uuid4().hex[:8]}.ae"
    result = runner.invoke(cli.app, ["site", "add", domain, "--control"])
    assert result.exit_code == 0, result.output
    assert "control site — default schedules not wired" in result.output
    site_id = result.output.splitlines()[0].strip()
    assert schedule_rows(conn, {"site_id": site_id}) == {}


# --- CLI: gm site backfill-schedules ----------------------------------------------------------


def test_backfill_cli_requires_exactly_one_of_domain_or_all():
    result = runner.invoke(cli.app, ["site", "backfill-schedules"])
    assert result.exit_code != 0
    assert "exactly one" in result.output
    result = runner.invoke(cli.app, ["site", "backfill-schedules", "x.ae", "--all"])
    assert result.exit_code != 0
    assert "exactly one" in result.output


@requires_db
def test_backfill_cli_single_domain_and_dry_run(conn, org):
    site = make_site(conn, org)
    result = runner.invoke(
        cli.app, ["site", "backfill-schedules", site["domain"], "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "DRY RUN — nothing written" in result.output
    assert "schedules created: " + ", ".join(DEFAULT_TYPES) in result.output
    assert schedule_rows(conn, site) == {}

    result = runner.invoke(cli.app, ["site", "backfill-schedules", site["domain"]])
    assert result.exit_code == 0, result.output
    assert f"{site['domain']}:" in result.output
    assert schedule_rows(conn, site).keys() == set(DEFAULT_TYPES)


@requires_db
def test_backfill_cli_all_skips_controls_and_refuses_control_domain(conn, org):
    site = make_site(conn, org)
    ctrl = make_site(conn, org, control=True)
    result = runner.invoke(cli.app, ["site", "backfill-schedules", "--all"])
    assert result.exit_code == 0, result.output
    assert f"{site['domain']}:" in result.output
    assert ctrl["domain"] not in result.output
    assert schedule_rows(conn, site).keys() == set(DEFAULT_TYPES)
    assert schedule_rows(conn, ctrl) == {}

    result = runner.invoke(cli.app, ["site", "backfill-schedules", ctrl["domain"]])
    assert result.exit_code == 1
    assert "control site" in result.output


# --- CLI: wa-connect re-invokes ensure ---------------------------------------------------------


@requires_db
def test_wa_connect_creates_conditional_schedule_the_moment_connection_exists(conn, org):
    domain = f"client-{uuid.uuid4().hex[:8]}.ae"
    runner.invoke(cli.app, ["site", "add", domain, "--no-schedules"])
    result = runner.invoke(
        cli.app,
        ["wa-connect", domain, "--phone-number-id", "pn1", "--recipient", "97150"],
    )
    assert result.exit_code == 0, result.output
    assert "whatsapp connected" in result.output
    site_row = conn.execute(
        "select id from sites where org_id=%s and domain_norm=%s", (org, domain)
    ).fetchone()
    rows = schedule_rows(conn, {"site_id": site_row["id"]})
    # ensure is holistic: missing defaults + the newly-qualified conditional
    assert set(rows) == set(DEFAULT_TYPES) | {"send_lead_card"}
    assert rows["send_lead_card"]["every_minutes"] == schedules.WEEKLY
    assert "pull_ads_daily — no google_ads/meta_ads connection" in result.output


# --- CLI: gm ads connect / pull / status --------------------------------------------------------


def _plain(output: str) -> str:
    # Typer/rich wraps errors in an ANSI-styled panel whose line breaks depend on
    # the terminal env (CI differs from local) — strip escapes/borders and collapse
    # whitespace so substring assertions survive any wrapping.
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
    return " ".join(text.replace("│", " ").split())


def test_ads_connect_validates_channel_and_ids():
    result = runner.invoke(cli.app, ["ads", "connect", "x.ae", "--channel", "tiktok_ads"])
    assert result.exit_code != 0
    assert "google_ads, meta_ads" in _plain(result.output)
    result = runner.invoke(cli.app, ["ads", "connect", "x.ae", "--channel", "google_ads"])
    assert result.exit_code != 0
    assert "--customer-id and --login-customer-id" in _plain(result.output)
    result = runner.invoke(cli.app, ["ads", "connect", "x.ae", "--channel", "meta_ads"])
    assert result.exit_code != 0
    assert "--act-id" in _plain(result.output)


@requires_db
def test_ads_connect_google_stores_meta_only_and_schedules_daily_pull(conn, site):
    result = runner.invoke(
        cli.app,
        ["ads", "connect", site["domain"], "--channel", "google_ads",
         "--customer-id", "111-222", "--login-customer-id", "999-888"],
    )
    assert result.exit_code == 0, result.output
    assert "google_ads connected" in result.output
    assert "tokens stay in env, never stored" in result.output
    row = conn.execute(
        "select * from connections where site_id=%s and kind='google_ads'",
        (site["site_id"],),
    ).fetchone()
    assert row["meta"] == {"customer_id": "111-222", "login_customer_id": "999-888"}
    assert row["encrypted_credentials"] is None  # tokens live in env ONLY
    assert row["status"] == "ok"
    rows = schedule_rows(conn, site)
    assert "pull_ads_daily" in rows
    assert rows["pull_ads_daily"]["every_minutes"] == schedules.DAILY
    assert "pull_ads_daily" in result.output


@requires_db
def test_ads_connect_meta_and_reconnect_heals_broken_status(conn, site):
    result = runner.invoke(
        cli.app,
        ["ads", "connect", site["domain"], "--channel", "meta_ads", "--act-id", "1234567"],
    )
    assert result.exit_code == 0, result.output
    conn.execute(
        "update connections set status='broken', last_error='401 expired token'"
        " where site_id=%s and kind='meta_ads'",
        (site["site_id"],),
    )
    result = runner.invoke(
        cli.app,
        ["ads", "connect", site["domain"], "--channel", "meta_ads", "--act-id", "7654321"],
    )
    assert result.exit_code == 0, result.output
    row = conn.execute(
        "select status, last_error, meta from connections where site_id=%s and kind='meta_ads'",
        (site["site_id"],),
    ).fetchone()
    assert row["status"] == "ok"
    assert row["last_error"] is None
    assert row["meta"] == {"act_id": "7654321"}


@requires_db
def test_ads_pull_enqueues_job(conn, site):
    result = runner.invoke(cli.app, ["ads", "pull", site["domain"]])
    assert result.exit_code == 0, result.output
    assert "enqueued job" in result.output
    job = conn.execute(
        "select * from jobs where site_id=%s and type='pull_ads_daily'", (site["site_id"],)
    ).fetchone()
    assert job["payload"] == {"days": 7}
    assert job["org_id"] == site["org_id"]
    assert job["idempotency_key"] == (
        f"pull_ads_daily:{site['site_id']}:{dt.date.today().isoformat()}"
    )


@requires_db
def test_ads_pull_now_lazy_imports_wp_g_by_contract_signature(conn, site, monkeypatch):
    calls: list = []
    fake_ads_ingest(monkeypatch, calls)
    result = runner.invoke(cli.app, ["ads", "pull", site["domain"], "--now", "--days", "3"])
    assert result.exit_code == 0, result.output
    assert calls == [("pull", str(site["site_id"]), 3)]
    assert "no ads connections" in result.output  # the honest empty state, verbatim


@requires_db
def test_ads_status_empty_and_with_rows(conn, site):
    result = runner.invoke(cli.app, ["ads", "status", site["domain"]])
    assert result.exit_code == 1
    assert "no ads connections — run: gm ads connect" in result.output

    add_connection(conn, site, "google_ads", meta={"customer_id": "1", "login_customer_id": "2"})
    result = runner.invoke(cli.app, ["ads", "status", site["domain"]])
    assert result.exit_code == 0, result.output
    assert "google_ads: ok" in result.output
    assert "no daily rows pulled yet" in result.output  # honest, never fake zeros

    conn.execute(
        "insert into ads_daily (org_id, site_id, date, channel, campaign_id, spend, currency)"
        " values (%s, %s, '2026-07-01', 'google_ads', 'c1', 12.34, 'AED'),"
        "        (%s, %s, '2026-07-02', 'google_ads', 'c1', 8.00, 'AED')",
        (site["org_id"], site["site_id"], site["org_id"], site["site_id"]),
    )
    result = runner.invoke(cli.app, ["ads", "status", site["domain"]])
    assert result.exit_code == 0, result.output
    assert "coverage: 2 day(s)  2026-07-01 → 2026-07-02" in result.output

    add_connection(conn, site, "meta_ads", status="broken", meta={"act_id": "9"})
    conn.execute(
        "update connections set last_error='401: expired' where site_id=%s and kind='meta_ads'",
        (site["site_id"],),
    )
    result = runner.invoke(cli.app, ["ads", "status", site["domain"]])
    assert "meta_ads: broken" in result.output
    assert "last_error: 401: expired" in result.output


# --- worker registration ------------------------------------------------------------------------


def test_worker_registers_d3_handlers_lazily(monkeypatch):
    captured: dict = {}

    class FakeWorker:
        def __init__(self, handlers, **kwargs):
            captured.update(handlers)

        def run_forever(self, stop_event=None):
            return None

    monkeypatch.setattr(cli.jobs_mod, "Worker", FakeWorker)
    result = runner.invoke(cli.app, ["worker"])
    assert result.exit_code == 0, result.output
    assert {"assemble_receipt_monthly", "pull_ads_daily"} <= set(captured)
    # pre-D3 handlers still registered
    assert {"track_serps", "send_lead_card", "refresh_competitor_profiles"} <= set(captured)

    calls: list = []
    monkeypatch.setattr(
        "gm.core.schedules.handle_assemble_receipt_monthly",
        lambda ctx: calls.append(("receipt_monthly", ctx)),
    )
    fake_ads_ingest(monkeypatch, calls)
    sentinel = object()
    captured["assemble_receipt_monthly"](sentinel)
    captured["pull_ads_daily"](sentinel)
    assert calls == [("receipt_monthly", sentinel), ("handle", sentinel)]
