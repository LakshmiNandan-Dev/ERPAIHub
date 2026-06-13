"""
EBS Cloning service.

Builds an Oracle EBS Rapid Clone plan (RMAN active-duplicate for the database
tier + Rapid Clone for the application tier), produces high-fidelity simulated
logs, and generates a portable, parameterised clone runbook (clone.sh). All
passwords are shell variables — never embedded.
"""
import io
import re
import json
import zipfile
from datetime import datetime, timezone


def _ctx(run) -> dict:
    """Resolved, non-secret parameters used in command rendering."""
    p = run.params or {}
    st = str(p.get("target_storage") or "").strip().lower()
    storage = "asm" if st.startswith("a") else "fs"
    # Whether the RDBMS ORACLE_HOME must be cloned from the source DB server
    # (target node has no DB home). Default: home already present (do NOT clone).
    tdh = str(p.get("target_db_home") or "").strip().lower()
    clone_home = tdh in ("no", "n", "false", "absent", "none", "missing", "clone")
    # Target DB topology — single instance or RAC. RAC needs shared ASM storage.
    topo = str(p.get("db_topology") or "").strip().lower()
    rac = topo.startswith("r") or "cluster" in topo
    if rac:
        storage = "asm"
    try:
        nodes = max(2, int(p.get("rac_node_count") or 2)) if rac else 1
    except (TypeError, ValueError):
        nodes = 2
    target_sid = run.target_sid or "CLONE"
    db_unique = p.get("rac_db_unique_name") or target_sid
    inst_prefix = p.get("rac_instance_prefix") or db_unique
    hosts = [h.strip() for h in str(p.get("rac_db_hosts") or "").split(",") if h.strip()]
    return {
        "CLONE_DB_HOME": "yes" if clone_home else "no",
        "SOURCE_NAME": run.source_name or "SOURCE",
        "TARGET_NAME": run.target_name or "TARGET",
        "SOURCE_SID": run.source_sid or "PROD",
        "TARGET_SID": target_sid,
        "SOURCE_DB_HOST": run.source_db_host or "src-db",
        "TARGET_DB_HOST": run.target_db_host or "tgt-db",
        "SOURCE_APPS_HOST": run.source_apps_host or "src-apps",
        "TARGET_APPS_HOST": run.target_apps_host or "tgt-apps",
        "SOURCE_ORACLE_HOME": p.get("source_oracle_home", "/u01/app/oracle/${SOURCE_SID}/db/tech_st/19.0.0"),
        "TARGET_ORACLE_HOME": p.get("target_oracle_home", "/u01/app/oracle/${TARGET_SID}/db/tech_st/19.0.0"),
        "SOURCE_APPL_TOP": p.get("source_appl_top", "/u01/app/oracle/${SOURCE_NAME}/apps/apps_st/appl"),
        "TARGET_BASE": p.get("target_base", "/u01/app/oracle/${TARGET_NAME}"),
        "TARGET_DB_PORT": str(p.get("target_db_port", 1521)),
        # storage layout
        "TARGET_STORAGE": storage,
        "ASM_DG_DATA": p.get("asm_dg") or "+DATA",
        "ASM_DG_RECO": p.get("asm_dg_reco") or "+RECO",
        "SOURCE_DATAFILE_DIR": p.get("source_datafile_dir") or "/u01/app/oracle/oradata/${SOURCE_SID}",
        "TARGET_DATAFILE_DIR": p.get("target_datafile_dir") or "/u01/app/oracle/oradata/${TARGET_SID}",
        # RAC topology
        "DB_TOPOLOGY": "rac" if rac else "single",
        "RAC_NODES": str(nodes),
        "RAC_DB_HOSTS": ",".join(hosts) if hosts else "${TARGET_DB_HOST}",
        "RAC_DB_UNIQUE_NAME": db_unique,
        "RAC_INSTANCE_PREFIX": inst_prefix,
        "SCAN_NAME": p.get("scan_name") or "${TARGET_NAME}-scan",
        "GRID_HOME": p.get("grid_home") or "/u01/app/19.0.0/grid",
    }


def _rac_instances(c: dict) -> list:
    """Return [(instance_name, node_host, thread, undo_ts), ...] for the RAC nodes."""
    n = int(c["RAC_NODES"])
    hosts = [h.strip() for h in str(c["RAC_DB_HOSTS"]).split(",") if h.strip()]
    prefix = c["RAC_INSTANCE_PREFIX"]
    out = []
    for i in range(1, n + 1):
        host = hosts[i - 1] if i - 1 < len(hosts) else f"{c['TARGET_DB_HOST']}{i}"
        out.append((f"{prefix}{i}", host, i, f"UNDOTBS{i}"))
    return out


# ── Phase library ────────────────────────────────────────────────────────────────
# key -> (title, node, command_template, log_template). The active sequence is
# chosen by _phase_order() so the RDBMS ORACLE_HOME can be cloned when the target
# server has no database home.

_PHASE_DEFS = {
    "precheck": ("Pre-clone checks (incl. password validation)", "controller",
        "verify reachability, versions, space, ARCHIVELOG, and SYS password on source + target auxiliary",
        "<overridden>"),

    "src_apps_preclone": ("Source apps-tier pre-clone", "source-apps",
        "cd $ADMIN_SCRIPTS_HOME && perl adpreclone.pl appsTier",
        "Running adpreclone.pl appsTier on {SOURCE_APPS_HOST} ...\n"
        "  Creating stage area $COMMON_TOP/clone/...\n"
        "  StageAppsTier: collecting context, templates and APPL_TOP driver files\n"
        "  Generating fwkclone, jdkclone, techstack stage\n"
        "adpreclone.pl appsTier completed successfully."),

    # ── DB ORACLE_HOME clone (only when the target has no database home) ──────────
    "src_db_preclone": ("Source DB-tier pre-clone (stage the RDBMS ORACLE_HOME)", "source-db",
        "cd $ORACLE_HOME/appsutil/scripts/${SOURCE_SID}_${SOURCE_DB_HOST} && perl adpreclone.pl dbTier",
        "Running adpreclone.pl dbTier on {SOURCE_DB_HOST} ...\n"
        "  Preparing the DATABASE ORACLE_HOME (RDBMS tech stack) for cloning\n"
        "  Creating stage $ORACLE_HOME/appsutil/clone (home driver, inventory, tech-stack templates)\n"
        "adpreclone.pl dbTier completed — the Oracle Home is staged for cloning."),

    "db_home_copy": ("Clone the RDBMS ORACLE_HOME from the source DB server", "controller",
        "# Target node has NO database ORACLE_HOME — ship the RDBMS binaries from the source DB node.\n"
        "ssh ${TARGET_DB_HOST} 'mkdir -p ${TARGET_ORACLE_HOME}'\n"
        "rsync -az ${SOURCE_DB_HOST}:${SOURCE_ORACLE_HOME}/ ${TARGET_DB_HOST}:${TARGET_ORACLE_HOME}/",
        "Cloning the DATABASE ORACLE_HOME (RDBMS binaries):\n"
        "  {SOURCE_DB_HOST}:{SOURCE_ORACLE_HOME}\n"
        "  -> {TARGET_DB_HOST}:{TARGET_ORACLE_HOME}\n"
        "  sending incremental file list (bin, lib, rdbms, network, appsutil/clone stage)\n"
        "  sent 7.8G  received 0.9M bytes  total size 7.8G\n"
        "ORACLE_HOME binaries transferred to the target node."),

    "db_home_config": ("Configure the cloned ORACLE_HOME (relink + inventory + root.sh)", "target-db",
        "cd ${TARGET_ORACLE_HOME}/appsutil/clone/bin && perl adcfgclone.pl dbTechStack",
        "Configuring the cloned ORACLE_HOME on {TARGET_DB_HOST} (adcfgclone.pl dbTechStack) ...\n"
        "  Attaching the Oracle Home to the central inventory (oraInventory)\n"
        "  Relinking RDBMS executables (relink all) ... done\n"
        "  Running root.sh (ownership, oraenv, dbhome, oradism) ... done\n"
        "  Creating DB context file $ORACLE_HOME/appsutil/{TARGET_SID}_{TARGET_DB_HOST}.xml and listener\n"
        "adcfgclone.pl dbTechStack completed — the target ORACLE_HOME is configured and ready."),

    "target_aux_prep": ("Prepare target auxiliary (SYS password file + TNS)", "target-db",
        "<overridden>", "<overridden>"),

    "rman_duplicate": ("Database clone — RMAN active duplicate", "target-db",
        "<overridden>", "<overridden>"),

    # ── RAC conversion (only when target topology is RAC) ────────────────────────
    "rac_convert": ("Convert the duplicated database to RAC", "target-db",
        "<overridden>", "<overridden>"),

    "rac_register": ("Register the RAC database & instances with Grid Infrastructure", "target-db",
        "<overridden>", "<overridden>"),

    "tgt_db_config": ("Target DB tech-stack configuration", "target-db",
        "cd $ORACLE_HOME/appsutil/clone/bin && perl adcfgclone.pl dbTechStack",
        "Running adcfgclone.pl dbTechStack on {TARGET_DB_HOST} ...\n"
        "  Target system name : {TARGET_SID}   port pool/listener : {TARGET_DB_PORT}\n"
        "  Creating context file $ORACLE_HOME/appsutil/${TARGET_SID}_${TARGET_DB_HOST}.xml\n"
        "  Configuring listener {TARGET_SID}; running AutoConfig on the DB tier\n"
        "adcfgclone.pl dbTechStack completed successfully."),

    "apps_fs_copy": ("Copy application file system (apps tier only)", "source-apps",
        "rsync -az --delete ${SOURCE_APPL_TOP}/ ${TARGET_APPS_HOST}:${TARGET_BASE}/apps/apps_st/appl/",
        "Copying the APPLICATION file system (APPL_TOP / COMMON_TOP / OA tech-stack) "
        "from {SOURCE_APPS_HOST} to {TARGET_APPS_HOST} ...\n"
        "  (Database ORACLE_HOME is handled separately — RMAN duplicate, or cloned binaries when absent.)\n"
        "  sending incremental file list\n"
        "  $APPL_TOP/ ... $COMMON_TOP/ ... $ORACLE_HOME(10.1.2/web) ...\n"
        "  sent 18.4G  received 1.2M  bytes  total size 18.4G\n"
        "Application file system copy completed."),

    "tgt_apps_config": ("Target apps-tier configuration", "target-apps",
        "cd ${TARGET_BASE}/apps/apps_st/comn/clone/bin && perl adcfgclone.pl appsTier",
        "Running adcfgclone.pl appsTier on {TARGET_APPS_HOST} ...\n"
        "  Target system name : {TARGET_NAME}\n"
        "  Apps user/password and ports collected; building new context file\n"
        "  Relinking ADX/FND executables; configuring OHS, Forms, Concurrent Manager\n"
        "  Running AutoConfig on the apps tier\n"
        "adcfgclone.pl appsTier completed successfully."),

    "autoconfig": ("AutoConfig (DB + apps)", "controller",
        "$ADMIN_SCRIPTS_HOME/adautocfg.sh   # run on DB and apps tiers",
        "Running AutoConfig on {TARGET_DB_HOST} (dbTier) ... completed.\n"
        "Running AutoConfig on {TARGET_APPS_HOST} (appsTier) ... completed.\n"
        "AutoConfig completed successfully on all tiers."),

    "validate": ("Post-clone validation", "controller",
        "validate services, listener, concurrent managers and login page",
        "[validate] Listener {TARGET_SID} : UP\n"
        "[validate] Database {TARGET_SID} : OPEN, APPS schema reachable\n"
        "[validate] Internal Concurrent Manager : ACTIVE\n"
        "[validate] OHS / Forms / login page (http://{TARGET_APPS_HOST}) : RESPONDING\n"
        "[validate] Clone {SOURCE_NAME} -> {TARGET_NAME} PASSED."),
}


def _phase_order(clone_home: bool, rac: bool = False) -> list:
    """Ordered phase keys. When the target has no DB ORACLE_HOME, stage + ship +
    configure the RDBMS home before preparing the auxiliary. For a RAC target the
    database is duplicated single-instance, then converted and registered with Grid
    Infrastructure; the RAC conversion supersedes the single-instance tgt_db_config."""
    seq = ["precheck", "src_apps_preclone"]
    if clone_home:
        seq += ["src_db_preclone", "db_home_copy", "db_home_config"]
    seq += ["target_aux_prep", "rman_duplicate"]
    if rac:
        seq += ["rac_convert", "rac_register"]
        if not clone_home:
            seq += ["tgt_db_config"]   # EBS dbTechStack on the primary RAC node
    elif not clone_home:
        seq += ["tgt_db_config"]
    seq += ["apps_fs_copy", "tgt_apps_config", "autoconfig", "validate"]
    return seq


def _rac_convert_block(c: dict):
    """(command, log) — convert the freshly duplicated single instance to RAC:
    shared spfile in ASM, per-instance redo threads + undo tablespaces, cluster
    parameters, then the RAC dictionary (catclust.sql)."""
    insts = _rac_instances(c)
    dg = c["ASM_DG_DATA"]
    uniq = c["RAC_DB_UNIQUE_NAME"]
    add_lines, log_inst = [], []
    for name, host, thread, undo in insts:
        if thread > 1:   # thread 1 / undo already exist from the duplicate
            add_lines.append(f"CREATE UNDO TABLESPACE {undo} DATAFILE SIZE 4G AUTOEXTEND ON;")
            add_lines.append(f"ALTER DATABASE ADD LOGFILE THREAD {thread} "
                             f"GROUP {thread}0 SIZE 512M, GROUP {thread}1 SIZE 512M;")
            add_lines.append(f"ALTER DATABASE ENABLE PUBLIC THREAD {thread};")
        add_lines.append(f"ALTER SYSTEM SET INSTANCE_NUMBER={thread} SCOPE=SPFILE SID='{name}';")
        add_lines.append(f"ALTER SYSTEM SET THREAD={thread} SCOPE=SPFILE SID='{name}';")
        add_lines.append(f"ALTER SYSTEM SET UNDO_TABLESPACE={undo} SCOPE=SPFILE SID='{name}';")
        log_inst.append(f"  instance {name} -> node {host}: thread {thread}, {undo}, redo thread {thread}")
    sql = "\n".join(add_lines)
    cmd = (
        "# The database was duplicated single-instance (CLUSTER_DATABASE=FALSE).\n"
        "# Move the SPFILE to shared ASM and add per-instance RAC parameters.\n"
        "export ORACLE_SID=" + insts[0][0] + "\n"
        "sqlplus / as sysdba <<EOF\n"
        f"CREATE SPFILE='{dg}/{uniq}/spfile{uniq}.ora' FROM PFILE;\n"
        "ALTER SYSTEM SET CLUSTER_DATABASE=TRUE SCOPE=SPFILE;\n"
        f"ALTER SYSTEM SET CLUSTER_DATABASE_INSTANCES={len(insts)} SCOPE=SPFILE;\n"
        f"{sql}\n"
        "@?/rdbms/admin/catclust.sql\n"
        "SHUTDOWN IMMEDIATE;\nEOF\n"
        f"# point each node's $ORACLE_HOME/dbs/init<inst>.ora at {dg}/{uniq}/spfile{uniq}.ora")
    log = ("Converting the duplicated database to RAC ({RAC_NODES} instances, db_unique_name {RAC_DB_UNIQUE_NAME}):\n"
           f"  Created shared SPFILE {dg}/{uniq}/spfile{uniq}.ora\n"
           "  CLUSTER_DATABASE=TRUE; per-instance parameters set:\n"
           + "\n".join(log_inst) + "\n"
           "  Ran catclust.sql to build the RAC (GV$) data dictionary.\n"
           "Database converted to RAC.")
    return cmd, log


def _rac_register_block(c: dict):
    """(command, log) — register the RAC database + instances with srvctl and start it."""
    insts = _rac_instances(c)
    uniq = c["RAC_DB_UNIQUE_NAME"]
    lines = [f"srvctl add database -db {uniq} -oraclehome ${{TARGET_ORACLE_HOME}} "
             f"-spfile {c['ASM_DG_DATA']}/{uniq}/spfile{uniq}.ora -dbname ${{TARGET_SID}} -role PRIMARY"]
    for name, host, _thread, _undo in insts:
        lines.append(f"srvctl add instance -db {uniq} -instance {name} -node {host}")
    lines.append(f"srvctl start database -db {uniq}")
    lines.append(f"srvctl status database -db {uniq}")
    cmd = "# Run from one RAC node as the Grid/oracle owner.\n" + "\n".join(lines)
    log = ("Registering the RAC database with Grid Infrastructure (srvctl):\n"
           f"  srvctl add database -db {uniq} (oraclehome, shared spfile)\n"
           + "\n".join(f"  + instance {n} on node {h}" for n, h, _t, _u in insts) + "\n"
           f"  srvctl start database -db {uniq} ... all {len(insts)} instances OPEN\n"
           "  SCAN listener {SCAN_NAME} registers all instances; load-balanced service is available.\n"
           "RAC database registered and started.")
    return cmd, log


def _target_aux_prep_block(clone_home: bool, rac: bool = False):
    """(command, log) for the auxiliary prep — wording depends on whether the
    ORACLE_HOME was just cloned or was already present, and whether the target is RAC
    (the duplicate runs single-instance on the first node, CLUSTER_DATABASE=FALSE)."""
    cmd_head = ("# Using the ORACLE_HOME just cloned from the source DB node.\n"
                if clone_home else
                "# Target DB ORACLE_HOME must already be installed — binaries are NOT cloned.\n")
    rac_cmd = ("# RAC target: prepare the auxiliary on the FIRST node only, single-instance\n"
               "# (CLUSTER_DATABASE=FALSE). It is converted to RAC after the duplicate.\n" if rac else "")
    cmd = (cmd_head + rac_cmd +
           "# Active duplicate requires the auxiliary SYS password to MATCH the source.\n"
           "orapwd file=$ORACLE_HOME/dbs/orapw${TARGET_SID} password=${SYS_PWD} force=y\n"
           "# add listener + tnsnames.ora entries for ${SOURCE_SID} and ${TARGET_SID}, then:\n"
           "export ORACLE_SID=${TARGET_SID}\n"
           "sqlplus / as sysdba <<EOF\nSTARTUP NOMOUNT PFILE=$ORACLE_HOME/dbs/init${TARGET_SID}.ora;\nEOF")
    log_head = ("Using the freshly cloned ORACLE_HOME on {TARGET_DB_HOST}.\n"
                if clone_home else
                "Verifying target DB ORACLE_HOME is installed on {TARGET_DB_HOST} (binaries NOT cloned) ... present.\n")
    rac_log = ("RAC target: preparing the auxiliary single-instance on the first node "
               "(CLUSTER_DATABASE=FALSE; converted to RAC after duplicate).\n" if rac else "")
    log = (log_head + rac_log +
           "Creating auxiliary password file orapw{TARGET_SID} with the SOURCE SYS password.\n"
           "  (RMAN active duplicate authenticates source + auxiliary via matching SYS password files.)\n"
           "Adding TNS entries for {SOURCE_SID} and {TARGET_SID}; starting auxiliary {TARGET_SID} in NOMOUNT.\n"
           "Target auxiliary instance ready for RMAN duplicate.")
    return cmd, log


def _rman_block(c: dict):
    """Storage-aware RMAN active-duplicate. Returns (command [${VAR}], log [{VAR}])."""
    rac = c["DB_TOPOLOGY"] == "rac"
    uniq = "${RAC_DB_UNIQUE_NAME}" if rac else "${TARGET_SID}"
    head = ('rman target sys/${SYS_PWD}@${SOURCE_SID} auxiliary sys/${SYS_PWD}@${TARGET_SID} <<EOF\n'
            'DUPLICATE TARGET DATABASE TO ${TARGET_SID} FROM ACTIVE DATABASE\n'
            "  SPFILE SET DB_NAME='${TARGET_SID}' SET DB_UNIQUE_NAME='" + uniq + "'\n")
    if rac:
        # Duplicate single-instance; RAC parameters are applied in the rac_convert phase.
        head += "    SET CLUSTER_DATABASE='FALSE'\n"
    if c["TARGET_STORAGE"] == "asm":
        cmd = (head +
               "    SET DB_CREATE_FILE_DEST='${ASM_DG_DATA}'\n"
               "    SET DB_RECOVERY_FILE_DEST='${ASM_DG_RECO}'\n"
               "  NOFILENAMECHECK;\nEOF")
        log = ("RMAN: connected to target {SOURCE_SID}; auxiliary {TARGET_SID} (ASM / OMF)\n"
               "Starting Duplicate Db at <ts>\n"
               "  Oracle Managed Files -> data diskgroup {ASM_DG_DATA}, FRA {ASM_DG_RECO}\n"
               "  restoring datafiles into {ASM_DG_DATA} ... switch to datafile copies complete\n"
               "  database mounted; media recovery complete; opened with RESETLOGS\n"
               "Finished Duplicate Db at <ts>. {TARGET_SID} is OPEN on ASM.")
    else:
        cmd = (head +
               "    SET DB_FILE_NAME_CONVERT='${SOURCE_DATAFILE_DIR}','${TARGET_DATAFILE_DIR}'\n"
               "    SET LOG_FILE_NAME_CONVERT='${SOURCE_DATAFILE_DIR}','${TARGET_DATAFILE_DIR}'\n"
               "  NOFILENAMECHECK;\nEOF")
        log = ("RMAN: connected to target {SOURCE_SID}; auxiliary {TARGET_SID} (filesystem)\n"
               "Starting Duplicate Db at <ts>\n"
               "  DB_FILE_NAME_CONVERT {SOURCE_DATAFILE_DIR} -> {TARGET_DATAFILE_DIR}\n"
               "  restoring datafiles to {TARGET_DATAFILE_DIR} ... switch to datafile copies complete\n"
               "  database mounted; media recovery complete; opened with RESETLOGS\n"
               "Finished Duplicate Db at <ts>. {TARGET_SID} is OPEN on filesystem.")
    if rac:
        log += ("\n  (RAC target: duplicated single-instance with CLUSTER_DATABASE=FALSE — "
                "converted to {RAC_NODES} instances next.)")
    return cmd, log


def _precheck_log(c: dict) -> str:
    if c["CLONE_DB_HOME"] == "yes":
        home_line = ("[precheck] Target DB ORACLE_HOME NOT found on {TARGET_DB_HOST} -> RDBMS home will be "
                     "CLONED from {SOURCE_DB_HOST} ({SOURCE_ORACLE_HOME} -> {TARGET_ORACLE_HOME}).\n"
                     "[precheck] Target OS user/group, oraInventory and kernel prerequisites for the home: OK.\n")
    else:
        home_line = "[precheck] Target DB ORACLE_HOME present on {TARGET_DB_HOST} (binaries NOT cloned).\n"
    base = ("[precheck] Source DB {SOURCE_SID}@{SOURCE_DB_HOST} reachable; ARCHIVELOG mode ON.\n"
            "[precheck] SYS authentication to {SOURCE_SID} validated (connect sys/****@{SOURCE_SID} as sysdba).\n"
            + home_line +
            "[precheck] Auxiliary SYS password file matches the source SYS password — OK for active duplicate.\n"
            "[precheck] Apps tiers {SOURCE_APPS_HOST} -> {TARGET_APPS_HOST} reachable.\n")
    if c["TARGET_STORAGE"] == "asm":
        store_line = ("[precheck] Target ASM diskgroup {ASM_DG_DATA}: free 612 GB / 1024 GB — OK; "
                      "{ASM_DG_RECO}: free 240 GB — OK.\n")
        word = "ASM"
    else:
        store_line = "[precheck] Target mount {TARGET_DATAFILE_DIR}: free 612 GB — OK.\n"
        word = "filesystem"
    rac_line = ""
    if c["DB_TOPOLOGY"] == "rac":
        rac_line = ("[precheck] Target topology: RAC ({RAC_NODES} nodes: {RAC_DB_HOSTS}).\n"
                    "[precheck] Grid Infrastructure / clusterware UP on all nodes; SCAN {SCAN_NAME} resolves.\n"
                    "[precheck] Shared ASM diskgroup {ASM_DG_DATA} visible from every node (CSS/ASM up).\n")
        word = "RAC / ASM"
    return base + store_line + rac_line + "[precheck] All pre-clone checks PASSED (" + word + " target)."


def _autoconfig_block(c: dict):
    """AutoConfig phase — run on every DB node for RAC, plus the apps tier."""
    if c["DB_TOPOLOGY"] != "rac":
        return _PHASE_DEFS["autoconfig"][2], _PHASE_DEFS["autoconfig"][3]
    insts = _rac_instances(c)
    node_lines = "\n".join(f"  AutoConfig on {h} ({n}, dbTier) ... completed." for n, h, _t, _u in insts)
    cmd = ("# RAC: run AutoConfig on EVERY database node, then the apps tier.\n"
           + "\n".join(f"ssh {h} '$ORACLE_HOME/appsutil/scripts/{c['TARGET_SID']}_{h}/adautocfg.sh'"
                       for _n, h, _t, _u in insts)
           + "\n$ADMIN_SCRIPTS_HOME/adautocfg.sh   # apps tier")
    log = ("Running AutoConfig across the RAC cluster:\n" + node_lines +
           "\n  AutoConfig on {TARGET_APPS_HOST} (appsTier) ... completed.\n"
           "AutoConfig completed successfully on all {RAC_NODES} DB nodes and the apps tier.")
    return cmd, log


def _validate_block(c: dict):
    if c["DB_TOPOLOGY"] != "rac":
        return _PHASE_DEFS["validate"][2], _PHASE_DEFS["validate"][3]
    insts = _rac_instances(c)
    inst_lines = "\n".join(f"[validate] Instance {n} on {h} : OPEN (gv$instance)" for n, h, _t, _u in insts)
    cmd = "srvctl status database -db ${RAC_DB_UNIQUE_NAME}; validate SCAN, services, ICM and login page"
    log = ("[validate] SCAN listener {SCAN_NAME} : UP — registers all instances\n"
           + inst_lines + "\n"
           "[validate] Database {RAC_DB_UNIQUE_NAME} : OPEN on {RAC_NODES} instances, APPS schema reachable\n"
           "[validate] Internal Concurrent Manager : ACTIVE\n"
           "[validate] OHS / Forms / login page (http://{TARGET_APPS_HOST}) : RESPONDING\n"
           "[validate] RAC clone {SOURCE_NAME} -> {TARGET_NAME} PASSED.")
    return cmd, log


def build_clone_steps(run) -> list:
    c = _ctx(run)
    clone_home = c["CLONE_DB_HOME"] == "yes"
    rac = c["DB_TOPOLOGY"] == "rac"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def fill(s):
        out = s.replace("<ts>", ts)
        for k, v in c.items():
            out = out.replace("{" + k + "}", v)
        return out

    steps = []
    for i, key in enumerate(_phase_order(clone_home, rac), 1):
        title, node, cmd, log = _PHASE_DEFS[key]
        if key == "rman_duplicate":
            cmd, log = _rman_block(c)
        elif key == "precheck":
            log = _precheck_log(c)
        elif key == "target_aux_prep":
            cmd, log = _target_aux_prep_block(clone_home, rac)
        elif key == "rac_convert":
            cmd, log = _rac_convert_block(c)
        elif key == "rac_register":
            cmd, log = _rac_register_block(c)
        elif key == "autoconfig":
            cmd, log = _autoconfig_block(c)
        elif key == "validate":
            cmd, log = _validate_block(c)
        steps.append({
            "step": i,
            "phase": key,
            "title": title,
            "node": node,
            "command": cmd,   # keep ${VAR} form
            "log": fill(log),
            "status": "success",
        })
    return steps


# ── Runbook (clone.sh) ─────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]', '_', str(name or ''))


def build_runbook_sh(run, steps) -> str:
    c = _ctx(run)
    clone_home = c["CLONE_DB_HOME"] == "yes"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    L = []
    L.append("#!/usr/bin/env bash")
    L.append("#")
    L.append(f"# OraEBS Agent — EBS clone runbook  (RMAN duplicate DB + Rapid Clone apps)")
    L.append(f"# Clone : {c['SOURCE_NAME']} ({c['SOURCE_SID']})  ->  {c['TARGET_NAME']} ({c['TARGET_SID']})")
    L.append(f"# Generated : {ts}")
    L.append("#")
    L.append("# Passwords are NEVER embedded — export them before running:")
    L.append("#   export SYS_PWD=...  SYSTEM_PWD=...  APPS_PWD=...")
    L.append("# IMPORTANT: SYS_PWD must be the SOURCE database SYS password. RMAN active duplicate")
    L.append("#   authenticates source + auxiliary via password files, so the target auxiliary")
    L.append("#   password file is (re)created with this same SYS password (orapwd step below).")
    if clone_home:
        L.append("# The target node has NO database ORACLE_HOME: the RDBMS binaries are CLONED from the")
        L.append("#   source DB server (adpreclone dbTier -> rsync home -> adcfgclone dbTechStack) before")
        L.append("#   the auxiliary is prepared. Tune SOURCE_ORACLE_HOME / TARGET_ORACLE_HOME below.")
    else:
        L.append("# The target database ORACLE_HOME (binaries) must already be installed — it is NOT cloned.")
    rac = c["DB_TOPOLOGY"] == "rac"
    if rac:
        L.append(f"# RAC target: {c['RAC_NODES']} instances on nodes {c['RAC_DB_HOSTS']} (SCAN {c['SCAN_NAME']}).")
        L.append("#   The DB is duplicated single-instance, then converted to RAC and registered with srvctl.")
        L.append("#   Grid Infrastructure must already be installed and running on all nodes (it is NOT cloned).")
    L.append("# Run from a controller host that can ssh to all tier nodes (key-based auth).")
    L.append("")
    L.append("set -euo pipefail")
    L.append("")
    L.append("# ── Parameters (override via environment) ───────────────────────────────────")
    param_keys = ["SOURCE_NAME", "TARGET_NAME", "SOURCE_SID", "TARGET_SID",
                  "SOURCE_DB_HOST", "TARGET_DB_HOST", "SOURCE_APPS_HOST", "TARGET_APPS_HOST",
                  "TARGET_DB_PORT"]
    if clone_home:
        param_keys += ["SOURCE_ORACLE_HOME", "TARGET_ORACLE_HOME"]
    for k in param_keys:
        L.append('%s="${%s:-%s}"' % (k, k, c[k]))
    L.append("")
    L.append("# ── Storage layout (TARGET_STORAGE = asm | fs) ──────────────────────────────")
    for k in ["TARGET_STORAGE", "ASM_DG_DATA", "ASM_DG_RECO", "SOURCE_DATAFILE_DIR", "TARGET_DATAFILE_DIR"]:
        L.append('%s="${%s:-%s}"' % (k, k, c[k]))
    L.append("")
    if rac:
        L.append("# ── RAC topology (Grid Infrastructure must already exist) ───────────────────")
        for k in ["DB_TOPOLOGY", "RAC_NODES", "RAC_DB_HOSTS", "RAC_DB_UNIQUE_NAME",
                  "RAC_INSTANCE_PREFIX", "SCAN_NAME", "GRID_HOME"]:
            L.append('%s="${%s:-%s}"' % (k, k, c[k]))
        L.append("")
    L.append("# ── Passwords (required; never stored in this file) ─────────────────────────")
    L.append('SYS_PWD="${SYS_PWD:?export SYS_PWD before running}"')
    L.append('SYSTEM_PWD="${SYSTEM_PWD:?export SYSTEM_PWD before running}"')
    L.append('APPS_PWD="${APPS_PWD:?export APPS_PWD before running}"')
    L.append("")
    L.append('log(){ echo; echo "=== [$(date "+%F %T")] $* ==="; }')
    L.append('# run_on <host> <command...>  — execute a command on a remote tier node')
    L.append('run_on(){ local host="$1"; shift; echo "[ssh ${host}] $*"; ssh "${host}" "$*"; }')
    L.append("")
    node_host = {
        "controller": "localhost",
        "source-db": "${SOURCE_DB_HOST}",
        "source-apps": "${SOURCE_APPS_HOST}",
        "target-db": "${TARGET_DB_HOST}",
        "target-apps": "${TARGET_APPS_HOST}",
    }
    for s in steps:
        L.append(f'log "Step {s["step"]}/{len(steps)} — {s["title"]}  [{s["node"]}]"')
        host = node_host.get(s["node"], "localhost")
        cmd = s["command"].strip()
        if s["node"] == "controller":
            L.append(f"# {cmd}")
        elif "\n" in cmd:
            # multi-line (RMAN) — annotate the target node; run by hand or via ssh heredoc
            L.append(f"# Run the following on {host}:")
            for ln in cmd.splitlines():
                L.append(f"#   {ln}")
        else:
            L.append(f'run_on "{host}" \'{cmd}\'')
        L.append("")
    L.append('log "Clone runbook completed — validate ${TARGET_NAME} before handing over."')
    L.append("")
    return "\n".join(L)


def build_readme(run, steps) -> str:
    c = _ctx(run)
    clone_home = c["CLONE_DB_HOME"] == "yes"
    phase_list = "\n".join(f"{s['step']}. **{s['title']}** — `{s['node']}`" for s in steps)
    if c["TARGET_STORAGE"] == "asm":
        storage_line = f"Target storage: **ASM** — data `{c['ASM_DG_DATA']}`, FRA `{c['ASM_DG_RECO']}` (OMF; `DB_CREATE_FILE_DEST`)."
    else:
        storage_line = (f"Target storage: **filesystem** — `DB_FILE_NAME_CONVERT` "
                        f"`{c['SOURCE_DATAFILE_DIR']}` → `{c['TARGET_DATAFILE_DIR']}`.")
    rac = c["DB_TOPOLOGY"] == "rac"
    method_line = ("Method: **RMAN active duplicate** (single-instance) → **RAC conversion** (database tier) "
                   "+ **Rapid Clone** (application tier)." if rac else
                   "Method: **RMAN active duplicate** (database tier) + **Rapid Clone** (application tier).")
    insts = _rac_instances(c) if rac else []
    if rac:
        inst_md = "\n".join(f"  - `{n}` on node `{h}` (thread {t}, `{u}`)" for n, h, t, u in insts)
        rac_section = (
            "## Database topology — RAC\n"
            f"- Target is **RAC** with **{c['RAC_NODES']} instances**, db_unique_name `{c['RAC_DB_UNIQUE_NAME']}`, "
            f"SCAN `{c['SCAN_NAME']}`:\n{inst_md}\n"
            "- **Grid Infrastructure is NOT cloned** — clusterware, ASM and the SCAN must already exist on the\n"
            "  target nodes. The database is duplicated **single-instance** (`CLUSTER_DATABASE=FALSE`) on the first\n"
            "  node, then converted: a shared SPFILE is created in ASM, per-instance redo threads + undo\n"
            "  tablespaces are added, `CLUSTER_DATABASE=TRUE` is set and `catclust.sql` is run.\n"
            "- The database and each instance are registered with `srvctl add database` / `srvctl add instance`\n"
            "  and started; AutoConfig is then run on **every** DB node.\n\n")
    else:
        rac_section = ""
    if clone_home:
        home_section = (
            "## Database ORACLE_HOME — CLONED\n"
            "- The target server has **no database ORACLE_HOME**, so the **RDBMS binaries are cloned from the\n"
            "  source DB server** before the database is duplicated: `adpreclone.pl dbTier` stages the home on\n"
            f"  the source, the home is shipped (`{c['SOURCE_ORACLE_HOME']}` → `{c['TARGET_ORACLE_HOME']}`), and\n"
            "  `adcfgclone.pl dbTechStack` relinks it, attaches it to the central inventory and runs `root.sh`.\n"
            "- RMAN active duplicate then creates the datafiles into this freshly configured home.\n"
            "- The Oracle **version/patch level of the source home is reproduced** on the target (a clone, not a\n"
            "  fresh install) — make sure the target OS, users/groups and kernel prerequisites match the source.\n\n")
    else:
        home_section = (
            "## Database ORACLE_HOME — NOT cloned\n"
            "- The **database ORACLE_HOME (binaries)** is **not** copied — the target DB home must already be\n"
            "  installed; RMAN active duplicate creates only the datafiles. (If the target has no home, re-run the\n"
            "  interview and answer **no** to the Oracle Home question to clone the RDBMS binaries from source.)\n"
            "- Only the **application** file system (APPL_TOP/COMMON_TOP/OA) is copied for the apps tier.\n\n")
    return (
        f"# EBS Clone Runbook — {c['SOURCE_NAME']} → {c['TARGET_NAME']}\n\n"
        f"{method_line}\n\n"
        f"{storage_line}\n\n"
        "## Passwords\n"
        "- **`SYS_PWD` must be the SOURCE database SYS password.** RMAN active duplicate authenticates the\n"
        "  source (target) and the auxiliary via password files, so the runbook re-creates the target\n"
        "  auxiliary password file with this SYS password (`orapwd … password=$SYS_PWD`).\n"
        "- `SYSTEM_PWD` / `APPS_PWD` are used during `adcfgclone` on the target.\n"
        "- Nothing is stored in this file — export them at run time.\n\n"
        f"{rac_section}"
        f"{home_section}"
        "## How to run\n"
        "1. Copy this directory to a controller host with key-based SSH to all four tier nodes.\n"
        "2. Export the passwords (never stored here):\n"
        "   ```bash\n   export SYS_PWD=<source SYS pwd> SYSTEM_PWD=... APPS_PWD=...\n   ```\n"
        "3. Review/adjust the parameters at the top of `clone.sh` (hosts, SIDs, ports, paths).\n"
        "4. Run it:\n   ```bash\n   chmod +x clone.sh && ./clone.sh\n   ```\n\n"
        "`set -euo pipefail` stops at the first failing step.\n\n"
        "## Phases\n"
        f"{phase_list}\n\n"
        "> Generated by OraEBS Agent in simulator mode. Validate against your standards "
        "(storage layout, NOFILENAMECHECK, listener/port pool, AutoConfig) before production use.\n"
    )


def build_runbook_zip(run) -> tuple[bytes, str]:
    steps = run.steps or build_clone_steps(run)
    root = f"oraebs_clone_{run.id}_{_safe(run.source_name)}_to_{_safe(run.target_name)}"
    clone_sh = build_runbook_sh(run, steps)
    readme = build_readme(run, steps)
    c = _ctx(run)
    manifest = json.dumps({
        "clone_run_id": run.id,
        "method": run.db_method,
        "source": {"name": run.source_name, "sid": run.source_sid,
                   "db_host": run.source_db_host, "apps_host": run.source_apps_host},
        "target": {"name": run.target_name, "sid": run.target_sid,
                   "db_host": run.target_db_host, "apps_host": run.target_apps_host},
        "storage": {"type": c["TARGET_STORAGE"], "asm_dg_data": c["ASM_DG_DATA"],
                    "asm_dg_reco": c["ASM_DG_RECO"], "source_datafile_dir": c["SOURCE_DATAFILE_DIR"],
                    "target_datafile_dir": c["TARGET_DATAFILE_DIR"]},
        "db_oracle_home": {"clone_from_source": c["CLONE_DB_HOME"] == "yes",
                           "source_oracle_home": c["SOURCE_ORACLE_HOME"],
                           "target_oracle_home": c["TARGET_ORACLE_HOME"]},
        "db_topology": {"type": c["DB_TOPOLOGY"], "rac_nodes": int(c["RAC_NODES"]),
                        "rac_db_hosts": c["RAC_DB_HOSTS"], "db_unique_name": c["RAC_DB_UNIQUE_NAME"],
                        "scan_name": c["SCAN_NAME"],
                        "instances": [{"instance": n, "node": h, "thread": t, "undo": u}
                                      for n, h, t, u in _rac_instances(c)]} if c["DB_TOPOLOGY"] == "rac"
                       else {"type": "single"},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "steps": [{"step": s["step"], "phase": s["phase"], "title": s["title"],
                   "node": s["node"], "command": s["command"]} for s in steps],
    }, indent=2)
    sim_log = "\n\n".join(
        f"===== Step {s['step']}: {s['title']} [{s['node']}] =====\n{s['log']}" for s in steps)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo(f"{root}/clone.sh")
        info.external_attr = 0o100755 << 16
        z.writestr(info, clone_sh)
        z.writestr(f"{root}/README.md", readme)
        z.writestr(f"{root}/manifest.json", manifest)
        z.writestr(f"{root}/simulated_run.log", sim_log)
    return buf.getvalue(), f"{root}.zip"


# ════════════════════════════════════════════════════════════════════════════════
# Interactive APPLICATION-TIER clone (human-in-the-loop, phase-by-phase)
# ════════════════════════════════════════════════════════════════════════════════
# A real, step-confirmed apps-tier clone: on the SOURCE apps node — source the env,
# validate APPS connectivity, run adpreclone.pl appsTier, tar the run-FS EBSapps
# directory; on the TARGET apps node — validate, back up the context file, rename
# the existing fs1/fs2/fs_ne, recreate empty FS, copy + untar EBSapps into fs1
# (preserving the path), then adcfgclone.pl appsTier. Each phase is run on demand
# and can execute live over SSH (clone_exec) or render a simulated step.

APPS_CLONE_CYCLE = [
    "source_validate", "source_preclone", "source_tar",
    "target_validate", "target_backup_context", "target_rename_fs",
    "target_copy_untar", "target_adcfgclone", "validate",
]

# Destructive phases — flagged for the UI / confirmation.
APPS_CLONE_DESTRUCTIVE = {"target_rename_fs", "target_copy_untar", "target_adcfgclone"}


def apps_ctx(run) -> dict:
    """Resolved, non-secret parameters for the apps-tier clone phases."""
    p = run.params or {}
    src = run.source_name or "SRC"
    tgt = run.target_name or "TGT"
    src_base = p.get("source_base") or f"/u01/app/{src}"
    tgt_base = p.get("target_base") or f"/u01/app/{tgt}"
    src_run_fs = p.get("source_run_fs") or f"{src_base}/fs1"
    tar_file = p.get("tar_file") or f"/tmp/EBSapps_{_safe(src)}.tgz"
    return {
        "SRC": src, "TGT": tgt,
        "SOURCE_APPS_HOST": run.source_apps_host or "src-apps",
        "TARGET_APPS_HOST": run.target_apps_host or "tgt-apps",
        "SOURCE_SID": run.source_sid or src, "TARGET_SID": run.target_sid or tgt,
        "APPS_USER": p.get("apps_user") or "apps",
        "SOURCE_BASE": src_base, "TARGET_BASE": tgt_base,
        "SOURCE_RUN_FS": src_run_fs,
        "TARGET_FS1": f"{tgt_base}/fs1", "TARGET_FS2": f"{tgt_base}/fs2", "TARGET_FS_NE": f"{tgt_base}/fs_ne",
        "EBSAPPS": "EBSapps",
        "TAR_FILE": tar_file, "TAR_NAME": tar_file.rsplit("/", 1)[-1],
        "SOURCE_OS_USER": p.get("source_os_user") or "applmgr",
        "TARGET_OS_USER": p.get("target_os_user") or "applmgr",
        "TS": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
    }


def _apps_phase(key: str, c: dict):
    """(title, node, display_command, simulated_log) for one apps-clone phase.
    Display commands are secret-free; clone_exec builds the real password feed."""
    defs = {
        "source_validate": (
            "Source — source env + validate APPS credentials", "source-apps",
            "source {SOURCE_RUN_FS}/EBSapps.env run\n"
            "df -h {SOURCE_RUN_FS}\n"
            "sqlplus -L apps/****@{SOURCE_SID}   # connectivity test (password fed at run time)\n"
            "adop -status",
            "[source] Sourced {SOURCE_RUN_FS}/EBSapps.env (run edition).\n"
            "[source] APPS connect to {SOURCE_SID}: OK.\n"
            "[source] Free space on {SOURCE_RUN_FS}: sufficient for the EBSapps tar.\n"
            "[source] adop -status: no patch cycle in progress.\n"
            "Source instance and APPS credentials validated."),
        "source_preclone": (
            "Source — adpreclone.pl appsTier", "source-apps",
            "source {SOURCE_RUN_FS}/EBSapps.env run\n"
            "cd $ADMIN_SCRIPTS_HOME && perl adpreclone.pl appsTier   # prompts: APPS, WebLogic",
            "Running adpreclone.pl appsTier on {SOURCE_APPS_HOST} ...\n"
            "  Creating clone stage under {SOURCE_RUN_FS}/EBSapps/comn/clone/\n"
            "  StageAppsTier: context, templates, APPL_TOP/COMMON_TOP/OA techstack drivers\n"
            "adpreclone.pl appsTier completed — the run file system is staged for cloning."),
        "source_tar": (
            "Source — tar the EBSapps directory", "source-apps",
            "cd {SOURCE_RUN_FS} && tar czf {TAR_FILE} {EBSAPPS} && ls -lh {TAR_FILE}",
            "Creating {TAR_FILE} from {SOURCE_RUN_FS}/{EBSAPPS} ...\n"
            "  tar archives EBSapps/ relative to the run FS so the path is reproduced on the target\n"
            "  archive size ~ 18G\n"
            "EBSapps tarball created on {SOURCE_APPS_HOST}."),
        "target_validate": (
            "Target — validate existing credentials & FS", "target-apps",
            "source {TARGET_FS1}/EBSapps.env run   # if already configured\n"
            "sqlplus -L apps/****@{TARGET_SID}     # validate existing APPS credentials\n"
            "ls -ld {TARGET_BASE}/fs1 {TARGET_BASE}/fs2 {TARGET_BASE}/fs_ne",
            "[target] APPS connect to {TARGET_SID}: OK (existing credentials valid).\n"
            "[target] Current file systems detected: {TARGET_BASE}/fs1, fs2, fs_ne.\n"
            "Target credentials validated; current layout captured."),
        "target_backup_context": (
            "Target — back up the context file", "target-apps",
            "source {TARGET_FS1}/EBSapps.env run\n"
            'cp -p "$CONTEXT_FILE" {TARGET_BASE}/context_{TGT}_{TS}.xml.bak',
            "Backing up the apps context file on {TARGET_APPS_HOST}:\n"
            "  $CONTEXT_FILE -> {TARGET_BASE}/context_{TGT}_{TS}.xml.bak\n"
            "Context file backed up (restore point before the clone)."),
        "target_rename_fs": (
            "Target — rename fs1 / fs2 / fs_ne, create empty FS", "target-apps",
            "cd {TARGET_BASE}\n"
            "for d in fs1 fs2 fs_ne; do [ -e \"$d\" ] && mv -v \"$d\" \"${{d}}_old_{TS}\"; done\n"
            "mkdir -pv fs1 fs2 fs_ne",
            "Renaming existing file systems on {TARGET_APPS_HOST} (preserved, not deleted):\n"
            "  fs1 -> fs1_old_{TS}\n  fs2 -> fs2_old_{TS}\n  fs_ne -> fs_ne_old_{TS}\n"
            "Created empty {TARGET_BASE}/fs1, fs2, fs_ne for the new file system."),
        "target_copy_untar": (
            "Target — copy EBSapps tar to fs1 and untar", "target-apps",
            "scp {SOURCE_OS_USER}@{SOURCE_APPS_HOST}:{TAR_FILE} {TARGET_FS1}/\n"
            "cd {TARGET_FS1} && tar xzf {TAR_NAME} && rm -f {TAR_NAME} && ls -d {TARGET_FS1}/{EBSAPPS}",
            "Staging the source EBSapps tarball onto {TARGET_APPS_HOST}:\n"
            "  scp {SOURCE_APPS_HOST}:{TAR_FILE} -> {TARGET_FS1}/\n"
            "  tar xzf {TAR_NAME} in {TARGET_FS1} -> {TARGET_FS1}/{EBSAPPS} (path preserved)\n"
            "Source run file system copied into the target fs1."),
        "target_adcfgclone": (
            "Target — adcfgclone.pl appsTier", "target-apps",
            "cd {TARGET_FS1}/{EBSAPPS}/comn/clone/bin\n"
            "perl adcfgclone.pl appsTier   # prompts: APPS, WebLogic (+ SYSTEM)",
            "Running adcfgclone.pl appsTier on {TARGET_APPS_HOST} ...\n"
            "  Target system name: {TGT}\n"
            "  Collecting apps user/ports; building the new context file\n"
            "  Relinking ADX/FND; configuring OHS, Forms, WebLogic, Concurrent Manager\n"
            "  Running AutoConfig on the apps tier\n"
            "adcfgclone.pl appsTier completed — the target apps tier is configured."),
        "validate": (
            "Target — start services & validate", "target-apps",
            "source {TARGET_FS1}/EBSapps.env run\n"
            "adop -status\n"
            "$ADMIN_SCRIPTS_HOME/adstrtal.sh   # start apps services",
            "[validate] adop -status: clean; run edition healthy.\n"
            "[validate] Services started (OHS / Forms / oacore / Concurrent Managers).\n"
            "[validate] Login page on {TARGET_APPS_HOST}: RESPONDING.\n"
            "[validate] Apps-tier clone {SRC} -> {TGT}: PASSED."),
    }
    return defs[key]


def _apps_fill(s: str, c: dict) -> str:
    out = s
    for k, v in c.items():
        if isinstance(v, str):
            out = out.replace("{" + k + "}", v)
    return out


def build_apps_clone_steps(run) -> list:
    """The ordered apps-tier clone plan (display steps; secret-free commands)."""
    c = apps_ctx(run)
    steps = []
    for i, key in enumerate(APPS_CLONE_CYCLE, 1):
        title, node, cmd, log = _apps_phase(key, c)
        steps.append({
            "step": i, "phase": key, "title": title, "node": node,
            "command": _apps_fill(cmd, c), "log": _apps_fill(log, c),
            "destructive": key in APPS_CLONE_DESTRUCTIVE, "status": "pending",
        })
    return steps


def clone_phase_step(run, phase: str) -> dict:
    """Display step for a single apps-clone phase (built fresh, vars filled)."""
    if phase not in APPS_CLONE_CYCLE:
        return None
    c = apps_ctx(run)
    title, node, cmd, log = _apps_phase(phase, c)
    return {"phase": phase, "title": title, "node": node,
            "command": _apps_fill(cmd, c), "log": _apps_fill(log, c),
            "destructive": phase in APPS_CLONE_DESTRUCTIVE, "status": "success"}


def _clone_prereq_ok(phase: str, executed: list) -> bool:
    """Strictly sequential — a phase may run only when it's the next unexecuted one.
    abort is allowed any time before the final validate."""
    if phase == "abort":
        return bool(executed) and "validate" not in executed
    if phase in executed:
        return False
    return clone_next_phase(executed) == phase


def clone_next_phase(executed: list):
    for p in APPS_CLONE_CYCLE:
        if p not in executed:
            return p
    return None


def clone_status(run) -> dict:
    """Interactive apps-clone cycle state (executed phases, what's next)."""
    p = run.params or {}
    interactive = bool(p.get("interactive"))
    cyc = p.get("cycle") or {}
    executed = list(cyc.get("executed") or [])
    aborted = bool(cyc.get("aborted"))
    nxt = None if aborted else clone_next_phase(executed)
    allowed = [] if aborted else ([nxt] if nxt else [])
    if not aborted and executed and "validate" not in executed:
        allowed.append("abort")
    allowed.append("status")
    if not interactive:
        summary = "This clone was run as a batch simulation (not phase-by-phase)."
    elif aborted:
        summary = "ABORTED — stop here; the target file systems were renamed (…_old_*), not deleted, so the original layout can be restored."
    elif not executed:
        summary = "Ready. Start with `source_validate` on the SOURCE apps tier."
    elif "validate" in executed:
        summary = f"COMPLETE — apps-tier clone {run.source_name} → {run.target_name} finished."
    else:
        side = "TARGET" if executed[-1].startswith("target") else "SOURCE"
        summary = f"In progress on the {side} apps tier — last phase '{executed[-1]}'. Next: {nxt}."
    return {
        "source_name": run.source_name, "target_name": run.target_name,
        "interactive": interactive, "executed_phases": executed,
        "next_phase": nxt, "allowed_phases": allowed,
        "destructive_next": (nxt in APPS_CLONE_DESTRUCTIVE) if nxt else False,
        "aborted": aborted, "summary": summary,
    }
