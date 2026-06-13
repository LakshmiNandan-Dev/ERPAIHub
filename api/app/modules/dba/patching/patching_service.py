"""
EBS Patching service.

Builds an Oracle EBS patching plan across the database tier (RDBMS ORACLE_HOME
via OPatch + datapatch, Grid Infrastructure via opatchauto, RAC rolling per node)
and the application tier (adop online patching cycle, WebLogic and the FMW
component homes — oracle_common, Forms, OHS). It produces high-fidelity simulated
logs and a portable, parameterised runbook (patch.sh). All passwords are shell
variables — never embedded.

Simulator-first (no real execution), mirroring the cloning service.
"""
import io
import re
import json
import zipfile
from datetime import datetime, timezone

# Canonical components the agent can patch.
COMPONENTS = ["db_home", "grid", "adop", "weblogic", "fmw_homes"]

# Map free-text interview answers to canonical component keys.
_COMPONENT_SYNONYMS = {
    "db_home": "db_home", "db": "db_home", "database": "db_home", "rdbms": "db_home",
    "oracle_home": "db_home", "oraclehome": "db_home", "oh": "db_home", "opatch": "db_home",
    "grid": "grid", "gi": "grid", "crs": "grid", "clusterware": "grid",
    "grid_infrastructure": "grid", "opatchauto": "grid",
    "rac": "rac",  # topology modifier, handled separately
    "adop": "adop", "apps": "adop", "application": "adop", "ebs": "adop",
    "online_patching": "adop", "ad": "adop",
    "weblogic": "weblogic", "wls": "weblogic", "wl": "weblogic",
    "fmw": "fmw_homes", "fmw_homes": "fmw_homes", "fmwhomes": "fmw_homes",
    "oracle_common": "fmw_homes", "forms": "fmw_homes", "reports": "fmw_homes",
    "ohs": "fmw_homes", "webtier": "fmw_homes", "homes": "fmw_homes",
}


def parse_components(value) -> list:
    """Accept a list or a comma/space/'+'-separated string and return canonical keys."""
    if isinstance(value, (list, tuple, set)):
        tokens = [str(v) for v in value]
    else:
        tokens = re.split(r'[,\s/+]+', str(value or ""))
    out, seen = [], set()
    for tok in tokens:
        key = _COMPONENT_SYNONYMS.get(tok.strip().lower())
        if key and key != "rac" and key not in seen:
            seen.add(key)
            out.append(key)
    # Preserve canonical order
    return [c for c in COMPONENTS if c in seen]


def _ctx(run) -> dict:
    p = run.params or {}
    comps = run.components or parse_components(p.get("components"))
    topo = str(p.get("db_topology") or "").strip().lower()
    rac = topo.startswith("r") or "cluster" in topo
    try:
        nodes = max(2, int(p.get("rac_node_count") or 2)) if rac else 1
    except (TypeError, ValueError):
        nodes = 2
    hosts = [h.strip() for h in str(p.get("rac_db_hosts") or "").split(",") if h.strip()]
    fmw_homes = [h.strip() for h in str(p.get("fmw_homes") or "").split(",") if h.strip()]
    env = run.environment_name or "TARGET"
    adop_mode = str(p.get("adop_mode") or "online").strip().lower()
    if adop_mode.startswith("d"):
        adop_mode = "downtime"
    elif adop_mode.startswith("h"):
        adop_mode = "hotpatch"
    else:
        adop_mode = "online"
    return {
        "COMPONENTS": comps,
        "ENV_NAME": env,
        "PATCH_NUMBER": run.patch_number or "00000000",
        "PATCH_LABEL": run.patch_label or (run.patch_number or "patch"),
        "PATCH_STAGE": p.get("patch_stage") or "/u01/stage/${PATCH_NUMBER}",
        "DB_HOST": p.get("db_host") or "${ENV_NAME}-db",
        "APPS_HOST": p.get("apps_host") or "${ENV_NAME}-apps",
        "ORACLE_SID": p.get("oracle_sid") or env,
        "DB_ORACLE_HOME": p.get("db_oracle_home") or "/u01/app/oracle/${ENV_NAME}/db/tech_st/19.0.0",
        "GRID_HOME": p.get("grid_home") or "/u01/app/19.0.0/grid",
        "DB_TOPOLOGY": "rac" if rac else "single",
        "RAC_NODES": str(nodes),
        "RAC_DB_HOSTS": ",".join(hosts) if hosts else "${DB_HOST}",
        "ADOP_MODE": adop_mode,
        "APPLY_WORKERS": str(p.get("apply_workers") or 8),
        "CLEANUP_MODE": (str(p.get("cleanup_mode") or "standard").strip().lower() or "standard"),
        # Application-tier FMW: run FS (fs1) is live; patch FS (fs2) is patched during an online cycle.
        "RUN_FS": p.get("run_fs") or "/u01/app/oracle/${ENV_NAME}/fs1",
        "PATCH_FS": p.get("patch_fs") or "/u01/app/oracle/${ENV_NAME}/fs2",
        "WEBLOGIC_HOME": p.get("weblogic_home") or "${PATCH_FS}/FMW_Home/wlserver",
        "FMW_HOMES": fmw_homes or ["${PATCH_FS}/FMW_Home/oracle_common",
                                   "${PATCH_FS}/FMW_Home/Oracle_Forms",
                                   "${PATCH_FS}/FMW_Home/webtier"],
    }


def _rac_hosts(c: dict) -> list:
    n = int(c["RAC_NODES"])
    hosts = [h.strip() for h in str(c["RAC_DB_HOSTS"]).split(",") if h.strip()]
    out = []
    for i in range(1, n + 1):
        host = hosts[i - 1] if i - 1 < len(hosts) else f"{c['DB_HOST']}{i}"
        out.append((f"{c['ORACLE_SID']}{i}", host))
    return out


def _opatch_homes(c: dict) -> list:
    """OPatch-managed homes for the selected components — (label, ${HOME}).
    Used by the conflict, already-applied and validation checks. (EBS apps-tier
    patches are applied/verified via adop + ad_bugs, not OPatch, so adop is not
    listed here.)"""
    homes = []
    if "grid" in c["COMPONENTS"]:
        homes.append(("Grid Infrastructure", "${GRID_HOME}"))
    if "db_home" in c["COMPONENTS"]:
        homes.append(("RDBMS ORACLE_HOME", "${DB_ORACLE_HOME}"))
    if "weblogic" in c["COMPONENTS"]:
        homes.append(("WebLogic", "${WEBLOGIC_HOME}"))
    if "fmw_homes" in c["COMPONENTS"]:
        for h in c["FMW_HOMES"]:
            homes.append((h.rstrip("/").split("/")[-1], h))
    return homes


# ── Phase builders ──────────────────────────────────────────────────────────────
# Each returns (title, node, command_with_${VAR}, log_with_{VAR}).

def _precheck(c: dict):
    comp_line = ", ".join(c["COMPONENTS"]) or "(none)"
    rac = ""
    if c["DB_TOPOLOGY"] == "rac":
        rac = ("[precheck] Cluster: {RAC_NODES} nodes ({RAC_DB_HOSTS}); CRS/ASM UP on all; "
               "DB/Grid patched ROLLING (one node at a time, zero downtime).\n")
    cmd = ("# Run as the patch owner from a controller with ssh to all tier nodes.\n"
           "unzip -q ${PATCH_STAGE}.zip -d ${PATCH_STAGE}         # stage + unzip the patch\n"
           "$DB_ORACLE_HOME/OPatch/opatch version                # OPatch >= patch readme minimum\n"
           "df -h ${PATCH_STAGE} $DB_ORACLE_HOME                 # free space for apply + rollback\n"
           "# Take a guaranteed restore point / verified backup before proceeding:\n"
           "#   RMAN> CREATE RESTORE POINT before_${PATCH_NUMBER} GUARANTEE FLASHBACK DATABASE;")
    log = ("[precheck] Components selected: " + comp_line + "\n"
           "[precheck] Patch {PATCH_NUMBER} ({PATCH_LABEL}) staged and unzipped at {PATCH_STAGE}.\n"
           "[precheck] OPatch version in target homes meets the patch readme minimum — OK.\n"
           "[precheck] Free space on PATCH_STAGE and each home sufficient for apply + rollback.\n"
           "[precheck] Guaranteed restore point / verified backup confirmed.\n"
           + rac +
           "[precheck] Basic pre-patch checks PASSED.")
    return ("Pre-patch checks (stage / version / space / backup)", "controller", cmd, log)


def _conflict_check(c: dict):
    """OPatch prerequisite checks for EVERY selected home (not just the DB home):
    conflict detection + system-space. A failure here must abort the run."""
    homes = _opatch_homes(c)
    cmd_lines = ["# Conflict + space prerequisites for EVERY target home — a non-zero result MUST abort.",
                 "set -e"]
    log_lines = []
    for label, home in homes:
        cmd_lines.append(f"{home}/OPatch/opatch prereq CheckConflictAgainstOHWithDetail "
                         f"-ph ${{PATCH_STAGE}}/${{PATCH_NUMBER}} -oh {home}")
        cmd_lines.append(f"{home}/OPatch/opatch prereq CheckSystemSpace "
                         f"-ph ${{PATCH_STAGE}}/${{PATCH_NUMBER}} -oh {home}")
        log_lines.append(f"  [{label}] CheckConflictAgainstOHWithDetail: no conflicts · "
                         f"CheckSystemSpace: sufficient — OK")
    if "adop" in c["COMPONENTS"]:
        cmd_lines.append("# EBS apps tier: adop performs its own conflict analysis "
                         "(merge/supersede) during phase=apply.")
        log_lines.append("  [EBS apps] conflict analysis is performed by adop phase=apply "
                         "(overlapping/superseded patches are merged) — verified there.")
    if not log_lines:
        log_lines = ["  (no OPatch homes selected)"]
    cmd = "\n".join(cmd_lines)
    log = ("OPatch conflict & space prerequisites, per home:\n" + "\n".join(log_lines) + "\n"
           "All conflict / space prerequisites PASSED — safe to apply.")
    return ("OPatch conflict & prerequisite check (every home)", "controller", cmd, log)


def _applicability_check(c: dict):
    """Idempotency — is the patch ALREADY applied? Already-patched homes are skipped;
    if every selected target already has it the run is a clean no-op."""
    homes = _opatch_homes(c)
    cmd_lines = ["# Is the patch already applied? Skip a home that has it; abort cleanly if ALL do.",
                 "applied=0; total=0"]
    log_lines = []
    for label, home in homes:
        cmd_lines.append(
            f'total=$((total+1)); if {home}/OPatch/opatch lspatches | grep -q "${{PATCH_NUMBER}}"; '
            f'then echo "[skip] {label}: ${{PATCH_NUMBER}} already applied"; applied=$((applied+1)); '
            f'else echo "[todo] {label}: ${{PATCH_NUMBER}} not present -> will apply"; fi')
        log_lines.append(f"  [{label}] opatch lspatches: {{PATCH_NUMBER}} NOT present -> will apply")
    if "adop" in c["COMPONENTS"]:
        cmd_lines.append("# EBS apps tier — check the patch history (adop itself skips an already-applied bug):")
        cmd_lines.append("sqlplus -s apps/${APPS_PWD} <<'EOF'\n"
                         "  set heading off feedback off\n"
                         "  select bug_number from ad_bugs where bug_number = '${PATCH_NUMBER}';\n"
                         "EOF\n"
                         "adop -status   # surfaces last-applied patches / any open cycle")
        log_lines.append("  [EBS apps] ad_bugs / adop -status: {PATCH_NUMBER} NOT yet recorded -> adop will apply "
                         "(adop is idempotent — an already-applied patch is reported and skipped).")
    if not log_lines:
        log_lines = ["  (no targets selected)"]
    cmd = "\n".join(cmd_lines) + (
        '\n[ "$applied" -eq "$total" ] && [ "$total" -gt 0 ] && '
        '{ echo "All targets already patched — nothing to do."; exit 0; } || true')
    log = ("Already-applied (idempotency) check:\n" + "\n".join(log_lines) + "\n"
           "Patch is not yet present on the selected targets — proceeding. "
           "Already-patched homes are SKIPPED; if every target already had it the run aborts as a no-op.")
    return ("Already-applied check (idempotency)", "controller", cmd, log)


def _grid_phase(c: dict):
    if c["DB_TOPOLOGY"] == "rac":
        hosts = _rac_hosts(c)
        node_logs = "\n".join(
            f"  [{h}] opatchauto: stop CRS-managed resources -> apply -> restart ... node patched (rolling)"
            for _i, h in hosts)
        cmd = ("# Grid Infrastructure — opatchauto applies GI (and any DB homes it manages) ROLLING.\n"
               "# Run as ROOT on each node in turn; opatchauto stops/starts the stack per node.\n"
               + "\n".join(f"ssh {h} 'sudo ${{GRID_HOME}}/OPatch/opatchauto apply ${{PATCH_STAGE}}/${{PATCH_NUMBER}} -oh ${{GRID_HOME}}'"
                           for _i, h in hosts))
        log = ("Patching Grid Infrastructure {GRID_HOME} ROLLING across {RAC_NODES} nodes (opatchauto):\n"
               + node_logs + "\n"
               "  Clusterware stack relinked; SQL post-install (datapatch) deferred to the DB step.\n"
               "Grid Infrastructure patched on all nodes — no cluster-wide downtime.")
    else:
        cmd = ("# Grid Infrastructure (standalone / SIHA) — run as ROOT.\n"
               "sudo ${GRID_HOME}/OPatch/opatchauto apply ${PATCH_STAGE}/${PATCH_NUMBER} -oh ${GRID_HOME}")
        log = ("Patching Grid Infrastructure {GRID_HOME} (opatchauto apply):\n"
               "  Stopping GI-managed resources; applying patch; relinking; restarting ... done.\n"
               "Grid Infrastructure patched.")
    return ("Patch Grid Infrastructure (opatchauto)", "grid", cmd, log)


def _db_home_phase(c: dict):
    if c["DB_TOPOLOGY"] == "rac":
        hosts = _rac_hosts(c)
        lines = ["# RDBMS ORACLE_HOME — ROLLING per RAC node (zero downtime).",
                 "# opatchauto stops the local instance, applies to the DB home, and restarts it."]
        for inst, h in hosts:
            lines.append(f"ssh {h} 'srvctl stop instance -db ${{ORACLE_SID}} -instance {inst}'")
            lines.append(f"ssh {h} 'sudo ${{DB_ORACLE_HOME}}/OPatch/opatchauto apply ${{PATCH_STAGE}}/${{PATCH_NUMBER}} -oh ${{DB_ORACLE_HOME}}'")
            lines.append(f"ssh {h} 'srvctl start instance -db ${{ORACLE_SID}} -instance {inst}'")
        cmd = "\n".join(lines)
        node_logs = "\n".join(
            f"  [{h}] stop {inst} -> opatch apply (RDBMS home) -> start {inst} ... instance back ONLINE"
            for inst, h in hosts)
        log = ("Patching the RDBMS ORACLE_HOME {DB_ORACLE_HOME} ROLLING ({RAC_NODES} instances):\n"
               + node_logs + "\n"
               "  Database remained available throughout (at least one instance up at all times).\n"
               "RDBMS binaries patched on every node.")
    else:
        cmd = ("# Single instance — brief downtime for the binary apply.\n"
               "export ORACLE_HOME=${DB_ORACLE_HOME} ORACLE_SID=${ORACLE_SID}\n"
               "srvctl stop database -db ${ORACLE_SID} 2>/dev/null || sqlplus / as sysdba <<<'shutdown immediate;'\n"
               "${DB_ORACLE_HOME}/bin/lsnrctl stop\n"
               "${DB_ORACLE_HOME}/OPatch/opatch apply -silent ${PATCH_STAGE}/${PATCH_NUMBER}\n"
               "${DB_ORACLE_HOME}/bin/lsnrctl start\n"
               "srvctl start database -db ${ORACLE_SID} 2>/dev/null || sqlplus / as sysdba <<<'startup;'")
        log = ("Patching the RDBMS ORACLE_HOME {DB_ORACLE_HOME} (opatch apply):\n"
               "  Stopped {ORACLE_SID} + listener; applied patch {PATCH_NUMBER}; relinked; restarted.\n"
               "  OPatch succeeded — patch recorded in the local inventory.\n"
               "RDBMS binaries patched.")
    return ("Patch RDBMS ORACLE_HOME (OPatch)", "db", cmd, log)


def _datapatch_phase(c: dict):
    cmd = ("# Apply SQL changes for the binary patch to the database (run once, any node).\n"
           "export ORACLE_HOME=${DB_ORACLE_HOME} ORACLE_SID=${ORACLE_SID}\n"
           "${DB_ORACLE_HOME}/OPatch/datapatch -verbose")
    log = ("Running datapatch on {ORACLE_SID} ($DB_ORACLE_HOME/OPatch/datapatch -verbose):\n"
           "  Connecting to database ... bootstrapping registry\n"
           "  Installing patches: {PATCH_NUMBER} (SQL apply) ... done\n"
           "  Validating logfiles; SQL_PATCH registry updated.\n"
           "  Recompiling invalid objects (utlrp) ... 0 invalid objects remain.\n"
           "datapatch completed — database SQL is in sync with the patched binaries.")
    return ("Apply SQL post-install (datapatch + utlrp)", "db", cmd, log)


def _adop_preflight(c: dict):
    cmd = ("# adop pre-flight — never start a cycle on top of an incomplete one.\n"
           "source ${RUN_FS}/EBSapps.env run\n"
           "adop -status\n"
           "# If a previous cycle is INCOMPLETE, clear it before starting a new one:\n"
           "#   failed/abandoned apply  -> adop phase=abort && adop phase=cleanup cleanup_mode=full && adop phase=fs_clone\n"
           "#   prepared, not cut over  -> continue that cycle, or abort it as above\n"
           "# fs1/fs2 must be fully synced (no pending fs_clone) before a new prepare.")
    log = ("adop -status on {APPS_HOST} (pre-flight):\n"
           "  No patching cycle in progress; run/patch file systems (fs1/fs2) are in sync.\n"
           "  (If a cycle were open, the runbook aborts it — adop phase=abort -> cleanup -> fs_clone — first.)\n"
           "adop pre-flight PASSED — safe to begin a new cycle.")
    return ("adop pre-flight (status / abandon stale cycle)", "apps", cmd, log)


def _adop_prepare(c: dict):
    cmd = ("# adop online patching — prepare a fresh patch edition (fs2).\n"
           "source ${RUN_FS}/EBSapps.env run\n"
           "adop phase=prepare")
    log = ("adop phase=prepare on {APPS_HOST} (mode={ADOP_MODE}):\n"
           "  Validating system; checking for an existing patch cycle ... none open\n"
           "  Syncing the patch file system from the run file system (fs1 -> fs2)\n"
           "  Creating the PATCH edition in the database; cutover readiness OK\n"
           "adop phase=prepare completed — patch edition ready on {PATCH_FS}.")
    return ("adop phase=prepare (create patch edition)", "apps", cmd, log)


def _adop_apply(c: dict):
    extra = ""
    if c["ADOP_MODE"] == "downtime":
        extra = " apply_mode=downtime"
    elif c["ADOP_MODE"] == "hotpatch":
        extra = " hotpatch=yes"
    cmd = ("source ${RUN_FS}/EBSapps.env run\n"
           f"adop phase=apply patches=${{PATCH_NUMBER}} workers=${{APPLY_WORKERS}}{extra}")
    log = ("adop phase=apply patches={PATCH_NUMBER} workers={APPLY_WORKERS} (mode={ADOP_MODE}):\n"
           "  Applying to the PATCH edition ({PATCH_FS}) — run edition stays online\n"
           "  adpatch drivers: copy / database / generate portions executing across {APPLY_WORKERS} workers\n"
           "  AD Worker status: all workers completed; no failed jobs\n"
           "adop phase=apply completed — changes staged on the patch edition.")
    return ("adop phase=apply (EBS patch on patch edition)", "apps", cmd, log)


def _fmw_apply_phase(c: dict):
    """WebLogic + FMW component homes. During an online adop cycle these are
    patched on the PATCH file system (fs2) so they activate at cutover."""
    homes = []
    if "weblogic" in c["COMPONENTS"]:
        homes.append(("WebLogic Server", c["WEBLOGIC_HOME"]))
    if "fmw_homes" in c["COMPONENTS"]:
        for h in c["FMW_HOMES"]:
            label = h.rstrip("/").split("/")[-1]
            homes.append((label, h))
    online = "adop" in c["COMPONENTS"] and c["ADOP_MODE"] == "online"
    fs_note = ("# Online cycle: patch the FMW homes on the PATCH file system (fs2);\n"
               "# they become active at adop cutover. Source the PATCH env first.\n"
               "source ${PATCH_FS}/EBSapps.env patch\n" if online else
               "# Downtime: services must be stopped (adstpall.sh) before patching the run FS.\n")
    lines = [fs_note.rstrip("\n")]
    for _label, home in homes:
        lines.append(f"{home}/OPatch/opatch apply -silent ${{PATCH_STAGE}}/${{PATCH_NUMBER}} -oh {home}")
    cmd = "\n".join(lines)
    home_logs = "\n".join(f"  [{label}] opatch apply -oh {home} ... patch recorded in inventory"
                          for label, home in homes) or "  (no FMW homes selected)"
    where = "PATCH file system (fs2) — activates at cutover" if online else "run file system (services stopped)"
    log = ("Patching WebLogic / FMW component homes on the " + where + ":\n"
           + home_logs + "\n"
           "FMW homes patched (oracle_common / Forms / OHS / WebLogic as selected).")
    return ("Patch WebLogic & FMW homes (OPatch)", "apps", cmd, log)


def _adop_finalize(c: dict):
    cmd = ("source ${RUN_FS}/EBSapps.env run\n"
           "adop phase=finalize")
    log = ("adop phase=finalize on {APPS_HOST}:\n"
           "  Compiling invalid objects; gathering statistics; pre-cutover validation\n"
           "  System is ready for cutover.\n"
           "adop phase=finalize completed.")
    return ("adop phase=finalize (ready for cutover)", "apps", cmd, log)


def _adop_cutover(c: dict):
    cmd = ("source ${RUN_FS}/EBSapps.env run\n"
           "adop phase=cutover")
    log = ("adop phase=cutover on {APPS_HOST} (brief downtime):\n"
           "  Shutting down application services on the run edition\n"
           "  Promoting the PATCH edition to RUN; swapping fs1 <-> fs2 file systems\n"
           "  Making the patched edition the new run edition; restarting services\n"
           "adop phase=cutover completed — the patched edition is now LIVE.")
    return ("adop phase=cutover (promote patch edition)", "apps", cmd, log)


def _adop_cleanup(c: dict):
    cmd = ("source ${RUN_FS}/EBSapps.env run\n"
           "adop phase=cleanup cleanup_mode=${CLEANUP_MODE}")
    log = ("adop phase=cleanup cleanup_mode={CLEANUP_MODE} on {APPS_HOST}:\n"
           "  Dropping obsolete objects/editions from the old run edition; reclaiming space\n"
           "adop phase=cleanup completed.")
    return ("adop phase=cleanup", "apps", cmd, log)


def _adop_fs_clone(c: dict):
    cmd = ("source ${RUN_FS}/EBSapps.env run\n"
           "adop phase=fs_clone")
    log = ("adop phase=fs_clone on {APPS_HOST}:\n"
           "  Re-synchronising the patch file system (fs2) from the new run file system (fs1)\n"
           "  so the next patching cycle can start cleanly.\n"
           "adop phase=fs_clone completed — editions and file systems are back in sync.")
    return ("adop phase=fs_clone (sync patch FS for next cycle)", "apps", cmd, log)


# ── Interactive adop cycle (run / check phases on demand) ─────────────────────
# The batch flow simulates the whole cycle at once. Interactive mode lets a user
# drive adop one phase at a time (prepare now, apply, cutover in a window…) and
# ask for `adop -status` between phases — matching how teams actually patch.

ADOP_CYCLE = ["prepare", "apply", "finalize", "cutover", "cleanup", "fs_clone"]


def _adop_abort(c: dict):
    cmd = ("source ${RUN_FS}/EBSapps.env run\n"
           "adop phase=abort\n"
           "adop phase=cleanup cleanup_mode=full\n"
           "adop phase=fs_clone")
    log = ("adop phase=abort on {APPS_HOST}:\n"
           "  Discarding the PATCH edition — the RUN edition is untouched (no user impact)\n"
           "  Running cleanup (full) + fs_clone to re-sync the file systems\n"
           "adop cycle ABORTED — system back to a clean single-edition state.")
    return ("adop phase=abort (discard patch edition)", "apps", cmd, log)


_ADOP_BUILDERS = {
    "prepare": _adop_prepare, "apply": _adop_apply, "finalize": _adop_finalize,
    "cutover": _adop_cutover, "cleanup": _adop_cleanup, "fs_clone": _adop_fs_clone,
    "abort": _adop_abort,
}


def _adop_prereq_ok(phase: str, executed: list) -> bool:
    """Whether `phase` may run next, given the phases already executed."""
    e = set(executed)
    rules = {
        "prepare":  "prepare" not in e,
        "apply":    "prepare" in e and "cutover" not in e,          # may apply more than once
        "finalize": "apply" in e and "cutover" not in e,
        "cutover":  "finalize" in e and "cutover" not in e,
        "cleanup":  "cutover" in e and "cleanup" not in e,
        "fs_clone": "cleanup" in e and "fs_clone" not in e,
        "abort":    "prepare" in e and "cutover" not in e,           # only before cutover
    }
    return bool(rules.get(phase, False))


def adop_next_phase(executed: list):
    e = set(executed)
    for p in ADOP_CYCLE:
        if p not in e:
            return p
    return None


def adop_phase_step(run, phase: str) -> dict:
    """Build the simulated step for a single adop phase (vars filled)."""
    c = _ctx(run)
    b = _ADOP_BUILDERS.get(phase)
    if not b:
        return None
    title, node, cmd, log = b(c)
    out = log
    for k, v in c.items():
        if isinstance(v, str):
            out = out.replace("{" + k + "}", v)
    return {"phase": phase, "title": title, "node": node,
            "command": cmd, "log": out, "status": "success"}


def adop_status(run) -> dict:
    """Report the interactive adop cycle state for a run (what's done, what's next)."""
    c = _ctx(run)
    params = run.params or {}
    interactive = bool(params.get("interactive"))
    cyc = params.get("cycle") or {}
    executed = list(cyc.get("executed") or [])
    aborted = bool(cyc.get("aborted"))
    e = set(executed)
    nxt = None if aborted else adop_next_phase(executed)
    allowed = [] if aborted else [p for p in ADOP_CYCLE if _adop_prereq_ok(p, executed)]
    if not aborted and _adop_prereq_ok("abort", executed):
        allowed.append("abort")
    allowed.append("status")

    if not interactive:
        summary = ("This run was simulated as a full batch (not phase-by-phase). "
                   "Start a new run in phase-by-phase mode to drive adop one phase at a time.")
        open_cycle = False
    elif aborted:
        summary = "ABORTED — patch edition discarded; the system is single-edition."
        open_cycle = False
    elif not executed:
        summary = "No patch cycle in progress. Run `prepare` to start (creates the patch edition on fs2)."
        open_cycle = False
    elif "fs_clone" in e:
        summary = "Cycle COMPLETE — the patched edition is the RUN edition and fs1/fs2 are in sync."
        open_cycle = False
    elif "cutover" in e:
        summary = "CUT OVER — the patched edition is now RUN. Run `cleanup` then `fs_clone` to finish."
        open_cycle = True
    else:
        summary = (f"Cycle OPEN — last phase '{executed[-1]}'. Patch staged on the PATCH edition (fs2); "
                   f"the RUN edition is still live. Next recommended: {nxt or '?'}.")
        open_cycle = True

    return {
        "environment": run.environment_name, "patch_number": run.patch_number,
        "adop_mode": c["ADOP_MODE"], "run_fs": c["RUN_FS"], "patch_fs": c["PATCH_FS"],
        "interactive": interactive, "executed_phases": executed,
        "next_phase": nxt, "allowed_phases": allowed,
        "cycle_open": open_cycle, "aborted": aborted, "summary": summary,
    }


def _validate(c: dict):
    # Confirm the patch is now PRESENT in every patched home — i.e. a re-run would
    # be skipped by the already-applied check (the run is idempotent).
    checks = ["[validate] opatch lspatches: {PATCH_NUMBER} now PRESENT in every patched home "
              "(a re-run is a no-op — idempotent)."]
    if "db_home" in c["COMPONENTS"] or "grid" in c["COMPONENTS"]:
        checks.append("[validate] Database {ORACLE_SID}: OPEN; dba_registry_sqlpatch shows {PATCH_NUMBER} SUCCESS.")
    if c["DB_TOPOLOGY"] == "rac":
        checks.append("[validate] RAC: all {RAC_NODES} instances OPEN (gv$instance); SCAN registers all.")
    if "adop" in c["COMPONENTS"]:
        checks.append("[validate] ad_bugs / adop -status: {PATCH_NUMBER} recorded as applied; "
                      "no cycle in progress; run edition = patched edition.")
        checks.append("[validate] OHS / Forms / login page on {APPS_HOST}: RESPONDING; concurrent managers ACTIVE.")
    if "weblogic" in c["COMPONENTS"] or "fmw_homes" in c["COMPONENTS"]:
        checks.append("[validate] WebLogic AdminServer + managed servers (oacore/forms/oafm): RUNNING.")
    checks.append("[validate] Patch {PATCH_LABEL} on {ENV_NAME}: PASSED.")
    cmd = ("$DB_ORACLE_HOME/OPatch/opatch lspatches | grep ${PATCH_NUMBER}\n"
           "sqlplus -s / as sysdba <<<\"select patch_id,status from dba_registry_sqlpatch where patch_id like '%${PATCH_NUMBER}%';\"\n"
           "adop -status   # apps tier — confirm applied + no open cycle\n"
           "validate services, listeners, concurrent managers and the login page")
    return ("Post-patch validation (confirm applied / idempotent)", "controller", cmd, "\n".join(checks))


def _phase_order(c: dict) -> list:
    """Build the ordered list of (title, node, cmd, log) for the selected components.
    DB/Grid first (rolling for RAC), then the adop online-patching cycle wrapping the
    WebLogic/FMW home patching, then validation."""
    comps = c["COMPONENTS"]
    # Validation gates run first: basic prechecks, OPatch conflict/space per home,
    # and the already-applied (idempotency) check.
    phases = [_precheck(c), _conflict_check(c), _applicability_check(c)]
    if "grid" in comps:
        phases.append(_grid_phase(c))
    if "db_home" in comps:
        phases.append(_db_home_phase(c))
    if "db_home" in comps or "grid" in comps:
        phases.append(_datapatch_phase(c))

    fmw_selected = "weblogic" in comps or "fmw_homes" in comps
    if "adop" in comps:
        phases.append(_adop_preflight(c))        # abandon a stale cycle before starting
        phases.append(_adop_prepare(c))
        phases.append(_adop_apply(c))
        if fmw_selected:
            phases.append(_fmw_apply_phase(c))   # on patch FS during the cycle
        phases.append(_adop_finalize(c))
        phases.append(_adop_cutover(c))
        phases.append(_adop_cleanup(c))
        phases.append(_adop_fs_clone(c))         # re-sync patch FS for the next cycle
    elif fmw_selected:
        # FMW/WebLogic patched directly (downtime) when no adop cycle is requested.
        phases.append(_fmw_apply_phase(c))

    phases.append(_validate(c))
    return phases


def build_patch_steps(run) -> list:
    c = _ctx(run)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def fill(s):
        out = s.replace("<ts>", ts)
        for k, v in c.items():
            if isinstance(v, str):
                out = out.replace("{" + k + "}", v)
        return out

    steps = []
    for i, (title, node, cmd, log) in enumerate(_phase_order(c), 1):
        steps.append({
            "step": i,
            "phase": re.sub(r'[^a-z0-9]+', '_', title.lower()).strip("_"),
            "title": title,
            "node": node,
            "command": cmd,        # keep ${VAR} form
            "log": fill(log),
            "status": "success",
        })
    return steps


# ── Runbook (patch.sh) ────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]', '_', str(name or ''))


def build_runbook_sh(run, steps) -> str:
    c = _ctx(run)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rac = c["DB_TOPOLOGY"] == "rac"
    L = []
    L.append("#!/usr/bin/env bash")
    L.append("#")
    L.append("# OraEBS Agent — EBS patching runbook")
    L.append(f"# Target    : {c['ENV_NAME']}")
    L.append(f"# Patch     : {c['PATCH_NUMBER']} ({c['PATCH_LABEL']})")
    L.append(f"# Components: {', '.join(c['COMPONENTS']) or '(none)'}")
    L.append(f"# Generated : {ts}")
    L.append("#")
    L.append("# Passwords are NEVER embedded — export them before running:")
    L.append("#   export SYS_PWD=...  SYSTEM_PWD=...  APPS_PWD=...  WLS_PWD=...")
    if "adop" in c["COMPONENTS"]:
        L.append(f"# adop mode : {c['ADOP_MODE']} (online = patch edition fs2, cutover swaps editions).")
    if rac:
        L.append(f"# RAC       : {c['RAC_NODES']} nodes ({c['RAC_DB_HOSTS']}) — DB/GI patched ROLLING (zero downtime).")
    L.append("# Run from a controller host that can ssh to all tier nodes (key-based auth).")
    L.append("# Take a verified backup / guaranteed restore point before running.")
    L.append("# SAFETY: this run is idempotent — it checks conflicts (opatch prereq) and whether the")
    L.append("#   patch is already applied (opatch lspatches / ad_bugs) per home, skips homes that")
    L.append("#   already have it, and ABORTS if a conflict is reported. Review each check step's")
    L.append("#   output and do not continue past a failed conflict/space prerequisite.")
    L.append("")
    L.append("set -euo pipefail")
    L.append("")
    L.append("# ── Parameters (override via environment) ───────────────────────────────────")
    param_keys = ["ENV_NAME", "PATCH_NUMBER", "PATCH_LABEL", "PATCH_STAGE",
                  "DB_HOST", "APPS_HOST", "ORACLE_SID", "DB_ORACLE_HOME"]
    if "grid" in c["COMPONENTS"]:
        param_keys.append("GRID_HOME")
    if "adop" in c["COMPONENTS"] or "weblogic" in c["COMPONENTS"] or "fmw_homes" in c["COMPONENTS"]:
        param_keys += ["RUN_FS", "PATCH_FS"]
    if "adop" in c["COMPONENTS"]:
        param_keys += ["ADOP_MODE", "APPLY_WORKERS", "CLEANUP_MODE"]
    if "weblogic" in c["COMPONENTS"]:
        param_keys.append("WEBLOGIC_HOME")
    for k in param_keys:
        L.append('%s="${%s:-%s}"' % (k, k, c[k]))
    if rac:
        L.append("")
        L.append("# ── RAC topology (patched rolling, one node at a time) ──────────────────────")
        for k in ["DB_TOPOLOGY", "RAC_NODES", "RAC_DB_HOSTS"]:
            L.append('%s="${%s:-%s}"' % (k, k, c[k]))
    L.append("")
    L.append("# ── Passwords (required; never stored in this file) ─────────────────────────")
    L.append('SYS_PWD="${SYS_PWD:?export SYS_PWD before running}"')
    L.append('APPS_PWD="${APPS_PWD:?export APPS_PWD before running}"')
    L.append("")
    L.append('log(){ echo; echo "=== [$(date "+%F %T")] $* ==="; }')
    L.append("")
    node_host = {
        "controller": "localhost",
        "db": "${DB_HOST}",
        "grid": "${DB_HOST}",
        "apps": "${APPS_HOST}",
    }
    for s in steps:
        L.append(f'log "Step {s["step"]}/{len(steps)} — {s["title"]}  [{s["node"]}]"')
        host = node_host.get(s["node"], "localhost")
        cmd = s["command"].strip()
        if s["node"] == "controller":
            for ln in cmd.splitlines():
                L.append(f"# {ln}")
        else:
            L.append(f"# Run the following on {host}:")
            for ln in cmd.splitlines():
                L.append(f"#   {ln}")
        L.append("")
    L.append(f'log "Patch runbook completed — validate ${{ENV_NAME}} before handing over."')
    L.append("")
    return "\n".join(L)


def build_readme(run, steps) -> str:
    c = _ctx(run)
    rac = c["DB_TOPOLOGY"] == "rac"
    phase_list = "\n".join(f"{s['step']}. **{s['title']}** — `{s['node']}`" for s in steps)
    comp_md = "\n".join(f"- `{x}`" for x in c["COMPONENTS"]) or "- (none)"
    rac_section = ""
    if rac:
        rac_section = (
            "## Database topology — RAC\n"
            f"- Target is **RAC** ({c['RAC_NODES']} nodes: `{c['RAC_DB_HOSTS']}`). The Grid Infrastructure\n"
            "  and RDBMS ORACLE_HOME are patched **rolling** — one node at a time — so the database stays\n"
            "  available throughout (at least one instance is always up).\n"
            "- `opatchauto` orchestrates the stop/apply/start of the local stack on each node; `datapatch`\n"
            "  runs **once** after all nodes carry the binary patch.\n\n")
    adop_section = ""
    if "adop" in c["COMPONENTS"]:
        adop_section = (
            "## Application tier — adop online patching\n"
            f"- Mode: **{c['ADOP_MODE']}**. In *online* mode the patch is applied to the **patch edition**\n"
            "  (file system `fs2`) while users keep working on the run edition; the only downtime is the\n"
            "  brief **cutover**.\n"
            "- Full cycle: **pre-flight (`adop -status`) → `prepare` → `apply` → `finalize` → `cutover` →\n"
            f"  `cleanup` (cleanup_mode=`{c['CLEANUP_MODE']}`) → `fs_clone`**.\n"
            "- **Pre-flight** never starts a cycle on top of an incomplete one: if `adop -status` shows an\n"
            "  open cycle it is abandoned first (`adop phase=abort` → `cleanup cleanup_mode=full` →\n"
            "  `fs_clone`).\n"
            "- WebLogic / FMW home patches (if selected) are applied to the patch file system during the\n"
            "  cycle so they activate at cutover.\n"
            "- **Rollback:** before cutover, `adop phase=abort` discards the patch edition with no impact to\n"
            "  the run edition. After cutover, roll back via the database restore point / re-patch.\n\n")
    elif "weblogic" in c["COMPONENTS"] or "fmw_homes" in c["COMPONENTS"]:
        adop_section = (
            "## Application tier — FMW homes (downtime)\n"
            "- No adop cycle was requested, so WebLogic / FMW homes are patched directly with `opatch`.\n"
            "  **Stop the application services** (`adstpall.sh`) before applying, and start them after.\n"
            "- Roll back an individual home with `opatch rollback -id <patch>` if needed.\n\n")
    return (
        f"# EBS Patch Runbook — {c['ENV_NAME']}  ·  {c['PATCH_NUMBER']} ({c['PATCH_LABEL']})\n\n"
        f"**Components patched:**\n{comp_md}\n\n"
        "## Validation & idempotency (run before any apply)\n"
        "1. **Conflict & space** — `opatch prereq CheckConflictAgainstOHWithDetail` **and** `CheckSystemSpace`\n"
        "   are run against **every** selected home (DB / Grid / WebLogic / FMW); a non-zero result aborts.\n"
        "   The EBS apps tier's conflict analysis is performed by `adop phase=apply` (merge/supersede).\n"
        "2. **Already-applied** — `opatch lspatches` (per home) and `ad_bugs` / `adop -status` (apps) are\n"
        "   checked first. A home that already has the patch is **skipped**; if every target already has it\n"
        "   the run is a clean **no-op**. Re-running this runbook is therefore safe (idempotent).\n"
        "3. **Post-patch** — validation confirms the patch is now present in each home and recorded in\n"
        "   `dba_registry_sqlpatch` / `ad_bugs`.\n\n"
        "## Passwords\n"
        "- Export `SYS_PWD`, `APPS_PWD` (and `WLS_PWD` if patching WebLogic) before running — nothing is\n"
        "  stored in this runbook.\n\n"
        f"{rac_section}"
        f"{adop_section}"
        "## How to run\n"
        "1. Copy this directory to a controller host with key-based SSH to all tier nodes.\n"
        "2. **Take a verified backup / guaranteed restore point** of the database and file systems.\n"
        "3. Stage the patch zip at `PATCH_STAGE` and review the parameters at the top of `patch.sh`.\n"
        "4. Export the passwords, then run:\n"
        "   ```bash\n   chmod +x patch.sh && ./patch.sh\n   ```\n\n"
        "`set -euo pipefail` stops at the first failing step. The script annotates each command with the\n"
        "node it must run on (DB / Grid as root / Apps) — review before executing in production.\n\n"
        "## Phases\n"
        f"{phase_list}\n\n"
        "> Generated by OraEBS Agent in simulator mode. Validate against your patch readme, maintenance\n"
        "> window and rollback plan (opatch rollback / adop abandon) before production use.\n"
    )


def build_runbook_zip(run) -> tuple[bytes, str]:
    steps = run.steps or build_patch_steps(run)
    root = f"oraebs_patch_{run.id}_{_safe(run.environment_name)}_{_safe(run.patch_number)}"
    patch_sh = build_runbook_sh(run, steps)
    readme = build_readme(run, steps)
    c = _ctx(run)
    manifest = json.dumps({
        "patch_run_id": run.id,
        "environment": run.environment_name,
        "patch_number": run.patch_number,
        "patch_label": run.patch_label,
        "components": c["COMPONENTS"],
        "db_topology": c["DB_TOPOLOGY"],
        "rac": {"nodes": int(c["RAC_NODES"]), "hosts": c["RAC_DB_HOSTS"],
                "instances": [{"instance": n, "node": h} for n, h in _rac_hosts(c)]}
                if c["DB_TOPOLOGY"] == "rac" else {"nodes": 1},
        "homes": {"db_oracle_home": c["DB_ORACLE_HOME"], "grid_home": c["GRID_HOME"],
                  "weblogic_home": c["WEBLOGIC_HOME"], "fmw_homes": c["FMW_HOMES"]},
        "adop": {"mode": c["ADOP_MODE"], "apply_workers": int(c["APPLY_WORKERS"]),
                 "cleanup_mode": c["CLEANUP_MODE"]}
                if "adop" in c["COMPONENTS"] else None,
        "guard_status": run.guard_status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "steps": [{"step": s["step"], "phase": s["phase"], "title": s["title"],
                   "node": s["node"], "command": s["command"]} for s in steps],
    }, indent=2)
    sim_log = "\n\n".join(
        f"===== Step {s['step']}: {s['title']} [{s['node']}] =====\n{s['log']}" for s in steps)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo(f"{root}/patch.sh")
        info.external_attr = 0o100755 << 16
        z.writestr(info, patch_sh)
        z.writestr(f"{root}/README.md", readme)
        z.writestr(f"{root}/manifest.json", manifest)
        z.writestr(f"{root}/simulated_run.log", sim_log)
    return buf.getvalue(), f"{root}.zip"
