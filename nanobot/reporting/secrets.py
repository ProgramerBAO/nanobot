"""SecretRef resolution for reporting connectors.

The reporting layer accepts references only. A deployment can inject a richer
resolver for Vault or Kubernetes without changing connector contracts.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from nanobot.reporting.contracts import SecretRef


class SecretResolutionError(RuntimeError):
    """Raised when a connector secret cannot be resolved safely."""


def secret_ref_from_value(value: object) -> SecretRef | None:
    """Parse a SecretRef and reject plaintext secret values."""

    if value is None or value == "":
        return None
    if isinstance(value, SecretRef):
        return value
    if isinstance(value, Mapping):
        provider = str(value.get("provider") or "").strip()
        key = str(value.get("key") or "").strip()
        if provider not in {"env", "vault", "kubernetes"} or not key:
            raise ValueError("secret ref requires provider and key")
        return SecretRef(provider=provider, key=key)  # type: ignore[arg-type]
    raise ValueError("plaintext connector secrets are not allowed; use SecretRef")


def resolve_secret(ref: SecretRef, *, environ: Mapping[str, str] | None = None) -> str:
    """Resolve environment references; external providers require injection."""

    if ref.provider == "env":
        value = (environ or os.environ).get(ref.key, "")
        if not value:
            raise SecretResolutionError(f"environment variable {ref.key!r} is not set")
        return value
    raise SecretResolutionError(
        f"secret provider {ref.provider!r} requires an injected deployment resolver"
    )
