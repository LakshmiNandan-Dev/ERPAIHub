"""
EBS Patching agent — guided chat interview + simulated patch run + downloadable
patch runbook. Covers the database tier (RDBMS ORACLE_HOME via OPatch + datapatch,
Grid Infrastructure via opatchauto, RAC rolling per node) and the application tier
(adop online patching, WebLogic and the FMW component homes).

Simulator-first (no real execution). Patching a PRODUCTION target is allowed but
gated by the production guard + maker-checker approval (a different Admin/DBA must
approve), unlike cloning which forbids prod targets outright.
"""
from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone

from .. import database, models, patching_service, prod_guard, audit_service, patch_exec
from .auth import get_current_user, require_agent_access, require_approver

router = APIRouter(prefix="/patching", tags=["Patching Agent"])


# ── Guided interview ────────────────────────────────────────────────────────────

_BASE = [
    ("target_environment", None),   # dynamic — pick any registered environment
    ("components",  "Which components to patch? Reply a comma-separated list from "
                    "`db_home`, `grid`, `adop`, `weblogic`, `fmw_homes` (e.g. `db_home, grid, adop`)."),
    ("patch_number", "Which patch / bundle number(s) to apply? (e.g. `37123456` or `37123456,37987654`)"),
]
_ALL_KEYS = ([f for f, _ in _BASE]
             + ["patch_label", "patch_stage", "db_topology", "rac_node_count", "rac_db_hosts",
                "adop_mode", "db_oracle_home", "grid_home", "weblogic_home", "fmw_homes",
                "apps_host", "db_host", "oracle_sid", "apply_workers", "run_fs", "patch_fs"])


def _filled(ctx, f) -> bool:
    return bool(str(ctx.get(f) or "").strip())


def _comps(ctx) -> list:
    return patching_service.parse_components(ctx.get("components"))


def _is_rac(ctx) -> bool:
    t = str(ctx.get("db_topology") or "").strip().lower()
    return t.startswith("r") or "cluster" in t


def _all_envs(db):
    return db.query(models.EbsEnvironment).filter(
        models.EbsEnvironment.is_active == True   # noqa: E712
    ).order_by(models.EbsEnvironment.name).all()


def _resolve_env(db, name):
    if not str(name or "").strip():
        return None
    return db.query(models.EbsEnvironment).filter(
        models.EbsEnvironment.name == str(name).strip().upper()
    ).first()


def _next_field(ctx: dict):
    for f, q in _BASE:
        if not _filled(ctx, f):
            return f, q

    comps = _comps(ctx)
    if not comps:
        return "components", ("I didn't recognise any component. Reply a comma-separated list from "
                              "`db_home`, `grid`, `adop`, `weblogic`, `fmw_homes`.")

    db_side = ("db_home" in comps) or ("grid" in comps)
    if db_side:
        topo = str(ctx.get("db_topology") or "").strip().lower()
        if not topo:
            return "db_topology", ("Database **topology** — reply `single` (single instance) or `rac` "
                                   "(Real Application Clusters, patched rolling node-by-node)?")
        if not topo.startswith(("s", "r")) and "cluster" not in topo:
            return "db_topology", "Please reply `single` or `rac`."
        if _is_rac(ctx):
            if not _filled(ctx, "rac_node_count"):
                return "rac_node_count", "How many **RAC nodes** (instances)? (e.g. `2`)"
            if not _filled(ctx, "rac_db_hosts"):
                return "rac_db_hosts", ("**RAC node hostnames**, comma-separated, one per instance? "
                                        "(e.g. `prod-db1,prod-db2`)")

    if "adop" in comps and not _filled(ctx, "adop_mode"):
        return "adop_mode", ("adop **patching mode** — reply `online` (apply to patch edition, brief cutover; "
                             "recommended), `downtime` (services down), or `hotpatch` (single small patch)?")
    return None, None


def _components_desc(comps: list) -> str:
    label = {
        "db_home": "RDBMS ORACLE_HOME (OPatch + datapatch)",
        "grid": "Grid Infrastructure (opatchauto)",
        "adop": "EBS application tier (adop online patching)",
        "weblogic": "WebLogic Server (OPatch)",
        "fmw_homes": "FMW homes — oracle_common / Forms / OHS (OPatch)",
    }
    return "\n".join(f"- {label.get(c, c)}" for c in comps) or "- (none)"


def _topology_desc(ctx: dict) -> str:
    if _is_rac(ctx):
        n = str(ctx.get("rac_node_count") or "2")
        return (f"**RAC** — {n} nodes on `{ctx.get('rac_db_hosts','?')}`. DB/Grid patched **rolling** "
                "(one node at a time → zero downtime); datapatch runs once at the end.")
    return "**Single instance**."


def _plan_md(ctx: dict, env, verdict: dict) -> str:
    comps = _comps(ctx)
    db_side = ("db_home" in comps) or ("grid" in comps)
    is_prod = bool(verdict.get("production"))
    if is_prod:
        reasons = "\n".join(f"  - {r}" for r in (verdict.get("reasons") or [])) or "  - classified production"
        guard_line = ("🔴 **Production target.** Patching is allowed, but this run will be **logged and held for "
                      "approval by a different Admin/DBA** (maker-checker) before the runbook is generated:\n"
                      f"{reasons}")
    else:
        guard_line = ("🟢 **Non-production target** — the run proceeds immediately (production guard checks "
                      "registry tier, DBID/global_name and live behavioural signals).")
    topo_line = f"**DB topology:** {_topology_desc(ctx)}\n\n" if db_side else ""
    adop_line = ""
    if "adop" in comps:
        adop_line = f"**adop mode:** `{str(ctx.get('adop_mode') or 'online').strip().lower()}`\n\n"
    return (
        f"🩹 **EBS Patch Plan — {env.name}**\n\n"
        f"**Patch:** `{ctx.get('patch_number','?')}`"
        + (f" ({ctx.get('patch_label')})" if ctx.get('patch_label') else "") + "\n\n"
        "**Components:**\n"
        f"{_components_desc(comps)}\n\n"
        f"{topo_line}"
        f"{adop_line}"
        "**Validation gates (before any apply):** OPatch conflict + space prereq on **every** home, an "
        "**already-applied (idempotency)** check (skips homes that already have it; no-op if all do)"
        + (", and the full adop cycle — pre-flight `adop -status` → prepare → apply → finalize → cutover → "
           "cleanup → fs_clone" if "adop" in comps else "") + ".\n\n"
        f"{guard_line}\n\n"
        "Passwords are never embedded — the runbook parameterises `SYS_PWD` / `APPS_PWD` / `WLS_PWD`.\n\n"
        "Type **yes** to run the (simulated) patch and generate the runbook, or **cancel** to stop."
    )


class AgentRequest(BaseModel):
    context: dict = {}


@router.post("/agent")
def patch_agent(payload: AgentRequest,
                db: Session = Depends(database.get_db),
                current_user: models.User = Depends(require_agent_access)):
    ctx = {k: str(payload.context.get(k) or "") for k in _ALL_KEYS}
    field, question = _next_field(ctx)

    if field == "target_environment":
        envs = _all_envs(db)
        if not envs:
            return {"type": "error", "context": ctx,
                    "content": "⚠️ No environments are registered. An administrator must add the target "
                               "environment in **Admin Console → Environments** before patching."}
        names = ", ".join(f"{e.name}{' (PROD)' if (e.tier or '').lower() == 'prod' else ''}" for e in envs)
        return {"type": "question", "field": "target_environment", "context": ctx,
                "content": "🧩 Which **environment** do you want to patch?\n\n"
                           f"Available: {names}"}

    if field:
        return {"type": "question", "field": field, "content": f"🧩 {question}", "context": ctx}

    env = _resolve_env(db, ctx.get("target_environment"))
    if not env:
        return {"type": "question", "field": "target_environment", "context": ctx,
                "content": "🧩 That environment isn't registered. Choose one of the listed environments."}

    # Production guard: detect whether the target is genuinely production (allowed,
    # but it will require maker-checker approval at run time).
    verdict = prod_guard.evaluate(env, db)
    return {"type": "plan", "content": _plan_md(ctx, env, verdict), "context": ctx}


# ── Create + simulate a patch run ─────────────────────────────────────────────

class PatchCreate(BaseModel):
    target_environment: str
    components: str | list                      # csv string or list
    patch_number: str
    patch_label: Optional[str] = None
    patch_stage: Optional[str] = None
    db_topology: Optional[str] = None
    rac_node_count: Optional[str] = None
    rac_db_hosts: Optional[str] = None
    adop_mode: Optional[str] = None
    db_oracle_home: Optional[str] = None
    grid_home: Optional[str] = None
    weblogic_home: Optional[str] = None
    fmw_homes: Optional[str] = None
    apps_host: Optional[str] = None
    db_host: Optional[str] = None
    oracle_sid: Optional[str] = None
    apply_workers: Optional[str] = None
    cleanup_mode: Optional[str] = None
    run_fs: Optional[str] = None
    patch_fs: Optional[str] = None
    # Drive the adop cycle phase-by-phase (prepare/apply/cutover on demand) instead
    # of simulating the whole cycle in one batch. Only meaningful when adop is selected.
    interactive: Optional[bool] = False
    # Execute phases LIVE over SSH using credentials resolved from the environment
    # (read-only checks always go live when SSH is available; destructive phases
    # only when this is true). Falls back to the simulator if creds/SSH are missing.
    live: Optional[bool] = False
    params: Optional[dict] = None


class PhaseRequest(BaseModel):
    phase: str   # prepare | apply | finalize | cutover | cleanup | fs_clone | abort | status


class OverrideRequest(BaseModel):
    reason: str


# Interview-collected fields folded into PatchRun.params for command rendering.
_PARAM_KEYS = ["patch_stage", "db_topology", "rac_node_count", "rac_db_hosts", "adop_mode",
               "db_oracle_home", "grid_home", "weblogic_home", "fmw_homes",
               "apps_host", "db_host", "oracle_sid", "apply_workers", "cleanup_mode",
               "run_fs", "patch_fs"]


def _run_out(r: models.PatchRun) -> dict:
    return {
        "id": r.id, "environment_name": r.environment_name,
        "patch_number": r.patch_number, "patch_label": r.patch_label,
        "components": r.components or [], "status": r.status,
        "steps": r.steps or [],
        "interactive": bool((r.params or {}).get("interactive")),
        "guard_status": r.guard_status, "guard_result": r.guard_result,
        "override_reason": r.override_reason,
        "created_at": r.created_at, "completed_at": r.completed_at,
    }


def _simulate(run, db):
    run.steps = patching_service.build_patch_steps(run)
    flag_modified(run, "steps")
    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)


def _arm_interactive(run, db):
    """Build the full plan but leave the adop cycle to be driven phase-by-phase."""
    run.steps = patching_service.build_patch_steps(run)
    flag_modified(run, "steps")
    p = dict(run.params or {})
    p["interactive"] = True
    p.setdefault("cycle", {"executed": [], "aborted": False})
    run.params = p
    flag_modified(run, "params")
    run.status = "cycle_ready"
    db.commit()
    db.refresh(run)


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_patch(payload: PatchCreate, db: Session = Depends(database.get_db),
                 current_user: models.User = Depends(require_agent_access)):
    env = _resolve_env(db, payload.target_environment)
    if not env:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Target must be a registered environment. Add it in Admin Console → Environments.")

    components = patching_service.parse_components(payload.components)
    if not components:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Select at least one component to patch "
                                   "(db_home, grid, adop, weblogic, fmw_homes).")
    if not (payload.patch_number or "").strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="A patch / bundle number is required.")

    params = dict(payload.params or {})
    for k in _PARAM_KEYS:
        v = getattr(payload, k, None)
        if v:
            params[k] = v

    # Phase-by-phase (interactive) operation only applies when adop is selected.
    interactive = bool(payload.interactive) and "adop" in components
    if interactive:
        params["interactive"] = True
    if payload.live:
        params["live"] = True

    # Production guard: prod targets are allowed but require maker-checker approval.
    verdict = prod_guard.evaluate(env, db)
    needs_approval = bool(verdict["production"])

    run = models.PatchRun(
        user_id=current_user.id,
        environment_id=env.id, environment_name=env.name,
        patch_label=payload.patch_label or payload.patch_number,
        patch_number=payload.patch_number, components=components, params=params,
        status="awaiting_approval" if needs_approval else "running",
        guard_status="approval_required" if needs_approval else "passed",
        guard_result=verdict,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    audit_service.log("agent_invoke", user_id=current_user.id, username=current_user.username,
                      agent="patching", detail={"run_id": run.id, "environment": env.name,
                                                "components": components, "patch": run.patch_number,
                                                "production": needs_approval})

    if needs_approval:
        # Separation of duties: a DIFFERENT Admin/DBA must approve a production patch
        # before the runbook is generated (POST /patching/{id}/override).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": f"'{env.name}' is a PRODUCTION target. Patching is allowed but requires "
                               "approval by a different Admin/DBA.",
                    "guard": verdict, "run_id": run.id, "requires_approval": True},
        )

    if interactive:
        _arm_interactive(run, db)   # ready to drive prepare/apply/cutover on demand
    else:
        _simulate(run, db)
    return _run_out(run)


@router.post("/{run_id}/override")
def override_patch(run_id: int, payload: OverrideRequest,
                   db: Session = Depends(database.get_db),
                   approver: models.User = Depends(require_approver)):
    """Maker-checker: a DIFFERENT Admin/DBA approves a production patch run."""
    run = db.query(models.PatchRun).filter(models.PatchRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patch run not found")
    if run.guard_status != "approval_required":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This patch run is not awaiting approval.")
    if approver.id == run.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You cannot approve your own patch request — a different Admin/DBA must approve.")
    if not (payload.reason or "").strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="An approval reason is required.")

    run.guard_status = "overridden"
    run.override_reason = payload.reason.strip()
    run.overridden_by = approver.id
    run.status = "running"
    db.commit()
    if (run.params or {}).get("interactive"):
        _arm_interactive(run, db)   # approved — now driven phase-by-phase
    else:
        _simulate(run, db)
    audit_service.log("clone_override_approved", user_id=approver.id, username=approver.username,
                      agent="patching", detail={"run_id": run.id, "requested_by_id": run.user_id,
                                                "environment": run.environment_name, "reason": run.override_reason})
    return _run_out(run)


@router.post("/{run_id}/reject")
def reject_patch(run_id: int,
                 db: Session = Depends(database.get_db),
                 approver: models.User = Depends(require_approver)):
    """A different Admin/DBA rejects a production patch request."""
    run = db.query(models.PatchRun).filter(models.PatchRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patch run not found")
    if run.guard_status != "approval_required":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This patch run is not awaiting approval.")
    if approver.id == run.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You cannot reject your own patch request.")
    run.guard_status = "rejected"
    run.status = "rejected"
    db.commit()
    db.refresh(run)
    audit_service.log("clone_override_rejected", user_id=approver.id, username=approver.username,
                      agent="patching", detail={"run_id": run.id, "requested_by_id": run.user_id,
                                                "environment": run.environment_name})
    return _run_out(run)


@router.get("/")
def list_patches(db: Session = Depends(database.get_db),
                 current_user: models.User = Depends(get_current_user)):
    rows = db.query(models.PatchRun).filter(
        models.PatchRun.user_id == current_user.id
    ).order_by(models.PatchRun.created_at.desc()).all()
    return [_run_out(r) for r in rows]


@router.get("/{run_id}")
def get_patch(run_id: int, db: Session = Depends(database.get_db),
              current_user: models.User = Depends(get_current_user)):
    run = db.query(models.PatchRun).filter(
        models.PatchRun.id == run_id, models.PatchRun.user_id == current_user.id
    ).first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patch run not found")
    return _run_out(run)


@router.get("/{run_id}/runbook")
def download_runbook(run_id: int, db: Session = Depends(database.get_db),
                     current_user: models.User = Depends(get_current_user)):
    run = db.query(models.PatchRun).filter(
        models.PatchRun.id == run_id, models.PatchRun.user_id == current_user.id
    ).first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patch run not found")
    # The runbook is the full procedure document — available once the run is approved
    # (not awaiting approval) and the plan has been built, in batch or interactive mode.
    if run.guard_status == "approval_required" or not run.steps:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Runbook is available once the patch run is approved.")
    zip_bytes, filename = patching_service.build_runbook_zip(run)
    return Response(
        content=zip_bytes, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Interactive adop cycle — check status / run a single phase ─────────────────

def _owned_adop_run(run_id, db, user):
    run = db.query(models.PatchRun).filter(
        models.PatchRun.id == run_id, models.PatchRun.user_id == user.id
    ).first()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patch run not found")
    if "adop" not in (run.components or []):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This run does not include the adop (application-tier) cycle.")
    return run


def _enrich_status(run, db):
    """Agent cycle state + live `adop -status` over SSH (when creds allow) + readiness."""
    state = patching_service.adop_status(run)
    env = db.query(models.EbsEnvironment).filter(
        models.EbsEnvironment.id == run.environment_id).first()
    if not env:
        state["source"] = "simulated"
        state["readiness"] = {"live_checks": False, "live_phases": False, "ssh": False}
        return state
    rd = patch_exec.readiness(env, db)
    state["readiness"] = rd
    if rd["live_checks"]:
        c = patching_service._ctx(run)
        live = patch_exec.live_adop_status(patch_exec.resolve_creds(env, db), c["RUN_FS"])
        state["live"] = live
        state["source"] = "live" if "error" not in live else "simulated"
    else:
        state["source"] = "simulated"
    return state


@router.get("/{run_id}/adop-status")
def adop_status_endpoint(run_id: int, db: Session = Depends(database.get_db),
                         current_user: models.User = Depends(get_current_user)):
    """Report the adop cycle state — the agent's tracked phases plus a live
    `adop -status` over SSH when the environment has credentials configured."""
    run = _owned_adop_run(run_id, db, current_user)
    return _enrich_status(run, db)


@router.post("/{run_id}/phase")
def run_adop_phase(run_id: int, payload: PhaseRequest, db: Session = Depends(database.get_db),
                   current_user: models.User = Depends(require_agent_access)):
    """Run (simulate) a single adop phase on demand — prepare / apply / finalize /
    cutover / cleanup / fs_clone / abort — or 'status' to just report state."""
    run = _owned_adop_run(run_id, db, current_user)
    if run.guard_status == "approval_required":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This production patch run is still awaiting maker-checker approval.")
    if not (run.params or {}).get("interactive"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This run was executed as a full batch; phase-by-phase control is only "
                                   "available for interactive runs.")

    phase = (payload.phase or "").strip().lower()
    if phase in ("", "status"):
        return {"phase": "status", "status": _enrich_status(run, db)}
    if phase not in patching_service.ADOP_CYCLE and phase != "abort":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Unknown adop phase '{phase}'.")

    cyc = dict((run.params or {}).get("cycle") or {})
    executed = list(cyc.get("executed") or [])
    if cyc.get("aborted"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This cycle was aborted — start a new patch run.")
    if not patching_service._adop_prereq_ok(phase, executed):
        nxt = patching_service.adop_next_phase(executed)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Cannot run '{phase}' now. Next allowed phase: {nxt or 'cycle already complete'}.")

    step = patching_service.adop_phase_step(run, phase)
    step["source"] = "simulated"

    # Opt-in LIVE execution over SSH (per-run `live` flag). Read-only phases need
    # only SSH; destructive phases also need APPS + WebLogic passwords. Anything
    # missing or any SSH failure falls back to the simulated step.
    want_live = bool((run.params or {}).get("live"))
    failed = False
    if want_live:
        env = db.query(models.EbsEnvironment).filter(
            models.EbsEnvironment.id == run.environment_id).first()
        rd = patch_exec.readiness(env, db) if env else {"live_phases": False, "live_checks": False, "missing_for_phases": ["env"]}
        # Every adop phase here (incl. abort) invokes adop and is prompted for passwords.
        if env and rd["live_phases"]:
            c = patching_service._ctx(run)
            res = patch_exec.live_adop_phase(patch_exec.resolve_creds(env, db), c["RUN_FS"], phase, c)
            if "error" in res:
                step["source"] = "simulated"
                step["live_error"] = res["error"]
            else:
                step["source"] = "live"
                step["rc"] = res["rc"]
                step["log"] = f"$ {res.get('command')}\n[exit {res['rc']}]\n{res['output']}"
                if res["rc"] != 0:
                    step["status"] = "failed"
                    failed = True
        else:
            step["live_error"] = "live requested but unavailable: missing " + ", ".join(rd.get("missing_for_phases") or ["creds"])

    # Record. A failed LIVE phase does NOT advance the cycle (re-run after fixing).
    steps = list(run.steps or [])
    if failed:
        run.status = "failed"
        steps.append({**step, "step": len(steps) + 1})
        run.steps = steps
        flag_modified(run, "steps")
    elif phase == "abort":
        cyc["aborted"] = True
        steps.append({**step, "step": len(steps) + 1})
        run.steps = steps
        flag_modified(run, "steps")
        run.status = "aborted"
    else:
        executed.append(phase)
        cyc["executed"] = executed
        run.status = "completed" if phase == "fs_clone" else "in_cycle"
        if phase == "fs_clone":
            run.completed_at = datetime.now(timezone.utc)
    run.params = {**(run.params or {}), "cycle": cyc}
    flag_modified(run, "params")
    db.commit()
    db.refresh(run)

    audit_service.log("agent_invoke", user_id=current_user.id, username=current_user.username,
                      agent="patching", detail={"run_id": run.id, "adop_phase": phase,
                                                "environment": run.environment_name,
                                                "executed": step["source"], "rc": step.get("rc")})
    return {"phase": phase, "step": step, "status": _enrich_status(run, db)}
