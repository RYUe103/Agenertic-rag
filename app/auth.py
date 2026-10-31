from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuthenticatedUser:
    username: str
    is_admin: bool = False
    auth_source: str = "local"


@dataclass(frozen=True)
class LdapAuthentication:
    username: str
    groups: tuple[str, ...] = ()


def hash_password(password: str, iterations: int = 310_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def verify_bearer_key(authorization: str | None, expected_key: str) -> bool:
    if not authorization or not expected_key:
        return False
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token:
        return False
    return hmac.compare_digest(token.strip(), expected_key)


class SessionManager:
    cookie_name = "sansuokb_session"

    def __init__(self, username: str, password_hash: str, lifetime_hours: int = 12):
        self.username = username
        self.password_hash = password_hash
        self.lifetime_seconds = max(1, lifetime_hours) * 3600
        self._signing_key = hashlib.sha256(f"agentic-rag-session:{password_hash}".encode()).digest()

    def create(self, username: str | None = None, is_admin: bool = True, auth_source: str = "local") -> str:
        payload = json.dumps(
            {"sub": username or self.username, "admin": bool(is_admin), "source": auth_source,
             "exp": int(time.time()) + self.lifetime_seconds, "nonce": secrets.token_hex(8)},
            separators=(",", ":"),
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        signature = hmac.new(self._signing_key, encoded.encode(), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def verify(self, token: str | None) -> bool:
        return self.user(token) is not None

    def user(self, token: str | None) -> AuthenticatedUser | None:
        if not token or "." not in token:
            return None
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(self._signing_key, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
            username = str(payload.get("sub") or "").strip()
            if not username or int(payload.get("exp", 0)) <= int(time.time()):
                return None
            return AuthenticatedUser(username, bool(payload.get("admin", username == self.username)), str(payload.get("source") or "local"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None


class LdapAuthenticator:
    """Optional LDAP bind authentication. It is deliberately lazy so offline deployments need no LDAP package."""

    def __init__(self, enabled: bool, server_url: str, user_dn_template: str = "",
                 bind_dn: str = "", bind_password: str = "", timeout: float = 5.0,
                 base_dn: str = "", user_filter: str = "(uid={username})",
                 group_base_dn: str = "", group_filter: str = "(member={user_dn})",
                 group_attribute: str = "cn"):
        self.enabled = enabled
        self.server_url = server_url.strip()
        self.user_dn_template = user_dn_template.strip()
        self.bind_dn = bind_dn.strip()
        self.bind_password = bind_password
        self.timeout = timeout
        self.base_dn = base_dn.strip()
        self.user_filter = user_filter or "(uid={username})"
        self.group_base_dn = group_base_dn.strip()
        self.group_filter = group_filter or "(member={user_dn})"
        self.group_attribute = group_attribute.strip() or "cn"

    def authenticate(self, username: str, password: str) -> bool:
        return self.authenticate_with_groups(username, password) is not None

    def authenticate_with_groups(self, username: str, password: str) -> LdapAuthentication | None:
        if not self.enabled or not self.server_url or not username or not password:
            return None
        try:
            import ldap3
            from ldap3.utils.conv import escape_filter_chars
            from ldap3.utils.dn import escape_rdn
            server = ldap3.Server(self.server_url, connect_timeout=self.timeout, get_info=ldap3.NONE)
            user_dn = self.user_dn_template.format(username=escape_rdn(username)) if self.user_dn_template else username
            if self.bind_dn and self.base_dn:
                search = ldap3.Connection(server, user=self.bind_dn, password=self.bind_password, receive_timeout=self.timeout, auto_bind=False)
                if not search.bind():
                    return None
                search.search(self.base_dn, self.user_filter.format(username=escape_filter_chars(username)), attributes=["entryDN", "memberOf"])
                if len(search.entries) != 1:
                    return None
                entry = search.entries[0]
                user_dn = str(entry.entry_dn)
                groups = self._entry_groups(entry)
                if self.group_base_dn:
                    group_filter = self.group_filter.format(user_dn=escape_filter_chars(user_dn), username=escape_filter_chars(username))
                    search.search(self.group_base_dn, group_filter, attributes=[self.group_attribute])
                    for group_entry in search.entries:
                        groups.extend(self._entry_groups(group_entry, self.group_attribute))
            else:
                groups = []
            connection = ldap3.Connection(server, user=user_dn, password=password, receive_timeout=self.timeout, auto_bind=False)
            return LdapAuthentication(username, tuple(dict.fromkeys(groups))) if connection.bind() else None
        except Exception:
            return None

    @staticmethod
    def _entry_groups(entry: Any, attribute: str = "memberOf") -> list[str]:
        try:
            raw = entry.entry_attributes_as_dict.get(attribute, [])
        except Exception:
            raw = []
        if isinstance(raw, str):
            raw = [raw]
        result = []
        for value in raw or []:
            text = str(value).strip()
            if not text:
                continue
            if attribute.casefold() == "memberof":
                text = text.split(",", 1)[0].split("=", 1)[-1]
            result.append(text)
        return result
