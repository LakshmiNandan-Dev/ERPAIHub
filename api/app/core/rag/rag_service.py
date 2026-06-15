"""
RAG Service — ChromaDB-backed vector store using sentence-transformers for embeddings.
Supports PDF and TXT file ingestion, chunking, and semantic search.
"""
import os
import json
import time
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


def _env_bool(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).lower() not in ("0", "false", "no")


# ── Advanced retrieval: cross-encoder reranking + web fallback ──────────────────
# Two-stage retrieval: a fast embedding search pulls a wide candidate set, then a
# cross-encoder reranks it for precision. Falls back to plain distance ordering
# if the reranker can't load (e.g. air-gapped first run with no cached model).
RAG_RERANK_ENABLED = _env_bool("RAG_RERANK_ENABLED", "1")
RAG_RERANK_MODEL = os.getenv("RAG_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
RAG_RERANK_CANDIDATES = int(os.getenv("RAG_RERANK_CANDIDATES", "20"))  # first-stage fetch
# Cross-encoder relevance floor (ms-marco logits). Chunks scoring below this are
# dropped; if none clear the bar the query is treated as a local miss (no context
# injected) so the model abstains rather than grounding on off-topic chunks.
# Observed separation on this corpus: genuinely-relevant chunks score ~5-6, while
# off-topic "nearest" chunks score ~1-2 — so a floor of ~2 cleanly drops the
# noise that was otherwise injected as authoritative KB and inviting fabrication.
RAG_RERANK_MIN_SCORE = float(os.getenv("RAG_RERANK_MIN_SCORE", "2.0"))
# When the local KB has no confident match, ground the answer with a live
# DuckDuckGo search instead of returning nothing.
RAG_WEB_FALLBACK_ENABLED = _env_bool("RAG_WEB_FALLBACK_ENABLED", "1")
# Embedding-distance ceiling used only when reranking is OFF (legacy path).
RAG_DISTANCE_CEILING = float(os.getenv("RAG_DISTANCE_CEILING", "0.75"))

_reranker = None


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

def _get_reranker():
    """Lazily load the cross-encoder reranker. Returns None if it can't load."""
    global _reranker
    if _reranker is None:
        try:
            from sentence_transformers import CrossEncoder
            print(f"[RAG] Loading reranker ({RAG_RERANK_MODEL})...")
            _reranker = CrossEncoder(RAG_RERANK_MODEL)
            print("[RAG] Reranker ready.")
        except Exception as exc:
            print(f"[RAG] Reranker unavailable, using distance ordering: {exc}")
            _reranker = False  # sentinel: tried and failed, don't retry every call
    return _reranker or None


def _rerank(query: str, docs: list) -> list:
    """
    Score (query, doc) pairs with the cross-encoder and return them ordered
    most-relevant first as (doc, score) tuples. Returns [] if reranking is
    unavailable so the caller can fall back to the embedding-distance path.
    """
    reranker = _get_reranker()
    if reranker is None or not docs:
        return []
    scores = reranker.predict([(query, d) for d in docs])
    return sorted(zip(docs, (float(s) for s in scores)), key=lambda x: x[1], reverse=True)


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


def query_rag(query_text: str, n_results: int = 4, allow_web_fallback: bool = True) -> str:
    """
    Advanced retrieval: a wide embedding search (recall) is reranked by a
    cross-encoder (precision), and the top-N relevant chunks are returned as a
    formatted context string. When the local KB has no confident match and
    ``allow_web_fallback`` is set, a live DuckDuckGo search grounds the answer
    instead of returning nothing.

    Falls back to legacy embedding-distance filtering when the reranker can't
    load, so behaviour degrades gracefully in air-gapped environments.
    """
    from app.core import telemetry   # lazy import avoids any import cycle
    _t0 = time.monotonic()
    collection = _get_collection()
    have_corpus = collection.count() > 0

    # Retrieval cache — keyed on corpus version + retrieval params + query text,
    # so an index/delete (which bumps the version) makes prior results stale.
    # The 'r2' namespace distinguishes reranked results from the legacy cache.
    cache_key = None
    if RAG_CACHE_ENABLED and query_text:
        digest = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
        rr = "1" if RAG_RERANK_ENABLED else "0"
        cache_key = f"ragcache:q2:{_rag_version()}:{n_results}:{rr}:{digest}"
        try:
            hit = _r().get(cache_key)
            if hit is not None:
                telemetry.record_rag(hit=bool(hit), cached=True,
                                     latency_ms=(time.monotonic() - _t0) * 1000)
                return hit
        except Exception:
            pass

    result_str = ""
    from_web = False
    top_score = None
    relevant = []

    if have_corpus:
        # Stage 1 — recall: pull a wide candidate set when reranking, else just N.
        fetch_k = max(n_results, RAG_RERANK_CANDIDATES) if RAG_RERANK_ENABLED else n_results
        results = collection.query(
            query_embeddings=[embed_query(query_text)],
            n_results=min(fetch_k, collection.count()),
            include=["documents", "distances"],
        )
        docs = results.get("documents", [[]])[0]
        distances = results.get("distances", [[]])[0]

        # Stage 2 — precision: cross-encoder rerank, keep those above the floor.
        if RAG_RERANK_ENABLED:
            ranked = _rerank(query_text, docs)
            if ranked:
                top_score = ranked[0][1]   # best candidate's cross-encoder score
                relevant = [doc for doc, score in ranked[:n_results]
                            if score >= RAG_RERANK_MIN_SCORE]
        if not relevant and not (RAG_RERANK_ENABLED and _get_reranker() is not None):
            # Reranker off or unavailable → legacy embedding-distance filter.
            relevant = [doc for doc, dist in zip(docs, distances)
                        if dist < RAG_DISTANCE_CEILING][:n_results]

        result_str = "\n\n---\n\n".join(relevant) if relevant else ""

    # Web fallback — no confident local match → ground with live search.
    if not result_str and allow_web_fallback and RAG_WEB_FALLBACK_ENABLED:
        web = search_web_fallback(query_text)
        if web:
            result_str = web
            from_web = True

    # Cache local results (deterministic); skip web results so they stay fresh.
    if cache_key is not None and not from_web:
        try:
            _r().set(cache_key, result_str, ex=RAG_CACHE_TTL)
        except Exception:
            pass

    telemetry.record_rag(
        hit=bool(result_str), cached=False, from_web=from_web,
        num_chunks=len(relevant),
        top_score=(top_score if (result_str and not from_web) else None),
        latency_ms=(time.monotonic() - _t0) * 1000,
    )
    return result_str


def delete_document_chunks(doc_id: int):
    """Remove all chunks for a specific document from ChromaDB."""
    collection = _get_collection()
    collection.delete(where={"doc_id": str(doc_id)})
    _bump_rag_version()  # corpus changed — invalidate cached retrievals
