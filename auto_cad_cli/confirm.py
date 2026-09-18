"""The write gate: dry-run mints a token, confirm spends it exactly once.

CLI-SPEC section 7 in one place, so no write command re-implements any of it.

The token is an HMAC over everything that made the preview true - the command,
its arguments, the target file's digest, the target object's current state, the
account and the permission mode. Two consequences follow, and both are the
point:

- It cannot be forged by recomputing a public hash, because the key is a local
  secret generated on this machine. A token can only come from a real dry-run
  here.
- It stops being valid the moment anything it covers changes. Editing a drawing
  between preview and confirm invalidates the token rather than silently
  applying the preview to a file that has moved on.

Tokens are also single-use. A write that times out after the engine committed
must not be blindly retried; replaying the token is refused, and a fresh
dry-run shows what the file actually looks like now.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import autocad

# Resolved per call rather than at import, so a test can point the whole of it
# at a scratch directory. A suite that exercised the write gate against the
# operator's real config would both need their permission setting and leave
# spent tokens in their ledger.
ENV_STATE_DIR = "AUTO_CAD_CLI_STATE_DIR"


def state_directory() -> Path:
    override = os.environ.get(ENV_STATE_DIR, "").strip()
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".auto-cad-cli"


def secret_file() -> Path:
    return state_directory() / "confirm.secret"


def ledger_file() -> Path:
    return state_directory() / "confirm-consumed.json"


def config_file() -> Path:
    return state_directory() / "config.json"


TOKEN_PREFIX = "ct_"
TOKEN_LIFETIME = timedelta(minutes=15)

# Reading a whole DWG to digest it is what makes the token notice an edit that
# happened between preview and confirm. Drawings are megabytes, not gigabytes.
_DIGEST_CHUNK = 1 << 20


class PermissionError_(Exception):
    """Writes are disabled by local policy; only a human can change that."""


@dataclass(frozen=True)
class Token:
    value: str
    expires_at: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def permission() -> str:
    """`read` unless a human wrote `write` into the config file.

    Deliberately has no setter command. SEC-SPEC section 3 requires that an
    agent cannot escalate its own permission, and the simplest way to guarantee
    that is to give it nothing to call.
    """
    try:
        raw = json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "read"
    value = raw.get("permission")
    return "write" if value == "write" else "read"


def require_write_permission() -> None:
    if permission() != "write":
        raise autocad.EngineError(
            "E_FORBIDDEN",
            "local policy disables writes; a human must enable them",
            fix=f'set {{"permission": "write"}} in {config_file()}',
            config_file=str(config_file()),
        )


def _secret() -> bytes:
    """Machine-local HMAC key, created on first use.

    0600 is POSIX semantics. On Windows the protection is whatever ACL the user
    profile directory carries, so this does not claim to be owner-only there.
    """
    state_directory().mkdir(parents=True, exist_ok=True)
    if secret_file().is_file():
        return secret_file().read_bytes()
    key = secrets.token_bytes(32)
    secret_file().write_bytes(key)
    if os.name != "nt":
        secret_file().chmod(0o600)
    return key


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def scope(
    *,
    command: str,
    target: Path,
    arguments: dict[str, Any],
    observed: dict[str, Any],
) -> dict[str, Any]:
    """Everything the preview depended on, in a form that hashes stably."""
    return {
        "command": command,
        "file": str(target.resolve()),
        "file_digest": file_digest(target),
        "arguments": arguments,
        # The object's state as the preview saw it; an edit by anyone else
        # between preview and confirm changes this and voids the token.
        "observed": observed,
        "account": os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
        "permission": permission(),
    }


def _fingerprint(scope_data: dict[str, Any], expires_at: str) -> str:
    payload = json.dumps(
        {"scope": scope_data, "expires_at": expires_at},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def mint(scope_data: dict[str, Any]) -> Token:
    expires_at = _iso(_now() + TOKEN_LIFETIME)
    fingerprint = _fingerprint(scope_data, expires_at)
    # The expiry travels inside the token so verification needs no server state.
    return Token(value=f"{TOKEN_PREFIX}{fingerprint[:32]}.{expires_at}", expires_at=expires_at)


def _load_ledger() -> dict[str, str]:
    try:
        raw = json.loads(ledger_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_ledger(ledger: dict[str, str]) -> None:
    """Best effort: a ledger that cannot be written must not block the write.

    Losing replay protection is worse than nothing, but refusing an authorised
    write because a cache file is unwritable is worse still - and the resource
    digest in the scope still catches the dangerous case.
    """
    try:
        state_directory().mkdir(parents=True, exist_ok=True)
        ledger_file().write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    except OSError:
        pass


def _prune(ledger: dict[str, str]) -> dict[str, str]:
    now = _iso(_now())
    return {token: expiry for token, expiry in ledger.items() if expiry > now}


def verify_and_consume(presented: str, scope_data: dict[str, Any]) -> None:
    """Accept a token exactly once, or raise the error the agent should act on.

    Consumption is recorded *before* the caller performs the write. A crash
    mid-write then leaves the token spent, so a blind retry is refused and the
    agent is forced to look at what actually happened - the conservative
    direction (CLI-SPEC section 9).
    """
    if not presented.startswith(TOKEN_PREFIX) or "." not in presented:
        raise autocad.EngineError("E_CONFLICT", "confirm token is malformed; run --dry-run again")

    _, _, expires_at = presented.partition(".")
    if expires_at <= _iso(_now()):
        raise autocad.EngineError(
            "E_CONFLICT", "confirm token has expired; run --dry-run again", expired_at=expires_at
        )

    expected = f"{TOKEN_PREFIX}{_fingerprint(scope_data, expires_at)[:32]}.{expires_at}"
    if not hmac.compare_digest(presented, expected):
        raise autocad.EngineError(
            "E_CONFLICT",
            "confirm token does not match this operation or the drawing changed; "
            "run --dry-run again",
        )

    ledger = _prune(_load_ledger())
    if presented in ledger:
        raise autocad.EngineError(
            "E_CONFLICT",
            "confirm token has already been used; run --dry-run to see the current state",
        )
    ledger[presented] = expires_at
    _save_ledger(ledger)


def backup(target: Path) -> Path | None:
    """Byte copy of the drawing as it was before the write.

    Only covers what is on disk. A drawing open in the editor with unsaved
    changes is not represented here, and the write path says so rather than
    implying a rollback it cannot deliver.
    """
    try:
        directory = state_directory() / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        copy = directory / f"{target.stem}.{stamp}{target.suffix}"
        copy.write_bytes(target.read_bytes())
        return copy
    except OSError:
        return None
