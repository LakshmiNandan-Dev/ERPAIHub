"""
EBS Cloning agent — guided chat interview + simulated Rapid Clone run +
downloadable clone runbook. Simulator-first (no real execution).
"""
from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime, timezone

from app.core import database
from app import models
from app.modules.dba.cloning import cloning_service
from app.core.safety import prod_guard
from app.core.audit import audit_service
from app.core.auth.auth import get_current_user, require_agent_access, require_approver

router = APIRouter(prefix="/cloning", tags=["Cloning Agent"])


# ── Guided interview ────────────────────────────────────────────────────────────

# Base fields collected for every clone (in order). Storage adds conditional fields.
# The TARGET is a registered NON-PROD environment (governance + production guard).
_BASE = [
    ("source_name",        "What is the SOURCE instance name? (e.g. PROD)"),
    ("source_sid",         "SOURCE database SID / DB name? (e.g. PROD)"),
    ("source_db_host",     "SOURCE database server hostname?"),
    ("source_apps_host",   "SOURCE application-tier server hostname?"),
    ("target_environment", None),   # dynamic — pick a registered non-prod environment
    ("target_apps_host",   "TARGET application-tier server hostname?"),
    ("target_storage",     "Target DB **storage layout** — reply `asm` or `fs` (filesystem)?"),
    ("target_db_home",     "Does the **TARGET DB server** already have the database **ORACLE_HOME** "
                           "(RDBMS binaries) installed? Reply `yes` (use it) or `no` (clone the RDBMS "
                           "home from the source)."),
    ("db_topology",        "Target database **topology** — reply `single` (single instance) or `rac` "
                           "(Real Application Clusters)?"),
]
_ALL_KEYS = ([f for f, _ in _BASE]
             + ["asm_dg", "asm_dg_reco", "target_datafile_dir", "source_datafile_dir",
                "source_oracle_home", "target_oracle_home",
                "rac_node_count", "rac_db_hosts", "scan_name"])


def _filled(ctx, f) -> bool:
    return bool(str(ctx.get(f) or "").strip())


def _clone_home(ctx) -> bool:
    """True when the target DB server has no ORACLE_HOME and the RDBMS binaries
    must be cloned from the source DB server."""
    return str(ctx.get("target_db_home") or "").strip().lower() in (
        "no", "n", "false", "absent", "none", "missing", "clone")


def _is_rac(ctx) -> bool:
    t = str(ctx.get("db_topology") or "").strip().lower()
    return t.startswith("r") or "cluster" in t


def _nonprod_envs(db):
    return db.query(models.EbsEnvironment).filter(
        models.EbsEnvironment.is_active == True,   # noqa: E712
        models.EbsEnvironment.tier != "prod",
    ).order_by(models.EbsEnvironment.name).all()


def _resolve_env(db, name):
    if not str(name or "").strip():
        return None
    return db.query(models.EbsEnvironment).filter(
        models.EbsEnvironment.name == str(name).strip().upper()
    ).first()


_YES = ("yes", "y", "true", "present", "existing", "exists", "use", "have", "installed")


def _next_field(ctx: dict):
    for f, q in _BASE:
        if not _filled(ctx, f):
            return f, q
    st = str(ctx.get("target_storage") or "").strip().lower()
    rac = _is_rac(ctx)
    # RAC requires shared ASM storage — reject a filesystem choice up front.
    if rac and st.startswith("f"):
        return "target_storage", "RAC requires shared **ASM** storage — reply `asm`."
    if st.startswith("a"):                       # ASM
        if not _filled(ctx, "asm_dg"):
            return "asm_dg", "Target **ASM data diskgroup**? (e.g. `+DATA`)"
    elif st.startswith("f"):                      # filesystem
        if not _filled(ctx, "target_datafile_dir"):
            return "target_datafile_dir", "Target **datafile directory**? (e.g. `/u01/app/oracle/oradata/CLONE`)"
        if not _filled(ctx, "source_datafile_dir"):
            return "source_datafile_dir", "Source **datafile directory** (for path conversion)? (e.g. `/u01/app/oracle/oradata/PROD`)"
    else:
        return "target_storage", "Please reply `asm` or `fs` — target DB storage layout?"

    # Validate the ORACLE_HOME answer and, when cloning, collect the home paths.
    tdh = str(ctx.get("target_db_home") or "").strip().lower()
    if tdh not in _YES and not _clone_home(ctx):
        return "target_db_home", ("Please reply `yes` (target already has the database ORACLE_HOME) "
                                  "or `no` (clone the RDBMS binaries from the source DB server).")
    if _clone_home(ctx):
        if not _filled(ctx, "source_oracle_home"):
            return "source_oracle_home", ("Source DB **ORACLE_HOME** path (RDBMS binaries to clone)? "
                                          "(e.g. `/u01/app/oracle/PROD/db/tech_st/19.0.0`)")
        if not _filled(ctx, "target_oracle_home"):
            return "target_oracle_home", ("Target **ORACLE_HOME** path to install the cloned binaries? "
                                          "(e.g. `/u01/app/oracle/CLONE/db/tech_st/19.0.0`)")

    # Validate the topology answer and, for RAC, collect node count / hosts / SCAN.
    topo = str(ctx.get("db_topology") or "").strip().lower()
    if not rac and not topo.startswith("s"):
        return "db_topology", "Please reply `single` (single instance) or `rac` (Real Application Clusters)."
    if rac:
        if not _filled(ctx, "rac_node_count"):
            return "rac_node_count", "How many **RAC database nodes** (instances)? (e.g. `2`)"
        if not _filled(ctx, "rac_db_hosts"):
            return "rac_db_hosts", ("**RAC node hostnames**, comma-separated, one per instance? "
                                    "(e.g. `uat2-db1,uat2-db2`)")
        if not _filled(ctx, "scan_name"):
            return "scan_name", "**SCAN** listener name for the target cluster? (e.g. `uat2-scan`)"
    return None, None


def _storage_desc(ctx: dict) -> str:
    st = str(ctx.get("target_storage") or "").strip().lower()
    if st.startswith("a"):
        return f"ASM — data diskgroup `{ctx.get('asm_dg', '+DATA')}`, reco `{ctx.get('asm_dg_reco', '+RECO')}` (OMF)"
    if st.startswith("f"):
        return (f"Filesystem — convert `{ctx.get('source_datafile_dir', '?')}` → "
                f"`{ctx.get('target_datafile_dir', '?')}`")
    return "(not set)"


def _topology_desc(ctx: dict) -> str:
    if _is_rac(ctx):
        n = str(ctx.get("rac_node_count") or "2")
        return (f"**RAC** — {n} instances on `{ctx.get('rac_db_hosts','?')}`, SCAN `{ctx.get('scan_name','?')}` "
                "(shared ASM). Duplicated single-instance, then converted + registered with srvctl. "
                "_Grid Infrastructure must already exist (not cloned)._")
    return "**Single instance**."


def _plan_md(ctx: dict) -> str:
    clone_home = _clone_home(ctx)
    if clone_home:
        home_line = (f"**DB ORACLE_HOME:** target has none → **cloned from source** "
                     f"(`{ctx.get('source_oracle_home','?')}` → `{ctx.get('target_oracle_home','?')}`): "
                     "adpreclone dbTier → ship RDBMS binaries → adcfgclone dbTechStack (relink + inventory + root.sh).")
        phases = ("pre-checks → source apps pre-clone → **stage + ship + configure the RDBMS ORACLE_HOME** → "
                  "prepare target auxiliary (SYS password file) → RMAN duplicate → apps file-system copy → "
                  "target apps config → AutoConfig → validation")
        home_note = "The RDBMS ORACLE_HOME is cloned from the source (target had no database home)."
    else:
        home_line = "**DB ORACLE_HOME:** already installed on the target — binaries are **not** cloned."
        phases = ("pre-checks → source apps pre-clone → prepare target auxiliary (SYS password file) → "
                  "RMAN duplicate → target DB config → apps file-system copy → target apps config → "
                  "AutoConfig → validation")
        home_note = "Database ORACLE_HOME binaries are not cloned."
    if _is_rac(ctx):
        phases = phases.replace("RMAN duplicate", "RMAN duplicate (single-instance) → **RAC convert + register**")
    return (
        f"🧬 **EBS Clone Plan — {ctx.get('source_name','SOURCE')} → {ctx.get('target_name','TARGET')}**\n\n"
        "**Method:** RMAN active duplicate (database) + Rapid Clone (application tier)\n\n"
        "| | Source | Target |\n|---|---|---|\n"
        f"| Instance | {ctx.get('source_name','?')} | {ctx.get('target_name','?')} (non-prod) |\n"
        f"| DB SID | {ctx.get('source_sid','?')} | {ctx.get('target_sid','?')} |\n"
        f"| DB host | {ctx.get('source_db_host','?')} | {ctx.get('target_db_host','?')} |\n"
        f"| Apps host | {ctx.get('source_apps_host','?')} | {ctx.get('target_apps_host','?')} |\n\n"
        f"**Target storage:** {_storage_desc(ctx)}\n\n"
        f"**DB topology:** {_topology_desc(ctx)}\n\n"
        f"{home_line}\n\n"
        "**Production guard:** the target is verified at run time (registry tier + DBID/global_name + "
        "live ICX sessions / concurrent requests / context) — a production target is blocked.\n\n"
        f"**Phases:** {phases}.\n\n"
        f"_`SYS_PWD` must be the **source** SYS password. {home_note}_\n\n"
        "Type **yes** to run the (simulated) clone and generate the runbook, or **cancel** to stop."
    )


class AgentRequest(BaseModel):
    context: dict = {}


@router.post("/agent")
def clone_agent(payload: AgentRequest,
                db: Session = Depends(database.get_db),
                current_user: models.User = Depends(require_agent_access)):
    ctx = {k: str(payload.context.get(k) or "") for k in _ALL_KEYS}
    field, question = _next_field(ctx)

    if field == "target_environment":
        envs = _nonprod_envs(db)
        if not envs:
            return {"type": "error", "context": ctx,
                    "content": "⚠️ No non-prod environments are registered. An administrator must add a "
                               "target environment (tier = non-prod) in **Admin Console → Environments** "
                               "before cloning."}
        names = ", ".join(e.name for e in envs)
        return {"type": "question", "field": "target_environment", "context": ctx,
                "content": "🧩 Which **TARGET** environment? It must be a registered **non-prod** environment.\n\n"
                           f"Available: {names}"}

    if field:
        return {"type": "question", "field": field, "content": f"🧩 {question}", "context": ctx}

    # All collected — validate the chosen target environment and enrich the context.
    env = _resolve_env(db, ctx.get("target_environment"))
    if not env:
        return {"type": "question", "field": "target_environment", "context": ctx,
                "content": "🧩 That environment isn't registered. Choose one of the listed non-prod environments."}
    if (env.tier or "").lower() == "prod":
        return {"type": "error", "context": ctx,
                "content": f"🛑 '{env.name}' is classified **PRODUCTION** and cannot be used as a clone target."}
    ctx["target_name"] = env.name
    ctx["target_sid"] = env.db_sid or env.name
    ctx["target_db_host"] = env.db_host or ""
    return {"type": "plan", "content": _plan_md(ctx), "context": ctx}


# ── Create + simulate a clone run ───────────────────────────────────────────────

class CloneCreate(BaseModel):
    source_name: str
    target_environment: str            # registered NON-PROD environment name (governed target)
    source_sid: Optional[str] = None
    source_db_host: Optional[str] = None
    source_apps_host: Optional[str] = None
    target_apps_host: Optional[str] = None
    # storage layout (folded into params)
    target_storage: Optional[str] = None
    asm_dg: Optional[str] = None
    asm_dg_reco: Optional[str] = None
    target_datafile_dir: Optional[str] = None
    source_datafile_dir: Optional[str] = None
    # DB ORACLE_HOME — clone the RDBMS binaries when the target has no home (folded into params)
    target_db_home: Optional[str] = None
    source_oracle_home: Optional[str] = None
    target_oracle_home: Optional[str] = None
    # DB topology — single instance or RAC (folded into params)
    db_topology: Optional[str] = None
    rac_node_count: Optional[str] = None
    rac_db_hosts: Optional[str] = None
    scan_name: Optional[str] = None
    params: Optional[dict] = None


class OverrideRequest(BaseModel):
    reason: str


# Interview-collected fields folded into CloneRun.params for command rendering.
_STORAGE_KEYS = ["target_storage", "asm_dg", "asm_dg_reco", "target_datafile_dir", "source_datafile_dir",
                 "target_db_home", "source_oracle_home", "target_oracle_home",
                 "db_topology", "rac_node_count", "rac_db_hosts", "scan_name"]


def _run_out(r: models.CloneRun) -> dict:
    return {
        "id": r.id, "source_name": r.source_name, "target_name": r.target_name,
        "source_sid": r.source_sid, "target_sid": r.target_sid,
        "source_db_host": r.source_db_host, "target_db_host": r.target_db_host,
        "source_apps_host": r.source_apps_host, "target_apps_host": r.target_apps_host,
        "db_method": r.db_method, "status": r.status, "steps": r.steps or [],
        "guard_status": r.guard_status, "guard_result": r.guard_result,
        "override_reason": r.override_reason,
        "created_at": r.created_at, "completed_at": r.completed_at,
    }


def _simulate(run, db):
    run.steps = cloning_service.build_clone_steps(run)
    flag_modified(run, "steps")
    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_clone(payload: CloneCreate, db: Session = Depends(database.get_db),
                 current_user: models.User = Depends(require_agent_access)):
    # Target must be a registered, non-production environment.
    env = _resolve_env(db, payload.target_environment)
    if not env:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Target must be a registered environment. Add it (tier = non-prod) in Admin Console → Environments.")
    if (env.tier or "").lower() == "prod":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"'{env.name}' is classified PRODUCTION and cannot be a clone target.")

    # Production guard: verify the target is genuinely non-production.
    verdict = prod_guard.evaluate(env, db)
    blocked = bool(verdict["blocked"])

    params = dict(payload.params or {})
    for k in _STORAGE_KEYS:
        v = getattr(payload, k, None)
        if v:
            params[k] = v

    # Persist every attempt — including blocked ones — so the audit trail is complete.
    run = models.CloneRun(
        user_id=current_user.id,
        source_name=payload.source_name, target_name=env.name,
        source_sid=payload.source_sid, target_sid=env.db_sid or env.name,
        source_db_host=payload.source_db_host, target_db_host=env.db_host,
        source_apps_host=payload.source_apps_host, target_apps_host=payload.target_apps_host,
        target_environment_id=env.id,
        db_method="rman_duplicate", params=params,
        status="blocked" if blocked else "running",
        guard_status="blocked" if blocked else "passed", guard_result=verdict,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    if blocked:
        # Separation of duties: the requester cannot override their own block.
        # A DIFFERENT Admin/DBA must approve the override (POST /cloning/{id}/override).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": f"Production guard blocked target '{env.name}'. "
                               "Override requires approval by a different Admin/DBA.",
                    "guard": verdict, "run_id": run.id, "requires_approval": True},
        )

    _simulate(run, db)
    return _run_out(run)


@router.post("/{run_id}/override")
def override_clone(run_id: int, payload: OverrideRequest,
                   db: Session = Depends(database.get_db),
                   approver: models.User = Depends(require_approver)):
    """Maker-checker: a DIFFERENT Admin/DBA approves a blocked clone's override."""
    run = db.query(models.CloneRun).filter(models.CloneRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clone run not found")
    if run.guard_status != "blocked":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This clone is not awaiting override approval.")
    if approver.id == run.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You cannot approve your own clone request — a different Admin/DBA must approve.")
    if not (payload.reason or "").strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="An override reason is required.")

    run.guard_status = "overridden"
    run.override_reason = payload.reason.strip()
    run.overridden_by = approver.id
    run.status = "running"
    db.commit()
    _simulate(run, db)
    audit_service.log("clone_override_approved", user_id=approver.id, username=approver.username,
                      agent="cloning", detail={"run_id": run.id, "requested_by_id": run.user_id,
                                               "reason": run.override_reason})
    return _run_out(run)


@router.post("/{run_id}/reject")
def reject_clone(run_id: int,
                 db: Session = Depends(database.get_db),
                 approver: models.User = Depends(require_approver)):
    """A different Admin/DBA rejects a blocked clone request."""
    run = db.query(models.CloneRun).filter(models.CloneRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clone run not found")
    if run.guard_status != "blocked":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This clone is not awaiting approval.")
    if approver.id == run.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You cannot reject your own clone request.")
    run.status = "rejected"
    db.commit()
    db.refresh(run)
    audit_service.log("clone_override_rejected", user_id=approver.id, username=approver.username,
                      agent="cloning", detail={"run_id": run.id, "requested_by_id": run.user_id})
    return _run_out(run)


@router.get("/")
def list_clones(db: Session = Depends(database.get_db),
                current_user: models.User = Depends(get_current_user)):
    rows = db.query(models.CloneRun).filter(
        models.CloneRun.user_id == current_user.id
    ).order_by(models.CloneRun.created_at.desc()).all()
    return [_run_out(r) for r in rows]


@router.get("/{run_id}")
def get_clone(run_id: int, db: Session = Depends(database.get_db),
              current_user: models.User = Depends(get_current_user)):
    run = db.query(models.CloneRun).filter(
        models.CloneRun.id == run_id, models.CloneRun.user_id == current_user.id
    ).first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clone run not found")
    return _run_out(run)


@router.get("/{run_id}/runbook")
def download_runbook(run_id: int, db: Session = Depends(database.get_db),
                     current_user: models.User = Depends(get_current_user)):
    run = db.query(models.CloneRun).filter(
        models.CloneRun.id == run_id, models.CloneRun.user_id == current_user.id
    ).first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clone run not found")
    zip_bytes, filename = cloning_service.build_runbook_zip(run)
    return Response(
        content=zip_bytes, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
