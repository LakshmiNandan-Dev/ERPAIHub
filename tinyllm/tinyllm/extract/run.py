"""One call to extract a Schema -- from a real Oracle instance or the mock.

    extract_schema(dsn="user/pwd@host:1521/EBS")   # live, read-only
    extract_schema(mock=True)                       # offline demo / tests

`oracledb` is an optional dependency, imported only when a dsn is given, so the
package installs and tests without it.
"""

from __future__ import annotations

from ..schema_graph.types import Schema
from .catalog import MockCatalog, OracleCatalog
from .extractor import EbsExtractor


def extract_schema(dsn: str | None = None, mock: bool = False,
                   extra_owners=()) -> Schema:
    """Extract a Schema. With a dsn this pulls ALL tables in every licensed/shared
    EBS schema (plus any `extra_owners` you name, e.g. custom XX* schemas)."""
    if mock:
        source = MockCatalog()
    elif dsn:
        try:
            import oracledb
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("live extraction needs python-oracledb: "
                               "pip install 'tinyllm[oracle]'") from e
        # The DSN should point at a READ-ONLY account with dictionary access.
        conn = oracledb.connect(dsn)
        source = OracleCatalog(conn.cursor(), extra_owners=extra_owners)
    else:
        raise ValueError("extract_schema needs mock=True or dsn=...")
    return EbsExtractor(source).extract()
