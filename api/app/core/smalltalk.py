"""
Instant small-talk responder — a tiny transformer-based intent classifier.

Greetings, thanks, acknowledgements and "what can you do" don't need a generative
LLM. We reuse the sentence-transformer already loaded for RAG (all-MiniLM-L6-v2)
to embed the message and match it (cosine similarity) against a few intent
prototypes, then return a templated reply. This answers "Hi" / "heyy" / "thanks a
lot" in milliseconds with no Ollama call at all.

Conservative by design: only short messages are considered and a high similarity
floor is required, so genuine (even short) EBS questions fall through to the LLM.
Everything degrades safely — any failure returns None and the normal path runs.
"""
import os

SMALLTALK_ENABLED = os.getenv("SMALLTALK_ENABLED", "1").lower() not in ("0", "false", "no")
SMALLTALK_THRESHOLD = float(os.getenv("SMALLTALK_THRESHOLD", "0.72"))
SMALLTALK_MAX_WORDS = int(os.getenv("SMALLTALK_MAX_WORDS", "6"))

INTENTS = {
    "greeting": {
        "examples": ["hi", "hello", "hey", "hiya", "hey there", "hello there",
                     "good morning", "good afternoon", "good evening", "greetings"],
        "response": (
            "👋 Hello! I'm your Oracle EBS assistant. I can help with ADOP / online "
            "patching, FNDLOAD data loads, PL/SQL & Forms compilation, SQL diagnostics, "
            "deployments, performance analysis, and HR/Payroll inquiries.\n\n"
            "What would you like to do?"
        ),
    },
    "thanks": {
        "examples": ["thanks", "thank you", "thanks a lot", "thank you so much",
                     "appreciate it", "much appreciated", "cheers"],
        "response": "You're welcome! Anything else on Oracle EBS I can help with?",
    },
    "goodbye": {
        "examples": ["bye", "goodbye", "see you", "see ya", "good night", "that's all"],
        "response": "Goodbye! Reach out anytime you need a hand with Oracle EBS.",
    },
    "affirm": {
        "examples": ["ok", "okay", "got it", "understood", "sounds good", "great",
                     "perfect", "sure", "alright"],
        "response": "👍 Let me know what you'd like to tackle next in Oracle EBS.",
    },
    "capabilities": {
        "examples": ["what can you do", "help", "who are you", "what do you do",
                     "how can you help", "what are your capabilities"],
        "response": (
            "I'm an Oracle EBS assistant. I can:\n"
            "- Answer EBS questions grounded in your uploaded knowledge base (RAG)\n"
            "- Turn instructions into deployment steps (SQL / FNDLOAD / Forms / Workflow)\n"
            "- Run database performance diagnostics + AWR analysis\n"
            "- Guide ADOP patching and environment cloning\n"
            "- Answer HCM / Payroll functional questions (read-only)\n\n"
            "Ask me anything EBS-related, or pick a specialized agent from the dropdown."
        ),
    },
}

_protos = None  # intent -> unit-normalised mean embedding (numpy array); False if unavailable


def _ensure_protos():
    """Compute (once) the prototype embedding per intent from its examples."""
    global _protos
    if _protos is not None:
        return _protos
    try:
        import numpy as np
        from app.core.rag import rag_service
        protos = {}
        for intent, spec in INTENTS.items():
            arr = np.asarray(rag_service.embed_texts(spec["examples"]), dtype=float)
            mean = arr.mean(axis=0)
            protos[intent] = mean / (np.linalg.norm(mean) or 1.0)
        _protos = protos
    except Exception as exc:
        print(f"[Smalltalk] embedding prototypes unavailable, exact-match only: {exc}")
        _protos = False
    return _protos


def match(message: str):
    """Return a templated reply for greeting/small-talk, or None to fall through
    to the LLM. Only short messages are considered, so real questions pass through."""
    if not SMALLTALK_ENABLED:
        return None
    text = (message or "").strip()
    if not text or len(text.split()) > SMALLTALK_MAX_WORDS:
        return None

    protos = _ensure_protos()
    if not protos:
        # Degraded path: exact (normalised) lookup against the example phrases.
        low = text.lower().rstrip("?.!,")
        for spec in INTENTS.values():
            if low in spec["examples"]:
                return spec["response"]
        return None

    try:
        import numpy as np
        from app.core.rag import rag_service
        q = np.asarray(rag_service.embed_query(text), dtype=float)
        q = q / (np.linalg.norm(q) or 1.0)
        best_intent, best_sim = None, -1.0
        for intent, proto in protos.items():
            sim = float(np.dot(q, proto))
            if sim > best_sim:
                best_intent, best_sim = intent, sim
        if best_sim >= SMALLTALK_THRESHOLD:
            return INTENTS[best_intent]["response"]
    except Exception as exc:
        print(f"[Smalltalk] match failed: {exc}")
    return None
