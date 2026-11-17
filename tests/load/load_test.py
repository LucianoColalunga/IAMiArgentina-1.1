"""Carga sostenida y escalonada sobre la plataforma completa, con webhooks firmados de 4 canales.

Cada mensaje sale de una persona distinta (contacto nuevo) y se mide:

- recepción del webhook: cuánto tardan nginx y la API en contestar 200 (lo que exige cada plataforma);
- respuesta completa: desde que sale el webhook hasta que la respuesta llega a la plataforma simulada
  (cola, worker, motor RAG con el modelo simulado, dispatcher y API de salida del canal);
- mensajes sin respuesta al final de la prueba y respuestas duplicadas.

    python load_test.py --rate 20 --duration 60 --out /results/sostenida.json
    python load_test.py --ramp 10,25,50,100 --step 40 --out /results/escalonada.json

Como hacen las plataformas reales, un webhook que no recibe 200 se reintenta con espera creciente
(hasta 6 intentos): una caída breve de un componente no debería perder mensajes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import statistics
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from iamia.security.signatures import meta_signature

BASE_URL = os.environ.get("E2E_BASE_URL", "http://nginx")
PLATFORMS_URL = os.environ.get("E2E_PLATFORMS_URL", "http://platforms:9000")
RETRY_WAITS = (1, 2, 4, 8, 16)

QUESTIONS = (
    "¿Cómo solicito y activo el DNI en el celular?",
    "¿Cuánto cuesta el DNI en el celular?",
    "Olvidé mi contraseña de Mi Argentina, ¿cómo la recupero?",
    "¿Puedo viajar al exterior con el DNI digital?",
    "¿Cómo valido mi identidad en Mi Argentina?",
    "¿Cómo asocio a mis hijos a mi cuenta de Mi Argentina?",
    "¿Qué requisitos necesito para tener el DNI en el celular?",
    "¿Cómo saco turno para renovar el DNI?",
    "¿Dónde veo mi licencia de conducir digital?",
    "¿Cómo cambio el correo electrónico de mi cuenta?",
)
OTHER_TOPICS = ("¿Cuál es la receta del locro?", "Contame un chiste")
# Reparto aproximado por canal en un servicio público de mensajería.
CHANNEL_MIX = ("whatsapp",) * 11 + ("telegram",) * 5 + ("messenger",) * 2 + ("instagram",) * 2


@dataclass
class Message:
    number: int
    step: int
    channel: str
    recipient: str
    question: str
    kind: str = "repetida"
    sent_at: float = 0.0
    ack_ms: float | None = None
    status: int | None = None
    attempts: int = 0
    replies: list[float] = field(default_factory=list)


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None}
    if len(values) == 1:
        return {"p50": values[0], "p95": values[0], "p99": values[0], "max": values[0]}
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return {
        "p50": round(cuts[49], 1),
        "p95": round(cuts[94], 1),
        "p99": round(cuts[98], 1),
        "max": round(max(values), 1),
    }


class Generator:
    def __init__(self, client: httpx.AsyncClient, tools: httpx.AsyncClient) -> None:
        self.client = client
        self.tools = tools
        self.messages: dict[tuple[str, str], Message] = {}
        self.seen: set[str] = set()
        # Identificadores de Telegram únicos entre corridas (si se repiten, se descartan como duplicados).
        self.update_base = 10**9 + secrets.randbelow(10**9) * 10**6
        self.all_sent = False
        self.all_answered = asyncio.Event()
        self.last_seen_at = 0.0
        self.secrets = {
            "telegram": os.environ["TELEGRAM_WEBHOOK_SECRET"],
            "whatsapp": os.environ["WHATSAPP_APP_SECRET"],
            "messenger": os.environ["MESSENGER_APP_SECRET"],
            "instagram": os.environ["INSTAGRAM_APP_SECRET"],
        }
        self.accounts = {
            "whatsapp": os.environ["WHATSAPP_PHONE_NUMBER_ID"],
            "messenger": os.environ["MESSENGER_PAGE_ID"],
            "instagram": os.environ["INSTAGRAM_ACCOUNT_ID"],
        }

    # -------------------------------------------------------------- webhooks como las plataformas
    def _request(self, message: Message) -> tuple[str, bytes, dict[str, str]]:
        now = int(time.time())
        if message.channel == "telegram":
            chat = int(message.recipient)
            update = {
                "update_id": self.update_base + message.number,
                "message": {
                    "message_id": message.number,
                    "from": {"id": chat, "is_bot": False, "first_name": "Carga"},
                    "chat": {"id": chat, "type": "private"},
                    "date": now,
                    "text": message.question,
                },
            }
            headers = {"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": self.secrets["telegram"]}
            return "/webhooks/telegram", json.dumps(update).encode(), headers
        if message.channel == "whatsapp":
            value = {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "5491100000000", "phone_number_id": self.accounts["whatsapp"]},
                "contacts": [{"profile": {"name": "Carga"}, "wa_id": message.recipient}],
                "messages": [
                    {
                        "from": message.recipient,
                        "id": f"wamid.LOAD{uuid.uuid4().hex}",
                        "timestamp": str(now),
                        "type": "text",
                        "text": {"body": message.question},
                    }
                ],
            }
            payload: dict[str, Any] = {
                "object": "whatsapp_business_account",
                "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
            }
        else:
            account = self.accounts[message.channel]
            event = {
                "sender": {"id": message.recipient},
                "recipient": {"id": account},
                "timestamp": now * 1000,
                "message": {"mid": f"m_LOAD{uuid.uuid4().hex}", "text": message.question},
            }
            webhook_object = "page" if message.channel == "messenger" else "instagram"
            payload = {"object": webhook_object, "entry": [{"id": account, "time": now * 1000, "messaging": [event]}]}
        body = json.dumps(payload).encode()
        signature = meta_signature(self.secrets[message.channel], body)
        return (
            f"/webhooks/{message.channel}",
            body,
            {"Content-Type": "application/json", "X-Hub-Signature-256": signature},
        )

    async def send(self, message: Message) -> None:
        path, body, headers = self._request(message)
        message.sent_at = time.time()
        for wait in (*RETRY_WAITS, None):
            message.attempts += 1
            started = time.perf_counter()
            try:
                response = await self.client.post(path, content=body, headers=headers)
                message.status = response.status_code
            except httpx.HTTPError:
                message.status = 0
            if message.ack_ms is None or message.status == 200:
                message.ack_ms = (time.perf_counter() - started) * 1000
            if message.status == 200 or wait is None:
                return
            await asyncio.sleep(wait)

    # -------------------------------------------------------------- respuestas en el simulador
    async def collect(self, since: float) -> None:
        # Lectura incremental: solo lo nuevo (con un margen de 2 s por envíos concurrentes).
        since = max(since, self.last_seen_at - 2)
        response = await self.tools.get(f"{PLATFORMS_URL}/_outbox", params={"since": since})
        for item in response.json()["items"]:
            if item["id"] in self.seen:
                continue
            self.seen.add(item["id"])
            self.last_seen_at = max(self.last_seen_at, item["at"])
            message = self.messages.get((item["channel"], item["recipient"]))
            if message is not None:
                message.replies.append(item["at"])
        if self.all_sent and not self.pending():
            self.all_answered.set()

    def pending(self) -> int:
        return sum(1 for m in self.messages.values() if not m.replies)

    # -------------------------------------------------------------- ejecución
    async def run(self, steps: list[tuple[float, float]], drain: float, concurrency: int, unique_ratio: float) -> float:
        started = time.time()
        limiter = asyncio.Semaphore(concurrency)
        tasks: list[asyncio.Task[None]] = []
        poller = asyncio.create_task(self._poll(started))

        async def guarded(message: Message) -> None:
            async with limiter:
                await self.send(message)

        number = 0
        clock = time.perf_counter()
        for step, (rate, seconds) in enumerate(steps):
            total = int(rate * seconds)
            for i in range(total):
                target = clock + i / rate
                delay = target - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                number += 1
                channel = CHANNEL_MIX[number % len(CHANNEL_MIX)]
                question, kind = self._question(number, unique_ratio)
                recipient = self._recipient(channel)
                message = Message(number, step, channel, recipient, question, kind)
                self.messages[(channel, recipient)] = message
                tasks.append(asyncio.create_task(guarded(message)))
            clock += seconds
        await asyncio.gather(*tasks)
        self.all_sent = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.all_answered.wait(), drain)
        await asyncio.sleep(2)  # duplicados tardíos
        poller.cancel()
        await self.collect(started - 1)
        return started

    async def _poll(self, started: float) -> None:
        while True:
            try:
                await self.collect(started - 1)
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)

    @staticmethod
    def _question(number: int, unique_ratio: float) -> tuple[str, str]:
        """Una de cada 20 es de otro tema; del resto, una parte es única (no está en ninguna caché y
        recorre el circuito completo: búsqueda vectorial, modelo y controles) y otra se repite."""
        if number % 20 == 0:
            return OTHER_TOPICS[number % 2], "otro_tema"
        base = QUESTIONS[number % len(QUESTIONS)]
        if (number * 7919 % 1000) / 1000 < unique_ratio:
            return f"{base} (consulta {number})", "unica"
        return base, "repetida"

    @staticmethod
    def _recipient(channel: str) -> str:
        number = 10**8 + secrets.randbelow(9 * 10**8)
        return {
            "telegram": str(number),
            "whatsapp": f"54911{number}",
            "messenger": f"25{number}",
            "instagram": f"18{number}",
        }[channel]

    # -------------------------------------------------------------- informe
    def timeline(self, path: str, started: float) -> None:
        rows = ["segundo,canal,tipo,intentos_webhook,recepcion_ms,respuesta_ms,respuestas"]
        for m in sorted(self.messages.values(), key=lambda item: item.sent_at):
            reply = f"{(min(m.replies) - m.sent_at) * 1000:.1f}" if m.replies else ""
            ack = f"{m.ack_ms:.1f}" if m.ack_ms is not None else ""
            second = m.sent_at - started
            rows.append(f"{second:.2f},{m.channel},{m.kind},{m.attempts},{ack},{reply},{len(m.replies)}")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(chr(10).join(rows) + chr(10))

    def report(self, steps: list[tuple[float, float]]) -> dict[str, Any]:
        result: dict[str, Any] = {"steps": []}
        groups = [[m for m in self.messages.values() if m.step == index] for index in range(len(steps))]
        for (rate, seconds), group in zip(steps, groups, strict=True):
            result["steps"].append(self._summary(group, rate, seconds))
        everything = list(self.messages.values())
        result["total"] = self._summary(everything, None, sum(seconds for _, seconds in steps))
        return result

    @staticmethod
    def _summary(group: list[Message], rate: float | None, seconds: float) -> dict[str, Any]:
        answered = [m for m in group if m.replies]
        latencies = [(min(m.replies) - m.sent_at) * 1000 for m in answered]
        acks = [m.ack_ms for m in group if m.ack_ms is not None and m.status == 200]
        by_channel: dict[str, int] = {}
        for message in group:
            by_channel[message.channel] = by_channel.get(message.channel, 0) + 1
        by_kind = {
            kind: _percentiles([(min(m.replies) - m.sent_at) * 1000 for m in answered if m.kind == kind])
            for kind in ("unica", "repetida", "otro_tema")
        }
        return {
            "target_rate_per_s": rate,
            "seconds": seconds,
            "sent": len(group),
            "by_channel": by_channel,
            "accepted": sum(1 for m in group if m.status == 200),
            "retried_webhooks": sum(1 for m in group if m.attempts > 1),
            "rejected_after_retries": sum(1 for m in group if m.status != 200),
            "answered": len(answered),
            "unanswered": len(group) - len(answered),
            "duplicated": sum(1 for m in group if len(m.replies) > 1),
            "webhook_ack_ms": _percentiles(acks),
            "reply_ms": _percentiles(latencies),
            "reply_ms_by_kind": by_kind,
        }


def _steps(args: argparse.Namespace) -> list[tuple[float, float]]:
    if args.ramp:
        return [(float(rate), args.step) for rate in args.ramp.split(",")]
    return [(args.rate, args.duration)]


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    steps = _steps(args)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with (
        httpx.AsyncClient(base_url=BASE_URL, timeout=30, limits=limits) as client,
        httpx.AsyncClient(timeout=30) as tools,
    ):
        generator = Generator(client, tools)
        started = await generator.run(steps, args.drain, args.concurrency, args.unique_ratio)
        if args.timeline:
            generator.timeline(args.timeline, started)
        return {"label": args.label, "started_at": started, **generator.report(steps)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rate", type=float, default=10.0, help="mensajes por segundo")
    parser.add_argument("--duration", type=float, default=60.0, help="segundos de carga sostenida")
    parser.add_argument("--ramp", default="", help="tasas por escalón, por ejemplo 10,25,50,100")
    parser.add_argument("--step", type=float, default=40.0, help="segundos por escalón")
    parser.add_argument("--drain", type=float, default=180.0, help="espera máxima de respuestas al final")
    parser.add_argument("--concurrency", type=int, default=400, help="webhooks simultáneos como máximo")
    parser.add_argument("--unique-ratio", type=float, default=0.5, help="proporción de consultas únicas")
    parser.add_argument("--label", default="", help="nombre de la corrida en el informe")
    parser.add_argument("--out", default="", help="archivo JSON con el resultado")
    parser.add_argument("--timeline", default="", help="CSV con un renglón por mensaje (envío y demoras)")
    args = parser.parse_args()
    report = asyncio.run(execute(args))
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    total = report["total"]
    return 0 if total["unanswered"] == 0 and total["duplicated"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
