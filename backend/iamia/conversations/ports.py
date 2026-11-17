"""Puertos que el gestor de conversaciones necesita del exterior (persistencia y límites).

Con estas interfaces el gestor se prueba con dobles en memoria, sin base de datos.
"""

from __future__ import annotations

from typing import Any, Protocol


class ContactCommands(Protocol):
    async def set_opt_out(self, contact_id: str, opted_out: bool) -> None: ...

    async def request_erasure(self, contact_id: str) -> None: ...

    async def create_link_code(self, contact_id: str, code_hash: str, ttl_seconds: int) -> None: ...

    async def redeem_link_code(self, code_hash: str, contact_id: str) -> bool: ...


class AuditTrail(Protocol):
    async def record(
        self,
        actor: str,
        action: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None: ...


class ContactThrottle(Protocol):
    async def check(self, contact_key: str) -> tuple[bool, bool]:
        """(permitido, avisar): avisar es True solo la primera vez que se excede en la ventana."""
        ...


class CodeHasher(Protocol):
    def __call__(self, code: str) -> str: ...
