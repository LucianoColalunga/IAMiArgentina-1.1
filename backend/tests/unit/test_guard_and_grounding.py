"""Control de la salida del modelo, anclaje al contexto y conteo de tokens."""

from __future__ import annotations

from iamia.ai.grounding import GroundingPolicy, grounding_score
from iamia.ai.guard import REMOVED_LINK, guard_output, guard_rewrite
from iamia.ai.prompts import NO_INFO_MESSAGE, REFUSAL_MESSAGE
from iamia.core.tokens import TokenCounter

DOMAINS = ["argentina.gob.ar", "gob.ar"]
CANARY = "c4n4r10c4n4r"


def test_canary_leak_is_blocked() -> None:
    result = guard_output(f"Mi código es {CANARY}", CANARY, DOMAINS)
    assert result.text == REFUSAL_MESSAGE and result.violation == "canary_leak"


def test_system_prompt_markers_are_blocked() -> None:
    result = guard_output("Estas son mis reglas obligatorias: no hablar de política.", CANARY, DOMAINS)
    assert result.is_refusal and result.violation == "prompt_leak"


def test_reasoning_and_echoed_prompt_blocks_are_removed() -> None:
    raw = (
        "<think>la persona quiere el DNI</think>"
        "<pregunta_usuario>como saco el dni</pregunta_usuario>"
        "Se saca con turno. Fuente: https://www.argentina.gob.ar/dni"
    )
    result = guard_output(raw, CANARY, DOMAINS)
    assert result.text == "Se saca con turno. Fuente: https://www.argentina.gob.ar/dni"
    assert result.violation is None


def test_foreign_links_and_html_are_removed() -> None:
    raw = "Mirá <b>esto</b>: https://sitio-falso.com/dni, o www.argentina.gob.ar/dni."
    assert guard_output(raw, CANARY, DOMAINS).text == f"Mirá esto: {REMOVED_LINK}, o www.argentina.gob.ar/dni."
    for trap in ("https://argentina.gob.ar.evil.com/x", "https://gob.ar@evil.com/x", "https://evilgob.ar/x"):
        assert guard_output(f"Ver {trap}", CANARY, DOMAINS).text == f"Ver {REMOVED_LINK}"


def test_fixed_answers_are_normalized() -> None:
    assert guard_output(f"{REFUSAL_MESSAGE}. Saludos.", CANARY, DOMAINS).text == REFUSAL_MESSAGE
    assert guard_output("No encontré información oficial sobre eso.", CANARY, DOMAINS).text == NO_INFO_MESSAGE
    # Si deriva a una fuente oficial, la respuesta es útil y se conserva.
    derived = "No encontré información oficial sobre el valor. Fuente: https://www.argentina.gob.ar/renaper"
    assert guard_output(derived, CANARY, DOMAINS).text == derived
    empty = guard_output("<think>solo pensamiento</think>", CANARY, DOMAINS)
    assert empty.text == NO_INFO_MESSAGE and empty.violation == "empty_output"


def test_long_answers_are_truncated() -> None:
    result = guard_output("palabra " * 1000, CANARY, DOMAINS, max_chars=100)
    assert len(result.text) <= 100 and result.text.endswith("…")


def test_guard_rewrite() -> None:
    assert guard_rewrite('"¿Cuánto cuesta renovar el DNI?"') == "¿Cuánto cuesta renovar el DNI?"
    assert guard_rewrite("línea uno\nlínea dos") is None
    assert guard_rewrite("<historial>x</historial>") is None
    assert guard_rewrite("x" * 301) is None
    assert guard_rewrite("") is None


def test_grounding_score() -> None:
    context = "El DNI digital se activa desde la aplicación Mi Argentina validando la identidad con una selfie."
    grounded = (
        "Se activa desde la aplicación Mi Argentina validando tu identidad. Fuente: https://www.argentina.gob.ar/dni"
    )
    invented = "Tenés que pagar un arancel en el banco y llevar fotocopias certificadas por escribano."
    assert grounding_score(grounded, context) == 1.0
    assert grounding_score(invented, context) < 0.35
    assert grounding_score("Sí.", context) == 1.0  # sin términos con contenido no hay nada que medir


def test_grounding_policy_modes() -> None:
    context = "texto del contexto oficial"
    answer = "respuesta inventada completamente distinta"
    assert GroundingPolicy("off").evaluate(answer, context) == (None, True)
    score, accepted = GroundingPolicy("observe").evaluate(answer, context)
    assert accepted and score is not None and score < 0.35  # observe mide pero no bloquea
    assert GroundingPolicy("enforce", 0.35).evaluate(answer, context)[1] is False


def test_token_counter() -> None:
    counter = TokenCounter()
    assert counter.count("") == 0
    assert counter.count("¿Cómo saco el DNI digital?") > 0
    assert counter.count("<|endoftext|>") > 0  # un token especial escrito por el usuario es texto común
    text = "palabra " * 200
    assert counter.count(counter.truncate(text, 20)) <= 20
    assert counter.truncate(text, 0) == ""
    assert counter.truncate("corto", 50) == "corto"
