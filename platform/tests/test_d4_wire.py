"""Phase D4 WP-WIRE tests: gm/api.py + gm/cli.py wiring over WP-H/I/J.

- Auth-guard trio on every new /admin route (env unset / no header / wrong
  header -> uniform 404) and proof the WP-H console router is mounted on the
  real api.app. These are pure: the guard fires before any handler touches a
  connection, so they always run.
- CLI happy paths via the Typer runner: `gm spend` over planted cost_events,
  `gm refusal` add -> list -> stats round-trip, and the `gm queue` rendering
  golden over one row of EVERY queue kind through normalize_at_stake.
- Honest empty states on a fresh DB: true zeros stay zeros, zero-denominator
  aggregates stay None ("no refusals logged", "no cap configured",
  "balance unreachable" — never invented numbers).

ZERO network: dataforseo_balance is either short-circuited by unset env (its
documented no-credentials path returns before any transport) or monkeypatched.
DB tests run under the DATABASE_URL skip guard.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from gm import api, cli

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

runner = CliRunner()
client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """No ambient budget/credentials/token may leak into any test (WP-I's
    hermeticity discipline); with DATAFORSEO_* unset the balance call returns
    its honest None before any network transport is built."""
    monkeypatch.delenv("GM_DFS_MONTHLY_BUDGET_CENTS", raising=False)
    monkeypatch.delenv("DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DATAFORSEO_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)


# --- DB fixtures ----------------------------------------------------------------------------


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
def org(conn):
    """Exactly ONE org (the /admin refusal routes resolve the sole org)."""
    conn.execute("truncate orgs restart identity cascade")
    return conn.execute("insert into orgs (name) values ('wire') returning id").fetchone()["id"]


@pytest.fixture()
def cli_org(org, monkeypatch):
    """`gm` commands resolve to the test org (test_d2_cli's _org patch)."""
    monkeypatch.setattr(cli, "_org", lambda _conn: {"id": org, "name": "wire"})
    return org


@pytest.fixture()
def site(conn, cli_org):
    domain = f"wire-{uuid.uuid4().hex[:8]}.ae"
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
        (cli_org, domain),
    ).fetchone()["id"]
    return {"org_id": cli_org, "site_id": site_id, "domain": domain}


@pytest.fixture()
def clean_costs(conn):
    conn.execute("truncate cost_events restart identity")


def _cost(conn, provider, purpose, cents, *, days_ago=0):
    conn.execute(
        "insert into cost_events (provider, purpose, cost_cents, created_at)"
        " values (%s, %s, %s, now() - make_interval(days => %s))",
        (provider, purpose, cents, days_ago),
    )


# --- auth-guard trio on every new admin route (always-run; guard precedes any DB) -----------

NEW_ROUTES = [
    ("GET", "/admin/spend"),
    ("GET", "/admin/refusals"),
    ("POST", "/admin/refusals"),
]


@pytest.mark.parametrize("method,path", NEW_ROUTES)
def test_new_routes_404_when_admin_token_env_unset(method, path):
    # even a header-bearing request sees no surface (env already deleted)
    r = client.request(method, path, headers={"X-Admin-Token": "anything"})
    assert r.status_code == 404


@pytest.mark.parametrize("method,path", NEW_ROUTES)
def test_new_routes_404_on_missing_header(monkeypatch, method, path):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    assert client.request(method, path).status_code == 404


@pytest.mark.parametrize("method,path", NEW_ROUTES)
def test_new_routes_404_on_wrong_header(monkeypatch, method, path):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    r = client.request(method, path, headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 404


def test_post_refusals_guard_fires_before_body_validation(monkeypatch):
    # a bodyless POST with a bad token is a 404, never a 422 shape oracle
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    assert client.post("/admin/refusals").status_code == 404


def test_console_router_mounted_on_api_app(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    r = client.get("/admin/ui")
    assert r.status_code == 200
    assert 'id="spend"' in r.text  # WP-H's shell, served through gm.api.app
    # console JSON endpoints answer through api.app with the same 404 posture
    assert client.get("/admin/overview", headers={"X-Admin-Token": "wrong"}).status_code == 404
    monkeypatch.delenv("ADMIN_TOKEN")
    assert client.get("/admin/ui").status_code == 404


# --- pure renderers: _usd + _budget_bar -----------------------------------------------------


def test_usd_never_floors_real_spend_to_zero():
    assert cli._usd(0) == "$0.00"  # a true zero is a true zero
    assert cli._usd(42) == "$0.42"
    assert cli._usd(12345.6) == "$123.46"
    assert cli._usd(0.2) == "$0.0020"  # sub-cent spend stays visible


def test_budget_bar_no_cap_is_a_sentence_not_a_fake_bar():
    line = cli._budget_bar(
        {"cap_cents": None, "spent_cents": 0.0, "projected_month_cents": None,
         "exceeded": False, "note": "no cap configured"}
    )
    assert line == "no cap configured"
    assert "[" not in line and "%" not in line


def test_budget_bar_contract_golden_42_percent():
    line = cli._budget_bar(
        {"cap_cents": 100, "spent_cents": 42.0, "projected_month_cents": 100.0,
         "exceeded": False, "note": None}
    )
    assert line == "[####----] 42% of cap ($0.42 of $1.00)"


def test_budget_bar_exceeded_clamps_fill_and_says_so():
    line = cli._budget_bar(
        {"cap_cents": 40, "spent_cents": 42.0, "projected_month_cents": 84.0,
         "exceeded": True, "note": None}
    )
    assert line.startswith("[########] 105% of cap ($0.42 of $0.40)")
    assert "EXCEEDED" in line and "refusing" in line


# --- GET /admin/spend (DB) -------------------------------------------------------------------


@requires_db
def test_admin_spend_endpoint_shape_and_serialization(conn, clean_costs, monkeypatch):
    _cost(conn, "dataforseo", "serp", 42)
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "100")
    monkeypatch.setattr(
        "gm.intel.spend.dataforseo_balance",
        lambda client=None: {"balance": 5.5, "note": None},
    )
    r = client.get("/admin/spend?days=7", headers={"X-Admin-Token": "sekret"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"rollup", "balance", "budget"}
    rollup = body["rollup"]
    assert rollup["window_days"] == 7
    assert rollup["total_cents"] == 42.0
    assert rollup["by_provider"] == [
        {"provider": "dataforseo", "cost_cents": 42.0, "events": 1}
    ]
    assert rollup["by_purpose"] == [
        {"provider": "dataforseo", "purpose": "serp", "cost_cents": 42.0, "events": 1}
    ]
    # WP-I hands back a native date; the wire layer must serialize it
    assert rollup["by_day"] == [
        {"date": dt.date.today().isoformat(), "provider": "dataforseo", "cost_cents": 42.0}
    ]
    assert rollup["last_event"]["purpose"] == "serp"
    assert body["balance"] == {"balance": 5.5, "note": None}
    budget = body["budget"]
    assert budget["cap_cents"] == 100
    assert budget["spent_cents"] == 42.0
    assert budget["exceeded"] is False
    assert budget["projected_month_cents"] > 0


@requires_db
def test_admin_spend_endpoint_fresh_db_honesty(conn, clean_costs, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    r = client.get("/admin/spend", headers={"X-Admin-Token": "sekret"})
    assert r.status_code == 200
    body = r.json()
    assert body["rollup"]["window_days"] == 30  # contract default
    assert body["rollup"]["total_cents"] == 0.0  # true zero: nothing spent
    assert body["rollup"]["last_event"] is None
    assert body["balance"]["balance"] is None  # env unset -> honest None + note
    assert body["balance"]["note"]
    assert body["budget"]["cap_cents"] is None
    assert body["budget"]["projected_month_cents"] is None  # never invented
    assert body["budget"]["note"] == "no cap configured"


# --- GET/POST /admin/refusals (DB) ------------------------------------------------------------


@requires_db
def test_admin_refusals_roundtrip(conn, org, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    headers = {"X-Admin-Token": "sekret"}

    r = client.get("/admin/refusals", headers=headers)
    assert r.status_code == 200
    assert r.json() == {
        "refusals": [],
        "stats": {
            "total": 0,
            "by_reason": {"diy": 0, "price": 0, "timing": 0, "trust": 0, "other": 0},
            "diy_share": None,  # empty ledger is NOT "0% DIY"
        },
    }

    r = client.post(
        "/admin/refusals", headers=headers,
        json={"prospect": "Smile Clinic", "reason": "diy"},
    )
    assert r.status_code == 200
    assert uuid.UUID(r.json()["id"])

    older = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    r = client.post(
        "/admin/refusals", headers=headers,
        json={"prospect": "Bright Dental", "reason": "price", "source": "referral_call",
              "notes": "wants half the retainer", "refused_at": older},
    )
    assert r.status_code == 200

    body = client.get("/admin/refusals?days=180", headers=headers).json()
    assert [row["prospect"] for row in body["refusals"]] == ["Smile Clinic", "Bright Dental"]
    assert body["refusals"][0]["source"] == "agency_pitch"  # defaults applied
    assert body["refusals"][1]["refused_at"] == older
    assert body["refusals"][1]["notes"] == "wants half the retainer"
    assert body["stats"]["total"] == 2
    assert body["stats"]["diy_share"] == 0.5


@requires_db
def test_admin_refusals_bad_reason_is_400_with_typed_message(conn, org, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    r = client.post(
        "/admin/refusals", headers={"X-Admin-Token": "sekret"},
        json={"prospect": "Smile Clinic", "reason": "ghosted"},
    )
    assert r.status_code == 400
    assert "diy/price/timing/trust/other" in r.json()["detail"]
    assert "'ghosted'" in r.json()["detail"]
    assert conn.execute("select count(*) as n from refusals").fetchone()["n"] == 0


# --- gm spend (CLI) ---------------------------------------------------------------------------


@requires_db
def test_spend_cli_planted_rollup_balance_and_bar(conn, clean_costs, monkeypatch):
    _cost(conn, "dataforseo", "serp", 42)
    _cost(conn, "anthropic", "llm", 10, days_ago=1)
    monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "100")
    monkeypatch.setattr(
        "gm.intel.spend.dataforseo_balance",
        lambda client=None: {"balance": 103.55, "note": None},
    )
    result = runner.invoke(cli.app, ["spend"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "spend last 30d: $0.52 total" in out
    assert "by provider:" in out and "by purpose:" in out and "by day:" in out
    assert "dataforseo" in out and "$0.42" in out
    assert "anthropic" in out and "$0.10" in out
    assert "serp" in out and "llm" in out
    assert dt.date.today().isoformat() in out  # by-day row for today's event
    assert "last event:" in out
    assert "dataforseo balance: $103.55" in out
    assert "budget: [####----] 42% of cap ($0.42 of $1.00)" in out
    assert out.index("42% of cap") > out.index("dataforseo balance")  # bar after balance line


@requires_db
def test_spend_cli_days_flag_narrows_the_window(conn, clean_costs, monkeypatch):
    _cost(conn, "anthropic", "llm", 10, days_ago=5)
    monkeypatch.setattr(
        "gm.intel.spend.dataforseo_balance",
        lambda client=None: {"balance": 1.0, "note": None},
    )
    result = runner.invoke(cli.app, ["spend", "--days", "3"])
    assert result.exit_code == 0, result.output
    assert "spend last 3d: $0.00 total" in result.output
    assert "no provider spend recorded in this window" in result.output


@requires_db
def test_spend_cli_fresh_db_empty_states(conn, clean_costs):
    # env fully unset (autouse): balance short-circuits WITHOUT network
    result = runner.invoke(cli.app, ["spend"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "spend last 30d: $0.00 total" in out  # a true zero, not "no data"
    assert "no provider spend recorded in this window" in out
    assert "last event:" not in out
    assert "dataforseo balance: unreachable" in out  # honest None, reason attached
    assert "DATAFORSEO_LOGIN" in out
    assert "budget: no cap configured" in out
    assert "% of cap" not in out  # no cap -> never a fake bar
    assert "projected month: no spend yet this month" in out  # None, not $0.00


@requires_db
def test_spend_cli_exceeded_cap_shows_full_bar_and_refusal_note(conn, clean_costs, monkeypatch):
    _cost(conn, "dataforseo", "serp", 42)
    monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "40")
    result = runner.invoke(cli.app, ["spend"])
    assert result.exit_code == 0, result.output
    assert "[########] 105% of cap" in result.output
    assert "EXCEEDED: paid DataForSEO calls are refusing" in result.output


# --- gm refusal add -> list -> stats (CLI) -----------------------------------------------------


@requires_db
def test_refusal_add_list_stats_roundtrip(conn, cli_org):
    result = runner.invoke(cli.app, ["refusal", "add", "Smile Clinic", "--reason", "diy"])
    assert result.exit_code == 0, result.output
    assert uuid.UUID(result.output.strip())  # echoes the row id

    older = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    result = runner.invoke(
        cli.app,
        ["refusal", "add", "Bright Dental", "--reason", "price", "--source", "referral_call",
         "--notes", "wants half the retainer", "--date", older],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli.app, ["refusal", "list"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2
    assert "Smile Clinic" in lines[0] and "[agency_pitch]" in lines[0]  # newest first
    assert lines[1].startswith(older)
    assert "Bright Dental" in lines[1]
    assert "[referral_call]" in lines[1] and "wants half the retainer" in lines[1]

    result = runner.invoke(cli.app, ["refusal", "stats"])
    assert result.exit_code == 0, result.output
    assert "refusals last 180d: 2" in result.output
    assert "diy=1 price=1 timing=0 trust=0 other=0" in result.output
    assert "DIY share: 50%" in result.output


@requires_db
def test_refusal_stats_and_list_empty_are_honest(conn, cli_org):
    result = runner.invoke(cli.app, ["refusal", "stats"])
    assert result.exit_code == 0, result.output
    assert "refusals last 180d: 0" in result.output
    assert "diy=0 price=0 timing=0 trust=0 other=0" in result.output
    assert "DIY share: no refusals logged" in result.output  # tripwire honesty
    assert "0%" not in result.output  # an empty ledger must never read 0% DIY

    result = runner.invoke(cli.app, ["refusal", "list", "--days", "30"])
    assert result.exit_code == 0, result.output
    assert "no refusals logged in the last 30d" in result.output


@requires_db
def test_refusal_add_bad_reason_typed_message_no_row(conn, cli_org):
    result = runner.invoke(cli.app, ["refusal", "add", "Smile Clinic", "--reason", "ghosted"])
    assert result.exit_code != 0
    assert "diy/price/timing/trust/other" in result.output
    assert conn.execute("select count(*) as n from refusals").fetchone()["n"] == 0


@requires_db
def test_refusal_add_bad_date_rejected_before_db(conn, cli_org):
    result = runner.invoke(
        cli.app, ["refusal", "add", "Smile Clinic", "--reason", "diy", "--date", "junk"]
    )
    assert result.exit_code != 0
    assert "YYYY-MM-DD" in result.output
    assert conn.execute("select count(*) as n from refusals").fetchone()["n"] == 0


# --- gm queue rendering golden: one row of every kind through normalize_at_stake ---------------


def _queue_item(conn, site, kind, target, at_stake):
    conn.execute(
        "insert into queue_items (org_id, site_id, kind, target, target_hash, at_stake)"
        " values (%s, %s, %s, %s, %s, %s)",
        (site["org_id"], site["site_id"], kind, Jsonb(target),
         uuid.uuid4().hex, Jsonb(at_stake)),
    )


@requires_db
def test_queue_golden_every_kind(conn, site):
    _queue_item(conn, site, "striking_distance",
                {"query": "botox dubai", "page": "https://x.ae/botox"},
                {"est_clicks_gain": 45.0, "position": 8.0, "ctr": 0.02, "basis": "final"})
    _queue_item(conn, site, "ctr_outlier",
                {"query": "fillers dubai", "page": "https://x.ae/fillers"},
                {"est_clicks_gain": 80.0, "position": 3.0, "ctr": 0.01,
                 "expected_ctr": 0.09, "basis": "provisional"})
    _queue_item(conn, site, "decay",
                {"page": "https://x.ae/peels"},
                {"est_clicks_gain": 50.0, "drop_pct": 0.3333, "basis": "final"})
    _queue_item(conn, site, "cannibalization",
                {"query": "morpheus8 dubai"},
                {"est_clicks_gain": 12.5, "basis": "final",
                 "pages": [{"page": "https://x.ae/a"}, {"page": "https://x.ae/b"}]})
    _queue_item(conn, site, "keyword_gap",
                {"query": "dental implants dubai"},
                {"volume": 720, "best_competitor": "rival.example",
                 "their_position": 4, "basis": "labs"})
    _queue_item(conn, site, "competitor_candidate",
                {"domain": "rival.ae"},
                {"intersections": 37, "avg_position": 12.4,
                 "their_etv": 30124.5, "basis": "labs"})
    _queue_item(conn, site, "local_presence",
                {"check_id": "LP-01"},
                {"issue": "local pack present but site absent",
                 "queries_with_pack": 3, "basis": "serp_local_pack"})
    # an unquantified row must say so, never render a fake zero
    _queue_item(conn, site, "striking_distance", {"query": "mystery"}, {"basis": "final"})

    result = runner.invoke(cli.app, ["queue", site["domain"]])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]

    # gain desc first, then the unranked kinds alphabetically (deterministic golden)
    expected = [
        ("ctr_outlier", "+80 clicks/mo", "fillers dubai",
         "position 3, ctr 1.00%, expected 9.00%"),
        ("decay", "+50 clicks/mo", "https://x.ae/peels", "down 33% vs prior 28d"),
        ("striking_distance", "+45 clicks/mo", "botox dubai", "position 8, ctr 2.00%"),
        ("cannibalization", "+12.5 clicks/mo", "morpheus8 dubai", "2 pages competing"),
        ("competitor_candidate", "37 shared keywords", "rival.ae",
         "avg position 12.4, their traffic value 30124.5"),
        ("keyword_gap", "720 searches/mo", "dental implants dubai",
         "best: rival.example at #4"),
        ("local_presence", "local pack present but site absent", "LP-01",
         "packs on 3 tracked queries"),
        ("striking_distance", "at stake: not quantified", "mystery", '{"basis":"final"}'),
    ]
    assert len(lines) == len(expected)
    for line, (kind, headline, tgt, detail) in zip(lines, expected, strict=True):
        assert line == f"{kind:18} {headline:26} {tgt[:50]:50} {detail}"
    # the ad-hoc pre-D4 renderings are gone
    assert "vol " not in result.output and "[labs]" not in result.output


@requires_db
def test_queue_kind_filter_and_empty_state(conn, site):
    result = runner.invoke(cli.app, ["queue", site["domain"]])
    assert result.exit_code == 0, result.output
    assert "queue empty" in result.output

    _queue_item(conn, site, "keyword_gap", {"query": "veneers dubai"},
                {"volume": 90, "basis": "labs"})
    result = runner.invoke(cli.app, ["queue", site["domain"], "--kind", "keyword_gap"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("keyword_gap")
    assert "90 searches/mo" in lines[0]
    assert "no data yet" in lines[0]  # volume without competitor context stays honest

    result = runner.invoke(cli.app, ["queue", site["domain"], "--kind", "decay"])
    assert result.exit_code == 0, result.output
    assert "queue empty" in result.output
