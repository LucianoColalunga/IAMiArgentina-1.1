"""Recifrado después de rotar la clave: cómo lo reparte el mantenimiento entre ciclos.

El recorrido de las tablas se prueba contra PostgreSQL en tests/integration/test_key_rotation.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from iamia.persistence.rotation import TARGETS, RotationStep
from iamia.workers.maintenance import MaintenanceConfig, MaintenanceRunner


@dataclass
class FakeRotation:
    """Devuelve, para cada tabla, los pasos programados y después la da por terminada."""

    script: dict[str, list[RotationStep]]
    key_id: str = "k2"
    finished: set[str] = field(default_factory=set)
    steps: list[str] = field(default_factory=list)
    pending_calls: int = 0

    async def pending(self) -> list[str]:
        self.pending_calls += 1
        return [target for target in TARGETS if target not in self.finished]

    async def step(self, target: str) -> RotationStep:
        self.steps.append(target)
        planned = self.script.get(target, [])
        step = planned.pop(0) if planned else RotationStep(target, finished=True)
        if step.finished:
            self.finished.add(target)
        return step


def _runner(rotation: Any, budget: float = 60.0) -> MaintenanceRunner:
    return MaintenanceRunner(
        repository=None,  # type: ignore[arg-type]
        work=None,  # type: ignore[arg-type]
        publisher=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        semantic_cache=None,
        rotation=rotation,
        config=MaintenanceConfig(rotation_budget_seconds=budget),
    )


async def test_each_table_advances_until_it_waits_or_finishes_and_then_nothing_is_asked() -> None:
    rotation = FakeRotation(
        {
            "contacts": [RotationStep("contacts", rotated=1000), RotationStep("contacts", rotated=10, waiting=True)],
            "messages": [RotationStep("messages", rotated=500)],
        }
    )
    runner = _runner(rotation)

    assert await runner.rotate_keys() == {"rotated": 1510, "failed": 0}
    # oauth_tokens terminó; contacts espera el margen (no se insiste en el mismo ciclo); messages terminó.
    assert rotation.steps == ["oauth_tokens", "contacts", "contacts", "messages", "messages"]
    assert not runner.rotation_done

    assert await runner.rotate_keys() == {"rotated": 0, "failed": 0}
    assert rotation.steps[-1] == "contacts" and runner.rotation_done

    calls = rotation.pending_calls
    await runner.rotate_keys()  # al día: ni siquiera consulta
    assert rotation.pending_calls == calls


async def test_a_cycle_never_goes_over_its_time_budget() -> None:
    endless = FakeRotation({"oauth_tokens": [RotationStep("oauth_tokens", rotated=1)] * 1_000_000})
    runner = _runner(endless, budget=0.05)
    totals = await runner.rotate_keys()
    assert 0 < totals["rotated"] < 1_000_000 and not runner.rotation_done


async def test_without_rotation_configured_there_is_nothing_to_do() -> None:
    runner = _runner(None)
    assert runner.rotation_done and await runner.rotate_keys() == {"rotated": 0, "failed": 0}
