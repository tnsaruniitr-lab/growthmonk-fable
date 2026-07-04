"""Tests for the refusal log (Phase D4, WP-J — roadmap D.7 tripwire ledger).

The reason-validation test is pure (validation fires before any DB work). CRUD +
stats tests need migration 012 and skip cleanly when DATABASE_URL is unset.
No network.
"""

import datetime as dt
import os
import uuid

import pytest

from gm import db
from gm.core import refusals

needs_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


# --- pure: reason validation happens BEFORE the insert ----------------------------------


def test_bad_reason_raises_typed_valueerror_before_any_db_work():
    # conn=None proves validation precedes any database call.
    with pytest.raises(ValueError, match="diy/price/timing/trust/other"):
        refusals.add_refusal(None, org_id=None, prospect="Smile Clinic", reason="busy")
    with pytest.raises(ValueError, match="got ''"):
        refusals.add_refusal(None, org_id=None, prospect="Smile Clinic", reason="")
    # case-sensitive: the check constraint is, so the validator must be too
    with pytest.raises(ValueError):
        refusals.add_refusal(None, org_id=None, prospect="Smile Clinic", reason="DIY")


def test_reasons_tuple_matches_migration_check_list():
    assert refusals.REASONS == ("diy", "price", "timing", "trust", "other")


# --- DB fixtures ------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    with db.connect(autocommit=True) as c:
        c.execute("truncate refusals")
        yield c


@pytest.fixture()
def org_id(conn):
    return conn.execute(
        "insert into orgs (name) values (%s) returning id", (f"ref-{uuid.uuid4().hex[:8]}",)
    ).fetchone()["id"]


# --- CRUD -------------------------------------------------------------------------------


@needs_db
def test_add_defaults_and_returned_id(conn, org_id):
    rid = refusals.add_refusal(conn, org_id=org_id, prospect="Smile Clinic", reason="diy")
    assert uuid.UUID(rid)  # a real row id
    row = conn.execute("select * from refusals where id = %s", (rid,)).fetchone()
    assert row["prospect"] == "Smile Clinic"
    assert row["source"] == "agency_pitch"  # default channel
    assert row["reason"] == "diy"
    assert row["notes"] is None
    assert row["refused_at"] == dt.date.today()  # column default current_date


@needs_db
def test_add_explicit_fields_roundtrip(conn, org_id):
    when = dt.date.today() - dt.timedelta(days=10)
    rid = refusals.add_refusal(
        conn,
        org_id=org_id,
        prospect="Bright Dental",
        reason="price",
        source="referral_call",
        notes="wants half the retainer",
        refused_at=when,
    )
    row = conn.execute("select * from refusals where id = %s", (rid,)).fetchone()
    assert row["source"] == "referral_call"
    assert row["notes"] == "wants half the retainer"
    assert row["refused_at"] == when


@needs_db
def test_list_newest_first_and_window(conn, org_id):
    today = dt.date.today()
    refusals.add_refusal(
        conn, org_id=org_id, prospect="old", reason="other",
        refused_at=today - dt.timedelta(days=170),
    )
    refusals.add_refusal(conn, org_id=org_id, prospect="new", reason="diy", refused_at=today)
    refusals.add_refusal(
        conn, org_id=org_id, prospect="mid", reason="price",
        refused_at=today - dt.timedelta(days=30),
    )
    rows = refusals.list_refusals(conn, org_id=org_id)
    assert [r["prospect"] for r in rows] == ["new", "mid", "old"]
    # narrower window drops the old row
    rows = refusals.list_refusals(conn, org_id=org_id, days=90)
    assert [r["prospect"] for r in rows] == ["new", "mid"]


@needs_db
def test_window_edge_is_inclusive(conn, org_id):
    today = dt.date.today()
    refusals.add_refusal(
        conn, org_id=org_id, prospect="edge", reason="diy",
        refused_at=today - dt.timedelta(days=180),
    )
    refusals.add_refusal(
        conn, org_id=org_id, prospect="beyond", reason="diy",
        refused_at=today - dt.timedelta(days=181),
    )
    rows = refusals.list_refusals(conn, org_id=org_id, days=180)
    assert [r["prospect"] for r in rows] == ["edge"]  # exactly-180-days-old is in; 181 is out
    stats = refusals.refusal_stats(conn, org_id=org_id, days=180)
    assert stats["total"] == 1


@needs_db
def test_org_scoping(conn, org_id):
    other = conn.execute(
        "insert into orgs (name) values (%s) returning id", (f"ref-{uuid.uuid4().hex[:8]}",)
    ).fetchone()["id"]
    refusals.add_refusal(conn, org_id=other, prospect="theirs", reason="trust")
    assert refusals.list_refusals(conn, org_id=org_id) == []
    assert refusals.refusal_stats(conn, org_id=org_id)["total"] == 0


@needs_db
def test_bad_reason_leaves_no_row_behind(conn, org_id):
    with pytest.raises(ValueError):
        refusals.add_refusal(conn, org_id=org_id, prospect="x", reason="ghosted")
    assert conn.execute("select count(*) as n from refusals").fetchone()["n"] == 0


# --- stats honesty ----------------------------------------------------------------------


@needs_db
def test_stats_empty_ledger_is_none_share_not_zero(conn, org_id):
    stats = refusals.refusal_stats(conn, org_id=org_id)
    assert stats["total"] == 0
    # every check-list reason present, all true zeros
    assert stats["by_reason"] == {"diy": 0, "price": 0, "timing": 0, "trust": 0, "other": 0}
    # the tripwire must never read "no refusals logged" as "0% DIY"
    assert stats["diy_share"] is None


@needs_db
def test_stats_share_math(conn, org_id):
    for prospect, reason in [
        ("a", "diy"), ("b", "diy"), ("c", "price"), ("d", "other"),
    ]:
        refusals.add_refusal(conn, org_id=org_id, prospect=prospect, reason=reason)
    stats = refusals.refusal_stats(conn, org_id=org_id)
    assert stats["total"] == 4
    assert stats["by_reason"] == {"diy": 2, "price": 1, "timing": 0, "trust": 0, "other": 1}
    assert stats["diy_share"] == pytest.approx(0.5)


@needs_db
def test_stats_window_excludes_old_rows(conn, org_id):
    today = dt.date.today()
    refusals.add_refusal(conn, org_id=org_id, prospect="new", reason="price", refused_at=today)
    refusals.add_refusal(
        conn, org_id=org_id, prospect="ancient", reason="diy",
        refused_at=today - dt.timedelta(days=200),
    )
    stats = refusals.refusal_stats(conn, org_id=org_id, days=180)
    assert stats["total"] == 1
    assert stats["by_reason"]["diy"] == 0
    assert stats["diy_share"] == 0.0  # a true zero: refusals exist, none said DIY
