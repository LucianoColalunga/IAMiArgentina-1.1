"""Configuración: límites por canal y controles de seguridad al arrancar."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from iamia.bootstrap import Container, check_security
from iamia.security.crypto import generate_key
from iamia.settings import Settings, parse_rate_limits


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_parse_rate_limits() -> None:
    parsed = parse_rate_limits("x=15/900;1440/86400, whatsapp=80 ,sms=10/1,roto=abc,vacio=")
    assert parsed == {"x": ((15, 900), (1440, 86400)), "whatsapp": ((80, 1),), "sms": ((10, 1),)}


def test_default_channel_limits_follow_each_platform() -> None:
    limits = _settings().channel_rate_limit_map
    assert limits["x"] == ((15, 900), (1440, 86400))
    assert limits["whatsapp"] == ((80, 1),) and limits["telegram"] == ((25, 1),)


def test_insecure_configuration_is_reported() -> None:
    problems = " | ".join(_settings(app_env="production", channels_enabled="webchat,telegram").security_problems())
    assert "DATA_ENCRYPTION_KEYS" in problems and "PSEUDONYMIZATION_KEY" in problems
    assert "DATABASE_URL usa la contraseña por defecto" in problems
    assert "OIDC_JWKS_URL" in problems
    assert "el canal telegram está habilitado sin TELEGRAM_BOT_TOKEN" in problems
    assert "el canal webchat está habilitado sin WEBCHAT_TOKEN_SECRET" in problems


def test_compose_default_passwords_are_reported() -> None:
    problems = _settings(
        database_url="postgresql://iamia:iamia-local@postgres:5432/iamia",
        redis_url="redis://:iamia-local@redis:6379/0",
        amqp_url="amqp://iamia:iamia-local@rabbitmq:5672/iamia",
    ).security_problems()
    for name in ("DATABASE_URL", "REDIS_URL", "AMQP_URL"):
        assert f"{name} usa la contraseña por defecto" in problems


def test_complete_configuration_has_no_problems() -> None:
    settings = _settings(
        data_encryption_keys=f"k1:{generate_key()}",
        pseudonymization_key=generate_key(),
        database_url="postgresql://iamia:otra-clave@db/iamia",
        amqp_url="amqp://iamia:otra-clave@mq/iamia",
        oidc_public_key_path="/run/secrets/oidc.pem",
        channels_enabled="webchat",
        webchat_token_secret="s" * 40,
    )
    assert settings.security_problems() == []


def test_x_static_mode_requires_its_token() -> None:
    problems = _settings(channels_enabled="x", x_token_mode="static").security_problems()
    assert any("X_USER_ACCESS_TOKEN" in problem for problem in problems)


def test_check_security_blocks_only_production() -> None:
    with pytest.raises(RuntimeError):
        check_security(_settings(app_env="production"))
    check_security(_settings(app_env="development"))  # en desarrollo solo se avisa


def test_missing_oidc_key_disables_the_console_without_stopping_the_api(tmp_path: Path) -> None:
    settings = _settings(oidc_public_key_path=str(tmp_path / "no-existe.pem"))
    assert Container.token_verifier(SimpleNamespace(settings=settings)) is None  # type: ignore[arg-type]
    assert Container.token_verifier(SimpleNamespace(settings=_settings())) is None  # type: ignore[arg-type]
