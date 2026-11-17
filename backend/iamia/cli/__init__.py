"""Línea de comandos de la plataforma.

Cada rol corre en su propio proceso (y en Kubernetes en su propio Deployment):

    python -m iamia api           gateway HTTP: webhooks, webchat, API de consulta y consola
    python -m iamia worker        procesa los mensajes entrantes con el motor de IA
    python -m iamia dispatcher    envía las respuestas por cada canal
    python -m iamia maintenance   sweeper, vencimientos, retención y limpieza

Y comandos de operación: migrate, ingest, check-config, gen-secrets, key-usage, dev-token,
x-authorize.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from iamia import __version__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iamia", description="IAMiArgentina 1.1")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    api = commands.add_parser("api", help="servidor HTTP (gateway)")
    api.add_argument("--host", default="0.0.0.0")  # noqa: S104 - dentro del contenedor
    api.add_argument("--port", type=int, default=8000)

    commands.add_parser("worker", help="procesamiento de mensajes entrantes")
    dispatcher = commands.add_parser("dispatcher", help="envío de respuestas por canal")
    dispatcher.add_argument(
        "--channels", default="", help="subconjunto de canales (por defecto, todos los habilitados)"
    )
    commands.add_parser("maintenance", help="sweeper y tareas periódicas")
    commands.add_parser("migrate", help="aplica las migraciones de la base de datos")
    commands.add_parser("check-config", help="revisa la configuración de seguridad")
    commands.add_parser("gen-secrets", help="genera claves nuevas para el archivo .env")
    commands.add_parser("key-usage", help="cuántos datos cifrados usa cada clave (antes de retirar una)")

    ingest = commands.add_parser("ingest", help="indexa la base de conocimiento")
    ingest.add_argument("--faq-dir", type=Path, default=Path("data/faqs"))
    ingest.add_argument("--urls-file", type=Path, default=Path("data/sources.txt"))
    ingest.add_argument("--from-web", action="store_true", help="descarga además las páginas oficiales listadas")
    ingest.add_argument("--skip-local", action="store_true", help="no usa las FAQs locales")
    ingest.add_argument("--if-empty", action="store_true", help="solo si todavía no hay una base activa")
    ingest.add_argument("--if-changed", action="store_true", help="solo si el contenido cambió")
    ingest.add_argument("--dry-run", action="store_true", help="muestra qué se indexaría, sin escribir")
    ingest.add_argument("--chunk-size", type=int, default=1000)
    ingest.add_argument("--chunk-overlap", type=int, default=150)
    ingest.add_argument("--batch-size", type=int, default=32)
    ingest.add_argument("--delay", type=float, default=1.0, help="segundos entre descargas")
    ingest.add_argument("--rollback", action="store_true", help="vuelve a la versión anterior")

    token = commands.add_parser("dev-token", help="token de prueba para la consola (solo desarrollo)")
    token.add_argument("--role", action="append", default=[], help="admin, supervisor, operator o auditor")
    token.add_argument("--subject", default="operador.demo")
    token.add_argument("--key-dir", type=Path, default=Path(".dev-keys"))
    token.add_argument("--minutes", type=int, default=480)

    commands.add_parser("x-authorize", help="autorización inicial de la cuenta de X (OAuth 2.0 con PKCE)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command: str = args.command
    if command == "api":
        from iamia.cli.processes import run_api

        return run_api(args.host, args.port)
    if command in ("worker", "dispatcher", "maintenance"):
        from iamia.cli.processes import run_role

        return asyncio.run(run_role(command, channels=getattr(args, "channels", "")))
    if command in ("migrate", "ingest", "key-usage"):
        from iamia.cli import knowledge

        if command == "migrate":
            return asyncio.run(knowledge.migrate_command())
        if command == "key-usage":
            return asyncio.run(knowledge.key_usage_command())
        return asyncio.run(knowledge.ingest_command(args))
    from iamia.cli import security

    handlers = {
        "check-config": security.check_config,
        "gen-secrets": security.gen_secrets,
        "dev-token": security.dev_token,
        "x-authorize": security.x_authorize,
    }
    return handlers[command](args)
