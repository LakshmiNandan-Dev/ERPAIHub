"""
File-version drift verification — compares every patchable file under an
environment's run-edition filesystem (ALL products in one pass: ap/, gl/,
fnd/, ... beneath the configured run_fs_path) against Oracle EBS's own
AD_FILES/AD_FILE_VERSIONS record of what version should be current.

Unlike patch_gap_service (which diffs a small admin-curated patch-number list
against a live inventory), this scans everything patch_exec.live_file_header_scan
finds and surfaces only the drift as Findings through the same
DiagnosticRun/Finding pipeline the performance monitoring scan and
patch_gap_service both use — no separate snapshot table, mismatches are the
only thing persisted (matches monitoring_service's existing "findings, not
raw diagnostic dumps" convention).

v1 scope: on-demand only (triggered via BackgroundTasks from the router),
matching patch_gap_service's own scope.
"""
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import models
from app.core import crypto, database
from app.modules.dba.patching import patch_exec
from app.modules.dba.performance import monitoring_service

_CHUNK_SIZE = 500


def _product_from_path(path: str, run_fs_path: str) -> str:
    """First path segment beneath run_fs_path — EBS's $APPL_TOP/<product>/...
    convention (ap/12.0.0/..., gl/12.0.0/..., fnd/12.0.0/...)."""
    rel = path[len(run_fs_path):].lstrip("/")
    parts = rel.split("/", 1)
    return parts[0] if parts and parts[0] else "unknown"


def _live_ad_file_versions(env_row, filenames: list) -> dict:
    """Best-effort AD_FILES/AD_FILE_VERSIONS lookup, chunked to avoid a huge
    single IN-list — 'current' version taken as the AD_FILE_VERSIONS row with
    the most recent last_update_date per file. AD schema column-level
    behavior can vary by EBS release/patch level; validate this query against
    your instance before relying on it (same "best-effort, hardcoded, not
    admin-editable" status as performance_service._REAL_SQL).
    Returns {"versions": {filename: db_version}} or {"error": ...}."""
    if not (env_row.db_host and env_row.db_sid and env_row.db_user):
        return {"error": "no DB connection configured for this environment"}
    if not filenames:
        return {"versions": {}}
    try:
        import oracledb
        dsn = oracledb.makedsn(env_row.db_host, env_row.db_port or 1521, sid=env_row.db_sid)
        conn = oracledb.connect(user=env_row.db_user, password=crypto.decrypt(env_row.db_password_enc),
                                dsn=dsn, tcp_connect_timeout=8)
        try:
            cur = conn.cursor()
            versions = {}
            distinct = sorted(set(filenames))
            for i in range(0, len(distinct), _CHUNK_SIZE):
                chunk = distinct[i:i + _CHUNK_SIZE]
                binds = {f"f{j}": name for j, name in enumerate(chunk)}
                placeholders = ", ".join(f":{k}" for k in binds)
                cur.execute(
                    f"SELECT af.filename, afv.file_version "
                    f"FROM ad_files af JOIN ad_file_versions afv ON af.file_id = afv.file_id "
                    f"WHERE af.filename IN ({placeholders}) "
                    f"AND afv.last_update_date = ("
                    f"    SELECT MAX(afv2.last_update_date) FROM ad_file_versions afv2 "
                    f"    WHERE afv2.file_id = af.file_id"
                    f")",
                    binds,
                )
                for fname, ver in cur.fetchall():
                    versions[fname] = ver
            return {"versions": versions}
        finally:
            conn.close()
    except Exception as exc:
        return {"error": str(exc)[:300]}


def _upsert_file_inventory(db, environment_id: int, run_id: int, inventory_rows: list) -> None:
    """Bulk upsert-latest into patch_file_inventory — one row per
    (environment_id, product, filename), covering every scanned file
    (matched, mismatched, and not-registered), not just the drift Findings
    persist. Postgres ON CONFLICT DO UPDATE cannot affect the same
    conflict-target row twice within one statement, so within-batch
    duplicates (e.g. a symlinked file appearing twice under one product) are
    deduped first — last occurrence wins, same precedent as
    monitoring_service.upsert_findings."""
    if not inventory_rows:
        return
    now = datetime.now(timezone.utc)
    deduped = {(r["product"], r["filename"]): r for r in inventory_rows}
    values = [{
        "environment_id": environment_id, "product": r["product"], "filename": r["filename"],
        "path": r["path"], "fs_version": r["fs_version"], "db_version": r["db_version"],
        "match_status": r["match_status"], "last_scan_run_id": run_id,
        "last_scanned_at": now, "updated_at": now,
    } for r in deduped.values()]
    for i in range(0, len(values), _CHUNK_SIZE):
        chunk = values[i:i + _CHUNK_SIZE]
        stmt = pg_insert(models.PatchFileInventory).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["environment_id", "product", "filename"],
            set_={
                "path": stmt.excluded.path, "fs_version": stmt.excluded.fs_version,
                "db_version": stmt.excluded.db_version, "match_status": stmt.excluded.match_status,
                "last_scan_run_id": stmt.excluded.last_scan_run_id,
                "last_scanned_at": stmt.excluded.last_scanned_at, "updated_at": stmt.excluded.updated_at,
            },
        )
        db.execute(stmt)


def run_file_gap_scan(config_id: int, triggered_by: int | None = None) -> None:
    """BackgroundTasks target. Owns its own DB session (mirrors
    patch_gap_service.run_gap_scan) — never raises."""
    db = database.SessionLocal()
    run = None
    try:
        config = db.query(models.PatchFileScanConfig).filter(
            models.PatchFileScanConfig.id == config_id
        ).first()
        if not config:
            return
        env = db.query(models.EbsEnvironment).filter(
            models.EbsEnvironment.id == config.environment_id
        ).first()
        if not env:
            return

        run = models.DiagnosticRun(
            environment_id=env.id, environment_name=env.name, kind="patch_file_gap",
            trigger="manual", areas=[], status="running",
        )
        db.add(run)
        db.flush()

        creds = patch_exec.resolve_creds(env, db)
        scan_result = patch_exec.live_file_header_scan(creds, config.run_fs_path, config.file_globs or [])

        if "error" in scan_result:
            run.data_source = "error"
            run.status = "completed"
            run.error = scan_result["error"][:2000]
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return

        fs_files = scan_result.get("files", [])
        db_result = _live_ad_file_versions(env, [f["filename"] for f in fs_files])

        if "error" in db_result:
            run.data_source = "error"
            run.status = "completed"
            run.error = db_result["error"][:2000]
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return

        db_versions = db_result.get("versions", {})

        # Every product actually present in this scan — computed up front so
        # a fully-clean product (zero findings) still participates in
        # resolve_stale_findings and clears any previously-open findings.
        all_products = sorted({_product_from_path(f["path"], config.run_fs_path) for f in fs_files})
        findings_by_product = {p: [] for p in all_products}
        inventory_rows = []

        for f in fs_files:
            product = _product_from_path(f["path"], config.run_fs_path)
            fs_version = f["fs_version"]
            db_version = db_versions.get(f["filename"])
            if db_version is None:
                findings_by_product[product].append({
                    "finding_key": f"filever:{f['filename']}",
                    "severity": "info",
                    "title": f"{f['filename']}: not registered in AD_FILES",
                    "detail": {"filename": f["filename"], "path": f["path"],
                              "fs_version": fs_version, "db_version": None, "product": product},
                })
                match_status = "fs_only"
            elif str(db_version) != str(fs_version):
                findings_by_product[product].append({
                    "finding_key": f"filever:{f['filename']}",
                    "severity": "warning",
                    "title": f"{f['filename']}: filesystem version {fs_version} != AD_FILES version {db_version}",
                    "detail": {"filename": f["filename"], "path": f["path"], "fs_version": fs_version,
                              "db_version": str(db_version), "product": product},
                })
                match_status = "mismatch"
            else:
                match_status = "match"
            inventory_rows.append({
                "product": product, "filename": f["filename"], "path": f["path"],
                "fs_version": fs_version, "db_version": str(db_version) if db_version is not None else None,
                "match_status": match_status,
            })

        _upsert_file_inventory(db, env.id, run.id, inventory_rows)

        for product, extracted in findings_by_product.items():
            monitoring_service.upsert_findings(db, run, env.id, env.name, "patch_file_gap", product, extracted)
        monitoring_service.resolve_stale_findings(db, run, env.id, "patch_file_gap", all_products)

        run.areas = all_products
        run.data_source = "live"
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()

    except Exception as exc:
        db.rollback()
        try:
            if run is not None:
                run.status = "failed"
                run.error = str(exc)[:2000]
                run.completed_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()
