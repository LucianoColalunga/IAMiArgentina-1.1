"""Línea de comandos: generación de secretos, tokens de desarrollo, revisión de configuración e ingesta."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from iamia.cli import main
from iamia.security.auth import JwtVerifier, OidcConfig
from iamia.security.crypto import FieldCipher, Pseudonymizer
from iamia.security.rbac import Role
from iamia.security.session_tokens import SessionTokens
from iamia.settings import get_settings


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Cada prueba arma su propia configuración desde variables de entorno."""
    for name in ("APP_ENV", "OIDC_ISSUER", "OIDC_ROLES_CLAIM", "CHANNELS_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _values(output: str) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in output.splitlines() if "=" in line and not line.startswith("#"))
    return {name: value for name, value in pairs}


def test_gen_secrets_produces_usable_keys(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gen-secrets"]) == 0
    values = _values(capsys.readouterr().out)
    cipher = FieldCipher.from_config(values["DATA_ENCRYPTION_KEYS"])
    assert cipher.decrypt(cipher.encrypt("dato")) == "dato"
    Pseudonymizer.from_config(values["PSEUDONYMIZATION_KEY"])
    SessionTokens(values["WEBCHAT_TOKEN_SECRET"], 60)
    assert len(set(values.values())) == len(values)  # ningún secreto se repite
    assert main(["gen-secrets"]) == 0
    assert _values(capsys.readouterr().out)["POSTGRES_PASSWORD"] != values["POSTGRES_PASSWORD"]


def test_dev_token_is_valid_for_the_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    assert main(["dev-token", "--role", "supervisor", "--subject", "ana", "--key-dir", str(tmp_path)]) == 0
    token = capsys.readouterr().out.strip()
    public = (tmp_path / "oidc-dev-public.pem").read_text(encoding="utf-8")
    verifier = JwtVerifier(OidcConfig(audience="iamia-admin", algorithms=["RS256"], public_key_pem=public))
    principal = verifier.verify(token)
    assert principal.subject == "ana" and principal.roles == frozenset({Role.SUPERVISOR})
    # Una segunda llamada reutiliza la misma clave.
    assert main(["dev-token", "--key-dir", str(tmp_path)]) == 0
    assert verifier.verify(capsys.readouterr().out.strip()).roles == frozenset({Role.OPERATOR})


def test_dev_token_is_refused_in_production_and_for_unknown_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    assert main(["dev-token", "--key-dir", str(tmp_path)]) == 2
    assert not (tmp_path / "oidc-dev-private.pem").exists()
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    assert main(["dev-token", "--role", "superusuario", "--key-dir", str(tmp_path)]) == 2


def test_check_config_fails_in_production(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    assert main(["check-config"]) == 1
    output = capsys.readouterr().out
    assert "entorno: production" in output and "DATA_ENCRYPTION_KEYS" in output
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    assert main(["check-config"]) == 0  # en desarrollo solo informa


def test_ingest_dry_run_needs_no_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    faqs = [
        {
            "pregunta": "¿Cómo saco el DNI?",
            "respuesta": "Se tramita con turno previo en el Registro Civil.",
            "url": "https://www.argentina.gob.ar/dni",
        }
    ]
    (tmp_path / "faqs.json").write_text(json.dumps(faqs), encoding="utf-8")
    code = main(["ingest", "--dry-run", "--faq-dir", str(tmp_path), "--urls-file", str(tmp_path / "urls.txt")])
    assert code == 0
    output = capsys.readouterr().out
    assert "entradas: 1  fragmentos: 1" in output and "Pregunta: ¿Cómo saco el DNI?" in output


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0 and capsys.readouterr().out.strip()
