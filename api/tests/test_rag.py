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

    def test_upload_duplicate_rejected(self, client, admin_headers):
        """TC-RG-09: Re-uploading byte-identical content returns 409 Conflict."""
        dup = io.BytesIO(_txt_bytes())
        with patch("app.core.rag.rag_service.index_document", return_value=2):
            r1 = client.post(
                "/rag/documents",
                files={"file": ("first.txt", dup, "text/plain")},
                headers=admin_headers,
            )
            assert r1.status_code == 201
            # Same bytes, different filename → still a duplicate.
            r2 = client.post(
                "/rag/documents",
                files={"file": ("renamed_copy.txt", io.BytesIO(_txt_bytes()), "text/plain")},
                headers=admin_headers,
            )
        assert r2.status_code == 409
        assert "already in the knowledge base" in r2.json()["detail"]

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

    def test_reindex_document(self, client, admin_headers):
        """TC-RG-10: PUT replaces an existing document and re-indexes it."""
        with patch("app.core.rag.rag_service.index_document", return_value=2):
            r = client.post(
                "/rag/documents",
                files={"file": ("v1.txt", io.BytesIO(b"alpha beta"), "text/plain")},
                headers=admin_headers,
            )
        doc_id = r.json()["id"]
        with patch("app.core.rag.rag_service.index_document", return_value=3):
            r2 = client.put(
                f"/rag/documents/{doc_id}",
                files={"file": ("v2.txt", io.BytesIO(b"alpha beta gamma delta"), "text/plain")},
                headers=admin_headers,
            )
        assert r2.status_code == 200
        assert r2.json()["status"] in ("ready", "indexing")

    def test_reindex_missing_returns_404(self, client, admin_headers):
        """TC-RG-11: Re-indexing a non-existent document returns 404."""
        with patch("app.core.rag.rag_service.index_document", return_value=1):
            r = client.put(
                "/rag/documents/999999",
                files={"file": ("x.txt", io.BytesIO(b"x"), "text/plain")},
                headers=admin_headers,
            )
        assert r.status_code == 404

    def test_upload_requires_auth(self, client):
        """TC-SC-01: Unauthenticated upload returns 401."""
        r = client.post(
            "/rag/documents",
            files={"file": ("test.txt", io.BytesIO(b"test"), "text/plain")},
        )
        assert r.status_code == 401


class TestIncrementalEmbedding:
    """TC-RG-12: content-addressed delta indexing only re-embeds changed chunks."""

    @staticmethod
    def _fakes():
        import numpy as np

        class FakeCollection:
            def __init__(self):
                self.store = {}  # id -> (doc, meta)

            def get(self, where=None, include=None):
                did = where["doc_id"]
                return {"ids": [i for i, (_d, m) in self.store.items()
                                if m["doc_id"] == did]}

            def delete(self, ids=None, where=None):
                for i in (ids or []):
                    self.store.pop(i, None)

            def upsert(self, ids, embeddings, documents, metadatas):
                for i, d, m in zip(ids, documents, metadatas):
                    self.store[i] = (d, m)

        class FakeModel:
            def __init__(self):
                self.encoded = []

            def encode(self, texts):
                self.encoded.extend(texts)
                return np.zeros((len(texts), 3))

        return FakeCollection(), FakeModel()

    def test_only_changed_chunks_reembedded(self):
        from app.core.rag import rag_service

        col, model = self._fakes()
        s1 = rag_service._incremental_upsert(
            col, model, 1, ["aaa", "bbb", "ccc"], {"filename": "f"}, "doc")
        assert s1 == {"total": 3, "added": 3, "deleted": 0, "unchanged": 0}
        assert len(model.encoded) == 3  # cold start embeds everything

        # Re-index: one chunk edited, two unchanged.
        model.encoded.clear()
        s2 = rag_service._incremental_upsert(
            col, model, 1, ["aaa", "bbb", "CHANGED"], {"filename": "f"}, "doc")
        assert s2 == {"total": 3, "added": 1, "deleted": 1, "unchanged": 2}
        assert model.encoded == ["CHANGED"]  # ONLY the changed chunk re-embedded

    def test_noop_reindex_embeds_nothing(self):
        from app.core.rag import rag_service

        col, model = self._fakes()
        rag_service._incremental_upsert(col, model, 7, ["x", "y"], {"filename": "f"}, "doc")
        model.encoded.clear()
        stats = rag_service._incremental_upsert(col, model, 7, ["x", "y"], {"filename": "f"}, "doc")
        assert stats == {"total": 2, "added": 0, "deleted": 0, "unchanged": 2}
        assert model.encoded == []  # identical content → zero embedding work


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
