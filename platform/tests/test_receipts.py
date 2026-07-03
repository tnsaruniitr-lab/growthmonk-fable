"""Tests for gm.delivery.receipts (Delta Receipt v1, Phase C wave 3).

Pure math (windows, periods, Wilson wiring) and renderer goldens run
everywhere; DB-backed tests (content deltas, site rollup, handlers) skip
cleanly when DATABASE_URL is unset. ZERO network anywhere.
"""

import datetime as dt
import os
import uuid
from types import SimpleNamespace

import pytest

from gm.delivery import receipts
from gm.delivery.evidence import CLAIM_CEILING
from gm.intel.variance import wilson

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

PUB = dt.datetime(2026, 6, 1, 10, 0, tzinfo=dt.UTC)  # publish pivot used by DB tests


# --- pure window / period math -----------------------------------------------------------


def test_delta_windows_lag_offset():
    b_start, b_end, a_start, a_end = receipts.delta_windows(dt.date(2026, 6, 1))
    # before: 28 days ending the day before publish — no lag correction needed
    assert (b_start, b_end) == (dt.date(2026, 5, 4), dt.date(2026, 5, 31))
    # after: starts GSC_LAG_DAYS after publish, 28 days long
    assert (a_start, a_end) == (dt.date(2026, 6, 4), dt.date(2026, 7, 1))
    assert (b_end - b_start).days + 1 == 28
    assert (a_end - a_start).days + 1 == 28
    assert (a_start - dt.date(2026, 6, 1)).days == receipts.GSC_LAG_DAYS


def test_period_bounds_and_rollover():
    assert receipts.period_bounds("2026-06") == (dt.date(2026, 6, 1), dt.date(2026, 7, 1))
    assert receipts.period_bounds("2026-12") == (dt.date(2026, 12, 1), dt.date(2027, 1, 1))
    with pytest.raises(ValueError, match="YYYY-MM"):
        receipts.period_bounds("June 2026")
    with pytest.raises(ValueError, match="YYYY-MM"):
        receipts.period_bounds("2026-13")


def test_prior_period():
    assert receipts.prior_period("2026-06") == "2026-05"
    assert receipts.prior_period("2026-01") == "2025-12"


def test_citation_entry_wilson_wiring():
    entry = receipts.citation_entry("p1", "best clinic", before=(1, 9), after=(7, 9))
    assert entry["before"] == {"k": 1, "n": 9}
    assert entry["after"] == {"k": 7, "n": 9}
    assert entry["gain"] == pytest.approx(7 / 9 - 1 / 9, abs=1e-4)
    assert entry["ci_before"] == [round(v, 4) for v in wilson(1, 9)]
    assert entry["ci_after"] == [round(v, 4) for v in wilson(7, 9)]
    # n=0 window: wilson's honest (0, 1) interval, not a fake tight one
    empty = receipts.citation_entry("p2", "x", before=(0, 0), after=(2, 3))
    assert empty["ci_before"] == [0.0, 1.0]


def test_url_variants_trailing_slash():
    got = receipts._url_variants({"https://ex.com/a", "https://ex.com/b/", ""})
    assert set(got) == {
        "https://ex.com/a", "https://ex.com/a/", "https://ex.com/b", "https://ex.com/b/"
    }


# --- renderer goldens (pure) ----------------------------------------------------------------


def _payload(**over) -> dict:
    base = {
        "period": "2026-06",
        "prior_period": "2026-05",
        "audits": {
            "run": 2,
            "movement": {
                "first": {"score": 60, "grade": "D", "at": "2026-06-05"},
                "last": {"score": 72, "grade": "C", "at": "2026-06-25"},
                "change": 12.0,
            },
        },
        "fix_log": {
            "levers": [{"applied_at": "2026-06-15", "lever_class": "schema",
                        "description": "Added LocalBusiness JSON-LD"}],
            "published": [{"url": "https://ex.com/page", "kind": "refresh",
                           "published_at": "2026-06-10"}],
        },
        "content": [{
            "content_item_id": "ci-1",
            "url": "https://ex.com/page",
            "findings": {
                "resolved": ["C1"],
                "regressed": ["C3"],
                "non_comparable": [{"check_id": "C2", "before_version": 1,
                                    "after_version": 2}],
                "summary": "Score up 12 (60 -> 72). 1 resolved, 1 regressed.",
            },
            "gsc_before": {"clicks": 8, "impressions": 150, "position": 6.67},
            "gsc_after": {"clicks": 10, "impressions": 100, "position": 5.0},
        }],
        "citations": {
            "prompts": [receipts.citation_entry("p1", "best clinic dubai", (1, 9), (7, 9))],
            "controls": {"sites": [{"domain": "ctrl.com", "before": {"k": 1, "n": 4},
                                    "after": {"k": 1, "n": 4}, "gain": 0.0}],
                         "mean_abs_drift": 0.0},
        },
        "queue": {"opened": 3, "actions": [{"status": "actioned", "n": 2}]},
        "spend": {"total_cents": 123.4,
                  "by_provider": [{"provider": "openai", "cents": 123.4}]},
        "gsc": {"connected": True},
    }
    base.update(over)
    return base


SITE = {"domain_norm": "ex.com"}


def test_render_claim_ceiling_and_beta():
    html = receipts.render_receipt_html(SITE, _payload())
    assert f"Claim ceiling: {CLAIM_CEILING}." in html
    assert "BETA" in html
    assert "named in 7/9 runs" in html
    assert "was 1/9" in html
    assert "+12" in html  # movement stamp
    assert "Delta Receipt" in html


def test_render_no_script_ever_with_hostile_inputs():
    hostile = "<script>alert(1)</script>"
    payload = _payload()
    payload["fix_log"]["levers"][0]["description"] = hostile
    payload["citations"]["prompts"][0]["prompt"] = hostile
    payload["content"][0]["url"] = hostile
    html = receipts.render_receipt_html({"domain_norm": hostile}, payload)
    assert "<script" not in html
    assert "&lt;script&gt;" in html


def test_render_comparable_only_per_adr13():
    html = receipts.render_receipt_html(SITE, _payload(),
                                        checks_meta={"C1": {"name": "Title present"}})
    assert "<code>C1</code>" in html  # comparable, resolved
    assert "Title present" in html  # checks_meta name lookup
    assert "<code>C3</code>" in html  # comparable, regressed
    assert "<code>C2</code>" not in html  # version changed -> never listed as movement
    assert "not comparable" in html  # ...but honestly noted


def test_render_gsc_honest_absence_line():
    html = receipts.render_receipt_html(SITE, _payload(gsc={"connected": False}))
    assert "No GSC connection" in html
    # connected but windows empty -> different honest line, still no fake zeros
    payload = _payload()
    payload["content"][0]["gsc_before"] = {}
    payload["content"][0]["gsc_after"] = {}
    html2 = receipts.render_receipt_html(SITE, payload)
    assert "no finalized GSC data" in html2


def test_render_no_movement_stamp_inconclusive():
    payload = _payload(audits={"run": 0, "movement": {"first": None, "last": None,
                                                      "change": None}})
    html = receipts.render_receipt_html(SITE, payload)
    assert "stamp inconclusive" in html
    assert "no data" in html


def test_render_findings_skipped_noted():
    payload = _payload()
    payload["content"][0]["findings"] = {"skipped": True,
                                         "note": "no pre-publish page audit"}
    html = receipts.render_receipt_html(SITE, payload)
    assert "Findings diff skipped: no pre-publish page audit." in html


# --- DB fixtures ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute(
            "truncate orgs, sites, pages, page_url_history, audits, audit_findings,"
            " content_items, drafts, publish_events, verify_events, content_deltas,"
            " site_deltas, levers, queue_items, tracked_prompts, citation_runs,"
            " citation_results, connections, cost_events, jobs restart identity cascade"
        )
        c.execute("truncate gsc_daily, gsc_ingest_log, gsc_page_daily, gsc_window_agg")
        yield c


def _org(conn) -> str:
    return conn.execute("insert into orgs (name) values ('t') returning id").fetchone()["id"]


def _site(conn, org, domain="ex.com", *, control=False) -> str:
    return conn.execute(
        "insert into sites (org_id, domain_norm, is_control) values (%s, %s, %s) returning id",
        (org, domain, control),
    ).fetchone()["id"]


def _page(conn, org, site, url_norm) -> str:
    return conn.execute(
        "insert into pages (org_id, site_id, url_norm) values (%s, %s, %s) returning id",
        (org, site, url_norm),
    ).fetchone()["id"]


def _content_item(conn, org, site, page=None, *, kind="refresh", status="published") -> str:
    return conn.execute(
        "insert into content_items (org_id, site_id, page_id, kind, status)"
        " values (%s, %s, %s, %s, %s) returning id",
        (org, site, page, kind, status),
    ).fetchone()["id"]


def _publish(conn, org, item, url, at=PUB) -> str:
    return conn.execute(
        "insert into publish_events (org_id, content_item_id, target, url, published_at)"
        " values (%s, %s, 'wordpress', %s, %s) returning id",
        (org, item, url, at),
    ).fetchone()["id"]


def _gsc(conn, site, day, page, clicks, imps, pos, *, final=True):
    from gm.intel import gsc_ingest

    gsc_ingest.ensure_partition(conn, day)
    conn.execute(
        "insert into gsc_daily (site_id, date, search_type, page, query, clicks,"
        " impressions, ctr, position) values (%s, %s, 'web', %s, 'q', %s, %s, 0, %s)",
        (site, day, page, clicks, imps, pos),
    )
    conn.execute(
        "insert into gsc_ingest_log (site_id, date, search_type, rows, final)"
        " values (%s, %s, 'web', 1, %s)"
        " on conflict (site_id, date, search_type) do update set final = excluded.final",
        (site, day, final),
    )


def _audit(conn, org, site, page, finished, score, findings, *, gate_state="ok",
           draft_id=None, url="https://ex.com/page") -> str:
    from psycopg.types.json import Jsonb

    aid = conn.execute(
        "insert into audits (org_id, site_id, page_id, draft_id, url, registry_version,"
        " status, gate_state, scores, finished_at)"
        " values (%s, %s, %s, %s, %s, 'r1', 'done', %s, %s, %s) returning id",
        (org, site, page, draft_id, url, gate_state,
         Jsonb({"overall_score": score, "overall_grade": "X"}), finished),
    ).fetchone()["id"]
    for check_id, version, status in findings:
        conn.execute(
            "insert into audit_findings (org_id, audit_id, check_id, check_version,"
            " status, badge) values (%s, %s, %s, %s, %s, 'static_rule')",
            (org, aid, check_id, version, status),
        )
    return aid


class FakeCtx:
    """Just enough of jobs.JobContext for the handlers."""

    def __init__(self, conn, site_id, payload=None, org_id=None):
        self.conn = conn
        self.job = SimpleNamespace(id=1, site_id=site_id, org_id=org_id, payload=payload or {})

    def heartbeat(self):
        pass


# --- compute_content_delta ---------------------------------------------------------------


@requires_db
class TestComputeContentDelta:
    def _plant(self, conn):
        org = _org(conn)
        site = _site(conn, org)
        page = _page(conn, org, site, "https://ex.com/page")
        conn.execute(
            "insert into page_url_history (org_id, page_id, url_norm) values (%s, %s, %s)",
            (org, page, "https://ex.com/old"),
        )
        item = _content_item(conn, org, site, page)
        _publish(conn, org, item, "https://EX.com/page#frag")  # canonicalizes to url_norm
        return org, site, page, item

    def test_windows_gsc_union_and_findings(self, conn):
        org, site, page, item = self._plant(conn)
        # before window (05-04..05-31): old URL (via page_url_history) + current URL
        _gsc(conn, site, dt.date(2026, 5, 10), "https://ex.com/old", 5, 100, 8.0)
        _gsc(conn, site, dt.date(2026, 5, 11), "https://ex.com/page", 3, 50, 4.0)
        # inside the 3-day lag (after window starts 06-04): final but must NOT count
        _gsc(conn, site, dt.date(2026, 6, 2), "https://ex.com/page", 99, 990, 1.0)
        # after window: one final day counts, one non-final day must NOT count
        _gsc(conn, site, dt.date(2026, 6, 10), "https://ex.com/page", 10, 100, 5.0)
        _gsc(conn, site, dt.date(2026, 6, 5), "https://ex.com/page", 99, 990, 1.0,
             final=False)
        before = _audit(conn, org, site, page, dt.datetime(2026, 5, 20, tzinfo=dt.UTC), 60,
                        [("C1", 1, "fail"), ("C2", 1, "fail"), ("C3", 1, "pass")])
        after = _audit(conn, org, site, page, dt.datetime(2026, 6, 10, tzinfo=dt.UTC), 72,
                       [("C1", 1, "pass"), ("C2", 2, "fail"), ("C3", 1, "fail")])
        # a later draft scorecard must never be picked as the after-audit
        _audit(conn, org, site, page, dt.datetime(2026, 6, 20, tzinfo=dt.UTC), 5,
               [("C9", 1, "fail")], gate_state="draft", draft_id=uuid.uuid4())

        delta_id = receipts.compute_content_delta(conn, content_item_id=item)
        row = conn.execute("select * from content_deltas where id = %s",
                           (delta_id,)).fetchone()

        assert row["window_start"] == dt.date(2026, 5, 4)
        assert row["window_end"] == dt.date(2026, 7, 1)
        gb, ga = row["gsc_before"], row["gsc_after"]
        assert gb["clicks"] == 8  # old + new url_norm unioned
        assert gb["impressions"] == 150
        assert gb["position"] == pytest.approx((100 * 8.0 + 50 * 4.0) / 150, abs=0.01)
        assert gb["final_days"] == 2
        assert ga["clicks"] == 10  # lag day and non-final day both excluded
        assert ga["impressions"] == 100

        diff = row["findings_diff"]
        assert diff["resolved"] == ["C1"]
        assert diff["regressed"] == ["C3"]
        assert [nc["check_id"] for nc in diff["non_comparable"]] == ["C2"]
        assert diff["score_delta"]["change"] == 12.0
        assert row["before_audit_id"] == before
        assert row["after_audit_id"] == after  # not the draft scorecard
        assert diff["after_audit_id"] == str(after)

        # idempotent recompute: same row, refreshed in place
        assert receipts.compute_content_delta(conn, content_item_id=item) == delta_id
        n = conn.execute("select count(*) as n from content_deltas").fetchone()["n"]
        assert n == 1

    def test_empty_gsc_and_absent_audits_are_noted_not_faked(self, conn):
        org = _org(conn)
        site = _site(conn, org, "bare.com")
        page = _page(conn, org, site, "https://bare.com/p")
        item = _content_item(conn, org, site, page)
        _publish(conn, org, item, "https://bare.com/p")

        delta_id = receipts.compute_content_delta(conn, content_item_id=item)
        row = conn.execute("select * from content_deltas where id = %s",
                           (delta_id,)).fetchone()
        assert row["gsc_before"] == {}  # honest empty, never zeros
        assert row["gsc_after"] == {}
        diff = row["findings_diff"]
        assert diff["skipped"] is True
        assert "no pre-publish page audit" in diff["note"]
        assert "no post-publish page audit" in diff["note"]
        assert row["before_audit_id"] is None and row["after_audit_id"] is None

    def test_no_publish_event_fails_fast(self, conn):
        org = _org(conn)
        site = _site(conn, org)
        item = _content_item(conn, org, site)
        with pytest.raises(ValueError, match="no publish event"):
            receipts.compute_content_delta(conn, content_item_id=item)


# --- assemble_site_receipt -------------------------------------------------------------------


@requires_db
class TestAssembleSiteReceipt:
    PERIOD = "2026-06"

    def _plant(self, conn):
        from psycopg.types.json import Jsonb

        org = _org(conn)
        site = _site(conn, org)
        control = _site(conn, org, "ctrl.com", control=True)
        page = _page(conn, org, site, "https://ex.com/page")

        # audits: two in-period page audits; competitor reference + prior-month excluded
        _audit(conn, org, site, page, dt.datetime(2026, 6, 5, tzinfo=dt.UTC), 60, [])
        _audit(conn, org, site, page, dt.datetime(2026, 6, 25, tzinfo=dt.UTC), 72, [])
        _audit(conn, org, site, page, dt.datetime(2026, 6, 15, tzinfo=dt.UTC), 99, [],
               gate_state="competitor_reference", url="https://comp.com/x")
        _audit(conn, org, site, page, dt.datetime(2026, 5, 20, tzinfo=dt.UTC), 40, [])

        conn.execute(
            "insert into levers (org_id, site_id, applied_at, lever_class, description)"
            " values (%s, %s, '2026-06-15', 'schema', 'Added JSON-LD')",
            (org, site),
        )
        item = _content_item(conn, org, site, page)
        pub = _publish(conn, org, item, "https://ex.com/page",
                       dt.datetime(2026, 6, 10, tzinfo=dt.UTC))
        conn.execute(
            "insert into content_deltas (org_id, content_item_id, publish_event_id,"
            " window_start, window_end, gsc_before, gsc_after, findings_diff, created_at)"
            " values (%s, %s, %s, '2026-05-13', '2026-07-10', %s, %s, %s,"
            " '2026-06-16T00:00:00Z')",
            (org, item, pub, Jsonb({"clicks": 1}), Jsonb({"clicks": 5}),
             Jsonb({"resolved": ["C1"], "regressed": [], "non_comparable": []})),
        )

        # citations: treatment prompt 1/3 prior -> 2/3 current (+1 errored sample excluded)
        def add_samples(s, cited_flags, when, start_index=0):
            prompt = conn.execute(
                "insert into tracked_prompts (org_id, site_id, prompt, prompt_hash, engines)"
                " values (%s, %s, 'best clinic', md5(random()::text), '{openai}')"
                " returning id",
                (org, s),
            ).fetchone()["id"]
            run = conn.execute(
                "insert into citation_runs (org_id, site_id, panel) values (%s, %s, %s)"
                " returning id",
                (org, s, Jsonb([])),
            ).fetchone()["id"]
            for i, cited in enumerate(cited_flags):
                conn.execute(
                    "insert into citation_results (org_id, run_id, prompt_id, engine,"
                    " sample_index, sampled_at, cited, error)"
                    " values (%s, %s, %s, 'openai', %s, %s, %s, %s)",
                    (org, run, prompt, start_index + i, when,
                     bool(cited), None if cited is not None else "boom"),
                )
            return prompt

        prompt = add_samples(site, [True, False, False],
                             dt.datetime(2026, 5, 15, tzinfo=dt.UTC))
        # current period on the SAME prompt: new run, cited 2/3 + 1 error row
        run2 = conn.execute(
            "insert into citation_runs (org_id, site_id, panel) values (%s, %s, %s)"
            " returning id", (org, site, Jsonb([])),
        ).fetchone()["id"]
        for i, (cited, err) in enumerate([(True, None), (True, None), (False, None),
                                          (False, "boom")]):
            conn.execute(
                "insert into citation_results (org_id, run_id, prompt_id, engine,"
                " sample_index, sampled_at, cited, error)"
                " values (%s, %s, %s, 'openai', %s, '2026-06-15T00:00:00Z', %s, %s)",
                (org, run2, prompt, i, cited, err),
            )
        # control site: 0/2 prior -> 2/2 current => gain 1.0
        add_samples(control, [False, False], dt.datetime(2026, 5, 15, tzinfo=dt.UTC))
        add_samples(control, [True, True], dt.datetime(2026, 6, 15, tzinfo=dt.UTC),
                    start_index=10)

        conn.execute(
            "insert into queue_items (org_id, site_id, kind, target_hash, status,"
            " first_seen, last_seen) values"
            " (%s, %s, 'decay', 'h1', 'open', '2026-06-02T00:00:00Z', '2026-06-02T00:00:00Z'),"
            " (%s, %s, 'decay', 'h2', 'actioned', '2026-05-01T00:00:00Z',"
            "  '2026-06-20T00:00:00Z')",
            (org, site, org, site),
        )
        job_id = conn.execute(
            "insert into jobs (type, org_id, site_id) values ('audit_page', %s, %s)"
            " returning id", (org, site),
        ).fetchone()["id"]
        conn.execute(
            "insert into cost_events (org_id, job_id, provider, purpose, cost_cents,"
            " created_at) values (%s, %s, 'openai', 'audit', 123.4, '2026-06-05T00:00:00Z'),"
            " (%s, null, 'openai', 'org_misc', 999, '2026-06-05T00:00:00Z')",
            (org, job_id, org),
        )
        conn.execute(
            "insert into connections (org_id, site_id, kind, status) values"
            " (%s, %s, 'gsc', 'ok')", (org, site),
        )
        return org, site

    def test_rollup_payload(self, conn):
        org, site = self._plant(conn)
        rid = receipts.assemble_site_receipt(conn, site_id=site, period=self.PERIOD)
        row = conn.execute("select * from site_deltas where id = %s", (rid,)).fetchone()
        assert row["period"] == self.PERIOD
        p = row["payload"]

        assert p["audits"]["run"] == 2  # competitor ref + prior-month excluded
        mv = p["audits"]["movement"]
        assert mv["first"]["score"] == 60 and mv["last"]["score"] == 72
        assert mv["change"] == 12.0

        assert len(p["fix_log"]["levers"]) == 1
        assert len(p["fix_log"]["published"]) == 1
        assert p["content"][0]["findings"]["resolved"] == ["C1"]
        assert p["content"][0]["gsc_after"] == {"clicks": 5}

        (entry,) = p["citations"]["prompts"]
        assert entry["before"] == {"k": 1, "n": 3}
        assert entry["after"] == {"k": 2, "n": 3}  # errored sample excluded
        assert entry["ci_after"] == [round(v, 4) for v in wilson(2, 3)]

        (ctrl,) = p["citations"]["controls"]["sites"]
        assert ctrl["domain"] == "ctrl.com"
        assert ctrl["gain"] == 1.0
        assert p["citations"]["controls"]["mean_abs_drift"] == 1.0

        assert p["queue"]["opened"] == 1  # only the item first seen in June
        assert p["queue"]["actions"] == [{"status": "actioned", "n": 1}]
        assert p["spend"]["total_cents"] == 123.4  # org-wide unlinked event excluded
        assert p["spend"]["by_provider"] == [{"provider": "openai", "cents": 123.4}]
        assert p["gsc"]["connected"] is True

        # upsert idempotency: same (site, period) row refreshed in place
        assert receipts.assemble_site_receipt(conn, site_id=site,
                                              period=self.PERIOD) == rid
        n = conn.execute("select count(*) as n from site_deltas").fetchone()["n"]
        assert n == 1

    def test_render_from_assembled_payload(self, conn):
        org, site = self._plant(conn)
        rid = receipts.assemble_site_receipt(conn, site_id=site, period=self.PERIOD)
        payload = conn.execute("select payload from site_deltas where id = %s",
                               (rid,)).fetchone()["payload"]
        site_row = conn.execute("select * from sites where id = %s", (site,)).fetchone()
        html = receipts.render_receipt_html(site_row, payload)
        assert f"Claim ceiling: {CLAIM_CEILING}." in html
        assert "BETA" in html
        assert "named in 2/3 runs" in html
        assert "ctrl.com" in html
        assert "<script" not in html

    def test_unknown_site_fails_fast(self, conn):
        with pytest.raises(ValueError, match="not found"):
            receipts.assemble_site_receipt(conn, site_id=uuid.uuid4(), period=self.PERIOD)


# --- job handlers -----------------------------------------------------------------------------


@requires_db
class TestHandlers:
    def test_handle_compute_delta_marks_measured(self, conn):
        org = _org(conn)
        site = _site(conn, org)
        page = _page(conn, org, site, "https://ex.com/p")
        item = _content_item(conn, org, site, page, status="verified")
        _publish(conn, org, item, "https://ex.com/p")
        receipts.handle_compute_delta(
            FakeCtx(conn, site, payload={"content_item_id": str(item)}, org_id=org)
        )
        n = conn.execute("select count(*) as n from content_deltas").fetchone()["n"]
        assert n == 1
        status = conn.execute("select status from content_items where id = %s",
                              (item,)).fetchone()["status"]
        assert status == "measured"

    def test_handle_compute_delta_requires_payload(self, conn):
        with pytest.raises(ValueError, match="content_item_id"):
            receipts.handle_compute_delta(FakeCtx(conn, None))

    def test_handle_assemble_receipt(self, conn):
        org = _org(conn)
        site = _site(conn, org)
        receipts.handle_assemble_receipt(
            FakeCtx(conn, site, payload={"period": "2026-06"}, org_id=org)
        )
        row = conn.execute("select * from site_deltas where site_id = %s",
                           (site,)).fetchone()
        assert row["period"] == "2026-06"
        assert row["payload"]["audits"]["run"] == 0

    def test_handle_assemble_receipt_requires_period(self, conn):
        with pytest.raises(ValueError, match="period"):
            receipts.handle_assemble_receipt(FakeCtx(conn, uuid.uuid4()))
