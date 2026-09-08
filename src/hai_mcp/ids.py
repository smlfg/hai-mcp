from __future__ import annotations

import re

# \Z (not $) so a trailing newline cannot slip past — Python's $ matches before a final \n.
_GENERATED_ID_RE = re.compile(r"\A(M|S|P|A|I|C)-\d{8}T\d{6}-[0-9a-f]{8}\Z")

_PREFIX_LABELS = {
    "M": "mission_id",
    "S": "session_id",
    "P": "parking_id",
    "A": "audit_id",
    "I": "intake_id",
    "C": "challenge_id",
}


class IdentifierError(ValueError):
    """Raised when a syntactically invalid identifier reaches an internal store path.

    Carries a structured (code, field, message) so callers translate it into a
    fail-closed public result instead of masking an invalid id as ``None``.
    """

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


def validate_generated_id(value: object, *, expected_prefix: str | None = None) -> tuple[bool, str]:
    """Return (ok, error_message). Strict: no coercion, no whitespace trimming.

    Rejects non-strings, whitespace-padded ids, path separators, ``..``, null
    bytes and foreign prefixes so a user-controlled id can never become a path.
    """
    field = _PREFIX_LABELS.get(expected_prefix or "", "id")
    if not isinstance(value, str):
        return False, f"{field} must be a string"
    raw = value  # deliberately NOT stripped: surrounding whitespace is invalid
    if not raw:
        return False, f"{field} is required"
    if "/" in raw or "\\" in raw or ".." in raw or "\x00" in raw:
        return False, f"invalid {field}: path traversal rejected"
    if expected_prefix and not raw.startswith(f"{expected_prefix}-"):
        return False, f"invalid {field} format"
    if not _GENERATED_ID_RE.match(raw):
        label = _PREFIX_LABELS.get(expected_prefix or raw[:1], "id")
        return False, f"invalid {label} format"
    if expected_prefix and raw[0] != expected_prefix:
        return False, f"invalid {field} format"
    return True, ""


def validate_mission_id(mission_id: object) -> tuple[bool, str]:
    return validate_generated_id(mission_id, expected_prefix="M")


def validate_session_id(session_id: object) -> tuple[bool, str]:
    return validate_generated_id(session_id, expected_prefix="S")


def validate_intake_id(intake_id: object) -> tuple[bool, str]:
    return validate_generated_id(intake_id, expected_prefix="I")


def require_generated_id(value: object, *, expected_prefix: str | None = None) -> str:
    """Return the id if valid, else raise a structured IdentifierError.

    Used by internal store loaders so a syntactically invalid id fails closed
    rather than being tarnished as a benign ``None`` (not-found).
    """
    ok, message = validate_generated_id(value, expected_prefix=expected_prefix)
    if not ok:
        field = _PREFIX_LABELS.get(expected_prefix or "", "id")
        raise IdentifierError("invalid_args", field, message)
    return value  # type: ignore[return-value]


def require_mission_id(mission_id: object) -> str:
    return require_generated_id(mission_id, expected_prefix="M")


def require_session_id(session_id: object) -> str:
    return require_generated_id(session_id, expected_prefix="S")


def require_intake_id(intake_id: object) -> str:
    return require_generated_id(intake_id, expected_prefix="I")
