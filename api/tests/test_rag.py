"""TC-RG-01 to TC-RG-08 — RAG Knowledge Base: Document Upload & Query"""
import io
import pytest
from unittest.mock import patch, MagicMock


def _pdf_bytes():
    """Minimal PDF-like bytes for upload testing."""
    return b"%PDF-1.4 1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Count 0>>endobj xref trailer<</Root 1 0 R>>startxref 0 %%EOF"


def _txt_bytes():
    return b"Oracle EBS ADOP patching: run adop phase=apply to apply patches online."


class TestDocumentUpload:

    def test_upload_pdf_success(self, client, admin_headers):
        """TC-RG-01: PDF document uploads and gets indexed."""
        with patch("app.core.rag.rag_service.index_document", return_value=4):
            r = client.post(
                "/rag/documents",
                files={"file": ("guide.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")},
                headers=admin_headers,
            )
        assert r.status_code == 201
        data = r.json()
        assert data["filename"] == "guide.pdf"
        assert data["file_type"] == "pdf"
        assert data["status"] in ("ready", "indexing")

    def test_upload_txt_success(self, client, admin_headers):
        """TC-RG-02: TXT document uploads and gets indexed."""
        with patch("app.core.rag.rag_service.index_document", return_value=2):
            r = client.post(
                "/rag/documents",
                files={"file": ("notes.txt", io.BytesIO(_txt_bytes()), "text/plain")},
                headers=admin_headers,
            )
        assert r.status_code == 201
        assert r.json()["file_type"] == "txt"

    def test_upload_unsupported_type(self, client, admin_headers):
        """TC-RG-03: Uploading .docx returns an error."""
        r = client.post(
            "/rag/documents",
            files={"file": ("doc.docx", io.BytesIO(b"PK fake docx"), "application/vnd.openxmlformats")},
            headers=admin_headers,
        )
        assert r.status_code in (400, 422)

    def test_list_documents(self, client, admin_headers):
        """TC-RG-01 (list): Uploaded documents appear in the list."""
        with patch("app.core.rag.rag_service.index_document", return_value=3):
            client.post(
                "/rag/documents",
                files={"file": ("ebs_guide.txt", io.BytesIO(_txt_bytes()), "text/plain")},
                headers=admin_headers,
            )
        r = client.get("/rag/documents", headers=admin_headers)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_delete_document(self, client, admin_headers):
        """TC-RG-06: Deleting a document removes it from the DB and ChromaDB."""
        with patch("app.core.rag.rag_service.index_document", return_value=2):
            r = client.post(
                "/rag/documents",
                files={"file": ("to_delete.txt", io.BytesIO(_txt_bytes()), "text/plain")},
                headers=admin_headers,
            )
        doc_id = r.json()["id"]
        with patch("app.core.rag.rag_service.delete_document_chunks"):
            r2 = client.delete(f"/rag/documents/{doc_id}", headers=admin_headers)
        assert r2.status_code == 204

    def test_upload_requires_auth(self, client):
        """TC-SC-01: Unauthenticated upload returns 401."""
        r = client.post(
            "/rag/documents",
            files={"file": ("test.txt", io.BytesIO(b"test"), "text/plain")},
        )
        assert r.status_code == 401


class TestRAGQuery:

    def test_rag_context_injected_on_relevant_query(self, client, admin_headers, mock_llm):
        """TC-RG-04: When RAG returns results they appear in the stream response."""
        from app.core.rag import rag_service
        with patch.object(rag_service, "query_rag",
                          return_value="ADOP is the online patching tool for Oracle EBS 12.2."):
            from conftest import chat_session
            session_id = chat_session(client, admin_headers)
            r = client.post(
                f"/chat/sessions/{session_id}/stream",
                json={"content": "Explain ADOP patching cycle in Oracle EBS"},
                headers={**admin_headers, "X-LLM-Provider": "ollama"},
            )
        assert r.status_code == 200
        assert "rag knowledge base agent invoked" in r.text.lower()

    def test_no_rag_below_threshold(self, client, admin_headers, mock_llm):
        """TC-RG-05: When RAG distances are all >= 0.75 nothing is injected."""
        from app.core.rag import rag_service
        with patch.object(rag_service, "query_rag", return_value=""):
            from conftest import chat_session
            session_id = chat_session(client, admin_headers)
            r = client.post(
                f"/chat/sessions/{session_id}/stream",
                json={"content": "What is FNDLOAD used for in Oracle EBS?"},
                headers={**admin_headers, "X-LLM-Provider": "ollama"},
            )
        assert r.status_code == 200
        assert "rag knowledge base agent invoked" not in r.text.lower()
