from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class LdapConfigInput(BaseModel):
    enabled: bool = False
    server_url: str = Field(default="", max_length=500)
    user_dn_template: str = Field(default="", max_length=500)
    base_dn: str = Field(default="", max_length=500)
    user_filter: str = Field(default="(uid={username})", max_length=500)
    group_base_dn: str = Field(default="", max_length=500)
    group_filter: str = Field(default="(member={user_dn})", max_length=500)
    group_attribute: str = Field(default="cn", max_length=100)
    group_role_map: dict[str, str] = Field(default_factory=dict)
    bind_dn: str = Field(default="", max_length=500)
    bind_password: str | None = Field(default=None, max_length=500)
    timeout_seconds: float = Field(default=5.0, ge=1, le=60)


class LdapTestInput(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=500)


class LdapConfigStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, value: LdapConfigInput, current_password: str = "") -> dict[str, Any]:
        password = value.bind_password if value.bind_password is not None else current_password
        data = value.model_dump(exclude={"bind_password"})
        data["bind_password"] = password
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.path.chmod(0o600)
        return data

    @staticmethod
    def public(value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value)
        result["bind_password_configured"] = bool(result.pop("bind_password", ""))
        return result
