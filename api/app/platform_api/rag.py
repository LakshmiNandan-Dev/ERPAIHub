import hashlib
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


@router.post("/documents", response_model=schemas.RagDocumentOut, status_code=status.HTTP_201_CREATED)
@router.post("/upload", response_model=schemas.RagDocumentOut,
             status_code=status.HTTP_201_CREATED, include_in_schema=False)
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

    # Duplicate guard — reject re-uploads of byte-identical content (regardless
    # of filename). A prior *failed* indexing attempt is not a duplicate, so the
    # user can retry; only an existing ready/indexing copy blocks the upload.
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    existing = (
        db.query(models.RagDocument)
        .filter(
            models.RagDocument.content_hash == content_hash,
            models.RagDocument.status != "failed",
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This file is already in the knowledge base as "
                f"'{existing.filename}' (id={existing.id}, status={existing.status}). "
                f"Delete it first if you want to re-index."
            ),
        )

    # Save record to DB with status='indexing'
    doc_record = models.RagDocument(
        user_id=current_user.id,
        filename=file.filename,
        file_type=ext,
        content_hash=content_hash,
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


@router.put("/documents/{doc_id}", response_model=schemas.RagDocumentOut)
async def reindex_document(
    doc_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Replace an existing document with an updated file and re-index it
    *incrementally* — only chunks whose text actually changed are re-embedded
    (see rag_service._incremental_upsert). This is the cheap path for documents
    that change often; deleting and re-uploading would re-embed the whole file.
    """
    doc = db.query(models.RagDocument).filter(models.RagDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

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

    # No-op guard — identical bytes to what's already indexed for this doc.
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    if doc.content_hash == content_hash and doc.status == "ready":
        return doc

    # Flip to indexing; the background delta does the embedding work.
    doc.filename = file.filename
    doc.file_type = ext
    doc.content_hash = content_hash
    doc.status = "indexing"
    doc.error_message = None
    db.commit()
    db.refresh(doc)

    background_tasks.add_task(
        _do_index, file_bytes, file.filename, doc.id, database.SessionLocal
    )
    return doc


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
