# Phase C wave-1 module contracts

The measure leg: vault + GSC connection, two-phase ingest, opportunity detectors. Schema:
`ops/migrations/003_phase_c_measurement.sql`. Style/test rules per docs/phase-a-contracts.md
(ruff 100, pytest, DB tests skip without DATABASE_URL, ZERO network in tests). New deps already
in the venv: google-auth, pynacl.

## gm/connections/vault.py  (sealed-box credential vault, ADR + review fix)

```python
def generate_keypair() -> tuple[str, str]        # (public_b64, private_b64) — operator runs once
def seal(payload: dict) -> bytes                  # nacl SealedBox(public key from GM_VAULT_PUBLIC_KEY)
def open_sealed(blob: bytes, key_version: int = 1) -> dict
    # private key from GM_VAULT_PRIVATE_KEY (v1: single version; version param reserved).
    # RAISES VaultLocked when GM_VAULT_PRIVATE_KEY is unset — fetcher/API processes can seal
    # but never open; only publisher/ingest workers mount the private key.
def store_connection(conn, *, org_id, site_id, kind, credentials: dict, meta: dict) -> str
def load_connection(conn, site_id, kind) -> dict   # row + decrypted credentials (or VaultLocked)
def mark_connection(conn, connection_id, *, ok: bool, error: str | None = None) -> None
    # ok -> status='ok', last_ok_at=now; not ok -> status='broken', last_error set
```

## gm/connections/gsc.py  (Search Console client, service-account path)

```python
class GscClient:
    def __init__(self, service_account_info: dict, property_url: str,
                 client: httpx.Client | None = None): ...
    # token via google.auth service_account.Credentials(scopes=[".../webmasters.readonly"])
    # + google.auth.transport.requests refresh — WRAP token acquisition so tests can inject a
    # fake credentials object (constructor param credentials=None overrides).
    def query(self, *, start_date, end_date, dimensions: list[str],
              row_limit=25000, start_row=0, search_type="web",
              data_state="final") -> list[dict]
        # POST .../sites/{property}/searchAnalytics/query via httpx; single page.
    def query_all(self, **kw) -> Iterator[list[dict]]     # paginate start_row += 25k until empty
    def list_sites(self) -> list[dict]
# Retries/backoff on 429/5xx per the engines-package pattern (local copy);
# 403/401 raise GscAuthError (caller marks connection broken).
```

## gm/intel/gsc_ingest.py  (two-phase ingest — the review's critical fix)

```python
def ensure_partition(conn, month_start: date) -> None
    # CREATE TABLE IF NOT EXISTS gsc_daily_yYYYYmMM PARTITION OF gsc_daily FOR VALUES FROM .. TO ..

def initial_pull(conn, site_id: str, gsc: GscClient) -> dict
    # PHASE 1 (minutes): two whole-window aggregate pulls (28d and 90d ending today-3),
    # dimensions [page, query], first page only (top 25k by clicks) -> REPLACE gsc_window_agg
    # slices for the site. Returns {"rows_28": n, "rows_90": n}. Then caller runs detectors
    # in provisional mode.

def pull_day(conn, site_id: str, gsc: GscClient, day: date, search_type="web") -> int
    # PHASE 2 unit: one day-slice, dimensions [page, query], paginated fully; slice-replacement
    # (DELETE gsc_daily + gsc_page_daily slice, batch INSERT, rewrite rollup slice, upsert
    # gsc_ingest_log w/ final = day < today-3). ensure_partition first. Respects the
    # 50k-row/day cap via gm.infra.costs.bump_quota("gsc_rows", site_id, rows) bookkeeping.

def backfill_plan(conn, site_id: str, *, months=16) -> list[date]
    # newest-first list of missing (not in gsc_ingest_log OR not final and >= today-4) days.

def handle_gsc_initial(ctx)   # job 'gsc_initial': initial_pull + enqueue compute_queue +
                              # enqueue first backfill batch (N days) as 'gsc_backfill' jobs
def handle_gsc_backfill(ctx)  # job 'gsc_backfill' payload {days: [...]}: pull_day each with
                              # ctx.heartbeat(); re-enqueue next batch while backfill_plan
                              # non-empty, throttled: stop batch early if quota ledger for
                              # (gsc_rows, site) exceeds 45k rows today; next batch run_after
                              # tomorrow 06:00 in that case.
def handle_gsc_daily(ctx)     # scheduled: trailing window [today-4 .. today-2] slice re-pulls
                              # + compute_queue enqueue
```
Credentials: load via gm.connections (GscClient built from decrypted service-account JSON +
meta.property). All handlers mark_connection ok/broken on auth results.

## gm/intel/detectors.py  (the operator queue)

```python
def compute_queue(conn, site_id: str) -> dict     # runs all detectors; returns counts per kind
def handle_compute_queue(ctx)                     # job 'compute_queue'

# Each detector upserts queue_items (unique site+kind+target_hash): refresh at_stake/last_seen
# on conflict; never resurrect status='dismissed' unless snooze_until has passed; targets gone
# from data -> leave rows (operator history), do not delete.
# Data source selection: rollups/gsc_daily when >= 28 final days ingested (basis 'final'),
# else gsc_window_agg (basis 'provisional') — recorded in at_stake.basis.

def striking_distance(...)   # avg position 5-20, impressions >= 100 (28d window),
                             # est_clicks_gain = impressions * (ctr_at(3) - ctr_now)
def ctr_outlier(...)         # position <= 5 and ctr < 0.5 * expected_ctr(position);
                             # expected-CTR curve = module-level constant table (documented src)
def decay(...)               # FINAL data only (needs history): 28d clicks vs prior-28d and
                             # vs same-28d-last-year when available; drop >= 25% flags;
                             # provisional mode: skipped (honest gap, noted in compute_queue result)
def cannibalization(...)     # FINAL data only: same query, 2+ pages each >= 20% of query
                             # impressions in 28d
target_hash = sha256(canonical json of {kind-specific target keys})[:16]
```

## CLI additions (integrator wires; agents expose the functions above)

gm gsc connect <domain> --service-account <file.json> --property <sc-domain:x|url>
gm gsc status <domain> · gm queue <domain> [--kind] · job types registered in worker.

## Convergence diagnosis (read-only investigation — separate deliverable)

Written to docs/convergence-diagnosis.md: root cause of the auto-edit plateau (blog runs ending
42-51/90, technical layer = 0, terminal 'needs_review') in the serp-analyzer + blog-buster repos,
with evidence (exact file:line, history.json runs), and a concrete fix plan for the Phase C wave-2
wrap. NO code changes to any repo.
