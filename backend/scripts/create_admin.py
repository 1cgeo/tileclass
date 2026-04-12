"""CLI: create initial admin user."""
import getpass
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from backend.database import init_db, connect
from backend.auth import hash_password


def main():
    init_db()
    username = input("Admin username: ").strip()
    if not username:
        print("aborted")
        return
    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm:  ")
    if pw1 != pw2 or len(pw1) < 6:
        print("passwords mismatch or too short")
        return
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            print(f"user {username} already exists")
            return
        conn.execute(
            "INSERT INTO users(username, password_hash, role, active, created_at) VALUES (?,?,?,1,?)",
            (username, hash_password(pw1), "admin", datetime.now(timezone.utc).isoformat()),
        )
    finally:
        conn.close()
    print(f"admin '{username}' created")


if __name__ == "__main__":
    main()
