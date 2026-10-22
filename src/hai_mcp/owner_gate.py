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
1. The first call to an owner-gated action issues a *challenge* bound to a
   fingerprint of the exact change (proposal body, recontract diff, abandon
   request). The same pending code challenge is returned on repeated calls,
   so an agent cannot spam the owner by retrying.
2. Channels ``file`` and ``ntfy`` deliver a one-time code the agent is not
   expected to read. ``HAI_HOME`` stores a salted SHA-256 hash of the code,
   never the code itself. The action succeeds only when the client presents
   that code.
3. Channel ``touchid`` (macOS) does not mint a code. The server process spawns
   ``touchid_prompt.swift``, which calls ``LAContext.evaluatePolicy``. Only a
   successful biometric or device-owner authentication, attested back for the
   same fingerprint and reason, is recorded in this process. The agent's
   follow-up call with the same arguments applies that exact change. A
   different body or diff needs a new prompt. Disk state alone is not a
   credential: another process, or a rewritten challenge file, cannot redeem
   the approval.

What this does NOT do: it cannot protect an owner channel the agent can read.
The ``file`` channel is exactly as strong as the filesystem boundary around
``HAI_OWNER_HOME``. When the agent runs as the same user with unrestricted
file access, use ``ntfy`` or ``touchid``. Touch ID does not stop someone who
can replace this server or the helper binary; that is the same user replacing
the control plane. Full write-up: ``docs/OWNER_GATE.md``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hai_mcp.config import OWNER_CHANNELS, Config
from hai_mcp.ids import validate_generated_id
from hai_mcp.paths import real_path
from hai_mcp.storage import read_json, write_json

OWNER_GATE_MODES = frozenset({"nonce", "ack_legacy"})
GATE_ACTIONS = frozenset({"accept_next_step", "recontract", "abandon_mission"})
MAX_ATTEMPTS = 3
_LIVE_CHALLENGE_STATUSES = frozenset({"pending", "approved"})

# Unambiguous alphabet (no 0/O, 1/I/L): the owner reads the code aloud or types it.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LEN = 8
_NTFY_TIMEOUT_SECONDS = 5

# macOS rejects or truncates a very long LAContext reason. Keep the decision text short.
_TOUCHID_REASON_MAX = 120
_EXCERPT_KEYS = ("next_step", "diff", "abandon_reason", "objective", "reason")
_METHOD_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_HELPER_STDOUT_MAX = 8192


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


def _touchid_platform_ok() -> bool:
    return sys.platform == "darwin"


def _preview_text(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return " ".join(text.split())


def build_touchid_reason(action: str, summary: str, preview: dict[str, Any], fp: str) -> str:
    """Short text shown in the macOS prompt: action, project, excerpt, fingerprint prefix.

    The dialog prefers a project token plus the change excerpt. ``summary`` fills the
    excerpt when the preview has no change text, so the owner still sees the decision.
    """
    prefix = fp.removeprefix("sha256:")[:12] or "unknown"
    tail = f"fp {prefix}"
    project = ""
    project_path = preview.get("project_path")
    if isinstance(project_path, str) and project_path.strip():
        project = Path(project_path).name
    elif isinstance(preview.get("mission_id"), str):
        project = str(preview["mission_id"])[-8:]
    head = f"{action} · {project}" if project else action
    ordered = [key for key in _EXCERPT_KEYS if key in preview]
    ordered.extend(key for key in preview if key not in ordered and key not in {"project_path", "mission_id"})
    excerpt = ""
    for key in ordered:
        text = _preview_text(preview.get(key))
        if text:
            excerpt = f"{key}: {text}"
            break
    if not excerpt:
        excerpt = " ".join(summary.split())
    room = _TOUCHID_REASON_MAX - len(head) - len(tail) - 6
    if excerpt and room > 8:
        if len(excerpt) > room:
            excerpt = excerpt[: room - 1].rstrip() + "…"
        reason = f"{head} · {excerpt} · {tail}"
    else:
        reason = f"{head} · {tail}"
    reason = " ".join(reason.split())
    if len(reason) > _TOUCHID_REASON_MAX:
        reason = reason[: _TOUCHID_REASON_MAX - 1].rstrip() + "…"
    return reason or f"{action} · {tail}"


def _sanitize_method(value: Any) -> str:
    if isinstance(value, str) and _METHOD_RE.fullmatch(value):
        return value
    return "device_owner"


def resolve_touchid_helper(cfg: Config) -> Path | None:
    """Helper the server will spawn. Never taken from a tool argument."""
    if cfg.owner_touchid_helper is not None:
        return cfg.owner_touchid_helper
    packaged = Path(__file__).resolve().with_name("touchid_prompt.swift")
    if packaged.is_file():
        return packaged
    return None


def _map_helper_failure(returncode: int, parsed: dict[str, Any] | None) -> tuple[str, str, str]:
    error = str(parsed.get("error")) if isinstance(parsed, dict) and parsed.get("error") else ""
    if returncode == 2 or error == "timeout":
        return "timeout", "denied", "Touch ID timed out. The gate stays closed."
    if error == "unavailable" or returncode == 3:
        return (
            "biometry_unavailable",
            "unavailable",
            "Touch ID is not available on this Mac. The gate stays closed.",
        )
    if error == "user_cancel" or returncode == 1:
        return "user_cancel", "denied", "Touch ID was cancelled. The gate stays closed."
    if error == "auth_failed":
        return "auth_failed", "denied", "Touch ID authentication failed. The gate stays closed."
    if parsed is None:
        return (
            "helper_missing",
            "unavailable",
            "Touch ID helper failed before the owner authenticated. The gate stays closed.",
        )
    return "auth_failed", "denied", "Touch ID was not approved. The gate stays closed."


def _require_matching_attestation(expected: dict[str, Any], result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("ok") is not True:
        error = result.get("error") if isinstance(result, dict) else None
        kind = "unavailable" if error == "unavailable" else "denied"
        detail = "biometry_unavailable" if error == "unavailable" else "auth_failed"
        raise OwnerChannelError("Touch ID was not approved. The gate stays closed.", kind=kind, detail=detail)
    if result.get("fingerprint") != expected["fingerprint"]:
        raise OwnerChannelError(
            "Touch ID helper attested a different change than the one being approved. The gate stays closed.",
            kind="denied",
            detail="fingerprint_mismatch",
        )
    if result.get("reason") != expected["reason"]:
        raise OwnerChannelError(
            "Touch ID helper did not confirm the approval text for this change. The gate stays closed.",
            kind="denied",
            detail="reason_mismatch",
        )
    return result


def _coerce_touchid_result(expected: dict[str, Any], result: Any) -> dict[str, Any]:
    if isinstance(result, dict) and result.get("ok") is not True:
        code = {"user_cancel": 1, "timeout": 2, "unavailable": 3, "auth_failed": 1}.get(
            str(result.get("error") or ""), 1
        )
        detail, kind, message = _map_helper_failure(code, result)
        raise OwnerChannelError(message, kind=kind, detail=detail)
    return _require_matching_attestation(expected, result)


def invoke_touchid_helper(
    cfg: Config,
    *,
    reason: str,
    fingerprint: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Spawn the Touch ID helper and return its attestation. Raises OwnerChannelError.

    The caller must check the attestation against the challenge it just created.
    A non-zero exit, a timeout, or JSON that claims success without exit 0 all
    fail closed. The plaintext of a one-time code is never passed in.
    """
    if not _touchid_platform_ok():
        raise OwnerChannelError(
            "Touch ID is only available on macOS. The gate stays closed.",
            kind="unavailable",
            detail="not_macos",
        )
    helper = resolve_touchid_helper(cfg)
    if helper is None or not helper.is_file():
        raise OwnerChannelError(
            "Touch ID helper is missing. Install the Xcode command line tools so `swift` can run "
            "touchid_prompt.swift, or set HAI_OWNER_TOUCHID_HELPER to a compiled binary. "
            "The gate stays closed.",
            kind="unavailable",
            detail="helper_missing",
        )
    helper_r = real_path(helper)
    hai_r = real_path(cfg.hai_home)
    if helper_r == hai_r or hai_r in helper_r.parents:
        raise OwnerChannelError(
            "Touch ID helper is inside HAI_HOME; the agent could replace it. "
            "Point HAI_OWNER_TOUCHID_HELPER outside HAI_HOME. The gate stays closed.",
            kind="unavailable",
            detail="helper_untrusted",
        )
    if helper_r.suffix == ".swift":
        swift = shutil.which("swift")
        if not swift:
            raise OwnerChannelError(
                "Touch ID helper is a Swift source file but `swift` is not on PATH. "
                "Install the Xcode command line tools, or set HAI_OWNER_TOUCHID_HELPER to a compiled binary. "
                "The gate stays closed.",
                kind="unavailable",
                detail="helper_missing",
            )
        command = [swift, str(helper_r)]
        grace = 45  # `swift` compiles the helper before the prompt appears
    else:
        if not os.access(helper_r, os.X_OK):
            raise OwnerChannelError(
                "Touch ID helper is not executable. The gate stays closed.",
                kind="unavailable",
                detail="helper_missing",
            )
        command = [str(helper_r)]
        grace = 2

    timeout = max(1, min(int(timeout_seconds), 180))
    payload = {"reason": reason, "fingerprint": fingerprint, "timeout_seconds": timeout}
    try:
        proc = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=timeout + grace,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OwnerChannelError(
            "Touch ID timed out before the owner finished authenticating. The gate stays closed.",
            kind="denied",
            detail="timeout",
        ) from exc
    except OSError as exc:
        raise OwnerChannelError(
            f"Touch ID helper could not be started ({exc}). The gate stays closed.",
            kind="unavailable",
            detail="helper_missing",
        ) from exc

    stdout = proc.stdout or b""
    if len(stdout) > _HELPER_STDOUT_MAX:
        raise OwnerChannelError(
            "Touch ID helper returned an unexpected response. The gate stays closed.",
            kind="unavailable",
            detail="helper_missing",
        )
    parsed: dict[str, Any] | None
    try:
        decoded = json.loads(stdout.decode("utf-8")) if stdout.strip() else None
        parsed = decoded if isinstance(decoded, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None

    # Exit status is the credential. A helper that prints ok:true and exits non-zero
    # does not approve — the OS result is the exit the server observed.
    if proc.returncode == 0 and parsed is not None and parsed.get("ok") is True:
        return parsed
    detail, kind, message = _map_helper_failure(proc.returncode, parsed)
    raise OwnerChannelError(message, kind=kind, detail=detail)


class OwnerChannelError(Exception):
    """Delivery to the owner failed, or the owner did not authenticate. The gate stays closed."""

    def __init__(self, message: str, *, kind: str = "unavailable", detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind if kind in {"unavailable", "denied"} else "unavailable"
        self.detail = detail or ("unavailable" if self.kind == "unavailable" else "user_cancel")


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
    # Test hook for Touch ID. Production leaves this None and spawns the Swift helper.
    # The hook receives {reason, fingerprint, timeout_seconds} and returns the helper JSON.
    touchid_evaluate: Callable[[dict[str, Any]], Any] | None = None

    def __post_init__(self) -> None:
        # Process-local credential. A challenge file marked approved is not enough:
        # only this process, after it observed the helper, may redeem the approval.
        self._touchid_ok: dict[str, str] = {}
        self._touchid_mu = threading.Lock()

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
            if self.cfg.owner_channel == "touchid":
                helper = resolve_touchid_helper(self.cfg)
                info["touchid_timeout_seconds"] = self.cfg.owner_touchid_timeout_seconds
                info["touchid_fallback"] = self.cfg.owner_touchid_fallback
                info["touchid_helper"] = str(helper) if helper else None
                info["touchid_platform_ok"] = _touchid_platform_ok()
                info["approval_ttl_seconds"] = self.cfg.owner_code_ttl_seconds
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
            if (
                isinstance(record, dict)
                and record.get("status") in _LIVE_CHALLENGE_STATUSES
                and record.get("expires_ts", 0) > now
            ):
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
        """Latest live challenge for this exact change; expired ones are retired on sight.

        ``approved`` is live for Touch ID: the owner authenticated, and the follow-up
        has not consumed it yet. ``pending`` is live for a code waiting to be relayed.
        """
        if not self.challenges_dir.is_dir():
            return None
        now = time.time()
        best: dict[str, Any] | None = None
        for path in sorted(self.challenges_dir.glob("C-*.json")):
            record = read_json(path, None)
            if not isinstance(record, dict) or record.get("status") not in _LIVE_CHALLENGE_STATUSES:
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

    def _gc_touchid_memory(self) -> None:
        for challenge_id in list(self._touchid_ok):
            fp = self._touchid_ok.get(challenge_id)
            try:
                record = read_json(self._path(challenge_id), None)
            except (OSError, ValueError):
                self._touchid_ok.pop(challenge_id, None)
                continue
            if (
                not isinstance(record, dict)
                or record.get("status") != "approved"
                or record.get("approval") != "touchid"
                or record.get("fingerprint") != fp
                or record.get("expires_ts", 0) <= time.time()
            ):
                self._touchid_ok.pop(challenge_id, None)

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
        if self.cfg.owner_channel == "touchid":
            raise OwnerChannelError("touchid does not deliver a one-time code")
        if self.cfg.owner_channel not in OWNER_CHANNELS:
            raise OwnerChannelError(f"unknown owner channel {self.cfg.owner_channel!r}")
        return FileOwnerChannel(self.cfg.resolved_owner_home(), self.cfg.hai_home)

    def _issue(
        self,
        action: str,
        fp: str,
        summary: str,
        preview: dict[str, Any],
        *,
        channel: FileOwnerChannel | NtfyOwnerChannel | None = None,
        delivery_extra: dict[str, Any] | None = None,
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
            chan = channel if channel is not None else self._channel()
            record["delivery"] = chan.deliver(record, code)
            if delivery_extra:
                record["delivery"].update(delivery_extra)
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
        issued: dict[str, Any] = {
            "challenge_id": record["challenge_id"],
            "action": action,
            "fingerprint": fp,
            "channel": record["delivery"].get("channel"),
            "expires_at": record["expires_at"],
        }
        if record["delivery"].get("fallback_from"):
            issued["fallback_from"] = record["delivery"]["fallback_from"]
        self.audit("owner_challenge_issued", issued)
        return record, None

    def _pending_code_response(self, pending: dict[str, Any], action: str, fp: str) -> dict[str, Any]:
        channel = pending.get("delivery", {}).get("channel")
        body: dict[str, Any] = {
            "ok": False,
            "error": "owner_gate_required",
            "gate": "nonce",
            "status": "pending_owner_code",
            "challenge_id": pending["challenge_id"],
            "action": action,
            "fingerprint": fp,
            "expires_at": pending["expires_at"],
            "attempts_remaining": MAX_ATTEMPTS - int(pending.get("attempts", 0)),
            "channel": channel,
            "message": (
                "Owner code required. A one-time code was delivered to the owner "
                f"via '{channel}' — not to this client. "
                "Ask the owner for the code and call again with owner_code=<code>. "
                "The code is bound to this exact change."
            ),
        }
        if pending.get("delivery", {}).get("fallback_from") == "touchid":
            body["fallback_from"] = "touchid"
            body["message"] = (
                "Touch ID is unavailable on this machine, and HAI_OWNER_TOUCHID_FALLBACK=file "
                "is set. A one-time code was delivered via the file channel — not to this client. "
                "Ask the owner for the code and call again with owner_code=<code>. "
                "The code is bound to this exact change."
            )
        return body

    def _malformed_code_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "owner_gate_required",
            "gate": "nonce",
            "detail": "malformed_owner_code",
            "message": "owner_code must be the 8-character code delivered to the owner (format XXXX-XXXX)",
        }

    def _decide_code(
        self,
        pending: dict[str, Any] | None,
        action: str,
        fp: str,
        summary: str,
        preview: dict[str, Any],
        code: str | None,
    ) -> tuple[bool, dict[str, Any]]:
        if code is None:
            if pending is None:
                pending, err = self._issue(action, fp, summary, preview)
                if err:
                    return False, err
            assert pending is not None
            return False, self._pending_code_response(pending, action, fp)

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

    def _evaluate_touchid(self, reason: str, fp: str) -> dict[str, Any]:
        payload = {
            "reason": reason,
            "fingerprint": fp,
            "timeout_seconds": max(1, min(int(self.cfg.owner_touchid_timeout_seconds), 180)),
        }
        try:
            if self.touchid_evaluate is not None:
                result = self.touchid_evaluate(payload)
            else:
                result = invoke_touchid_helper(
                    self.cfg,
                    reason=reason,
                    fingerprint=fp,
                    timeout_seconds=payload["timeout_seconds"],
                )
        except OwnerChannelError:
            raise
        except Exception as exc:
            raise OwnerChannelError(
                f"Touch ID helper failed ({exc}). The gate stays closed.",
                kind="unavailable",
                detail="helper_missing",
            ) from exc
        return _coerce_touchid_result(payload, result)

    def _touchid_failed(
        self,
        action: str,
        fp: str,
        summary: str,
        preview: dict[str, Any],
        exc: OwnerChannelError,
    ) -> dict[str, Any]:
        now = time.time()
        record: dict[str, Any] = {
            "challenge_id": _new_challenge_id(),
            "action": action,
            "fingerprint": fp,
            "summary": summary,
            "preview": preview,
            "created_at": _utc_iso(now),
            "created_ts": now,
            "expires_at": _utc_iso(now),
            "expires_ts": now,
            "attempts": 0,
            "status": "cancelled",
            "cancel_reason": exc.detail,
            "approval": "touchid",
            "delivery": {"channel": "touchid", "error": exc.detail},
        }
        self._save(record)
        self.audit(
            "owner_challenge_cancelled",
            {
                "challenge_id": record["challenge_id"],
                "action": action,
                "fingerprint": fp,
                "channel": "touchid",
                "reason": exc.detail,
            },
        )
        error = "owner_denied" if exc.kind == "denied" else "owner_channel_unavailable"
        return {
            "ok": False,
            "error": error,
            "gate": "nonce",
            "detail": exc.detail,
            "channel": "touchid",
            "challenge_id": record["challenge_id"],
            "fingerprint": fp,
            "message": str(exc),
        }

    def _record_touchid_approval(
        self,
        action: str,
        fp: str,
        summary: str,
        preview: dict[str, Any],
        attestation: dict[str, Any],
    ) -> dict[str, Any]:
        now = time.time()
        ttl = self.cfg.owner_code_ttl_seconds
        record: dict[str, Any] = {
            "challenge_id": _new_challenge_id(),
            "action": action,
            "fingerprint": fp,
            "summary": summary,
            "preview": preview,
            "created_at": _utc_iso(now),
            "created_ts": now,
            "expires_at": _utc_iso(now + ttl),
            "expires_ts": now + ttl,
            "attempts": 0,
            "status": "approved",
            "approval": "touchid",
            "delivery": {
                "channel": "touchid",
                "outcome": "approved",
                "method": _sanitize_method(attestation.get("method")),
            },
        }
        self._save(record)
        # Memory is the credential. Set it only after the disk record exists so a
        # crash between the two fails closed (the file alone will not redeem).
        self._touchid_ok[record["challenge_id"]] = fp
        self.audit(
            "owner_challenge_issued",
            {
                "challenge_id": record["challenge_id"],
                "action": action,
                "fingerprint": fp,
                "channel": "touchid",
                "expires_at": record["expires_at"],
                "touchid_method": record["delivery"]["method"],
            },
        )
        return {
            "ok": False,
            "error": "owner_gate_required",
            "gate": "nonce",
            "status": "owner_approved",
            "challenge_id": record["challenge_id"],
            "action": action,
            "fingerprint": fp,
            "expires_at": record["expires_at"],
            "channel": "touchid",
            "message": (
                "Owner approved this exact change with Touch ID on this Mac. "
                "Call again with the same arguments and without owner_code to apply it. "
                "The approval is single-use, expires, and does not cover a changed body or diff. "
                "This client was not given a code."
            ),
        }

    def _consume_touchid(self, pending: dict[str, Any], action: str, fp: str) -> tuple[bool, dict[str, Any]]:
        pending["status"] = "consumed"
        pending["consumed_at"] = _utc_iso()
        self._save(pending)
        self._touchid_ok.pop(pending["challenge_id"], None)
        audit = self.audit(
            "owner_gate_passed",
            {
                "challenge_id": pending["challenge_id"],
                "action": action,
                "fingerprint": fp,
                "channel": "touchid",
            },
        )
        return True, {
            "gate": "nonce",
            "channel": "touchid",
            "challenge_id": pending["challenge_id"],
            "audit_event_id": audit["event_id"],
        }

    def _retire_untrusted(self, record: dict[str, Any], action: str) -> None:
        record["status"] = "cancelled"
        record["cancel_reason"] = "approval_not_in_process"
        record["cancelled_at"] = _utc_iso()
        self._save(record)
        self._touchid_ok.pop(record["challenge_id"], None)
        self.audit(
            "owner_challenge_cancelled",
            {
                "challenge_id": record["challenge_id"],
                "action": action,
                "reason": "approval_not_in_process",
                "channel": "touchid",
            },
        )

    def _prompt_touchid(
        self,
        action: str,
        fp: str,
        summary: str,
        preview: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        reason = build_touchid_reason(action, summary, preview, fp)
        try:
            attestation = self._evaluate_touchid(reason, fp)
        except OwnerChannelError as exc:
            if exc.kind == "unavailable" and self.cfg.owner_touchid_fallback == "file":
                record, err = self._issue(
                    action,
                    fp,
                    summary,
                    preview,
                    channel=FileOwnerChannel(self.cfg.resolved_owner_home(), self.cfg.hai_home),
                    delivery_extra={"fallback_from": "touchid", "fallback_detail": exc.detail},
                )
                if err or record is None:
                    assert err is not None
                    return False, err
                return False, self._pending_code_response(record, action, fp)
            return False, self._touchid_failed(action, fp, summary, preview, exc)
        return False, self._record_touchid_approval(action, fp, summary, preview, attestation)

    def _require_touchid_locked(
        self,
        *,
        action: str,
        fp: str,
        summary: str,
        preview: dict[str, Any],
        supplied: bool,
        code: str | None,
    ) -> tuple[bool, dict[str, Any]]:
        pending = self._find_pending(action, fp)
        self._gc_touchid_memory()

        # Explicit file fallback already delivered a code for this exact change.
        # Stay on that challenge; do not prompt again and do not treat a code as
        # proof unless this record is actually a file-channel code.
        if pending and pending.get("delivery", {}).get("channel") == "file" and pending.get("code_hash"):
            if supplied and code is None:
                return False, self._malformed_code_result()
            return self._decide_code(pending, action, fp, summary, preview, code)

        if pending and pending.get("status") == "approved" and pending.get("approval") == "touchid":
            attested = self._touchid_ok.get(pending["challenge_id"])
            if attested == fp and pending.get("fingerprint") == fp:
                # owner_code is not a credential on this channel. Ignore it.
                return self._consume_touchid(pending, action, fp)
            self._retire_untrusted(pending, action)

        return self._prompt_touchid(action, fp, summary, preview)

    def _require_code_locked(
        self,
        *,
        action: str,
        fp: str,
        summary: str,
        preview: dict[str, Any],
        code: str | None,
    ) -> tuple[bool, dict[str, Any]]:
        pending = self._find_pending(action, fp)
        return self._decide_code(pending, action, fp, summary, preview, code)

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

        # Touch ID holds an in-process lock across the prompt so two calls cannot
        # each pop a dialog, then the same cross-process flock as the code channels.
        if self.cfg.owner_channel == "touchid":
            with self._touchid_mu:
                with self._lock():
                    return self._require_touchid_locked(
                        action=action,
                        fp=fp,
                        summary=summary,
                        preview=preview,
                        supplied=supplied,
                        code=code,
                    )

        if supplied and code is None:
            return False, self._malformed_code_result()

        with self._lock():
            return self._require_code_locked(
                action=action,
                fp=fp,
                summary=summary,
                preview=preview,
                code=code,
            )
