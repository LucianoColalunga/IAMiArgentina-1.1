"""Fallas de infraestructura que se resuelven solas.

Una dependencia que se reinicia o todavía no responde (PostgreSQL, Redis, la red interna entre
contenedores) no es un error del programa: se contesta 503 con espera en la API y se reintenta
con espera en las colas, sin volcar la traza completa en cada intento.
"""

from __future__ import annotations

import socket

from asyncpg import exceptions as pg
from redis import exceptions as redis_errors

TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    ConnectionError,  # conexión rechazada, reseteada o cortada
    TimeoutError,
    socket.gaierror,  # el nombre del servicio no resuelve mientras el contenedor se reinicia
    pg.PostgresConnectionError,
    pg.CannotConnectNowError,  # "the database system is starting up / shutting down"
    pg.AdminShutdownError,
    pg.CrashShutdownError,
    pg.TooManyConnectionsError,
    pg.InterfaceError,  # conexión cerrada a mitad de una operación
    pg.InternalClientError,  # estado del protocolo inconsistente después de un corte
    redis_errors.ConnectionError,
    redis_errors.TimeoutError,
)


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT_ERRORS)
