"""Textos fijos que el asistente envía a las personas (comandos, avisos y errores)."""

from __future__ import annotations

WELCOME = (
    "Hola. Soy un asistente automático que responde consultas sobre trámites y servicios de "
    "Mi Argentina con información de sitios oficiales.\n\n"
    "Escribí tu consulta en una oración, por ejemplo: ¿Cómo saco el DNI digital?\n\n"
    "No compartas datos personales (DNI, CUIL, contraseñas ni códigos). Escribí AYUDA para ver las opciones."
)

HELP = (
    "Podés escribir tu consulta sobre un trámite o usar estas palabras:\n"
    "- HUMANO: hablar con una persona del equipo.\n"
    "- PRIVACIDAD: qué datos se guardan y cómo borrarlos.\n"
    "- BORRAR: borrar tus mensajes y datos de este canal.\n"
    "- VINCULAR: usar la misma identidad en otro canal.\n"
    "- BAJA: dejar de recibir mensajes."
)

PRIVACY = (
    "Guardamos tus mensajes cifrados, con los datos personales enmascarados, durante {days} días "
    "para responderte y atender reclamos. No se usan para publicidad ni se comparten con terceros. "
    "Podés pedir el borrado en cualquier momento escribiendo BORRAR. Los derechos de acceso, "
    "rectificación y supresión están previstos en la Ley 25.326 de Protección de Datos Personales."
)

ERASE_CONFIRMATION_REQUEST = (
    "Vas a borrar todos tus mensajes y datos de este canal. No se puede deshacer.\n"
    "Para confirmar, escribí: BORRAR CONFIRMAR"
)
ERASE_DONE = "Listo. Tus mensajes y datos de este canal se borran en los próximos minutos."

OPT_OUT_DONE = "Listo, no vas a recibir más mensajes de este asistente. Si querés volver, escribí ALTA."
OPT_IN_DONE = "Listo, el asistente está activo de nuevo. Escribí tu consulta."

HANDOFF_STARTED = (
    "Te comunico con una persona del equipo. Te va a responder por este mismo chat apenas haya alguien disponible."
)
HANDOFF_ALREADY = "Tu pedido ya está en la fila de atención. Una persona del equipo te va a responder por acá."
HANDOFF_DISABLED = "Por ahora la atención es solo automática. Podés seguir escribiendo tu consulta."
HANDOFF_SUGGESTION = "Si preferís, escribí HUMANO para hablar con una persona del equipo."
HANDOFF_TIMEOUT = (
    "En este momento no hay personas disponibles para atenderte. Podés seguir consultando al "
    "asistente automático o volver a escribir HUMANO más tarde."
)
HANDOFF_RELEASED = "La atención personalizada terminó. Si tenés otra consulta, escribila y te responde el asistente."

LINK_CODE = (
    "Tu código es {code} y vence en {minutes} minutos. Desde el otro canal donde usás el asistente, "
    "escribí: VINCULAR {code}\nNo compartas este código con nadie."
)
LINK_DONE = "Listo, vinculamos este canal con el anterior."
LINK_INVALID = "El código no es válido o ya venció. Pedí uno nuevo escribiendo VINCULAR en el otro canal."
LINK_DISABLED = "La vinculación de canales no está habilitada."

UNSUPPORTED_CONTENT = (
    "Por ahora solo puedo leer mensajes de texto. Escribí tu consulta en una oración, sin adjuntar archivos."
)
TOO_MANY_MESSAGES = "Recibimos muchos mensajes seguidos. Esperá un minuto y volvé a escribir."
