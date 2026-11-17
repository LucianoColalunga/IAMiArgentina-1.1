"""Simulador de las APIs de salida de cada plataforma, para las pruebas de punta a punta.

Atiende las mismas rutas que usan los adaptadores (Telegram Bot API, Graph API de Meta para
WhatsApp, Messenger e Instagram, API de mensajes directos de X y API de mensajes de Twilio) y
además levanta un servidor SMTP mínimo para el correo. Todo lo que recibe queda en una bandeja
consultable (GET /_outbox) y se pueden programar fallas por canal (POST /_faults): errores 5xx,
429 con espera, rechazos permanentes o credenciales inválidas.

También hace de colector OTLP/HTTP mínimo (POST /v1/traces): guarda los spans que exportan la
API, el worker y el dispatcher para que las pruebas sigan un mensaje de punta a punta
(GET /_traces?trace_id=...) sin levantar Tempo.

    python -m uvicorn platforms:app --app-dir tests/e2e/simulators --port 9000
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import itertools
import os
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

SMTP_PORT = int(os.getenv("SIM_SMTP_PORT", "2525"))
# Tope de spans guardados: el simulador no tiene que crecer sin límite si queda levantado.
MAX_SPANS = int(os.getenv("SIM_MAX_SPANS", "50000"))
WHATSAPP_PHONE_ID = os.getenv("SIM_WHATSAPP_PHONE_ID", "100000000000001")
MESSENGER_PAGE_ID = os.getenv("SIM_MESSENGER_PAGE_ID", "200000000000002")
INSTAGRAM_ACCOUNT_ID = os.getenv("SIM_INSTAGRAM_ACCOUNT_ID", "17841400000000003")

_ids = itertools.count(1)


@dataclass
class Fault:
    status: int
    remaining: int
    retry_after: float | None = None
    body: dict[str, Any] = field(default_factory=dict)


@dataclass
class State:
    outbox: list[dict[str, Any]] = field(default_factory=list)
    typing: list[dict[str, Any]] = field(default_factory=list)
    faults: dict[str, list[Fault]] = field(default_factory=lambda: defaultdict(list))
    attempts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    spans: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_SPANS))

    def reset(self) -> None:
        self.outbox.clear()
        self.typing.clear()
        self.faults.clear()
        self.attempts.clear()
        self.spans.clear()


state = State()


def _record(channel: str, recipient: str, text: str, **extra: Any) -> str:
    provider_id = f"{channel}-{next(_ids)}"
    state.outbox.append(
        {"channel": channel, "recipient": recipient, "text": text, "id": provider_id, "at": time.time(), **extra}
    )
    return provider_id


def _fault(channel: str) -> JSONResponse | None:
    """Si hay una falla programada para el canal, la consume y devuelve la respuesta de error."""
    state.attempts[channel] += 1
    queue = state.faults.get(channel)
    if not queue:
        return None
    fault = queue[0]
    fault.remaining -= 1
    if fault.remaining <= 0:
        queue.pop(0)
    headers = {"Retry-After": str(int(fault.retry_after))} if fault.retry_after is not None else None
    return JSONResponse(fault.body or {"error": "falla simulada"}, status_code=fault.status, headers=headers)


# ----------------------------------------------------------------------------- SMTP mínimo
class SmtpSink:
    """Servidor SMTP de prueba: acepta todo y guarda el mensaje en la bandeja."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._session, "0.0.0.0", self._port)  # noqa: S104

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def reply(line: str) -> None:
            writer.write((line + "\r\n").encode())
            await writer.drain()

        sender, recipients = "", []
        await reply("220 simulador ESMTP")
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                command = raw.decode("utf-8", "replace").strip()
                verb = command.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    writer.write(b"250-simulador\r\n250-8BITMIME\r\n250-SMTPUTF8\r\n250 AUTH PLAIN LOGIN\r\n")
                    await writer.drain()
                elif verb == "AUTH":
                    await reply("235 autenticado")
                elif verb == "MAIL":
                    fault = _fault("email")
                    if fault is not None:
                        await reply("451 falla temporal simulada")
                        continue
                    sender, recipients = command.split(":", 1)[1].strip().strip("<>").split(">")[0], []
                    await reply("250 ok")
                elif verb == "RCPT":
                    recipients.append(command.split(":", 1)[1].strip().strip("<>").split(">")[0])
                    await reply("250 ok")
                elif verb == "DATA":
                    await reply("354 terminar con <CRLF>.<CRLF>")
                    lines: list[bytes] = []
                    while True:
                        line = await reader.readline()
                        if line in (b".\r\n", b".\n", b""):
                            break
                        lines.append(line[1:] if line.startswith(b"..") else line)
                    message = message_from_bytes(b"".join(lines), policy=policy.default)
                    body = message.get_body(preferencelist=("plain",))
                    text = body.get_content() if body is not None else ""
                    _record(
                        "email",
                        ",".join(recipients),
                        str(text).strip(),
                        subject=str(message.get("Subject", "")),
                        sender=sender,
                        in_reply_to=str(message.get("In-Reply-To", "")),
                    )
                    await reply("250 aceptado")
                elif verb == "RSET":
                    sender, recipients = "", []
                    await reply("250 ok")
                elif verb == "NOOP":
                    await reply("250 ok")
                elif verb == "QUIT":
                    await reply("221 chau")
                    break
                else:
                    await reply("502 comando no implementado")
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()


smtp = SmtpSink(SMTP_PORT)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await smtp.start()
    try:
        yield
    finally:
        await smtp.stop()


app = FastAPI(title="Simulador de plataformas", lifespan=lifespan)


# ----------------------------------------------------------------------------- Telegram
@app.post("/bot{token}/{method}")
async def telegram(token: str, method: str, request: Request) -> JSONResponse:
    body = await request.json()
    if method == "sendChatAction":
        state.typing.append({"channel": "telegram", "recipient": str(body.get("chat_id")), "at": time.time()})
        return JSONResponse({"ok": True, "result": True})
    fault = _fault("telegram")
    if fault is not None:
        payload = {"ok": False, "error_code": fault.status_code, "description": "falla simulada"}
        if fault.status_code == 429:
            payload["parameters"] = {"retry_after": int(fault.headers.get("retry-after", "1"))}
        return JSONResponse(payload, status_code=fault.status_code)
    message_id = _record("telegram", str(body.get("chat_id")), str(body.get("text", "")))
    return JSONResponse({"ok": True, "result": {"message_id": int(message_id.rsplit("-", 1)[1])}})


# ----------------------------------------------------------------------------- Meta (Graph API)
@app.post("/{version}/{account_id}/messages")
async def graph(version: str, account_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    channel = {
        WHATSAPP_PHONE_ID: "whatsapp",
        MESSENGER_PAGE_ID: "messenger",
        INSTAGRAM_ACCOUNT_ID: "instagram",
    }.get(account_id, "meta-desconocido")
    if channel == "whatsapp" and body.get("status") == "read":
        state.typing.append({"channel": "whatsapp", "recipient": body.get("message_id"), "at": time.time()})
        return JSONResponse({"success": True})
    if channel != "whatsapp" and body.get("sender_action"):
        recipient = (body.get("recipient") or {}).get("id", "")
        state.typing.append({"channel": channel, "recipient": recipient, "at": time.time()})
        return JSONResponse({"recipient_id": recipient})
    fault = _fault(channel)
    if fault is not None:
        return fault
    if channel == "whatsapp":
        recipient = str(body.get("to") or body.get("recipient") or "")
        text = (body.get("text") or {}).get("body", "")
        if body.get("type") == "template":
            parameters = body["template"]["components"][0]["parameters"]
            text = parameters[0]["text"]
        provider_id = _record("whatsapp", recipient, text, kind=body.get("type"))
        return JSONResponse({"messaging_product": "whatsapp", "messages": [{"id": f"wamid.{provider_id}"}]})
    recipient = str((body.get("recipient") or {}).get("id", ""))
    provider_id = _record(channel, recipient, (body.get("message") or {}).get("text", ""), tag=body.get("tag"))
    return JSONResponse({"recipient_id": recipient, "message_id": f"m_{provider_id}"})


# ----------------------------------------------------------------------------- X
@app.post("/2/dm_conversations/with/{participant_id}/messages")
async def x_dm(participant_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    fault = _fault("x")
    if fault is not None:
        return fault
    provider_id = _record("x", participant_id, str(body.get("text", "")))
    return JSONResponse(
        {"data": {"dm_conversation_id": f"conv-{participant_id}", "dm_event_id": provider_id}}, status_code=201
    )


# ----------------------------------------------------------------------------- Twilio
@app.post("/2010-04-01/Accounts/{account_sid}/Messages.json")
async def twilio(account_sid: str, request: Request) -> JSONResponse:
    form = dict(parse_qsl((await request.body()).decode("utf-8"), keep_blank_values=True))
    fault = _fault("sms")
    if fault is not None:
        return fault
    provider_id = _record("sms", form.get("To", ""), form.get("Body", ""))
    return JSONResponse({"sid": f"SM{provider_id}", "status": "queued"}, status_code=201)


# ----------------------------------------------------------------------------- colector OTLP
def _value(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    return getattr(value, kind) if kind in ("string_value", "bool_value", "int_value", "double_value") else None


@app.post("/v1/traces")
async def otlp_traces(request: Request) -> Response:
    """Recibe spans en protobuf por OTLP/HTTP, como el colector de compose.observability.yml."""
    body = await request.body()
    if request.headers.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
    export = ExportTraceServiceRequest.FromString(body)
    for resource_spans in export.resource_spans:
        resource = {item.key: _value(item.value) for item in resource_spans.resource.attributes}
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                state.spans.append(
                    {
                        "service": resource.get("service.name", ""),
                        "name": span.name,
                        "trace_id": span.trace_id.hex(),
                        "span_id": span.span_id.hex(),
                        "parent_span_id": span.parent_span_id.hex(),
                        "kind": Span.SpanKind.Name(span.kind),
                        "status": Status.StatusCode.Name(span.status.code),
                        "attributes": {item.key: _value(item.value) for item in span.attributes},
                    }
                )
    return Response(ExportTraceServiceResponse().SerializeToString(), media_type="application/x-protobuf")


# ----------------------------------------------------------------------------- control de pruebas
@app.get("/_outbox")
async def outbox(channel: str | None = None, recipient: str | None = None, since: float = 0.0) -> dict[str, Any]:
    items = [
        item
        for item in state.outbox
        if (channel is None or item["channel"] == channel)
        and (recipient is None or item["recipient"] == recipient)
        and item["at"] >= since
    ]
    return {"items": items, "typing": state.typing, "attempts": state.attempts}


@app.get("/_traces")
async def traces(trace_id: str) -> dict[str, Any]:
    return {"spans": [span for span in state.spans if span["trace_id"] == trace_id]}


@app.post("/_faults")
async def program_fault(request: Request) -> dict[str, bool]:
    body = await request.json()
    fault = Fault(
        status=int(body["status"]),
        remaining=int(body.get("count", 1)),
        retry_after=body.get("retry_after"),
        body=body.get("body") or {},
    )
    state.faults[str(body["channel"])].append(fault)
    return {"ok": True}


@app.delete("/_faults")
async def clear_faults() -> dict[str, bool]:
    state.faults.clear()
    return {"ok": True}


@app.post("/_reset")
async def reset() -> dict[str, bool]:
    state.reset()
    return {"ok": True}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
