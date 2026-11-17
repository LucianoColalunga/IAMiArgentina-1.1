"""Entrada del usuario: limpieza, delimitación en el prompt y detección de prompt injection.

Las heurísticas no son infalibles: son la primera línea de una defensa en profundidad que se
completa con el prompt delimitado, el umbral de relevancia del RAG y el guard de salida.
"""

from __future__ import annotations

import re

from iamia.core.text import clean_text, fold


def sanitize_user_input(text: str | None) -> str:
    return clean_text(text)


def escape_for_prompt(text: str) -> str:
    """Impide que el usuario abra o cierre las etiquetas que delimitan el prompt."""
    return text.replace("<", "‹").replace(">", "›")


_SPECIAL_TOKENS = re.compile(r"<\|[^|>]{1,40}\|>|\[/?INST\]|<</?SYS>>", re.IGNORECASE)
_PROMPT_TAGS = re.compile(
    r"</?\s*(contexto_oficial|pregunta_usuario|historial|system|assistant|user)\s*>", re.IGNORECASE
)


def sanitize_context(text: str) -> str:
    """Limpia documentos recuperados: aunque vengan de la ingesta, no se confía en ellos."""
    text = _SPECIAL_TOKENS.sub(" ", text)
    text = _PROMPT_TAGS.sub(" ", text)
    return clean_text(text)


_OVERRIDE_VERBS = (
    r"(ignora|ignore|ignoren|ignorar|olvida|olvidate|olviden|olvidar|omiti|omite|omitir|"
    r"descarta|descartar|saltea|saltear|anula|anular|desactiva|desactivar|deja de lado|"
    r"dejar de lado|pasa por alto|pasar por alto|no sigas|no respetes|no cumplas|"
    r"desobedece|desobedecer|saltate|salta)"
)
_ASSISTANT_QUALIFIER = (
    r"(tus|tu|sus|todas|anteriores|previas|de arriba|del sistema|iniciales|originales|"
    r"que te (dieron|dio|pusieron|programaron)|que (tenes|tienes))"
)
_RULE_NOUNS = (
    r"(instrucciones|instruccion|reglas|regla|indicaciones|directivas|restricciones|"
    r"ordenes|politicas|limitaciones|filtros|prompt|programacion|configuracion|consignas)"
)
_REVEAL_VERBS = (
    r"(revela|revelame|revelar|mostra|mostrame|muestra|muestrame|mostrar|deci|decime|dime|"
    r"decir|repeti|repetime|repite|repetir|imprimi|imprime|imprimir|escribi|escribime|"
    r"escribe|copia|copiame|traduci|traduce|resumi|resume|lista|enumera|cuales son|"
    r"que dice|que dicen)"
)

# Se evalúan sobre el texto "plegado": minúsculas y sin tildes.
_FOLDED_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "override_rules_es",
        re.compile(
            rf"\b{_OVERRIDE_VERBS}\b[^.\n]{{0,40}}"
            rf"(\b{_ASSISTANT_QUALIFIER}\b[^.\n]{{0,25}}\b{_RULE_NOUNS}\b"
            rf"|\b{_RULE_NOUNS}\b[^.\n]{{0,15}}\b{_ASSISTANT_QUALIFIER}\b)"
        ),
    ),
    (
        "forget_everything_es",
        re.compile(
            r"\b(olvida|olvidate|ignora|ignore|borra)\b[^.\n]{0,10}\btodo (lo )?(anterior|que te (dije|dijeron))"
        ),
    ),
    (
        "role_change_es",
        re.compile(
            r"\b(a partir de (ahora|este momento)|desde ahora|de ahora en mas)\b[^.\n]{0,40}"
            r"\b(sos|eres|seras|vas a ser|actua|actuas|actuaras|responde|responderas|respondes|"
            r"hablas|hablaras|tu nombre es|te llamas|ya no)\b"
        ),
    ),
    (
        "unrestricted_persona_es",
        re.compile(
            r"\b(sos|eres|seras|serias|actua como|comportate como|you are)\b[^.\n]{0,40}"
            r"\b(sin (reglas|restricciones|limites|filtros|censura)|libre de (reglas|restricciones)|"
            r"without (any )?(rules|restrictions|filters|limits))\b"
        ),
    ),
    (
        "roleplay_es",
        re.compile(
            r"(^|[.!?,;:¡¿]\s*|\b(quiero|necesito|te pido) que\s+)"
            r"(actua|actues|comportate|te comportes|finge|fingi|finjas|simula|simules|"
            r"hace de cuenta|hagas de cuenta|pretende|interpreta|interpretes)\b"
        ),
    ),
    (
        "jailbreak_keywords",
        re.compile(
            r"\b(jailbreak|jail break|do anything now|developer mode|dan mode|roleplay|role play|"
            r"juego de rol|tu nuevo (rol|nombre|personaje)|cambia(r)? (de|tu) (rol|personalidad)|"
            r"modo (desarrollador|developer|dios|sin restricciones|sin filtros|sin censura|libre|"
            r"debug|depuracion|administrador|admin|root|jailbreak))\b"
        ),
    ),
    (
        "reveal_prompt_es",
        re.compile(rf"\b{_REVEAL_VERBS}\b[^.\n]{{0,30}}\b(tus|tu)\s+{_RULE_NOUNS}\b"),
    ),
    (
        "system_prompt_reference",
        re.compile(
            r"\b(system prompt|prompt (de|del) sistema|mensaje (de|del) sistema|"
            r"instrucciones (de|del) sistema|instrucciones (iniciales|originales|ocultas|internas)|"
            r"prompt (inicial|original|oculto|interno))\b"
        ),
    ),
    (
        "override_rules_en",
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass|skip)\b[^.\n]{0,30}"
            r"\b(previous|prior|above|earlier|all|your|the|any|system)\b[^.\n]{0,20}"
            r"\b(instructions?|rules?|prompts?|guidelines|directives|constraints)\b"
        ),
    ),
    (
        "role_change_en",
        re.compile(
            r"\b(you are now|act as|pretend (to be|you are)|roleplay as|from now on|"
            r"new instructions|reveal your|show me your (prompt|instructions|rules)|"
            r"print your (prompt|instructions)|what are your (instructions|rules))\b"
        ),
    ),
    (
        "chat_template_tokens",
        re.compile(
            r"<\|[a-z_ ]{2,30}\|>|\[/?inst\]|<</?sys>>"
            r"|<\s*/?\s*(system|assistant|contexto_oficial|pregunta_usuario|historial|instrucciones)\s*>"
            r"|(^|\n)\s*(#{1,6}\s*)?(system|assistant|sistema|asistente|instrucciones)\s*:"
        ),
    ),
]

# Payloads codificados (base64 largo) usados para esconder instrucciones.
_ENCODED_PAYLOAD = re.compile(r"[A-Za-z0-9+/]{100,}={0,2}")

# Frases de ataque buscadas en el texto "aplastado" (sin espacios ni símbolos) para detectar
# ofuscaciones como "i g n o r a   t u s   r e g l a s" o "1gn0r4".
_SQUASHED_KEYWORDS = (
    "ignoratusinstrucciones",
    "ignoratodaslasinstrucciones",
    "ignoralasinstruccionesanteriores",
    "ignoratusreglas",
    "olvidatusinstrucciones",
    "olvidatusreglas",
    "ignorepreviousinstructions",
    "ignoreallpreviousinstructions",
    "ignoreallinstructions",
    "disregardpreviousinstructions",
    "systemprompt",
    "jailbreak",
    "developermode",
    "mododesarrollador",
    "modosinrestricciones",
    "doanythingnow",
)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_NON_LETTERS = re.compile(r"[^a-z]")


def detect_prompt_injection(text: str) -> str | None:
    """Devuelve el nombre de la regla que detectó el ataque, o None si parece legítimo."""
    if _ENCODED_PAYLOAD.search(text):
        return "encoded_payload"
    folded = fold(text)
    for name, pattern in _FOLDED_RULES:
        if pattern.search(folded):
            return name
    squashed = _NON_LETTERS.sub("", folded.translate(_LEET))
    for keyword in _SQUASHED_KEYWORDS:
        if keyword in squashed:
            return "obfuscated_injection"
    return None
