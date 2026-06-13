from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import text
from sqlalchemy.orm import relationship
from app.core.database import Base


# ── Admin-managed resources (secrets stored as Fernet ciphertext) ──────────────

class SshServer(Base):
    __tablename__ = "ssh_servers"

    id = Column(Integer, primary_key=True, nullable=False)
    name = Column(String(100), unique=True, nullable=False)
    hostname = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False, server_default='22')
    username = Column(String(100), nullable=True)
    password_enc = Column(Text, nullable=True)  # encrypted
    server_type = Column(String(50), nullable=False, server_default='application')  # application | database
    app_services = Column(JSONB, nullable=True)  # e.g. ["web", "forms", "concurrent"]
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))


class EbsEnvironment(Base):
    __tablename__ = "ebs_environments"

    id = Column(Integer, primary_key=True, nullable=False)
    name = Column(String(50), unique=True, nullable=False)  # DEV | UAT | UAT2 | PROD | ...
    tier = Column(String(20), nullable=False, server_default='nonprod')  # 'prod' | 'nonprod'
    db_host = Column(String(255), nullable=True)
    db_port = Column(Integer, nullable=False, server_default='1521')
    db_sid = Column(String(100), nullable=True)
    db_user = Column(String(100), nullable=True, server_default='apps')
    db_password_enc = Column(Text, nullable=True)  # encrypted
    # Intrinsic database identity (captured by probing) — used to detect a mis-labelled PROD.
    db_id = Column(String(40), nullable=True)        # v$database.dbid
    global_name = Column(String(255), nullable=True) # global_name
    # Extra EBS credentials used by the patching agent's adop tools (encrypted at rest).
    # APPS is db_user/db_password_enc above. SYSTEM (or EBS_SYSTEM on 19c+) and the
    # WebLogic AdminServer account are prompted for by adop phases.
    # 'system_user' is a reserved word in PostgreSQL 16+, so force quoting of the
    # identifier; otherwise CREATE TABLE fails with a syntax error.
    system_user = Column("system_user", String(100), nullable=True, server_default='system', quote=True)   # system | ebs_system
    system_password_enc = Column(Text, nullable=True)
    weblogic_user = Column(String(100), nullable=True, server_default='weblogic')
    weblogic_password_enc = Column(Text, nullable=True)
    apps_os_user = Column(String(100), nullable=True)   # OS owner of the apps tier (e.g. applmgr)
    description = Column(Text, nullable=True)
    ssh_server_id = Column(Integer, ForeignKey("ssh_servers.id", ondelete="SET NULL"), nullable=True)
    is_active = Column(Boolean, server_default='TRUE', nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text('now()'))

    ssh_server = relationship("SshServer")
