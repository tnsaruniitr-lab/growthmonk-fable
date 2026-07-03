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
for name, sub in [
    ("db", db_app), ("org", org_app), ("site", site_app), ("prompt", prompt_app),
    ("run", run_app), ("schedule", schedule_app), ("lever", lever_app),
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

    def _handle_scheduled_run(ctx: jobs_mod.JobContext) -> None:
        payload = ctx.job.payload
        sampler.enqueue_run(
            ctx.conn, str(ctx.job.org_id), str(ctx.job.site_id),
            samples_per_run=int(payload.get("samples_per_run", 3)),
        )

    from gm.audit.pipeline import handle_audit_page

    handlers = {
        "sample_citations": sampler.handle_sample_citations,
        "scheduled_run": _handle_scheduled_run,
        "audit_page": handle_audit_page,
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
def serve(host: str = "0.0.0.0", port: int = 8080):
    """Run the API (share pages + admin)."""
    import uvicorn

    uvicorn.run("gm.api:app", host=host, port=port)


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
