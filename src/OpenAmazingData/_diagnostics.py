"""Failure-only JSON diagnostics on stderr; no response payloads or credentials."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

_credentials: set[str] = set()


def remember_credentials(*values) -> None:
    _credentials.update(str(v) for v in values if v)


def _redact(value) -> str:
    text = str(value)
    from tgwpy.compat import tgw_module as tgw
    secrets = _credentials | {os.environ.get(k, "") for k in ("TGW_USER", "TGW_PASSWORD", "TGW_TOKEN")}
    secrets.update((tgw.session().username, tgw.session().token))
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"""(?i)(["']?(?:password|passwd|token|username|userName|authorization)["']?\s*[:=]\s*)("[^"\n]*"|'[^'\n]*'|[^\s,;}]+)""", r"\1[REDACTED]", text)
    return text[:1600]


def emit_failure(event: str, **fields) -> None:
    # Logging must never prevent completion of an SDK response callback.
    try:
        record = {"time": datetime.now(timezone.utc).isoformat(), "event": event}
        record.update({key: value if isinstance(value, (int, float, bool)) or value is None else _redact(value)
                       for key, value in fields.items()})
        print("[OpenAmazingData] " + json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)
    except Exception:
        pass


def request_context(req) -> dict:
    return {key: getattr(req, key) for key in
            ("security_code", "market_type", "cyc_type", "begin_date", "end_date", "date")
            if hasattr(req, key)}
