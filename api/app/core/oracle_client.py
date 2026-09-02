"""Optional python-oracledb thick-mode init.

Every EBS DB connection in this app (see the various `import oracledb;
oracledb.connect(...)` call sites under app/modules/dba/) uses thin mode by
default — pure Python, no Oracle Client libs required. Thin mode cannot
negotiate Oracle's Native Network Encryption / Data Integrity (NNE) protocol;
against an EBS DB with SQLNET.ENCRYPTION_SERVER or SQLNET.CRYPTO_CHECKSUM_SERVER
set to REQUIRED, connects fail with DPY-3001 (wrapped in DPY-6005).

Thick mode fixes that by loading the real Oracle Client (OCI) libraries. It's
off by default so environments that don't need it keep the no-Instant-Client
deployment story; set ORACLE_THICK_MODE=1 + ORACLE_CLIENT_LIB_DIR to turn it on.
"""
import os

_THICK_MODE_ENABLED = os.getenv("ORACLE_THICK_MODE", "0").lower() in ("1", "true", "yes")
_CLIENT_LIB_DIR = os.getenv("ORACLE_CLIENT_LIB_DIR")


def init_thick_mode_if_configured():
    if not _THICK_MODE_ENABLED:
        return

    import oracledb

    if not _CLIENT_LIB_DIR:
        raise RuntimeError(
            "ORACLE_THICK_MODE=1 but ORACLE_CLIENT_LIB_DIR is not set — point it at an "
            "unzipped Oracle Instant Client 'Basic' package (e.g. /opt/oracle/instantclient_21_x)."
        )

    oracledb.init_oracle_client(lib_dir=_CLIENT_LIB_DIR)
    print(f"[oracle_client] thick mode enabled — lib_dir={_CLIENT_LIB_DIR}")
