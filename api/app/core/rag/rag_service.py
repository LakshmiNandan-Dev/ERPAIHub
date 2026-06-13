"""
RAG Service — ChromaDB-backed vector store using sentence-transformers for embeddings.
Supports PDF and TXT file ingestion, chunking, and semantic search.
"""
import os
import json
import hashlib
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
import tempfile
from duckduckgo_search import DDGS

# Paths — the Chroma store lives at api/chroma_db (one level above the `app`
# package), independent of where this module sits in the package tree.
# This file is at app/core/rag/, so three dirname() hops reach api/app.
_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHROMA_DB_PATH = os.path.join(_APP_DIR, "..", "chroma_db")
COLLECTION_NAME = "ebs_knowledge"

# Initialize ChromaDB (persistent, local)
_chroma_client = None
_collection = None
_embedding_model = None

# ── Redis-backed caches (query embeddings + RAG retrieval results) ──────────────
# Embeddings are deterministic for a given text, so they cache with a long TTL.
# Retrieval results depend on the indexed corpus, so their key is stamped with a
# version counter that is bumped on every index/delete — making stale results
# unreachable immediately (TTL is just a backstop). All ops degrade silently.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RAG_CACHE_ENABLED = os.getenv("RAG_CACHE_ENABLED", "1").lower() not in ("0", "false", "no")
RAG_CACHE_TTL = int(os.getenv("RAG_CACHE_TTL", "3600"))        # 1h
EMBED_CACHE_TTL = int(os.getenv("EMBED_CACHE_TTL", "86400"))   # 24h
_K_RAG_VER = "ragcache:ver"
_redis = None


def _r():
    global _redis
    if _redis is None:
        import redis
        _redis = redis.Redis.from_url(
            REDIS_URL, decode_responses=True,
            socket_connect_timeout=1, socket_timeout=1,
        )
    return _redis


def _rag_version() -> int:
    try:
        return int(_r().get(_K_RAG_VER) or 0)
    except Exception:
        return 0


def _bump_rag_version() -> None:
    """Invalidate all cached retrieval results after the corpus changes."""
    if not RAG_CACHE_ENABLED:
        return
    try:
        _r().incr(_K_RAG_VER)
    except Exception:
        pass


def embed_query(text: str) -> list:
    """
    Embed a single query string, cached in Redis. The model output is fully
    deterministic for a given text, so this is always safe. Returns one vector.
    """
    raw = text or ""
    if not RAG_CACHE_ENABLED or not raw.strip():
        return _get_embedding_model().encode([raw]).tolist()[0]
    key = "ragcache:emb:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    try:
        hit = _r().get(key)
        if hit is not None:
            return json.loads(hit)
    except Exception:
        pass
    vec = _get_embedding_model().encode([raw]).tolist()[0]
    try:
        _r().set(key, json.dumps(vec), ex=EMBED_CACHE_TTL)
    except Exception:
        pass
    return vec

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

def embed_texts(texts: list) -> list:
    """Embed strings with the shared sentence-transformer model (reused by caches)."""
    return _get_embedding_model().encode(texts).tolist()


def get_named_collection(name: str):
    """
    Return (creating if needed) an auxiliary cosine collection on the shared
    persistent client — used by the semantic response cache so it doesn't open a
    second ChromaDB client on the same path.
    """
    global _chroma_client
    if _chroma_client is None:
        _get_collection()  # initialises the shared client + main collection
    return _chroma_client.get_or_create_collection(
        name=name, metadata={"hnsw:space": "cosine"}
    )


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

        _bump_rag_version()  # corpus changed — invalidate cached retrievals
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

    _bump_rag_version()  # corpus changed — invalidate cached retrievals
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

    # Retrieval cache — keyed on corpus version + n_results + query text, so an
    # index/delete (which bumps the version) makes prior results unreachable.
    cache_key = None
    if RAG_CACHE_ENABLED and query_text:
        digest = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
        cache_key = f"ragcache:q:{_rag_version()}:{n_results}:{digest}"
        try:
            hit = _r().get(cache_key)
            if hit is not None:
                return hit
        except Exception:
            pass

    query_embedding = [embed_query(query_text)]

    results = collection.query(
        query_embeddings=query_embedding,
        n_results=min(n_results, collection.count()),
        include=["documents", "metadatas", "distances"]
    )

    docs = results.get("documents", [[]])[0]
    distances = results.get("distances", [[]])[0]

    relevant = [doc for doc, dist in zip(docs, distances) if dist < 0.75]
    result_str = "\n\n---\n\n".join(relevant) if relevant else ""

    if cache_key is not None:
        try:
            _r().set(cache_key, result_str, ex=RAG_CACHE_TTL)
        except Exception:
            pass

    return result_str


def delete_document_chunks(doc_id: int):
    """Remove all chunks for a specific document from ChromaDB."""
    collection = _get_collection()
    collection.delete(where={"doc_id": str(doc_id)})
    _bump_rag_version()  # corpus changed — invalidate cached retrievals
