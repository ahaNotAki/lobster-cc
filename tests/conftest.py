"""Shared test fixtures."""

import pytest

from remote_control.config import (
    AgentConfig,
    AppConfig,
    NotificationsConfig,
    ServerConfig,
    StorageConfig,
    WeComConfig,
)
from remote_control.core.store import ScopedStore, Store


TEST_AES_KEY = "kWxPEV2UEDyxWpmPB8jfIqLfNjGjRiIpG2lMGKEQCTm"


@pytest.fixture
def wecom_config():
    return WeComConfig(
        corp_id="test_corp",
        agent_id=1000002,
        secret="test_secret",
        token="test_token",
        encoding_aes_key=TEST_AES_KEY,
        mode="callback",
    )


@pytest.fixture
def app_config(tmp_path, wecom_config):
    return AppConfig(
        wecom=[wecom_config],
        agent=AgentConfig(default_working_dir=str(tmp_path)),
        server=ServerConfig(),
        storage=StorageConfig(db_path=str(tmp_path / "test.db")),
        notifications=NotificationsConfig(progress_interval_seconds=1),
    )


@pytest.fixture
def relay_config(tmp_path):
    return AppConfig(
        wecom=[WeComConfig(
            corp_id="test_corp",
            agent_id=1000002,
            secret="test_secret",
            token="test_token",
            encoding_aes_key=TEST_AES_KEY,
            mode="relay",
            relay_url="http://localhost:9999",
            relay_poll_interval_seconds=1.0,
        )],
        agent=AgentConfig(default_working_dir=str(tmp_path)),
        server=ServerConfig(),
        storage=StorageConfig(db_path=str(tmp_path / "test.db")),
        notifications=NotificationsConfig(progress_interval_seconds=1),
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.open()
    yield ScopedStore(s, "test_agent")
    s.close()


@pytest.fixture
def raw_store(tmp_path):
    """Unscoped Store for tests that need global access."""
    s = Store(tmp_path / "test.db")
    s.open()
    yield s
    s.close()
