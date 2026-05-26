"""
RAG Service — ChromaDB-backed vector store using sentence-transformers for embeddings.
Supports PDF and TXT file ingestion, chunking, and semantic search.
"""
import os
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
import tempfile
from duckduckgo_search import DDGS

# Paths
CHROMA_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "chroma_db")
COLLECTION_NAME = "ebs_knowledge"

# Initialize ChromaDB (persistent, local)
_chroma_client = None
_collection = None
_embedding_model = None

def _get_embedding_model() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        print("[RAG] Loading embedding model (all-MiniLM-L6-v2)...")
        _embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
        print("[RAG] Embedding model ready.")
    return _embedding_model

def _get_collection():
    global _chroma_client, _collection
    if _collection is None:
        _chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        _collection = _chroma_client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}
        )
        print(f"[RAG] ChromaDB collection '{COLLECTION_NAME}' ready. Items: {_collection.count()}")
    return _collection

def index_document(file_bytes: bytes, filename: str, doc_id: int) -> int:
    """
    Parse, chunk, embed, and store a document into ChromaDB.
    Returns the number of chunks stored.
    """
    collection = _get_collection()
    model = _get_embedding_model()
    ext = filename.rsplit(".", 1)[-1].lower()

    # Write to temp file for langchain loaders
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        if ext == "pdf":
            loader = PyPDFLoader(tmp_path)
        elif ext in ("txt", "md"):
            loader = TextLoader(tmp_path, encoding="utf-8")
        else:
            raise ValueError(f"Unsupported file type: {ext}")

        docs = loader.load()
        splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
        chunks = splitter.split_documents(docs)

        if not chunks:
            return 0

        texts = [c.page_content for c in chunks]
        embeddings = model.encode(texts).tolist()
        ids = [f"doc{doc_id}_chunk{i}" for i in range(len(chunks))]
        metadatas = [{"doc_id": str(doc_id), "filename": filename, "chunk_index": i} for i in range(len(chunks))]

        # Upsert in batches of 100
        batch = 100
        for start in range(0, len(texts), batch):
            collection.upsert(
                ids=ids[start:start + batch],
                embeddings=embeddings[start:start + batch],
                documents=texts[start:start + batch],
                metadatas=metadatas[start:start + batch],
            )

        return len(chunks)
    finally:
        os.unlink(tmp_path)


def index_text(text: str, doc_id: int, metadata: dict) -> int:
    """
    Chunk, embed, and store plain text directly into ChromaDB.
    Used to index deployment step results without needing a file upload.
    Returns number of chunks stored.
    """
    collection = _get_collection()
    model      = _get_embedding_model()

    splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    chunks   = splitter.split_text(text)

    if not chunks:
        return 0

    embeddings = model.encode(chunks).tolist()
    ids        = [f"dep{doc_id}_chunk{i}" for i in range(len(chunks))]
    metadatas  = [{**metadata, "doc_id": str(doc_id), "chunk_index": i}
                  for i in range(len(chunks))]

    batch = 100
    for start in range(0, len(chunks), batch):
        collection.upsert(
            ids=ids[start:start + batch],
            embeddings=embeddings[start:start + batch],
            documents=chunks[start:start + batch],
            metadatas=metadatas[start:start + batch],
        )

    return len(chunks)


def search_web_fallback(query: str, max_results: int = 3) -> str:
    """
    Query DuckDuckGo for live internet search grounding fallback
    when local RAG context is missing or holds low similarity scores.
    """
    print(f"[WebSearch] Executing live web search fallback for: {query}")
    try:
        with DDGS() as ddgs:
            search_query = f"Oracle EBS {query}"
            results = list(ddgs.text(search_query, max_results=max_results))
            if not results:
                return ""
            
            snippets = ["[WEB SOURCE FALLBACK - Grounded via DuckDuckGo Live Search]"]
            for i, r in enumerate(results, 1):
                snippets.append(
                    f"Source #{i}: {r.get('title')}\n"
                    f"URL: {r.get('href')}\n"
                    f"Extract: {r.get('body')}"
                )
            return "\n\n---\n\n".join(snippets)
    except Exception as e:
        print(f"[WebSearch] Error during search: {e}")
        return ""


def query_rag(query_text: str, n_results: int = 4) -> str:
    """
    Find the top-N most relevant chunks for a query.
    Returns a formatted context string, or empty string if nothing relevant is found.
    """
    collection = _get_collection()
    if collection.count() == 0:
        return ""

    model = _get_embedding_model()
    query_embedding = model.encode([query_text]).tolist()

    results = collection.query(
        query_embeddings=query_embedding,
        n_results=min(n_results, collection.count()),
        include=["documents", "metadatas", "distances"]
    )

    docs = results.get("documents", [[]])[0]
    distances = results.get("distances", [[]])[0]

    relevant = [doc for doc, dist in zip(docs, distances) if dist < 0.75]
    if not relevant:
        return ""

    return "\n\n---\n\n".join(relevant)


def delete_document_chunks(doc_id: int):
    """Remove all chunks for a specific document from ChromaDB."""
    collection = _get_collection()
    collection.delete(where={"doc_id": str(doc_id)})
