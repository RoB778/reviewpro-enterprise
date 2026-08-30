# -*- coding: utf-8 -*-
"""
=============================================================================
INFORME PDF DE MARCA BLANCA — motor de maquetación
=============================================================================

POR QUÉ ESTÁ EN SU PROPIO ARCHIVO
---------------------------------
El informe es el entregable que la agencia reenvía a su cliente final: es lo
que justifica la cuota. Merece un archivo propio en vez de vivir mezclado con
la lógica de negocio de app.py, por tres motivos:

  1. Todo reportlab se importa DENTRO de las funciones, así que el motor de
     maquetación solo se carga cuando alguien pulsa "generar informe". La
     arquitectura anterior ya hacía esto bien y se conserva intacta.
  2. Se puede probar sin levantar Streamlit: `python informe_pdf.py` genera
     un PDF de muestra con datos ficticios.
  3. app.py adelgaza unas 360 líneas.

QUÉ CAMBIA RESPECTO A LA VERSIÓN ANTERIOR
-----------------------------------------
  · BUG CORREGIDO: el número del Reputation Score se pintaba con el color de
    la banda (#232c47 para "Buena") sobre el fondo PDF_INK (#1a2238). Tinta
    oscura sobre tinta oscura: la cifra más importante del informe no se leía.
    Ahora la tarjeta es clara, el número va en tinta y el color de banda se
    reserva para el arco del anillo, donde sí contrasta.
  · Anillo de progreso para el score, en vez de un número suelto.
  · Desglose visual de los 4 factores del score. Ya los calculaba
    calcular_reputation_score() y no se enseñaban en ninguna parte.
  · Curva de evolución diaria a partir de creado_en, que también estaba en
    cada fila sin usarse.
  · Tarjetas KPI con deltas separados, en vez de una tabla de 4 columnas con
    los deltas metidos entre paréntesis dentro de la misma celda.
  · Cabecera y pie en TODAS las páginas, con numeración "X de Y".
  · Los extractos se recortan por palabra completa. En el informe anterior se
    leía «...y en los que el entorno deberí», cortado a mitad de palabra.
  · Tablas con filas alternas y filetes horizontales, sin rejilla completa.

Todos los gráficos están hechos con reportlab.graphics puro. Nada de pandas ni
numpy, para no repetir el segfault de pyarrow que ya se sufrió en su día.
=============================================================================
"""

from io import BytesIO


# =============================================================================
# PALETA — "Editorial Light", la identidad del resto de la app
# =============================================================================
# Se guardan como cadenas hexadecimales y se convierten dentro de las
# funciones, para no obligar a importar reportlab al cargar el módulo.
INK = "#1a2238"          # tinta / índigo de marca
BODY = "#232c47"         # gris azulado de cuerpo
MUTED = "#6b7280"        # gris cálido para notas y pies
LINEA = "#E3E3EA"        # filetes y bordes
FONDO_SUAVE = "#F7F7F9"  # tarjetas y filas alternas
FONDO_TENUE = "#F1F1F5"  # bloques destacados
POSITIVO = "#1a2238"     # barras de positivas
NEGATIVO = "#B8B7C9"     # barras de negativas


def _c(hex_str):
    """Convierte '#RRGGBB' en un color de reportlab."""
    from reportlab.lib import colors
    return colors.HexColor(hex_str)


def _color_marca_seguro(hex_str):
    """
    Decide si el color de marca de la agencia sirve como fondo con texto blanco.

    Se rechaza en dos casos, y en ambos se cae al índigo corporativo:
      1. Demasiado claro: el texto blanco encima no se leería.
      2. Demasiado saturado y brillante: queda "fosforito" y rompe la sobriedad
         del informe. El caso típico es el morado #635BFF del esquema antiguo.

    Misma lógica que ya existía en app.py, movida aquí sin cambiar el criterio
    para no alterar el aspecto de los informes de agencias ya existentes.
    """
    indigo = _c(INK)
    try:
        h = (hex_str or "").lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return indigo
    luminancia = 0.299 * r + 0.587 * g + 0.114 * b
    maximo, minimo = max(r, g, b), min(r, g, b)
    saturacion = (maximo - minimo) / maximo if maximo > 0 else 0
    if luminancia > 130:
        return indigo
    if saturacion > 0.45 and maximo > 150:
        return indigo
    return _c(f"#{h}")


def _recortar(texto, maximo=300):
    """
    Recorta respetando la palabra completa y cierra con puntos suspensivos.

    El informe anterior cortaba a pelo por número de caracteres y dejaba cosas
    como «...y en los que el entorno deberí», que en un documento que la
    agencia reenvía a su cliente queda a medio hacer.
    """
    texto = (texto or "").strip()
    if len(texto) <= maximo:
        return texto
    corte = texto[:maximo]
    ultimo_espacio = corte.rfind(" ")
    if ultimo_espacio > maximo * 0.6:
        corte = corte[:ultimo_espacio]
    return corte.rstrip(" ,.;:") + "…"


def _escapar(texto):
    """Escapa para el mini-marcado de Paragraph de reportlab."""
    return (
        str(texto or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# =============================================================================
# GRÁFICOS — reportlab.graphics puro
# =============================================================================

def grafico_donut_score(score, color_banda_hex, ancho=95, alto=95):
    """
    Anillo de progreso para el Reputation Score.

    Dos sectores anulares superpuestos: la pista completa en gris y encima el
    arco de progreso. El número va en el centro, en tinta sobre fondo claro,
    que es donde de verdad se lee (ver la nota del bug en la cabecera).
    """
    from reportlab.graphics.shapes import Drawing, Wedge, String

    d = Drawing(ancho, alto)
    cx, cy = ancho / 2, alto / 2
    radio = min(ancho, alto) / 2 - 3
    grosor = 9

    d.add(Wedge(cx, cy, radio, 0, 360, radius1=radio - grosor,
                fillColor=_c(LINEA), strokeColor=None))

    if score is not None:
        # Empieza arriba (90°) y avanza en sentido horario, como se lee
        # cualquier indicador de progreso.
        barrido = 360.0 * max(0, min(100, score)) / 100.0
        if barrido > 0:
            d.add(Wedge(cx, cy, radio, 90 - barrido, 90, radius1=radio - grosor,
                        fillColor=_c(color_banda_hex), strokeColor=None))
        d.add(String(cx, cy - 5, str(score), fontName="Helvetica-Bold",
                     fontSize=21, fillColor=_c(INK), textAnchor="middle"))
        d.add(String(cx, cy - 17, "/ 100", fontName="Helvetica",
                     fontSize=7, fillColor=_c(MUTED), textAnchor="middle"))
    else:
        d.add(String(cx, cy - 4, "—", fontName="Helvetica-Bold",
                     fontSize=18, fillColor=_c(MUTED), textAnchor="middle"))
    return d


def grafico_factores_score(factores, pesos, ancho=250, alto=95):
    """
    Desglose del score en sus cuatro factores, como barras de progreso.

    Estos datos ya los devolvía calcular_reputation_score() y no se enseñaban
    en ningún sitio. Son justo lo que convierte un número opaco ("74") en algo
    que la agencia puede explicar en una reunión: de dónde sale y qué palanca
    hay que mover para subirlo.
    """
    from reportlab.graphics.shapes import Drawing, Rect, String

    d = Drawing(ancho, alto)
    if not factores:
        d.add(String(0, alto / 2, "Sin datos suficientes para el desglose.",
                     fontName="Helvetica", fontSize=8, fillColor=_c(MUTED)))
        return d

    orden = ["Sentimiento", "Volumen", "Constancia", "Tendencia"]
    presentes = [k for k in orden if k in factores] or list(factores.keys())

    x_barra = 68
    ancho_barra = ancho - x_barra - 34
    alto_barra = 7
    paso = alto / max(len(presentes), 1)

    for i, clave in enumerate(presentes):
        obtenido = float(factores.get(clave, 0) or 0)
        maximo = float(pesos.get(clave.lower(), 0) or 0)
        y = alto - (i + 1) * paso + (paso - alto_barra) / 2

        d.add(String(0, y + 1, clave, fontName="Helvetica",
                     fontSize=7.5, fillColor=_c(BODY)))
        d.add(Rect(x_barra, y, ancho_barra, alto_barra,
                   fillColor=_c(LINEA), strokeColor=None))
        if maximo > 0:
            ratio = max(0.0, min(1.0, obtenido / maximo))
            if ratio > 0:
                d.add(Rect(x_barra, y, ancho_barra * ratio, alto_barra,
                           fillColor=_c(INK), strokeColor=None))
        d.add(String(x_barra + ancho_barra + 5, y + 1,
                     f"{obtenido:.0f}/{maximo:.0f}", fontName="Helvetica",
                     fontSize=7, fillColor=_c(MUTED)))
    return d


def grafico_evolucion(pares_fecha_valor, ancho=460, alto=110):
    """
    Curva de actividad diaria: área rellena, línea y puntos.

    Se alimenta de creado_en, que ya venía en cada fila del histórico y no se
    aprovechaba. Responde a "¿se ha trabajado de forma sostenida o todo de
    golpe el último día?", que es exactamente lo que mide el factor Constancia
    y que hasta ahora nadie podía ver.
    """
    from reportlab.graphics.shapes import (Drawing, String, Line, Polygon,
                                           PolyLine, Circle)

    d = Drawing(ancho, alto)
    if not pares_fecha_valor:
        d.add(String(0, alto / 2, "Sin actividad registrada en el periodo.",
                     fontName="Helvetica", fontSize=8, fillColor=_c(MUTED)))
        return d

    margen_izq, margen_inf, margen_sup = 24, 18, 10
    w = ancho - margen_izq - 8
    h = alto - margen_inf - margen_sup

    maximo = max(v for _, v in pares_fecha_valor) or 1
    pasos = 4 if maximo > 4 else int(maximo)
    pasos = max(1, pasos)
    techo = int((int(maximo) + pasos - 1) // pasos) * pasos
    techo = max(techo, pasos)

    for i in range(pasos + 1):
        y = margen_inf + h * i / pasos
        d.add(Line(margen_izq, y, margen_izq + w, y,
                   strokeColor=_c(LINEA), strokeWidth=0.5))
        d.add(String(margen_izq - 5, y - 2.5, str(int(techo * i / pasos)),
                     fontName="Helvetica", fontSize=6.5,
                     fillColor=_c(MUTED), textAnchor="end"))

    n = len(pares_fecha_valor)
    paso_x = w / max(n - 1, 1) if n > 1 else 0
    puntos = []
    for i, (_, v) in enumerate(pares_fecha_valor):
        x = margen_izq + (i * paso_x if n > 1 else w / 2)
        y = margen_inf + (h * v / techo if techo else 0)
        puntos.append((x, y))

    if len(puntos) > 1:
        coords = [puntos[0][0], margen_inf]
        for x, y in puntos:
            coords.extend([x, y])
        coords.extend([puntos[-1][0], margen_inf])
        d.add(Polygon(coords, fillColor=_c(FONDO_TENUE), strokeColor=None))

        planos = []
        for x, y in puntos:
            planos.extend([x, y])
        d.add(PolyLine(planos, strokeColor=_c(INK), strokeWidth=1.4))

    # Los puntos solo si son pocos; si no, se convierten en un collar ilegible.
    if n <= 20:
        for x, y in puntos:
            d.add(Circle(x, y, 2, fillColor=_c(INK),
                         strokeColor=_c("#FFFFFF"), strokeWidth=0.8))

    salto = max(1, n // 6)
    for i, (etiqueta, _) in enumerate(pares_fecha_valor):
        if i % salto == 0 or i == n - 1:
            x = margen_izq + (i * paso_x if n > 1 else w / 2)
            d.add(String(x, 6, etiqueta, fontName="Helvetica", fontSize=6.5,
                         fillColor=_c(MUTED), textAnchor="middle"))

    d.add(Line(margen_izq, margen_inf, margen_izq + w, margen_inf,
               strokeColor=_c("#CFCFD8"), strokeWidth=0.8))
    return d


def grafico_barras_locales(categorias, positivas, negativas, ancho=460, alto=125):
    """
    Barras agrupadas por local, dibujadas coordenada a coordenada.

    Sustituye al VerticalBarChart de reportlab, que en el informe anterior
    salía descolocado y con la leyenda pisando el borde del lienzo. Aquí se
    controla cada posición, se etiqueta el valor encima de cada barra y los
    nombres largos de local se recortan en vez de solaparse entre ellos.
    """
    from reportlab.graphics.shapes import Drawing, Rect, String, Line

    d = Drawing(ancho, alto)
    if not categorias:
        d.add(String(0, alto / 2, "Sin actividad por local en el periodo.",
                     fontName="Helvetica", fontSize=8, fillColor=_c(MUTED)))
        return d

    margen_izq, margen_inf, margen_sup = 24, 24, 20
    w = ancho - margen_izq - 8
    h = alto - margen_inf - margen_sup

    pico = max(max(positivas or [0]), max(negativas or [0]), 1)
    pasos = 4 if pico > 4 else int(pico)
    pasos = max(1, pasos)
    techo = int((int(pico) + pasos - 1) // pasos) * pasos
    techo = max(techo, pasos)

    for i in range(pasos + 1):
        y = margen_inf + h * i / pasos
        d.add(Line(margen_izq, y, margen_izq + w, y,
                   strokeColor=_c(LINEA), strokeWidth=0.5))
        d.add(String(margen_izq - 5, y - 2.5, str(int(techo * i / pasos)),
                     fontName="Helvetica", fontSize=6.5,
                     fillColor=_c(MUTED), textAnchor="end"))

    n = len(categorias)
    ancho_grupo = w / n
    ancho_barra = max(6, min(18, (ancho_grupo - 10) / 2))

    for i, nombre in enumerate(categorias):
        centro = margen_izq + ancho_grupo * (i + 0.5)
        for desplazamiento, valor, color in (
            (-ancho_barra - 1, positivas[i], POSITIVO),
            (1, negativas[i], NEGATIVO),
        ):
            altura = h * valor / techo if techo else 0
            x = centro + desplazamiento
            if altura > 0:
                d.add(Rect(x, margen_inf, ancho_barra, altura,
                           fillColor=_c(color), strokeColor=None))
            if valor:
                d.add(String(x + ancho_barra / 2, margen_inf + altura + 3,
                             str(valor), fontName="Helvetica-Bold",
                             fontSize=6.5, fillColor=_c(BODY),
                             textAnchor="middle"))

        etiqueta = nombre if len(nombre) <= 18 else nombre[:17] + "…"
        d.add(String(centro, 12, etiqueta, fontName="Helvetica", fontSize=6.8,
                     fillColor=_c(BODY), textAnchor="middle"))

    d.add(Line(margen_izq, margen_inf, margen_izq + w, margen_inf,
               strokeColor=_c("#CFCFD8"), strokeWidth=0.8))

    lx, ly = margen_izq + w - 108, alto - 9
    d.add(Rect(lx, ly, 7, 7, fillColor=_c(POSITIVO), strokeColor=None))
    d.add(String(lx + 11, ly + 1, "Positivas", fontName="Helvetica",
                 fontSize=7, fillColor=_c(BODY)))
    d.add(Rect(lx + 55, ly, 7, 7, fillColor=_c(NEGATIVO), strokeColor=None))
    d.add(String(lx + 66, ly + 1, "Negativas", fontName="Helvetica",
                 fontSize=7, fillColor=_c(BODY)))
    return d


def grafico_barras_equipo(pares_nombre_valor, ancho=460, alto=None):
    """
    Reparto de trabajo por usuario, en barras horizontales.

    Horizontal y no vertical a propósito: los nombres de persona son largos y
    en vertical se solapan o hay que girarlos, que es justo lo que hace que un
    informe parezca casero.
    """
    from reportlab.graphics.shapes import Drawing, Rect, String

    filas = pares_nombre_valor[:8]
    alto_fila = 16
    alto = alto or max(30, len(filas) * alto_fila + 8)

    d = Drawing(ancho, alto)
    if not filas:
        d.add(String(0, alto / 2, "Sin datos de usuario para este periodo.",
                     fontName="Helvetica", fontSize=8, fillColor=_c(MUTED)))
        return d

    x_barra = 120
    disponible = ancho - x_barra - 30
    maximo = max(v for _, v in filas) or 1

    for i, (nombre, valor) in enumerate(filas):
        y = alto - (i + 1) * alto_fila + 4
        etiqueta = nombre if len(nombre) <= 24 else nombre[:23] + "…"
        d.add(String(0, y + 1, etiqueta, fontName="Helvetica", fontSize=7.5,
                     fillColor=_c(BODY)))
        d.add(Rect(x_barra, y, disponible, 8,
                   fillColor=_c(FONDO_SUAVE), strokeColor=None))
        largo = disponible * valor / maximo
        if largo > 0:
            d.add(Rect(x_barra, y, largo, 8, fillColor=_c(INK), strokeColor=None))
        d.add(String(x_barra + disponible + 6, y + 1, str(valor),
                     fontName="Helvetica-Bold", fontSize=7.5, fillColor=_c(BODY)))
    return d


# =============================================================================
# CABECERA Y PIE DE PÁGINA
# =============================================================================

def _crear_pintor_pagina(nombre_agencia, periodo_texto, color_marca):
    """
    Devuelve la función que reportlab llama al cerrar cada página.

    El informe anterior no tenía cabecera ni pie más allá de la primera página:
    a partir de la segunda las hojas quedaban sueltas, sin marca y sin numerar.
    En un documento que se imprime y se deja encima de una mesa, eso es
    exactamente lo que lo hace parecer un borrador.
    """
    from reportlab.lib.units import cm

    def pintar(canvas, doc):
        canvas.saveState()
        ancho, alto = doc.pagesize

        # Cabecera solo a partir de la página 2: en la primera va el logo
        # grande y repetir la marca sería redundante.
        if doc.page > 1:
            canvas.setFont("Helvetica-Bold", 8)
            canvas.setFillColor(color_marca)
            canvas.drawString(2 * cm, alto - 1.15 * cm, nombre_agencia.upper())
            canvas.setFont("Helvetica", 7.5)
            canvas.setFillColor(_c(MUTED))
            canvas.drawRightString(ancho - 2 * cm, alto - 1.15 * cm,
                                   "Informe de reputación online")
            canvas.setStrokeColor(_c(LINEA))
            canvas.setLineWidth(0.6)
            canvas.line(2 * cm, alto - 1.35 * cm, ancho - 2 * cm, alto - 1.35 * cm)

        canvas.setStrokeColor(_c(LINEA))
        canvas.setLineWidth(0.6)
        canvas.line(2 * cm, 1.35 * cm, ancho - 2 * cm, 1.35 * cm)
        canvas.setFont("Helvetica", 6.8)
        canvas.setFillColor(_c(MUTED))
        canvas.drawString(2 * cm, 0.95 * cm, periodo_texto)

        # El "X de Y" NO se pinta aquí: el total de páginas todavía no se
        # conoce. Lo estampa _LienzoNumerado al guardar el documento.
        canvas.restoreState()

    return pintar


def _crear_lienzo_numerado(margen_lateral):
    """
    Lienzo que sabe cuántas páginas tiene el documento.

    reportlab no conoce el total hasta que termina de componer, así que el
    primer intento fue construir el PDF dos veces: una para contar y otra para
    escribir "3 de 7". Mala idea: platypus MUTA los flowables mientras compone
    (parte tablas, envuelve dibujos), así que la segunda pasada recibía objetos
    ya usados y reventaba con LayoutError de forma intermitente.

    Este enfoque compone UNA sola vez: se guarda el estado de cada página y el
    número se estampa al final, cuando ya se sabe el total.
    """
    from reportlab.pdfgen import canvas as canvas_mod
    from reportlab.lib.units import cm

    class _LienzoNumerado(canvas_mod.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._paginas_guardadas = []

        def showPage(self):
            self._paginas_guardadas.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._paginas_guardadas)
            for estado in self._paginas_guardadas:
                self.__dict__.update(estado)
                self._numerar(total)
                super().showPage()
            super().save()

        def _numerar(self, total):
            ancho = self._pagesize[0]
            self.saveState()
            self.setFont("Helvetica", 6.8)
            self.setFillColor(_c(MUTED))
            self.drawRightString(ancho - margen_lateral, 0.95 * cm,
                                 f"{self._pageNumber} de {total}")
            self.restoreState()

    return _LienzoNumerado


# =============================================================================
# INFORME
# =============================================================================

def generar_informe_pdf_mensual(agencia, historico, historico_anterior, locales_agencia,
                                id_a_nombre_usuario, contenido_seo_periodo, periodo_texto,
                                cliente_ia=None, resultado_score=None, dias_periodo=30,
                                roi=None, roi_estrellas_actuales=None,
                                roi_estrellas_objetivo=None,
                                calcular_reputation_score=None,
                                etiqueta_reputation_score=None,
                                generar_resumen_ejecutivo_ia=None, fmt_eur=None,
                                pesos_score=None, es_marca_blanca=True):
    """
    Genera el informe PDF y devuelve sus bytes.

    Los parámetros hasta roi_estrellas_objetivo son idénticos a los de la
    versión anterior, para no tocar el punto de llamada. Los siguientes son
    funciones que se inyectan desde app.py para que este módulo no tenga que
    importar app.py y crear una dependencia circular.

    es_marca_blanca: en el plan Individual el informe menciona a Reselia; en el
    resto lleva solo la marca del cliente. Aquí no se decide nada sobre planes,
    se respeta lo que venga de app.py.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, Image as RLImage, KeepTogether)

    fmt_eur = fmt_eur or (lambda n: f"{n} €")
    pesos_score = pesos_score or {"sentimiento": 50, "volumen": 20,
                                  "constancia": 20, "tendencia": 10}

    color_marca = _color_marca_seguro(agencia.get("color_marca", INK))

    # Ancho útil REAL. Ojo: no es "A4 menos los márgenes". SimpleDocTemplate
    # crea su frame con 6pt de padding por lado que no se pueden desactivar
    # desde aquí, así que el espacio de verdad disponible es 12pt menos.
    # Poner 17 cm a pelo hacía que cualquier gráfico de ancho completo midiera
    # 481,9pt dentro de un frame de 469,9pt y reportlab abortara con
    # "LayoutError: flowable too large". Solo se notaba en informes con
    # gráficos anchos, así que era una bomba de relojería para producción.
    MARGEN_LATERAL = 2 * cm
    PADDING_FRAME = 6  # por lado, valor por defecto de reportlab
    ANCHO = A4[0] - 2 * MARGEN_LATERAL - 2 * PADDING_FRAME

    base = getSampleStyleSheet()
    est_titulo = ParagraphStyle("T", parent=base["Title"], fontSize=21, leading=25,
                                textColor=color_marca, alignment=0, spaceAfter=2)
    est_subtitulo = ParagraphStyle("St", parent=base["Normal"], fontSize=9.5,
                                   leading=13, textColor=_c(MUTED))
    est_seccion = ParagraphStyle("Sec", parent=base["Heading2"], fontSize=12,
                                 leading=15, textColor=_c(INK),
                                 spaceBefore=16, spaceAfter=5)
    est_intro = ParagraphStyle("Intro", parent=base["Normal"], fontSize=8.5,
                               leading=12, textColor=_c(MUTED), spaceAfter=6)
    est_resumen = ParagraphStyle("Res", parent=base["Normal"], fontSize=11,
                                 leading=16, textColor=_c(INK))
    est_nota = ParagraphStyle("N", parent=base["Normal"], fontSize=7.2,
                              leading=10, textColor=_c(MUTED), spaceBefore=4)
    est_cita = ParagraphStyle("Cita", parent=base["Normal"], fontSize=9,
                              leading=13, textColor=_c(BODY),
                              leftIndent=10, rightIndent=6)
    est_cita_label = ParagraphStyle("CL", parent=base["Normal"], fontSize=7,
                                    leading=10, textColor=_c(MUTED),
                                    leftIndent=10, spaceAfter=1)
    est_kpi_valor = ParagraphStyle("KV", parent=base["Normal"], fontSize=19,
                                   leading=22, textColor=_c(INK), alignment=1)
    est_kpi_label = ParagraphStyle("KL", parent=base["Normal"], fontSize=6.8,
                                   leading=9, textColor=_c(MUTED), alignment=1)
    est_kpi_delta = ParagraphStyle("KD", parent=base["Normal"], fontSize=7,
                                   leading=9, textColor=_c(MUTED), alignment=1)
    est_banda = ParagraphStyle("B", parent=base["Normal"], fontSize=8,
                               leading=11, textColor=_c(INK), alignment=1)
    est_mini = ParagraphStyle("Mini", parent=base["Normal"], fontSize=8,
                              leading=11, textColor=_c(MUTED))

    story = []

    # -------------------------------------------------------------------------
    # 1. CABECERA
    # -------------------------------------------------------------------------
    imagen_logo = None
    try:
        import requests
        from PIL import Image as PILImage
        resp = requests.get(agencia.get("logo_url") or "", timeout=5)
        logo = PILImage.open(BytesIO(resp.content))
        # Aplanar transparencia: un PNG con canal alfa puede reventar más tarde
        # dentro de doc.build(), fuera de este try, y dejar un PDF corrupto.
        if logo.mode in ("RGBA", "LA", "P"):
            fondo = PILImage.new("RGB", logo.size, (255, 255, 255))
            conv = logo.convert("RGBA")
            fondo.paste(conv, mask=conv.split()[-1])
            logo = fondo
        else:
            logo = logo.convert("RGB")

        px_w, px_h = logo.size
        prop = px_h / px_w if px_w else 0.4
        ancho_logo = 4.2 * cm
        alto_logo = ancho_logo * prop
        if alto_logo > 2.0 * cm:
            alto_logo = 2.0 * cm
            ancho_logo = alto_logo / prop if prop else 4.2 * cm

        buf = BytesIO()
        logo.save(buf, format="PNG")
        buf.seek(0)
        imagen_logo = RLImage(buf, width=ancho_logo, height=alto_logo)
        imagen_logo.hAlign = "LEFT"
    except Exception:
        imagen_logo = None

    bloque_titulo = [
        Paragraph("Informe de reputación online", est_titulo),
        Paragraph(f"{_escapar(agencia.get('nombre_agencia', ''))} &nbsp;·&nbsp; "
                  f"{_escapar(periodo_texto)}", est_subtitulo),
    ]

    if imagen_logo is not None:
        cab = Table([[imagen_logo, bloque_titulo]],
                    colWidths=[ANCHO * 0.30, ANCHO * 0.70])
        cab.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("RIGHTPADDING", (1, 0), (1, 0), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(cab)
    else:
        story.extend(bloque_titulo)

    story.append(Spacer(1, 8))
    # Filete de marca: un trazo grueso en color de agencia sobre uno fino gris.
    filete = Table([[""], [""]], colWidths=[ANCHO], rowHeights=[2.2, 0.6])
    filete.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), color_marca),
        ("BACKGROUND", (0, 1), (0, 1), _c(LINEA)),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(filete)
    story.append(Spacer(1, 14))

    # -------------------------------------------------------------------------
    # 2. MÉTRICAS
    # -------------------------------------------------------------------------
    total = len(historico)
    positivas = sum(1 for r in historico if r.get("sentimiento") == "positivo")
    negativas = total - positivas
    pct = round(positivas / total * 100) if total else 0

    total_ant = len(historico_anterior or [])
    pct_ant = (round(sum(1 for r in historico_anterior
                         if r.get("sentimiento") == "positivo") / total_ant * 100)
               if total_ant else None)

    def delta_txt(actual, anterior, sufijo=""):
        """Delta legible. Sin verde/rojo: el informe se mantiene sobrio."""
        if anterior is None:
            return "—"
        d = actual - anterior
        if d > 0:
            return f"▲ +{d}{sufijo}"
        if d < 0:
            return f"▼ {d}{sufijo}"
        return "= sin cambio"

    id_a_nombre_local = {l["id"]: l["nombre"] for l in (locales_agencia or [])}
    conteo_pos, conteo_neg = {}, {}
    for fila in historico:
        nombre = id_a_nombre_local.get(fila.get("local_id"), "Local desconocido")
        if fila.get("sentimiento") == "positivo":
            conteo_pos[nombre] = conteo_pos.get(nombre, 0) + 1
        else:
            conteo_neg[nombre] = conteo_neg.get(nombre, 0) + 1
    locales_activos = sorted(set(conteo_pos) | set(conteo_neg),
                             key=lambda n: conteo_pos.get(n, 0) + conteo_neg.get(n, 0),
                             reverse=True)
    local_principal = locales_activos[0] if locales_activos else None

    if resultado_score is None and calcular_reputation_score:
        resultado_score = calcular_reputation_score(historico, historico_anterior,
                                                    dias_periodo)
    resultado_score = resultado_score or {}
    score_valor = resultado_score.get("score")
    if etiqueta_reputation_score:
        banda, color_banda = etiqueta_reputation_score(score_valor)
    else:
        banda, color_banda = ("Sin datos", MUTED)

    # -------------------------------------------------------------------------
    # 3. HERO: score + desglose
    # -------------------------------------------------------------------------
    detalle = resultado_score.get("detalle", {}) or {}
    lineas = []
    if detalle.get("dias_con_actividad") is not None:
        n_dias = detalle["dias_con_actividad"]
        lineas.append(f"Actividad en {n_dias} día{'s' if n_dias != 1 else ''} distintos")
    if detalle.get("delta_pct_positivas") is not None:
        dd = detalle["delta_pct_positivas"]
        lineas.append(f"Sentimiento {'+' if dd > 0 else ''}{dd} pts vs. periodo anterior")

    izquierda = [
        Paragraph("REPUTATION SCORE", est_kpi_label),
        Spacer(1, 4),
        grafico_donut_score(score_valor, color_banda),
        Spacer(1, 4),
        Paragraph(f"<b>{_escapar(banda)}</b>", est_banda),
    ]
    derecha = [
        Paragraph("Cómo se compone", est_mini),
        Spacer(1, 6),
        grafico_factores_score(resultado_score.get("factores", {}), pesos_score),
    ]
    if lineas:
        derecha.append(Spacer(1, 4))
        derecha.append(Paragraph(" · ".join(lineas), est_nota))

    hero = Table([[izquierda, derecha]], colWidths=[ANCHO * 0.34, ANCHO * 0.66])
    hero.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _c(FONDO_SUAVE)),
        ("BOX", (0, 0), (-1, -1), 0.7, _c(LINEA)),
        ("LINEAFTER", (0, 0), (0, 0), 0.7, _c(LINEA)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(hero)
    story.append(Spacer(1, 12))

    # -------------------------------------------------------------------------
    # 4. RESUMEN EJECUTIVO
    # -------------------------------------------------------------------------
    if generar_resumen_ejecutivo_ia:
        resumen = generar_resumen_ejecutivo_ia(cliente_ia, total, positivas, negativas,
                                               pct, local_principal,
                                               len(locales_agencia or []))
    else:
        resumen = (f"Se gestionaron {total} reseñas en el periodo, "
                   f"con un {pct}% de carácter positivo.")

    bloque = Table([[Paragraph(_escapar(resumen), est_resumen)]], colWidths=[ANCHO])
    bloque.setStyle(TableStyle([
        ("LINEBEFORE", (0, 0), (0, 0), 2.5, color_marca),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(bloque)
    story.append(Spacer(1, 16))

    # -------------------------------------------------------------------------
    # 5. TARJETAS KPI
    # -------------------------------------------------------------------------
    # Antes era una tabla de 4 columnas con el delta metido entre paréntesis en
    # la misma celda que el número. Separarlos deja respirar a la cifra y el
    # delta se lee como lo que es: contexto.
    def kpi(valor, etiqueta, delta=None):
        celda = [Paragraph(f"<b>{valor}</b>", est_kpi_valor),
                 Paragraph(etiqueta.upper(), est_kpi_label)]
        if delta is not None:
            celda.append(Spacer(1, 2))
            celda.append(Paragraph(delta, est_kpi_delta))
        return celda

    tabla_kpi = Table([[
        kpi(total, "Respuestas generadas",
            delta_txt(total, total_ant if historico_anterior else None)),
        kpi(positivas, "Reseñas positivas"),
        kpi(negativas, "Reseñas negativas"),
        kpi(f"{pct}%", "Positivas sobre el total", delta_txt(pct, pct_ant, " pts")),
    ]], colWidths=[ANCHO / 4] * 4)
    tabla_kpi.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.7, _c(LINEA)),
        ("INNERGRID", (0, 0), (-1, -1), 0.7, _c(LINEA)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(tabla_kpi)
    if not historico_anterior:
        story.append(Paragraph(
            "Todavía no hay datos del periodo anterior con los que comparar. "
            "A partir del próximo informe se mostrará la evolución.", est_nota))

    # -------------------------------------------------------------------------
    # 6. ROI
    # -------------------------------------------------------------------------
    if roi and roi.get("delta_estrellas", 0) > 0:
        est_cifra = ParagraphStyle("RC", parent=base["Normal"], fontSize=15,
                                   leading=18, textColor=_c(INK), alignment=1)
        story.append(Paragraph("Potencial de ingresos", est_seccion))
        story.append(Paragraph(
            f"Impacto estimado de subir la valoración de "
            f"{roi_estrellas_actuales}★ a {roi_estrellas_objetivo}★.", est_intro))
        tabla_roi = Table([
            [Paragraph("INGRESOS EXTRA / MES", est_kpi_label),
             Paragraph("INGRESOS EXTRA / AÑO", est_kpi_label)],
            [Paragraph(f"<b>{fmt_eur(roi['mensual_min'])} – "
                       f"{fmt_eur(roi['mensual_max'])}</b>", est_cifra),
             Paragraph(f"<b>{fmt_eur(roi['anual_min'])} – "
                       f"{fmt_eur(roi['anual_max'])}</b>", est_cifra)],
        ], colWidths=[ANCHO / 2] * 2)
        tabla_roi.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _c(FONDO_TENUE)),
            ("BOX", (0, 0), (-1, -1), 0.7, _c(LINEA)),
            ("LINEAFTER", (0, 0), (0, -1), 0.7, _c(LINEA)),
            ("TOPPADDING", (0, 0), (-1, 0), 9),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
            ("TOPPADDING", (0, 1), (-1, 1), 2),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
        ]))
        story.append(tabla_roi)
        story.append(Paragraph(
            "Estimación basada en el estudio de Harvard Business School (Michael "
            "Luca): cada estrella adicional supone entre un 5% y un 9% más de "
            "ingresos en negocios independientes. Es una proyección orientativa, "
            "no una garantía.", est_nota))

    # -------------------------------------------------------------------------
    # 7. EVOLUCIÓN DIARIA
    # -------------------------------------------------------------------------
    conteo_dia = {}
    for fila in historico:
        f = fila.get("creado_en")
        if f:
            conteo_dia[str(f)[:10]] = conteo_dia.get(str(f)[:10], 0) + 1

    if len(conteo_dia) >= 2:
        story.append(KeepTogether([
            Paragraph("Evolución de la actividad", est_seccion),
            Paragraph("Respuestas gestionadas por día. Una línea sostenida indica "
                      "gestión constante; los picos aislados señalan trabajo "
                      "acumulado.", est_intro),
            grafico_evolucion([(f"{d[8:10]}/{d[5:7]}", v)
                               for d, v in sorted(conteo_dia.items())], ancho=ANCHO),
        ]))

    # -------------------------------------------------------------------------
    # 8. ACTIVIDAD POR LOCAL
    # -------------------------------------------------------------------------
    def estilo_tabla_base(filas, color_cab):
        """Filetes horizontales y filas alternas. Sin rejilla completa: una
        tabla llena de líneas verticales parece una hoja de cálculo."""
        estilo = [
            ("BACKGROUND", (0, 0), (-1, 0), color_cab),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, _c(LINEA)),
            ("BOX", (0, 0), (-1, -1), 0.7, _c(LINEA)),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ]
        for i in range(1, len(filas)):
            if i % 2 == 0:
                estilo.append(("BACKGROUND", (0, i), (-1, i), _c(FONDO_SUAVE)))
        return TableStyle(estilo)

    if locales_activos:
        story.append(Paragraph("Actividad por local", est_seccion))
        filas = [["Local", "Positivas", "Negativas", "Total"]]
        cats, s_pos, s_neg = [], [], []
        for nombre in locales_activos:
            p, n = conteo_pos.get(nombre, 0), conteo_neg.get(nombre, 0)
            filas.append([nombre, str(p), str(n), str(p + n)])
            cats.append(nombre)
            s_pos.append(p)
            s_neg.append(n)
        # repeatRows=1: si la tabla se parte entre páginas, la cabecera se
        # repite arriba. Sin esto, la segunda página empezaba con filas de
        # números sin ninguna columna que los explicara.
        t = Table(filas, colWidths=[ANCHO * 0.46] + [ANCHO * 0.18] * 3,
                  repeatRows=1)
        t.setStyle(estilo_tabla_base(filas, color_marca))
        story.append(t)
        story.append(Spacer(1, 10))
        story.append(grafico_barras_locales(cats, s_pos, s_neg, ancho=ANCHO))

    # -------------------------------------------------------------------------
    # 9. EQUIPO
    # -------------------------------------------------------------------------
    conteo_usuario = {}
    for fila in historico:
        nombre_u = id_a_nombre_usuario.get(fila.get("usuario_id"), "Usuario eliminado")
        conteo_usuario[nombre_u] = conteo_usuario.get(nombre_u, 0) + 1

    if conteo_usuario:
        # KeepTogether evita que el titular se quede solo al final de una página
        # con el gráfico saltando a la siguiente.
        story.append(KeepTogether([
            Paragraph("Reparto de trabajo por usuario", est_seccion),
            Paragraph("Respuestas generadas por cada miembro del equipo en el "
                      "periodo.", est_intro),
            grafico_barras_equipo(sorted(conteo_usuario.items(), key=lambda x: -x[1]),
                                  ancho=ANCHO),
        ]))

    # -------------------------------------------------------------------------
    # 10. CASO DESTACADO
    # -------------------------------------------------------------------------
    casos = [r for r in historico
             if r.get("sentimiento") == "negativo" and r.get("extracto_resena")]
    if casos:
        caso = max(casos, key=lambda r: r.get("longitud_palabras", 0) or 0)
        piezas = [
            Paragraph("Caso destacado del periodo", est_seccion),
            Paragraph("Ejemplo real de gestión de una reseña crítica, con la "
                      "respuesta publicada.", est_intro),
        ]
        t1 = Table([[[Paragraph("LO QUE DIJO EL CLIENTE", est_cita_label),
                      Paragraph(f"«{_escapar(_recortar(caso['extracto_resena'], 330))}»",
                                est_cita)]]], colWidths=[ANCHO])
        t1.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _c(FONDO_SUAVE)),
            ("LINEBEFORE", (0, 0), (0, 0), 2.5, _c(NEGATIVO)),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ]))
        piezas.append(t1)

        if caso.get("extracto_respuesta"):
            piezas.append(Spacer(1, 6))
            t2 = Table([[[Paragraph("CÓMO SE RESPONDIÓ", est_cita_label),
                          Paragraph(f"«{_escapar(_recortar(caso['extracto_respuesta'], 380))}»",
                                    est_cita)]]], colWidths=[ANCHO])
            t2.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.7, _c(LINEA)),
                ("LINEBEFORE", (0, 0), (0, 0), 2.5, color_marca),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]))
            piezas.append(t2)
        story.append(KeepTogether(piezas))

    # -------------------------------------------------------------------------
    # 11. CONTENIDO SEO
    # -------------------------------------------------------------------------
    story.append(Paragraph("Contenido SEO y redes generado", est_seccion))
    if contenido_seo_periodo:
        conteo_tipo = {}
        for fila in contenido_seo_periodo:
            tipo = fila.get("tipo_contenido", "Otro")
            conteo_tipo[tipo] = conteo_tipo.get(tipo, 0) + 1
        filas_seo = [["Tipo de contenido", "Piezas generadas"]] + \
                    [[t, str(n)] for t, n in sorted(conteo_tipo.items(),
                                                    key=lambda x: -x[1])]
        t_seo = Table(filas_seo, colWidths=[ANCHO * 0.7, ANCHO * 0.3],
                      repeatRows=1)
        t_seo.setStyle(estilo_tabla_base(filas_seo, color_marca))
        story.append(t_seo)
    else:
        story.append(Paragraph("No se generó contenido SEO adicional en este periodo.",
                               est_nota))

    # -------------------------------------------------------------------------
    # 12. CIERRE
    # -------------------------------------------------------------------------
    story.append(Spacer(1, 18))
    nombre_ag = _escapar(agencia.get("nombre_agencia", ""))
    if es_marca_blanca:
        cierre = (f"Informe elaborado por {nombre_ag} sobre la gestión de reputación "
                  f"online del periodo indicado. Documento de uso interno y comercial.")
    else:
        cierre = (f"Informe generado automáticamente por Reselia en nombre de "
                  f"{nombre_ag}. Documento de uso interno y comercial para justificar "
                  f"la gestión de reputación online frente a sus clientes.")
    story.append(Paragraph(cierre, est_nota))

    # -------------------------------------------------------------------------
    # CONSTRUCCIÓN
    # -------------------------------------------------------------------------
    pintor = _crear_pintor_pagina(agencia.get("nombre_agencia", ""),
                                  periodo_texto, color_marca)
    Lienzo = _crear_lienzo_numerado(MARGEN_LATERAL)

    def construir(elementos):
        buf = BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            topMargin=1.7 * cm, bottomMargin=1.9 * cm,
            leftMargin=MARGEN_LATERAL, rightMargin=MARGEN_LATERAL,
            title=f"Informe de reputación online · {agencia.get('nombre_agencia', '')}",
            author=agencia.get("nombre_agencia", ""), subject=periodo_texto)
        doc.build(elementos, onFirstPage=pintor, onLaterPages=pintor,
                  canvasmaker=Lienzo)
        buf.seek(0)
        return buf.getvalue()

    # Red de seguridad heredada de la versión anterior: el logo es el único
    # elemento externo e impredecible. Mejor un informe perfecto sin logo que
    # un archivo corrupto que el cliente no puede abrir.
    try:
        return construir(story)
    except Exception:
        if imagen_logo is not None:
            story[0:1] = bloque_titulo
        return construir(story)


# =============================================================================
# PRUEBA LOCAL — `python informe_pdf.py` genera un PDF de muestra
# =============================================================================
if __name__ == "__main__":
    import random
    from datetime import datetime, timedelta

    random.seed(7)
    hoy = datetime(2026, 8, 28)
    locales = [{"id": "l1", "nombre": "Clínica Valverde"},
               {"id": "l2", "nombre": "Clínica Valverde Norte"},
               {"id": "l3", "nombre": "Centro Dental Mediterráneo"}]
    usuarios = {"u1": "Roberto Valverde", "u2": "Ana Ruiz", "u3": "Luis Sánchez"}

    def generar(n, dias, desde):
        return [{
            "sentimiento": "positivo" if random.random() < 0.82 else "negativo",
            "local_id": random.choice(["l1", "l1", "l2", "l3"]),
            "usuario_id": random.choice(["u1", "u1", "u2", "u3"]),
            "creado_en": (desde + timedelta(days=random.randint(0, dias - 1))).isoformat(),
            "longitud_palabras": random.randint(20, 120),
        } for _ in range(n)]

    hist = generar(64, 30, hoy - timedelta(days=30))
    hist[0].update({
        "sentimiento": "negativo", "longitud_palabras": 500,
        "extracto_resena": (
            "Soy paciente de la clínica desde hace años y mi última experiencia ha "
            "sido muy negativa. No la recomendaría a nadie que espere una atención "
            "cuidadosa, personalizada y acorde con lo que cabría esperar de una "
            "clínica especializada en estética dental. El trato en recepción fue frío "
            "y tuve que esperar más de cuarenta minutos sin que nadie me informara."),
        "extracto_respuesta": (
            "Estimada señora, gracias por tomarse el tiempo de escribir con tanto "
            "detalle y con una mesura que, dadas las circunstancias que describe, no "
            "era obligada. Hay momentos en los que una persona llega a una consulta "
            "cargando algo más pesado que el motivo de la cita, y en los que el "
            "entorno debería estar a la altura. Nos gustaría revisar su caso."),
    })
    hist_ant = generar(48, 30, hoy - timedelta(days=60))

    def banda(s):
        if s is None:
            return ("Sin datos", MUTED)
        if s >= 80:
            return ("Excelente", "#1a2238")
        if s >= 60:
            return ("Buena", "#232c47")
        if s >= 40:
            return ("Mejorable", "#6b7280")
        return ("En riesgo", "#A23A34")

    pdf = generar_informe_pdf_mensual(
        agencia={"nombre_agencia": "Clínica Valverde",
                 "color_marca": "#1a2238", "logo_url": ""},
        historico=hist, historico_anterior=hist_ant, locales_agencia=locales,
        id_a_nombre_usuario=usuarios,
        contenido_seo_periodo=[{"tipo_contenido": "Post de Google Business"}] * 4 +
                              [{"tipo_contenido": "Publicación en redes"}] * 7 +
                              [{"tipo_contenido": "Artículo de blog"}] * 2,
        periodo_texto="Últimos 30 días · 29/07/2026 – 28/08/2026",
        resultado_score={"score": 74, "total": len(hist),
                         "factores": {"Sentimiento": 41.0, "Volumen": 20.0,
                                      "Constancia": 8.7, "Tendencia": 5.2},
                         "detalle": {"pct_positivas": 82, "dias_con_actividad": 18,
                                     "delta_pct_positivas": 4}},
        dias_periodo=30,
        roi={"delta_estrellas": 0.5, "mensual_min": 500, "mensual_max": 900,
             "anual_min": 6000, "anual_max": 10800},
        roi_estrellas_actuales=3.8, roi_estrellas_objetivo=4.3,
        etiqueta_reputation_score=banda,
        fmt_eur=lambda n: f"{int(round(n)):,}".replace(",", ".") + " €",
        es_marca_blanca=True)

    with open("muestra_informe.pdf", "wb") as f:
        f.write(pdf)
    print(f"OK — muestra_informe.pdf ({len(pdf):,} bytes)")
