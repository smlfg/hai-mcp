"""Owner gate as a separate principal: challenge/response with hash-only storage.

Threat model
-----------
The MCP client — i.e. the agent — can call every tool and can usually read
everything under ``HAI_HOME``. Before this module an owner-gated action was
passed by sending ``owner_ack=true``: an assertion made by the very party the
gate is meant to constrain. A drifting, hallucinating or prompt-injected agent
walked straight through.

Mechanism
---------
1. The first call to an owner-gated action issues a *challenge*: a short
   one-time code bound to a fingerprint of the exact change (proposal body,
   recontract diff, abandon request). The same pending challenge is returned
   on repeated calls, so an agent cannot spam the owner by retrying.
2. The plaintext code is delivered only through an *owner channel* the agent
   is not expected to read: a separate directory (``file``) or a push
   notification (``ntfy``). ``HAI_HOME`` stores a salted SHA-256 hash of the
   code, never the code itself.
3. The action succeeds only when the client presents the code — which it can
   only have if a human relayed it. Codes are single-use, expire after
   ``HAI_OWNER_CODE_TTL`` seconds and allow ``MAX_ATTEMPTS`` guesses.

What this does NOT do: it cannot protect an owner channel the agent can read.
The ``file`` channel is exactly as strong as the filesystem boundary around
``HAI_OWNER_HOME``. When the agent runs as the same user with unrestricted
file access, use ``ntfy`` (or another off-machine channel).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hai_mcp.config import Config
from hai_mcp.ids import validate_generated_id
from hai_mcp.paths import real_path
from hai_mcp.storage import read_json, write_json

OWNER_GATE_MODES = frozenset({"nonce", "ack_legacy"})
OWNER_CHANNELS = frozenset({"file", "ntfy"})
GATE_ACTIONS = frozenset({"accept_next_step", "recontract", "abandon_mission"})
MAX_ATTEMPTS = 3

# Unambiguous alphabet (no 0/O, 1/I/L): the owner reads the code aloud or types it.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LEN = 8
_NTFY_TIMEOUT_SECONDS = 5


def _utc_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def _new_challenge_id() -> str:
    return f"C-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def generate_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(value: Any) -> str | None:
    """Accept ``k7f3-m9qw``, ``K7F3 M9QW`` or ``K7F3M9QW``; return canonical ``K7F3-M9QW`` or None."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value.upper() if ch.isalnum())
    if len(cleaned) != _CODE_LEN or any(ch not in _CODE_ALPHABET for ch in cleaned):
        return None
    return f"{cleaned[:4]}-{cleaned[4:]}"


def fingerprint(subject: dict[str, Any]) -> str:
    """Bind a challenge to one exact change: same subject → same fingerprint."""
    canonical = json.dumps(subject, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hash_code(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def _owner_message(challenge: dict[str, Any], code: str) -> str:
    """The text the OWNER sees. It must say what is being approved — this is a decision, not a captcha."""
    preview = challenge.get("preview") or {}
    lines = [
        f"HAI owner code: {code}",
        f"Action: {challenge['action']}",
        f"Summary: {challenge['summary']}",
    ]
    for key, value in preview.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        lines.append(f"{key}: {text}")
    lines.append(f"Expires: {challenge['expires_at']}")
    lines.append(
        "Approve by giving this code to your agent. To deny, do nothing — the code expires."
    )
    return "\n".join(lines) + "\n"


class OwnerChannelError(Exception):
    """Delivery to the owner failed; the gate stays closed."""


class FileOwnerChannel:
    """Write the code to HAI_OWNER_HOME — a directory the agent must not be able to read."""

    name = "file"

    def __init__(self, owner_home: Path, hai_home: Path) -> None:
        self.owner_home = owner_home
        self.hai_home = hai_home

    def deliver(self, challenge: dict[str, Any], code: str) -> dict[str, Any]:
        owner_home = real_path(self.owner_home)
        hai_home = real_path(self.hai_home)
        if owner_home == hai_home or hai_home in owner_home.parents:
            raise OwnerChannelError(
                "HAI_OWNER_HOME is inside HAI_HOME; the agent could read every code. "
                "Point HAI_OWNER_HOME to a directory the agent cannot read."
            )
        owner_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(owner_home, 0o700)
        except OSError:
            pass
        path = owner_home / f"{challenge['challenge_id']}.txt"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_owner_message(challenge, code))
        return {"channel": self.name, "path": str(path)}


class NtfyOwnerChannel:
    """Push the code to an ntfy topic (phone/desktop). The topic name is a secret: use a random one."""

    name = "ntfy"

    def __init__(
        self,
        url: str,
        topic: str,
        token: str | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.topic = topic
        self.token = token
        self._opener = opener or urllib.request.urlopen

    def deliver(self, challenge: dict[str, Any], code: str) -> dict[str, Any]:
        headers = {
            "Title": f"HAI owner code for {challenge['action']}",
            "Priority": "high",
            "Tags": "lock",
            "Content-Type": "text/plain; charset=utf-8",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.url}/{self.topic}",
            data=_owner_message(challenge, code).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener(request, timeout=_NTFY_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise OwnerChannelError(f"ntfy delivery failed: {exc}") from exc
        if not 200 <= int(status) < 300:
            raise OwnerChannelError(f"ntfy delivery failed with HTTP {status}")
        return {"channel": self.name, "topic": self.topic, "http_status": int(status)}


@dataclass
class OwnerGate:
    cfg: Config
    audit: Callable[[str, dict[str, Any]], dict[str, Any]]
    opener: Callable[..., Any] | None = None  # test hook for the ntfy channel

    # ----- introspection -------------------------------------------------

    @property
    def mode(self) -> str:
        return self.cfg.owner_gate

    @property
    def challenges_dir(self) -> Path:
        return self.cfg.hai_home / "owner" / "challenges"

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "mode": self.mode,
            "max_attempts": MAX_ATTEMPTS,
            "code_ttl_seconds": self.cfg.owner_code_ttl_seconds,
        }
        if self.mode == "nonce":
            info["channel"] = self.cfg.owner_channel
            if self.cfg.owner_channel == "file":
                info["owner_home"] = str(self.cfg.resolved_owner_home())
            if self.cfg.owner_channel == "ntfy":
                info["ntfy_url"] = self.cfg.owner_ntfy_url
                info["ntfy_topic_configured"] = bool(self.cfg.owner_ntfy_topic)
        else:
            info["warning"] = (
                "ack_legacy: the client asserts owner approval itself; "
                "this is an honor system, not a gate"
            )
        return info

    def pending_count(self) -> int:
        if not self.challenges_dir.is_dir():
            return 0
        now = time.time()
        count = 0
        for path in self.challenges_dir.glob("C-*.json"):
            record = read_json(path, None)
            if isinstance(record, dict) and record.get("status") == "pending" and record.get("expires_ts", 0) > now:
                count += 1
        return count

    # ----- storage ---------------------------------------------------------

    @contextlib.contextmanager
    def _lock(self):
        lock_path = self.cfg.hai_home / ".owner_gate.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _path(self, challenge_id: str) -> Path:
        ok, message = validate_generated_id(challenge_id, expected_prefix="C")
        if not ok:
            raise ValueError(message)
        return self.challenges_dir / f"{challenge_id}.json"

    def _save(self, record: dict[str, Any]) -> None:
        write_json(self._path(record["challenge_id"]), record)

    def _find_pending(self, action: str, fp: str) -> dict[str, Any] | None:
        """Latest live challenge for this exact change; expired ones are retired on sight."""
        if not self.challenges_dir.is_dir():
            return None
        now = time.time()
        best: dict[str, Any] | None = None
        for path in sorted(self.challenges_dir.glob("C-*.json")):
            record = read_json(path, None)
            if not isinstance(record, dict) or record.get("status") != "pending":
                continue
            if record.get("action") != action or record.get("fingerprint") != fp:
                continue
            if record.get("expires_ts", 0) <= now:
                record["status"] = "expired"
                record["expired_at"] = _utc_iso(now)
                self._save(record)
                self.audit("owner_challenge_expired", {"challenge_id": record["challenge_id"], "action": action})
                continue
            if best is None or record.get("created_ts", 0) >= best.get("created_ts", 0):
                best = record
        return best

    # ----- channel -----------------------------------------------------------

    def _channel(self) -> FileOwnerChannel | NtfyOwnerChannel:
        if self.cfg.owner_channel == "ntfy":
            if not self.cfg.owner_ntfy_topic:
                raise OwnerChannelError("HAI_OWNER_NTFY_TOPIC is not set")
            return NtfyOwnerChannel(
                self.cfg.owner_ntfy_url,
                self.cfg.owner_ntfy_topic,
                token=self.cfg.owner_ntfy_token,
                opener=self.opener,
            )
        return FileOwnerChannel(self.cfg.resolved_owner_home(), self.cfg.hai_home)

    def _issue(
        self,
        action: str,
        fp: str,
        summary: str,
        preview: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        now = time.time()
        code = generate_code()
        salt = secrets.token_hex(16)
        record: dict[str, Any] = {
            "challenge_id": _new_challenge_id(),
            "action": action,
            "fingerprint": fp,
            "summary": summary,
            "preview": preview,
            "code_hash": _hash_code(salt, code),
            "salt": salt,
            "created_at": _utc_iso(now),
            "created_ts": now,
            "expires_at": _utc_iso(now + self.cfg.owner_code_ttl_seconds),
            "expires_ts": now + self.cfg.owner_code_ttl_seconds,
            "attempts": 0,
            "status": "pending",
        }
        try:
            record["delivery"] = self._channel().deliver(record, code)
        except OwnerChannelError as exc:
            record["status"] = "cancelled"
            record["cancel_reason"] = "delivery_failed"
            record["delivery"] = {"channel": self.cfg.owner_channel, "error": str(exc)}
            self._save(record)
            self.audit(
                "owner_challenge_cancelled",
                {"challenge_id": record["challenge_id"], "action": action, "reason": "delivery_failed"},
            )
            return None, {
                "ok": False,
                "error": "owner_channel_unavailable",
                "gate": "nonce",
                "message": f"owner code could not be delivered: {exc}",
            }
        finally:
            del code  # the plaintext never outlives delivery
        self._save(record)
        self.audit(
            "owner_challenge_issued",
            {
                "challenge_id": record["challenge_id"],
                "action": action,
                "fingerprint": fp,
                "channel": record["delivery"].get("channel"),
                "expires_at": record["expires_at"],
            },
        )
        return record, None

    # ----- the gate ------------------------------------------------------------

    def require(
        self,
        *,
        action: str,
        subject: dict[str, Any],
        summary: str,
        preview: dict[str, Any],
        owner_code: Any,
    ) -> tuple[bool, dict[str, Any]]:
        """Return ``(passed, result)``.

        ``passed=False`` → ``result`` is a complete fail-closed tool result.
        ``passed=True``  → ``result`` describes the consumed challenge for the audit trail.
        """
        if action not in GATE_ACTIONS:
            raise ValueError(f"unknown gate action: {action}")
        if self.mode != "nonce":
            raise RuntimeError("OwnerGate.require is only valid in nonce mode")

        fp = fingerprint(subject)
        supplied = owner_code not in (None, "")
        code = normalize_code(owner_code) if supplied else None
        if supplied and code is None:
            return False, {
                "ok": False,
                "error": "owner_gate_required",
                "gate": "nonce",
                "detail": "malformed_owner_code",
                "message": "owner_code must be the 8-character code delivered to the owner (format XXXX-XXXX)",
            }

        with self._lock():
            pending = self._find_pending(action, fp)

            if code is None:
                if pending is None:
                    pending, err = self._issue(action, fp, summary, preview)
                    if err:
                        return False, err
                assert pending is not None
                return False, {
                    "ok": False,
                    "error": "owner_gate_required",
                    "gate": "nonce",
                    "status": "pending_owner_code",
                    "challenge_id": pending["challenge_id"],
                    "action": action,
                    "fingerprint": fp,
                    "expires_at": pending["expires_at"],
                    "attempts_remaining": MAX_ATTEMPTS - int(pending.get("attempts", 0)),
                    "channel": pending.get("delivery", {}).get("channel"),
                    "message": (
                        "Owner code required. A one-time code was delivered to the owner "
                        f"via '{pending.get('delivery', {}).get('channel')}' — not to this client. "
                        "Ask the owner for the code and call again with owner_code=<code>. "
                        "The code is bound to this exact change."
                    ),
                }

            if pending is None:
                return False, {
                    "ok": False,
                    "error": "owner_gate_required",
                    "gate": "nonce",
                    "detail": "no_pending_challenge",
                    "fingerprint": fp,
                    "message": (
                        "no live owner challenge for this exact change (never issued, already used, "
                        "expired or cancelled); call again without owner_code to issue a new one"
                    ),
                }

            if hmac.compare_digest(_hash_code(pending["salt"], code), pending["code_hash"]):
                pending["status"] = "consumed"
                pending["consumed_at"] = _utc_iso()
                self._save(pending)
                audit = self.audit(
                    "owner_gate_passed",
                    {"challenge_id": pending["challenge_id"], "action": action, "fingerprint": fp},
                )
                return True, {
                    "gate": "nonce",
                    "challenge_id": pending["challenge_id"],
                    "audit_event_id": audit["event_id"],
                }

            pending["attempts"] = int(pending.get("attempts", 0)) + 1
            remaining = MAX_ATTEMPTS - pending["attempts"]
            if remaining <= 0:
                pending["status"] = "cancelled"
                pending["cancel_reason"] = "max_attempts"
            self._save(pending)
            self.audit(
                "owner_gate_failed",
                {
                    "challenge_id": pending["challenge_id"],
                    "action": action,
                    "attempts": pending["attempts"],
                    "cancelled": remaining <= 0,
                },
            )
            return False, {
                "ok": False,
                "error": "owner_gate_required",
                "gate": "nonce",
                "detail": "invalid_owner_code",
                "challenge_id": pending["challenge_id"],
                "attempts_remaining": max(remaining, 0),
                "status": "cancelled" if remaining <= 0 else "pending_owner_code",
                "message": (
                    "owner code rejected; the challenge is cancelled — the owner will be asked again on the next call"
                    if remaining <= 0
                    else f"owner code rejected; {remaining} attempt(s) remaining"
                ),
            }
