"""Pruebas de punta a punta contra la plataforma completa en Docker (ver tests/e2e/compose.e2e.yml).

Cada prueba hace lo mismo que haría la plataforma real: arma el webhook con su formato, lo firma
con el secreto del canal y lo manda a nginx. Después espera la respuesta en la bandeja del
simulador de plataformas. Cada prueba usa remitentes nuevos para no depender de las demás.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest

from iamia.security.signatures import meta_signature, twilio_signature, x_signature

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:18080")
PLATFORMS_URL = os.environ.get("E2E_PLATFORMS_URL", "http://localhost:9000")
AI_URL = os.environ.get("E2E_AI_URL", "http://localhost:9001")
KEY_DIR = os.environ.get("E2E_KEY_DIR", "/keys")
READY_QUESTION = "¿Cómo solicito y activo el DNI en el celular?"


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.fail(f"falta la variable {name}: correr las pruebas con scripts/e2e.sh")
    return value


def wait_for[T](check: Callable[[], T | None], *, timeout: float = 30.0, interval: float = 0.3, what: str = "") -> T:
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"no se cumplió en {timeout:.0f} s: {what}")
        time.sleep(interval)


def unique_number() -> int:
    return 10**8 + secrets.randbelow(9 * 10**8)


def fold(text: str) -> str:
    """Sin tildes: el SMS translitera a GSM-7 ("Según" llega como "Segun")."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# ----------------------------------------------------------------------------- simuladores
class Platforms:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def _get(self, **params: str) -> dict[str, Any]:
        response = self._client.get(f"{PLATFORMS_URL}/_outbox", params=params)
        response.raise_for_status()
        return response.json()

    def outbox(self, channel: str, recipient: str) -> list[dict[str, Any]]:
        return self._get(channel=channel, recipient=recipient)["items"]

    def attempts(self, channel: str) -> int:
        return int(self._get()["attempts"].get(channel, 0))

    def replies(self, channel: str, recipient: str, count: int = 1, timeout: float = 30.0) -> list[dict[str, Any]]:
        def check() -> list[dict[str, Any]] | None:
            items = self.outbox(channel, recipient)
            return items if len(items) >= count else None

        return wait_for(check, timeout=timeout, what=f"{count} respuesta(s) por {channel} a {recipient}")

    def fault(self, channel: str, status: int, count: int = 1, **extra: Any) -> None:
        payload = {"channel": channel, "status": status, "count": count, **extra}
        self._client.post(f"{PLATFORMS_URL}/_faults", json=payload).raise_for_status()

    def clear_faults(self) -> None:
        self._client.delete(f"{PLATFORMS_URL}/_faults").raise_for_status()

    def spans(self, trace_id: str) -> list[dict[str, Any]]:
        """Spans que la plataforma exportó por OTLP al simulador para esa traza."""
        response = self._client.get(f"{PLATFORMS_URL}/_traces", params={"trace_id": trace_id})
        response.raise_for_status()
        return response.json()["spans"]


class MockAI:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def control(self, **values: Any) -> None:
        self._client.post(f"{AI_URL}/_control", json=values).raise_for_status()

    def stats(self) -> dict[str, Any]:
        return self._client.get(f"{AI_URL}/_stats").json()


# ----------------------------------------------------------------------------- canales
class Channels:
    """Envía webhooks firmados como cada plataforma."""

    def __init__(self, client: httpx.Client) -> None:
        self.http = client

    def telegram(
        self,
        chat_id: int,
        text: str,
        *,
        update_id: int | None = None,
        secret: str | None = None,
        request_id: str | None = None,
    ) -> httpx.Response:
        update = {
            "update_id": update_id or unique_number(),
            "message": {
                "message_id": unique_number(),
                "from": {"id": chat_id, "is_bot": False, "first_name": "Prueba", "language_code": "es"},
                "chat": {"id": chat_id, "type": "private", "first_name": "Prueba"},
                "date": int(time.time()),
                "text": text,
            },
        }
        headers = {"X-Telegram-Bot-Api-Secret-Token": secret or env("TELEGRAM_WEBHOOK_SECRET")}
        if request_id:
            headers["X-Request-ID"] = request_id
        return self.http.post("/webhooks/telegram", json=update, headers=headers)

    def _meta(self, channel: str, payload: dict[str, Any], secret: str) -> httpx.Response:
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": meta_signature(secret, body)}
        return self.http.post(f"/webhooks/{channel}", content=body, headers=headers)

    def whatsapp(self, phone: str, text: str, *, secret: str | None = None) -> httpx.Response:
        value = {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "5491100000000", "phone_number_id": env("WHATSAPP_PHONE_NUMBER_ID")},
            "contacts": [{"profile": {"name": "Prueba"}, "wa_id": phone}],
            "messages": [
                {
                    "from": phone,
                    "id": f"wamid.E2E{uuid.uuid4().hex}",
                    "timestamp": str(int(time.time())),
                    "type": "text",
                    "text": {"body": text},
                }
            ],
        }
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "WABA-E2E", "changes": [{"field": "messages", "value": value}]}],
        }
        return self._meta("whatsapp", payload, secret or env("WHATSAPP_APP_SECRET"))

    def _messaging(
        self, channel: str, webhook_object: str, account: str, sender: str, text: str, secret: str
    ) -> httpx.Response:
        now_ms = int(time.time() * 1000)
        event = {
            "sender": {"id": sender},
            "recipient": {"id": account},
            "timestamp": now_ms,
            "message": {"mid": f"m_E2E{uuid.uuid4().hex}", "text": text},
        }
        payload = {"object": webhook_object, "entry": [{"id": account, "time": now_ms, "messaging": [event]}]}
        return self._meta(channel, payload, secret)

    def messenger(self, psid: str, text: str) -> httpx.Response:
        return self._messaging("messenger", "page", env("MESSENGER_PAGE_ID"), psid, text, env("MESSENGER_APP_SECRET"))

    def instagram(self, igsid: str, text: str) -> httpx.Response:
        account = env("INSTAGRAM_ACCOUNT_ID")
        return self._messaging("instagram", "instagram", account, igsid, text, env("INSTAGRAM_APP_SECRET"))

    def x(self, user_id: str, text: str) -> httpx.Response:
        account = env("X_ACCOUNT_ID")
        created = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        event = {
            "id": str(unique_number()),
            "event_type": "MessageCreate",
            "text": text,
            "sender_id": user_id,
            "dm_conversation_id": f"{account}-{user_id}",
            "created_at": created,
        }
        payload = {
            "data": {
                "event_type": "dm.received",
                "filter": {"user_id": account},
                "tag": "iamia-dm",
                "payload": {"direct_message_events": [event]},
            }
        }
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Twitter-Webhooks-Signature": x_signature(env("X_WEBHOOK_SECRET"), body),
        }
        return self.http.post("/webhooks/x", content=body, headers=headers)

    def sms(self, phone: str, text: str) -> httpx.Response:
        params = {
            "MessageSid": f"SM{uuid.uuid4().hex}",
            "AccountSid": env("TWILIO_ACCOUNT_SID"),
            "From": phone,
            "To": env("TWILIO_FROM_NUMBER"),
            "Body": text,
            "NumMedia": "0",
            "SmsStatus": "received",
        }
        # Twilio firma la URL pública configurada en la consola más los parámetros del POST.
        signature = twilio_signature(env("TWILIO_AUTH_TOKEN"), env("SMS_WEBHOOK_URL"), params.items())
        headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature}
        return self.http.post("/webhooks/sms", content=urlencode(params), headers=headers)

    def email(
        self, sender: str, text: str, subject: str = "Consulta sobre un trámite", *, timestamp: int | None = None
    ) -> httpx.Response:
        message = EmailMessage()
        message["From"] = f"Persona de prueba <{sender}>"
        message["To"] = env("EMAIL_FROM_ADDRESS")
        message["Subject"] = subject
        message["Date"] = format_datetime(datetime.now(UTC))
        message["Message-ID"] = f"<{uuid.uuid4().hex}@e2e.test>"
        message.set_content(text)
        body = message.as_bytes()
        timestamp = timestamp or int(time.time())
        digest = hmac.new(env("EMAIL_INBOUND_SECRET").encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
        headers = {
            "Content-Type": "message/rfc822",
            "X-IAMIA-Timestamp": str(timestamp),
            "X-IAMIA-Signature": f"v1={digest.hexdigest()}",
        }
        return self.http.post("/webhooks/email", content=body, headers=headers)


class Webchat:
    def __init__(self, client: httpx.Client) -> None:
        self.http = client

    def session(self) -> str:
        response = self.http.post("/webchat/sessions")
        response.raise_for_status()
        return response.json()["token"]

    def send(self, token: str, text: str) -> httpx.Response:
        return self.http.post("/webchat/messages", json={"text": text}, headers={"Authorization": f"Bearer {token}"})

    def poll(self, token: str, after: int = 0) -> list[dict[str, Any]]:
        response = self.http.get(
            "/webchat/messages", params={"after": after}, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
        response.raise_for_status()
        return response.json()["messages"]


class Console:
    """API de la consola de operadores con tokens de desarrollo firmados con la clave local."""

    def __init__(self, client: httpx.Client, role: str, subject: str) -> None:
        self.http = client
        command = [
            sys.executable,
            "-m",
            "iamia",
            "dev-token",
            "--role",
            role,
            "--subject",
            subject,
            "--key-dir",
            KEY_DIR,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True)  # noqa: S603
        self.headers = {"Authorization": f"Bearer {result.stdout.strip().splitlines()[-1]}"}

    def get(self, path: str, **params: Any) -> httpx.Response:
        return self.http.get(f"/admin/api{path}", params=params, headers=self.headers)

    def post(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return self.http.post(f"/admin/api{path}", json=body, headers=self.headers)

    def delete(self, path: str) -> httpx.Response:
        return self.http.delete(f"/admin/api{path}", headers=self.headers)

    def find(self, channel: str, address: str, state: str) -> dict[str, Any]:
        """Busca la conversación como lo haría un operador: canal, estado y contacto enmascarado."""
        expected = masked(address)

        def check() -> dict[str, Any] | None:
            items = self.get("/conversations", state=state).json()["items"]
            return next((i for i in items if i["channel"] == channel and i["contact"] == expected), None)

        return wait_for(check, timeout=20, what=f"conversación {state} de {channel} {expected}")


def masked(address: str) -> str:
    """Mismo formato que muestra la consola (security/pii.py: mask_address)."""
    if "@" in address:
        user, _, domain = address.partition("@")
        return f"{user[:1]}***@{domain}"
    return "****" if len(address) <= 4 else "*" * (len(address) - 4) + address[-4:]


# ----------------------------------------------------------------------------- fixtures
def _wait_until_ready(client: httpx.Client) -> None:
    def ready() -> bool:
        try:
            if client.get("/health/ready").status_code != 200:
                return False
            # La base de conocimiento ya está indexada cuando la consulta directa cita una fuente.
            answer = client.post("/v1/chat", json={"question": READY_QUESTION})
            return answer.status_code == 200 and bool(answer.json().get("sources"))
        except httpx.HTTPError:
            return False

    wait_for(ready, timeout=240, interval=2.0, what="plataforma lista con la base de conocimiento indexada")


@pytest.fixture(scope="session")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        _wait_until_ready(client)
        yield client


@pytest.fixture(scope="session")
def tools() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=10) as client:
        yield client


@pytest.fixture
def platforms(tools: httpx.Client) -> Iterator[Platforms]:
    simulator = Platforms(tools)
    yield simulator
    simulator.clear_faults()


@pytest.fixture
def ai(tools: httpx.Client) -> Iterator[MockAI]:
    mock = MockAI(tools)
    yield mock
    mock.control(chat_failures=0, embedding_failures=0, chat_delay=0)


@pytest.fixture
def channels(http: httpx.Client) -> Channels:
    return Channels(http)


@pytest.fixture
def webchat(http: httpx.Client) -> Webchat:
    return Webchat(http)


@pytest.fixture
def console(http: httpx.Client) -> Callable[[str, str], Console]:
    def make(role: str, subject: str) -> Console:
        return Console(http, role, subject)

    return make
