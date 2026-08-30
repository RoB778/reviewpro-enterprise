# -*- coding: utf-8 -*-
"""
=============================================================================
KIT DE CAPTACIÓN — generador de QRs múltiples y hoja imprimible
=============================================================================

QUÉ RESUELVE
------------
La pestaña "Pedir reseñas" pedía una URL de Google, generaba UN QR, y ahí
acababa la cosa. Dos problemas:

  1. El hostelero recibe un PNG suelto. No sabe qué hacer con él. Lo mira, lo
     guarda en el escritorio y no llega nunca a la mesa del local.
  2. La agencia no tiene nada tangible que entregar. Vender "gestión de
     reseñas" es abstracto; entregar un pack de QRs para el local es concreto.

Este módulo genera, por cada local:

  · QR de reseñas de Google (el de siempre)
  · QR de la carta digital
  · QR de reservas (URL propia o del sistema que ya usen: TheFork, etc.)
  · QR de un enlace libre (menú del día, ofertas, redes...)

Y una HOJA A4 IMPRIMIBLE con los QRs que estén configurados, maquetada para
plastificar y dejar en la mesa. Es lo que convierte cuatro PNGs sueltos en un
entregable que la agencia puede facturar como "kit de captación".

POR QUÉ EN UN MÓDULO APARTE
----------------------------
Mismo criterio que informe_pdf.py: reportlab se importa DENTRO de las
funciones, así el motor de PDF solo se carga cuando alguien pulsa "descargar
kit". app.py no gana peso al arrancar, y la maquetación se puede probar sin
Streamlit con `python kit_captacion.py`.

CONTRATO DE DATOS
-----------------
Se le pasa un dict `enlaces` con estas claves opcionales:
    {
        "resenas": "https://g.page/...",
        "carta":   "https://mibar.com/carta",
        "reservas":"https://thefork.es/...",
        "extra":   {"etiqueta": "Menú del día", "url": "..."},
    }
Cualquiera que venga vacía o mal formada se ignora en silencio. La hoja se
adapta a lo que haya: si solo hay reseñas, se genera una tarjeta grande; si
hay cuatro, cuatro más pequeñas. Es responsabilidad de app.py preguntar solo
por los enlaces que el usuario ha guardado.
=============================================================================
"""

from io import BytesIO
from urllib.parse import urlparse


# =============================================================================
# COLORES — misma paleta que el resto de la marca
# =============================================================================
INK = "#1a2238"
BODY = "#232c47"
MUTED = "#6b7280"
LINEA = "#E3E3EA"
FONDO_SUAVE = "#F7F7F9"


def _c(hex_str):
    from reportlab.lib import colors
    return colors.HexColor(hex_str)


# =============================================================================
# VALIDACIÓN Y NORMALIZACIÓN DE URLS
# =============================================================================

def es_url_valida(url):
    """
    Comprobación mínima pero real: esquema http/https y un host razonable.

    Se evita a propósito una regex compleja. La biblioteca urlparse basta para
    descartar el 99% de lo que un usuario puede pegar por error: "www.mibar.es"
    sin http delante, "mibar.es/carta" mal copiado, una cadena vacía, etc.

    Lo que NO se valida aquí:
      · Que la URL exista de verdad (haría falta una petición HTTP, que abre
        la puerta a SSRF y ralentiza la interfaz).
      · Que sea una URL "de Google" específicamente. El usuario puede querer
        un QR a cualquier página propia, y no tenemos por qué juzgarlo.
    """
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not url:
        return False
    try:
        partes = urlparse(url)
    except Exception:
        return False
    if partes.scheme not in ("http", "https"):
        return False
    if not partes.netloc or "." not in partes.netloc:
        return False
    return True


def normalizar_url(url):
    """
    Añade https:// si el usuario pegó el dominio a pelo, y quita espacios.

    Casos típicos que arregla:
      · "www.mibar.com/carta" -> "https://www.mibar.com/carta"
      · " https://mibar.com " -> "https://mibar.com"
      · "MiBar.COM" -> "MiBar.COM" (los dominios NO se pasan a minúsculas
        porque el path SÍ es case-sensitive en muchos servidores).

    Devuelve la URL normalizada o cadena vacía si no se puede rescatar.
    """
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    if not url:
        return ""
    # Si no lleva esquema, se asume https (nadie debería servir en http en 2026,
    # y de todas formas Google no acepta enlaces de reseñas en http).
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url if es_url_valida(url) else ""


def diagnostico_qr(url):
    """
    Devuelve (nivel, mensaje) indicando si la URL dará un QR legible.

    Niveles:
      "ok"      — QR limpio, escaneable sin problema.
      "warning" — QR denso pero probable que funcione en buenas condiciones.
      "error"   — QR casi imposible de leer; hay que acortar la URL.

    Los umbrales están calibrados con los datos reales:
      ≤ 40 chars  → 29x29 módulos  (perfecto)
      ≤ 80 chars  → 37x37 módulos  (bien)
      ≤ 120 chars → 45x45 módulos  (aceptable en buenas condiciones)
      > 120 chars → 49x49 o más    (problemático)
    """
    n = len(url or "")
    if n <= 80:
        return "ok", None
    if n <= 120:
        return "warning", (
            "Este enlace es largo y generará un QR algo denso. Funcionará, "
            "pero si puedes conseguir la versión corta del enlace (pulsando "
            "'Compartir' en Google Maps y copiando el enlace corto), el QR "
            "quedará mucho más limpio."
        )
    return "error", (
        f"Este enlace tiene {n} caracteres y generará un QR muy complejo que "
        "muchos móviles no podrán leer. Para obtener un enlace corto: en "
        "Google Maps busca el local → pulsa 'Compartir' → copia el enlace "
        "corto (empieza por maps.app.goo.gl). Alternativamente, en Google "
        "Business Profile → 'Solicitar reseñas' → 'Copiar enlace' da una "
        "URL del tipo g.page/r/... que también es corta."
    )


# =============================================================================
# QR INDIVIDUAL
# =============================================================================

def generar_qr_png(url, tamano_caja=10, borde=2):
    """
    QR en PNG. Reutiliza qrcode con parámetros generosos para que aguante el
    escaneo desde móvil incluso con el papel ligeramente doblado o mal iluminado.

    tamano_caja=10 da un QR de ~330px de lado, tamaño de sobra para imprimir a
    5 cm. Un valor más bajo (los 8 originales) queda pixelado al ampliar.
    """
    # Import diferido: qrcode y Pillow solo se cargan cuando alguien pide un QR.
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(box_size=tamano_caja, border=borde,
                       error_correction=ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    imagen = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    imagen.save(buffer, format="PNG")
    return buffer.getvalue()


# =============================================================================
# HOJA IMPRIMIBLE — PDF A4 con todos los QRs configurados
# =============================================================================

# Catálogo de QRs. Cada uno tiene:
#   · clave    : cómo se identifica en el dict `enlaces`
#   · titulo   : lo que se pinta encima del QR en la hoja
#   · llamada  : el mensaje corto que va debajo, en lenguaje del cliente final
#
# El ORDEN de esta lista es también el orden de aparición en la hoja. Se pone
# reseñas primero a propósito: es el único QR que aporta valor DEVUELTA al
# negocio (los otros son servicios AL cliente).
CATALOGO_QR = [
    {
        "clave": "resenas",
        "titulo": "Déjanos tu reseña",
        "llamada": "Un minuto de tu tiempo nos ayuda muchísimo",
    },
    {
        "clave": "carta",
        "titulo": "Nuestra carta",
        "llamada": "Todos los platos, alérgenos y precios",
    },
    {
        "clave": "reservas",
        "titulo": "Reserva tu mesa",
        "llamada": "Aparta sitio en unos segundos",
    },
    {
        "clave": "extra",  # etiqueta libre — ver enriquecer_enlaces()
        "titulo": None,
        "llamada": None,
    },
]


def _preparar_qrs(enlaces):
    """
    Toma el dict `enlaces` y devuelve la lista de QRs que hay que pintar.

    Filtra silenciosamente los que no vengan o vengan mal formados. El caso
    especial es "extra", que trae su propia etiqueta y URL. Devuelve una lista
    de tuplas (titulo, llamada, url_normalizada) en el orden de CATALOGO_QR.
    """
    preparados = []
    for definicion in CATALOGO_QR:
        clave = definicion["clave"]
        if clave == "extra":
            extra = enlaces.get("extra") or {}
            etiqueta = (extra.get("etiqueta") or "").strip()
            url = normalizar_url(extra.get("url", ""))
            if url and etiqueta:
                preparados.append((etiqueta, "Escanea con tu móvil", url))
        else:
            url = normalizar_url(enlaces.get(clave, ""))
            if url:
                preparados.append((definicion["titulo"], definicion["llamada"], url))
    return preparados


def generar_hoja_imprimible(nombre_local, enlaces, color_marca=INK,
                            nombre_agencia=None):
    """
    Devuelve un PDF A4 con los QRs configurados, maquetados para imprimir.

    La distribución cambia según cuántos QRs haya:
      · 1 QR  : uno grande centrado, tipo "cartel de mesa".
      · 2 QRs : lado a lado, mitades iguales.
      · 3-4 QRs: rejilla 2x2 (el cuarto hueco queda vacío si son 3).

    Todo el texto es del "cliente final" (el comensal), no de la agencia. El
    nombre de la agencia solo aparece en un pie discreto si se pasa, y en el
    plan Individual/Free ni siquiera se pasa.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas as canvas_mod

    qrs = _preparar_qrs(enlaces)
    if not qrs:
        # Sin QRs válidos NO se genera un PDF vacío: es responsabilidad de la
        # interfaz avisar antes de llegar aquí. Lanzar la excepción hace que
        # un fallo en la interfaz se note en vez de descargar un archivo mudo.
        raise ValueError("No hay ningún enlace válido para generar el kit.")

    buffer = BytesIO()
    c = canvas_mod.Canvas(buffer, pagesize=A4)
    ancho, alto = A4
    color_ink = _c(INK)
    color_marca_rl = _c(color_marca)
    color_muted = _c(MUTED)
    color_linea = _c(LINEA)

    # --- Cabecera ---
    margen = 1.8 * cm
    y_cursor = alto - margen

    c.setFillColor(color_marca_rl)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(margen, y_cursor - 18, nombre_local)

    c.setFillColor(color_muted)
    c.setFont("Helvetica", 10)
    c.drawString(margen, y_cursor - 34,
                 "Escanea con la cámara de tu móvil")

    # Filete de marca — mismo detalle que el informe PDF, para que el kit se
    # sienta parte del mismo sistema visual.
    c.setFillColor(color_marca_rl)
    c.rect(margen, y_cursor - 46, ancho - 2 * margen, 2.5, fill=1, stroke=0)
    c.setFillColor(color_linea)
    c.rect(margen, y_cursor - 49, ancho - 2 * margen, 0.6, fill=1, stroke=0)

    y_inicio_qrs = y_cursor - 65

    # --- Rejilla de QRs ---
    # Se elige la disposición según cuántos hay. La lógica está aquí y no en
    # una tabla porque se necesita libertad para tamaños distintos: un QR
    # grande y solo NO es cuatro cuartos con los otros tres vacíos.
    y_pie = margen + 1.2 * cm
    alto_zona = y_inicio_qrs - y_pie
    ancho_zona = ancho - 2 * margen

    if len(qrs) == 1:
        celdas = [(margen, y_pie, ancho_zona, alto_zona)]
    elif len(qrs) == 2:
        w = ancho_zona / 2
        celdas = [(margen, y_pie, w, alto_zona),
                  (margen + w, y_pie, w, alto_zona)]
    else:
        # 3 o 4 QRs -> rejilla 2x2
        w = ancho_zona / 2
        h = alto_zona / 2
        celdas = [
            (margen,     y_pie + h, w, h),
            (margen + w, y_pie + h, w, h),
            (margen,     y_pie,     w, h),
            (margen + w, y_pie,     w, h),
        ]

    for i, (titulo, llamada, url) in enumerate(qrs[:len(celdas)]):
        _pintar_celda_qr(c, celdas[i], titulo, llamada, url,
                         color_ink, color_muted, color_marca_rl, color_linea)

    # --- Pie ---
    c.setFillColor(color_muted)
    c.setFont("Helvetica", 7.5)
    if nombre_agencia:
        c.drawString(margen, margen - 4,
                     f"Kit de captación · {nombre_local} · elaborado por {nombre_agencia}")
    else:
        c.drawString(margen, margen - 4,
                     f"Kit de captación · {nombre_local}")
    c.drawRightString(ancho - margen, margen - 4,
                      "Imprime en A4, plastifica y colócalo en la mesa o en la barra")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()


def _pintar_celda_qr(c, celda, titulo, llamada, url,
                     color_titulo, color_muted, color_marca, color_linea):
    """
    Pinta una tarjeta QR dentro de la celda (x, y, w, h) que se le pasa.

    Se ha sacado a función aparte porque generar_hoja_imprimible ya era larga
    y la lógica de "centrar título encima del QR y llamada debajo" se repite
    idéntica sea cual sea el tamaño de la celda: adaptarlo a otra celda es
    solo cambiar las coordenadas de entrada.
    """
    from reportlab.lib.units import cm
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M
    from reportlab.lib.utils import ImageReader

    x, y, w, h = celda
    padding = 12
    x_i, y_i, w_i, h_i = x + padding, y + padding, w - 2 * padding, h - 2 * padding

    # Fondo con borde discreto: define la tarjeta sin robar atención al QR.
    c.setFillColor(_c(FONDO_SUAVE))
    c.setStrokeColor(color_linea)
    c.setLineWidth(0.6)
    c.roundRect(x_i, y_i, w_i, h_i, 6, fill=1, stroke=1)

    # Título en la parte alta de la celda.
    c.setFillColor(color_titulo)
    c.setFont("Helvetica-Bold", 14)
    _dibujar_centrado(c, titulo or "", x_i + w_i / 2, y_i + h_i - 22)

    # Llamada (línea secundaria).
    if llamada:
        c.setFillColor(color_muted)
        c.setFont("Helvetica", 8.5)
        _dibujar_centrado(c, llamada, x_i + w_i / 2, y_i + h_i - 38)

    # QR: tan grande como quepa dentro de la tarjeta dejando aire para los
    # dos bloques de texto (título arriba, URL abajo). El tamaño se calcula
    # con el mínimo entre lo que da la altura útil y lo que da la anchura, y
    # DESPUÉS se centra tanto horizontal como verticalmente dentro del espacio
    # que queda libre. Sin ese centrado vertical, en celdas altas y estrechas
    # (dos QRs a lo alto) el QR se apoyaba en el bloque de texto de abajo y
    # dejaba un hueco enorme en la parte superior.
    espacio_texto_arriba = 55
    espacio_texto_abajo = 22
    lado_max_v = h_i - espacio_texto_arriba - espacio_texto_abajo
    lado_max_h = w_i - 30
    lado_qr = max(1.5 * cm, min(lado_max_v, lado_max_h))

    qr = qrcode.QRCode(box_size=10, border=1, error_correction=ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    imagen = qr.make_image(fill_color="black", back_color="white")
    buf_qr = BytesIO()
    imagen.save(buf_qr, format="PNG")
    buf_qr.seek(0)

    x_qr = x_i + (w_i - lado_qr) / 2
    # Centrado vertical dentro del espacio libre entre título y URL.
    espacio_vertical_libre = h_i - espacio_texto_arriba - espacio_texto_abajo
    y_qr = y_i + espacio_texto_abajo + (espacio_vertical_libre - lado_qr) / 2
    c.drawImage(ImageReader(buf_qr), x_qr, y_qr, lado_qr, lado_qr,
                preserveAspectRatio=True)

    # URL corta debajo del QR: ayuda si el QR no se lee y alguien quiere
    # teclearla, y da confianza (el usuario ve a dónde va antes de escanear).
    url_visible = url.replace("https://", "").replace("http://", "")
    if len(url_visible) > 42:
        url_visible = url_visible[:40] + "…"
    c.setFillColor(color_muted)
    c.setFont("Helvetica", 7)
    _dibujar_centrado(c, url_visible, x_i + w_i / 2, y_i + 10)


def _dibujar_centrado(c, texto, x_centro, y):
    """Pequeño helper para no repetir la aritmética de centrado en cada llamada."""
    ancho_texto = c.stringWidth(texto, c._fontname, c._fontsize)
    c.drawString(x_centro - ancho_texto / 2, y, texto)


# =============================================================================
# PRUEBA LOCAL — `python kit_captacion.py`
# =============================================================================
if __name__ == "__main__":
    # Escenarios: 1, 2, 3 y 4 QRs para verificar que la rejilla se adapta bien.
    ESCENARIOS = {
        "1_solo_resenas": {
            "resenas": "https://g.page/r/CabcXYZ123/review",
        },
        "2_resenas_carta": {
            "resenas": "https://g.page/r/CabcXYZ123/review",
            "carta": "https://larestaurada.es/carta",
        },
        "3_sin_extra": {
            "resenas": "https://g.page/r/CabcXYZ123/review",
            "carta": "https://larestaurada.es/carta",
            "reservas": "https://thefork.es/restaurante/la-restaurada",
        },
        "4_completo": {
            "resenas": "https://g.page/r/CabcXYZ123/review",
            "carta": "https://larestaurada.es/carta",
            "reservas": "https://thefork.es/restaurante/la-restaurada",
            "extra": {"etiqueta": "Menú del día",
                      "url": "https://larestaurada.es/menu-dia"},
        },
        # Caso peligroso: URLs mal escritas, deben filtrarse en silencio.
        "5_mixto_valido_e_invalido": {
            "resenas": "https://g.page/r/CabcXYZ123/review",
            "carta": "no soy una url",
            "reservas": "www.reservas.com",  # sin http, debe rescatarse
            "extra": {"etiqueta": "", "url": "https://x.com"},  # etiqueta vacía, se ignora
        },
    }

    for nombre, enlaces in ESCENARIOS.items():
        try:
            pdf = generar_hoja_imprimible(
                nombre_local="La Restaurada",
                enlaces=enlaces,
                color_marca="#1a2238",
                nombre_agencia="Agencia Ejemplo")
            assert pdf[:4] == b"%PDF"
            with open(f"kit_{nombre}.pdf", "wb") as f:
                f.write(pdf)
            print(f"  OK  {nombre:30s} {len(pdf):>7,} bytes")
        except ValueError as e:
            print(f"  --  {nombre:30s} (correctamente rechazado: {e})")
