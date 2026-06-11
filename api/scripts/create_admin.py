"""
Promote an existing user to administrator.

Usage (from the api/ directory):
    python -m scripts.create_admin <username>
    python scripts/create_admin.py <username>
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import database, models  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.create_admin <username>")
        return 2

    username = sys.argv[1]
    db = database.SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == username).first()
        if not user:
            print(f"User '{username}' not found.")
            return 1
        user.is_admin = True
        db.commit()
        print(f"User '{username}' (id={user.id}) is now an administrator.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
