import os
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, Query
from sqlalchemy.orm import Session
import httpx
import json
import re
import time
from datetime import datetime, timezone
from typing import List, Optional

from app import database, schemas, models, rag_service, llm_service
from app.routers.auth import get_current_user, require_agent_access

router = APIRouter(
    prefix="/deployments",
    tags=['Deployments']
)

_OLLAMA_BASE = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = llm_service.DEFAULT_MODELS["ollama"]  # follows OLLAMA_DEFAULT_MODEL
OLLAMA_EXTRACT_TIMEOUT = 30.0

IN_PROGRESS_STATUSES = {"extracting", "downloading", "deploying"}
CANCELLABLE_STATUSES = {"pending"} | IN_PROGRESS_STATUSES


def _index_step_in_rag(deployment, step, db) -> None:
    """
    Index a successfully executed deployment step into ChromaDB so it can be
    retrieved in future RAG queries (training / reference).

    Stores: deployment context, file name, execution type, command, and the
    raw log output so the AI can learn from real execution results.
    """
    try:
        # Build a structured text document for this step
        text = (
            f"# Deployment Step — {step.execution_type.replace('_', ' ').title()}\n\n"
            f"**Deployment ID**: {deployment.id}\n"
            f"**Environment**: {deployment.target_instance}\n"
            f"**Source**: {deployment.source_doc_name or 'Agent Deployment'}\n"
            f"**File**: {step.file_name}\n"
            f"**Execution Type**: {step.execution_type}\n"
            f"**Command**:\n```\n{step.command}\n```\n\n"
            f"## Deployment Instructions\n{deployment.source_content[:800]}\n\n"
            f"## Execution Log\n```\n{(step.log_output or '')[:1500]}\n```\n\n"
            f"**Outcome**: Successfully executed on {deployment.target_instance}"
        )

        # Create a lightweight RagDocument record so it appears in the KB list
        doc = models.RagDocument(
            user_id=deployment.user_id,
            filename=f"deployment_{deployment.id}_step_{step.step_number}_{step.file_name}.md",
            file_type="deployment",
            status="indexing",
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)

        chunk_count = rag_service.index_text(
            text=text,
            doc_id=doc.id,
            metadata={
                "source":          "deployment",
                "deployment_id":   str(deployment.id),
                "step_number":     str(step.step_number),
                "file_name":       step.file_name,
                "execution_type":  step.execution_type,
                "environment":     deployment.target_instance,
                "filename":        doc.filename,
            },
        )

        doc.status      = "ready"
        doc.chunk_count = chunk_count
        db.commit()
        print(f"[RAG] Indexed deployment {deployment.id} step {step.step_number} → {chunk_count} chunks")

    except Exception as exc:
        print(f"[RAG] Failed to index step {step.id}: {exc}")
        # Don't re-raise — RAG failure must never block a deployment


def _inject_db_credentials(cmd: str, deployment) -> str:
    """
    Replace any hardcoded Oracle credential pattern in a command with the
    credentials stored on the deployment record.

    Handles the common Oracle EBS tool patterns:
      sqlplus user/pass[@conn]  →  sqlplus db_user/db_password@//host:port/sid
      fndload user/pass[@conn]  →  same
      wfload  user/pass[@conn]  →  same

    The substitution is applied whether or not the original command had
    a connection string — the stored credentials always win.
    """
    db_user = deployment.db_user or ""
    db_pass = deployment.db_password or ""
    db_host = deployment.db_host or ""
    db_port = deployment.db_port or 1521
    db_sid  = deployment.db_sid  or ""

    if not db_user:
        return cmd  # no credentials stored — leave command unchanged

    # Build the authoritative connection string
    if db_host and db_sid:
        conn_str = f"{db_user}/{db_pass}@//{db_host}:{db_port}/{db_sid}"
    elif db_sid:
        conn_str = f"{db_user}/{db_pass}@{db_sid}"
    else:
        conn_str = f"{db_user}/{db_pass}"

    def _replace(m):
        return f"{m.group(1)} {conn_str}"

    # Pattern: tool  user/pass[@anything_without_space]
    # Stops at first whitespace so trailing args (script names etc.) are kept.
    return re.sub(
        r'(?i)(sqlplus|fndload|wfload)\s+[^\s/]+/[^\s@]*(?:@[^\s]+)?',
        _replace,
        cmd,
    )


def fallback_regex_parser(content: str) -> list:
    """Parses text using simple regex fallback if LLM parse fails."""
    steps = []
    lines = content.split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue

        if '.sql' in line.lower() or 'sqlplus' in line.lower():
            sql_file = "patch_script.sql"
            match = re.search(r'[\w_]+\.sql', line, re.IGNORECASE)
            if match:
                sql_file = match.group(0)
            steps.append({
                "file_name": sql_file,
                "execution_type": "sql_script",
                "command": f"sqlplus apps/apps @{sql_file}"
            })

        elif '.pls' in line.lower() or '.plb' in line.lower() or 'package' in line.lower():
            pls_file = "package_script.pls"
            match = re.search(r'[\w_]+\.(pls|plb)', line, re.IGNORECASE)
            if match:
                pls_file = match.group(0)
            steps.append({
                "file_name": pls_file,
                "execution_type": "plsql_package",
                "command": f"alter package {pls_file.split('.')[0]} compile body;"
            })

        elif '.ldt' in line.lower() or 'fndload' in line.lower():
            ldt_file = "data_definition.ldt"
            match = re.search(r'[\w_]+\.ldt', line, re.IGNORECASE)
            if match:
                ldt_file = match.group(0)
            steps.append({
                "file_name": ldt_file,
                "execution_type": "fndload_upload",
                "command": f"FNDLOAD apps/apps 0 Y UPLOAD @FND:patch/115/import/afscursp.lct {ldt_file}"
            })

        elif '.fmb' in line.lower() or 'frmcmp' in line.lower():
            fmb_file = "custom_screen.fmb"
            match = re.search(r'[\w_]+\.fmb', line, re.IGNORECASE)
            if match:
                fmb_file = match.group(0)
            fmx_file = fmb_file.replace(".fmb", ".fmx").replace(".FMB", ".fmx")
            steps.append({
                "file_name": fmb_file,
                "execution_type": "forms_compile",
                "command": f"frmcmp_batch Module={fmb_file} Userid=apps/apps Output_File={fmx_file} Module_Type=form Compile_All=special"
            })

        elif '.wft' in line.lower() or 'wfload' in line.lower():
            wft_file = "custom_workflow.wft"
            match = re.search(r'[\w_]+\.wft', line, re.IGNORECASE)
            if match:
                wft_file = match.group(0)
            steps.append({
                "file_name": wft_file,
                "execution_type": "workflow_upload",
                "command": f"WFLOAD apps/apps 0 Y UPLOAD {wft_file}"
            })

        elif ('xmlimporter' in line.lower() or 'jrad' in line.lower() or 'webui' in line.lower()) and '.xml' in line.lower():
            xml_file = "customPagePG.xml"
            match = re.search(r'[\w_]+\.xml', line, re.IGNORECASE)
            if match:
                xml_file = match.group(0)
            steps.append({
                "file_name": xml_file,
                "execution_type": "oaf_import",
                "command": f"java oracle.jrad.tools.xml.importer.XMLImporter {xml_file} -username apps -password apps -dbconnection \"(DESCRIPTION=(ADDRESS=(PROTOCOL=tcp)(HOST=localhost)(PORT=1521))(CONNECT_DATA=(SID=XE)))\" -rootdir $JAVA_TOP"
            })

        elif '.jar' in line.lower() or '.class' in line.lower() or 'host_copy' in line.lower() or 'copy' in line.lower():
            copy_file = "custom_file.jar"
            match = re.search(r'[\w_]+\.(jar|class|zip|tar\.gz)', line, re.IGNORECASE)
            if match:
                copy_file = match.group(0)
            steps.append({
                "file_name": copy_file,
                "execution_type": "host_copy",
                "command": f"cp {copy_file} $JAVA_TOP/oracle/apps/custom/"
            })

        elif '.sh' in line.lower():
            sh_file = "run_patch.sh"
            match = re.search(r'[\w_]+\.sh', line, re.IGNORECASE)
            if match:
                sh_file = match.group(0)
            steps.append({
                "file_name": sh_file,
                "execution_type": "shell_script",
                "command": f"sh {sh_file}"
            })

    if not steps:
        steps.append({
            "file_name": "deployment_patch.sql",
            "execution_type": "sql_script",
            "command": "sqlplus apps/apps @deployment_patch.sql"
        })
    return steps


def extract_deployment_steps(content: str) -> list:
    """Queries Ollama to extract deployment steps; falls back to regex parser on failure."""
    prompt = (
        "You are an expert Oracle EBS Deployment Agent. Your task is to analyze the following deployment "
        "instructions (which might be Confluence notes, chat messages, Word logs, or PDF text) and extract "
        "the files to deploy and their exact execution steps.\n\n"
        "=== Instructions ===\n"
        f"{content}\n\n"
        "Extract each step that represents a file copy or command execution. Provide exactly a valid JSON array. "
        "Each object in the array must have the following keys:\n"
        '- "file_name": the name of the file to deploy (e.g. xxap_supplier_pkg.pls, xxap_invoice_tbl.sql, my_resp.ldt, my_screen.fmb)\n'
        '- "execution_type": one of "sql_script", "plsql_package", "shell_script", "fndload_upload", "forms_compile", "workflow_upload", "oaf_import", "host_copy"\n'
        '- "command": the exact command used to execute or upload this file from application server\n\n'
        "You must output ONLY a raw JSON array matching this structure. Do not include markdown codeblocks, intro text, or extra descriptions."
    )

    try:
        # Deterministic (temp 0) → benefits from the exact-match response cache:
        # re-running the same deployment doc skips re-extraction.
        res_content = llm_service.complete_sync(
            messages=[{"role": "user", "content": prompt}],
            provider="ollama", model=OLLAMA_MODEL, base_url=_OLLAMA_BASE,
            max_tokens=400, temperature=0.0, timeout=OLLAMA_EXTRACT_TIMEOUT,
        ).strip()
        if res_content.startswith("```"):
            res_content = re.sub(r'^```(json)?\n', '', res_content)
            res_content = re.sub(r'\n```$', '', res_content)
        parsed = json.loads(res_content.strip())
        if isinstance(parsed, list) and len(parsed) > 0:
            return parsed
    except Exception:
        pass

    return fallback_regex_parser(content)


def _simulate_step_logs(step: models.DeploymentStep, deployment: models.DeploymentRun) -> str:
    """Returns realistic simulator log output for a given step execution type."""
    dep_id = deployment.id
    if step.execution_type == "sql_script":
        return (
            "SQL*Plus: Release 19.0.0.0.0 - Production on Mon May 18 14:30:00 2026\n"
            "Copyright (c) 1982, 2019, Oracle. All rights reserved.\n"
            f"Connected to target instance {deployment.target_instance} schema APPS\n"
            f"SQL> @{step.file_name}\n"
            "Table created.\n"
            "Index created.\n"
            "SQL> commit;\n"
            "Commit complete.\n"
            "SQL> Disconnected from Oracle Database.\n"
        )
    elif step.execution_type == "plsql_package":
        return (
            f"SQL> ALTER SESSION SET CURRENT_SCHEMA = APPS;\n"
            "Session altered.\n"
            f"SQL> ALTER PACKAGE {step.file_name.split('.')[0]} COMPILE BODY;\n"
            "Package body compiled.\n"
            "SQL> SHOW ERRORS;\n"
            "No errors.\n"
        )
    elif step.execution_type == "fndload_upload":
        return (
            f"$ {step.command}\n"
            "FNDLOAD: Release 12.2.0 - Production on Mon May 18 15:00:00 2026\n"
            "Copyright (c) 1982, 2015, Oracle. All rights reserved.\n"
            f"Log file: L{dep_id}912.log\n"
            f"Opening loader data file: {step.file_name}...\n"
            "Entities found: FND_RESPONSIBILITY, FND_FORM_FUNCTIONS\n"
            "Uploading responsibility configuration data...\n"
            "Successfully compiled responsibilities.\n"
            "FNDLOAD completed successfully with return code 0.\n"
        )
    elif step.execution_type == "forms_compile":
        return (
            f"$ {step.command}\n"
            "Oracle Forms 12c Batch Compiler (frmcmp_batch) - Release 12.2.1.4.0\n"
            f"Processing Form Module: {step.file_name}\n"
            "Compiling Block: SUPPLIER_DETAILS\n"
            "Compiling Trigger: KEY-COMMIT\n"
            "Compiling Trigger: WHEN-NEW-FORM-INSTANCE\n"
            "Compiling Block: INVOICE_ATTACHMENTS\n"
            f"Form module compiled successfully. Executable output written to {step.file_name.split('.')[0]}.fmx\n"
        )
    elif step.execution_type == "workflow_upload":
        return (
            f"$ {step.command}\n"
            "Oracle Workflow Load Utility (WFLOAD) - Version 2.6.4\n"
            f"Opening definition file: {step.file_name}\n"
            "Uploading Workflow Item Type: XXAPAPPR\n"
            "Uploading Process Activities...\n"
            "Uploading Transition definitions...\n"
            "Uploading Notification definitions...\n"
            "Workflow uploaded and persisted successfully to target EBS metadata repository.\n"
        )
    elif step.execution_type == "oaf_import":
        return (
            f"$ {step.command}\n"
            "Oracle JRAD XMLImporter - Version 12.2.0\n"
            "Initializing MDS database connection...\n"
            f"Parsing JRAD XML Document: {step.file_name}\n"
            "Importing document path: /oracle/apps/xxap/supplier/webui/xxapSupplierPG\n"
            "Successfully imported JRAD page component to MDS metadata repository.\n"
            "Clearing MDS page definitions cache...\n"
            "MDS Page Definition Import completed successfully.\n"
        )
    elif step.execution_type == "host_copy":
        return (
            f"$ {step.command}\n"
            f"Copying {step.file_name} to target application server directory...\n"
            f"Source: /u01/install/patch/{step.file_name}\n"
            f"Destination: $JAVA_TOP/oracle/apps/custom/\n"
            "File permissions set: 755\n"
            "Checksum verified: OK\n"
            f"Host file copy of {step.file_name} completed successfully.\n"
        )
    elif step.execution_type == "shell_script":
        return (
            f"$ sh {step.file_name}\n"
            "Applying Oracle E-Business Suite ADOP patch session...\n"
            "Copying custom templates...\n"
            "Reloading EBS HTTP Services...\n"
            "Reloading complete. Status: OK\n"
        )
    else:
        return f"Executed step for file {step.file_name}. Status: OK\n"


SQL_ONLY_TYPES = {"sql_script", "plsql_package"}


def _split_sql_statements(sql: str) -> list[str]:
    """
    Split a SQL script into individual statements for direct execution.
    Handles:
      - Regular statements ending in ;
      - PL/SQL blocks ending in / on its own line (CREATE OR REPLACE PACKAGE BODY ... END; /)
    """
    statements = []
    current: list[str] = []
    in_plsql = False

    for line in sql.splitlines():
        stripped = line.strip()

        # Detect start of a PL/SQL block
        if re.match(r'(?i)(CREATE\s+(OR\s+REPLACE\s+)?(PACKAGE|PROCEDURE|FUNCTION|TRIGGER|TYPE)\b'
                    r'|BEGIN\b|DECLARE\b)', stripped):
            in_plsql = True

        current.append(line)

        if in_plsql:
            # PL/SQL block ends at a bare / on its own line
            if stripped == '/':
                stmt = '\n'.join(current[:-1]).strip()  # exclude the /
                if stmt:
                    statements.append(stmt)
                current = []
                in_plsql = False
        else:
            # Regular SQL: ends at ;
            if stripped.endswith(';'):
                stmt = '\n'.join(current).strip().rstrip(';')
                if stmt and not stmt.startswith('--'):
                    statements.append(stmt)
                current = []

    # Flush any remaining content
    remaining = '\n'.join(current).strip().rstrip(';')
    if remaining and not remaining.startswith('--'):
        statements.append(remaining)

    return [s for s in statements if s.strip()]


def _execute_sql_directly(deployment, steps: list, db) -> None:
    """
    Execute SQL/PLSQL steps directly against the Oracle database without SSH.
    Uses oracledb thin mode — no Oracle Instant Client required.

    Called when all steps are SQL-only types AND no SSH host is configured.
    If git_repo_url is set, the repo is cloned locally on the API server first.
    """
    import oracledb, subprocess, tempfile, shutil

    db_user = deployment.db_user or ""
    db_pass = deployment.db_password or ""
    db_host = deployment.db_host or ""
    db_port = deployment.db_port or 1521
    db_sid  = deployment.db_sid  or ""

    if not (db_user and db_host and db_sid):
        for step in steps:
            step.status = "failed"
            step.log_output = (
                "[DB] Cannot execute: DB credentials (host, user, password, SID) "
                "are not configured. Add them in Settings → Environments.\n"
            )
            step.completed_at = datetime.now(timezone.utc)
        db.commit()
        return

    # Connect to Oracle (thin mode — pure Python, no client needed)
    dsn = f"{db_host}:{db_port}/{db_sid}"
    conn_log = f"[DB] Connecting to {db_user}@{db_host}:{db_port}/{db_sid}...\n"
    try:
        conn = oracledb.connect(user=db_user, password=db_pass, dsn=dsn)
        conn_log += "[DB] Connected.\n"
    except Exception as exc:
        for step in steps:
            step.status = "failed"
            step.log_output = conn_log + f"[DB] Connection failed: {exc}\n"
            step.completed_at = datetime.now(timezone.utc)
        db.commit()
        return

    # If git source, clone locally so we can read the SQL files
    local_clone = None
    if deployment.git_repo_url:
        local_clone = tempfile.mkdtemp(prefix=f"deploy_{deployment.id}_")
        git_url = deployment.git_repo_url
        if deployment.git_token and git_url.startswith("https://"):
            git_url = git_url.replace("https://", f"https://oauth2:{deployment.git_token}@", 1)
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", deployment.git_branch or "main",
             git_url, local_clone],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            for step in steps:
                step.status = "failed"
                step.log_output = conn_log + f"[GIT] Clone failed:\n{result.stderr}\n"
                step.completed_at = datetime.now(timezone.utc)
            db.commit()
            conn.close()
            shutil.rmtree(local_clone, ignore_errors=True)
            return
        conn_log += f"[GIT] Cloned {deployment.git_repo_url} (branch: {deployment.git_branch or 'main'})\n"

    try:
        cursor = conn.cursor()
        for step in steps:
            step.status = "running"
            step.log_output = conn_log
            db.commit()

            # Resolve SQL content: from git file, or extract inline from command
            sql_content = ""
            if local_clone:
                import os as _os
                sql_path = _os.path.join(local_clone, step.file_name)
                if _os.path.exists(sql_path):
                    with open(sql_path, encoding="utf-8", errors="ignore") as f:
                        sql_content = f.read()
                    step.log_output += f"[DB] Reading {step.file_name} from git clone.\n"
                else:
                    step.log_output += f"[DB] File not found in clone: {step.file_name}\n"
                    # Fall back to command as inline SQL
                    sql_content = step.command
            else:
                # Extract SQL from command: strip leading 'sqlplus user/pass@conn' prefix
                sql_content = re.sub(
                    r'(?i)sqlplus\s+\S+/\S+(?:@\S+)?\s*',
                    '', step.command
                ).strip()
                # Strip sqlplus script notation (@file.sql → just run the instruction)
                if sql_content.startswith('@'):
                    sql_content = sql_content[1:]
                # If what remains looks like a filename, note we can't read it
                if not sql_content or re.match(r'^[\w./]+\.sql$', sql_content):
                    step.status = "failed"
                    step.log_output += (
                        "[DB] Cannot read SQL file without a git source or SSH server. "
                        "Provide a Git repository URL or enable SSH.\n"
                    )
                    step.completed_at = datetime.now(timezone.utc)
                    db.commit()
                    continue

            # Execute each statement
            statements = _split_sql_statements(sql_content)
            step.log_output += f"[DB] Executing {len(statements)} statement(s)...\n"
            db.commit()

            errors = []
            for idx, stmt in enumerate(statements, 1):
                try:
                    cursor.execute(stmt)
                    step.log_output += f"[DB] Statement {idx}: OK\n"
                except Exception as exc:
                    errors.append(f"Statement {idx}: {exc}")
                    step.log_output += f"[DB] Statement {idx} FAILED: {exc}\n"
                db.commit()

            if errors:
                conn.rollback()
                step.log_output += "[DB] Transaction rolled back due to errors.\n"
                step.status = "failed"
            else:
                conn.commit()
                step.log_output += "[DB] Transaction committed successfully.\n"
                step.status = "success"

            step.completed_at = datetime.now(timezone.utc)
            db.commit()

            if step.status == "success":
                _index_step_in_rag(deployment, step, db)

    finally:
        cursor.close()
        conn.close()
        if local_clone:
            shutil.rmtree(local_clone, ignore_errors=True)


def _resolve_managed_credentials(deployment, db) -> None:
    """
    If the deployment references an admin-managed environment / SSH server, load
    and decrypt their credentials into the in-memory deployment object.

    Uses set_committed_value so the decrypted secrets are populated for execution
    WITHOUT marking the columns dirty — they are never flushed back to
    deployment_runs, so no plaintext is persisted at rest.
    """
    from sqlalchemy.orm.attributes import set_committed_value
    from app import crypto

    if getattr(deployment, "environment_id", None):
        env = db.query(models.EbsEnvironment).filter(
            models.EbsEnvironment.id == deployment.environment_id
        ).first()
        if env:
            set_committed_value(deployment, "db_host", env.db_host)
            set_committed_value(deployment, "db_port", env.db_port or 1521)
            set_committed_value(deployment, "db_sid", env.db_sid)
            set_committed_value(deployment, "db_user", env.db_user)
            set_committed_value(deployment, "db_password", crypto.decrypt(env.db_password_enc))
            # Inherit the environment's linked SSH server if the run didn't pick one
            if not deployment.ssh_server_id and env.ssh_server_id:
                set_committed_value(deployment, "ssh_server_id", env.ssh_server_id)

    if getattr(deployment, "ssh_server_id", None):
        srv = db.query(models.SshServer).filter(
            models.SshServer.id == deployment.ssh_server_id
        ).first()
        if srv:
            set_committed_value(deployment, "ssh_host", srv.hostname)
            set_committed_value(deployment, "ssh_port", srv.port or 22)
            set_committed_value(deployment, "ssh_username", srv.username)
            set_committed_value(deployment, "ssh_password", crypto.decrypt(srv.password_enc))

    # Git: inject an admin-managed PAT for the repo host when none was supplied.
    if getattr(deployment, "git_repo_url", None) and not deployment.git_token:
        from app import config_service
        _, git_token = config_service.resolve_git_token(deployment.git_repo_url, db)
        if git_token:
            set_committed_value(deployment, "git_token", git_token)


def execute_deployment_task(deployment_id: int):
    """Background task: extracts steps from source content and executes them against the target instance."""
    db = database.SessionLocal()
    deployment = None
    try:
        deployment = db.query(models.DeploymentRun).filter(models.DeploymentRun.id == deployment_id).first()
        if not deployment:
            return

        # Abort if cancelled between scheduling and execution start
        if deployment.status == "cancelled":
            return

        # Resolve admin-managed env/server credentials (in-memory only)
        _resolve_managed_credentials(deployment, db)

        deployment.status = "extracting"
        db.commit()

        extracted_steps = extract_deployment_steps(deployment.source_content)

        for idx, s in enumerate(extracted_steps):
            step = models.DeploymentStep(
                deployment_id=deployment_id,
                step_number=idx + 1,
                file_name=s.get("file_name", "patch.sql"),
                execution_type=s.get("execution_type", "sql_script"),
                command=s.get("command", ""),
                status="pending",
                log_output=""
            )
            db.add(step)
        db.commit()

        # ── Direct DB path — SQL/PLSQL only, no SSH server configured ──────────
        steps_for_check = db.query(models.DeploymentStep).filter(
            models.DeploymentStep.deployment_id == deployment_id
        ).order_by(models.DeploymentStep.step_number).all()

        all_sql = all(s.execution_type in SQL_ONLY_TYPES for s in steps_for_check)

        if all_sql and not deployment.ssh_host:
            deployment.status = "deploying"
            db.commit()
            _execute_sql_directly(deployment, steps_for_check, db)
            # Determine final status
            db.refresh(deployment)
            all_updated = db.query(models.DeploymentStep).filter(
                models.DeploymentStep.deployment_id == deployment_id
            ).all()
            has_failure = any(s.status == "failed" for s in all_updated)
            deployment.status = "failed" if has_failure else "completed"
            deployment.completed_at = datetime.now(timezone.utc)
            db.commit()
            return   # done — skip the SSH block entirely

        ssh_client = None
        sftp_client = None
        real_ssh = False
        ssh_conn_log = ""
        clone_dir = None

        if deployment.ssh_host:
            ssh_conn_log += f"[SSH] Connecting to {deployment.ssh_host}:{deployment.ssh_port or 22}...\n"
            try:
                import paramiko
                ssh_client = paramiko.SSHClient()
                # WarningPolicy logs unknown host keys rather than silently accepting them.
                # For production use, load a verified known_hosts file via load_system_host_keys()
                # and switch to RejectPolicy to prevent MITM attacks.
                ssh_client.set_missing_host_key_policy(paramiko.WarningPolicy())
                ssh_client.connect(
                    hostname=deployment.ssh_host,
                    port=deployment.ssh_port or 22,
                    username=deployment.ssh_username,
                    password=deployment.ssh_password,
                    timeout=10.0
                )
                sftp_client = ssh_client.open_sftp()
                real_ssh = True
                ssh_conn_log += f"[SSH] Authenticated as '{deployment.ssh_username}'.\n"
            except Exception as ssh_err:
                ssh_conn_log += f"[SSH] Connection failed: {ssh_err}\n"
                ssh_conn_log += "[SSH] Falling back to high-fidelity sandbox simulation.\n"

        if deployment.status == "cancelled":
            return

        deployment.status = "downloading"
        db.commit()

        steps = db.query(models.DeploymentStep).filter(
            models.DeploymentStep.deployment_id == deployment_id
        ).order_by(models.DeploymentStep.step_number).all()

        # ── Git clone on the remote server (real SSH only) ─────────────────────
        clone_dir = None
        if deployment.git_repo_url and real_ssh:
            clone_dir  = f"/tmp/deploy_{deployment_id}"
            branch     = deployment.git_branch or "main"
            git_url    = deployment.git_repo_url
            git_token  = deployment.git_token or ""

            # Embed PAT/token into HTTPS URL so git doesn't prompt for credentials
            if git_token and git_url.startswith("https://"):
                git_url_auth = git_url.replace("https://", f"https://oauth2:{git_token}@", 1)
                display_url  = git_url  # don't log the token
            else:
                git_url_auth = git_url
                display_url  = git_url

            clone_log = (
                f"[GIT] Cloning {display_url}\n"
                f"[GIT] Branch: {branch}\n"
            )
            # Show initial progress on first step before clone starts
            if steps:
                steps[0].log_output = ssh_conn_log + clone_log + "[GIT] Cloning...\n"
                db.commit()

            # Remove any stale clone, then clone fresh
            ssh_client.exec_command(f"rm -rf {clone_dir}")
            stdin, stdout, stderr = ssh_client.exec_command(
                f"git clone --depth 1 --branch {branch} '{git_url_auth}' {clone_dir} 2>&1"
            )
            exit_status = stdout.channel.recv_exit_status()
            clone_out   = stdout.read().decode("utf-8", errors="ignore")

            if exit_status != 0:
                clone_log += f"[GIT] Clone failed:\n{clone_out}\n"
                deployment.status = "failed"
                deployment.completed_at = datetime.now(timezone.utc)
                if steps:
                    steps[0].status     = "failed"
                    steps[0].log_output = ssh_conn_log + clone_log
                    steps[0].completed_at = datetime.now(timezone.utc)
                db.commit()
                return

            clone_log += f"[GIT] Clone complete → {clone_dir}\n"
            if steps:
                steps[0].log_output = ssh_conn_log + clone_log
                db.commit()

        elif deployment.git_repo_url and not real_ssh:
            time.sleep(2.0)  # simulate download delay for demo mode

        deployment.status = "deploying"
        db.commit()

        for step in steps:
            # Honour a cancel that arrived mid-run
            db.refresh(deployment)
            if deployment.status == "cancelled":
                step.status = "failed"
                step.log_output = (step.log_output or "") + "[CANCELLED] Deployment was cancelled by user.\n"
                step.completed_at = datetime.now(timezone.utc)
                db.commit()
                break

            step.status = "running"

            # Inject stored DB credentials, replacing any hardcoded user/pass in the command
            safe_cmd = _inject_db_credentials(step.command, deployment)

            if clone_dir:
                # Files live in the git clone on the server
                source_log = f"[GIT] Source directory: {clone_dir}\n"
                exec_cmd   = f"cd {clone_dir} && {safe_cmd}"
            elif deployment.git_repo_url:
                # Simulated git (no real SSH)
                source_log = (
                    f"[GIT] Connecting to {deployment.git_repo_url}...\n"
                    f"[GIT] Checking out branch '{deployment.git_branch or 'main'}'...\n"
                    f"[GIT] Fetched {step.file_name} successfully.\n"
                )
                exec_cmd = safe_cmd
            else:
                source_log = (
                    "[LOCAL] Direct local file source mode active.\n"
                    f"[LOCAL] Staging '{step.file_name}' from parsed plan.\n"
                )
                exec_cmd = safe_cmd

            if real_ssh:
                step.log_output = ssh_conn_log + source_log + f"[EXEC] Running: {safe_cmd}\n"
                db.commit()

                try:
                    stdin, stdout, stderr = ssh_client.exec_command(exec_cmd)
                    exit_status = stdout.channel.recv_exit_status()

                    out_msg = stdout.read().decode('utf-8', errors='ignore')
                    err_msg = stderr.read().decode('utf-8', errors='ignore')

                    step.log_output += f"--- STDOUT ---\n{out_msg}\n"
                    if err_msg:
                        step.log_output += f"--- STDERR ---\n{err_msg}\n"

                    if exit_status == 0:
                        step.status = "success"
                    else:
                        step.status = "failed"
                        step.log_output += f"[EXEC] Command exited with status {exit_status}.\n"
                except Exception as run_err:
                    step.status = "failed"
                    step.log_output += f"[EXEC] SSH execution error: {run_err}\n"

                step.completed_at = datetime.now(timezone.utc)
                db.commit()

                # Index successful step into RAG for future reference / training
                if step.status == "success":
                    _index_step_in_rag(deployment, step, db)

                continue

            # Simulator path
            step.log_output = ssh_conn_log + source_log + (
                f"[SCP] Copying {step.file_name} to {deployment.target_instance}...\n"
                f"[SCP] Upload complete: /u01/install/patch/{step.file_name}\n"
                f"[EXEC] Running command: {safe_cmd}\n"
            )
            db.commit()
            time.sleep(2.0)

            step.log_output += _simulate_step_logs(step, deployment)
            step.status = "success"
            step.completed_at = datetime.now(timezone.utc)
            db.commit()

            # Index successful step into RAG for future reference / training
            _index_step_in_rag(deployment, step, db)

            time.sleep(1.0)

        # Final status: failed if any step failed, otherwise completed
        db.refresh(deployment)
        if deployment.status != "cancelled":
            all_steps = db.query(models.DeploymentStep).filter(
                models.DeploymentStep.deployment_id == deployment_id
            ).all()
            has_failure = any(s.status == "failed" for s in all_steps)
            deployment.status = "failed" if has_failure else "completed"
            deployment.completed_at = datetime.now(timezone.utc)
            db.commit()

    except Exception as e:
        if deployment:
            deployment.status = "failed"
            deployment.completed_at = datetime.now(timezone.utc)
            # Attach the error to the first incomplete step so it's visible in the UI
            incomplete = db.query(models.DeploymentStep).filter(
                models.DeploymentStep.deployment_id == deployment_id,
                models.DeploymentStep.status.in_(["pending", "running"])
            ).first()
            if incomplete:
                incomplete.status = "failed"
                incomplete.log_output = (incomplete.log_output or "") + f"\n[ERROR] {e}"
                incomplete.completed_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        # Clean up git clone directory on the remote server
        if ssh_client and clone_dir:
            try:
                ssh_client.exec_command(f"rm -rf {clone_dir}")
            except Exception:
                pass
        if sftp_client:
            try:
                sftp_client.close()
            except Exception:
                pass
        if ssh_client:
            try:
                ssh_client.close()
            except Exception:
                pass
        db.close()


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/", response_model=schemas.DeploymentRunOut, status_code=status.HTTP_201_CREATED)
def trigger_deployment(
    deployment_data: schemas.DeploymentCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_agent_access)
):
    """Creates a new deployment run and schedules the agent to deploy it in the background."""
    deployment = models.DeploymentRun(
        user_id=current_user.id,
        source_doc_type=deployment_data.source_doc_type,
        source_doc_name=deployment_data.source_doc_name,
        source_content=deployment_data.source_content,
        target_instance=deployment_data.target_instance,
        environment_id=deployment_data.environment_id,
        ssh_server_id=deployment_data.ssh_server_id,
        git_repo_url=deployment_data.git_repo_url,
        git_branch=deployment_data.git_branch,
        git_token=deployment_data.git_token,
        ssh_host=deployment_data.ssh_host,
        ssh_port=deployment_data.ssh_port,
        ssh_username=deployment_data.ssh_username,
        ssh_password=deployment_data.ssh_password,
        db_host=deployment_data.db_host,
        db_port=deployment_data.db_port,
        db_sid=deployment_data.db_sid,
        db_user=deployment_data.db_user,
        db_password=deployment_data.db_password,
        status="pending"
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)

    background_tasks.add_task(execute_deployment_task, deployment.id)
    return deployment


@router.get("/", response_model=List[schemas.DeploymentRunSummaryOut])
def list_deployments(
    filter_status: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Lists deployment run history. Optionally filter by status (pending, deploying, completed, failed, cancelled)."""
    query = db.query(models.DeploymentRun).filter(models.DeploymentRun.user_id == current_user.id)
    if filter_status:
        query = query.filter(models.DeploymentRun.status == filter_status)
    return query.order_by(models.DeploymentRun.created_at.desc()).all()


@router.get("/{id}", response_model=schemas.DeploymentRunOut)
def get_deployment(
    id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves full detail and step logs for a deployment run."""
    deployment = db.query(models.DeploymentRun).filter(
        models.DeploymentRun.id == id,
        models.DeploymentRun.user_id == current_user.id
    ).first()
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment run not found")
    return deployment


@router.get("/{id}/artifact")
def download_deployment_artifact(
    id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Generate and download a portable artifact bundle (zip) for a deployment:
    a runnable deploy.sh with the full step sequence, the apps password as a
    variable, a README and a manifest. Intended for completed runs so the bundle
    can be copied and re-run manually on another instance.
    """
    from fastapi import Response
    from app import artifact_service

    deployment = db.query(models.DeploymentRun).filter(
        models.DeploymentRun.id == id,
        models.DeploymentRun.user_id == current_user.id
    ).first()
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment run not found")

    steps = db.query(models.DeploymentStep).filter(
        models.DeploymentStep.deployment_id == id
    ).order_by(models.DeploymentStep.step_number).all()
    if not steps:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This deployment has no steps to package.")

    zip_bytes, filename = artifact_service.build_artifact_zip(deployment, steps)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{id}/steps", response_model=List[schemas.DeploymentStepOut])
def get_deployment_steps(
    id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Returns only the steps for a deployment. Useful for lightweight polling from the UI."""
    deployment = db.query(models.DeploymentRun).filter(
        models.DeploymentRun.id == id,
        models.DeploymentRun.user_id == current_user.id
    ).first()
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment run not found")
    return db.query(models.DeploymentStep).filter(
        models.DeploymentStep.deployment_id == id
    ).order_by(models.DeploymentStep.step_number).all()


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_deployment(
    id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Deletes a deployment run. Cannot delete an in-progress deployment."""
    deployment = db.query(models.DeploymentRun).filter(
        models.DeploymentRun.id == id,
        models.DeploymentRun.user_id == current_user.id
    ).first()
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment run not found")
    if deployment.status in IN_PROGRESS_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete an in-progress deployment. Cancel it first."
        )
    db.delete(deployment)
    db.commit()


@router.patch("/{id}/cancel", response_model=schemas.DeploymentRunOut)
def cancel_deployment(
    id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Marks a deployment as cancelled. The background worker will stop after its current step completes."""
    deployment = db.query(models.DeploymentRun).filter(
        models.DeploymentRun.id == id,
        models.DeploymentRun.user_id == current_user.id
    ).first()
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment run not found")
    if deployment.status not in CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel a deployment with status '{deployment.status}'."
        )
    deployment.status = "cancelled"
    deployment.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(deployment)
    return deployment


@router.post("/{id}/retry", response_model=schemas.DeploymentRunOut)
def retry_deployment(
    id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_agent_access)
):
    """Clears existing steps and re-runs a failed or cancelled deployment from scratch."""
    deployment = db.query(models.DeploymentRun).filter(
        models.DeploymentRun.id == id,
        models.DeploymentRun.user_id == current_user.id
    ).first()
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment run not found")
    if deployment.status not in ("failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only failed or cancelled deployments can be retried (current status: '{deployment.status}')."
        )

    db.query(models.DeploymentStep).filter(models.DeploymentStep.deployment_id == id).delete()
    deployment.status = "pending"
    deployment.completed_at = None
    db.commit()
    db.refresh(deployment)

    background_tasks.add_task(execute_deployment_task, deployment.id)
    return deployment


@router.post("/{id}/migrate", response_model=schemas.DeploymentRunOut)
def migrate_deployment(
    id: int,
    payload: schemas.MigrateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(require_agent_access)
):
    """
    Clone a completed deployment and re-run it against a different target instance.

    The caller must supply the target environment's DB credentials (they differ
    from the source environment). If no SSH overrides are provided the source
    deployment's SSH server is reused (common when app-server is shared).
    """
    old_run = db.query(models.DeploymentRun).filter(
        models.DeploymentRun.id == id,
        models.DeploymentRun.user_id == current_user.id
    ).first()
    if not old_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment run not found.")
    if old_run.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only completed deployments can be migrated (current status: '{old_run.status}')."
        )
    if payload.target_instance == old_run.target_instance:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Target instance must differ from the source instance ('{old_run.target_instance}')."
        )

    # SSH: use supplied override if provided, otherwise inherit from source run
    ssh_host     = payload.ssh_host     or old_run.ssh_host
    ssh_port     = payload.ssh_port     if payload.ssh_host else (old_run.ssh_port or 22)
    ssh_username = payload.ssh_username or old_run.ssh_username
    ssh_password = payload.ssh_password or old_run.ssh_password

    new_run = models.DeploymentRun(
        user_id=current_user.id,
        source_doc_type=old_run.source_doc_type,
        source_doc_name=f"Migrated from {old_run.target_instance}: {old_run.source_doc_name or 'Deployment'}",
        source_content=old_run.source_content,
        target_instance=payload.target_instance,
        # Managed-resource references (credentials resolved at execution time).
        # SSH server: override or inherit from the source run.
        environment_id=payload.environment_id,
        ssh_server_id=payload.ssh_server_id or old_run.ssh_server_id,
        # Git — identical to source
        git_repo_url=old_run.git_repo_url,
        git_branch=old_run.git_branch,
        git_token=old_run.git_token,
        # SSH — override or inherit
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_username=ssh_username,
        ssh_password=ssh_password,
        # DB — always use TARGET environment credentials
        db_host=payload.db_host,
        db_port=payload.db_port,
        db_sid=payload.db_sid,
        db_user=payload.db_user,
        db_password=payload.db_password,
        status="pending",
    )
    db.add(new_run)
    db.commit()
    db.refresh(new_run)

    background_tasks.add_task(execute_deployment_task, new_run.id)
    return new_run
