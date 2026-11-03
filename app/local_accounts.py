"""Local non-admin accounts. Provisioned by the host operator, never self-registered."""
from pathlib import Path
import re
import sqlite3

from app.auth import hash_password, verify_password


class LocalAccountStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS local_accounts (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL)')
        path.chmod(0o600)

    def create(self, username: str, password: str, *, reserved_username: str):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{2,63}', username):
            raise ValueError('用户名需为 3–64 位字母、数字、点、横线或下划线')
        if username.casefold() == reserved_username.casefold():
            raise ValueError('不能使用管理员用户名')
        if not 12 <= len(password) <= 500:
            raise ValueError('密码长度需为 12–500 位')
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO local_accounts VALUES (?, ?)', (username, hash_password(password)))

    def exists(self, username: str) -> bool:
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT 1 FROM local_accounts WHERE username=?', (username,)).fetchone() is not None

    def authenticate(self, username: str, password: str) -> bool:
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT password_hash FROM local_accounts WHERE username=?', (username,)).fetchone()
        return bool(row and verify_password(password, row[0]))
