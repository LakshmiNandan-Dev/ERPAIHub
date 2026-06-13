import re
from typing import Optional
from app import database, models, llm_service


def audit_message_rlaif(
    message_id: int,
    query: str,
    rag_context: str,
    assistant_response: str,
    provider: str = "ollama",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
):
    """
    RLAIF audit — critiques the assistant response and stores score/reasoning in DB.
    Uses the same LLM provider the user has configured.
    """
    if not rag_context:
        rag_context = "No specific reference document was uploaded for this query."

    prompt = (
        "You are an Oracle EBS QA Audit Agent. Your job is to perform a strict automated critique (RLAIF) "
        "of a generated AI response against verified database/documentation context.\n\n"
        "=== Verified Context ===\n"
        f"{rag_context}\n\n"
        "=== User Query ===\n"
        f"{query}\n\n"
        "=== Generated AI Response ===\n"
        f"{assistant_response}\n\n"
        "Perform a rigorous audit and check:\n"
        "1. Faithfulness: Does the response contain any information, scripts, or details NOT supported by "
        "or conflicting with the Verified Context? (i.e. hallucination?)\n"
        "2. Correctness: Are the Oracle EBS SQL scripts, configurations, or commands accurate?\n"
        "3. Helpfulness: Did the response correctly and safely answer the user query?\n\n"
        "You must output ONLY a valid JSON object (no markdown, no extra text):\n"
        '{"score": 1 or -1, "reasoning": "...", "suggested_correction": "..."}'
    )

    try:
        eval_content = llm_service.complete_sync(
            messages=[{"role": "user", "content": prompt}],
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
        ).strip()

        print(f"[RLAIF] Raw critique: {eval_content[:200]}")

        json_match = re.search(r"\{.*\}", eval_content, re.DOTALL)
        if json_match:
            eval_content = json_match.group(0)

        import json
        eval_data = json.loads(eval_content)
        score = eval_data.get("score", 1)
        reasoning = eval_data.get("reasoning", "No explanation provided.")
        correction = eval_data.get("suggested_correction", "")

        try:
            score_val = -1 if int(score) < 0 else 1
        except (ValueError, TypeError):
            score_val = 1

        db = database.SessionLocal()
        try:
            msg = db.query(models.ChatMessage).filter(models.ChatMessage.id == message_id).first()
            if msg:
                msg.rlaif_rating = score_val
                msg.rlaif_critique = reasoning
                msg.rlaif_correction = correction or None
                db.commit()
                print(f"[RLAIF] Msg #{message_id} score={score_val}")
        except Exception as e:
            print(f"[RLAIF] DB update error: {e}")
        finally:
            db.close()

    except Exception as e:
        print(f"[RLAIF] Audit failed: {e}")
        db = database.SessionLocal()
        try:
            msg = db.query(models.ChatMessage).filter(models.ChatMessage.id == message_id).first()
            if msg:
                msg.rlaif_rating = 1
                msg.rlaif_critique = f"Critique agent unavailable: {e}"
                db.commit()
        except Exception:
            pass
        finally:
            db.close()
