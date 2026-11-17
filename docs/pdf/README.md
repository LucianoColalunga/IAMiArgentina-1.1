# Documentación técnica en PDF

Fuente del documento [IAMiArgentina-1.1-Documentacion-Tecnica.pdf](../IAMiArgentina-1.1-Documentacion-Tecnica.pdf).

| Archivo | Qué tiene |
|---|---|
| `capitulos/` | el texto, un Markdown por capítulo; los prefijos 90 en adelante no se numeran (conclusiones y glosario) |
| `diagramas.py` | las figuras, dibujadas como vectores con reportlab |
| `generar.py` | arma el PDF: portada, índice, encabezados, pies, numeración, tablas y marcadores |

## Generar

```bash
pip install -r docs/pdf/requirements.txt
python docs/pdf/generar.py                 # escribe docs/IAMiArgentina-1.1-Documentacion-Tecnica.pdf
python docs/pdf/generar.py /tmp/prueba.pdf # o en otra ruta
```

Usa Arial y Consolas en Windows, DejaVu en Linux o Arial en macOS; las fuentes quedan incrustadas
en el PDF.

## Escribir un capítulo

El Markdown admite un subconjunto: títulos `#`, `##` y `###`, párrafos, listas (con una línea en
blanco antes), tablas, bloques de código, citas `>` (se muestran como nota), `**negrita**`,
`*cursiva*`, `` `código` `` y enlaces. Además:

- `[[figura:nombre|Epígrafe]]` inserta una figura de `diagramas.FIGURAS`.
- `<<<` fuerza un salto de página.
- En la fila separadora de una tabla, si alguna columna tiene más de tres guiones, la cantidad de
  guiones de cada una fija su ancho relativo (`|------|--|`); con `|---|---|` el ancho se calcula
  según el contenido.
- Un párrafo que termina en dos puntos queda en la misma página que lo que presenta.

Si cambia un comportamiento descrito en el documento, se actualiza el capítulo en el mismo cambio
y se vuelve a generar el PDF.
