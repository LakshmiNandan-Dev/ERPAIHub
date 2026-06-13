from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
import json
import asyncio
import time
import re as _re
from app.core import database, config_service, telemetry
from app import schemas, models
from app.ml import rlaif_service
from app.core.rag import rag_service
from app.core.llm import llm_service, semantic_cache, llm_guard_service
from app.core.auth.auth import get_current_user

router = APIRouter(
    prefix="/chat",
    tags=['Chat']
)

import os
_OLLAMA_BASE = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_URL = f"{_OLLAMA_BASE}/api/chat"


def get_ebs_fallback_response(query: str) -> str:
    query_lower = query.lower()
    if "deploy" in query_lower or "patch" in query_lower or "compile" in query_lower or "fmb" in query_lower or "pls" in query_lower or "ldt" in query_lower:
        return (
            "🛠️ **Oracle EBS Developer Agent (Auto-Healed Response)**\n\n"
            "I detected a request related to code deployment or package compilation. "
            "To deploy custom objects in Oracle EBS, you can use the following commands:\n\n"
            "1. **PL/SQL Packages (`.pls`/`.plb`)**:\n"
            "   ```sql\n"
            "   sqlplus apps/apps @xxap_supplier_pkg.pls\n"
            "   ```\n"
            "2. **Forms Binaries (`.fmb`)**:\n"
            "   ```bash\n"
            "   frmcmp_batch Module=custom.fmb Userid=apps/apps Output_File=custom.fmx Module_Type=form Compile_All=special\n"
            "   ```\n"
            "3. **FNDLOAD Templates (`.ldt`)**:\n"
            "   ```bash\n"
            "   FNDLOAD apps/apps 0 Y UPLOAD @FND:patch/115/import/afscursp.lct custom_resp.ldt\n"
            "   ```\n"
            "4. **Workflow Templates (`.wft`)**:\n"
            "   ```bash\n"
            "   WFLOAD apps/apps 0 Y UPLOAD custom_workflow.wft\n"
            "   ```\n\n"
            "Would you like me to trigger a target compilation or setup an SSH connection to your application node?"
        )
    return (
        "💡 **Oracle EBS Developer Agent (Auto-Healed Response)**\n\n"
        "Here are standard diagnostic actions for Oracle EBS:\n"
        "- **Check concurrent requests**: `SELECT request_id, phase_code, status_code FROM fnd_concurrent_requests WHERE request_id = :id;`\n"
        "- **Compile invalid objects**: Run `utl_recomp.reconstitute()` or `@$AD_TOP/patch/115/sql/adcompon.sql`\n"
        "- **Find forms path**: Check `$AU_TOP/forms/US` or `$PROD_TOP/forms/US`\n\n"
        "Please let me know how I can assist you with your EBS system!"
    )


_MAX_HISTORY_MESSAGES = 20   # recent turns kept verbatim
_COMPRESS_THRESHOLD   = 40   # total messages before compression triggers

# ── Rule-based key-fact extractor ─────────────────────────────────────────────

_ENV_RE  = _re.compile(r'\b(DEV|UAT2?|PROD)\b')
_FILE_RE = _re.compile(r'\b[\w./-]+\.(?:pls|plb|fmb|fmx|ldt|sql|sh|wft|xml)\b', _re.IGNORECASE)
_ERR_RE  = _re.compile(r'\b(?:ORA|PLS|APP)-\d{4,6}\b')
_CMD_RE  = _re.compile(r'\b(?:sqlplus|fndload|frmcmp(?:_batch)?|wfload|adop|adadmin|adpatch|xmlimporter)\b', _re.IGNORECASE)


def _extract_key_facts(messages) -> str:
    envs, files, errors, cmds = set(), set(), set(), set()
    for msg in messages:
        text = msg.content if hasattr(msg, 'content') else msg.get('content', '')
        envs.update(_ENV_RE.findall(text))
        files.update(m.lower() for m in _FILE_RE.findall(text))
        errors.update(_ERR_RE.findall(text))
        cmds.update(m.lower() for m in _CMD_RE.findall(text))
    if not any([envs, files, errors, cmds]):
        return ""
    lines = ["**Auto-extracted EBS context:**"]
    if envs:   lines.append(f"- Environments: {', '.join(sorted(envs))}")
    if files:  lines.append(f"- Files: {', '.join(sorted(files))}")
    if errors: lines.append(f"- Errors seen: {', '.join(sorted(errors))}")
    if cmds:   lines.append(f"- Commands used: {', '.join(sorted(cmds))}")
    return "\n".join(lines)


# ── LLM-based history compression ─────────────────────────────────────────────

async def _maybe_compress_session(
    session: models.ChatSession,
    all_messages: list,
    db,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
) -> str:
    """
    When history exceeds _COMPRESS_THRESHOLD, summarize the old messages into a
    compact context block (key facts + LLM narrative summary) and cache it on the
    session row.  Returns the block to inject, or "" if history is still short.
    """
    if len(all_messages) <= _COMPRESS_THRESHOLD:
        return ""

    old_messages  = all_messages[:-_MAX_HISTORY_MESSAGES]
    last_old_id   = old_messages[-1].id

    # Re-use existing summary if it already covers these messages
    if (
        session.summarized_through_id
        and session.summarized_through_id >= last_old_id
        and session.context_summary
    ):
        return session.context_summary

    key_facts = _extract_key_facts(old_messages)

    # Cap the text sent to the summarizer so small models don't choke
    convo_lines = []
    for m in old_messages[-30:]:
        convo_lines.append(f"{m.role.upper()}: {m.content[:300]}")
    convo_text = "\n".join(convo_lines)

    summary_prompt = [
        {"role": "system", "content": (
            "You are a concise Oracle EBS assistant. Summarize the conversation "
            "below in 3-5 sentences covering: the user's goal, files/packages "
            "discussed, environments mentioned, problems encountered, and decisions "
            "made. Be factual — do not add anything not in the text."
        )},
        {"role": "user", "content": f"Conversation to summarize:\n\n{convo_text}"},
    ]

    llm_summary = ""
    try:
        loop = asyncio.get_event_loop()
        llm_summary = await loop.run_in_executor(
            None,
            lambda: llm_service.complete_sync(
                messages=summary_prompt,
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
            ),
        )
    except Exception as exc:
        print(f"[Memory] Summary LLM call failed: {exc}")

    parts = []
    if key_facts:
        parts.append(key_facts)
    if llm_summary:
        parts.append(f"**Earlier conversation summary:**\n{llm_summary.strip()}")

    combined = "\n\n".join(parts) if parts else ""

    if combined:
        session.context_summary = combined
        session.summarized_through_id = last_old_id
        try:
            db.commit()
        except Exception as exc:
            print(f"[Memory] Failed to persist summary: {exc}")

    return combined


def _build_ollama_messages(messages, message_content: str, rag_context: str, context_block: str = "") -> list:
    """Build the full Ollama messages list with system prompt and chat history."""
    # Sliding window — keep only the most recent messages verbatim
    if len(messages) > _MAX_HISTORY_MESSAGES:
        messages = messages[-_MAX_HISTORY_MESSAGES:]

    system_content = (
        "You are an expert Oracle E-Business Suite (EBS) developer assistant. "
        "Help developers with PL/SQL package compilation, SQL script execution, ADOP patching, FNDLOAD data uploads, "
        "Forms compilation, Workflow uploads, OAF page imports, SSH-based deployments, and general EBS configuration tasks. "
        "Provide complete, accurate command examples and scripts tailored to Oracle EBS 12.x environments. "
        "When a question is ambiguous, ask for clarification rather than guessing."
    )
    system_prompt = {"role": "system", "content": system_content}

    ollama_msgs = []

    # Inject compressed context from older messages (only when history was trimmed)
    if context_block:
        ollama_msgs.append({"role": "user", "content": f"[EARLIER CONVERSATION CONTEXT]\n{context_block}\n[END EARLIER CONTEXT]"})
        ollama_msgs.append({"role": "assistant", "content": "Understood. I have noted the earlier conversation context and will use it to inform my answers."})

    # Add recent messages verbatim up to the second-to-last
    for msg in messages[:-1]:
        ollama_msgs.append({"role": msg.role, "content": msg.content})
        
    # Inject RAG context directly into the current active user message
    if messages:
        last_msg = messages[-1]
        if last_msg.role == "user" and rag_context:
            injected_content = (
                "[VERIFIED REFERENCE DOCUMENTATION]\n"
                f"{rag_context}\n"
                "[END VERIFIED REFERENCE DOCUMENTATION]\n\n"
                "INSTRUCTION: If the verified reference documentation above is relevant to the question below, you may use it to provide a more accurate answer. "
                "Otherwise, feel free to ignore it and answer the user's question directly using general Oracle EBS guidelines.\n\n"
                f"QUESTION: {last_msg.content}"
            )
            ollama_msgs.append({"role": "user", "content": injected_content})
        else:
            ollama_msgs.append({"role": last_msg.role, "content": last_msg.content})
    else:
        # Fallback if messages list is empty
        if rag_context:
            injected_content = (
                "[VERIFIED REFERENCE DOCUMENTATION]\n"
                f"{rag_context}\n"
                "[END VERIFIED REFERENCE DOCUMENTATION]\n\n"
                "INSTRUCTION: If the verified reference documentation above is relevant to the question below, you may use it to answer. "
                "Otherwise, ignore it and answer directly.\n\n"
                f"QUESTION: {message_content}"
            )
            ollama_msgs.append({"role": "user", "content": injected_content})
        else:
            ollama_msgs.append({"role": "user", "content": message_content})
            
    return [system_prompt] + ollama_msgs


@router.post("/sessions", response_model=schemas.ChatSessionOut, status_code=status.HTTP_201_CREATED)
def create_session(session_data: schemas.ChatSessionCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    new_session = models.ChatSession(user_id=current_user.id, title=session_data.title)
    db.add(new_session)
    db.commit()
    db.refresh(new_session)
    return new_session


@router.get("/sessions", response_model=list[schemas.ChatSessionOut])
def get_sessions(db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    return db.query(models.ChatSession).filter(models.ChatSession.user_id == current_user.id).order_by(models.ChatSession.created_at.desc()).all()


@router.patch("/sessions/{session_id}", response_model=schemas.ChatSessionOut)
def rename_session(session_id: int, data: schemas.ChatSessionRename, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    session = db.query(models.ChatSession).filter(models.ChatSession.id == session_id, models.ChatSession.user_id == current_user.id).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")
    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Title cannot be empty")
    session.title = title
    db.commit()
    db.refresh(session)
    return session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    session = db.query(models.ChatSession).filter(models.ChatSession.id == session_id, models.ChatSession.user_id == current_user.id).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")
    db.delete(session)
    db.commit()


@router.get("/sessions/{session_id}/messages", response_model=list[schemas.ChatMessageOut])
def get_session_messages(session_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    session = db.query(models.ChatSession).filter(models.ChatSession.id == session_id, models.ChatSession.user_id == current_user.id).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")
    return db.query(models.ChatMessage).filter(models.ChatMessage.session_id == session_id).order_by(models.ChatMessage.created_at.asc()).all()


@router.post("/sessions/{session_id}/agent_runs", response_model=schemas.AgentRunOut, status_code=status.HTTP_201_CREATED)
def start_agent_run(session_id: int, run_data: schemas.AgentRunCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    session = db.query(models.ChatSession).filter(models.ChatSession.id == session_id, models.ChatSession.user_id == current_user.id).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")
    agent = db.query(models.Agent).filter(models.Agent.id == run_data.agent_id).first()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    new_run = models.AgentRun(session_id=session_id, agent_id=run_data.agent_id)
    db.add(new_run)
    db.commit()
    db.refresh(new_run)
    return new_run


# ─── Streaming endpoint ────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/stream")
async def stream_message(
    session_id: int,
    message_data: schemas.MessageCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Streams LLM tokens using Server-Sent Events (SSE).
    Provider is selected via X-LLM-Provider / X-LLM-Model / X-LLM-Api-Key headers.
    """
    # Read LLM provider config from request headers (set by frontend from localStorage)
    llm_provider = request.headers.get("X-LLM-Provider", "ollama")
    llm_model    = request.headers.get("X-LLM-Model") or None
    llm_api_key  = request.headers.get("X-LLM-Api-Key") or None
    llm_base_url = request.headers.get("X-LLM-Base-Url") or None
    # Resolve the API key (and model/base_url fallbacks) from the admin-managed
    # credential store when the browser didn't supply one.
    llm_provider, llm_model, llm_api_key, llm_base_url = config_service.resolve_llm(
        llm_provider, llm_model, llm_api_key, llm_base_url, db, agent="chat",
        route_text=message_data.content,
    )
    # 1. Verify session
    session = db.query(models.ChatSession).filter(
        models.ChatSession.id == session_id,
        models.ChatSession.user_id == current_user.id
    ).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")

    # 2. Save user message
    user_message = models.ChatMessage(
        session_id=session_id,
        role="user",
        content=message_data.content,
        agent_run_id=message_data.agent_run_id,
        agent_step_id=message_data.agent_step_id
    )
    db.add(user_message)
    db.commit()

    # 2.5. Classify the message once — shared by all downstream gates
    # (LLM Guard, RAG, semantic cache, RLAIF, history compression).
    _cleaned = message_data.content.strip().lower().rstrip("?.,!")
    is_simple_query = (
        len(_cleaned) < 25
        or len(_cleaned.split()) <= 3
        or _cleaned in {"hi", "hello", "hey", "good morning", "good afternoon",
                        "good evening", "thanks", "thank you", "ok", "okay",
                        "yes", "no", "sure", "great", "got it", "understood"}
    )

    # 2.5a. LLM Guard — input scan (skipped for simple/short messages)
    if not is_simple_query:
        _guard_in = await asyncio.get_event_loop().run_in_executor(
            None, llm_guard_service.scan_input, message_data.content
        )
        if _guard_in.blocked:
            _block_msg = (
                f"🛡️ **Content Safety Block**\n\n"
                f"Your message was flagged by the **{_guard_in.scanner}** scanner "
                f"(risk score: {_guard_in.risk_score:.2f}).\n\n"
                "Please rephrase your request without potentially unsafe content."
            )
            async def _blocked_input_stream():
                yield f"data: {json.dumps(_block_msg)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(
                _blocked_input_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    # 2.5. Retrieve chat history to detect previous interview loops
    history = db.query(models.ChatMessage).filter(
        models.ChatMessage.session_id == session_id
    ).order_by(models.ChatMessage.created_at.desc()).limit(5).all()

    lower_content = message_data.content.lower().strip()

    # Questions / informational requests — never trigger the deployment agent
    _QUESTION_STARTERS = (
        "how to", "how do", "how can", "how should", "how would", "how does",
        "what is", "what are", "what's", "whats", "what does",
        "why ", "when ", "which ", "where ",
        "explain", "describe", "tell me", "show me",
        "steps to", "steps for", "procedure", "process for",
        "guide", "tutorial", "difference between",
        "can you explain", "can you describe",
        "what command", "what commands",
    )
    is_informational = any(lower_content.startswith(p) for p in _QUESTION_STARTERS)

    is_deploy_intent = (
        not is_informational
        and ("deploy" in lower_content or "patch" in lower_content or "compile" in lower_content)
    )
    
    # Check if files or extensions are present
    has_files = (
        ".sql" in lower_content or 
        ".pls" in lower_content or 
        "sqlplus" in lower_content or 
        "alter package" in lower_content or 
        ".sh" in lower_content or
        ".ldt" in lower_content or
        "fndload" in lower_content or
        ".fmb" in lower_content or
        "frmcmp" in lower_content or
        ".wft" in lower_content or
        "wfload" in lower_content or
        ".xml" in lower_content or
        "xmlimporter" in lower_content
    )
    
    # Check if target environment is specified
    target_env = None
    if "dev" in lower_content:
        target_env = "DEV"
    elif "uat2" in lower_content:
        target_env = "UAT2"
    elif "uat" in lower_content:
        target_env = "UAT"
    elif "prod" in lower_content:
        target_env = "PROD"

    # Detect if we were already in an active interview
    was_interviewing = False
    if len(history) >= 2:
        # history[0] is the user message we just saved
        prev_assistant = history[1]
        if prev_assistant.role == "assistant" and "code deployment agent interview" in prev_assistant.content.lower():
            was_interviewing = True
            
            # Combine history turns to parse variables across multiple turns
            combined_history = ""
            for h_msg in reversed(history[:4]):
                combined_history += " " + h_msg.content.lower()
                
            # Scan combined context for target environment
            if "dev" in combined_history:
                target_env = "DEV"
            elif "uat2" in combined_history:
                target_env = "UAT2"
            elif "uat" in combined_history:
                target_env = "UAT"
            elif "prod" in combined_history:
                target_env = "PROD"
                
            # Scan combined context for files
            if (
                ".sql" in combined_history or 
                ".pls" in combined_history or 
                "sqlplus" in combined_history or 
                "alter package" in combined_history or 
                ".sh" in combined_history or
                ".ldt" in combined_history or
                "fndload" in combined_history or
                ".fmb" in combined_history or
                "frmcmp" in combined_history or
                ".wft" in combined_history or
                "wfload" in combined_history or
                ".xml" in combined_history or
                "xmlimporter" in combined_history
            ):
                has_files = True

    is_deploy = False
    if (is_deploy_intent or was_interviewing) and not is_informational:
        # We need BOTH files AND target environment to trigger the actual code deployment agent!
        if has_files and target_env:
            is_deploy = True
        else:
            # We are missing parameters! Let's interview the user.
            missing_items = []
            if not target_env:
                missing_items.append("• **Target EBS Environment** (DEV, UAT, UAT2, or PROD)")
            if not has_files:
                missing_items.append("• **EBS Code File Names or Script Commands** (e.g. `.sql` scripts, `.pls` packages, `.ldt` load files, `.fmb` Forms definitions, or shell utilities)")
                
            response_text = (
                f"🧠 **EBS Code Deployment Agent Interview**\n\n"
                f"I detected that you want to launch a target code deployment, but some parameters are missing to invoke the agent safely.\n\n"
                f"Could you please specify:\n"
                + "\n".join(missing_items) + "\n\n"
                f"Once you reply with these details, I will immediately run the deployment background worker!"
            )
            
            # Save assistant message
            assistant_msg = models.ChatMessage(
                session_id=session_id,
                role="assistant",
                content=response_text
            )
            db.add(assistant_msg)
            db.commit()
            
            async def interview_stream():
                words = response_text.split(" ")
                for word in words:
                    yield f"data: {json.dumps(word + ' ')}\n\n"
                    time.sleep(0.03)
                yield "data: [DONE]\n\n"
                
            return StreamingResponse(
                interview_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                }
            )

    if is_deploy:
        # We have both files and target_env! Let's construct a premium consolidated source content.
        assembled_content = message_data.content
        if was_interviewing:
            # Look back in history to pull original instruction
            for h_msg in history[1:4]:
                if h_msg.role == "user" and not ("code deployment agent interview" in h_msg.content.lower()):
                    assembled_content = f"{h_msg.content}\n\nAdditional details:\n{message_data.content}"
                    break
        
        # Determine doc type
        doc_type = "chat"
        if "confluence" in assembled_content.lower():
            doc_type = "confluence"
        elif "pdf" in assembled_content.lower():
            doc_type = "pdf"
        elif "word" in assembled_content.lower():
            doc_type = "word"
            
        from app.modules.dba.deployment.deployments import execute_deployment_task
        
        # Pull optional SSH parameters from target settings or user profile config
        ssh_host = localStorage_or_none = None # Loaded dynamically in background thread task if needed
        
        # Check if git repository is mentioned or used. Default to None (Direct Local)
        git_repo_url = None
        git_branch = None
        if "git" in assembled_content.lower() or "github" in assembled_content.lower() or "gitlab" in assembled_content.lower():
            git_repo_url = "https://github.com/oracle-ebs-org/custom-ap-module"
            git_branch = "main"

        dep_run = models.DeploymentRun(
            user_id=current_user.id,
            source_doc_type=doc_type,
            source_doc_name="Chat Interview Auto-Deployment",
            source_content=assembled_content,
            target_instance=target_env,
            git_repo_url=git_repo_url,
            git_branch=git_branch,
            status="pending"
        )
        db.add(dep_run)
        db.commit()
        db.refresh(dep_run)
        
        background_tasks.add_task(execute_deployment_task, dep_run.id)
        
        async def event_stream():
            git_details = (
                f"- **Git Repository**: `{git_repo_url}` (`{git_branch}` branch)\n"
                if git_repo_url
                else "- **Source Files**: Direct Local Compilation (No Git sync)\n"
            )
            response_text = (
                f"🚀 **EBS Code Deployment AI Agent Activated!**\n\n"
                f"Thank you! I have successfully collected all parameters and scheduled the background compilation:\n\n"
                f"- **Deployment Run**: `#{dep_run.id}`\n"
                f"- **Target Environment**: `{target_env}`\n"
                f"- **Extracted Source Instructions**:\n```\n{assembled_content[:250]}...\n```\n"
                f"{git_details}\n"
                f"You can now switch your **Active Agent** at the top header to **🚀 Code Deployment Agent** to watch the remote terminal logs in real-time!"
            )
            
            # Save assistant message
            new_db = database.SessionLocal()
            try:
                assistant_message = models.ChatMessage(
                    session_id=session_id,
                    role="assistant",
                    content=response_text
                )
                new_db.add(assistant_message)
                new_db.commit()
            except Exception as se:
                print("[Auto-Deploy] Save message failed:", se)
            finally:
                new_db.close()
                
            # Stream tokens
            words = response_text.split(" ")
            for word in words:
                yield f"data: {json.dumps(word + ' ')}\n\n"
                time.sleep(0.03)
            yield "data: [DONE]\n\n"
            
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )

    # 3. Auto-title session on first message
    msg_count = db.query(models.ChatMessage).filter(models.ChatMessage.session_id == session_id).count()
    if msg_count == 1 and (not session.title or session.title == "New Conversation"):
        session.title = message_data.content[:50] + ('...' if len(message_data.content) > 50 else '')
        db.commit()

    # 4. Fetch history
    messages = db.query(models.ChatMessage).filter(
        models.ChatMessage.session_id == session_id
    ).order_by(models.ChatMessage.created_at.asc()).all()

    # 4.5. Compress old history into a summary block when session grows long
    context_block = await _maybe_compress_session(
        session, messages, db, llm_provider, llm_model, llm_api_key, llm_base_url
    )

    # 5. RAG context — skip for simple queries (is_simple_query computed at step 2.5)
    rag_context = ""
    if not is_simple_query:
        try:
            rag_context = rag_service.query_rag(message_data.content)
        except Exception as e:
            print(f"[RAG] query_rag failed: {e}")
            rag_context = ""

    print(f"[RAG] skipped={is_simple_query} | context={bool(rag_context)} ({len(rag_context)} chars)")

    # 5.5. Semantic cache — only safe for standalone, context-free questions:
    # the first turn of a session (no prior conversation the answer depends on),
    # substantive (not a greeting), not a deploy/interview flow, and with no RAG
    # context (so indexing new docs can't silently stale a cached answer).
    # Matching is scoped by provider+model, so different models never share answers.
    semantic_eligible = (
        not is_simple_query
        and not rag_context
        and not is_deploy
        and not was_interviewing
        and msg_count == 1
    )
    if semantic_eligible:
        cached_answer = semantic_cache.lookup(message_data.content, llm_provider, llm_model)
        if cached_answer:
            async def cache_stream():
                # Persist the assistant turn so session history stays consistent.
                new_db = database.SessionLocal()
                try:
                    new_db.add(models.ChatMessage(
                        session_id=session_id, role="assistant",
                        content=cached_answer, agent_run_id=message_data.agent_run_id,
                    ))
                    new_db.commit()
                except Exception as e:
                    print(f"[SemanticCache] save failed: {e}")
                finally:
                    new_db.close()

                telemetry.stream_open(current_user.id)
                banner = "⚡ *Instant answer — semantically matched a previous question.*\n\n"
                yield f"data: {json.dumps(banner)}\n\n"
                for word in cached_answer.split(" "):
                    yield f"data: {json.dumps(word + ' ')}\n\n"
                    await asyncio.sleep(0.005)
                # A cache hit consumes no model tokens — record the request truthfully.
                background_tasks.add_task(
                    telemetry.record_llm,
                    user_id=current_user.id, username=current_user.username,
                    endpoint="chat.stream.cache_hit", provider=llm_provider, model=llm_model,
                    prompt_tokens=0, completion_tokens=0, context_chars=len(cached_answer),
                )
                telemetry.stream_close(current_user.id)
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                cache_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    llm_messages = _build_ollama_messages(messages, message_data.content, rag_context, context_block)

    # Telemetry: measure the assembled context size and capture token usage.
    prompt_text = "".join(m.get("content", "") for m in llm_messages)
    context_chars = len(prompt_text)
    usage: dict = {}

    # 6. Stream generator — yields SSE lines
    async def event_stream():
        full_content = []
        telemetry.stream_open(current_user.id)
        if rag_context:
            rag_label = "📚 **RAG Knowledge Base Agent Invoked!** Searching indexed reference documentation... \n\n"
            full_content.append(rag_label)
            yield f"data: {json.dumps(rag_label)}\n\n"

        refusal_detected = False
        got_token = False
        try:
            buffering = True
            buffer_text = ""

            async for token in llm_service.stream_tokens(
                llm_messages, llm_provider, llm_model, llm_api_key, llm_base_url, usage=usage
            ):
                if buffering:
                    buffer_text += token
                    if len(buffer_text) >= 40:
                        buffering = False
                        _REFUSALS = ["can't help", "cannot help", "sorry, but", "unable to",
                                     "cannot fulfill", "apologize, but", "not allowed"]
                        if any(kw in buffer_text.lower() for kw in _REFUSALS):
                            refusal_detected = True
                            break
                        full_content.append(buffer_text)
                        got_token = True
                        yield f"data: {json.dumps(buffer_text)}\n\n"
                else:
                    full_content.append(token)
                    got_token = True
                    yield f"data: {json.dumps(token)}\n\n"

            # Flush buffer if stream ended before 40 chars
            if buffering and buffer_text and not refusal_detected:
                _REFUSALS = ["can't help", "cannot help", "sorry, but", "unable to",
                             "cannot fulfill", "apologize, but", "not allowed"]
                if any(kw in buffer_text.lower() for kw in _REFUSALS):
                    refusal_detected = True
                else:
                    full_content.append(buffer_text)
                    got_token = True
                    yield f"data: {json.dumps(buffer_text)}\n\n"

            if refusal_detected:
                fallback_msg = get_ebs_fallback_response(message_data.content)
                full_content.clear()
                full_content.append(fallback_msg)
                for word in fallback_msg.split(" "):
                    yield f"data: {json.dumps(word + ' ')}\n\n"
                    await asyncio.sleep(0.02)
            elif not got_token:
                # Model connected but returned nothing — surface a clear warning.
                full_content.clear()
                warn = (
                    f"The '{llm_provider}' model returned an empty response. "
                    "The selected model may be unavailable or not pulled. "
                    "Check the model in AI Model settings, or ask an administrator to verify the provider."
                )
                yield f"data: {json.dumps({'error': warn})}\n\n"
                return

        except Exception as e:
            # Surface connection / auth / model-availability failures to the user.
            full_content.clear()
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return
        finally:
            # Save complete response to DB regardless
            if full_content:
                assembled = "".join(full_content)

                # LLM Guard — output scan (skipped for simple queries, same gate as input)
                if not is_simple_query:
                    _guard_out = await asyncio.get_event_loop().run_in_executor(
                        None, llm_guard_service.scan_output, message_data.content, assembled
                    )
                    if _guard_out.blocked:
                        _filtered_notice = (
                            f"⚠️ **Response filtered by content-safety scanner "
                            f"({_guard_out.scanner})**.\n\n"
                            "The model output contained potentially sensitive content "
                            "and has been redacted. Please rephrase your question."
                        )
                        assembled = _filtered_notice
                        yield f"data: {json.dumps(_filtered_notice)}\n\n"
                    elif _guard_out.text and _guard_out.text != assembled:
                        assembled = _guard_out.text

                new_db = database.SessionLocal()
                try:
                    assistant_message = models.ChatMessage(
                        session_id=session_id,
                        role="assistant",
                        content=assembled,
                        agent_run_id=message_data.agent_run_id
                    )
                    new_db.add(assistant_message)
                    new_db.commit()

                    # Populate the semantic cache with this fresh, standalone answer
                    # (skip refusals/fallbacks — only genuine model output is cached).
                    if semantic_eligible and got_token and not refusal_detected:
                        semantic_cache.store(
                            message_data.content, assembled, llm_provider, llm_model
                        )

                    # Trigger background RLAIF Audit — only for substantive queries
                    if not is_simple_query:
                        background_tasks.add_task(
                            rlaif_service.audit_message_rlaif,
                            message_id=assistant_message.id,
                            query=message_data.content,
                            rag_context=rag_context,
                            assistant_response=assembled,
                            provider=llm_provider,
                            model=llm_model,
                            api_key=llm_api_key,
                            base_url=llm_base_url,
                        )
                    # Telemetry: record token usage + context size for this turn
                    background_tasks.add_task(
                        telemetry.record_llm,
                        user_id=current_user.id,
                        username=current_user.username,
                        endpoint="chat.stream",
                        provider=llm_provider,
                        model=llm_model,
                        prompt_tokens=usage.get("prompt_tokens") or telemetry.est_tokens(prompt_text),
                        completion_tokens=usage.get("completion_tokens") or telemetry.est_tokens(assembled),
                        context_chars=context_chars,
                    )
                except Exception as save_err:
                    print(f"[Stream] Failed to save assistant message: {save_err}")
                finally:
                    new_db.close()

            telemetry.stream_close(current_user.id)
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
