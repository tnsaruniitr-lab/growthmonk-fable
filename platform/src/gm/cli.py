"""Operator CLI for the Phase A proof engine.

Typical flow:
  gm db migrate
  gm org create "GrowthMonk"
  gm site add glowclinic.ae --brand-term "Glow Aesthetic"
  gm site add competitor-control.ae --control
  gm prompt add glowclinic.ae "best morpheus8 clinic in dubai"
  gm run start glowclinic.ae            # enqueue a sampling run
  gm worker --with-scheduler            # process jobs (leave running)
  gm schedule add glowclinic.ae --every-minutes 10080
  gm verdict glowclinic.ae --before <run,run,run> --after <run,run,run> --controls ctrl.ae
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid as uuid_mod
from pathlib import Path

import typer
import yaml
from psycopg.types.json import Jsonb

from gm import config, db
from gm.core import panel as panel_mod
from gm.core import schedules as schedules_mod
from gm.delivery import evidence
from gm.infra import jobs as jobs_mod
from gm.infra import scheduler as scheduler_mod
from gm.intel import sampler, variance

app = typer.Typer(help="GrowthMonk Fable — Phase A citation proof engine", no_args_is_help=True)
db_app = typer.Typer(help="Database operations", no_args_is_help=True)
org_app = typer.Typer(help="Org management", no_args_is_help=True)
site_app = typer.Typer(help="Site (treatment/control) management", no_args_is_help=True)
prompt_app = typer.Typer(help="Tracked prompt management", no_args_is_help=True)
run_app = typer.Typer(help="Citation runs", no_args_is_help=True)
schedule_app = typer.Typer(help="Recurring schedules", no_args_is_help=True)
lever_app = typer.Typer(help="Per-domain lever log (Gate-1 requirement)", no_args_is_help=True)
gsc_app = typer.Typer(help="Google Search Console connection + ingest", no_args_is_help=True)
vault_app = typer.Typer(help="Credential vault", no_args_is_help=True)
for name, sub in [
    ("db", db_app), ("org", org_app), ("site", site_app), ("prompt", prompt_app),
    ("run", run_app), ("schedule", schedule_app), ("lever", lever_app),
    ("gsc", gsc_app), ("vault", vault_app),
]:
    app.add_typer(sub, name=name)

ENGINES_DEFAULT = "openai,perplexity,gemini"


def _org(conn) -> dict:
    return panel_mod.get_default_org(conn)


@db_app.command("migrate")
def db_migrate():
    applied = db.run_migrations()
    typer.echo(f"applied: {applied or 'nothing (up to date)'}")


@org_app.command("create")
def org_create(name: str):
    with db.connect() as conn:
        org_id = panel_mod.create_org(conn, name)
        conn.commit()
    typer.echo(org_id)


def _echo_schedule_result(result: dict) -> None:
    """Render an ensure_default_schedules result: created/existing/skipped, honestly."""
    typer.echo(f"schedules created: {', '.join(result['created']) or 'none'}")
    if result["existing"]:
        typer.echo(f"schedules existing (untouched): {', '.join(result['existing'])}")
    for job_type, reason in result["skipped"].items():
        typer.echo(f"schedules skipped: {job_type} — {reason}")


@site_app.command("add")
def site_add(
    domain: str,
    control: bool = typer.Option(False, "--control", help="Mark as an untouched control domain"),
    brand_term: list[str] = typer.Option([], "--brand-term", help="Extra mention-match strings"),
    notes: str = typer.Option(None, "--notes"),
    no_schedules: bool = typer.Option(
        False, "--no-schedules", help="Skip wiring the default schedules (opt-out)"
    ),
):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site_id = panel_mod.add_site(
            conn, org["id"], domain, is_control=control, brand_terms=brand_term, notes=notes
        )
        schedule_result = None
        if control:
            note = "control site — default schedules not wired (controls stay untouched)"
        elif no_schedules:
            note = "default schedules skipped (--no-schedules)"
        else:
            note = None
            schedule_result = schedules_mod.ensure_default_schedules(
                conn, org_id=org["id"], site_id=site_id
            )
        conn.commit()
    typer.echo(site_id)
    if schedule_result is not None:
        _echo_schedule_result(schedule_result)
    else:
        typer.echo(note)


@site_app.command("backfill-schedules")
def site_backfill_schedules(
    domain: str = typer.Argument(None, help="One site; or pass --all for the whole org"),
    all_sites: bool = typer.Option(False, "--all", help="Every non-control site in the org"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report only; write nothing"),
):
    """Wire the default schedules onto existing sites (idempotent, tuned rows untouched)."""
    if bool(domain) == all_sites:
        raise typer.BadParameter("pass exactly one of <domain> or --all")
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        if all_sites:
            per_site = schedules_mod.backfill_default_schedules(
                conn, org_id=org["id"], dry_run=dry_run
            )["sites"]
        else:
            site = panel_mod.get_site(conn, org["id"], domain)
            if site["is_control"]:
                typer.echo(f"{site['domain_norm']} is a control site — no default schedules")
                raise typer.Exit(1)
            if dry_run:
                to_create, existing, skipped = schedules_mod._plan(conn, site["id"])
                result = {
                    "created": [jt for jt, _ in to_create],
                    "existing": existing,
                    "skipped": skipped,
                }
            else:
                result = schedules_mod.ensure_default_schedules(
                    conn, org_id=org["id"], site_id=str(site["id"])
                )
            per_site = {site["domain_norm"]: result}
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    if dry_run:
        typer.echo("DRY RUN — nothing written")
    if not per_site:
        typer.echo("no non-control sites in org")
    for domain_norm, result in per_site.items():
        typer.echo(f"{domain_norm}:")
        _echo_schedule_result(result)


@prompt_app.command("add")
def prompt_add(
    domain: str,
    text: str,
    engines: str = typer.Option(ENGINES_DEFAULT, help="Comma-separated engine list"),
):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        pid = panel_mod.add_prompt(conn, org["id"], str(site["id"]), text, engines.split(","))
        conn.commit()
    typer.echo(pid)


@run_app.command("start")
def run_start(
    domain: str,
    samples: int = typer.Option(3, help="Samples per prompt x engine"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use the fake engine (no spend)"),
):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        run_id = sampler.enqueue_run(
            conn, org["id"], str(site["id"]), samples_per_run=samples, dry_run=dry_run
        )
        conn.commit()
    typer.echo(run_id)


@run_app.command("list")
def run_list(domain: str):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        rows = conn.execute(
            "select id, status, scheduled_for, finished_at,"
            " (select count(*) from citation_results cr where cr.run_id = r.id"
            "   and cr.error is null) as ok_samples,"
            " (select count(*) from citation_results cr where cr.run_id = r.id"
            "   and cr.error is not null) as err_samples"
            " from citation_runs r where site_id=%s order by scheduled_for desc limit 30",
            (site["id"],),
        ).fetchall()
    for r in rows:
        typer.echo(
            f"{r['id']}  {r['status']:8}  {r['scheduled_for']:%Y-%m-%d %H:%M}"
            f"  ok={r['ok_samples']} err={r['err_samples']}"
        )


@schedule_app.command("add")
def schedule_add(
    domain: str,
    every_minutes: int = typer.Option(schedules_mod.WEEKLY, help="Default weekly"),
    samples: int = typer.Option(3),
):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        conn.execute(
            "insert into schedules (org_id, site_id, job_type, payload, every_minutes)"
            " values (%s, %s, 'scheduled_run', %s, %s)",
            (org["id"], site["id"], Jsonb({"samples_per_run": samples}), every_minutes),
        )
        conn.commit()
    typer.echo("scheduled")


@lever_app.command("add")
def lever_add(
    domain: str,
    description: str,
    lever_class: str = typer.Option(..., "--class", help="e.g. onsite_fix | directory_listing"),
    date: str = typer.Option(None, help="YYYY-MM-DD, default today"),
):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        conn.execute(
            "insert into levers (org_id, site_id, applied_at, lever_class, description)"
            " values (%s, %s, %s, %s, %s)",
            (org["id"], site["id"], date or dt.date.today().isoformat(), lever_class, description),
        )
        conn.commit()
    typer.echo("logged")


@app.command()
def worker(
    with_scheduler: bool = typer.Option(False, "--with-scheduler"),
):
    """Run the job worker (and optionally the catch-up scheduler) until Ctrl-C."""

    def _lazy_send_lead_card(ctx: jobs_mod.JobContext) -> None:
        from gm.delivery.leadcard import handle_send_lead_card

        handle_send_lead_card(ctx)

    def _lazy_refresh_competitor_profiles(ctx: jobs_mod.JobContext) -> None:
        from gm.intel.competitors import handle_refresh_competitor_profiles

        handle_refresh_competitor_profiles(ctx)

    def _lazy_discover_competitors(ctx: jobs_mod.JobContext) -> None:
        from gm.intel.discovery import handle_discover_competitors

        handle_discover_competitors(ctx)

    def _lazy_assemble_receipt_monthly(ctx: jobs_mod.JobContext) -> None:
        from gm.core.schedules import handle_assemble_receipt_monthly

        handle_assemble_receipt_monthly(ctx)

    def _lazy_pull_ads_daily(ctx: jobs_mod.JobContext) -> None:
        from gm.intel.ads_ingest import handle_pull_ads_daily

        handle_pull_ads_daily(ctx)

    def _handle_scheduled_run(ctx: jobs_mod.JobContext) -> None:
        payload = ctx.job.payload
        sampler.enqueue_run(
            ctx.conn, str(ctx.job.org_id), str(ctx.job.site_id),
            samples_per_run=int(payload.get("samples_per_run", 3)),
        )

    from gm.audit.compare import handle_compare_serp
    from gm.audit.group import handle_audit_group
    from gm.audit.pipeline import handle_audit_page
    from gm.content.briefs import handle_generate_brief
    from gm.content.fixcloser import handle_close_fixes
    from gm.delivery.receipts import handle_assemble_receipt, handle_compute_delta
    from gm.delivery.verify import handle_verify_publish
    from gm.delivery.wordpress import handle_publish
    from gm.intel.detectors import handle_compute_queue
    from gm.intel.gsc_ingest import handle_gsc_backfill, handle_gsc_daily, handle_gsc_initial
    from gm.intel.labs import handle_keyword_gap
    from gm.intel.rank_tracker import handle_track_serps

    handlers = {
        "sample_citations": sampler.handle_sample_citations,
        "scheduled_run": _handle_scheduled_run,
        "audit_page": handle_audit_page,
        "audit_group": handle_audit_group,
        "gsc_initial": handle_gsc_initial,
        "gsc_backfill": handle_gsc_backfill,
        "gsc_daily": handle_gsc_daily,
        "compute_queue": handle_compute_queue,
        "compare_serp": handle_compare_serp,
        "generate_brief": handle_generate_brief,
        "close_fixes": handle_close_fixes,
        "publish": handle_publish,
        "verify_publish": handle_verify_publish,
        "compute_delta": handle_compute_delta,
        "assemble_receipt": handle_assemble_receipt,
        "track_serps": handle_track_serps,
        "keyword_gap": handle_keyword_gap,
        "send_lead_card": _lazy_send_lead_card,
        "refresh_competitor_profiles": _lazy_refresh_competitor_profiles,
        "discover_competitors": _lazy_discover_competitors,
        "assemble_receipt_monthly": _lazy_assemble_receipt_monthly,
        "pull_ads_daily": _lazy_pull_ads_daily,
    }
    stop = threading.Event()
    if with_scheduler:
        t = threading.Thread(
            target=scheduler_mod.scheduler_loop, args=(stop,), daemon=True, name="scheduler"
        )
        t.start()
    typer.echo(f"worker up (handlers: {', '.join(handlers)})")
    jobs_mod.Worker(handlers).run_forever(stop_event=stop)


@app.command()
def audit(
    domain: str,
    url: str = typer.Option(None, help="Page URL (default: https://<domain>/)"),
    now: bool = typer.Option(False, "--now", help="Run inline instead of enqueueing"),
):
    """Enqueue (or run) a 103-check page audit."""
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        target = url or f"https://{site['domain_norm']}/"
        if now:
            from gm.audit.pipeline import run_page_audit
            from gm.infra.llm import LlmClient

            audit_id = run_page_audit(
                conn, org_id=org["id"], site_id=str(site["id"]), url=target, llm=LlmClient()
            )
            row = conn.execute(
                "select status, scores->>'overall_grade' as grade,"
                " scores->>'overall_score' as score, cost_cents from audits where id=%s",
                (audit_id,),
            ).fetchone()
            conn.commit()
            typer.echo(
                f"{audit_id}  {row['status']}  grade={row['grade']}"
                f" score={row['score']} cost=${float(row['cost_cents']) / 100:.2f}"
            )
        else:
            job_id = jobs_mod.enqueue(
                conn, type="audit_page", org_id=org["id"], site_id=str(site["id"]),
                payload={"url": target},
                idempotency_key=f"audit:{site['id']}:{target}:{dt.date.today().isoformat()}",
            )
            conn.commit()
            typer.echo(f"enqueued job {job_id} (audit_page {target})")


@vault_app.command("keygen")
def vault_keygen():
    """Generate the sealed-box keypair (run once; put keys in env per runbook)."""
    from gm.connections.vault import generate_keypair

    pub, priv = generate_keypair()
    typer.echo(f"GM_VAULT_PUBLIC_KEY={pub}")
    typer.echo(f"GM_VAULT_PRIVATE_KEY={priv}   # publisher/ingest workers ONLY; escrow a copy")


@gsc_app.command("connect")
def gsc_connect(
    domain: str,
    service_account: Path = typer.Option(..., help="Service-account JSON key file"),
    property: str = typer.Option(..., help='GSC property, e.g. "sc-domain:example.com"'),
):
    """Store GSC credentials (sealed) and verify access with a 1-row query."""
    import json as _json

    from gm.connections.gsc import GscClient
    from gm.connections.vault import store_connection

    creds = _json.loads(service_account.read_text())
    client = GscClient(creds, property)
    client.query(
        start_date=dt.date.today() - dt.timedelta(days=10),
        end_date=dt.date.today() - dt.timedelta(days=3),
        dimensions=["page"], row_limit=1,
    )  # raises GscAuthError if the service account lacks property access
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        store_connection(
            conn, org_id=org["id"], site_id=str(site["id"]), kind="gsc",
            credentials=creds, meta={"property": property},
        )
        conn.commit()
    typer.echo(f"connected + verified: {property}")


@gsc_app.command("pull")
def gsc_pull(domain: str):
    """Start the two-phase ingest (provisional queue in minutes; backfill in background)."""
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        job_id = jobs_mod.enqueue(
            conn, type="gsc_initial", org_id=org["id"], site_id=str(site["id"]),
            idempotency_key=f"gsc_initial:{site['id']}:{dt.date.today().isoformat()}",
        )
        conn.commit()
    typer.echo(f"enqueued job {job_id} (gsc_initial)")


@gsc_app.command("status")
def gsc_status(domain: str):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        c = conn.execute(
            "select status, last_ok_at, last_error, meta from connections"
            " where site_id=%s and kind='gsc'", (site["id"],),
        ).fetchone()
        cov = conn.execute(
            "select count(*) as days, count(*) filter (where final) as final_days,"
            " min(date) as oldest, max(date) as newest"
            " from gsc_ingest_log where site_id=%s", (site["id"],),
        ).fetchone()
    if not c:
        typer.echo("no GSC connection — run: gm gsc connect")
        raise typer.Exit(1)
    typer.echo(f"connection: {c['status']}  property={c['meta'].get('property')}")
    typer.echo(
        f"history coverage: {cov['days']} day(s) ({cov['final_days']} final)"
        f"  {cov['oldest'] or '—'} → {cov['newest'] or '—'}"
    )


@app.command()
def queue(domain: str, kind: str = typer.Option(None, help="Filter by detector kind")):
    """The operator queue: what to fix this week, ranked by clicks at stake."""
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        q = (
            "select kind, target, at_stake, status, last_seen from queue_items"
            " where site_id=%s and status='open'"
        )
        params: list = [site["id"]]
        if kind:
            q += " and kind=%s"
            params.append(kind)
        q += " order by coalesce((at_stake->>'est_clicks_gain')::float, 0) desc limit 30"
        rows = conn.execute(q, params).fetchall()
    if not rows:
        typer.echo("queue empty — run gm gsc pull / wait for compute_queue")
    for r in rows:
        gain = r["at_stake"].get("est_clicks_gain")
        vol = r["at_stake"].get("volume")
        stake = f"+{gain:>6} clicks/mo" if gain is not None else f"vol {vol or '?':>5}/mo"
        basis = r["at_stake"].get("basis", "?")
        tgt = r["target"].get("query") or r["target"].get("page") or ""
        typer.echo(f"{r['kind']:18} {stake} [{basis}]  {tgt[:70]}")


@site_app.command("set-author")
def site_set_author(
    domain: str,
    name: str = typer.Option(...),
    title: str = typer.Option(None),
    same_as: list[str] = typer.Option([], "--same-as", help="LinkedIn/profile URLs"),
    credentials: str = typer.Option(None),
):
    """Set the real author entity — the convergence-fix input for the fix-closer."""
    import json as _json

    author = {k: v for k, v in
              {"name": name, "title": title, "sameAs": same_as, "credentials": credentials}.items()
              if v}
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        conn.execute("update sites set author=%s where id=%s",
                     (_json.dumps(author), site["id"]))
        conn.commit()
    typer.echo("author set")


@app.command("close-fixes")
def close_fixes(
    domain: str,
    brief_id: str = typer.Option(..., help="Approved brief to execute"),
    kind: str = typer.Option("new"),
    now: bool = typer.Option(False, "--now"),
):
    """Run the fix-closer: brief → draft (convergence inputs enforced) → registry scorecard."""
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        row = conn.execute(
            "insert into content_items (org_id, site_id, brief_id, kind)"
            " values (%s,%s,%s,%s) returning id",
            (org["id"], site["id"], brief_id, kind),
        ).fetchone()
        ci_id = str(row["id"])
        if now:
            from gm.content.fixcloser import handle_close_fixes as run

            class _Ctx:
                def __init__(self, conn, job):
                    self.conn, self.job = conn, job

                def heartbeat(self):
                    pass

            class _Job:
                id = None
                org_id = org["id"]
                site_id = str(site["id"])
                payload = {"content_item_id": ci_id}

            run(_Ctx(conn, _Job()))
            d = conn.execute(
                "select d.version, a.scores->>'overall_grade' as grade,"
                " a.scores->>'overall_score' as score, d.human_todos"
                " from drafts d left join audits a on a.id = d.scorecard_audit_id"
                " where d.content_item_id=%s order by d.version desc limit 1", (ci_id,),
            ).fetchone()
            conn.commit()
            typer.echo(f"content_item {ci_id} draft v{d['version']}"
                       f" scorecard grade={d['grade']} score={d['score']}")
            for t in (d["human_todos"] or [])[:6]:
                typer.echo(f"  HUMAN: {t if isinstance(t, str) else t.get('note', t)}")
        else:
            jobs_mod.enqueue(conn, type="close_fixes", org_id=org["id"],
                             site_id=str(site["id"]),
                             payload={"content_item_id": ci_id},
                             idempotency_key=f"close:{ci_id}")
            conn.commit()
            typer.echo(f"content_item {ci_id} enqueued (close_fixes)")


@app.command("wp-connect")
def wp_connect(
    domain: str,
    base_url: str = typer.Option(...),
    username: str = typer.Option(...),
    app_password: str = typer.Option(..., prompt=True, hide_input=True),
):
    """Connect WordPress (Application Password) with least-privilege preflight."""
    from gm.delivery.wordpress import connect_wordpress

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        report = connect_wordpress(
            conn, org_id=org["id"], site_id=str(site["id"]),
            base_url=base_url, username=username, app_password=app_password,
        )
        conn.commit()
    typer.echo(f"preflight ok={report.get('ok')} role={report.get('role')}")
    for w in report.get("warnings", []):
        typer.echo(f"  WARN: {w}")


@app.command()
def publish(domain: str, content_item_id: str = typer.Option(...)):
    """Publish the latest draft to WordPress (draft-mode) + IndexNow + verify jobs."""
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        jobs_mod.enqueue(conn, type="publish", org_id=org["id"], site_id=str(site["id"]),
                         payload={"content_item_id": content_item_id},
                         idempotency_key=f"publish:{content_item_id}")
        conn.commit()
    typer.echo("publish enqueued")


@app.command()
def receipt(
    domain: str,
    period: str = typer.Option(None, help="YYYY-MM, default current month"),
    out: Path = typer.Option(None),
):
    """Assemble + render the monthly Delta Receipt."""
    from gm.audit.registry import load_registry
    from gm.delivery.receipts import assemble_site_receipt, render_receipt_html

    period = period or dt.date.today().strftime("%Y-%m")
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        rid = assemble_site_receipt(conn, site_id=str(site["id"]), period=period)
        row = conn.execute("select * from site_deltas where id=%s", (rid,)).fetchone()
        conn.commit()
    html = render_receipt_html(dict(site), row["payload"], checks_meta=load_registry().checks)
    out = out or config.repo_root() / "ops" / "receipts" / f"{period}-{site['domain_norm']}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    typer.echo(f"receipt {rid}\n{out}")


track_app = typer.Typer(help="Tracked queries (rank + AI Overview)", no_args_is_help=True)
app.add_typer(track_app, name="track")
lead_app = typer.Typer(help="Booked leads (the attribution denominator)", no_args_is_help=True)
app.add_typer(lead_app, name="lead")


@lead_app.command("add")
def lead_add(
    domain: str,
    source: str = typer.Option("manual", help="manual | call | booking_system"),
    notes: str = typer.Option(None),
    occurred: str = typer.Option(None, help="ISO timestamp, default now"),
):
    from gm.delivery.leadcard import add_lead

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        lid = add_lead(conn, org_id=org["id"], site_id=str(site["id"]), source=source,
                       occurred_at=occurred, notes=notes)
        conn.commit()
    typer.echo(lid)


@lead_app.command("list")
def lead_list(domain: str, days: int = typer.Option(28)):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        rows = conn.execute(
            "select occurred_at, source, notes,"
            " attribution->'referral'->>'source_url' as ref"
            " from booked_leads where site_id=%s"
            " and occurred_at > now() - make_interval(days => %s)"
            " order by occurred_at desc limit 100",
            (site["id"], days),
        ).fetchall()
        counts = conn.execute(
            "select source, count(*) c from booked_leads where site_id=%s"
            " and occurred_at > now() - make_interval(days => %s) group by source",
            (site["id"], days),
        ).fetchall()
    summary = ", ".join(f"{r['source']}={r['c']}" for r in counts) or "0 leads"
    typer.echo(f"last {days}d: {summary}")
    for r in rows[:30]:
        ref = f"  via {r['ref']}" if r["ref"] else ""
        typer.echo(f"{r['occurred_at']:%m-%d %H:%M}  {r['source']:14} {r['notes'] or ''}{ref}")


@lead_app.command("card")
def lead_card(domain: str, send: bool = typer.Option(False, "--send")):
    """Preview (or send) this week's WhatsApp trend card."""
    from gm.delivery.leadcard import build_card_text

    monday = dt.date.today() - dt.timedelta(days=dt.date.today().weekday())
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        text = build_card_text(conn, str(site["id"]), week_start=monday)
        if send:
            from gm.delivery.whatsapp import WabaClient

            wa = conn.execute(
                "select meta from connections where site_id=%s and kind='whatsapp'",
                (site["id"],),
            ).fetchone()
            if not wa:
                raise typer.Exit("no whatsapp connection — run gm wa-connect")
            WabaClient().send_text(wa["meta"]["recipient_wa_id"], text)
            typer.echo("sent ✓")
        conn.commit()
    typer.echo(text)


@app.command("wa-connect")
def wa_connect(
    domain: str,
    phone_number_id: str = typer.Option(..., help="WABA phone number id (webhook mapping)"),
    recipient: str = typer.Option(..., help="Buyer's wa_id to receive the weekly card"),
):
    """Map a WABA number to this site (token stays in env, never stored)."""
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        conn.execute(
            "insert into connections (org_id, site_id, kind, meta) values (%s,%s,'whatsapp',%s)"
            " on conflict (site_id, kind) do update set meta=excluded.meta, status='ok'",
            (org["id"], site["id"],
             Jsonb({"phone_number_id": phone_number_id, "recipient_wa_id": recipient})),
        )
        schedule_result = None
        if not site["is_control"]:
            schedule_result = schedules_mod.ensure_default_schedules(
                conn, org_id=org["id"], site_id=str(site["id"])
            )
        conn.commit()
    typer.echo("whatsapp connected")
    if schedule_result is not None:
        _echo_schedule_result(schedule_result)


# --- ads: read-only ad-platform connections (Phase D3, WP-G consumed by contract) -----------

ads_app = typer.Typer(
    help="Ad platform connections — read-only ROAS (google_ads / meta_ads)",
    no_args_is_help=True,
)
app.add_typer(ads_app, name="ads")

_ADS_CHANNELS = ("google_ads", "meta_ads")


@ads_app.command("connect")
def ads_connect(
    domain: str,
    channel: str = typer.Option(..., help="google_ads | meta_ads"),
    customer_id: str = typer.Option(None, help="google_ads: client customer id"),
    login_customer_id: str = typer.Option(None, help="google_ads: manager (login) customer id"),
    act_id: str = typer.Option(None, help="meta_ads: ad account id (digits, no act_ prefix)"),
):
    """Register a read-only ads connection (tokens stay in env, never stored)."""
    if channel not in _ADS_CHANNELS:
        raise typer.BadParameter(f"channel must be one of: {', '.join(_ADS_CHANNELS)}")
    if channel == "google_ads":
        if not customer_id or not login_customer_id:
            raise typer.BadParameter("google_ads needs --customer-id and --login-customer-id")
        meta = {"customer_id": customer_id, "login_customer_id": login_customer_id}
    else:
        if not act_id:
            raise typer.BadParameter("meta_ads needs --act-id")
        meta = {"act_id": act_id}
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        conn.execute(
            "insert into connections (org_id, site_id, kind, meta) values (%s,%s,%s,%s)"
            " on conflict (site_id, kind) do update"
            " set meta=excluded.meta, status='ok', last_error=null",
            (org["id"], site["id"], channel, Jsonb(meta)),
        )
        schedule_result = None
        if not site["is_control"]:
            schedule_result = schedules_mod.ensure_default_schedules(
                conn, org_id=org["id"], site_id=str(site["id"])
            )
        conn.commit()
    typer.echo(f"{channel} connected (read-only; tokens stay in env, never stored)")
    if schedule_result is not None:
        _echo_schedule_result(schedule_result)


@ads_app.command("pull")
def ads_pull(
    domain: str,
    days: int = typer.Option(7, help="Trailing-window re-pull width (platforms restate)"),
    now: bool = typer.Option(False, "--now", help="Run inline instead of enqueueing"),
):
    """Pull daily spend rows for every ok ads connection (one-off; DAILY schedule automates)."""
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        if now:
            from gm.intel.ads_ingest import pull_ads_daily

            result = pull_ads_daily(conn, org_id=org["id"], site_id=str(site["id"]), days=days)
            conn.commit()
            typer.echo(result)
        else:
            job_id = jobs_mod.enqueue(
                conn, type="pull_ads_daily", org_id=org["id"], site_id=str(site["id"]),
                payload={"days": days},
                idempotency_key=f"pull_ads_daily:{site['id']}:{dt.date.today().isoformat()}",
            )
            conn.commit()
            typer.echo(f"enqueued job {job_id} (pull_ads_daily days={days})")


@ads_app.command("status")
def ads_status(domain: str):
    """Ads connections + ads_daily coverage; honest empty states throughout."""
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        conns = conn.execute(
            "select kind, status, last_ok_at, last_error, meta from connections"
            " where site_id=%s and kind = any(%s) order by kind",
            (site["id"], list(_ADS_CHANNELS)),
        ).fetchall()
        # migration 011 creates ads_daily; tolerate a pre-011 database honestly
        has_ads_daily = conn.execute(
            "select to_regclass('ads_daily') is not null as ok"
        ).fetchone()["ok"]
        coverage = {}
        if has_ads_daily and conns:
            coverage = {
                r["channel"]: r
                for r in conn.execute(
                    "select channel, count(distinct date) as days, min(date) as oldest,"
                    " max(date) as newest, max(pulled_at) as last_pull"
                    " from ads_daily where site_id=%s group by channel",
                    (site["id"],),
                ).fetchall()
            }
        conn.rollback()  # read-only
    if not conns:
        typer.echo("no ads connections — run: gm ads connect")
        raise typer.Exit(1)
    for c in conns:
        ids = ", ".join(f"{k}={v}" for k, v in sorted(c["meta"].items()))
        typer.echo(f"{c['kind']}: {c['status']}  {ids}")
        if c["last_error"]:
            typer.echo(f"  last_error: {c['last_error']}")
        cov = coverage.get(c["kind"])
        if cov:
            typer.echo(
                f"  coverage: {cov['days']} day(s)  {cov['oldest']} → {cov['newest']}"
                f"  (last pull {cov['last_pull']:%Y-%m-%d %H:%M})"
            )
        else:
            typer.echo("  coverage: no daily rows pulled yet")


def _check_depth_flag(depth: int | None) -> None:
    if depth is not None and depth not in (10, 100):
        raise typer.BadParameter("depth must be 10 or 100")


@track_app.command("add")
def track_add(
    domain: str,
    query: str,
    target_page: str = typer.Option(None),
    depth: int = typer.Option(
        None, "--depth", help="SERP depth 10 (default) or 100 (opt-in, ~2x provider cost)"
    ),
):
    from gm.intel.rank_tracker import add_tracked_query

    _check_depth_flag(depth)
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        tid = add_tracked_query(
            conn, org["id"], str(site["id"]), query, target_page=target_page, serp_depth=depth
        )
        conn.commit()
    typer.echo(tid)


@track_app.command("set-depth")
def track_set_depth(
    domain: str,
    query: str,
    depth: int = typer.Option(..., "--depth", help="SERP depth: 10 or 100"),
):
    """Change the SERP depth of an already-tracked query (10 default, 100 opt-in)."""
    from gm.intel.rank_tracker import set_query_depth

    _check_depth_flag(depth)
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        updated = set_query_depth(conn, str(site["id"]), query, depth)
        conn.commit()
    if not updated:
        typer.echo(f"query not tracked for {site['domain_norm']}: {query!r} — run: gm track add")
        raise typer.Exit(1)
    typer.echo(f"depth={depth} set for {query!r}")


@track_app.command("run")
def track_run(domain: str):
    """Snapshot all tracked queries now (weekly schedule does this automatically)."""
    from gm.intel.rank_tracker import track_site

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        result = track_site(conn, org_id=org["id"], site_id=str(site["id"]))
        conn.commit()
    typer.echo(result)


@track_app.command("list")
def track_list(domain: str):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        rows = conn.execute(
            "select tq.query_norm, rh.rank, rh.aio_present, rh.aio_cited, rh.checked_on"
            " from tracked_queries tq left join lateral ("
            "   select * from rank_history rh where rh.site_id=tq.site_id"
            "   and rh.query_norm=tq.query_norm order by checked_on desc limit 1) rh on true"
            " where tq.site_id=%s and tq.active order by tq.query_norm",
            (site["id"],),
        ).fetchall()
    for r in rows:
        rank = f"#{r['rank']}" if r["rank"] else "—"
        aio = "AIO✓cited" if r["aio_cited"] else ("AIO present" if r["aio_present"] else "no AIO")
        typer.echo(f"{rank:>5}  {aio:12}  {r['query_norm']}  ({r['checked_on'] or 'never'})")


@site_app.command("set-competitors")
def site_set_competitors(domain: str, competitors: str = typer.Option(..., help="comma-sep hosts")):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        conn.execute("update sites set competitor_domains=%s where id=%s",
                     ([c.strip() for c in competitors.split(",") if c.strip()], site["id"]))
        conn.commit()
    typer.echo("competitors set")


# --- competitor intelligence (Phase D2, WP-D) ---------------------------------------------

competitors_app = typer.Typer(
    help="Competitor intelligence: profiles, discovery, competitive position", no_args_is_help=True
)
app.add_typer(competitors_app, name="competitors")

# The D2 refresh cadence — sourced from gm.core.schedules, the only copy (D3 COMMON).
MONTHLY_MINUTES = schedules_mod.MONTHLY


def _fmt(value, none: str = "—") -> str:
    return none if value is None else str(value)


def _profile_line(profile: dict | None) -> str:
    """One-line render of latest_profile output. None = never fetched -> the
    empty-state law's "no data yet"; a stored NULLs row renders dashes with its
    check date (we looked, the provider had nothing — different from never)."""
    if profile is None:
        return "no data yet"
    traffic = profile["est_traffic"]
    movers = profile.get("movers") or {}
    mover_bits = " ".join(
        f"{key}={movers[key]}"
        for key in ("new", "up", "down", "lost")
        if movers.get(key) is not None
    )
    line = (
        f"kw={_fmt(profile['total_keywords'])} top10={_fmt(profile['top10_keywords'])}"
        f" traffic={_fmt(round(traffic) if traffic is not None else None)}"
        f" ({profile['checked_on']})"
    )
    return f"{line}  {mover_bits}" if mover_bits else line


@competitors_app.command("list")
def competitors_list(domain: str):
    """Configured competitors + latest monthly profile each + open candidate count."""
    from gm.intel.competitors import latest_profile

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        configured = [c for c in (site["competitor_domains"] or []) if c]
        profiles = [(comp, latest_profile(conn, site["id"], comp)) for comp in configured]
        open_candidates = conn.execute(
            "select count(*) as n from queue_items where site_id=%s"
            " and kind='competitor_candidate' and status='open'",
            (site["id"],),
        ).fetchone()["n"]
        conn.rollback()  # read-only
    if not configured:
        typer.echo(
            "no competitors configured — run: gm competitors discover"
            " / gm site set-competitors"
        )
    for comp, profile in profiles:
        typer.echo(f"{comp:40} {_profile_line(profile)}")
    suffix = "  — review: gm competitors confirm/dismiss" if open_candidates else ""
    typer.echo(f"open candidates: {open_candidates}{suffix}")


@competitors_app.command("discover")
def competitors_discover(
    domain: str,
    limit: int = typer.Option(10, help="Candidates to queue (refused above the max of 10)"),
    now: bool = typer.Option(False, "--now", help="Run inline instead of enqueueing"),
):
    """Discover competitor candidates via Labs; queue them for confirm/dismiss review."""
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        if now:
            from gm.intel.discovery import discover_competitors

            result = discover_competitors(
                conn, org_id=org["id"], site_id=str(site["id"]), limit=limit
            )
            conn.commit()
            typer.echo(
                f"candidates={result['candidates']} queued={result['queued']}"
                f" cost=${result['cost_cents'] / 100:.4f}"
            )
            if result["note"]:
                typer.echo(f"note: {result['note']}")
        else:
            job_id = jobs_mod.enqueue(
                conn, type="discover_competitors", org_id=org["id"], site_id=str(site["id"]),
                payload={"site_id": str(site["id"]), "limit": limit},
                idempotency_key=(
                    f"discover_competitors:{site['id']}:{dt.date.today().isoformat()}"
                ),
            )
            conn.commit()
            typer.echo(f"enqueued job {job_id} (discover_competitors limit={limit})")


@competitors_app.command("confirm")
def competitors_confirm(domain: str, host: str):
    """Confirm a discovery candidate: append to sites.competitor_domains."""
    from gm.intel.discovery import confirm_candidate

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        appended = confirm_candidate(conn, site_id=site["id"], domain=host)
        conn.commit()
    typer.echo(
        f"{host}: added to competitor_domains" if appended
        else f"{host}: already configured (candidate actioned)"
    )


@competitors_app.command("dismiss")
def competitors_dismiss(
    domain: str,
    host: str,
    snooze_days: int = typer.Option(90, help="Discovery re-queues only after this elapses"),
):
    """Dismiss a discovery candidate (snoozed; actioned/done rows are never touched)."""
    from gm.intel.discovery import dismiss_candidate

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        dismissed = dismiss_candidate(
            conn, site_id=site["id"], domain=host, snooze_days=snooze_days
        )
        conn.commit()
    if not dismissed:
        typer.echo(f"{host}: no open candidate to dismiss")
        raise typer.Exit(1)
    typer.echo(f"{host}: dismissed (snoozed {snooze_days}d)")


@competitors_app.command("refresh")
def competitors_refresh(
    domain: str,
    now: bool = typer.Option(False, "--now", help="Run inline instead of enqueueing"),
    monthly: bool = typer.Option(
        False, "--monthly", help="Insert the monthly schedules row instead of a one-off job"
    ),
):
    """Refresh monthly competitor profiles (one-off job by default)."""
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        if monthly:
            existing = conn.execute(
                "select id from schedules where site_id=%s"
                " and job_type='refresh_competitor_profiles' and enabled",
                (site["id"],),
            ).fetchone()
            if existing:
                typer.echo("monthly refresh already scheduled")
            else:
                conn.execute(
                    "insert into schedules (org_id, site_id, job_type, payload, every_minutes)"
                    " values (%s, %s, 'refresh_competitor_profiles', %s, %s)",
                    (org["id"], site["id"], Jsonb({}), MONTHLY_MINUTES),
                )
                typer.echo(f"monthly refresh scheduled (every {MONTHLY_MINUTES} minutes)")
        if now:
            from gm.intel.competitors import refresh_competitor_profiles

            result = refresh_competitor_profiles(conn, org_id=org["id"], site_id=str(site["id"]))
            typer.echo(
                f"refreshed={result['refreshed']} cached={result['cached']}"
                f" empty={result['empty']} cost=${result['cost_cents'] / 100:.4f}"
            )
            if result["note"]:
                typer.echo(f"note: {result['note']}")
        elif not monthly:
            job_id = jobs_mod.enqueue(
                conn, type="refresh_competitor_profiles", org_id=org["id"],
                site_id=str(site["id"]), payload={},
                idempotency_key=(
                    f"refresh_competitor_profiles:{site['id']}:{dt.date.today().isoformat()}"
                ),
            )
            typer.echo(f"enqueued job {job_id} (refresh_competitor_profiles)")
        conn.commit()


def _position_lines(position: dict) -> list[str]:
    """competitive_position payload -> CLI text; empty-state notes verbatim,
    None values as em-dashes (never fake zeros), has_data=False as 'no data yet'."""
    you = position["you"]
    lines = [f"window {position['window']['since']} → {position['window']['until']}"]
    if position.get("note"):
        lines.append(f"note: {position['note']}")
    lines.append(
        f"you: {you['domain']}  tracked={you['tracked_queries']}"
        f" top3={_fmt(you['rank_top3'])} top10={_fmt(you['rank_top10'])}"
        f" aio={_fmt(you['aio_citations'])}"
        f" audit={_fmt(you['audit_median'])} (n={you['audit_n']})"
    )
    for comp in position["competitors"]:
        if not comp["has_data"]:
            lines.append(f"  {comp['domain']}: no data yet")
            continue
        line = (
            f"  {comp['domain']}: top3={_fmt(comp['rank_top3'])}"
            f" top10={_fmt(comp['rank_top10'])} aio={_fmt(comp['aio_citations'])}"
            f" audit={_fmt(comp['audit_median'])} (n={comp['audit_n']})"
        )
        if comp["profile"] is not None:
            line += f"  profile: {_profile_line(comp['profile'])}"
        lines.append(line)
    share = position["feature_share"]
    lines.append(f"feature share ({share['queries']} tracked queries):")
    if share.get("note"):
        lines.append(f"  note: {share['note']}")
    for week in share["weeks"]:
        for ftype, bucket in week["features"].items():
            if not bucket["present"]:
                continue
            comps = " ".join(f"{host}={n}" for host, n in sorted(bucket["competitors"].items()))
            lines.append(
                f"  {week['week_start']} {ftype}: present={bucket['present']}"
                f" you={bucket['you']}" + (f" {comps}" if comps else "")
                + f" other={bucket['other']} unattributed={bucket['unattributed']}"
            )
    return lines


@competitors_app.command("position")
def competitors_position(
    domain: str,
    month: str = typer.Option(None, "--month", help="YYYY-MM, default current month"),
):
    """Competitive position + feature share for a month, as text (zero spend)."""
    from gm.delivery.receipts import period_bounds
    from gm.intel.feature_share import competitive_position

    month = month or dt.date.today().strftime("%Y-%m")
    start, end = period_bounds(month)
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        position = competitive_position(
            conn, str(site["id"]), since=start, until=end - dt.timedelta(days=1)
        )
        conn.rollback()  # read-only
    for line in _position_lines(position):
        typer.echo(line)


@app.command()
def gap(domain: str):
    """Run the keyword-gap detector vs the site's tracked competitors."""
    from gm.intel.labs import keyword_gap

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        result = keyword_gap(conn, org_id=org["id"], site_id=str(site["id"]))
        conn.commit()
    typer.echo(result)


@app.command()
def serp(domain: str, query: str):
    """Pull (or reuse) a SERP snapshot; shows top-10 + PAA + the client's rank."""
    from gm.intel.serp import DataForSeoClient, get_snapshot

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        snap = get_snapshot(conn, str(site["id"]), query, client=DataForSeoClient())
        conn.commit()
    marker = site["domain_norm"]
    for r in snap["results"][:10]:
        me = "  ◀ you" if marker in (r.get("domain") or "") else ""
        line = f"#{r.get('rank'):>2}  {(r.get('domain') or '')[:40]:41} {r.get('title', '')[:50]}"
        typer.echo(line + me)
    paa = [f for f in snap.get("features", []) if f.get("type") == "people_also_ask"]
    for q in (paa[0].get("questions", []) if paa else [])[:4]:
        typer.echo(f"PAA: {q}")


@app.command()
def compare(
    domain: str,
    query: str = typer.Option(...),
    page: str = typer.Option(None, help="Client page URL (defaults to homepage)"),
):
    """Audit the competitors above you for a query; show the precise gaps."""
    from gm.audit.compare import run_comparison
    from gm.infra.llm import LlmClient

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        cid = run_comparison(
            conn, org_id=org["id"], site_id=str(site["id"]), query=query,
            llm=LlmClient(), client_page_url=page,
        )
        row = conn.execute("select * from serp_comparisons where id=%s", (cid,)).fetchone()
        conn.commit()
    typer.echo(f"comparison {cid}")
    for g in (row["gaps"] or [])[:10]:
        typer.echo(
            f"GAP {g.get('check_id'):6} {g.get('name','')[:50]:51}"
            f" you={g.get('client_status')} comps_passing={g.get('competitors_passing')}"
        )


@app.command()
def brief(
    domain: str,
    query: str = typer.Option(...),
    kind: str = typer.Option("new", help="new | refresh"),
    page: str = typer.Option(None),
    out: Path = typer.Option(None, help="Write markdown here (default ops/briefs/)"),
):
    """Generate a content brief: SERP + PAA + volumes + competitor gaps + required fixes."""
    from gm.audit.registry import load_registry
    from gm.content.briefs import generate_brief, render_brief_markdown
    from gm.infra.llm import LlmClient

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        try:
            llm = LlmClient()
        except Exception:
            llm = None  # deterministic sections still assemble
        bid = generate_brief(
            conn, org_id=org["id"], site_id=str(site["id"]), query=query,
            llm=llm, kind=kind, page_url=page,
        )
        row = conn.execute("select * from briefs where id=%s", (bid,)).fetchone()
        conn.commit()
    md = render_brief_markdown(row, checks_meta=load_registry().checks)
    out = out or config.repo_root() / "ops" / "briefs" / (
        f"{dt.date.today().isoformat()}-{site['domain_norm']}-{'-'.join(query.split()[:5])}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    typer.echo(f"brief {bid}\n{out}")


@app.command("audit-group")
def audit_group(
    domain: str,
    urls: str = typer.Option(..., help="Comma-separated location page URLs"),
    now: bool = typer.Option(False, "--now", help="Run inline instead of enqueueing"),
    dry_run: bool = typer.Option(False, "--dry-run", help="FakeLlm classification (no spend)"),
):
    """Group autopsy: audit N location pages, roll up sitewide vs per-location fixes."""
    url_list = [u.strip() for u in urls.split(",") if u.strip()]
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        if now:
            from gm.audit.group import persist_group_summary, run_group_audit

            if dry_run:
                from gm.infra.llm import FakeLlm

                llm = FakeLlm(["[]"])
            else:
                from gm.infra.llm import LlmClient

                llm = LlmClient()
            result = run_group_audit(
                conn, org_id=org["id"], site_id=str(site["id"]), urls=url_list, llm=llm
            )
            group_id = persist_group_summary(
                conn, org_id=org["id"], site_id=str(site["id"]), assembled=result
            )
            conn.commit()
            typer.echo(f"group audit {group_id}: {len(url_list)} locations")
        else:
            job_id = jobs_mod.enqueue(
                conn, type="audit_group", org_id=org["id"], site_id=str(site["id"]),
                payload={"urls": url_list},
                idempotency_key=f"group:{site['id']}:{dt.date.today().isoformat()}",
            )
            conn.commit()
            typer.echo(f"enqueued job {job_id} (audit_group, {len(url_list)} urls)")


@app.command()
def share(audit_id: str, ttl_days: int = typer.Option(60)):
    """Create a share token for an audit report; prints the /r/<token> path."""
    from gm.delivery.shares import create_share

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        token = create_share(conn, org["id"], audit_id, ttl_days=ttl_days)
        conn.commit()
    typer.echo(f"/r/{token}")


@app.command()
def serve(host: str = "0.0.0.0", port: int = typer.Option(None, help="Default: $PORT or 8080")):
    """Run the API (share pages + admin)."""
    import os

    import uvicorn

    resolved = port if port is not None else int(os.environ.get("PORT", "8080"))
    uvicorn.run("gm.api:app", host=host, port=resolved)


@app.command()
def status():
    with db.connect() as conn:
        jrows = conn.execute(
            "select status, count(*) as c from jobs group by status order by status"
        ).fetchall()
        crow = conn.execute(
            "select coalesce(sum(cost_cents),0) as cents from cost_events"
            " where created_at > now() - interval '30 days'"
        ).fetchone()
    typer.echo("jobs: " + (", ".join(f"{r['status']}={r['c']}" for r in jrows) or "none"))
    typer.echo(f"llm spend last 30d: ${float(crow['cents']) / 100:.2f}")


@app.command()
def verdict(
    domain: str,
    before: str = typer.Option(..., help="Comma-separated run ids (baseline window)"),
    after: str = typer.Option(..., help="Comma-separated run ids (treatment window)"),
    controls: str = typer.Option("", help="Comma-separated control domains with same-window runs"),
    out: Path = typer.Option(None, help="Output path (default ops/evidence/<date>-<domain>.md)"),
):
    """Compute the pre-registered gate verdict and export the evidence log."""
    thresholds_path = config.repo_root() / "ops" / "gate1-thresholds.yaml"
    thresholds = yaml.safe_load(thresholds_path.read_text())
    before_ids, after_ids = before.split(","), after.split(",")

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        sid = str(site["id"])
        b_stats = sampler.window_stats(conn, before_ids)
        a_stats = sampler.window_stats(conn, after_ids)

        prompts = {
            str(p["id"]): p["prompt"] for p in panel_mod.active_prompts(conn, sid)
        }
        windows = {}
        for pid in prompts:
            b = b_stats.get(pid, {"k": 0, "n": 0})
            a = a_stats.get(pid, {"k": 0, "n": 0})
            windows[pid] = (
                variance.Window(k=b["k"], n=b["n"]),
                variance.Window(k=a["k"], n=a["n"]),
            )

        control_gains: list[float] = []
        control_rows: list[dict] = []
        for cdomain in [c for c in controls.split(",") if c.strip()]:
            csite = panel_mod.get_site(conn, org["id"], cdomain)
            cb = _pool(sampler.window_stats(conn, _site_runs(conn, csite["id"], before_ids)))
            ca = _pool(sampler.window_stats(conn, _site_runs(conn, csite["id"], after_ids)))
            gain = _rate(ca) - _rate(cb)
            control_gains.append(gain)
            control_rows.append({"domain": csite["domain_norm"], "gain": round(gain, 3)})

        gate = variance.gate_verdict(windows, control_gains, thresholds)
        details_by_id = {d.prompt_id: d for d in gate.details}

        report = {
            "site": {"domain": site["domain_norm"], "is_control": site["is_control"]},
            "window_before": {"label": "baseline", "run_ids": before_ids},
            "window_after": {"label": "post-fix", "run_ids": after_ids},
            "prompts": [
                {
                    "prompt_text": text,
                    "engine_breakdown": _engine_breakdown(
                        b_stats.get(pid, {}), a_stats.get(pid, {})
                    ),
                    "pooled": _verdict_dict(details_by_id.get(pid)),
                }
                for pid, text in prompts.items()
            ],
            "gate": {
                "status": gate.status,
                "moved_prompt_ids": gate.moved_prompt_ids,
                "control_mean_drift": gate.control_mean_drift,
                "reasons": gate.reasons,
            },
            "controls": control_rows,
            "levers": [
                dict(r)
                for r in conn.execute(
                    "select applied_at, lever_class, description from levers"
                    " where site_id=%s order by applied_at",
                    (sid,),
                ).fetchall()
            ],
            "panel_hash": panel_mod.panel_hash(panel_mod.freeze_panel(conn, sid)),
            "raw_ref_count": sampler.raw_ref_count(conn, before_ids + after_ids),
            "thresholds_status": thresholds.get("status", "UNKNOWN"),
            "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }

    md = evidence.export_markdown(report)
    out = out or config.repo_root() / "ops" / "evidence" / (
        f"{dt.date.today().isoformat()}-{site['domain_norm']}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    typer.echo(f"{gate.status} — moved {len(gate.moved_prompt_ids)} prompt(s); evidence: {out}")


def _engine_breakdown(b_stat: dict, a_stat: dict) -> dict:
    empty = {"k": 0, "n": 0}
    b_eng, a_eng = b_stat.get("engines", {}), a_stat.get("engines", {})
    return {
        e: {"before": b_eng.get(e, empty), "after": a_eng.get(e, empty)}
        for e in sorted(set(b_eng) | set(a_eng))
    }


def _site_runs(conn, site_id, run_ids: list[str]) -> list[str]:
    ids = [uuid_mod.UUID(r) for r in run_ids]
    rows = conn.execute(
        "select id from citation_runs where site_id=%s and id = any(%s)", (site_id, ids)
    ).fetchall()
    return [str(r["id"]) for r in rows]


def _pool(stats: dict) -> dict:
    return {"k": sum(s["k"] for s in stats.values()), "n": sum(s["n"] for s in stats.values())}


def _rate(kn: dict) -> float:
    return (kn["k"] / kn["n"]) if kn["n"] else 0.0


def _verdict_dict(v) -> dict:
    if v is None:
        return {}
    return {
        "before": {"k": v.before.k, "n": v.before.n},
        "after": {"k": v.after.k, "n": v.after.n},
        "gain": round(v.gain, 3),
        "ci_before": [round(x, 3) for x in v.ci_before],
        "ci_after": [round(x, 3) for x in v.ci_after],
        "sufficient": v.sufficient,
    }


if __name__ == "__main__":
    app()
