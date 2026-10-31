from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field


PERMISSIONS = ('knowledge.read', 'document.read', 'document.write', 'tool.invoke', 'tool.advanced', 'mcp.use', 'code.execute', 'sandbox.use', 'admin')


class RoleInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    permissions: list[str] = Field(default_factory=list, max_length=30)


class UserRoleInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    role_name: str = Field(min_length=1, max_length=80)


class AccessStore:
    def __init__(self, path: Path, admin_user_id: str):
        self.path = path
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS roles(name TEXT PRIMARY KEY, permissions TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS user_roles(user_id TEXT PRIMARY KEY, role_name TEXT NOT NULL, updated_at TEXT NOT NULL);''')
            now = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT OR IGNORE INTO roles VALUES(?,?,?)', ('admin', ','.join(PERMISSIONS), now))
            db.execute('INSERT OR IGNORE INTO roles VALUES(?,?,?)', ('viewer', 'knowledge.read,document.read,tool.invoke', now))
            db.execute('INSERT OR IGNORE INTO user_roles VALUES(?,?,?)', (admin_user_id, 'admin', now))

    def roles(self) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [{'name': row['name'], 'permissions': row['permissions'].split(',') if row['permissions'] else []} for row in db.execute('SELECT * FROM roles ORDER BY name')]

    def save_role(self, value: RoleInput) -> dict:
        unknown = set(value.permissions) - set(PERMISSIONS)
        if unknown:
            raise ValueError('未知权限：' + ','.join(sorted(unknown)))
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT OR REPLACE INTO roles VALUES(?,?,?)', (value.name.strip(), ','.join(sorted(set(value.permissions))), datetime.now(timezone.utc).isoformat()))
        return {'name': value.name.strip(), 'permissions': sorted(set(value.permissions))}

    def assign(self, value: UserRoleInput) -> dict:
        with sqlite3.connect(self.path) as db:
            if db.execute('SELECT 1 FROM roles WHERE name=?', (value.role_name,)).fetchone() is None:
                raise KeyError(value.role_name)
            db.execute('INSERT OR REPLACE INTO user_roles VALUES(?,?,?)', (value.user_id, value.role_name, datetime.now(timezone.utc).isoformat()))
        return {'user_id': value.user_id, 'role_name': value.role_name}

    def users(self) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT user_id,role_name,updated_at FROM user_roles ORDER BY user_id')]

    def permissions(self, user_id: str, is_admin: bool = False) -> set[str]:
        if is_admin:
            return set(PERMISSIONS)
        with sqlite3.connect(self.path) as db:
            row = db.execute('''SELECT r.permissions FROM user_roles ur JOIN roles r ON r.name=ur.role_name WHERE ur.user_id=?''', (user_id,)).fetchone()
        return set(row[0].split(',')) if row and row[0] else ({'knowledge.read', 'document.read', 'tool.invoke', 'sandbox.use'} if user_id.startswith('ldap:') else set())
