from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
import json
from app.core import database
from app import schemas, models
from app.routers.auth import get_current_user

router = APIRouter(
    prefix="/rl",
    tags=['Reinforcement Learning Hub']
)

@router.post("/messages/{message_id}/feedback", response_model=schemas.ChatMessageOut)
def submit_feedback(
    message_id: int,
    feedback_data: schemas.FeedbackSubmit,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Fetch the message
    message = db.query(models.ChatMessage).filter(models.ChatMessage.id == message_id).first()
    if not message:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat message not found")
    
    # Ensure it's an assistant message and belongs to the user's session
    if message.role != "assistant":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Feedback can only be submitted for assistant responses")
    
    session = db.query(models.ChatSession).filter(
        models.ChatSession.id == message.session_id,
        models.ChatSession.user_id == current_user.id
    ).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to edit feedback for this session")

    # Update feedback columns
    message.feedback_rating = feedback_data.rating
    message.feedback_correction = feedback_data.correction
    message.feedback_notes = feedback_data.notes

    db.commit()
    db.refresh(message)
    return message

@router.get("/stats")
def get_rl_stats(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    # --- Human Feedback (RLHF) Stats ---
    total_rated = db.query(models.ChatMessage).filter(
        models.ChatMessage.role == "assistant",
        models.ChatMessage.feedback_rating.isnot(None)
    ).count()

    thumbs_up = db.query(models.ChatMessage).filter(
        models.ChatMessage.role == "assistant",
        models.ChatMessage.feedback_rating == 1
    ).count()

    thumbs_down = db.query(models.ChatMessage).filter(
        models.ChatMessage.role == "assistant",
        models.ChatMessage.feedback_rating == -1
    ).count()

    with_corrections = db.query(models.ChatMessage).filter(
        models.ChatMessage.role == "assistant",
        models.ChatMessage.feedback_rating == -1,
        models.ChatMessage.feedback_correction.isnot(None),
        models.ChatMessage.feedback_correction != ""
    ).count()

    # --- AI Feedback (RLAIF) Stats ---
    rlaif_total = db.query(models.ChatMessage).filter(
        models.ChatMessage.role == "assistant",
        models.ChatMessage.rlaif_rating.isnot(None)
    ).count()

    rlaif_passed = db.query(models.ChatMessage).filter(
        models.ChatMessage.role == "assistant",
        models.ChatMessage.rlaif_rating == 1
    ).count()

    rlaif_failed = db.query(models.ChatMessage).filter(
        models.ChatMessage.role == "assistant",
        models.ChatMessage.rlaif_rating == -1
    ).count()

    rlaif_corrections = db.query(models.ChatMessage).filter(
        models.ChatMessage.role == "assistant",
        models.ChatMessage.rlaif_rating == -1,
        models.ChatMessage.rlaif_correction.isnot(None),
        models.ChatMessage.rlaif_correction != ""
    ).count()

    return {
        "total_rated": total_rated,
        "thumbs_up": thumbs_up,
        "thumbs_down": thumbs_down,
        "with_corrections": with_corrections,
        "rlaif_total": rlaif_total,
        "rlaif_passed": rlaif_passed,
        "rlaif_failed": rlaif_failed,
        "rlaif_corrections": rlaif_corrections
    }

@router.get("/export")
def export_preference_dataset(
    type: str = Query("rlhf", regex="^(rlhf|rlaif)$"),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Exports preference data in DPO format as a JSONL stream.
    Supports either Human Feedback (type=rlhf) or AI Feedback (type=rlaif).
    """
    if type == "rlaif":
        # Fetch RLAIF audited messages
        messages = db.query(models.ChatMessage).filter(
            models.ChatMessage.role == "assistant",
            models.ChatMessage.rlaif_rating.isnot(None)
        ).order_by(models.ChatMessage.created_at.asc()).all()
    else:
        # Fetch human feedback messages
        messages = db.query(models.ChatMessage).filter(
            models.ChatMessage.role == "assistant",
            models.ChatMessage.feedback_rating.isnot(None)
        ).order_by(models.ChatMessage.created_at.asc()).all()

    def generate_jsonl():
        for msg in messages:
            # Find the user query preceding this message
            prev_user_msg = db.query(models.ChatMessage).filter(
                models.ChatMessage.session_id == msg.session_id,
                models.ChatMessage.role == "user",
                models.ChatMessage.created_at < msg.created_at
            ).order_by(models.ChatMessage.created_at.desc()).first()

            prompt = prev_user_msg.content if prev_user_msg else "Ask Oracle EBS expert"

            if type == "rlaif":
                if msg.rlaif_rating == -1 and msg.rlaif_correction:
                    dpo_sample = {
                        "prompt": prompt,
                        "chosen": msg.rlaif_correction,
                        "rejected": msg.content,
                        "critique": msg.rlaif_critique or ""
                    }
                    yield json.dumps(dpo_sample) + "\n"
                elif msg.rlaif_rating == 1:
                    dpo_sample = {
                        "prompt": prompt,
                        "chosen": msg.content,
                        "rejected": "",
                        "critique": msg.rlaif_critique or ""
                    }
                    yield json.dumps(dpo_sample) + "\n"
            else:
                if msg.feedback_rating == -1 and msg.feedback_correction:
                    dpo_sample = {
                        "prompt": prompt,
                        "chosen": msg.feedback_correction,
                        "rejected": msg.content,
                        "notes": msg.feedback_notes or ""
                    }
                    yield json.dumps(dpo_sample) + "\n"
                elif msg.feedback_rating == 1:
                    dpo_sample = {
                        "prompt": prompt,
                        "chosen": msg.content,
                        "rejected": "",
                        "notes": msg.feedback_notes or ""
                    }
                    yield json.dumps(dpo_sample) + "\n"

    filename = f"ebs_{type}_dataset.jsonl"
    return StreamingResponse(
        generate_jsonl(),
        media_type="application/x-jsonlines",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
