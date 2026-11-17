"""Borde de la plataforma: nginx, salud, encabezados de seguridad, páginas y suscripción de webhooks."""

from __future__ import annotations

import httpx

from conftest import env


def test_health_endpoints(http: httpx.Client) -> None:
    assert http.get("/health/live").status_code == 200
    assert http.get("/health/ready").status_code == 200


def test_metrics_are_not_published_on_the_edge(http: httpx.Client) -> None:
    assert http.get("/metrics").status_code == 404


def test_api_security_headers(http: httpx.Client) -> None:
    response = http.get("/health/live")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert response.headers["x-request-id"]


def test_webchat_and_console_pages(http: httpx.Client) -> None:
    root = http.get("/", follow_redirects=False)
    assert root.status_code in (302, 307) and root.headers["location"] == "/chat/"
    for path in ("/chat/", "/console/"):
        page = http.get(path)
        assert page.status_code == 200 and "<html" in page.text.lower()
        policy = page.headers["content-security-policy"]
        assert "script-src 'self'" in policy and "unsafe-inline" not in policy


def test_direct_calls_to_the_api_need_the_edge_secret(http: httpx.Client) -> None:
    """La API rechaza /v1/chat si no pasó por nginx (que agrega el secreto compartido)."""
    with httpx.Client(base_url="http://api:8000", timeout=10) as direct:
        response = direct.post("/v1/chat", json={"question": "¿Cómo saco el DNI digital?"})
    assert response.status_code in (401, 403)


def test_meta_subscription_challenge(http: httpx.Client) -> None:
    params = {"hub.mode": "subscribe", "hub.verify_token": env("WHATSAPP_VERIFY_TOKEN"), "hub.challenge": "1158201444"}
    accepted = http.get("/webhooks/whatsapp", params=params)
    assert accepted.status_code == 200 and accepted.text == "1158201444"
    rejected = http.get("/webhooks/whatsapp", params={**params, "hub.verify_token": "otro"})
    assert rejected.status_code == 403


def test_unknown_channels_are_not_found(http: httpx.Client) -> None:
    assert http.post("/webhooks/fax", content=b"{}").status_code == 404
