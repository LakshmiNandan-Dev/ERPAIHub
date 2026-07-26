"""
Finding upsert / delta-detection logic shared by the scheduled performance
scan (monitoring_scheduler.py) and the patch-gap scan (patch_gap_service.py).

A Finding is keyed by (environment_id, source, area, finding_key) — one row
per distinct issue, upserted every run rather than one row per occurrence.
This is what makes the "what's new since last time" feed cheap: a run only
ever touches rows for the areas it actually scanned.
"""
from datetime import datetime, timezone

from app import models
from app.modules.dba.performance.performance_service import value_hash


def upsert_findings(db, run: "models.DiagnosticRun", environment_id, environment_name,
                    source: str, area: str, extracted: list) -> None:
    """Create/update Finding rows for one area's extracted findings on `run`.

    - not found                          -> create, status='open', occurrence_count=1;
                                             run.new_findings_count += 1
    - found, status='resolved'           -> reopen (status='open'); run.new_findings_count += 1
    - found, status='open', hash changed -> update detail/value_hash/last_changed_at;
                                             run.changed_findings_count += 1
    - found, status='open', hash same    -> bump last_seen only, no delta counted
    Always stamps last_seen_run_id/last_seen_at on every touched row.
    """
    now = datetime.now(timezone.utc)
    # A source area can legitimately emit the same finding_key twice in one
    # extraction pass (e.g. the same program appearing twice in a simulated
    # "frequently erroring" list) — dedupe first (last occurrence wins) so a
    # single run doesn't process — and spuriously "change" — the same row
    # against itself, which would otherwise churn on every subsequent rescan.
    deduped = {item["finding_key"]: item for item in extracted}
    for finding_key, item in deduped.items():
        detail = item.get("detail") or {}
        new_hash = value_hash(detail, *detail.keys()) if detail else None

        row = db.query(models.Finding).filter(
            models.Finding.environment_id == environment_id,
            models.Finding.source == source,
            models.Finding.area == area,
            models.Finding.finding_key == finding_key,
        ).first()

        if row is None:
            row = models.Finding(
                environment_id=environment_id, environment_name=environment_name,
                source=source, area=area, finding_key=finding_key,
                severity=item.get("severity", "info"), title=item.get("title", ""),
                detail=detail, value_hash=new_hash, status="open",
                first_seen_run_id=run.id, first_seen_at=now,
                last_seen_run_id=run.id, last_seen_at=now,
                occurrence_count=1,
            )
            db.add(row)
            run.new_findings_count = (run.new_findings_count or 0) + 1
            continue

        row.last_seen_run_id = run.id
        row.last_seen_at = now
        row.occurrence_count = (row.occurrence_count or 0) + 1
        row.severity = item.get("severity", row.severity)
        row.title = item.get("title", row.title)

        if row.status == "resolved":
            row.status = "open"
            row.detail = detail
            row.value_hash = new_hash
            run.new_findings_count = (run.new_findings_count or 0) + 1
        elif row.value_hash != new_hash:
            row.detail = detail
            row.value_hash = new_hash
            row.last_changed_at = now
            run.changed_findings_count = (run.changed_findings_count or 0) + 1


def resolve_stale_findings(db, run: "models.DiagnosticRun", environment_id,
                           source: str, scanned_areas: list) -> None:
    """Any status='open' Finding for (environment_id, source, area in
    scanned_areas) not touched by this run gets marked resolved. Scoped to
    scanned_areas so a partial-area run doesn't wrongly resolve findings for
    areas it didn't check this pass."""
    if not scanned_areas:
        return
    now = datetime.now(timezone.utc)
    stale = db.query(models.Finding).filter(
        models.Finding.environment_id == environment_id,
        models.Finding.source == source,
        models.Finding.area.in_(scanned_areas),
        models.Finding.status == "open",
        models.Finding.last_seen_run_id != run.id,
    ).all()
    for row in stale:
        row.status = "resolved"
        row.resolved_run_id = run.id
        row.resolved_at = now
        run.resolved_findings_count = (run.resolved_findings_count or 0) + 1
