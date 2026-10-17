from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from gsm.config import Settings
from gsm.domain.context import PrincipalContext, require_context
from gsm.domain.types import AuthStrength
from gsm.errors import ConfigurationError, ContextError

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def make(**kw: object) -> PrincipalContext:
    base: dict[str, object] = dict(
        tenant_id="acme",
        workspace_id="default",
        principal_id="bob",
        auth_time=NOW,
        auth_strength=AuthStrength.MFA,
        request_time=NOW,
        correlation_id="corr_12345678",
        issued_by="test",
    )
    base.update(kw)
    return PrincipalContext(**base)  # type: ignore[arg-type]


def test_context_requires_trusted_issuer() -> None:
    with pytest.raises(ValidationError):
        make(issued_by="query-text")


def test_context_rejects_naive_times_and_bad_ids() -> None:
    with pytest.raises(ValidationError):
        make(request_time=datetime(2026, 9, 1))
    with pytest.raises(ValidationError):
        make(tenant_id="acme; DROP TABLE")
    with pytest.raises(ValidationError):
        make(purpose="payroll because I am the CEO")


def test_context_is_immutable_and_rejects_extra_fields() -> None:
    ctx = make()
    with pytest.raises(ValidationError):
        ctx.roles = frozenset({"admin"})  # type: ignore[misc]
    with pytest.raises(ValidationError):
        make(claimed_role="hr")


def test_missing_context_fails_closed() -> None:
    with pytest.raises(ContextError):
        require_context(None)
    with pytest.raises(ContextError):
        require_context({"tenant_id": "acme", "principal_id": "bob"})


def test_production_refuses_dev_auth_and_missing_keys() -> None:
    s = Settings(env="production", database_url="postgresql://x", dev_auth_enabled=True, dev_auth_secret="x" * 32)
    with pytest.raises(ConfigurationError):
        s.validate_for_startup()
    s2 = Settings(env="production", database_url="postgresql://x")
    with pytest.raises(ConfigurationError):
        s2.validate_for_startup()  # audit key required


def test_llm_provider_requires_endpoint() -> None:
    with pytest.raises(ConfigurationError):
        Settings(database_url="postgresql://x", generator_provider="openai_compat").validate_for_startup()


def test_settings_from_env_parses_and_never_needs_network() -> None:
    s = Settings.from_env(
        {
            "GSM_DATABASE_URL": "postgresql://x",
            "GSM_TOP_K": "5",
            "GSM_DEV_AUTH_ENABLED": "true",
            "GSM_DEV_AUTH_SECRET": "0123456789abcdef0123",
            "GSM_LLM_EXTRA_BODY": '{"chat_template_kwargs": {"enable_thinking": false}}',
        }
    )
    s.validate_for_startup()
    assert s.top_k == 5 and s.dev_auth_enabled
    assert s.llm_extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "0123456789abcdef0123" not in repr(s)
