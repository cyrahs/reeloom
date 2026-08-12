from __future__ import annotations

import pytest

from reeloom.config import ConfigError, Settings

BASE = {
    "REELOOM_DATABASE_URL": "postgresql:///reeloom",
    "REELOOM_ADMIN_TOKEN": "0123456789abcdef",
}


def env(**overrides: str) -> dict[str, str]:
    return BASE | overrides


def test_listen_defaults() -> None:
    settings = Settings.from_env(env())
    assert (settings.host, settings.port) == ("0.0.0.0", 8080)


def test_listen_pair_is_read_from_the_listen_names() -> None:
    settings = Settings.from_env(
        env(REELOOM_LISTEN_HOST="127.0.0.1", REELOOM_LISTEN_PORT="9000")
    )
    assert (settings.host, settings.port) == ("127.0.0.1", 9000)


def test_old_listen_names_are_no_longer_read() -> None:
    settings = Settings.from_env(
        env(REELOOM_HOST="127.0.0.1", REELOOM_PORT="9000")
    )
    assert (settings.host, settings.port) == ("0.0.0.0", 8080)


def test_service_link_injection_does_not_reach_the_listen_port() -> None:
    """A `reeloom` Service injects these into every container in the namespace."""

    settings = Settings.from_env(
        env(
            REELOOM_PORT="tcp://10.43.234.53:80",
            REELOOM_SERVICE_HOST="10.43.234.53",
            REELOOM_SERVICE_PORT="80",
            REELOOM_PORT_80_TCP="tcp://10.43.234.53:80",
        )
    )
    assert settings.port == 8080


def test_an_injected_value_landing_on_an_int_key_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings.from_env(
        env(REELOOM_SCAN_INTERVAL_SECONDS="tcp://10.43.234.53:80")
    )
    assert settings.scan_interval_seconds == 30
    assert "REELOOM_SCAN_INTERVAL_SECONDS" in caplog.text


def test_public_url_defaults_to_empty() -> None:
    assert Settings.from_env(env()).public_url == ""


def test_public_url_drops_the_trailing_slash() -> None:
    settings = Settings.from_env(
        env(REELOOM_PUBLIC_URL="https://reeloom.example/")
    )
    assert settings.public_url == "https://reeloom.example"


def test_a_mistyped_integer_still_fails() -> None:
    with pytest.raises(ConfigError) as error:
        Settings.from_env(env(REELOOM_LISTEN_PORT="808O"))
    assert error.value.code == "invalid_integer"


def test_an_out_of_range_port_still_fails() -> None:
    with pytest.raises(ConfigError) as error:
        Settings.from_env(env(REELOOM_LISTEN_PORT="70000"))
    assert error.value.code == "out_of_range"
