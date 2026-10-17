"""Shared fixtures.

Database tests need ``GSM_TEST_DATABASE_URL`` pointing at a PostgreSQL 16 +
pgvector database the tests may wipe (e.g. from ``scripts/local_pg.sh`` or
``docker compose up db``). The schema is rebuilt once per session through the
real Alembic migrations, and every test starts from empty tables. The service
connects through the non-superuser ``gsm_app`` role so row-level security is
active during tests exactly as in deployment.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from gsm.auth import TrustedAuthenticator
from gsm.clock import FrozenClock
from gsm.config import Settings
from gsm.db.database import Database
from gsm.service import GovernedMemoryService, Providers
from tests.helpers import T0, World, build_world

ROOT = Path(__file__).resolve().parents[1]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "db_url" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.db)


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("GSM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("GSM_TEST_DATABASE_URL is not set; database tests skipped")
    return url


@pytest.fixture(scope="session")
def owner_engine(db_url: str) -> Iterator[Engine]:
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = db_url
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_db(db_url: str, owner_engine: Engine) -> Iterator[Database]:
    db = Database(db_url, app_role="gsm_app")
    yield db
    db.dispose()


@pytest.fixture(autouse=True)
def _clean(request: pytest.FixtureRequest) -> Iterator[None]:
    yield
    if "owner_engine" in request.fixturenames:
        engine: Engine = request.getfixturevalue("owner_engine")
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE tenants CASCADE"))


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def settings(db_url: str) -> Settings:
    return Settings(env="test", database_url=db_url, cascade_mode="inline", cache_ttl_seconds=60)


@pytest.fixture
def providers() -> Providers:
    return Providers()


@pytest.fixture
def service(app_db: Database, settings: Settings, clock: FrozenClock, providers: Providers) -> GovernedMemoryService:
    return GovernedMemoryService(app_db, settings, clock=clock, providers=providers)


@pytest.fixture
def auth(service: GovernedMemoryService) -> TrustedAuthenticator:
    return TrustedAuthenticator(service)


@pytest.fixture
def world(service: GovernedMemoryService, auth: TrustedAuthenticator, clock: FrozenClock) -> World:
    return build_world(service, auth, clock)
