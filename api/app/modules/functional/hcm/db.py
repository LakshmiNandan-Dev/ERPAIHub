"""
Read-only Oracle connection helper for the HCM functional agent.

Connection details come from a stored ``EbsEnvironment`` (by name, password
decrypted) or inline request credentials. Queries run in a read-only transaction
as defense-in-depth on top of the SELECT-only, bind-parameterized catalog.
"""
import re
from datetime import date

from app.core import crypto
from app import models

# Belt-and-braces guard. The catalog is fixed SELECT-only SQL, but we still
# refuse anything that isn't a single SELECT before sending it to the database.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|truncate|grant|revoke|create|"
    r"begin|declare|call)\b",
    re.IGNORECASE,
)


def assert_select_only(sql: str) -> None:
    """Raise ValueError unless ``sql`` is a single read-only SELECT statement."""
    s = (sql or "").strip().rstrip(";").strip()
    if not s.lower().startswith(("select", "with")):
        raise ValueError("Only SELECT statements are permitted.")
    if ";" in s:
        raise ValueError("Only a single statement is permitted.")
    if _FORBIDDEN.search(s):
        raise ValueError("Statement contains a non-read-only keyword.")


def resolve_conn(db, environment: str | None, payload) -> dict | None:
    """
    Resolve connection parameters from inline payload creds first, else a stored
    EbsEnvironment by name. Returns dict(user, password, host, port, sid) or None
    when no usable connection can be formed (caller then uses the simulator).
    """
    host = getattr(payload, "db_host", None)
    user = getattr(payload, "db_user", None)
    sid = getattr(payload, "db_sid", None)
    port = getattr(payload, "db_port", None) or 1521
    password = getattr(payload, "db_password", None)

    if not (host and user and sid):
        if not environment:
            return None
        env = (
            db.query(models.EbsEnvironment)
            .filter(
                models.EbsEnvironment.name == environment,
                models.EbsEnvironment.is_active == True,  # noqa: E712
            )
            .first()
        )
        if not env or not (env.db_host and env.db_user and env.db_sid):
            return None
        host, user, sid = env.db_host, env.db_user, env.db_sid
        port = env.db_port or 1521
        password = crypto.decrypt(env.db_password_enc) or ""

    return {"user": user, "password": password or "", "host": host, "port": port, "sid": sid}


def run_inquiry_live(conn_params: dict, sql: str, binds: dict, max_rows: int = 200) -> list:
    """
    Execute a read-only SELECT and return rows as a list of dicts. Uses oracledb
    thin mode (no Instant Client). ``as_of`` defaults to today when not supplied.
    """
    assert_select_only(sql)
    import oracledb

    binds = dict(binds or {})
    binds.setdefault("as_of", date.today())

    dsn = f"{conn_params['host']}:{conn_params['port']}/{conn_params['sid']}"
    conn = oracledb.connect(user=conn_params["user"], password=conn_params["password"], dsn=dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SET TRANSACTION READ ONLY")  # defense-in-depth
        except Exception:
            pass  # not fatal — the statement itself is already SELECT-only
        cur.execute(sql, binds)
        cols = [d[0].lower() for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchmany(max_rows)]
        # Stringify non-JSON-native values (dates, LOBs) for SSE serialization.
        for row in rows:
            for k, v in list(row.items()):
                if v is not None and not isinstance(v, (str, int, float, bool)):
                    row[k] = str(v)
        return rows
    finally:
        conn.close()
