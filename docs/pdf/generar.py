"""Genera la documentación técnica en PDF a partir de los capítulos en Markdown.

    pip install -r docs/pdf/requirements.txt
    python docs/pdf/generar.py

Lee docs/pdf/capitulos/*.md en orden y escribe docs/IAMiArgentina-1.1-Documentacion-Tecnica.pdf.
Los capítulos usan un subconjunto de Markdown: títulos (#, ##, ###), párrafos, listas de un nivel,
tablas, bloques de código, notas (>) y figuras ([[figura:nombre|Título]]), que dibuja
diagramas.py. Los archivos con prefijo 90 o mayor (conclusiones, glosario) van sin número.
"""

from __future__ import annotations

import io
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Flowable,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

import diagramas

AQUI = Path(__file__).resolve().parent
CAPITULOS = AQUI / "capitulos"
SALIDA = AQUI.parent / "IAMiArgentina-1.1-Documentacion-Tecnica.pdf"

TITULO = "IAMiArgentina 1.1"
SUBTITULO = "Documentación técnica"
VERSION = "1.1.0"
FECHA = "Noviembre de 2026"
AUTOR = "Luciano Colalunga"
DESCRIPCION = (
    "Plataforma tecnológica de inteligencia artificial local, orientada a la atención automatizada, "
    "recuperación de información y comunicación multicanal."
)
AVISO = (
    "Proyecto independiente. No es un servicio oficial de Mi Argentina ni del Estado nacional, no está "
    "vinculado a ningún organismo ni autorizado por él. Las respuestas de demostración salen de preguntas "
    "frecuentes públicas de argentina.gob.ar."
)

# ---------------------------------------------------------------------------------------- fuentes

# (carpeta, normal, negrita, cursiva, negrita cursiva, monoespaciada). Se usa la primera completa.
FUENTES = [
    ("C:/Windows/Fonts", "arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf", "consola.ttf"),
    (
        "/usr/share/fonts/truetype/dejavu",
        "DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
        "DejaVuSans-Oblique.ttf",
        "DejaVuSans-BoldOblique.ttf",
        "DejaVuSansMono.ttf",
    ),
    ("/Library/Fonts", "Arial.ttf", "Arial Bold.ttf", "Arial Italic.ttf", "Arial Bold Italic.ttf", "Courier New.ttf"),
]


def registrar_fuentes() -> None:
    """Registra Sans (con negrita y cursivas) y Mono. Si no hay ninguna, usa Vera, que trae reportlab."""
    for carpeta, *archivos in FUENTES:
        rutas = [Path(carpeta) / archivo for archivo in archivos]
        if all(ruta.exists() for ruta in rutas):
            break
    else:
        import reportlab

        vera = Path(reportlab.__file__).parent / "fonts"
        rutas = [vera / nombre for nombre in ("Vera.ttf", "VeraBd.ttf", "VeraIt.ttf", "VeraBI.ttf", "Vera.ttf")]
    for nombre, ruta in zip(("Sans", "Sans-Bold", "Sans-Italic", "Sans-BoldItalic", "Mono"), rutas, strict=True):
        pdfmetrics.registerFont(TTFont(nombre, str(ruta)))
    pdfmetrics.registerFontFamily(
        "Sans", normal="Sans", bold="Sans-Bold", italic="Sans-Italic", boldItalic="Sans-BoldItalic"
    )


# ---------------------------------------------------------------------------------------- estilos

TINTA = colors.HexColor("#1E293B")
GRIS = colors.HexColor("#64748B")
ACENTO = colors.HexColor("#0F766E")
ACENTO_CLARO = colors.HexColor("#E6F2F0")
LINEA = colors.HexColor("#CBD5E1")
CABECERA = colors.HexColor("#E2E8F0")
CEBRA = colors.HexColor("#F8FAFC")
FONDO_CODIGO = colors.HexColor("#F1F5F9")
COLOR_CODIGO = "#0B4F4A"

MARGEN_X = 2.2 * cm
MARGEN_ARRIBA = 2.5 * cm
MARGEN_ABAJO = 2.2 * cm
ANCHO_UTIL = A4[0] - 2 * MARGEN_X


def estilos() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("cuerpo", fontName="Sans", fontSize=9.8, leading=14.2, textColor=TINTA, spaceAfter=6)
    return {
        "cuerpo": base,
        "etiqueta": ParagraphStyle(
            "etiqueta", parent=base, fontName="Sans-Bold", fontSize=9.5, textColor=ACENTO, spaceAfter=4
        ),
        "h1": ParagraphStyle(
            "h1", parent=base, fontName="Sans-Bold", fontSize=22, leading=27, spaceAfter=10, textColor=TINTA
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base,
            fontName="Sans-Bold",
            fontSize=13.2,
            leading=17,
            spaceBefore=12,
            spaceAfter=6,
            keepWithNext=1,
        ),
        "h3": ParagraphStyle(
            "h3",
            parent=base,
            fontName="Sans-Bold",
            fontSize=10.6,
            leading=14,
            spaceBefore=8,
            spaceAfter=3,
            keepWithNext=1,
        ),
        "vineta": ParagraphStyle(
            "vineta", parent=base, leftIndent=15, bulletIndent=4, bulletFontName="Sans", spaceAfter=3
        ),
        "celda": ParagraphStyle("celda", parent=base, fontSize=8.5, leading=11.2, spaceAfter=0),
        "celda_cabecera": ParagraphStyle(
            "celda_cabecera", parent=base, fontName="Sans-Bold", fontSize=8.5, leading=11.2, spaceAfter=0
        ),
        "codigo": ParagraphStyle(
            "codigo", parent=base, fontName="Mono", fontSize=8.1, leading=10.8, textColor=TINTA, spaceAfter=0
        ),
        "nota": ParagraphStyle("nota", parent=base, fontSize=9.3, leading=13.2, spaceAfter=0),
        "epigrafe": ParagraphStyle(
            "epigrafe",
            parent=base,
            fontName="Sans-Italic",
            fontSize=8.5,
            leading=11,
            textColor=GRIS,
            alignment=TA_CENTER,
            spaceBefore=4,
            spaceAfter=10,
        ),
        "indice_titulo": ParagraphStyle(
            "indice_titulo", parent=base, fontName="Sans-Bold", fontSize=22, leading=27, spaceAfter=14
        ),
        "indice_0": ParagraphStyle(
            "indice_0", parent=base, fontName="Sans-Bold", fontSize=10.2, leading=14, spaceBefore=7, spaceAfter=1
        ),
        "indice_1": ParagraphStyle(
            "indice_1", parent=base, fontSize=9.2, leading=12.4, leftIndent=18, spaceAfter=0, textColor=TINTA
        ),
    }


ESTILOS: dict[str, ParagraphStyle] = {}

# -------------------------------------------------------------------------------- texto en línea

CODIGO_EN_LINEA = re.compile(r"`([^`]+)`")
ENLACE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
NEGRITA = re.compile(r"\*\*(.+?)\*\*")
CURSIVA = re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])")


def en_linea(texto: str) -> str:
    """Markdown en línea (código, enlaces, negrita, cursiva) a las marcas de los Paragraph."""
    codigos: list[str] = []
    enlaces: list[tuple[str, str]] = []

    def guardar_codigo(m: re.Match[str]) -> str:
        codigos.append(m.group(1))
        return f"\x00{len(codigos) - 1}\x00"

    def guardar_enlace(m: re.Match[str]) -> str:
        enlaces.append((m.group(1), m.group(2)))
        return f"\x01{len(enlaces) - 1}\x01"

    texto = CODIGO_EN_LINEA.sub(guardar_codigo, texto)
    texto = ENLACE.sub(guardar_enlace, texto)
    texto = escape(texto)
    texto = NEGRITA.sub(r"<b>\1</b>", texto)
    texto = CURSIVA.sub(r"<i>\1</i>", texto)

    def poner_enlace(m: re.Match[str]) -> str:
        etiqueta, url = enlaces[int(m.group(1))]
        return f'<a href="{escape(url)}" color="{ACENTO.hexval()}"><u>{escape(etiqueta)}</u></a>'

    def poner_codigo(m: re.Match[str]) -> str:
        return f'<font face="Mono" color="{COLOR_CODIGO}">{escape(codigos[int(m.group(1))])}</font>'

    texto = re.sub("\x01(\\d+)\x01", poner_enlace, texto)
    return re.sub("\x00(\\d+)\x00", poner_codigo, texto)


def sin_marcas(texto: str) -> str:
    return re.sub(r"[`*]", "", ENLACE.sub(r"\1", texto))


# ------------------------------------------------------------------------------------- Markdown


@dataclass
class Bloque:
    tipo: str  # h1, h2, h3, p, ul, ol, tabla, codigo, nota, figura, salto
    texto: str = ""
    items: list[str] = field(default_factory=list)
    filas: list[list[str]] = field(default_factory=list)
    pesos: list[int] | None = None
    inicio: int = 1


ITEM = re.compile(r"^([-*]|\d+\.)\s+(.*)$")
FIGURA = re.compile(r"^\[\[figura:([a-z0-9_-]+)\|(.+)\]\]$")
TITULO_MD = re.compile(r"^(#{1,3})\s+(.*)$")
SEPARADOR = re.compile(r":?-{3,}:?")


def partir_fila(linea: str) -> list[str]:
    interior = linea.strip().removeprefix("|").removesuffix("|")
    celdas, actual, en_codigo = [], [], False
    for caracter in interior:
        if caracter == "`":
            en_codigo = not en_codigo
        if caracter == "|" and not en_codigo:
            celdas.append("".join(actual).strip())
            actual = []
        else:
            actual.append(caracter)
    celdas.append("".join(actual).strip())
    return celdas


def leer_markdown(texto: str) -> list[Bloque]:
    lineas = texto.splitlines()
    bloques: list[Bloque] = []
    parrafo: list[str] = []

    def cerrar_parrafo() -> None:
        if parrafo:
            bloques.append(Bloque("p", " ".join(linea.strip() for linea in parrafo)))
            parrafo.clear()

    i = 0
    while i < len(lineas):
        linea = lineas[i]
        limpia = linea.strip()
        if not limpia:
            cerrar_parrafo()
            i += 1
            continue
        if limpia.startswith("```"):
            cerrar_parrafo()
            fin = i + 1
            while fin < len(lineas) and not lineas[fin].strip().startswith("```"):
                fin += 1
            bloques.append(Bloque("codigo", "\n".join(lineas[i + 1 : fin])))
            i = fin + 1
            continue
        if m := TITULO_MD.match(limpia):
            cerrar_parrafo()
            bloques.append(Bloque(f"h{len(m.group(1))}", m.group(2).strip()))
            i += 1
            continue
        if m := FIGURA.match(limpia):
            cerrar_parrafo()
            bloques.append(Bloque("figura", m.group(2), items=[m.group(1)]))
            i += 1
            continue
        if limpia == "<<<":
            cerrar_parrafo()
            bloques.append(Bloque("salto"))
            i += 1
            continue
        if limpia.startswith("|"):
            cerrar_parrafo()
            filas = []
            while i < len(lineas) and lineas[i].strip().startswith("|"):
                filas.append(partir_fila(lineas[i]))
                i += 1
            pesos = None
            if len(filas) > 1 and all(SEPARADOR.fullmatch(celda) for celda in filas[1]):
                guiones = [celda.count("-") for celda in filas.pop(1)]
                pesos = guiones if any(cantidad > 3 for cantidad in guiones) else None
            bloques.append(Bloque("tabla", filas=filas, pesos=pesos))
            continue
        if limpia.startswith(">"):
            cerrar_parrafo()
            nota = []
            while i < len(lineas) and lineas[i].strip().startswith(">"):
                nota.append(lineas[i].strip()[1:].strip())
                i += 1
            bloques.append(Bloque("nota", " ".join(nota)))
            continue
        if (m := ITEM.match(linea)) and not parrafo:
            ordenada = m.group(1)[0].isdigit()
            items: list[str] = []
            inicio = int(m.group(1)[:-1]) if ordenada else 1
            while i < len(lineas):
                actual = lineas[i]
                item = ITEM.match(actual)
                if item and item.group(1)[0].isdigit() == ordenada:
                    items.append(item.group(2).strip())
                elif actual.startswith("  ") and actual.strip() and items:
                    items[-1] += " " + actual.strip()
                else:
                    break
                i += 1
            bloques.append(Bloque("ol" if ordenada else "ul", items=items, inicio=inicio))
            continue
        parrafo.append(linea)
        i += 1
    cerrar_parrafo()
    return bloques


# ------------------------------------------------------------------------------------ elementos


def medir_celda(celda: str, fuente: str, tamano: float = 8.5) -> tuple[float, float]:
    """Ancho natural de una celda y el de su palabra más larga; el código se mide en letra mono."""
    total = 0.0
    mas_larga = 0.0
    for numero, tramo in enumerate(celda.split("`")):
        letra = "Mono" if numero % 2 else fuente
        limpio = sin_marcas(tramo)
        total += pdfmetrics.stringWidth(limpio, letra, tamano)
        for palabra in limpio.split():
            mas_larga = max(mas_larga, pdfmetrics.stringWidth(palabra, letra, tamano))
    return total, mas_larga


def anchos_de_columnas(filas: list[list[str]], pesos: list[int] | None, ancho: float) -> list[float]:
    """Columnas cortas a su ancho natural; el resto del espacio, en proporción al texto."""
    columnas = len(filas[0])
    if pesos and len(pesos) == columnas:
        return [ancho * peso / sum(pesos) for peso in pesos]
    relleno = 11.0
    natural = [0.0] * columnas
    minimo = [0.0] * columnas
    for numero, fila in enumerate(filas):
        fuente = "Sans-Bold" if numero == 0 else "Sans"
        for columna, celda in enumerate(fila[:columnas]):
            total, mas_larga = medir_celda(celda, fuente)
            natural[columna] = max(natural[columna], total + relleno)
            minimo[columna] = max(minimo[columna], min(mas_larga + relleno, ancho * 0.4))
    if sum(natural) <= ancho:
        return [n * ancho / sum(natural) for n in natural]
    fijas = {c: natural[c] for c in range(columnas) if natural[c] <= ancho * 0.2}
    flexibles = [c for c in range(columnas) if c not in fijas]
    if not flexibles:
        return [n * ancho / sum(natural) for n in natural]
    # Cada columna flexible arranca en su palabra más larga (así ninguna palabra se corta) y el
    # espacio que sobra se reparte según cuánto texto le falta para entrar en una línea.
    resto = ancho - sum(fijas.values())
    piso = sum(minimo[c] for c in flexibles)
    if resto < piso:
        anchos = [fijas.get(c, minimo[c]) for c in range(columnas)]
        return [a * ancho / sum(anchos) for a in anchos]
    falta = {c: max(natural[c] - minimo[c], 0.0) for c in flexibles}
    total = sum(falta.values()) or 1.0
    return [fijas[c] if c in fijas else minimo[c] + (resto - piso) * falta[c] / total for c in range(columnas)]


def tabla(filas: list[list[str]], pesos: list[int] | None, ancho: float = ANCHO_UTIL) -> Table:
    columnas = len(filas[0])
    filas = [fila + [""] * (columnas - len(fila)) for fila in filas]
    datos = [[Paragraph(en_linea(celda), ESTILOS["celda_cabecera"]) for celda in filas[0]]]
    datos += [[Paragraph(en_linea(celda), ESTILOS["celda"]) for celda in fila[:columnas]] for fila in filas[1:]]
    resultado = Table(datos, colWidths=anchos_de_columnas(filas, pesos, ancho), repeatRows=1, hAlign="LEFT")
    resultado.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), CABECERA),
                ("LINEBELOW", (0, 0), (-1, 0), 0.9, ACENTO),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, CEBRA]),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, LINEA),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3.2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return resultado


def bloque_de_codigo(texto: str, ancho: float = ANCHO_UTIL) -> Table:
    maximo = int((ancho - 18) / pdfmetrics.stringWidth("M", "Mono", 8.1))
    lineas: list[str] = []
    for linea in texto.splitlines():
        while len(linea) > maximo:
            corte = linea.rfind(" ", 0, maximo)
            corte = corte if corte > maximo // 2 else maximo
            lineas.append(linea[:corte] + " \\")
            linea = "    " + linea[corte:].lstrip()
        lineas.append(linea)
    caja = Table([[Preformatted("\n".join(lineas), ESTILOS["codigo"])]], colWidths=[ancho], hAlign="LEFT")
    caja.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), FONDO_CODIGO),
                ("BOX", (0, 0), (-1, -1), 0.4, LINEA),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return caja


def nota(texto: str, ancho: float = ANCHO_UTIL) -> Table:
    caja = Table([[Paragraph(en_linea(texto), ESTILOS["nota"])]], colWidths=[ancho], hAlign="LEFT")
    caja.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), ACENTO_CLARO),
                ("LINEBEFORE", (0, 0), (0, -1), 2.4, ACENTO),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return caja


class Titulo(Paragraph):
    """Título de capítulo o sección: al dibujarse registra su entrada del índice y su marcador."""

    def __init__(self, texto_md: str, estilo: ParagraphStyle, nivel: int, indice: str, capitulo: str = "") -> None:
        super().__init__(texto_md, estilo)
        self.nivel, self.indice, self.capitulo = nivel, indice, capitulo
        self.clave = "t-" + re.sub(r"[^a-z0-9]+", "-", indice.lower()).strip("-")


class Regla(Flowable):
    def __init__(self, ancho: float, color: colors.Color = ACENTO, grosor: float = 1.6, largo: float = 46) -> None:
        super().__init__()
        self.ancho, self.color, self.grosor, self.largo = ancho, color, grosor, largo

    def wrap(self, *_: float) -> tuple[float, float]:
        return self.ancho, self.grosor + 10

    def draw(self) -> None:
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.grosor)
        self.canv.line(0, 6, self.largo, 6)


# ----------------------------------------------------------------------------------- capítulos


def capitulo(numero: int | None, bloques: list[Bloque], figuras: dict[str, Callable[[float], Flowable]]) -> list:
    historia: list = []
    seccion = 0
    figura = 0
    for bloque in bloques:
        if bloque.tipo == "h1":
            titulo = sin_marcas(bloque.texto)
            texto_indice = f"{numero}. {titulo}" if numero else titulo
            encabezado = f"Capítulo {numero} · {titulo}" if numero else titulo
            if numero:
                historia.append(Paragraph(f"CAPÍTULO {numero}", ESTILOS["etiqueta"]))
            historia += [
                Titulo(en_linea(bloque.texto), ESTILOS["h1"], 0, texto_indice, encabezado),
                Regla(ANCHO_UTIL),
                Spacer(1, 8),
            ]
        elif bloque.tipo == "h2":
            seccion += 1
            numero_seccion = f"{numero}.{seccion}" if numero else ""
            texto_indice = f"{numero_seccion} {sin_marcas(bloque.texto)}".strip()
            prefijo = f'<font color="{ACENTO.hexval()}">{numero_seccion}</font>&nbsp;&nbsp;' if numero else ""
            historia += [
                CondPageBreak(3.2 * cm),
                Titulo(prefijo + en_linea(bloque.texto), ESTILOS["h2"], 1, texto_indice),
            ]
        elif bloque.tipo == "h3":
            historia += [CondPageBreak(2.4 * cm), Paragraph(en_linea(bloque.texto), ESTILOS["h3"])]
        elif bloque.tipo == "p":
            parrafo = Paragraph(en_linea(bloque.texto), ESTILOS["cuerpo"])
            if sin_marcas(bloque.texto).rstrip().endswith(":"):
                parrafo.keepWithNext = 1  # presenta lo que sigue (tabla, lista o código): van juntos
            historia.append(parrafo)
        elif bloque.tipo in ("ul", "ol"):
            for orden, item in enumerate(bloque.items, start=bloque.inicio):
                vineta = f"{orden}." if bloque.tipo == "ol" else "•"
                historia.append(Paragraph(en_linea(item), ESTILOS["vineta"], bulletText=vineta))
            historia.append(Spacer(1, 4))
        elif bloque.tipo == "tabla":
            historia += [tabla(bloque.filas, bloque.pesos), Spacer(1, 10)]
        elif bloque.tipo == "codigo":
            historia += [bloque_de_codigo(bloque.texto), Spacer(1, 9)]
        elif bloque.tipo == "nota":
            historia += [nota(bloque.texto), Spacer(1, 10)]
        elif bloque.tipo == "figura":
            figura += 1
            dibujo = figuras[bloque.items[0]](ANCHO_UTIL)
            rotulo = f"Figura {numero}.{figura}. {bloque.texto}" if numero else bloque.texto
            contenido = [Spacer(1, 4), dibujo, Paragraph(en_linea(rotulo), ESTILOS["epigrafe"])]
            if historia and isinstance(historia[-1], Titulo):
                # keepWithNext no agrupa un título con un KeepTogether: se agrupan acá.
                contenido.insert(0, historia.pop())
            historia.append(KeepTogether(contenido))
        elif bloque.tipo == "salto":
            historia.append(PageBreak())
    return historia


# ------------------------------------------------------------------------------------ páginas


class Documento(BaseDocTemplate):
    def __init__(self, destino: Path | io.BytesIO, total_paginas: int | None = None) -> None:
        super().__init__(
            str(destino) if isinstance(destino, Path) else destino,
            pagesize=A4,
            leftMargin=MARGEN_X,
            rightMargin=MARGEN_X,
            topMargin=MARGEN_ARRIBA,
            bottomMargin=MARGEN_ABAJO,
            title=f"{TITULO}: {SUBTITULO}",
            author=AUTOR,
            subject=DESCRIPCION,
            creator="docs/pdf/generar.py",
            keywords="IAMiArgentina, documentación técnica, multicanal, RAG, vLLM, Kubernetes",
            lang="es-AR",
        )
        portada = Frame(0, 0, A4[0], A4[1], leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        normal = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="normal")
        self.addPageTemplates(
            [
                PageTemplate("portada", [portada], onPage=dibujar_portada),
                PageTemplate("normal", [normal], onPageEnd=self.decorar),
            ]
        )
        self.total_paginas = total_paginas
        self.titulo_actual = ""
        self.pagina_de_apertura = 0

    def beforeDocument(self) -> None:
        # multiBuild arma el documento varias veces: cada pasada empieza sin capítulo actual.
        self.titulo_actual = ""
        self.pagina_de_apertura = 0

    def afterFlowable(self, flowable: Flowable) -> None:
        if not isinstance(flowable, Titulo):
            return
        self.canv.bookmarkPage(flowable.clave)
        self.canv.addOutlineEntry(flowable.indice, flowable.clave, level=flowable.nivel, closed=True)
        self.notify("TOCEntry", (flowable.nivel, flowable.indice, self.page, flowable.clave))
        if flowable.nivel == 0:
            self.titulo_actual = flowable.capitulo
            self.pagina_de_apertura = self.page

    def decorar(self, canvas: Canvas, _: BaseDocTemplate) -> None:
        """Encabezado y pie de las páginas comunes (la portada tiene su propio dibujo)."""
        self.encabezado(canvas)
        self.pie(canvas)

    def encabezado(self, canvas: Canvas) -> None:
        if self.page == self.pagina_de_apertura or not self.titulo_actual:
            return
        canvas.saveState()
        alto = A4[1] - MARGEN_ARRIBA + 22
        canvas.setFont("Sans", 7.8)
        canvas.setFillColor(GRIS)
        canvas.drawString(MARGEN_X, alto, f"{TITULO} · {SUBTITULO}")
        canvas.drawRightString(A4[0] - MARGEN_X, alto, self.titulo_actual)
        canvas.setStrokeColor(LINEA)
        canvas.setLineWidth(0.5)
        canvas.line(MARGEN_X, alto - 5, A4[0] - MARGEN_X, alto - 5)
        canvas.restoreState()

    def pie(self, canvas: Canvas) -> None:
        # El total sale de un armado previo: el pie no cambia el largo del documento.
        total = f" de {self.total_paginas}" if self.total_paginas else ""
        canvas.saveState()
        canvas.setStrokeColor(LINEA)
        canvas.setLineWidth(0.5)
        canvas.line(MARGEN_X, MARGEN_ABAJO - 16, A4[0] - MARGEN_X, MARGEN_ABAJO - 16)
        canvas.setFont("Sans", 7.8)
        canvas.setFillColor(GRIS)
        canvas.drawString(MARGEN_X, MARGEN_ABAJO - 27, f"Versión {VERSION} · {FECHA}")
        canvas.drawRightString(A4[0] - MARGEN_X, MARGEN_ABAJO - 27, f"Página {self.page}{total}")
        canvas.restoreState()


def dibujar_portada(canvas: Canvas, _: BaseDocTemplate) -> None:
    ancho, alto = A4
    canvas.saveState()
    canvas.setFillColor(TINTA)
    canvas.rect(0, alto * 0.52, ancho, alto * 0.48, stroke=0, fill=1)
    diagramas.motivo_de_portada(canvas, ancho, alto * 0.52, alto * 0.48)
    canvas.setFillColor(ACENTO)
    canvas.rect(0, alto * 0.52 - 6, ancho, 6, stroke=0, fill=1)

    canvas.setFillColor(colors.HexColor("#99F6E4"))
    canvas.setFont("Sans-Bold", 11)
    canvas.drawString(MARGEN_X, alto - 4.2 * cm, "DOCUMENTACIÓN TÉCNICA")
    canvas.setFillColor(colors.white)
    canvas.setFont("Sans-Bold", 40)
    canvas.drawString(MARGEN_X, alto - 6.3 * cm, TITULO)
    canvas.setFont("Sans", 15)
    canvas.setFillColor(colors.HexColor("#CBD5E1"))
    canvas.drawString(MARGEN_X, alto - 7.4 * cm, f"Versión {VERSION} · {FECHA}")

    descripcion = Paragraph(
        DESCRIPCION, ParagraphStyle("portada", fontName="Sans", fontSize=15, leading=21, textColor=TINTA)
    )
    _, alto_descripcion = descripcion.wrap(ancho - 2 * MARGEN_X - 2 * cm, 200)
    descripcion.drawOn(canvas, MARGEN_X, alto * 0.52 - 2.2 * cm - alto_descripcion)

    datos = [
        ("Autor", AUTOR),
        ("Versión", VERSION),
        ("Fecha", FECHA),
        (
            "Contenido",
            "arquitectura, integraciones, motor de IA, infraestructura, seguridad, base de datos, "
            "instalación, administración, pruebas, código y roadmap",
        ),
    ]
    y = alto * 0.52 - 2.2 * cm - alto_descripcion - 1.4 * cm
    for etiqueta, valor in datos:
        canvas.setFont("Sans-Bold", 9)
        canvas.setFillColor(ACENTO)
        canvas.drawString(MARGEN_X, y, etiqueta.upper())
        texto = Paragraph(valor, ParagraphStyle("dato", fontName="Sans", fontSize=10, leading=13.5, textColor=TINTA))
        _, alto_valor = texto.wrap(ancho - 2 * MARGEN_X - 3.2 * cm, 100)
        texto.drawOn(canvas, MARGEN_X + 3.2 * cm, y - alto_valor + 10)
        y -= max(alto_valor, 14) + 8

    aviso = Paragraph(AVISO, ParagraphStyle("aviso", fontName="Sans", fontSize=8.2, leading=11.5, textColor=GRIS))
    _, alto_aviso = aviso.wrap(ancho - 2 * MARGEN_X, 100)
    canvas.setStrokeColor(LINEA)
    canvas.setLineWidth(0.5)
    canvas.line(MARGEN_X, MARGEN_ABAJO + alto_aviso + 10, ancho - MARGEN_X, MARGEN_ABAJO + alto_aviso + 10)
    aviso.drawOn(canvas, MARGEN_X, MARGEN_ABAJO)
    canvas.restoreState()


# ------------------------------------------------------------------------------------- armado


def indice() -> list:
    toc = TableOfContents(dotsMinLevel=0, levelStyles=[ESTILOS["indice_0"], ESTILOS["indice_1"]])
    return [Paragraph("Índice", ESTILOS["indice_titulo"]), Regla(ANCHO_UTIL), Spacer(1, 6), toc]


def historia_completa(archivos: list[Path]) -> list:
    historia: list = [Spacer(1, 1), NextPageTemplate("normal"), PageBreak(), *indice()]
    numero = 0
    for archivo in archivos:
        prefijo = int(archivo.name.split("-", 1)[0])
        numero_capitulo = None if prefijo >= 90 else (numero := numero + 1)
        historia.append(PageBreak())
        historia += capitulo(numero_capitulo, leer_markdown(archivo.read_text(encoding="utf-8")), diagramas.FIGURAS)
    return historia


def armar(salida: Path = SALIDA) -> Path:
    registrar_fuentes()
    ESTILOS.update(estilos())
    archivos = sorted(CAPITULOS.glob("*.md"))
    if not archivos:
        raise SystemExit(f"No hay capítulos en {CAPITULOS}")
    # Un primer armado en memoria da el total de páginas para el pie ("Página X de Y").
    borrador = Documento(io.BytesIO())
    borrador.multiBuild(historia_completa(archivos))
    total = borrador.page
    for _ in range(3):
        documento = Documento(salida, total)
        documento.multiBuild(historia_completa(archivos))
        if documento.page == total:
            return salida
        total = documento.page
    raise SystemExit("El total de páginas no se estabilizó")


if __name__ == "__main__":
    ruta = armar(Path(sys.argv[1]) if len(sys.argv) > 1 else SALIDA)
    print(f"PDF generado: {ruta}")
