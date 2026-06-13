import bcrypt
import secrets
from datetime import datetime, timedelta, timezone

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    pwd_bytes = password.encode('utf-8')
    return bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    pwd_bytes = plain_password.encode('utf-8')
    hashed_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(pwd_bytes, hashed_bytes)

def create_session_token() -> str:
    return secrets.token_urlsafe(32)

def get_session_expiration(hours: int = 24) -> datetime:
    # Use timezone aware datetime since models use TIMESTAMP(timezone=True)
    return datetime.now(timezone.utc) + timedelta(hours=hours)
