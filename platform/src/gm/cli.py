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


@site_app.command("add")
def site_add(
    domain: str,
    control: bool = typer.Option(False, "--control", help="Mark as an untouched control domain"),
    brand_term: list[str] = typer.Option([], "--brand-term", help="Extra mention-match strings"),
    notes: str = typer.Option(None, "--notes"),
):
    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site_id = panel_mod.add_site(
            conn, org["id"], domain, is_control=control, brand_terms=brand_term, notes=notes
        )
        conn.commit()
    typer.echo(site_id)


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
    every_minutes: int = typer.Option(10080, help="Default weekly"),
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
        basis = r["at_stake"].get("basis", "?")
        tgt = r["target"].get("query") or r["target"].get("page") or ""
        typer.echo(f"{r['kind']:18} +{gain or '?':>6} clicks/mo [{basis}]  {tgt[:70]}")


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
        conn.commit()
    typer.echo("whatsapp connected")


@track_app.command("add")
def track_add(domain: str, query: str, target_page: str = typer.Option(None)):
    from gm.intel.rank_tracker import add_tracked_query

    with db.connect() as conn:
        org = _org(conn)
        db.set_org(conn, org["id"])
        site = panel_mod.get_site(conn, org["id"], domain)
        tid = add_tracked_query(conn, org["id"], str(site["id"]), query, target_page=target_page)
        conn.commit()
    typer.echo(tid)


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
