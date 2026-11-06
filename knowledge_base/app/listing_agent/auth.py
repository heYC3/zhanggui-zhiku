"""服务端会话。业务接口只认这里签发的令牌，不认请求体里的身份。"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path


def _hash_password(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return digest.hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionActor:
    user_id: str
    role: str
    org_id: str


class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                user_id TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL,
                org_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                org_id TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def register(self, user_id: str, password: str, role: str, org_id: str) -> None:
        if role not in {"editor", "viewer"}:
            raise ValueError("身份只能是编辑或查看")
        salt = secrets.token_bytes(16)
        self._conn.execute(
            """
            INSERT INTO accounts (user_id, password_hash, salt, role, org_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                password_hash = excluded.password_hash,
                salt = excluded.salt,
                role = excluded.role,
                org_id = excluded.org_id
            """,
            (user_id.strip(), _hash_password(password, salt), salt.hex(), role, org_id.strip()),
        )
        self._conn.commit()

    def login(self, user_id: str, password: str) -> str | None:
        row = self._conn.execute(
            "SELECT password_hash, salt, role, org_id FROM accounts WHERE user_id = ?",
            (user_id.strip(),),
        ).fetchone()
        if row is None:
            return None
        salt = bytes.fromhex(row["salt"])
        candidate = _hash_password(password, salt)
        if not hmac.compare_digest(candidate, row["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        self._conn.execute(
            "INSERT INTO sessions (token_hash, user_id, role, org_id) VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id.strip(), row["role"], row["org_id"]),
        )
        self._conn.commit()
        return token

    def resolve(self, token: str) -> SessionActor | None:
        if not token:
            return None
        row = self._conn.execute(
            "SELECT user_id, role, org_id FROM sessions WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
        if row is None:
            return None
        return SessionActor(row["user_id"], row["role"], row["org_id"])


def load_accounts_from_env(store: AuthStore) -> None:
    """LISTING_ACCOUNTS=用户:密码:角色:机构,用户2:密码:角色:机构"""
    raw = os.getenv("LISTING_ACCOUNTS", "").strip()
    if not raw:
        return
    for piece in raw.split(","):
        parts = [item.strip() for item in piece.split(":")]
        if len(parts) != 4 or not parts[0] or not parts[1]:
            continue
        user_id, password, role, org_id = parts
        if role not in {"editor", "viewer"}:
            continue
        store.register(user_id, password, role, org_id)
