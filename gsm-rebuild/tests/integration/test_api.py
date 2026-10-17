"""HTTP API: auth, admin separation, and the governed flow over the wire."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gsm.api.app import create_app
from gsm.auth import DevHmacAuthenticator
from gsm.clock import FrozenClock
from gsm.config import Settings
from gsm.db.database import Database
from gsm.errors import ConfigurationError
from gsm.service import GovernedMemoryService

SECRET = b"dev-secret-for-tests-only-0123456789"
ADMIN = "admin-key-for-tests-0123456789"


@pytest.fixture
def api(app_db: Database, settings: Settings, clock: FrozenClock) -> tuple[TestClient, GovernedMemoryService]:
    s = settings.model_copy(
        update={
            "dev_auth_enabled": True,
            "dev_auth_secret": SecretStr(SECRET.decode()),
            "admin_api_key": SecretStr(ADMIN),
        }
    )
    svc = GovernedMemoryService(app_db, s, clock=clock)
    return TestClient(create_app(s, service=svc)), svc


def token(clock: FrozenClock, principal: str, **extra: object) -> dict[str, str]:
    now = clock.now().timestamp()
    claims = {
        "tenant_id": "acme",
        "workspace_id": "default",
        "principal_id": principal,
        "auth_strength": "password",
        "auth_time": now - 5,
        "exp": now + 600,
        **extra,
    }
    return {"X-GSM-Context": DevHmacAuthenticator.mint(SECRET, claims)}


def seed(client: TestClient) -> None:
    h = {"X-GSM-Admin-Key": ADMIN}
    assert (
        client.post("/admin/tenants", json={"tenant_id": "acme", "display_name": "Acme"}, headers=h).status_code == 201
    )
    for pid, roles in (("alice", ["engineer"]), ("bob", ["engineer"]), ("carol", ["manager"])):
        r = client.post(
            "/admin/tenants/acme/principals", json={"principal_id": pid, "display_name": pid, "roles": roles}, headers=h
        )
        assert r.status_code == 201
    for pid in ("alice", "bob"):
        client.post(
            "/admin/tenants/acme/relationships",
            headers=h,
            json={
                "subject_principal_id": pid,
                "relation": "member_of",
                "object_type": "project",
                "object_id": "apollo",
            },
        )


DECISION = {
    "event_type": "decision.recorded",
    "operations": [
        {
            "op": "ADD_FACT",
            "fact": {
                "semantic_type": "decision",
                "content": {"text": "Launch Apollo on October 14"},
                "topic": "Apollo launch",
                "subject_ids": ["project:apollo"],
                "policy_template": {"name": "project", "project_id": "apollo"},
            },
        }
    ],
}


def test_health(api: tuple[TestClient, GovernedMemoryService]) -> None:
    client, _ = api
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"database": "ok"}


def test_admin_routes_require_admin_key(api: tuple[TestClient, GovernedMemoryService], clock: FrozenClock) -> None:
    client, _ = api
    assert client.post("/admin/tenants", json={"tenant_id": "x", "display_name": "x"}).status_code == 403
    seed(client)
    # an agent token is not an admin credential
    r = client.post("/admin/tenants", json={"tenant_id": "y", "display_name": "y"}, headers=token(clock, "alice"))
    assert r.status_code == 403


def test_governed_flow_over_http(api: tuple[TestClient, GovernedMemoryService], clock: FrozenClock) -> None:
    client, _ = api
    seed(client)
    r = client.post("/v1/events", json=DECISION, headers=token(clock, "alice"))
    assert r.status_code == 200 and r.json()["status"] == "committed"
    mid = r.json()["results"][0]["memory_id"]
    bob = client.post("/v1/query", json={"query": "Apollo launch"}, headers=token(clock, "bob")).json()
    assert bob["action"] == "ANSWER" and "October 14" in bob["answer"]
    carol = client.post("/v1/query", json={"query": "Apollo launch"}, headers=token(clock, "carol")).json()
    assert carol["action"] == "NO_MEMORY"
    missing = client.get("/v1/memories/mem_" + "0" * 32, headers=token(clock, "carol"))
    hidden = client.get(f"/v1/memories/{mid}", headers=token(clock, "carol"))
    assert hidden.status_code == missing.status_code == 404 and hidden.json() == missing.json()
    f = client.post("/v1/forget", json={"memory_ids": [mid]}, headers=token(clock, "alice")).json()
    assert f["cascade_status"] == "completed"
    receipt = client.get(f"/v1/receipts/{f['receipt_id']}", headers=token(clock, "alice")).json()
    assert receipt["memory_ids"] == [mid] and "October" not in str(receipt)
    assert (
        client.post("/v1/query", json={"query": "Apollo launch"}, headers=token(clock, "bob")).json()["action"]
        == "NO_MEMORY"
    )


def test_token_roles_can_only_narrow(api: tuple[TestClient, GovernedMemoryService], clock: FrozenClock) -> None:
    client, svc = api
    seed(client)
    ctx = DevHmacAuthenticator(svc, SECRET).authenticate(
        {k.lower(): v for k, v in token(clock, "carol", roles=["manager", "hr", "admin"]).items()}
    )
    assert ctx.roles == frozenset({"manager"})


def test_bad_tokens_fail_closed(api: tuple[TestClient, GovernedMemoryService], clock: FrozenClock) -> None:
    client, _ = api
    seed(client)
    good = token(clock, "bob")["X-GSM-Context"]
    payload, sig = good.split(".")
    forged = DevHmacAuthenticator.mint(
        b"attacker-secret-0123456789abcdef",
        {"tenant_id": "acme", "principal_id": "bob", "auth_time": 0, "exp": time.time() + 99},
    )
    now = clock.now().timestamp()
    for headers in (
        {},
        {"X-GSM-Context": f"{payload}.{sig[:-2]}AA"},
        {"X-GSM-Context": forged},
        token(clock, "bob", exp=now - 1),
        token(clock, "nobody"),
        {"X-GSM-Context": "not-a-token"},
    ):
        r = client.post("/v1/query", json={"query": "x"}, headers=headers)
        assert r.status_code == 401, headers


def test_validation_errors_do_not_echo_input(api: tuple[TestClient, GovernedMemoryService], clock: FrozenClock) -> None:
    client, _ = api
    seed(client)
    bad = {
        "event_type": "x",
        "operations": [
            {
                "op": "ADD_FACT",
                "fact": {
                    "semantic_type": "decision",
                    "content": {"text": "TOP-SECRET-PAYLOAD"},
                    "topic": "t",
                    "unexpected": "TOP-SECRET-PAYLOAD",
                },
            }
        ],
    }
    r = client.post("/v1/events", json=bad, headers=token(clock, "alice"))
    assert r.status_code == 422 and "TOP-SECRET-PAYLOAD" not in r.text


def test_production_refuses_dev_auth(app_db: Database, settings: Settings) -> None:
    s = settings.model_copy(
        update={
            "env": "production",
            "dev_auth_enabled": True,
            "dev_auth_secret": SecretStr(SECRET.decode()),
            "audit_hmac_key": SecretStr("k" * 32),
        }
    )
    with pytest.raises(ConfigurationError):
        create_app(s, service=GovernedMemoryService(app_db, s))


def test_no_authenticator_means_no_access(app_db: Database, settings: Settings) -> None:
    client = TestClient(create_app(settings, service=GovernedMemoryService(app_db, settings)))
    assert client.post("/v1/query", json={"query": "x"}).status_code == 401
    assert client.post("/admin/tenants", json={"tenant_id": "x", "display_name": "x"}).status_code == 404
