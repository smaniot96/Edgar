"""Log redaction and error summaries shared by the API, agent and CLI sinks.

Patterns catch only known credential forms. Free text, including provider validation prose,
is not detectable, so failures log `error_summary` instead of the exception message.
"""

import re

REDACTED = "[REDACTED]"
REQUEST_ID = re.compile(r"[a-zA-Z0-9._:-]{1,128}\Z")
_CREDENTIAL_URL = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@]+(?::[^\s/@]*)?@", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[a-z0-9._~+/-]+=*", re.IGNORECASE)
_OPENAI_KEY = re.compile(r"\bsk-[a-zA-Z0-9_-]{16,}")
_POSTGRES_DETAIL = re.compile(r"(?ms)^[ \t|]*DETAIL:.*")


def redact_text(value: str) -> str:
    # PostgreSQL includes full row contents / conflicting keys independently of
    # SQLAlchemy's hide_parameters setting. Rows may themselves contain newlines.
    value = _POSTGRES_DETAIL.sub("[PostgreSQL detail redacted]\n", value)
    value = _CREDENTIAL_URL.sub(r"\1[REDACTED]@", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    return _OPENAI_KEY.sub(REDACTED, value)


def error_summary(exc: BaseException) -> str:
    """Exception class, HTTP status and provider request id, never the message.

    Provider and validation messages can echo prompts, player text or response bodies, so the
    message is dropped rather than redacted.
    """
    parts = [type(exc).__name__]
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        parts.append(f"status={status}")
    request_id = getattr(exc, "request_id", None)
    if isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id):
        parts.append(f"provider_request_id={request_id}")
    return " ".join(parts)
