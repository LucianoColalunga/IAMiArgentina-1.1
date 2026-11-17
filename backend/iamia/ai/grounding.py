"""Control de alucinaciones por anclaje léxico.

Mide qué proporción de los términos con contenido de la respuesta aparece en el contexto
recuperado. Una respuesta que menciona muchos términos ausentes del contexto probablemente
agregó información. Es una señal barata (no requiere otro modelo); por defecto solo se mide
(modo "observe") y se puede pasar a "enforce" una vez calibrado el umbral con datos reales.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from iamia.core.text import fold

_WORD = re.compile(r"[a-z0-9]+")
_SOURCE_LINE = re.compile(r"^\s*fuente\s*:.*$", re.IGNORECASE | re.MULTILINE)
_URL = re.compile(r"https?://\S+")
_PREFIX = 5  # comparación por prefijo: "tramites" y "tramite" cuentan como el mismo término

# Palabras funcionales y verbos genéricos de una respuesta de atención: no aportan contenido.
_STOPWORDS = frozenset(
    """
    para como pero porque cuando donde desde hasta sobre entre este esta estos estas ese esa esos esas
    aquel aquella tambien solo puede pueden podes podras debes deben tenes tiene tienen hacer hace
    hacelo realizar realiza ingresar ingresa ingresando seguir segui siguiendo pasos paso luego
    despues antes primero segundo tercero ultimo cada todo todos todas otra otro otros otras mismo
    misma mismos mismas manera forma caso casos parte vez veces informacion consulta consultas
    respuesta podra podran debera deberas necesitas necesita necesario necesaria siguiente siguientes
    usted vos ustedes nosotros ellos ellas sus suyo suya esto eso aqui alli tenga tengas sera seran
    fuente oficial oficiales cual cuales quien quienes cuanto cuanta cuantos cuantas mas menos muy
    """.split()
)


def _terms(text: str) -> set[str]:
    words = _WORD.findall(fold(text))
    return {word[:_PREFIX] for word in words if len(word) >= 4 and not word.isdigit() and word not in _STOPWORDS}


def grounding_score(answer: str, context: str) -> float:
    """1.0 = todos los términos de la respuesta están en el contexto; 0.0 = ninguno."""
    body = _URL.sub(" ", _SOURCE_LINE.sub(" ", answer))
    answer_terms = _terms(body)
    if not answer_terms:
        return 1.0
    context_terms = _terms(context)
    return len(answer_terms & context_terms) / len(answer_terms)


@dataclass(frozen=True)
class GroundingPolicy:
    mode: Literal["off", "observe", "enforce"] = "observe"
    min_score: float = 0.35

    def evaluate(self, answer: str, context: str) -> tuple[float | None, bool]:
        """Devuelve (puntaje, aceptada). En modo observe siempre se acepta."""
        if self.mode == "off":
            return None, True
        score = grounding_score(answer, context)
        return score, (self.mode == "observe" or score >= self.min_score)
