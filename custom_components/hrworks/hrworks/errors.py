"""Fixed error codes: never employee data, error text or credentials."""

from __future__ import annotations

ERRORS = {
    "invalid_auth",
    "login_timeout",
    "mfa_required",
    "invalid_code",
    "unsupported_mfa",
    "session_expired",
    "portal_changed",
    "browser_unavailable",
    "writes_disabled",
    "overlap",
    "proposal_mismatch",
    "invalid_interval",
    "write_uncertain",
    "not_found",
    "busy",
    "invalid_request",
}
ERROR_STAGES = {"browser_connect", "browser_operation"}
ERROR_DETAILS = {
    "connection_failed",
    "http_401",
    "http_403",
    "http_429",
    "http_5xx",
    "tab_gone",
    "timeout",
}


class WorkerError(Exception):
    """Fixed error codes only; never employee data, stderr, or credentials."""

    def __init__(self, code: str, *, stage: str | None = None, detail: str | None = None):
        self.code = code
        self.stage = stage
        self.detail = detail
        super().__init__(code)
