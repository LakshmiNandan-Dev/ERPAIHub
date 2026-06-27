from .service import QueryResult, QueryService

__all__ = ["QueryService", "QueryResult", "build_app"]


def build_app(service, **kwargs):
    """Lazy so importing the package doesn't require fastapi."""
    from .app import build_app as _build
    return _build(service, **kwargs)
