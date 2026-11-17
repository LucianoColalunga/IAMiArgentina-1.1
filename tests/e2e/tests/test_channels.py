"""Cada canal, de punta a punta: webhook firmado -> cola -> motor RAG -> dispatcher -> plataforma."""

from __future__ import annotations

import time

import pytest

from conftest import Channels, Platforms, Webchat, fold, unique_number, wait_for

QUESTION = "¿Cómo solicito y activo el DNI en el celular?"
SOURCE = "https://www.argentina.gob.ar/interior/dni-en-tu-celular"


def assert_official_answer(text: str) -> None:
    assert fold(text).startswith("Segun la informacion oficial:"), text
    assert SOURCE in text, text


def test_telegram(channels: Channels, platforms: Platforms) -> None:
    chat_id = unique_number()
    response = channels.telegram(chat_id, QUESTION)
    assert response.status_code == 200
    (reply,) = platforms.replies("telegram", str(chat_id))
    assert_official_answer(reply["text"])


def test_whatsapp(channels: Channels, platforms: Platforms) -> None:
    phone = f"54911{unique_number()}"
    assert channels.whatsapp(phone, QUESTION).status_code == 200
    (reply,) = platforms.replies("whatsapp", phone)
    assert_official_answer(reply["text"])
    assert reply["kind"] == "text"  # dentro de la ventana de 24 h: texto libre, sin plantilla


def test_messenger(channels: Channels, platforms: Platforms) -> None:
    psid = f"24{unique_number()}"
    assert channels.messenger(psid, QUESTION).status_code == 200
    (reply,) = platforms.replies("messenger", psid)
    assert_official_answer(reply["text"])


def test_instagram(channels: Channels, platforms: Platforms) -> None:
    igsid = f"17{unique_number()}"
    assert channels.instagram(igsid, QUESTION).status_code == 200
    (reply,) = platforms.replies("instagram", igsid)
    assert_official_answer(reply["text"])
    assert len(reply["text"].encode("utf-8")) <= 1000  # límite de Instagram en bytes


def test_x(channels: Channels, platforms: Platforms) -> None:
    user_id = str(unique_number())
    assert channels.x(user_id, QUESTION).status_code == 200
    (reply,) = platforms.replies("x", user_id)
    assert_official_answer(reply["text"])


def test_sms(channels: Channels, platforms: Platforms) -> None:
    phone = f"+54911{unique_number()}"
    response = channels.sms(phone, QUESTION)
    assert response.status_code == 200 and "<Response" in response.text  # Twilio espera TwiML
    replies = platforms.replies("sms", phone)
    text = " ".join(reply["text"] for reply in replies)
    assert fold(text).startswith("Segun la informacion oficial:"), text
    assert all(ch.isascii() or ch in "ñÑ¿¡éÉüÜ" for ch in text)  # GSM-7: sin pasar a UCS-2


def test_email(channels: Channels, platforms: Platforms) -> None:
    sender = f"persona{unique_number()}@ejemplo.test"
    assert channels.email(sender, QUESTION).status_code == 200
    (reply,) = platforms.replies("email", sender)
    assert fold(reply["text"]).startswith("Segun la informacion oficial:")
    assert SOURCE in reply["text"]
    assert reply["subject"].lower().startswith("re:")


def test_webchat(webchat: Webchat) -> None:
    token = webchat.session()
    assert webchat.send(token, QUESTION).status_code == 202
    messages = wait_for(lambda: webchat.poll(token), timeout=30, what="respuesta del chat web")
    assert_official_answer(messages[0]["text"])


@pytest.mark.parametrize("send", ["telegram", "whatsapp"])
def test_reply_latency_is_low(channels: Channels, platforms: Platforms, send: str) -> None:
    """Con el modelo simulado, el circuito completo (colas incluidas) responde en segundos."""
    recipient = unique_number()
    started = time.monotonic()
    if send == "telegram":
        channels.telegram(recipient, "¿Puedo viajar al exterior con el DNI digital?")
        platforms.replies("telegram", str(recipient))
    else:
        channels.whatsapp(f"54911{recipient}", "¿Puedo viajar al exterior con el DNI digital?")
        platforms.replies("whatsapp", f"54911{recipient}")
    assert time.monotonic() - started < 10
