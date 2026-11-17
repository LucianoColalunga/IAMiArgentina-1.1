"""Figuras de la documentación técnica, dibujadas con reportlab.graphics.

Cada figura es una función que recibe el ancho disponible y devuelve un Drawing. Los componentes
llevan los nombres de docker-compose.yml, de los manifiestos de Kubernetes y de las migraciones de
la base: si cambia alguno, hay que cambiarlo también acá.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Callable, Sequence

from reportlab.graphics.shapes import Circle, Drawing, Ellipse, Line, Polygon, PolyLine, Rect, String
from reportlab.lib import colors
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

SANS, NEGRITA, MONO = "Sans", "Sans-Bold", "Mono"

TINTA = colors.HexColor("#1E293B")
GRIS = colors.HexColor("#475569")
GRIS_CLARO = colors.HexColor("#94A3B8")
FLECHA = colors.HexColor("#334155")

ESTILOS: dict[str, tuple[colors.Color, colors.Color]] = {
    "externo": (colors.HexColor("#F1F5F9"), colors.HexColor("#64748B")),
    "borde": (colors.HexColor("#FEF3C7"), colors.HexColor("#B45309")),
    "app": (colors.HexColor("#E0F2FE"), colors.HexColor("#0369A1")),
    "cola": (colors.HexColor("#FFEDD5"), colors.HexColor("#C2410C")),
    "datos": (colors.HexColor("#DCFCE7"), colors.HexColor("#15803D")),
    "modelo": (colors.HexColor("#EDE9FE"), colors.HexColor("#6D28D9")),
    "ops": (colors.HexColor("#FCE7F3"), colors.HexColor("#BE185D")),
    "neutro": (colors.white, colors.HexColor("#94A3B8")),
    "panel": (colors.HexColor("#F8FAFC"), colors.HexColor("#E2E8F0")),
}


class Lienzo:
    def __init__(self, ancho: float, alto: float) -> None:
        self.dibujo = Drawing(ancho, alto)
        self.ancho, self.alto = ancho, alto

    # ------------------------------------------------------------------------------ texto
    def texto(
        self,
        x: float,
        y: float,
        texto: str,
        tam: float = 7,
        fuente: str = SANS,
        color: colors.Color = GRIS,
        ancla: str = "start",
    ) -> None:
        self.dibujo.add(String(x, y, texto, fontName=fuente, fontSize=tam, fillColor=color, textAnchor=ancla))

    def bloque_de_texto(self, cx: float, cy: float, titulo: str, detalle: str = "", tam: float = 8) -> None:
        """Título en negrita y líneas de detalle, centrados en (cx, cy)."""
        lineas = [(titulo, NEGRITA, tam, TINTA)] if titulo else []
        lineas += [(linea, SANS, tam - 1.3, GRIS) for linea in detalle.split("\n") if linea]
        alturas = [linea[2] * 1.28 for linea in lineas]
        arriba = cy + sum(alturas) / 2
        for (texto, fuente, tamano, color), altura in zip(lineas, alturas, strict=True):
            base = arriba - altura + (altura - tamano) / 2 + tamano * 0.2
            self.texto(cx, base, texto, tamano, fuente, color, "middle")
            arriba -= altura

    def etiqueta(
        self,
        x: float,
        y: float,
        texto: str,
        tam: float = 6.4,
        ancla: str = "middle",
        fondo: bool = True,
        color: colors.Color = GRIS,
    ) -> None:
        """Texto chico (una o más líneas, la primera con base en y) con fondo blanco para tapar líneas."""
        lineas = texto.split("\n")
        if fondo:
            ancho = max(stringWidth(linea, SANS, tam) for linea in lineas) + 4
            izquierda = {"start": x - 2, "middle": x - ancho / 2, "end": x - ancho + 2}[ancla]
            alto = len(lineas) * tam * 1.2 + 1.5
            arriba = y + tam * 0.95
            self.dibujo.add(Rect(izquierda, arriba - alto, ancho, alto, fillColor=colors.white, strokeColor=None))
        for numero, linea in enumerate(lineas):
            self.texto(x, y - numero * tam * 1.2, linea, tam, SANS, color, ancla)

    # ----------------------------------------------------------------------------- formas
    def caja(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        titulo: str,
        detalle: str = "",
        estilo: str = "app",
        tam: float = 8,
        punteada: bool = False,
    ) -> tuple[float, float, float, float]:
        fondo, borde = ESTILOS[estilo]
        self.dibujo.add(
            Rect(
                x,
                y,
                w,
                h,
                rx=3.5,
                ry=3.5,
                fillColor=fondo,
                strokeColor=borde,
                strokeWidth=0.8,
                strokeDashArray=[2.5, 2] if punteada else None,
            )
        )
        self.bloque_de_texto(x + w / 2, y + h / 2, titulo, detalle, tam)
        return x, y, w, h

    def panel(self, x: float, y: float, w: float, h: float, titulo: str, punteado: bool = False) -> None:
        fondo, borde = ESTILOS["panel"]
        self.dibujo.add(
            Rect(
                x,
                y,
                w,
                h,
                rx=5,
                ry=5,
                fillColor=fondo,
                strokeColor=GRIS_CLARO if punteado else borde,
                strokeWidth=0.7,
                strokeDashArray=[3, 2] if punteado else None,
            )
        )
        self.texto(x + 7, y + h - 11, titulo, 7.2, NEGRITA, GRIS)

    def cilindro(self, x: float, y: float, w: float, h: float, titulo: str, detalle: str = "") -> None:
        fondo, borde = ESTILOS["datos"]
        e = min(5.5, h * 0.14)
        self.dibujo.add(Ellipse(x + w / 2, y + e, w / 2, e, fillColor=fondo, strokeColor=borde, strokeWidth=0.8))
        self.dibujo.add(Rect(x, y + e, w, h - 2 * e, fillColor=fondo, strokeColor=None))
        self.dibujo.add(Line(x, y + e, x, y + h - e, strokeColor=borde, strokeWidth=0.8))
        self.dibujo.add(Line(x + w, y + e, x + w, y + h - e, strokeColor=borde, strokeWidth=0.8))
        self.dibujo.add(Ellipse(x + w / 2, y + h - e, w / 2, e, fillColor=fondo, strokeColor=borde, strokeWidth=0.8))
        self.bloque_de_texto(x + w / 2, y + (h - e) / 2 + 0.5, titulo, detalle)

    def cola(self, x: float, y: float, w: float, h: float, titulo: str, detalle: str = "") -> None:
        fondo, borde = ESTILOS["cola"]
        self.dibujo.add(Rect(x, y, w, h, rx=h / 2.6, ry=h / 2.6, fillColor=fondo, strokeColor=borde, strokeWidth=0.8))
        for desplazamiento in (6, 10):
            self.dibujo.add(
                Line(
                    x + w - desplazamiento, y + 6, x + w - desplazamiento, y + h - 6, strokeColor=borde, strokeWidth=0.6
                )
            )
        self.bloque_de_texto(x + w / 2 - 5, y + h / 2, titulo, detalle)

    def tabla_er(
        self, x: float, arriba: float, w: float, nombre: str, columnas: Sequence[tuple[str, str]], estilo: str = "datos"
    ) -> tuple[float, float, float, float]:
        """Tabla de un diagrama entidad-relación; devuelve (x, abajo, ancho, alto)."""
        fondo, borde = ESTILOS[estilo]
        alto = 15 + len(columnas) * 8.8 + 4
        abajo = arriba - alto
        self.dibujo.add(Rect(x, abajo, w, alto, rx=3, ry=3, fillColor=colors.white, strokeColor=borde, strokeWidth=0.8))
        self.dibujo.add(Rect(x, arriba - 15, w, 15, rx=3, ry=3, fillColor=fondo, strokeColor=borde, strokeWidth=0.8))
        self.texto(x + 5, arriba - 10.6, nombre, 7.4, NEGRITA, TINTA)
        y = arriba - 15 - 9
        for columna, marca in columnas:
            self.texto(x + 5, y + 1.4, columna, 6.4, MONO, TINTA)
            if marca:
                self.texto(x + w - 5, y + 1.4, marca, 5.8, NEGRITA, borde, "end")
            y -= 8.8
        return x, abajo, w, alto

    # ---------------------------------------------------------------------------- flechas
    def flecha(
        self,
        puntos: Sequence[tuple[float, float]],
        texto: str = "",
        punteada: bool = False,
        doble: bool = False,
        en: tuple[float, float] | None = None,
        ancla: str = "middle",
        color: colors.Color = FLECHA,
    ) -> None:
        planos = [coordenada for punto in puntos for coordenada in punto]
        self.dibujo.add(
            PolyLine(planos, strokeColor=color, strokeWidth=0.85, strokeDashArray=[2.6, 2] if punteada else None)
        )
        self._punta(puntos[-2], puntos[-1], color)
        if doble:
            self._punta(puntos[1], puntos[0], color)
        if texto:
            if en is None:
                (x1, y1), (x2, y2) = max(itertools.pairwise(puntos), key=lambda s: math.dist(s[0], s[1]))
                en = ((x1 + x2) / 2, (y1 + y2) / 2 + 2.5)
            self.etiqueta(en[0], en[1], texto, ancla=ancla)

    def _punta(self, desde: tuple[float, float], hasta: tuple[float, float], color: colors.Color) -> None:
        angulo = math.atan2(hasta[1] - desde[1], hasta[0] - desde[0])
        largo, ancho = 5.5, 2.4
        bx, by = hasta[0] - largo * math.cos(angulo), hasta[1] - largo * math.sin(angulo)
        dx, dy = ancho * math.sin(angulo), -ancho * math.cos(angulo)
        self.dibujo.add(
            Polygon(
                [hasta[0], hasta[1], bx + dx, by + dy, bx - dx, by - dy],
                fillColor=color,
                strokeColor=color,
                strokeWidth=0.4,
            )
        )

    def leyenda(self, x: float, y: float, entradas: Sequence[tuple[str, str]]) -> None:
        for estilo, texto in entradas:
            fondo, borde = ESTILOS[estilo]
            self.dibujo.add(Rect(x, y - 1, 9, 7, rx=1.5, ry=1.5, fillColor=fondo, strokeColor=borde, strokeWidth=0.7))
            self.texto(x + 12, y, texto, 6.6)
            x += 12 + stringWidth(texto, SANS, 6.6) + 12


# ----------------------------------------------------------------------------------- figuras


def arquitectura(ancho: float) -> Drawing:
    l = Lienzo(ancho, 368)  # noqa: E741
    l.texto(0, 357, "Plataformas y canales propios", 7.2, NEGRITA)
    nombres = ["WhatsApp", "Telegram", "Instagram", "Messenger", "X", "SMS (Twilio)", "Correo", "Chat web"]
    paso = (ancho - 2) / len(nombres)
    for numero, nombre in enumerate(nombres):
        l.caja(1 + numero * paso, 320, paso - 5, 26, nombre, estilo="externo", tam=7)

    l.caja(0, 262, 170, 34, "nginx / ingress-nginx", "TLS, límites por IP, cuerpo de hasta 3 MB", "borde")
    l.caja(196, 262, 92, 34, "Consola", "operadores", "ops")
    l.caja(304, 262, 84, 34, "oauth2-proxy", "login OIDC", "ops")
    l.flecha([(60, 320), (60, 296)])
    l.etiqueta(66, 305, "webhooks firmados (HTTPS)", ancla="start")
    l.flecha([(288, 279), (304, 279)])

    fila = 180
    l.caja(0, fila, 80, 46, "api", "valida la firma\nguarda y encola")
    l.cola(96, fila, 82, 46, "RabbitMQ", "iamia.inbound")
    l.caja(194, fila, 80, 46, "worker", "conversación\nmotor RAG")
    l.cola(290, fila, 94, 46, "RabbitMQ", "iamia.outbound.<canal>")
    l.caja(400, fila, ancho - 400, 46, "dispatcher", "ritmo y circuit\nbreaker por canal")
    l.flecha([(40, 262), (40, 226)])
    l.flecha([(346, 262), (346, 244), (66, 244), (66, 226)])
    l.etiqueta(206, 249, "API de la consola, con el JWT verificado en la api")
    for x1, x2 in ((80, 96), (178, 194), (274, 290), (384, 400)):
        l.flecha([(x1, fila + 23), (x2, fila + 23)])
    l.flecha([(452, fila + 46), (452, 320)])
    l.etiqueta(446, 307, "envío por la API\nde cada plataforma", ancla="end")

    abajo = 76
    l.cilindro(0, abajo, 124, 52, "PostgreSQL + pgvector", "mensajes cifrados, conocimiento\ny caché semántica")
    l.cilindro(140, abajo, 68, 52, "Redis", "caché, límites\nidempotencia")
    l.caja(224, abajo, 80, 52, "vLLM", "modelo de lenguaje\nen GPU", "modelo")
    l.caja(316, abajo, 72, 52, "embeddings", "bge-m3 en vLLM", "modelo")
    l.caja(400, abajo, ancho - 400, 52, "maintenance", "sweeper, retención\nrotación de claves")
    l.flecha([(40, fila), (40, abajo + 52)])
    l.etiqueta(44, 152, "guarda", ancla="start")
    l.flecha([(204, fila), (110, abajo + 52)], doble=True)
    l.flecha([(220, fila), (182, abajo + 52)], doble=True)
    l.flecha([(240, fila), (258, abajo + 52)], doble=True)
    l.flecha([(262, fila), (346, abajo + 52)], doble=True)

    l.texto(
        0,
        60,
        "api, worker, dispatcher y maintenance leen y escriben en PostgreSQL. Por las colas viajan solo "
        "identificadores.",
        6.8,
    )
    l.leyenda(
        0,
        42,
        [
            ("externo", "plataforma"),
            ("borde", "borde"),
            ("app", "roles de la aplicación"),
            ("cola", "cola"),
            ("datos", "datos"),
            ("modelo", "modelos"),
            ("ops", "operación"),
        ],
    )
    # Se dibujó con margen abajo: se baja todo y se recorta el alto para no dejar espacio vacío.
    l.dibujo.translate(0, -34)
    l.dibujo.height -= 34
    return l.dibujo


def secuencia(ancho: float) -> Drawing:
    actores = ["Plataforma", "api", "Redis", "PostgreSQL", "RabbitMQ", "worker", "Modelos", "dispatcher"]
    mensajes: list[tuple[str, str, str, str]] = [
        ("Plataforma", "api", "POST /webhooks/<canal> (firmado)", "llamada"),
        ("api", "api", "valida la firma sobre los bytes", "propio"),
        ("api", "Redis", "SET NX del id (corta reintentos)", "llamada"),
        ("api", "PostgreSQL", "contacto + conversación + mensaje", "llamada"),
        ("api", "RabbitMQ", "publica el id en iamia.inbound", "llamada"),
        ("api", "Plataforma", "200 en milisegundos", "respuesta"),
        ("RabbitMQ", "worker", "trabajo con el id", "llamada"),
        ("worker", "PostgreSQL", "reserva el mensaje (lease de 180 s)", "llamada"),
        ("worker", "worker", "baja, comandos, derivación, límites, contexto", "propio"),
        ("worker", "Redis", "caché exacta", "llamada"),
        ("worker", "Modelos", "embedding de la consulta", "llamada"),
        ("worker", "PostgreSQL", "caché semántica y búsqueda en la base vigente", "llamada"),
        ("worker", "Modelos", "generación (si hace falta)", "llamada"),
        ("worker", "worker", "control de salida y anclaje", "propio"),
        ("worker", "PostgreSQL", "respuesta + estado (una transacción)", "llamada"),
        ("worker", "RabbitMQ", "publica el id en iamia.outbound.<canal>", "llamada"),
        ("RabbitMQ", "dispatcher", "trabajo con el id", "llamada"),
        ("dispatcher", "Redis", "turno en el ritmo del canal", "llamada"),
        ("dispatcher", "Plataforma", "envío por la API de la plataforma", "llamada"),
        ("dispatcher", "PostgreSQL", "estado: enviado", "llamada"),
    ]
    fila = 15.6
    alto = 34 + len(mensajes) * fila + 10
    l = Lienzo(ancho, alto)  # noqa: E741
    margen = 30
    paso = (ancho - 2 * margen) / (len(actores) - 1)
    x = {actor: margen + numero * paso for numero, actor in enumerate(actores)}
    for actor, posicion in x.items():
        l.dibujo.add(
            Line(posicion, 4, posicion, alto - 22, strokeColor=GRIS_CLARO, strokeWidth=0.6, strokeDashArray=[2, 2])
        )
        estilo = {
            "Plataforma": "externo",
            "Redis": "datos",
            "PostgreSQL": "datos",
            "RabbitMQ": "cola",
            "Modelos": "modelo",
        }.get(actor, "app")
        l.caja(posicion - 27, alto - 22, 54, 18, actor, estilo=estilo, tam=6.9)
    y = alto - 34
    for numero, (desde, hasta, texto, tipo) in enumerate(mensajes, start=1):
        rotulo = f"{numero}. {texto}"
        if tipo == "propio":
            ancho_nota = stringWidth(rotulo, SANS, 6.3) + 10
            l.dibujo.add(
                Rect(
                    x[desde] - ancho_nota / 2,
                    y - 4.5,
                    ancho_nota,
                    11,
                    rx=2,
                    ry=2,
                    fillColor=colors.HexColor("#FFFBEB"),
                    strokeColor=colors.HexColor("#D6A64B"),
                    strokeWidth=0.6,
                )
            )
            l.texto(x[desde], y - 1.4, rotulo, 6.3, SANS, TINTA, "middle")
        else:
            l.flecha([(x[desde], y - 5.5), (x[hasta], y - 5.5)], punteada=tipo == "respuesta")
            l.etiqueta((x[desde] + x[hasta]) / 2, y, rotulo, tam=6.3)
        y -= fila
    return l.dibujo


def estados(ancho: float) -> Drawing:
    l = Lienzo(ancho, 188)  # noqa: E741
    y, h = 80, 34
    medio = y + h / 2
    l.dibujo.add(Circle(10, medio, 4.5, fillColor=TINTA, strokeColor=None))
    l.caja(36, y, 92, h, "bot", "atiende el asistente")
    l.caja(196, y, 100, h, "waiting_human", "espera a una persona", "borde")
    l.caja(362, y, 100, h, "human", "con un operador", "ops")
    l.caja(362, 8, 78, 30, "closed", "cerrada", "neutro")
    l.flecha([(14.5, medio), (36, medio)])
    l.flecha([(128, medio), (196, medio)])
    l.etiqueta(162, 128, "HUMANO o dos\nconsultas sin\ninformación")
    l.flecha([(296, medio), (362, medio)])
    l.etiqueta(329, 120, "un operador\nla toma")
    l.flecha([(246, y + h), (246, 148), (96, 148), (96, y + h)])
    l.etiqueta(171, 152, "nadie la tomó en 4 horas: se avisa a la persona")
    l.flecha([(412, y + h), (412, 172), (68, 172), (68, y + h)])
    l.etiqueta(240, 176, "el operador la devuelve al asistente")
    l.flecha([(82, y), (82, 23), (362, 23)])
    l.etiqueta(210, 28, "30 minutos sin actividad (se cierra al llegar el mensaje siguiente)")
    l.flecha([(412, y), (412, 38)])
    l.etiqueta(418, 62, "el operador\nla cierra", ancla="start")
    l.dibujo.add(Circle(458, 23, 4.5, fillColor=colors.white, strokeColor=TINTA, strokeWidth=0.9))
    l.dibujo.add(Circle(458, 23, 2.4, fillColor=TINTA, strokeColor=None))
    l.flecha([(440, 23), (453, 23)])
    return l.dibujo


def adaptador(ancho: float) -> Drawing:
    l = Lienzo(ancho, 150)  # noqa: E741
    l.panel(84, 6, 228, 128, "adaptador del canal: integrations/<canal>/adapter.py", punteado=True)
    arriba, abajo, h = 74, 18, 36
    l.caja(0, arriba, 66, h, "Plataforma", "webhook HTTPS", "externo")
    l.caja(96, arriba, 96, h, "Verificador", "firma sobre los\nbytes recibidos")
    l.caja(206, arriba, 96, h, "Parser", "InboundMessage o\nStatusUpdate")
    l.caja(0, abajo, 66, h, "Plataforma", "API de envío", "externo")
    l.caja(96, abajo, 96, h, "Sender", "llamada a la API,\nerrores clasificados")
    l.caja(206, abajo, 96, h, "Formatter", "largo, partes,\nformato del canal")
    l.caja(
        330,
        abajo,
        ancho - 330,
        arriba + h - abajo,
        "Núcleo común",
        "idempotencia y colas\ngestor de conversaciones\nmotor de respuesta\nderivación y auditoría\n"
        "(igual para los 8 canales)",
        "datos",
    )
    l.flecha([(66, arriba + h / 2), (96, arriba + h / 2)])
    l.flecha([(192, arriba + h / 2), (206, arriba + h / 2)])
    l.flecha([(302, arriba + h / 2), (330, arriba + h / 2)])
    l.flecha([(330, abajo + h / 2), (302, abajo + h / 2)])
    l.flecha([(206, abajo + h / 2), (192, abajo + h / 2)])
    l.flecha([(96, abajo + h / 2), (66, abajo + h / 2)])
    return l.dibujo


def motor(ancho: float) -> Drawing:
    pasos: list[tuple[str, str, str, str]] = [
        ("Limpieza", "Unicode, invisibles, largo", "pedido inválido", "app"),
        ("Manipulación", "reglas de prompt injection", "respuesta fija, sin llamar al modelo", "app"),
        ("Datos personales", "enmascara DNI, CUIL, teléfono...", "", "app"),
        ("Caché exacta", "Redis", "respuesta guardada", "datos"),
        ("Embedding", "bge-m3, 1024 dimensiones", "", "modelo"),
        ("Caché semántica", "pgvector, similitud 0,95 o más", "respuesta guardada", "datos"),
        ("Recuperación", "4 fragmentos, umbral y margen", "sin información o fuera de tema", "datos"),
        ("Single-flight", "una generación por pregunta", "espera la generación en curso", "app"),
        ("Presupuesto", "tokens por cliente y por día", "cupo agotado", "app"),
        ("Circuit breaker", "y compuerta de concurrencia", "modelo ocupado: se reintenta", "app"),
        ("Generación", "vLLM, contexto delimitado", "", "modelo"),
        ("Control de salida", "fuga del prompt, enlaces", "respuesta rechazada", "app"),
        ("Anclaje", "apoyo en el contexto", "se registra o se bloquea", "app"),
        ("Respuesta", "con su fuente; queda en las cachés", "", "datos"),
    ]
    alto_paso, separacion = 17, 7
    alto = len(pasos) * (alto_paso + separacion) + 14
    l = Lienzo(ancho, alto)  # noqa: E741
    l.texto(0, alto - 8, "Paso", 7, NEGRITA)
    l.texto(266, alto - 8, "Si corta, el resultado es...", 7, NEGRITA)
    y = alto - 14 - alto_paso
    for numero, (titulo, detalle, corte, estilo) in enumerate(pasos):
        fondo, borde = ESTILOS[estilo]
        l.dibujo.add(Rect(0, y, 220, alto_paso, rx=3, ry=3, fillColor=fondo, strokeColor=borde, strokeWidth=0.8))
        l.texto(7, y + 5.6, f"{numero + 1}. {titulo}", 7.3, NEGRITA, TINTA)
        l.texto(214, y + 5.8, detalle, 6.3, SANS, GRIS, "end")
        if corte:
            l.flecha([(220, y + alto_paso / 2), (262, y + alto_paso / 2)], punteada=True)
            l.dibujo.add(
                Rect(
                    262,
                    y + 1,
                    ancho - 262,
                    alto_paso - 2,
                    rx=2.5,
                    ry=2.5,
                    fillColor=colors.white,
                    strokeColor=GRIS_CLARO,
                    strokeWidth=0.6,
                    strokeDashArray=[2, 1.6],
                )
            )
            l.texto(270, y + 5.6, corte, 6.8, SANS, TINTA)
        if numero < len(pasos) - 1:
            l.flecha([(110, y), (110, y - separacion)])
        y -= alto_paso + separacion
    return l.dibujo


def servidor(ancho: float) -> Drawing:
    l = Lienzo(ancho, 222)  # noqa: E741
    l.panel(0, 4, ancho, 214, "Gigabyte G493-ZB3-AAP1 (4U): uso propuesto de cada servidor de modelos")
    l.caja(12, 34, 104, 150, "Procesadores", "2 × AMD EPYC 9005\n768 GB DDR5\n(24 × 32 GB,\n1 DIMM por canal)")
    filas = [
        (
            130,
            "Switch PCIe 1",
            [("GPU 1", "modelo"), ("GPU 2", "modelo"), ("GPU 3", "modelo"), ("GPU 4", "embeddings")],
        ),
        (32, "Switch PCIe 2", [("GPU 5", "modelo"), ("GPU 6", "modelo"), ("GPU 7", "modelo"), ("GPU 8", "libre")]),
    ]
    detalles = {
        "modelo": ("modelo", "réplica del\nmodelo de\nlenguaje"),
        "embeddings": ("datos", "réplica de\nembeddings"),
        "libre": ("neutro", "reserva para\nactualizar\nsin corte"),
    }
    for y, switch, gpus in filas:
        l.caja(140, y, 76, 50, switch, "PEX89144\n4 ranuras x16", "neutro")
        l.flecha([(116, y + 25), (140, y + 25)], doble=True)
        bus = y - 6
        l.dibujo.add(PolyLine([216, y + 25, 228, y + 25, 228, bus, 432, bus], strokeColor=FLECHA, strokeWidth=0.85))
        for numero, (gpu, uso) in enumerate(gpus):
            x = 242 + numero * 55
            estilo, detalle = detalles[uso]
            l.dibujo.add(Line(x + 25, bus, x + 25, y, strokeColor=FLECHA, strokeWidth=0.85))
            l.caja(x, y, 50, 62, gpu, detalle, estilo, tam=7.4, punteada=uso == "libre")
    return l.dibujo


def despliegue(ancho: float) -> Drawing:
    l = Lienzo(ancho, 330)  # noqa: E741
    l.caja(0, 296, 132, 28, "Plataformas y personas", "internet", "externo", tam=7.4)
    l.caja(170, 296, 130, 28, "Balanceador de carga", "TLS, protección de red", "borde", tam=7.4)
    l.flecha([(132, 310), (170, 310)])
    l.flecha([(235, 296), (235, 270)])

    l.panel(0, 56, ancho, 230, "Clúster de Kubernetes: namespace iamia, con la red cerrada por defecto")
    l.caja(8, 248, ancho - 16, 22, "ingress-nginx", "certificados de cert-manager", "borde", tam=7.4)
    l.flecha([(100, 248), (100, 226)])  # a la derecha del rótulo de la columna
    columnas = [
        (
            "Nodos de aplicación",
            8,
            "app",
            [
                ("api", "HPA: 3 a 12"),
                ("worker", "KEDA: 2 a 24"),
                ("dispatcher-whatsapp", "KEDA: 2 a 8"),
                ("dispatcher-general", "KEDA: 2 a 6"),
                ("maintenance", "1 réplica"),
                ("oauth2-proxy", "login de la consola"),
            ],
        ),
        (
            "Nodos de datos",
            124,
            "datos",
            [
                ("PostgreSQL", "CloudNativePG, 3 instancias"),
                ("PgBouncer", "pooler de conexiones"),
                ("RabbitMQ", "3 nodos, colas quorum"),
                ("Redis", "caché, sin disco"),
            ],
        ),
        (
            "Servidores GPU (N+1)",
            240,
            "modelo",
            [
                ("llm", "vLLM, 6 por servidor"),
                ("embeddings", "vLLM, 1 por servidor"),
                ("GPU Operator", "drivers y dcgm-exporter"),
            ],
        ),
        (
            "Operación",
            356,
            "ops",
            [
                ("External Secrets", "secretos desde Vault"),
                ("KEDA", "escalado por cola"),
                ("kube-prometheus-stack", "métricas y alertas"),
                ("Loki y Tempo", "logs y trazas"),
            ],
        ),
    ]
    for titulo, x, estilo, items in columnas:
        l.texto(x + 1, 233, titulo, 7, NEGRITA)
        for numero, (nombre, detalle) in enumerate(items):
            l.caja(x, 204 - numero * 26, 108, 22, nombre, detalle, estilo, tam=7)

    l.caja(0, 6, 150, 38, "Sitio de contingencia", "clúster réplica de PostgreSQL\ny un servidor de modelos", "neutro")
    l.caja(162, 6, 150, 38, "Almacenamiento de objetos", "backups continuos de\nPostgreSQL (Barman Cloud)", "datos")
    l.caja(324, 6, ancho - 324, 38, "Vault", "secretos que lee\nExternal Secrets", "ops")
    return l.dibujo


def datos(ancho: float) -> Drawing:
    l = Lienzo(ancho, 486)  # noqa: E741
    l.panel(0, 238, ancho, 248, "Atención: contactos, conversaciones y mensajes")
    personas = l.tabla_er(8, 466, 122, "persons", [("id", "PK"), ("created_at", "")])
    contactos = l.tabla_er(
        8,
        405,
        122,
        "contacts",
        [
            ("id", "PK"),
            ("person_id", "FK"),
            ("channel", ""),
            ("account_id", ""),
            ("address_hash", "HMAC"),
            ("address_enc", "cifrado"),
            ("opted_out", ""),
            ("erase_requested_at", ""),
            ("last_seen_at", ""),
        ],
    )
    l.tabla_er(8, 286, 122, "link_codes", [("code_hash", "PK"), ("contact_id", "FK"), ("expires_at", "")])
    l.tabla_er(
        174,
        425,
        122,
        "conversations",
        [
            ("id", "PK"),
            ("contact_id", "FK"),
            ("channel", ""),
            ("state", ""),
            ("assigned_to", ""),
            ("no_info_count", ""),
            ("thread_ref", ""),
            ("subject", ""),
            ("last_activity_at", ""),
            ("closed_at", ""),
        ],
    )
    mensajes = l.tabla_er(
        340,
        466,
        122,
        "messages",
        [
            ("id", "PK"),
            ("seq", ""),
            ("conversation_id", "FK"),
            ("contact_id", "FK"),
            ("reply_to_id", "FK"),
            ("direction", ""),
            ("author", ""),
            ("kind", ""),
            ("body_enc", "cifrado"),
            ("provider_message_id", ""),
            ("status", ""),
            ("attempts", ""),
            ("lease_until", ""),
            ("kb_version", ""),
            ("created_at", ""),
        ],
    )

    def cardinalidad(x: float, y: float, texto: str) -> None:
        l.texto(x, y, texto, 6.6, NEGRITA, colors.HexColor("#15803D"), "middle")

    l.flecha([(69, personas[1]), (69, 405)])
    cardinalidad(76, personas[1] - 8, "1")
    cardinalidad(76, 408, "N")
    l.flecha([(130, 380), (174, 380)])
    cardinalidad(136, 384, "1")
    cardinalidad(167, 384, "N")
    l.flecha([(296, 380), (340, 380)])
    cardinalidad(302, 384, "1")
    cardinalidad(333, 384, "N")
    l.flecha([(130, 314), (152, 314), (152, 300), (318, 300), (318, 330), (340, 330)])
    cardinalidad(136, 318, "1")
    cardinalidad(333, 334, "N")
    l.flecha([(69, contactos[1]), (69, 286)])
    cardinalidad(76, contactos[1] - 8, "1")
    cardinalidad(76, 289, "N")
    abajo = mensajes[1]
    l.flecha([(380, abajo), (380, abajo - 10), (430, abajo - 10), (430, abajo)])
    l.etiqueta(405, abajo - 19, "reply_to_id")
    l.texto(174, 286, "Una sola conversación abierta por contacto.", 6.6)
    l.texto(174, 276, "Un mensaje entrante por (canal, id del proveedor).", 6.6)
    l.texto(174, 266, "Las colas solo llevan el id del mensaje.", 6.6)

    l.panel(0, 124, ancho, 106, "Operación y seguridad (sin relaciones con los datos de atención)")
    l.tabla_er(
        8,
        212,
        140,
        "audit_log",
        [
            ("id", "PK"),
            ("at", ""),
            ("actor", ""),
            ("action", ""),
            ("target_type, target_id", ""),
            ("detail", "sin datos personales"),
        ],
        "ops",
    )
    l.tabla_er(
        164,
        212,
        140,
        "oauth_tokens",
        [("provider", "PK"), ("access_token_enc", "cifrado"), ("refresh_token_enc", "cifrado"), ("expires_at", "")],
        "ops",
    )
    l.tabla_er(
        320,
        212,
        ancho - 328,
        "key_rotation",
        [("target", "PK"), ("key_id", ""), ("last_id", ""), ("rotated, failed", ""), ("finished_at", "")],
        "ops",
    )

    l.panel(0, 0, ancho, 116, "Base de conocimiento y caché semántica (pgvector)")
    versiones = l.tabla_er(
        8,
        98,
        112,
        "kb_versions",
        [
            ("version", "PK"),
            ("table_name", ""),
            ("embed_model", ""),
            ("dimensions", ""),
            ("status", ""),
            ("source_digest", ""),
        ],
        "modelo",
    )
    activa = l.tabla_er(
        140, 98, 90, "kb_active", [("singleton", "PK"), ("version", "FK"), ("updated_at", "")], "modelo"
    )
    fragmentos = l.tabla_er(
        250,
        98,
        100,
        "kb_chunks_<versión>",
        [("id", "PK"), ("question", ""), ("content", ""), ("url, category", ""), ("embedding", "HNSW")],
        "modelo",
    )
    l.tabla_er(
        366,
        98,
        ancho - 374,
        "semantic_cache_d1024",
        [("answer_key", "PK"), ("scope", ""), ("embedding", "HNSW"), ("expires_at", "")],
        "modelo",
    )
    l.flecha([(activa[0], 76), (versiones[0] + versiones[2], 76)])
    l.flecha(
        [
            (versiones[0] + 56, versiones[1]),
            (versiones[0] + 56, 10),
            (fragmentos[0] + 50, 10),
            (fragmentos[0] + 50, fragmentos[1]),
        ],
        punteada=True,
    )
    l.etiqueta(186, 13, "table_name apunta a la tabla de su versión")
    return l.dibujo


FIGURAS: dict[str, Callable[[float], Drawing]] = {
    "arquitectura": arquitectura,
    "secuencia": secuencia,
    "estados": estados,
    "adaptador": adaptador,
    "motor": motor,
    "servidor": servidor,
    "despliegue": despliegue,
    "datos": datos,
}


def motivo_de_portada(canvas: Canvas, ancho: float, abajo: float, alto: float) -> None:
    """Red de puntos y líneas tenues en la franja oscura de la portada (siempre igual: semilla fija)."""
    azar = random.Random(11)  # noqa: S311 - decoración, no seguridad
    puntos = [(azar.uniform(ancho * 0.6, ancho - 20), azar.uniform(abajo + 30, abajo + alto - 30)) for _ in range(22)]
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#334155"))
    canvas.setLineWidth(0.6)
    for numero, (x1, y1) in enumerate(puntos):
        cercanos = sorted(puntos[numero + 1 :], key=lambda p: math.dist(p, (x1, y1)))[:2]
        for x2, y2 in cercanos:
            if math.dist((x1, y1), (x2, y2)) < 120:
                canvas.line(x1, y1, x2, y2)
    for x, y in puntos:
        canvas.setFillColor(colors.HexColor("#0F766E") if azar.random() < 0.3 else colors.HexColor("#475569"))
        canvas.circle(x, y, azar.uniform(1.6, 3.4), stroke=0, fill=1)
    canvas.restoreState()
