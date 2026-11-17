"""Roles y permisos de la API de administración (RBAC).

Los roles vienen del proveedor de identidad (Keycloak u otro OIDC). Cada endpoint pide un
permiso, no un rol: así se puede reorganizar qué hace cada rol sin tocar los endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    SUPERVISOR = "supervisor"
    OPERATOR = "operator"
    AUDITOR = "auditor"


class Permission(StrEnum):
    CONVERSATIONS_READ = "conversations:read"
    CONVERSATIONS_CLAIM = "conversations:claim"
    CONVERSATIONS_REPLY = "conversations:reply"
    CONVERSATIONS_CLOSE = "conversations:close"
    CONVERSATIONS_REASSIGN = "conversations:reassign"
    CHANNELS_READ = "channels:read"
    DEAD_LETTERS_MANAGE = "dead_letters:manage"
    AUDIT_READ = "audit:read"
    CONTACTS_DELETE = "contacts:delete"


_OPERATOR = {
    Permission.CONVERSATIONS_READ,
    Permission.CONVERSATIONS_CLAIM,
    Permission.CONVERSATIONS_REPLY,
    Permission.CONVERSATIONS_CLOSE,
}
_SUPERVISOR = _OPERATOR | {Permission.CONVERSATIONS_REASSIGN, Permission.CHANNELS_READ, Permission.AUDIT_READ}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.SUPERVISOR: frozenset(_SUPERVISOR),
    Role.OPERATOR: frozenset(_OPERATOR),
    # El auditor revisa la trazabilidad, pero no lee conversaciones ni actúa sobre ellas.
    Role.AUDITOR: frozenset({Permission.AUDIT_READ, Permission.CHANNELS_READ}),
}


@dataclass(frozen=True)
class Principal:
    """Usuario autenticado de la consola (operador, supervisor, etc.)."""

    subject: str
    roles: frozenset[Role] = field(default_factory=frozenset)
    name: str | None = None

    @property
    def permissions(self) -> frozenset[Permission]:
        granted: set[Permission] = set()
        for role in self.roles:
            granted |= ROLE_PERMISSIONS.get(role, frozenset())
        return frozenset(granted)

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions


def parse_roles(values: object) -> frozenset[Role]:
    """Toma la lista de roles del token y se queda solo con los conocidos."""
    if not isinstance(values, list | tuple | set | frozenset):
        return frozenset()
    known = {role.value: role for role in Role}
    roles: set[Role] = set()
    for value in values:
        if isinstance(value, str):
            name = value.strip().lower().removeprefix("iamia-").removeprefix("iamia:")
            if name in known:
                roles.add(known[name])
    return frozenset(roles)
