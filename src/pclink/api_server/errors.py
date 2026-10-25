# src/pclink/api_server/errors.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025 AZHAR ZOUHIR / BYTEDz

"""One machine-readable error envelope for every HTTP failure.

Every non-2xx response uses::

    {
      "code": "SERVICE_UNAVAILABLE",   # stable, client-switchable
      "message": "...",                # human readable
      "retryable": true,               # safe to retry later
      "details": {...} | [...] | null, # structured context
      "detail": ...                    # legacy mirror (pre-envelope clients)
    }

Clients must never have to infer behaviour from a status code alone.
"""

from typing import Any, Dict, Optional

from fastapi.responses import JSONResponse

# Status codes where replaying the request later can legitimately succeed.
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

STATUS_CODE_NAMES: Dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    408: "REQUEST_TIMEOUT",
    409: "CONFLICT",
    410: "GONE",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    425: "TOO_EARLY",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    501: "NOT_IMPLEMENTED",
    502: "UPSTREAM_UNAVAILABLE",
    503: "SERVICE_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}


def _split_detail(detail: Any) -> tuple[str, Any]:
    """Derive a human message from any FastAPI ``detail`` shape."""
    if isinstance(detail, str):
        return detail, None
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail") or detail.get("error")
        if not isinstance(message, str) or not message:
            message = "Request failed"
        return message, detail
    if isinstance(detail, list):
        return "Validation failed", detail
    if detail is None:
        return "Request failed", None
    return str(detail), None


def error_payload(status_code: int, detail: Any = None) -> Dict[str, Any]:
    message, details = _split_detail(detail)
    return {
        "code": STATUS_CODE_NAMES.get(status_code, f"HTTP_{status_code}"),
        "message": message,
        "retryable": status_code in RETRYABLE_STATUS_CODES,
        "details": details,
        # Kept until every shipped client reads the envelope.
        "detail": detail if detail is not None else message,
    }


def error_response(
    status_code: int,
    detail: Any = None,
    headers: Optional[Dict[str, str]] = None,
    details: Any = None,
) -> JSONResponse:
    payload = error_payload(status_code, detail)
    if details is not None:
        payload["details"] = details
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers=headers,
    )
