"""Structured gateway status, also a string for legacy callback consumers."""
from __future__ import annotations


class GatewayStatus(str):
    def __new__(cls, status, tag, message=""):
        value = super().__new__(cls, f"{message} (status={status}, tag={tag})")
        value.status = status
        value.tag = tag
        return value


class DataEmpty(GatewayStatus):
    """Explicit empty envelope, not evidence of suspension or window coverage."""
    def __new__(cls):
        return super().__new__(cls, -100, "DataEmpty")


def envelope_status(env: dict, *, partial: bool = False) -> GatewayStatus:
    status = env.get("status")
    tag = (env.get("headers") or {}).get("tag")
    # A bare -100, contradictory payload or unfinished multipart result is not
    # the observed empty-envelope contract. Never discard those as successful.
    if status == -100 and tag == "DataEmpty" and not env.get("data") and not partial:
        return DataEmpty()
    return GatewayStatus(status, tag, env.get("msg") or "")
