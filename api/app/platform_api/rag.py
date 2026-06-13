from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List
from app.core import database
from app import schemas, models
from app.core.auth.auth import get_current_user
from app.core.rag import rag_service

router = APIRouter(
    prefix="/rag",
    tags=["Knowledge Base"]
)

ALLOWED_EXTENSIONS = {"pdf", "txt", "md"}
MAX_FILE_SIZE_MB = 50


def _do_index(file_bytes: bytes, filename: str, doc_id: int, db_session_factory):
    """Background task: indexes the document into ChromaDB and updates status in DB."""
    db = db_session_factory()
    try:
        chunk_count = rag_service.index_document(file_bytes, filename, doc_id)
        doc = db.query(models.RagDocument).filter(models.RagDocument.id == doc_id).first()
        if doc:
            doc.status = "ready"
            doc.chunk_count = chunk_count
            db.commit()
        print(f"[RAG] Indexed doc_id={doc_id} ({filename}) → {chunk_count} chunks.")
    except Exception as e:
        db.rollback()
        doc = db.query(models.RagDocument).filter(models.RagDocument.id == doc_id).first()
        if doc:
            doc.status = "failed"
            doc.error_message = str(e)
            db.commit()
        print(f"[RAG] Indexing failed for doc_id={doc_id}: {e}")
    finally:
        db.close()


@router.post("/upload", response_model=schemas.RagDocumentOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '.{ext}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    file_bytes = await file.read()

    if len(file_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {MAX_FILE_SIZE_MB}MB limit."
        )

    # Save record to DB with status='indexing'
    doc_record = models.RagDocument(
        user_id=current_user.id,
        filename=file.filename,
        file_type=ext,
        status="indexing"
    )
    db.add(doc_record)
    db.commit()
    db.refresh(doc_record)

    # Run indexing in background so the API responds immediately
    background_tasks.add_task(
        _do_index,
        file_bytes,
        file.filename,
        doc_record.id,
        database.SessionLocal
    )

    return doc_record


@router.get("/documents", response_model=List[schemas.RagDocumentOut])
def list_documents(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    docs = db.query(models.RagDocument).order_by(models.RagDocument.created_at.desc()).all()
    return docs


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    doc_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user)
):
    doc = db.query(models.RagDocument).filter(models.RagDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Remove from ChromaDB
    rag_service.delete_document_chunks(doc_id)

    # Remove from DB
    db.delete(doc)
    db.commit()
