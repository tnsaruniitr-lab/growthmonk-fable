"""Phase D4 (WP-I) spend-rail tests — ZERO network; HTTP goes through httpx.MockTransport.

Covers spend_rollup math over planted cost_events, dataforseo_balance envelope
honesty (None + note on every failure mode, true-zero stays 0.0), the budget
matrix (no cap / under / over / exact cap), require_dfs_budget's typed refusal,
and the serp purchase-path guard (cache hits stay free; purchases are refused
BEFORE any HTTP request). DB tests skip cleanly without DATABASE_URL.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import uuid

import httpx
import pytest
from psycopg.types.json import Jsonb

from gm.intel import serp as serp_mod
from gm.intel.spend import (
    BudgetExceeded,
    budget_state,
    dataforseo_balance,
    require_dfs_budget,
    spend_rollup,
)

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

LOGIN, PASSWORD = "login@example.com", "s3cret"

# Fixed, injectable `now` for deterministic month math: March 2026 has 31 days,
# and the 10th leaves the month boundary days away from any server-timezone skew.
MARCH_10 = dt.datetime(2026, 3, 10, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Hermetic: no ambient cap or credentials leak into any test."""
    monkeypatch.delenv("GM_DFS_MONTHLY_BUDGET_CENTS", raising=False)
    monkeypatch.delenv("DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DATAFORSEO_PASSWORD", raising=False)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(serp_mod, "_sleep", lambda _s: None)


def make_http(responses: list[tuple[int, object]]) -> tuple[httpx.Client, list[httpx.Request]]:
    """MockTransport client replaying `responses` in order (last one repeats)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status, body = responses[min(len(requests) - 1, len(responses) - 1)]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


# --- dataforseo_balance (no DB) --------------------------------------------------------


def balance_envelope(balance: object = 87.4523) -> dict:
    return {
        "version": "0.1.20240801",
        "status_code": 20000,
        "status_message": "Ok.",
        "time": "0.05 sec.",
        "cost": 0,
        "tasks_count": 1,
        "tasks_error": 0,
        "tasks": [
            {
                "id": "07040912-1535-0387-0000-a1b2c3d4e5f6",
                "status_code": 20000,
                "status_message": "Ok.",
                "time": "0.03 sec.",
                "cost": 0,
                "result_count": 1,
                "path": ["v3", "appendix", "user_data"],
                "data": {"api": "appendix", "function": "user_data"},
                "result": [
                    {
                        "login": LOGIN,
                        "timezone": "UTC",
                        "money": {
                            "total": 250.0,
                            "balance": balance,
                            "limits": {"day": 0.0, "minute": 0.0},
                        },
                        "rates": {"limits": {"day": 0, "minute": 0}},
                    }
                ],
            }
        ],
    }


def _with_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAFORSEO_LOGIN", LOGIN)
    monkeypatch.setenv("DATAFORSEO_PASSWORD", PASSWORD)


class TestDataforseoBalance:
    def test_success_via_get_with_basic_auth(self, monkeypatch: pytest.MonkeyPatch):
        _with_creds(monkeypatch)
        client, requests = make_http([(200, balance_envelope())])
        assert dataforseo_balance(client) == {"balance": pytest.approx(87.4523), "note": None}
        assert len(requests) == 1
        assert requests[0].method == "GET"
        assert str(requests[0].url) == "https://api.dataforseo.com/v3/appendix/user_data"
        token = base64.b64encode(f"{LOGIN}:{PASSWORD}".encode()).decode()
        assert requests[0].headers["Authorization"] == f"Basic {token}"

    def test_true_zero_balance_stays_zero_not_none(self, monkeypatch: pytest.MonkeyPatch):
        _with_creds(monkeypatch)
        client, _ = make_http([(200, balance_envelope(balance=0))])
        out = dataforseo_balance(client)
        assert out["balance"] == 0.0
        assert out["note"] is None

    def test_env_unset_is_a_note_and_no_request(self):
        client, requests = make_http([(200, balance_envelope())])
        out = dataforseo_balance(client)
        assert out["balance"] is None
        assert "not set" in out["note"]
        assert requests == []  # no credentials -> no call attempted

    def test_transport_failure_never_raises(self, monkeypatch: pytest.MonkeyPatch):
        _with_creds(monkeypatch)

        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = httpx.Client(transport=httpx.MockTransport(boom))
        out = dataforseo_balance(client)
        assert out["balance"] is None
        assert "unreachable" in out["note"]

    def test_http_error_status(self, monkeypatch: pytest.MonkeyPatch):
        _with_creds(monkeypatch)
        client, _ = make_http([(503, "down")])
        out = dataforseo_balance(client)
        assert out["balance"] is None
        assert "HTTP 503" in out["note"]

    def test_non_json_body(self, monkeypatch: pytest.MonkeyPatch):
        _with_creds(monkeypatch)
        client, _ = make_http([(200, "<html>totally not json</html>")])
        out = dataforseo_balance(client)
        assert out["balance"] is None
        assert "non-JSON" in out["note"]

    @pytest.mark.parametrize(
        "envelope",
        [
            {"status_code": 40100, "status_message": "auth failed", "tasks": []},
            {"status_code": 20000, "tasks": []},
            {"status_code": 20000, "tasks": [{"status_code": 40401, "result": None}]},
            {"status_code": 20000, "tasks": [{"status_code": 20000, "result": None}]},
            {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{}]}]},
            {
                "status_code": 20000,
                "tasks": [{"status_code": 20000, "result": [{"money": {"balance": "12.3"}}]}],
            },
        ],
        ids=["api-error", "no-tasks", "task-error", "null-result", "no-money", "string-balance"],
    )
    def test_bad_envelope_is_honest_none(
        self, monkeypatch: pytest.MonkeyPatch, envelope: dict
    ):
        _with_creds(monkeypatch)
        client, _ = make_http([(200, envelope)])
        out = dataforseo_balance(client)
        assert out["balance"] is None
        assert out["note"] is not None


# --- DB fixtures -------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute("truncate cost_events restart identity")
        c.execute("truncate serp_snapshots, keyword_metrics cascade")
        yield c


@pytest.fixture()
def site(conn):
    org_id = conn.execute(
        "insert into orgs (name) values (%s) returning id", (f"spend-{uuid.uuid4().hex[:8]}",)
    ).fetchone()["id"]
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
        (org_id, f"client-{uuid.uuid4().hex[:8]}.ae"),
    ).fetchone()["id"]
    return {"org_id": org_id, "site_id": site_id}


def plant(
    conn,
    *,
    provider: str = "dataforseo",
    purpose: str = "serp_live",
    cost: float = 1.0,
    days_ago: int = 0,
    created_at: dt.datetime | None = None,
    units: dict | None = None,
) -> None:
    conn.execute(
        "insert into cost_events (provider, purpose, units, cost_cents, created_at) values"
        " (%s, %s, %s, %s, coalesce(%s, now() - make_interval(days => %s)))",
        (provider, purpose, Jsonb(units or {}), cost, created_at, days_ago),
    )


def _event_count(conn) -> int:
    return conn.execute("select count(*) as n from cost_events").fetchone()["n"]


# --- spend_rollup -------------------------------------------------------------------------


@requires_db
class TestSpendRollup:
    def test_empty_window_honest_zero(self, conn):
        assert spend_rollup(conn) == {
            "window_days": 30,
            "total_cents": 0.0,  # zero events = zero spent: an honest true-zero
            "by_provider": [],
            "by_purpose": [],
            "by_day": [],
            "last_event": None,
        }

    def test_rollup_math_ordering_and_last_event(self, conn):
        plant(conn, purpose="serp_live", cost=2.0, days_ago=5)
        plant(conn, purpose="labs_ranked_keywords", cost=1.0, days_ago=2)
        plant(conn, provider="anthropic", purpose="draft", cost=5.5, days_ago=1)
        plant(conn, purpose="serp_live", cost=0.5, days_ago=0, units={"query": "botox dubai"})
        plant(conn, purpose="serp_live", cost=100.0, days_ago=45)  # outside the window

        out = spend_rollup(conn, days=30)
        assert out["window_days"] == 30
        assert out["total_cents"] == pytest.approx(9.0)
        assert out["by_provider"] == [
            {"provider": "anthropic", "cost_cents": pytest.approx(5.5), "events": 1},
            {"provider": "dataforseo", "cost_cents": pytest.approx(3.5), "events": 3},
        ]
        assert out["by_purpose"] == [
            {
                "provider": "anthropic",
                "purpose": "draft",
                "cost_cents": pytest.approx(5.5),
                "events": 1,
            },
            {
                "provider": "dataforseo",
                "purpose": "serp_live",
                "cost_cents": pytest.approx(2.5),
                "events": 2,
            },
            {
                "provider": "dataforseo",
                "purpose": "labs_ranked_keywords",
                "cost_cents": pytest.approx(1.0),
                "events": 1,
            },
        ]
        today = conn.execute("select current_date as d").fetchone()["d"]
        assert out["by_day"] == [  # chronological
            {
                "date": today - dt.timedelta(days=5),
                "provider": "dataforseo",
                "cost_cents": pytest.approx(2.0),
            },
            {
                "date": today - dt.timedelta(days=2),
                "provider": "dataforseo",
                "cost_cents": pytest.approx(1.0),
            },
            {
                "date": today - dt.timedelta(days=1),
                "provider": "anthropic",
                "cost_cents": pytest.approx(5.5),
            },
            {"date": today, "provider": "dataforseo", "cost_cents": pytest.approx(0.5)},
        ]
        last = out["last_event"]
        assert last["provider"] == "dataforseo"
        assert last["purpose"] == "serp_live"
        assert last["cost_cents"] == pytest.approx(0.5)
        assert last["units"] == {"query": "botox dubai"}
        assert last["created_at"] is not None

    def test_window_days_param_narrows(self, conn):
        plant(conn, cost=2.0, days_ago=5)
        plant(conn, cost=1.0, days_ago=2)
        out = spend_rollup(conn, days=3)
        assert out["window_days"] == 3
        assert out["total_cents"] == pytest.approx(1.0)
        assert out["by_provider"] == [
            {"provider": "dataforseo", "cost_cents": pytest.approx(1.0), "events": 1}
        ]


# --- budget_state matrix --------------------------------------------------------------------


@requires_db
class TestBudgetState:
    def test_no_cap(self, conn):
        assert budget_state(conn, now=MARCH_10) == {
            "cap_cents": None,
            "spent_cents": 0.0,
            "projected_month_cents": None,  # no spend: a projection would be invented
            "exceeded": False,
            "note": "no cap configured",
        }
        require_dfs_budget(conn)  # no cap: never refuses

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_cap_is_no_cap(self, conn, monkeypatch: pytest.MonkeyPatch, raw: str):
        monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", raw)
        state = budget_state(conn, now=MARCH_10)
        assert state["cap_cents"] is None
        assert state["exceeded"] is False
        assert state["note"] == "no cap configured"

    def test_non_integer_cap_is_an_honest_note(self, conn, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "ten dollars")
        state = budget_state(conn, now=MARCH_10)
        assert state["cap_cents"] is None
        assert state["exceeded"] is False
        assert "not an integer" in state["note"]

    def test_under_cap_with_projection(self, conn, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "1000")
        plant(conn, cost=400.0, created_at=dt.datetime(2026, 3, 5, 9, 0, tzinfo=dt.UTC))
        plant(conn, cost=100.0, created_at=dt.datetime(2026, 3, 8, 9, 0, tzinfo=dt.UTC))
        state = budget_state(conn, now=MARCH_10)
        assert state["cap_cents"] == 1000
        assert state["spent_cents"] == pytest.approx(500.0)
        # 500 cents over 10 elapsed days of a 31-day month
        assert state["projected_month_cents"] == pytest.approx(500.0 / 10 * 31)
        assert state["exceeded"] is False
        assert state["note"] is None

    def test_over_cap(self, conn, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "100")
        plant(conn, cost=150.0, created_at=dt.datetime(2026, 3, 5, 9, 0, tzinfo=dt.UTC))
        state = budget_state(conn, now=MARCH_10)
        assert state["exceeded"] is True
        assert state["spent_cents"] == pytest.approx(150.0)

    def test_exact_cap_is_a_ceiling(self, conn, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "100")
        plant(conn, cost=100.0, created_at=dt.datetime(2026, 3, 5, 9, 0, tzinfo=dt.UTC))
        assert budget_state(conn, now=MARCH_10)["exceeded"] is True

    def test_scoped_to_calendar_month_and_provider(self, conn, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "100")
        # previous calendar month: out of scope
        plant(conn, cost=999.0, created_at=dt.datetime(2026, 2, 20, 9, 0, tzinfo=dt.UTC))
        # other provider: out of scope
        plant(
            conn,
            provider="anthropic",
            purpose="draft",
            cost=999.0,
            created_at=dt.datetime(2026, 3, 5, 9, 0, tzinfo=dt.UTC),
        )
        plant(conn, cost=40.0, created_at=dt.datetime(2026, 3, 5, 9, 0, tzinfo=dt.UTC))
        state = budget_state(conn, now=MARCH_10)
        assert state["spent_cents"] == pytest.approx(40.0)
        assert state["projected_month_cents"] == pytest.approx(40.0 / 10 * 31)
        assert state["exceeded"] is False


# --- require_dfs_budget ---------------------------------------------------------------------


@requires_db
class TestRequireDfsBudget:
    def test_passes_under_cap(self, conn, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "1000")
        plant(conn, cost=10.0)
        require_dfs_budget(conn)  # no raise

    def test_refuses_with_typed_non_retryable_exception(
        self, conn, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "100")
        plant(conn, cost=60.0)
        plant(conn, cost=40.0)
        with pytest.raises(BudgetExceeded) as err:
            require_dfs_budget(conn)
        exc = err.value
        assert exc.retryable is False  # dead after cheap no-spend retries, never infinite
        assert exc.cap_cents == 100
        assert exc.spent_cents == pytest.approx(100.0)
        assert "budget exceeded" in str(exc)
        assert "GM_DFS_MONTHLY_BUDGET_CENTS" in str(exc)


# --- serp purchase-path guard ---------------------------------------------------------------


SNAPSHOT_RESULTS = [
    {
        "rank": 1,
        "url": "https://x.ae/",
        "domain": "x.ae",
        "title": "t",
        "description": "",
        "type": "organic",
    }
]


def plant_snapshot(conn, site, query: str = "botox dubai", days_old: int = 0) -> None:
    conn.execute(
        "insert into serp_snapshots (org_id, site_id, query_norm, location, results, features,"
        " provider, cost_cents, depth, fetched_at)"
        " values (%s, %s, %s, 'United Arab Emirates', %s, %s, 'dataforseo', 0.2, 10,"
        " now() - make_interval(days => %s))",
        (site["org_id"], site["site_id"], query, Jsonb(SNAPSHOT_RESULTS), Jsonb([]), days_old),
    )


def plant_metric(conn, site, query: str = "kw one") -> None:
    conn.execute(
        "insert into keyword_metrics (org_id, site_id, query_norm, volume, cpc, competition)"
        " values (%s, %s, %s, 320, 1.5, 0.4)",
        (site["org_id"], site["site_id"], query),
    )


def make_dfs(
    responses: list[tuple[int, object]],
) -> tuple[serp_mod.DataForSeoClient, list[httpx.Request]]:
    http_client, requests = make_http(responses)
    return serp_mod.DataForSeoClient(login=LOGIN, password=PASSWORD, client=http_client), requests


def _exceed_budget(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """1-cent cap + 2 cents of dataforseo spend this month: every purchase must refuse."""
    monkeypatch.setenv("GM_DFS_MONTHLY_BUDGET_CENTS", "1")
    plant(conn, cost=2.0)


@requires_db
class TestSerpPurchaseGuard:
    def test_get_snapshot_cache_hit_stays_free(self, conn, site, monkeypatch: pytest.MonkeyPatch):
        _exceed_budget(conn, monkeypatch)
        plant_snapshot(conn, site)
        # client=None: a cache hit needs no client, no credentials, and no budget
        out = serp_mod.get_snapshot(conn, site["site_id"], "Botox  Dubai", client=None)
        assert out["fresh"] is False
        assert out["results"] == SNAPSHOT_RESULTS

    def test_get_snapshot_purchase_refused_before_any_http(
        self, conn, site, monkeypatch: pytest.MonkeyPatch
    ):
        _exceed_budget(conn, monkeypatch)
        client, requests = make_dfs([(500, "must never be reached")])
        with pytest.raises(BudgetExceeded) as err:
            serp_mod.get_snapshot(conn, site["site_id"], "new query", client=client)
        assert err.value.retryable is False
        assert requests == []  # refused BEFORE spending: zero HTTP
        n = conn.execute("select count(*) as n from serp_snapshots").fetchone()["n"]
        assert n == 0  # nothing stored
        assert _event_count(conn) == 1  # only the planted event: the refusal cost $0

    def test_get_snapshot_stale_row_is_a_purchase_and_refused(
        self, conn, site, monkeypatch: pytest.MonkeyPatch
    ):
        _exceed_budget(conn, monkeypatch)
        plant_snapshot(conn, site, days_old=8)  # past the 7-day TTL
        client, requests = make_dfs([(500, "must never be reached")])
        with pytest.raises(BudgetExceeded):
            serp_mod.get_snapshot(conn, site["site_id"], "botox dubai", client=client)
        assert requests == []

    def test_get_volumes_cached_rows_stay_free(self, conn, site, monkeypatch: pytest.MonkeyPatch):
        _exceed_budget(conn, monkeypatch)
        plant_metric(conn, site, "kw one")
        out = serp_mod.get_volumes(conn, site["site_id"], ["kw one"], client=None)
        assert out == {
            "kw one": {"volume": 320, "cpc": 1.5, "competition": pytest.approx(0.4)}
        }

    def test_get_volumes_miss_refused_before_any_http(
        self, conn, site, monkeypatch: pytest.MonkeyPatch
    ):
        _exceed_budget(conn, monkeypatch)
        plant_metric(conn, site, "kw one")
        client, requests = make_dfs([(500, "must never be reached")])
        with pytest.raises(BudgetExceeded):
            serp_mod.get_volumes(conn, site["site_id"], ["kw one", "kw two"], client=client)
        assert requests == []
        n = conn.execute("select count(*) as n from keyword_metrics").fetchone()["n"]
        assert n == 1  # no placeholder row was written for the refused miss
        assert _event_count(conn) == 1
