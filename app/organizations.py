from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field


class OrganizationInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    tenant_key: str = Field(min_length=1, max_length=80, pattern=r'^[a-z][a-z0-9_-]{1,79}$')


class MemberInput(BaseModel):
    tenant_key: str = Field(min_length=1, max_length=80)
    user_id: str = Field(min_length=1, max_length=200)
    role: str = Field(default='member', min_length=1, max_length=40)


class OrganizationStore:
    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS organizations(
                tenant_key TEXT PRIMARY KEY, organization_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS organization_members(
                tenant_key TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(tenant_key,user_id));''')

    def list(self) -> dict:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            organizations = [dict(row) for row in db.execute('SELECT * FROM organizations ORDER BY name')]
            members = [dict(row) for row in db.execute('SELECT * FROM organization_members ORDER BY tenant_key,user_id')]
        return {'organizations': organizations, 'members': members}

    def create(self, value: OrganizationInput) -> dict:
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO organizations VALUES(?,?,?,?)',
                       (value.tenant_key, 'org_' + uuid.uuid4().hex, value.name.strip(), datetime.now(timezone.utc).isoformat()))
        return next(item for item in self.list()['organizations'] if item['tenant_key'] == value.tenant_key)

    def add_member(self, value: MemberInput) -> dict:
        with sqlite3.connect(self.path) as db:
            if db.execute('SELECT 1 FROM organizations WHERE tenant_key=?', (value.tenant_key,)).fetchone() is None:
                raise KeyError(value.tenant_key)
            db.execute('INSERT OR REPLACE INTO organization_members VALUES(?,?,?,?)',
                       (value.tenant_key, value.user_id, value.role, datetime.now(timezone.utc).isoformat()))
        return {'tenant_key': value.tenant_key, 'user_id': value.user_id, 'role': value.role}

    def is_member(self, tenant_key: str, user_id: str) -> bool:
        if not tenant_key:
            return False
        with sqlite3.connect(self.path) as db:
            return db.execute(
                'SELECT 1 FROM organization_members WHERE tenant_key=? AND user_id=?',
                (tenant_key, user_id),
            ).fetchone() is not None

    def remove_member(self, tenant_key: str, user_id: str) -> None:
        with sqlite3.connect(self.path) as db:
            if not db.execute('DELETE FROM organization_members WHERE tenant_key=? AND user_id=?', (tenant_key, user_id)).rowcount:
                raise KeyError((tenant_key, user_id))
