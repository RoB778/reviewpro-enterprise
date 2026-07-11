import json
import re
import sys
import traceback
import urllib.parse
from datetime import datetime, timedelta
from io import BytesIO

import bcrypt
import httpx
import qrcode
import requests
import stripe
import streamlit as st
from anthropic import Anthropic
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from supabase import create_client

# Configuración de las claves secretas de los servidores
# .strip() es crítico: un salto de línea o espacio colado en Secrets de
# Streamlit Cloud (p.ej. pegando la key con """triple comillas""" en el
# secrets.toml) provoca httpx.LocalProtocolError ("Illegal header value"),
# que el SDK de Anthropic enmascara como APIConnectionError.
_anthropic_api_key_raw = st.secrets["ANTHROPIC_API_KEY"]
_anthropic_api_key = _anthropic_api_key_raw.strip() if isinstance(_anthropic_api_key_raw, str) else _anthropic_api_key_raw

client = Anthropic(
    api_key=_anthropic_api_key,
    max_retries=3,   # reintenta automáticamente ante fallos de red transitorios
    timeout=60.0,    # más margen que el default, por si la conexión tarda en establecerse
)
supabase = create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
stripe.api_key = st.secrets["STRIPE_SECRET_KEY"]

# -----------------------------------------------------------------------
# Redacción de secretos en mensajes de error.
# Excepciones como httpx.LocalProtocolError incluyen el VALOR CRUDO de la
# cabecera rechazada dentro de str(e) — si esa cabecera es el Authorization
# con la API key, la key entera queda expuesta en pantalla y en logs.
# Por eso nunca se debe mostrar/loguear str(e) sin pasar por aquí antes.
# -----------------------------------------------------------------------
_SECRETOS_A_OCULTAR = [
    s for s in [
        _anthropic_api_key_raw,
        _anthropic_api_key,
        st.secrets.get("SUPABASE_KEY"),
        st.secrets.get("STRIPE_SECRET_KEY"),
    ] if isinstance(s, str) and s
]

_PATRONES_SECRETOS = [
    re.compile(r"sk-ant-api03-[A-Za-z0-9_\-]+"),  # Anthropic
    re.compile(r"sk_live_[A-Za-z0-9]+"),           # Stripe live secret
    re.compile(r"sk_test_[A-Za-z0-9]+"),           # Stripe test secret
    re.compile(r"rk_live_[A-Za-z0-9]+"),           # Stripe restricted key
    re.compile(r"eyJ[A-Za-z0-9_\-\.]{20,}"),        # JWT-like (Supabase)
]


def redactar_secretos(texto):
    """Sustituye cualquier secreto conocido (por valor exacto o por patrón)
    por un marcador, para que nunca se muestre ni se loguee en claro."""
    if not isinstance(texto, str):
        return texto
    resultado = texto
    for secreto in _SECRETOS_A_OCULTAR:
        resultado = resultado.replace(secreto, "[SECRETO_OCULTO]")
        resultado = resultado.replace(secreto.strip(), "[SECRETO_OCULTO]")
    for patron in _PATRONES_SECRETOS:
        resultado = patron.sub("[SECRETO_OCULTO]", resultado)
    return resultado


def log_error_completo(contexto, e):
    """
    Escribe en stderr (visible en Manage app -> Logs de Streamlit Cloud) el
    traceback COMPLETO y toda la cadena de excepciones (__cause__/__context__),
    no solo el texto resumido que se muestra con st.error(). Para errores de
    red tipo APIConnectionError, la excepción real (DNS, TLS, timeout, conexión
    rechazada...) casi siempre va colgada en e.__cause__, no en str(e).

    IMPORTANTE: algunas excepciones (p.ej. httpx.LocalProtocolError cuando
    rechaza una cabecera) incluyen el VALOR CRUDO de esa cabecera en su
    mensaje — si es el Authorization, ahí va la API key en claro. Por eso
    todo lo que se imprime o se devuelve aquí pasa por redactar_secretos().
    """
    tb_redactado = redactar_secretos(traceback.format_exc())
    print(f"\n===== ERROR EN: {contexto} =====", file=sys.stderr)
    print(tb_redactado, file=sys.stderr)

    causa = e
    cadena = []
    while causa is not None:
        cadena.append(redactar_secretos(f"{type(causa).__name__}: {causa}"))
        causa = causa.__cause__ or causa.__context__
    print("Cadena de causas: " + " -> ".join(cadena), file=sys.stderr)
    print("=====================================\n", file=sys.stderr)

    return cadena[-1] if cadena else redactar_secretos(f"{type(e).__name__}: {e}")

# URL pública de la app, necesaria para que Stripe sepa a dónde devolver al usuario tras
# el pago. OBLIGATORIA: si falta o está mal puesta, Stripe redirige a una URL que no existe
# y el usuario se queda "colgado" en la pantalla de éxito de Stripe sin volver nunca a la app.
# Configúrala en secrets.toml con la URL exacta de tu app en Streamlit Cloud, ej:
# APP_URL = "https://reviewpro-enterprise.streamlit.app"  (sin barra final)
if "APP_URL" not in st.secrets:
    st.error(
        "⚠️ Falta configurar APP_URL en los secrets de la app. Sin esto, Stripe no puede "
        "devolver al usuario tras el pago. Ve a 'Manage app' → Settings → Secrets y añade "
        "APP_URL = \"https://tu-url-real.streamlit.app\" (la URL exacta con la que accedes a tu app)."
    )
    st.stop()
APP_URL = st.secrets["APP_URL"].rstrip("/")

# 1. Configuración de página limpia y profesional
st.set_page_config(page_title="ReviewPro Enterprise", page_icon="🛡️", layout="centered")

st.markdown("""
    <style>
    #MainMenu, header, footer, .stAppDeployButton, .viewerBadge_container__1QS1h {
        display: none !important;
        visibility: hidden !important;
    }
    div[data-testid="stDecoration"],
    div[data-testid="stToolbar"],
    div[data-testid="stMainMenu"],
    div[data-testid="stHeader"] {
        display: none !important;
        height: 0px !important;
    }
    iframe {
        display: block;
    }
    .block-container {
        padding-top: 2rem !important;
        padding-bottom: 2rem !important;
    }
    </style>
    """, unsafe_allow_html=True)


def mostrar_barras_simples(conteo, color="#FFB454"):
    """Barras horizontales en HTML puro, sin pasar por pandas/pyarrow (evita el
    Segmentation fault de Python 3.14 + pyarrow en Streamlit Cloud)."""
    import html as _html
    if not conteo:
        st.caption("Sin datos suficientes todavía.")
        return
    maximo = max(conteo.values()) or 1
    filas_html = ""
    for etiqueta, valor in sorted(conteo.items(), key=lambda kv: kv[1], reverse=True):
        ancho_pct = int((valor / maximo) * 100)
        filas_html += f"""
        <div style="margin-bottom:8px;">
            <div style="display:flex; justify-content:space-between; font-size:0.85rem; color:#C7CDDB; margin-bottom:2px;">
                <span>{_html.escape(str(etiqueta))}</span><span>{valor}</span>
            </div>
            <div style="background:#232C42; border-radius:4px; height:10px; width:100%;">
                <div style="background:{color}; border-radius:4px; height:10px; width:{ancho_pct}%;"></div>
            </div>
        </div>"""
    st.markdown(f'<div>{filas_html}</div>', unsafe_allow_html=True)


def verificar_password(password_plano, password_hash):
    """Compara una contraseña en texto plano contra su hash bcrypt almacenado."""
    try:
        return bcrypt.checkpw(password_plano.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, AttributeError):
        return False


# =========================================================
# 🏆 REVIEWPRO REPUTATION SCORE
# Índice propio 0-100 que resume la salud reputacional de un local o de toda
# la agencia en un solo número, para que la agencia lo enseñe a su cliente.
# Se calcula como una media ponderada de 4 factores, todos derivables de datos
# que YA guardamos en Supabase (historico_respuestas). Los pesos siguen el
# patrón habitual del sector (el sentimiento manda; el resto lo matiza).
# =========================================================
PESOS_REPUTATION_SCORE = {
    "sentimiento": 50,   # % de reseñas positivas — el factor dominante
    "volumen": 20,       # cuántas reseñas se gestionan (normalizado)
    "constancia": 20,    # gestión sostenida en el tiempo, no a rachas
    "tendencia": 10,     # mejora o empeora respecto al periodo anterior
}
VOLUMEN_OBJETIVO_MENSUAL = 30  # nº de reseñas/mes a partir del cual el factor volumen puntúa al máximo


def calcular_reputation_score(historico_actual, historico_anterior, dias_periodo):
    """
    Devuelve un dict con el score global (0-100) y el desglose por factor.
    No usa pandas/numpy — solo aritmética con listas, seguro para Streamlit Cloud.

    historico_actual / historico_anterior: listas de filas de historico_respuestas.
    dias_periodo: nº de días del periodo (para normalizar volumen y constancia).
    """
    total = len(historico_actual)

    # Sin datos suficientes: no inventamos un score, lo indicamos.
    if total == 0:
        return {
            "score": None,
            "total": 0,
            "factores": {},
            "detalle": {},
        }

    # --- Factor 1: sentimiento (% de positivas) ---
    positivas = sum(1 for r in historico_actual if r.get("sentimiento") == "positivo")
    pct_positivas = positivas / total
    pts_sentimiento = pct_positivas * PESOS_REPUTATION_SCORE["sentimiento"]

    # --- Factor 2: volumen (respuestas gestionadas, normalizado a un objetivo mensual) ---
    # Escalamos el objetivo según la duración real del periodo.
    dias_ref = max(dias_periodo, 1)
    objetivo_periodo = VOLUMEN_OBJETIVO_MENSUAL * (dias_ref / 30.0)
    ratio_volumen = min(total / objetivo_periodo, 1.0) if objetivo_periodo > 0 else 0
    pts_volumen = ratio_volumen * PESOS_REPUTATION_SCORE["volumen"]

    # --- Factor 3: constancia (en cuántos días distintos hubo actividad) ---
    # Premia la gestión sostenida frente a hacerlo todo un día y desaparecer.
    dias_con_actividad = set()
    for r in historico_actual:
        fecha = r.get("creado_en")
        if fecha:
            dias_con_actividad.add(str(fecha)[:10])  # YYYY-MM-DD
    # Referencia: se espera actividad en, como mucho, ~60% de los días del periodo
    # (nadie gestiona reseñas todos los días). Acotamos para no penalizar de más.
    dias_esperados = max(1, round(dias_ref * 0.6))
    ratio_constancia = min(len(dias_con_actividad) / dias_esperados, 1.0)
    pts_constancia = ratio_constancia * PESOS_REPUTATION_SCORE["constancia"]

    # --- Factor 4: tendencia (mejora del % de positivas respecto al periodo anterior) ---
    total_ant = len(historico_anterior)
    if total_ant == 0:
        # Sin periodo anterior con el que comparar: puntuación neutra (mitad del peso).
        pts_tendencia = PESOS_REPUTATION_SCORE["tendencia"] * 0.5
        delta_pct_positivas = None
    else:
        pct_positivas_ant = sum(1 for r in historico_anterior if r.get("sentimiento") == "positivo") / total_ant
        delta_pct_positivas = pct_positivas - pct_positivas_ant
        # Mapear un delta de [-100%, +100%] a [0, 1], con 0.5 = sin cambios.
        factor_tendencia = max(0.0, min(1.0, 0.5 + delta_pct_positivas))
        pts_tendencia = factor_tendencia * PESOS_REPUTATION_SCORE["tendencia"]

    score = round(pts_sentimiento + pts_volumen + pts_constancia + pts_tendencia)
    score = max(0, min(100, score))

    return {
        "score": score,
        "total": total,
        "factores": {
            "Sentimiento": round(pts_sentimiento, 1),
            "Volumen": round(pts_volumen, 1),
            "Constancia": round(pts_constancia, 1),
            "Tendencia": round(pts_tendencia, 1),
        },
        "detalle": {
            "pct_positivas": round(pct_positivas * 100),
            "total_respuestas": total,
            "dias_con_actividad": len(dias_con_actividad),
            "delta_pct_positivas": None if delta_pct_positivas is None else round(delta_pct_positivas * 100),
        },
    }


def etiqueta_reputation_score(score):
    """Traduce el número a una banda con nombre y color, estilo semáforo."""
    if score is None:
        return ("Sin datos", "#8A94A6")
    if score >= 80:
        return ("Excelente", "#2ECC71")
    if score >= 60:
        return ("Buena", "#8BD1F7")
    if score >= 40:
        return ("Mejorable", "#FFB454")
    return ("En riesgo", "#E74C3C")


def generar_interpretacion_score_ia(cliente_ia, resultado_score, nombre_contexto):
    """Frase interpretativa del score, generada por IA con fallback en plantilla.
    nombre_contexto: 'tu agencia' o el nombre de un local concreto."""
    score = resultado_score.get("score")
    if score is None:
        return "Todavía no hay suficientes respuestas gestionadas en este periodo para calcular una puntuación fiable."

    detalle = resultado_score.get("detalle", {})
    banda, _ = etiqueta_reputation_score(score)

    # Identificar el factor más flojo para sugerir la palanca de mejora.
    factores = resultado_score.get("factores", {})
    pesos = {"Sentimiento": 50, "Volumen": 20, "Constancia": 20, "Tendencia": 10}
    factor_flojo = None
    peor_ratio = 1.1
    for nombre, pts in factores.items():
        tope = pesos.get(nombre, 1)
        ratio = pts / tope if tope else 1
        if ratio < peor_ratio:
            peor_ratio = ratio
            factor_flojo = nombre

    resumen_generico = (
        f"La puntuación de reputación de {nombre_contexto} es {score}/100 ({banda.lower()}). "
        f"El {detalle.get('pct_positivas', 0)}% de las reseñas gestionadas fueron positivas"
        + (f", y el punto con más margen de mejora es «{factor_flojo.lower()}»." if factor_flojo else ".")
    )
    if cliente_ia is None:
        return resumen_generico
    try:
        prompt = (
            f"Puntuación de reputación de {nombre_contexto}: {score}/100 (banda: {banda}). "
            f"Datos: {detalle.get('pct_positivas', 0)}% de reseñas positivas, "
            f"{detalle.get('total_respuestas', 0)} respuestas gestionadas, "
            f"actividad en {detalle.get('dias_con_actividad', 0)} días distintos"
            + (f", cambio de {detalle.get('delta_pct_positivas')} puntos en % positivas respecto al periodo anterior"
               if detalle.get('delta_pct_positivas') is not None else "")
            + (f". El factor con menor puntuación relativa es «{factor_flojo}»." if factor_flojo else ".")
            + " Escribe 2 frases (máximo 45 palabras en total) explicando el score de forma clara y "
              "accionable para el dueño de un negocio local, con UNA recomendación concreta para subirlo. "
              "Tono profesional y directo, nada de plantilla corporativa. Devuelve solo el texto, sin comillas."
        )
        respuesta = cliente_ia.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}]
        )
        for bloque in respuesta.content:
            if getattr(bloque, "type", None) == "text":
                texto = bloque.text.strip().strip('"')
                if texto:
                    return texto
        return resumen_generico
    except Exception:
        return resumen_generico


def mostrar_medidor_score(resultado_score, titulo, interpretacion):
    """Renderiza el score como un medidor visual en HTML puro (sin librerías de
    gráficos), con número grande, banda de color, barra de progreso y desglose
    de factores. Mismo enfoque HTML que mostrar_barras_simples para evitar el
    segfault de pyarrow."""
    import html as _html
    score = resultado_score.get("score")
    banda, color = etiqueta_reputation_score(score)

    if score is None:
        st.info(interpretacion)
        return

    # Cabecera: número grande + banda
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#1B2233,#232C42); border-radius:14px; padding:20px 24px; margin-bottom:14px;">
        <div style="font-size:0.9rem; color:#8A94A6; margin-bottom:4px;">{_html.escape(titulo)}</div>
        <div style="display:flex; align-items:baseline; gap:12px;">
            <span style="font-size:3.2rem; font-weight:700; color:{color}; line-height:1;">{score}</span>
            <span style="font-size:1.1rem; color:#C7CDDB;">/ 100</span>
            <span style="margin-left:auto; background:{color}; color:#0E1117; font-weight:700; font-size:0.85rem; padding:4px 12px; border-radius:20px;">{banda}</span>
        </div>
        <div style="background:#0E1117; border-radius:6px; height:12px; width:100%; margin-top:14px;">
            <div style="background:{color}; border-radius:6px; height:12px; width:{score}%;"></div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown(f"<div style='color:#C7CDDB; font-size:0.95rem; margin-bottom:14px;'>{_html.escape(interpretacion)}</div>", unsafe_allow_html=True)

    # Desglose de factores
    factores = resultado_score.get("factores", {})
    if factores:
        st.caption("Cómo se compone la puntuación:")
        topes = {"Sentimiento": 50, "Volumen": 20, "Constancia": 20, "Tendencia": 10}
        filas_html = ""
        for nombre, pts in factores.items():
            tope = topes.get(nombre, 1)
            ancho_pct = int((pts / tope) * 100) if tope else 0
            filas_html += f"""
            <div style="margin-bottom:8px;">
                <div style="display:flex; justify-content:space-between; font-size:0.85rem; color:#C7CDDB; margin-bottom:2px;">
                    <span>{_html.escape(nombre)}</span><span>{pts} / {tope} pts</span>
                </div>
                <div style="background:#232C42; border-radius:4px; height:9px; width:100%;">
                    <div style="background:{color}; border-radius:4px; height:9px; width:{ancho_pct}%;"></div>
                </div>
            </div>"""
        st.markdown(f'<div>{filas_html}</div>', unsafe_allow_html=True)


def calcular_roi_estrellas(facturacion_mensual, estrellas_actuales, estrellas_objetivo):
    """
    Traduce una mejora de valoración (en estrellas) a un rango de ingresos extra,
    usando el hallazgo del estudio de Luca (Harvard): +1 estrella ≈ +5-9% de ingresos
    en negocios independientes. Devuelve un dict con los importes mensuales y anuales.

    facturacion_mensual: € que factura el negocio al mes (aprox.).
    estrellas_actuales / estrellas_objetivo: valoración media (0-5), p.ej. 3.8 y 4.3.
    """
    if not facturacion_mensual or facturacion_mensual <= 0:
        return None
    delta_estrellas = max(0.0, estrellas_objetivo - estrellas_actuales)
    if delta_estrellas <= 0:
        return {
            "delta_estrellas": 0,
            "mensual_min": 0, "mensual_max": 0,
            "anual_min": 0, "anual_max": 0,
        }
    # El efecto del estudio es "por estrella"; escalamos linealmente al delta real.
    inc_min = facturacion_mensual * ROI_INCREMENTO_POR_ESTRELLA_MIN * delta_estrellas
    inc_max = facturacion_mensual * ROI_INCREMENTO_POR_ESTRELLA_MAX * delta_estrellas
    return {
        "delta_estrellas": round(delta_estrellas, 2),
        "mensual_min": round(inc_min),
        "mensual_max": round(inc_max),
        "anual_min": round(inc_min * 12),
        "anual_max": round(inc_max * 12),
    }


def _fmt_eur(n):
    """Formatea un entero como euros con separador de miles estilo español (1.234 €)."""
    try:
        return f"{int(round(n)):,}".replace(",", ".") + " €"
    except Exception:
        return f"{n} €"


def mostrar_calculadora_roi(roi, estrellas_actuales, estrellas_objetivo):
    """Renderiza el resultado de la calculadora de ROI en HTML puro (sin librerías
    de gráficos). Muestra el rango de ingresos extra mensual y anual, con la cita
    al estudio de Harvard como respaldo."""
    import html as _html
    if roi is None:
        st.caption("Introduce la facturación mensual aproximada para estimar el retorno.")
        return
    if roi["delta_estrellas"] <= 0:
        st.info("Pon una valoración objetivo mayor que la actual para ver el impacto potencial en ingresos.")
        return

    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#10241B,#15321F); border:1px solid #2ECC71; border-radius:14px; padding:20px 24px; margin:10px 0;">
        <div style="font-size:0.9rem; color:#8FE3B0; margin-bottom:6px;">
            Si subes de {estrellas_actuales}★ a {estrellas_objetivo}★ (+{roi['delta_estrellas']}★)
        </div>
        <div style="display:flex; gap:28px; flex-wrap:wrap;">
            <div>
                <div style="font-size:0.8rem; color:#8A94A6;">Ingresos extra / mes</div>
                <div style="font-size:1.9rem; font-weight:700; color:#2ECC71; line-height:1.2;">
                    {_html.escape(_fmt_eur(roi['mensual_min']))} – {_html.escape(_fmt_eur(roi['mensual_max']))}
                </div>
            </div>
            <div>
                <div style="font-size:0.8rem; color:#8A94A6;">Ingresos extra / año</div>
                <div style="font-size:1.9rem; font-weight:700; color:#2ECC71; line-height:1.2;">
                    {_html.escape(_fmt_eur(roi['anual_min']))} – {_html.escape(_fmt_eur(roi['anual_max']))}
                </div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.caption(f"Estimación basada en el estudio de Harvard Business School: +1★ ≈ +5-9% de ingresos en negocios independientes. {ROI_FUENTE}")



LIMITE_USOS_PLAN_GRATIS = 10  # respuestas por mes incluidas en el plan Free (referenciado en la landing)
# Límite de respuestas/mes por plan. None = ilimitado.
# El plan 'individual' es ahora ilimitado (1 local, sin tope de reseñas); el
# blindaje anti-abuso se hace por VELOCIDAD (rate limit por hora/día), no por cupo mensual.
LIMITE_USOS_POR_PLAN = {
    "free": 10,
    "individual": None,        # 1 local, respuestas ilimitadas
    "starter": None,
    "growth": None,
    "enterprise": None,
}
LIMITE_LOCALES_POR_PLAN = {"free": 1, "individual": 1,
                            "starter": 10, "growth": 30, "enterprise": None}  # None = sin límite
UMBRAL_ACTIVIDAD_INUSUAL_POR_LOCAL = 150  # aviso informativo, no bloqueante
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --- Constantes de la calculadora de ROI ---
# Basadas en el estudio de Michael Luca (Harvard Business School), "Reviews,
# Reputation, and Revenue: The Case of Yelp.com": una subida de 1 estrella en la
# valoración se asocia a un aumento del 5-9% de ingresos en negocios independientes.
ROI_INCREMENTO_POR_ESTRELLA_MIN = 0.05  # 5%
ROI_INCREMENTO_POR_ESTRELLA_MAX = 0.09  # 9%
ROI_FUENTE = "Estudio de Michael Luca, Harvard Business School (Reviews, Reputation, and Revenue)."

# Price IDs de Stripe (NO Product ID) — cópialos de tu Dashboard de Stripe:
# entra en Producto → apartado "Pricing" → pulsa en el precio recurrente → copia el
# "API ID" que empieza por "price_...". El Product ID empieza por "prod_..." y NO sirve
# aquí (es la causa exacta del error "No such price: 'prod_...'" que has visto).
#
# ⚠️ IMPORTANTE: ahora hay DOS precios por plan (mensual y anual con -20%).
# Tienes que crear en Stripe los precios ANUALES nuevos y pegar aquí sus IDs.
# Sugerencia de importe anual = mensual × 12 × 0,8 (dos meses gratis largos).
STRIPE_PRICES = {
    "individual": {
        "mensual": "price_TODO_INDIVIDUAL_25EUR_MES",   # ⚠️ crear en Stripe (25€/mes)
        "anual":   "price_TODO_INDIVIDUAL_240EUR_ANO",   # ⚠️ crear en Stripe (240€/año = 25×12×0,8)
    },
    "starter": {
        "mensual": "price_1TqCVYKwc34DG74MpaWMOaKt",     # existente (ajusta el importe a 79€ en Stripe)
        "anual":   "price_TODO_STARTER_758EUR_ANO",       # ⚠️ crear en Stripe (758€/año = 79×12×0,8)
    },
    "growth": {
        "mensual": "price_1TqCZFKwc34DG74Mpw8r8lfi",     # existente (ajusta el importe a 199€ en Stripe)
        "anual":   "price_TODO_GROWTH_1910EUR_ANO",       # ⚠️ crear en Stripe (1.910€/año = 199×12×0,8)
    },
    "enterprise": {
        "mensual": "price_1Tr1RoKwc34DG74M8L4sjSVL",     # existente (ajusta el importe a 449€ en Stripe)
        "anual":   "price_TODO_ENTERPRISE_4310EUR_ANO",   # ⚠️ crear en Stripe (4.310€/año = 449×12×0,8)
    },
}

DESCUENTO_ANUAL = 0.20  # -20% al pagar por año

def _redondear_bonito(n):
    """Redondea un importe a una cifra 'comercial' agradable.
    < 100 → sin tocar (25, 79...). >= 100 → al múltiplo de 10 más cercano
    (1908 → 1910... y si cae en 1905-1914 lo deja en 1910; 1900 se queda 1900).
    Esto evita precios feos tipo 1908€ en la facturación anual."""
    n = round(n)
    if n < 100:
        return n
    return int(round(n / 1.0) - 10)

def _redondear_bonito(n):
    """Redondea un importe a una cifra 'comercial' agradable.
    < 100 → sin tocar (25, 79...). >= 100 → al múltiplo de 10 más cercano
    (1908 → 1910... y si cae en 1905-1914 lo deja en 1910; 1900 se queda 1900).
    Esto evita precios feos tipo 1908€ en la facturación anual."""
    n = round(n)
    if n < 50:
        return n+2
    


def _precio_anual_mensualizado(precio_mensual):
    """Precio equivalente por mes cuando se paga el año con el descuento anual."""
    return round(precio_mensual * (1 - DESCUENTO_ANUAL))


def _precio_anual_total(precio_mensual):
    """Precio total del año con descuento, redondeado a una cifra bonita.
    Es lo que realmente se cobra de una vez al elegir facturación anual."""
    return _redondear_bonito(precio_mensual * 12 * (1 - DESCUENTO_ANUAL))

PLANES_AUTOSERVICIO = {
    "individual": {
        "nombre": "Individual", "target": "Un solo local · sin límite de reseñas",
        "precio_mensual": 25, "price_ids": STRIPE_PRICES["individual"],
        "features": ["1 local", "Respuestas ILIMITADAS", "Reputation Score + calculadora de ROI",
                     "Blindaje legal + informe PDF de marca"],
        "gancho": "Para el bar, restaurante o camping que gestiona sus propias reseñas.",
    },
    "starter": {
        "nombre": "Starter", "target": "Agencias pequeñas · hasta 10 locales",
        "precio_mensual": 79, "price_ids": STRIPE_PRICES["starter"],
        "features": ["Hasta 10 locales", "Respuestas ilimitadas", "Marca blanca completa",
                     "SEO invisible por local"],
        "gancho": "Desde 7,90€ por local al mes.",
    },
    "growth": {
        "nombre": "Growth", "target": "Agencias medianas · hasta 30 locales",
        "precio_mensual": 199, "price_ids": STRIPE_PRICES["growth"],
        "features": ["Hasta 30 locales", "Respuestas ilimitadas", "Marca blanca completa",
                     "Multi-usuario + analítica + ROI"],
        "gancho": "Solo 6,60€ por local — el favorito de las agencias.",
        "destacado": True,
    },
    "enterprise": {
        "nombre": "Enterprise", "target": "Agencias grandes · locales ilimitados",
        "precio_mensual": 449, "price_ids": STRIPE_PRICES["enterprise"],
        "features": ["Locales ilimitados", "Soporte prioritario", "Marca blanca completa",
                     "Multi-usuario + analítica + ROI"],
        "gancho": "Sin techo de crecimiento. Cuantos más locales, más barato sale cada uno.",
    },
}

# Compatibilidad hacia atrás: algunas partes del código antiguo referencian estos nombres.
STRIPE_PRICE_ID_INDIVIDUAL = STRIPE_PRICES["individual"]["mensual"]
STRIPE_PRICE_ID_STARTER = STRIPE_PRICES["starter"]["mensual"]
STRIPE_PRICE_ID_GROWTH = STRIPE_PRICES["growth"]["mensual"]
STRIPE_PRICE_ID_ENTERPRISE = STRIPE_PRICES["enterprise"]["mensual"]


def crear_sesion_pago_stripe(agencia_id, plan_nombre, price_id):
    """
    Crea una sesión de Stripe Checkout dinámica para que la agencia contrate un plan.
    Guarda agencia_id y plan en la metadata de la sesión: así, cuando el pago se confirme,
    sabremos automáticamente a qué agencia activarle qué plan sin tocar nada a mano.
    Devuelve la URL de pago, o None si algo falla.
    """
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            client_reference_id=str(agencia_id),
            metadata={"agencia_id": str(agencia_id), "plan": plan_nombre},
            success_url=f"{APP_URL}/?pago_exito=1&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_URL}/?pago_cancelado=1",
        )
        return session.url
    except Exception as e:
        st.error(redactar_secretos(f"No se pudo iniciar el proceso de pago: {e}"))
        return None


def crear_sesion_pago_nueva_agencia(plan_nombre, price_id):
    """
    Crea la sesión de pago para alguien que compra un plan directamente desde la landing,
    SIN tener cuenta todavía. Ya no pedimos nada extra dentro del propio Stripe: al volver
    del pago, la propia app le pide el email, el nombre de la agencia y la contraseña en
    una pantalla intermedia (igual que el alta del plan Free), así que aquí basta con el
    plan elegido.
    """
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={"plan": plan_nombre, "flujo": "alta_nueva"},
            success_url=f"{APP_URL}/?alta_nueva=1&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_URL}/?pago_cancelado=1",
        )
        return session.url
    except Exception as e:
        st.error(redactar_secretos(f"No se pudo iniciar el proceso de pago: {e}"))
        return None


def confirmar_pago_y_activar_plan(session_id):
    """
    Se llama cuando Stripe redirige de vuelta a la app tras un pago DE UPGRADE (agencia ya
    existente). Verifica contra la propia Stripe (nunca te fíes solo de la URL) que el pago
    se ha completado de verdad, y si es así, activa el plan de la agencia en Supabase
    automáticamente. Devuelve (True, plan_nombre) o (False, "motivo").
    """
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status != "paid":
            return False, "El pago todavía no se ha confirmado."
        agencia_id = session.metadata.get("agencia_id")
        plan_nombre = session.metadata.get("plan")
        if not agencia_id or not plan_nombre:
            return False, "No se pudo identificar la agencia o el plan asociado a este pago."
        supabase.table("agencias").update({"plan": plan_nombre}).eq("id", agencia_id).execute()
        return True, plan_nombre
    except Exception as e:
        return False, str(e)


def verificar_pago_alta_nueva(session_id):
    """
    Se llama cuando Stripe redirige de vuelta a la app tras un pago de un cliente NUEVO
    (todavía sin cuenta). Solo confirma que el pago está completado y recupera el plan y
    el stripe_customer_id (para guardarlo en la agencia, tal como pide el esquema). El
    email y el nombre de la agencia se piden directamente en la app justo después, en vez
    de depender de campos de Stripe. Devuelve (True, datos) o (False, "motivo").
    """
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status != "paid":
            return False, "El pago todavía no se ha confirmado."
        plan_nombre = session.metadata.get("plan")
        if not plan_nombre:
            return False, "No se pudo identificar el plan asociado a este pago."
        email_prefill = ""
        try:
            if session.customer_details and session.customer_details.email:
                email_prefill = session.customer_details.email
        except Exception:
            pass
        return True, {
            "session_id": session_id,
            "plan": plan_nombre,
            "stripe_customer_id": session.customer,
            "email_prefill": email_prefill,
        }
    except Exception as e:
        return False, str(e)


def registrar_agencia_gratuita(nombre_agencia, nombre_local, email, password_plano, nombre_usuario):
    """
    Alta de autoservicio para el plan Free: crea la agencia (plan='free'),
    su primer usuario y un primer local, sin intervención manual.
    Devuelve (True, None) si todo ha ido bien, o (False, "motivo") si ha fallado.
    """
    email_normalizado = email.lower().strip()

    if not EMAIL_REGEX.match(email_normalizado):
        return False, "El email no tiene un formato válido."
    if len(password_plano) < 8:
        return False, "La contraseña debe tener al menos 8 caracteres."

    existente = supabase.table("usuarios").select("id").eq("email", email_normalizado).execute()
    if existente.data:
        return False, "Ya existe una cuenta con ese email. Inicia sesión en su lugar."

    try:
        nueva_agencia = supabase.table("agencias").insert({
            "nombre_agencia": nombre_agencia.strip(),
            "logo_url": "https://dummyimage.com/200x60/635BFF/ffffff&text=ReviewPro",
            "color_marca": "#635BFF",
            "plan": "free"
        }).execute()
        agencia_id = nueva_agencia.data[0]["id"]

        supabase.table("usuarios").insert({
            "agencia_id": agencia_id,
            "email": email_normalizado,
            "password_hash": bcrypt.hashpw(password_plano.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
            "nombre_usuario": nombre_usuario.strip(),
            "rol": "admin"
        }).execute()

        supabase.table("locales").insert({
            "agencia_id": agencia_id,
            "nombre": nombre_local.strip(),
            "nicho": "general",
            "seo_keywords": []
        }).execute()

        return True, None
    except Exception as e:
        return False, f"Error al crear la cuenta: {e}"


def registrar_agencia_de_pago(nombre_agencia, nombre_local, email, password_plano, nombre_usuario, plan, stripe_customer_id=None):
    """
    Igual que registrar_agencia_gratuita, pero para agencias que ya han pagado un plan
    de pago (Starter/Growth/Enterprise) desde la landing. Se llama justo después de que
    el pago se ha verificado en Stripe y el usuario rellena email + contraseña en la
    pantalla intermedia. Devuelve (True, {"agencia":..., "usuario":..., "locales":...})
    o (False, "motivo").
    """
    email_normalizado = email.lower().strip()

    if not EMAIL_REGEX.match(email_normalizado):
        return False, "El email no tiene un formato válido."
    if len(password_plano) < 8:
        return False, "La contraseña debe tener al menos 8 caracteres."

    existente = supabase.table("usuarios").select("id").eq("email", email_normalizado).execute()
    if existente.data:
        return False, "Ya existe una cuenta con ese email. Inicia sesión en su lugar."

    try:
        datos_agencia = {
            "nombre_agencia": nombre_agencia.strip(),
            "logo_url": "https://dummyimage.com/200x60/635BFF/ffffff&text=ReviewPro",
            "color_marca": "#635BFF",
            "plan": plan
        }
        if stripe_customer_id:
            datos_agencia["stripe_customer_id"] = stripe_customer_id

        nueva_agencia = supabase.table("agencias").insert(datos_agencia).execute()
        agencia_id = nueva_agencia.data[0]["id"]

        nuevo_usuario = supabase.table("usuarios").insert({
            "agencia_id": agencia_id,
            "email": email_normalizado,
            "password_hash": bcrypt.hashpw(password_plano.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
            "nombre_usuario": nombre_usuario.strip(),
            "rol": "admin"
        }).execute()

        nuevo_local = supabase.table("locales").insert({
            "agencia_id": agencia_id,
            "nombre": nombre_local.strip(),
            "nicho": "general",
            "seo_keywords": []
        }).execute()

        return True, {
            "agencia": nueva_agencia.data[0],
            "usuario": nuevo_usuario.data[0],
            "locales": nuevo_local.data or []
        }
    except Exception as e:
        return False, f"Error al crear la cuenta: {e}"


def render_formulario_alta_pendiente():
    """
    Pantalla que se muestra justo después de un pago nuevo (cliente sin cuenta previa) ya
    verificado en Stripe. Pide los mismos datos que el alta del plan Free (agencia, local,
    nombre, email, contraseña) y crea la cuenta completa, dejando al usuario ya logueado.
    """
    datos = st.session_state.alta_pendiente
    st.success(f"✅ Pago confirmado — plan **{datos['plan'].capitalize()}**. Un último paso para entrar:")

    with st.form("crear_cuenta_alta_pendiente"):
        nombre_agencia_final = st.text_input("Nombre de tu agencia o negocio")
        nombre_local_final = st.text_input("Nombre de tu primer establecimiento")
        nombre_usuario_final = st.text_input("Tu nombre")
        email_final = st.text_input("Email", value=datos.get("email_prefill", ""))
        password_final = st.text_input("Crea tu contraseña (mín. 8 caracteres)", type="password")
        password_confirmar = st.text_input("Repite la contraseña", type="password")
        submit_alta = st.form_submit_button("Crear mi cuenta y entrar", use_container_width=True, type="primary")

    if submit_alta:
        if not all([nombre_agencia_final.strip(), nombre_local_final.strip(), nombre_usuario_final.strip(), email_final.strip(), password_final]):
            st.warning("Rellena todos los campos.")
        elif password_final != password_confirmar:
            st.error("Las contraseñas no coinciden.")
        else:
            ok, resultado = registrar_agencia_de_pago(
                nombre_agencia_final, nombre_local_final, email_final, password_final,
                nombre_usuario_final, datos["plan"], datos.get("stripe_customer_id")
            )
            if ok:
                st.session_state.alta_completada_session_id = datos["session_id"]
                st.session_state.alta_pendiente = None
                st.session_state.sesion_activa = True
                st.session_state.usuario_actual = resultado["usuario"]
                st.session_state.agencia_actual = resultado["agencia"]
                st.session_state.locales_agencia = resultado["locales"]
                st.success(f"¡Cuenta creada! Bienvenido/a, {resultado['usuario']['nombre_usuario']}.")
                st.rerun()
            else:
                st.error(resultado)


def grafico_barras_pos_neg(categorias, valores_positivas, valores_negativas,
                            color_positivas=colors.HexColor("#2ECC71"),
                            color_negativas=colors.HexColor("#E74C3C"),
                            ancho=16 * cm, alto=6 * cm):
    """Gráfico de barras agrupadas (positivas vs. negativas) hecho con
    reportlab.graphics puro — sin pandas ni numpy, para no repetir el
    segfault que ya tuvimos con pyarrow en Streamlit Cloud."""
    dibujo = Drawing(ancho, alto)
    grafico = VerticalBarChart()
    grafico.x = 40
    grafico.y = 25
    grafico.width = ancho - 90
    grafico.height = alto - 55
    grafico.data = [valores_positivas, valores_negativas]
    grafico.categoryAxis.categoryNames = categorias
    grafico.categoryAxis.labels.fontSize = 7.5
    grafico.categoryAxis.labels.boxAnchor = "n"
    grafico.valueAxis.valueMin = 0
    grafico.bars[0].fillColor = color_positivas
    grafico.bars[1].fillColor = color_negativas
    grafico.groupSpacing = 12
    grafico.barSpacing = 2
    dibujo.add(grafico)

    leyenda = Legend()
    leyenda.x = ancho - 55
    leyenda.y = alto - 8
    leyenda.dx = 7
    leyenda.dy = 7
    leyenda.fontSize = 7.5
    leyenda.colorNamePairs = [(color_positivas, "Positivas"), (color_negativas, "Negativas")]
    dibujo.add(leyenda)

    return dibujo


def generar_resumen_ejecutivo_ia(cliente_ia, total, positivas, negativas, pct_positivas, local_principal, num_locales):
    """Genera la frase-titular del informe con IA. Si la llamada falla por
    cualquier motivo (red, límite, lo que sea), cae a una plantilla fija —
    el informe nunca debe quedarse sin esta sección por un fallo de la API."""
    resumen_generico = (
        f"Durante este periodo se gestionaron {total} reseña{'s' if total != 1 else ''} para "
        f"{'tu local' if num_locales <= 1 else f'tus {num_locales} locales'}"
        + (f", con {local_principal} como el de mayor actividad" if local_principal and num_locales > 1 else "")
        + f". El {pct_positivas}% de las respuestas correspondieron a reseñas positivas."
    )
    if cliente_ia is None or total == 0:
        return resumen_generico
    try:
        prompt = (
            f"Datos de un periodo de gestión de reseñas: {total} respuestas generadas en total, "
            f"{positivas} a reseñas positivas y {negativas} a negativas ({pct_positivas}% positivas), "
            f"repartidas entre {num_locales} local(es)"
            + (f", el de más actividad es {local_principal}" if local_principal else "")
            + ". Escribe UNA sola frase (máximo 30 palabras) a modo de titular ejecutivo para la cabecera "
              "de un informe que una agencia de marketing reenvía a su cliente. Tono profesional pero "
              "natural, nada de plantilla corporativa ('estimado cliente', 'nos complace informar'...). "
              "Devuelve EXCLUSIVAMENTE la frase, sin comillas ni explicación."
        )
        respuesta = cliente_ia.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}]
        )
        for bloque in respuesta.content:
            if getattr(bloque, "type", None) == "text":
                texto = bloque.text.strip().strip('"')
                if texto:
                    return texto
        return resumen_generico
    except Exception:
        return resumen_generico


def generar_informe_pdf_mensual(agencia, historico, historico_anterior, locales_agencia,
                                 id_a_nombre_usuario, contenido_seo_periodo, periodo_texto,
                                 cliente_ia=None, resultado_score=None, dias_periodo=30,
                                 roi=None, roi_estrellas_actuales=None, roi_estrellas_objetivo=None):
    """
    Genera el informe PDF de marca blanca (v2): Reputation Score, resumen
    ejecutivo, comparación con el periodo anterior, actividad por local con
    gráfico, reparto por usuario del equipo, un caso destacado real y la
    actividad de contenido SEO generado. Devuelve los bytes del PDF.
    """
    buffer = BytesIO()
    color_hex = agencia.get("color_marca", "#635BFF").lstrip("#")
    color_rl = colors.HexColor(f"#{color_hex}")

    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    estilos = getSampleStyleSheet()
    estilo_titulo = ParagraphStyle("TituloInforme", parent=estilos["Title"], textColor=color_rl, fontSize=20)
    estilo_subtitulo = ParagraphStyle("Subtitulo", parent=estilos["Normal"], textColor=colors.grey, fontSize=11)
    estilo_seccion = ParagraphStyle("Seccion", parent=estilos["Heading2"], textColor=color_rl, spaceBefore=14)
    estilo_resumen_ejecutivo = ParagraphStyle(
        "ResumenEjecutivo", parent=estilos["Normal"], fontSize=11.5, leading=16,
        textColor=colors.HexColor("#1A1A1A"), spaceBefore=2, spaceAfter=2
    )
    estilo_caso = ParagraphStyle(
        "Caso", parent=estilos["Normal"], fontSize=9.5, leading=13.5,
        textColor=colors.HexColor("#333333"), leftIndent=8, spaceAfter=4
    )
    estilo_nota = ParagraphStyle("Nota", parent=estilos["Normal"], fontSize=8, textColor=colors.grey, spaceBefore=4)

    story = []

    # Logo (si se puede descargar; si falla, se omite sin romper el informe)
    try:
        resp_logo = requests.get(agencia["logo_url"], timeout=5)
        imagen_logo = RLImage(BytesIO(resp_logo.content), width=4 * cm, height=1.2 * cm)
        story.append(imagen_logo)
        story.append(Spacer(1, 10))
    except Exception:
        pass

    story.append(Paragraph("Informe de reputación online", estilo_titulo))
    story.append(Paragraph(f"{agencia['nombre_agencia']} · {periodo_texto}", estilo_subtitulo))
    story.append(Spacer(1, 14))

    # --- Métricas del periodo actual y del anterior (para la comparación) ---
    total = len(historico)
    positivas = sum(1 for r in historico if r["sentimiento"] == "positivo")
    negativas = total - positivas
    pct_positivas = round(positivas / total * 100) if total else 0

    total_ant = len(historico_anterior)
    pct_positivas_ant = round(sum(1 for r in historico_anterior if r["sentimiento"] == "positivo") / total_ant * 100) if total_ant else None

    def texto_delta(actual, anterior, sufijo=""):
        if anterior is None:
            return ""
        delta = actual - anterior
        if delta > 0:
            return f" (▲ +{delta}{sufijo})"
        elif delta < 0:
            return f" (▼ {delta}{sufijo})"
        return " (=)"

    delta_total = texto_delta(total, total_ant if historico_anterior else None)
    delta_pct = texto_delta(pct_positivas, pct_positivas_ant, sufijo=" pts")

    # --- Desglose por local (para la tabla, el gráfico y el resumen ejecutivo) ---
    id_a_nombre_local = {l["id"]: l["nombre"] for l in locales_agencia}
    conteo_local_pos, conteo_local_neg = {}, {}
    for fila in historico:
        nombre = id_a_nombre_local.get(fila["local_id"], "Local desconocido")
        if fila["sentimiento"] == "positivo":
            conteo_local_pos[nombre] = conteo_local_pos.get(nombre, 0) + 1
        else:
            conteo_local_neg[nombre] = conteo_local_neg.get(nombre, 0) + 1
    nombres_locales_activos = sorted(set(conteo_local_pos) | set(conteo_local_neg),
                                      key=lambda n: conteo_local_pos.get(n, 0) + conteo_local_neg.get(n, 0),
                                      reverse=True)
    local_principal = nombres_locales_activos[0] if nombres_locales_activos else None

    # --- Reputation Score (titular del informe, si se ha calculado) ---
    if resultado_score is None:
        resultado_score = calcular_reputation_score(historico, historico_anterior, dias_periodo)
    score_valor = resultado_score.get("score")
    if score_valor is not None:
        banda_score, color_banda_hex = etiqueta_reputation_score(score_valor)
        color_banda = colors.HexColor(color_banda_hex)
        estilo_score_num = ParagraphStyle(
            "ScoreNum", parent=estilos["Normal"], fontSize=30, leading=32,
            textColor=color_banda, alignment=1
        )
        estilo_score_label = ParagraphStyle(
            "ScoreLabel", parent=estilos["Normal"], fontSize=9, textColor=colors.white, alignment=1
        )
        celda_score = [
            [Paragraph(f"<b>{score_valor}</b> <font size=12>/ 100</font>", estilo_score_num)],
            [Paragraph(f"REPUTATION SCORE · {banda_score.upper()}", estilo_score_label)],
        ]
        tabla_score = Table(celda_score, colWidths=[16 * cm])
        tabla_score.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#1B2233")),
            ("TOPPADDING", (0, 0), (-1, 0), 12),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
            ("TOPPADDING", (0, 1), (-1, 1), 0),
            ("LINEBELOW", (0, 0), (-1, 0), 0, colors.HexColor("#1B2233")),
            ("BOX", (0, 0), (-1, -1), 1, color_banda),
        ]))
        story.append(tabla_score)
        story.append(Spacer(1, 12))

    # --- Resumen ejecutivo (IA con fallback en plantilla) ---
    resumen_texto = generar_resumen_ejecutivo_ia(
        cliente_ia, total, positivas, negativas, pct_positivas, local_principal, len(locales_agencia)
    )
    story.append(Paragraph(resumen_texto, estilo_resumen_ejecutivo))
    story.append(Spacer(1, 12))

    # --- Calculadora de ROI (si se han pasado datos de facturación/estrellas) ---
    if roi and roi.get("delta_estrellas", 0) > 0:
        estilo_roi_titulo = ParagraphStyle(
            "RoiTitulo", parent=estilos["Normal"], fontSize=10, textColor=colors.HexColor("#1E7A46"),
            spaceBefore=2, spaceAfter=4
        )
        estilo_roi_cifra = ParagraphStyle(
            "RoiCifra", parent=estilos["Normal"], fontSize=13, leading=16,
            textColor=colors.HexColor("#1E7A46"), alignment=1
        )
        estilo_roi_label = ParagraphStyle(
            "RoiLabel", parent=estilos["Normal"], fontSize=8, textColor=colors.HexColor("#4A5568"), alignment=1
        )
        story.append(Paragraph(
            f"Potencial de ingresos: subir de {roi_estrellas_actuales}★ a {roi_estrellas_objetivo}★",
            estilo_roi_titulo
        ))
        tabla_roi = Table([
            [Paragraph("INGRESOS EXTRA / MES", estilo_roi_label), Paragraph("INGRESOS EXTRA / AÑO", estilo_roi_label)],
            [Paragraph(f"<b>{_fmt_eur(roi['mensual_min'])} – {_fmt_eur(roi['mensual_max'])}</b>", estilo_roi_cifra),
             Paragraph(f"<b>{_fmt_eur(roi['anual_min'])} – {_fmt_eur(roi['anual_max'])}</b>", estilo_roi_cifra)],
        ], colWidths=[8 * cm, 8 * cm])
        tabla_roi.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EAF7EF")),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#2ECC71")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C7E8D3")),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(tabla_roi)
        story.append(Paragraph(
            "Estimación según el estudio de Harvard Business School (Michael Luca): cada estrella de más "
            "supone entre un 5% y un 9% más de ingresos en negocios independientes.",
            estilo_nota
        ))
        story.append(Spacer(1, 14))

    # --- Resumen del periodo, con comparación al periodo anterior ---
    story.append(Paragraph("Resumen del periodo", estilo_seccion))
    tabla_resumen = Table([
        ["Respuestas generadas", "Reseñas positivas", "Reseñas negativas", "% positivas"],
        [f"{total}{delta_total}", str(positivas), str(negativas), f"{pct_positivas}%{delta_pct}"]
    ], colWidths=[4 * cm, 4 * cm, 4 * cm, 5 * cm])
    tabla_resumen.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), color_rl),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(tabla_resumen)
    if not historico_anterior:
        story.append(Paragraph("Sin datos del periodo anterior todavía para comparar.", estilo_nota))
    story.append(Spacer(1, 16))

    # --- Actividad por local: tabla + gráfico ---
    story.append(Paragraph("Actividad por local", estilo_seccion))
    filas_tabla_local = [["Local", "Positivas", "Negativas", "Total"]]
    categorias, serie_pos, serie_neg = [], [], []
    for nombre in nombres_locales_activos:
        p, n = conteo_local_pos.get(nombre, 0), conteo_local_neg.get(nombre, 0)
        filas_tabla_local.append([nombre, str(p), str(n), str(p + n)])
        categorias.append(nombre)
        serie_pos.append(p)
        serie_neg.append(n)
    tabla_locales = Table(filas_tabla_local, colWidths=[7 * cm, 3 * cm, 3 * cm, 3 * cm])
    tabla_locales.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A3448")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(tabla_locales)
    story.append(Spacer(1, 10))
    if categorias:
        story.append(grafico_barras_pos_neg(categorias, serie_pos, serie_neg))
    story.append(Spacer(1, 16))

    # --- Reparto de trabajo por usuario del equipo (antes se calculaba y no se usaba) ---
    story.append(Paragraph("Reparto de trabajo por usuario del equipo", estilo_seccion))
    conteo_usuario = {}
    for fila in historico:
        nombre_u = id_a_nombre_usuario.get(fila.get("usuario_id"), "Usuario eliminado")
        conteo_usuario[nombre_u] = conteo_usuario.get(nombre_u, 0) + 1
    if conteo_usuario:
        filas_usuario = [["Usuario", "Respuestas generadas"]] + \
                         [[u, str(n)] for u, n in sorted(conteo_usuario.items(), key=lambda x: -x[1])]
        tabla_usuarios = Table(filas_usuario, colWidths=[10 * cm, 5 * cm])
        tabla_usuarios.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A3448")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(tabla_usuarios)
    else:
        story.append(Paragraph("Sin datos de usuario para este periodo.", estilo_nota))
    story.append(Spacer(1, 16))

    # --- Caso destacado del periodo (requiere la migración de extracto_resena/extracto_respuesta) ---
    casos_con_extracto = [r for r in historico if r["sentimiento"] == "negativo" and r.get("extracto_resena")]
    if casos_con_extracto:
        caso = max(casos_con_extracto, key=lambda r: r.get("longitud_palabras", 0))
        story.append(Paragraph("Caso destacado del periodo", estilo_seccion))
        story.append(Paragraph(f"<b>Lo que dijo el cliente:</b> «{caso['extracto_resena']}»", estilo_caso))
        if caso.get("extracto_respuesta"):
            story.append(Paragraph(f"<b>Cómo se respondió:</b> «{caso['extracto_respuesta']}»", estilo_caso))
        story.append(Spacer(1, 16))

    # --- Contenido SEO y redes generado en el periodo ---
    story.append(Paragraph("Contenido SEO y redes generado", estilo_seccion))
    if contenido_seo_periodo:
        conteo_tipo = {}
        for fila in contenido_seo_periodo:
            tipo = fila.get("tipo_contenido", "Otro")
            conteo_tipo[tipo] = conteo_tipo.get(tipo, 0) + 1
        filas_seo = [["Tipo de contenido", "Piezas generadas"]] + \
                    [[t, str(n)] for t, n in sorted(conteo_tipo.items(), key=lambda x: -x[1])]
        tabla_seo = Table(filas_seo, colWidths=[10 * cm, 5 * cm])
        tabla_seo.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A3448")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(tabla_seo)
    else:
        story.append(Paragraph("No se generó contenido SEO adicional en este periodo.", estilo_nota))
    story.append(Spacer(1, 20))

    story.append(Paragraph(
        f"Informe generado automáticamente por ReviewPro Enterprise en nombre de {agencia['nombre_agencia']}. "
        "Documento de uso interno/comercial para justificar la gestión de reputación online frente a sus clientes.",
        ParagraphStyle("Pie", parent=estilos["Normal"], fontSize=7, textColor=colors.grey)
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def generar_qr_png(url_destino):
    """Genera un código QR en PNG (bytes) que apunta a la URL indicada."""
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url_destino)
    qr.make(fit=True)
    imagen = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    imagen.save(buffer, format="PNG")
    return buffer.getvalue()


def generar_mensaje_whatsapp(nombre_local, enlace_resena):
    """Construye el enlace wa.me con un mensaje precargado para pedir una reseña."""
    mensaje = (
        f"¡Hola! Muchas gracias por confiar en {nombre_local} 🙌 "
        f"¿Nos ayudarías dejando tu opinión en Google? Solo te llevará 1 minuto: {enlace_resena}"
    )
    return "https://wa.me/?text=" + urllib.parse.quote(mensaje)


def generar_contenido_seo_extra(client, nombre_local, nicho, seo_keywords, tipo_contenido):
    """
    Reutiliza el motor de IA para generar contenido SEO adicional (más allá de
    respuestas a reseñas): publicaciones de Google Business o descripciones
    para redes sociales, usando las mismas keywords ya cargadas del local.
    """
    keywords_texto = ", ".join(seo_keywords) if seo_keywords else "sin keywords específicas cargadas"

    instrucciones_por_tipo = {
        "Publicación de Google Business": "Escribe una publicación breve (40-60 palabras) para la sección de novedades de Google Business Profile. Debe sonar natural, cercana y con una llamada a la acción sutil (visitar, reservar, preguntar).",
        "Descripción para redes sociales": "Escribe una descripción corta (25-40 palabras) pensada para el pie de una publicación de Instagram o Facebook. Tono cercano, sin hashtags excesivos (máximo 3 al final).",
        "Meta descripción SEO": "Escribe una meta descripción SEO de máximo 155 caracteres para la página web de este negocio, pensada para aparecer en los resultados de Google. Debe incluir una llamada a la acción."
    }

    system_prompt = f"""Eres la persona que lleva las redes y la ficha de Google de "{nombre_local}" (nicho: "{nicho}"), escribiendo como lo haría el propio negocio, no una agencia externa ni un redactor genérico. Evita sonar a plantilla: nada de "no te lo pierdas", "descúbrelo ya" ni llamadas a la acción intercambiables entre cualquier negocio.

Integra de forma natural, sin forzar, al menos 1-2 de estas palabras clave si el contexto lo permite: {keywords_texto}. Si forzar una keyword rompe la naturalidad de la frase, prescinde de ella.

Instrucción específica para este contenido: {instrucciones_por_tipo[tipo_contenido]}

Devuelve EXCLUSIVAMENTE el texto final, sin comillas, sin explicaciones, sin encabezados, sin markdown."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=system_prompt,
        messages=[{"role": "user", "content": f"Genera el contenido para {nombre_local}."}]
    )

    for bloque in response.content:
        if getattr(bloque, "type", None) == "text":
            return bloque.text.strip().strip('"')
    return ""


def contar_usos_del_mes(agencia_id):
    """Cuenta cuántas respuestas ha generado una agencia desde el día 1 del mes actual.
    Esta cuenta vive en Supabase, no en el navegador, por lo que no se puede
    burlar borrando cookies, usando incógnito o cambiando de dispositivo."""
    inicio_de_mes = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    resultado = supabase.table("historico_respuestas") \
        .select("id", count="exact") \
        .eq("agencia_id", agencia_id) \
        .gte("creado_en", inicio_de_mes) \
        .execute()
    return resultado.count or 0


def contar_usos_del_mes_por_local(local_id):
    """Igual que contar_usos_del_mes pero acotado a un local concreto.
    Se usa solo para el aviso informativo de actividad inusual, no para bloquear."""
    inicio_de_mes = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    resultado = supabase.table("historico_respuestas") \
        .select("id", count="exact") \
        .eq("local_id", local_id) \
        .gte("creado_en", inicio_de_mes) \
        .execute()
    return resultado.count or 0


def puede_agencia_anadir_local(agencia, locales_actuales):
    """Comprueba si la agencia puede añadir un local más según el límite de su plan.
    Devuelve (True, None) si puede, o (False, "motivo") si no."""
    limite = LIMITE_LOCALES_POR_PLAN.get(agencia.get("plan", "growth"))
    if limite is None:
        return True, None
    if len(locales_actuales) >= limite:
        return False, f"Tu plan '{agencia.get('plan')}' incluye hasta {limite} locales. Actualiza tu plan para añadir más."
    return True, None


# =========================================================
# 🛡️ BLINDAJE ANTI-ABUSO POR VELOCIDAD (no por cupo mensual)
# El plan Individual es ilimitado, así que el tope mensual no sirve para frenar a
# quien quiera exprimirlo (p.ej. una agencia metiendo cientos de respuestas en el
# plan de 1 local). Pero un negocio REAL —aunque tenga 2.000 reseñas— responde a
# ritmo humano: nunca genera 60 respuestas en 10 minutos. Así que limitamos la
# VELOCIDAD, no el volumen total. Un bufet legítimo con mucho tráfico ni lo nota;
# un script abusador choca contra el tope enseguida.
# Los límites se leen de historico_respuestas (creado_en), sin tabla nueva.
# =========================================================
LIMITES_VELOCIDAD_POR_PLAN = {
    # (respuestas/hora, respuestas/día). None = sin límite (planes de agencia grandes,
    # que legítimamente gestionan muchos locales a la vez).
    "free":       (10, 10),      # el cupo mensual (10) ya es el límite real
    "individual": (30, 150),     # 1 local: 30/h y 150/día es holgadísimo para un humano,
                                 # pero frena en seco el uso como si fuera multi-local
    "starter":    (80, None),    # varios locales; límite/hora alto para picos legítimos
    "growth":     (150, None),
    "enterprise": (None, None),  # sin límite
}


def _contar_respuestas_desde(agencia_id, desde_iso):
    """Cuenta respuestas de una agencia desde un instante ISO dado."""
    try:
        resultado = supabase.table("historico_respuestas") \
            .select("id", count="exact") \
            .eq("agencia_id", agencia_id) \
            .gte("creado_en", desde_iso) \
            .execute()
        return resultado.count or 0
    except Exception:
        # Si la comprobación falla, no bloqueamos (preferimos no cortar a un cliente real
        # por un fallo puntual de red; el resto de límites siguen aplicando).
        return 0


def verificar_velocidad(agencia):
    """
    Comprueba que la agencia no está generando respuestas a una velocidad impropia
    de un negocio real. Devuelve un dict:
      {"permitido": bool, "razon": str|None, "advertencia": str|None}
    No usa tablas nuevas: se apoya en historico_respuestas.
    """
    plan = agencia.get("plan", "growth")
    limite_hora, limite_dia = LIMITES_VELOCIDAD_POR_PLAN.get(plan, (None, None))
    if limite_hora is None and limite_dia is None:
        return {"permitido": True, "razon": None, "advertencia": None}

    ahora = datetime.utcnow()
    advertencia = None

    # --- Límite por hora ---
    if limite_hora is not None:
        hace_una_hora = (ahora - timedelta(hours=1)).isoformat()
        en_la_ultima_hora = _contar_respuestas_desde(agencia["id"], hace_una_hora)
        if en_la_ultima_hora >= limite_hora:
            return {
                "permitido": False,
                "razon": ("Has generado muchas respuestas en muy poco tiempo. Para evitar usos indebidos, "
                          "espera unos minutos y vuelve a intentarlo. Si gestionas un negocio con mucho "
                          "volumen real, escríbenos y ampliamos tu límite sin problema."),
                "advertencia": None,
            }
        if en_la_ultima_hora >= limite_hora * 0.8:
            advertencia = "Estás cerca del límite de velocidad por hora. Ve con calma para no tener que esperar."

    # --- Límite por día ---
    if limite_dia is not None:
        hace_un_dia = (ahora - timedelta(days=1)).isoformat()
        en_las_ultimas_24h = _contar_respuestas_desde(agencia["id"], hace_un_dia)
        if en_las_ultimas_24h >= limite_dia:
            return {
                "permitido": False,
                "razon": ("Has alcanzado el máximo de respuestas de las últimas 24 horas para tu plan. "
                          "Esto protege el servicio frente a usos automatizados. Vuelve mañana o "
                          "escríbenos si tu negocio necesita un volumen mayor de forma legítima."),
                "advertencia": None,
            }
        if en_las_ultimas_24h >= limite_dia * 0.85:
            advertencia = "Te acercas al máximo diario de tu plan."

    return {"permitido": True, "razon": None, "advertencia": advertencia}


def redirigir_a_stripe(url_pago):
    """
    Lleva al usuario a la pasarela de pago de Stripe. Intenta un salto automático
    (window.top para escapar del iframe interno de Streamlit; Stripe bloquea la carga
    dentro de iframes con "not able to run in an iFrame"). Si el navegador bloquea el
    salto automático, mostramos un botón grande y llamativo con el enlace, en vez del
    típico enlace de texto pequeño que pasa desapercibido.
    """
    # Intento de salto automático al nivel superior de la ventana.
    st.markdown(
        f"""<script>window.top.location.href = "{url_pago}";</script>""",
        unsafe_allow_html=True
    )
    # Botón-enlace bonito como respaldo (y como acción principal si el salto se bloquea).
    boton_pago_html = (
        '<div style="margin:14px 0; text-align:center;">'
        f'<a href="{url_pago}" target="_top" '
        'style="display:inline-block; background:linear-gradient(135deg,#635BFF,#8B7BFF); '
        'color:#FFFFFF; font-weight:700; font-size:1.05rem; text-decoration:none; '
        'padding:14px 32px; border-radius:12px; box-shadow:0 4px 14px rgba(99,91,255,0.45); '
        'letter-spacing:0.01em;">🔒 Continuar al pago seguro con Stripe →</a>'
        '<div style="color:#8B95A8; font-size:0.8rem; margin-top:8px;">'
        'Se abre la pasarela cifrada de Stripe. Si no salta sola, pulsa el botón.</div>'
        '</div>'
    )
    st.markdown(boton_pago_html, unsafe_allow_html=True)


def render_pagina_planes_upgrade(agencia, color_agencia):
    """
    Página de actualización de plan para usuarios ya logueados. Muestra las tarjetas
    de plan (igual que en la landing) en formato compacto; solo al elegir uno se genera la
    sesión de pago, ya ligada a la agencia.
    """
    if st.button("← Volver a mi panel"):
        st.session_state.mostrar_pagina_planes = False
        st.rerun()

    st.markdown(f"### Tu plan actual: {agencia.get('plan', 'free').capitalize()}")

    ciclo_up = st.radio(
        "Facturación:", ["Mensual", f"Anual (−{int(DESCUENTO_ANUAL*100)}%)"],
        horizontal=True, key="ciclo_upgrade"
    )
    es_anual_up = ciclo_up.startswith("Anual")
    if es_anual_up:
        st.caption(f"💚 Pagando por año te ahorras un {int(DESCUENTO_ANUAL*100)}%.")

    columnas = st.columns(len(PLANES_AUTOSERVICIO))

    for columna, (clave_plan, datos_plan) in zip(columnas, PLANES_AUTOSERVICIO.items()):
        with columna:
            with st.container(border=True):
                es_plan_actual = agencia.get("plan") == clave_plan
                st.markdown(f"**{datos_plan['nombre']}**")
                if es_anual_up:
                    anual_total = _precio_anual_total(datos_plan["precio_mensual"])
                    equivalente_mes = round(anual_total / 12)
                    st.markdown(f"## {anual_total}€/año")
                    st.caption(f"~~{datos_plan['precio_mensual']*12}€~~ · equivale a {equivalente_mes}€/mes")
                else:
                    st.markdown(f"## {datos_plan['precio_mensual']}€/mes")
                for feature in datos_plan["features"]:
                    st.caption(f"✓ {feature}")
                if es_plan_actual:
                    st.success("Tu plan actual")
                elif st.button(f"Elegir {datos_plan['nombre']}", key=f"elegir_{clave_plan}", use_container_width=True, type="primary"):
                    price_id = datos_plan["price_ids"]["anual" if es_anual_up else "mensual"]
                    url_pago = crear_sesion_pago_stripe(agencia["id"], clave_plan, price_id)
                    if url_pago:
                        redirigir_a_stripe(url_pago)


def cargar_perfil_login(email):
    """
    Busca el usuario por email y, si existe y está activo, devuelve también los
    datos de la agencia a la que pertenece y su cartera de locales.
    Devuelve None si el email no existe.
    """
    resultado_usuario = supabase.table("usuarios").select("*").eq("email", email).eq("activo", True).execute()
    if not resultado_usuario.data:
        return None

    usuario = resultado_usuario.data[0]

    resultado_agencia = supabase.table("agencias").select("*").eq("id", usuario["agencia_id"]).execute()
    if not resultado_agencia.data:
        return None
    agencia = resultado_agencia.data[0]

    resultado_locales = supabase.table("locales").select("*").eq("agencia_id", usuario["agencia_id"]).execute()

    return {
        "usuario": usuario,
        "agencia": agencia,
        "locales": resultado_locales.data or []
    }


def registrar_respuesta_en_historico(agencia_id, local_id, usuario_id, sentimiento, idioma_detectado,
                                      longitud_palabras, resena_cliente=None, respuesta_generada=None):
    """Guarda una fila en historico_respuestas cada vez que se genera una respuesta con éxito.

    resena_cliente / respuesta_generada se guardan truncados a 300 caracteres como
    extracto, solo para poder mostrar un "caso destacado" real en el informe PDF
    — no es un archivo completo de todas las reseñas, es solo un resumen corto.
    """
    try:
        fila = {
            "agencia_id": agencia_id,
            "local_id": local_id,
            "usuario_id": usuario_id,
            "sentimiento": sentimiento,
            "idioma_detectado": idioma_detectado,
            "longitud_palabras": longitud_palabras,
        }
        if resena_cliente:
            fila["extracto_resena"] = resena_cliente.strip()[:300]
        if respuesta_generada:
            fila["extracto_respuesta"] = respuesta_generada.strip()[:300]
        supabase.table("historico_respuestas").insert(fila).execute()
    except Exception:
        # Si falla el registro de analítica, no debe romper la generación de la respuesta.
        pass


def registrar_contenido_seo_generado(agencia_id, local_id, usuario_id, tipo_contenido):
    """Guarda un evento cada vez que se genera una pieza de contenido SEO extra
    (post de Google Business, descripción para redes, meta descripción), para
    que el informe PDF pueda mostrar esa actividad. Nunca debe romper la
    generación de contenido si falla."""
    try:
        supabase.table("historico_contenido_seo").insert({
            "agencia_id": agencia_id,
            "local_id": local_id,
            "usuario_id": usuario_id,
            "tipo_contenido": tipo_contenido,
        }).execute()
    except Exception:
        pass


# Inicializar los estados de sesión si no existen
if "sesion_activa" not in st.session_state:
    st.session_state.sesion_activa = False
if "usuario_actual" not in st.session_state:
    st.session_state.usuario_actual = None
if "agencia_actual" not in st.session_state:
    st.session_state.agencia_actual = None
if "locales_agencia" not in st.session_state:
    st.session_state.locales_agencia = []
if "local_activo" not in st.session_state:
    st.session_state.local_activo = None
if "vista_landing" not in st.session_state:
    st.session_state.vista_landing = "info"
if "mostrar_pagina_planes" not in st.session_state:
    st.session_state.mostrar_pagina_planes = False
if "alta_pendiente" not in st.session_state:
    st.session_state.alta_pendiente = None

# =========================================================
# 💳 VUELTA DESDE STRIPE: activación automática del plan
# Esto se comprueba nada más cargar la app, tanto si la sesión de Streamlit
# se ha mantenido como si no (el redirect a Stripe y de vuelta a veces la
# resetea) — por eso la activación se basa en la metadata de Stripe, no en
# el estado de sesión.
# =========================================================
parametros_url = st.query_params
if parametros_url.get("pago_exito") == "1" and "session_id" in parametros_url:
    session_id_pago = parametros_url["session_id"]
    if st.session_state.get("ultima_sesion_pago_confirmada") != session_id_pago:
        ok_pago, resultado_pago = confirmar_pago_y_activar_plan(session_id_pago)
        if ok_pago:
            st.session_state.ultima_sesion_pago_confirmada = session_id_pago
            st.session_state.mostrar_pagina_planes = False
            # Si la sesión de Streamlit se mantuvo, refrescamos el plan en memoria al momento.
            if st.session_state.sesion_activa and st.session_state.agencia_actual:
                st.session_state.agencia_actual["plan"] = resultado_pago
            st.success(f"✅ ¡Pago confirmado! Tu plan '{resultado_pago}' ya está activo.")
        else:
            st.error(redactar_secretos(f"No se pudo confirmar el pago automáticamente: {resultado_pago}. Escríbenos si el cargo sí se realizó."))
    st.query_params.clear()
elif parametros_url.get("alta_nueva") == "1" and "session_id" in parametros_url:
    session_id_alta = parametros_url["session_id"]
    if st.session_state.get("alta_completada_session_id") != session_id_alta:
        if not st.session_state.alta_pendiente or st.session_state.alta_pendiente.get("session_id") != session_id_alta:
            ok_alta, datos_alta = verificar_pago_alta_nueva(session_id_alta)
            if ok_alta:
                st.session_state.alta_pendiente = datos_alta
            else:
                st.error(redactar_secretos(f"No se pudo verificar el pago: {datos_alta}. Escríbenos si el cargo sí se realizó."))
    st.query_params.clear()
elif parametros_url.get("pago_cancelado") == "1":
    st.info("Has cancelado el proceso de pago. No se ha realizado ningún cargo.")
    st.query_params.clear()

# =========================================================
# 🔑 LANDING: PLANES Y PRECIOS + LOGIN
# =========================================================
if not st.session_state.sesion_activa and st.session_state.alta_pendiente:
    st.markdown('<div class="rp-hero-title" style="font-size:1.8rem;">Ya casi está 🎉</div>', unsafe_allow_html=True)
    render_formulario_alta_pendiente()
    st.stop()

if not st.session_state.sesion_activa:

    st.markdown("""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
        html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }
        .rp-hero-title {
            font-family: 'Fraunces', serif; font-weight: 700; font-size: 2.6rem;
            color: #F5F7FA; line-height: 1.15; margin-bottom: 0.3rem;
        }
        .rp-hero-sub { color: #8B95A8; font-size: 1.05rem; margin-bottom: 1.8rem; }
        .rp-card {
            background: #131B2E; border: 1px solid #232C42; border-radius: 14px;
            padding: 26px 22px; height: 100%;
        }
        .rp-card-destacado { border: 1px solid #FFB454; box-shadow: 0 0 0 1px #FFB45433; }
        .rp-plan-nombre { font-family: 'Fraunces', serif; font-size: 1.3rem; color: #F5F7FA; margin-bottom: 2px; }
        .rp-plan-target { color: #8B95A8; font-size: 0.85rem; margin-bottom: 14px; }
        .rp-precio { font-family: 'IBM Plex Sans', monospace; font-size: 2rem; font-weight: 600; color: #FFB454; }
        .rp-precio-periodo { color: #8B95A8; font-size: 0.9rem; }
        .rp-feature { color: #C7CDDB; font-size: 0.88rem; margin: 6px 0; }
        .rp-badge { display:inline-block; background:#FFB45422; color:#FFB454; font-size:0.72rem;
            padding: 3px 10px; border-radius: 20px; margin-bottom: 10px; font-weight:600; letter-spacing: 0.03em; }
        .rp-badge-verde { display:inline-block; background:#2ECC7122; color:#2ECC71; font-size:0.72rem;
            padding: 3px 10px; border-radius: 20px; margin-bottom: 10px; font-weight:600; letter-spacing: 0.03em; }
        .rp-precio-tachado { color:#6B7488; font-size:1rem; text-decoration:line-through; margin-right:6px; }
        .rp-precio-ahorro { color:#2ECC71; font-size:0.8rem; font-weight:600; margin-top:2px; }
        .rp-por-local { color:#8BD1F7; font-size:0.82rem; font-weight:600; margin-top:6px; }
        .rp-gancho { color:#9AA4B8; font-size:0.82rem; font-style:italic; margin-top:10px; min-height:34px; }
        .rp-roi-banner {
            background:linear-gradient(135deg,#10241B,#15321F); border:1px solid #2ECC71;
            border-radius:14px; padding:16px 22px; margin:6px 0 20px 0;
        }
        .rp-roi-banner strong { color:#2ECC71; }
        .rp-garantia { color:#8B95A8; font-size:0.85rem; text-align:center; margin-top:14px; }
        </style>
    """, unsafe_allow_html=True)

    st.markdown('<div class="rp-hero-title">Deja de improvisar respuestas<br>a las reseñas de tus clientes.</div>', unsafe_allow_html=True)
    st.markdown('<div class="rp-hero-sub">ReviewPro Enterprise redacta respuestas profesionales, con tu marca y con SEO integrado, para toda la cartera de tu agencia.</div>', unsafe_allow_html=True)

    # -----------------------------------------------------
    # VISTA 1: INFO — presentación del producto antes de pedir nada
    # -----------------------------------------------------
    if st.session_state.vista_landing == "info":
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            st.markdown("""<div class="rp-card">
                <div class="rp-plan-nombre" style="font-size:1.05rem;">🛡️ Blindaje legal</div>
                <div class="rp-feature">Nunca admite negligencias ni usa términos de alerta sanitaria. Cada respuesta pasa por reglas de redacción pensadas para proteger la reputación del negocio.</div>
            </div>""", unsafe_allow_html=True)
        with col_b:
            st.markdown("""<div class="rp-card">
                <div class="rp-plan-nombre" style="font-size:1.05rem;">🔎 SEO invisible</div>
                <div class="rp-feature">Cada respuesta integra de forma natural las palabras clave de posicionamiento de ese local concreto, sin que se note ni al cliente final ni a Google.</div>
            </div>""", unsafe_allow_html=True)
        with col_c:
            st.markdown("""<div class="rp-card">
                <div class="rp-plan-nombre" style="font-size:1.05rem;">🏢 Marca blanca real</div>
                <div class="rp-feature">Tu agencia entra con su propio logo y color corporativo. Tus clientes ven tu marca, no la nuestra.</div>
            </div>""", unsafe_allow_html=True)

        st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)
        col_cta1, col_cta2 = st.columns(2)
        with col_cta1:
            if st.button("🔑 Ya tengo cuenta — Iniciar sesión", use_container_width=True):
                st.session_state.vista_landing = "login"
                st.rerun()
        with col_cta2:
            if st.button("🚀 Ver planes y empezar", use_container_width=True, type="primary"):
                st.session_state.vista_landing = "planes"
                st.rerun()
        st.stop()

    # Botón para volver a la info desde las otras dos vistas
    if st.button("← Volver"):
        st.session_state.vista_landing = "info"
        st.rerun()

    mostrar_planes = st.session_state.vista_landing == "planes"
    mostrar_login = st.session_state.vista_landing == "login"

    if mostrar_planes:
        st.caption("¿Ya tienes cuenta? Usa el botón '← Volver' de arriba y elige 'Iniciar sesión'.")
    if mostrar_login:
        st.caption("¿Todavía no tienes cuenta? Usa el botón '← Volver' de arriba y elige 'Ver planes'.")

    # -----------------------------------------------------
    # VISTA: PLANES Y PRECIOS
    # -----------------------------------------------------
    if mostrar_planes:
        # --- Gancho de valor: el ROI antes que el precio ---
        st.markdown("""
            <div class="rp-roi-banner">
                Una sola estrella de más en Google puede suponer entre un <strong>5% y un 9% más de ingresos</strong>
                para un negocio (estudio de Harvard Business School). Para un local que factura 60.000&nbsp;€/mes,
                eso son hasta <strong>32.000&nbsp;€ más al año</strong>. Gestionar bien tus reseñas cuesta, aquí,
                desde 25&nbsp;€ al mes.
            </div>
        """, unsafe_allow_html=True)

        # --- Toggle mensual / anual ---
        col_toggle, _ = st.columns([1, 2])
        with col_toggle:
            ciclo = st.radio(
                "Facturación:",
                ["Mensual", f"Anual (−{int(DESCUENTO_ANUAL*100)}%)"],
                horizontal=True, key="ciclo_facturacion", label_visibility="collapsed"
            )
        es_anual = ciclo.startswith("Anual")
        if es_anual:
            st.caption(f"💚 Pagando por año te ahorras un {int(DESCUENTO_ANUAL*100)}% — equivale a llevarte más de 2 meses gratis.")

        def _bloque_precio(precio_mensual):
            """Devuelve el HTML del precio según el ciclo elegido, con anclaje visual.
            En anual, el número grande es el TOTAL del año (lo que se paga de una vez)."""
            if es_anual:
                anual_total = _precio_anual_total(precio_mensual)
                anual_sin_dto = precio_mensual * 12
                ahorro_anual = anual_sin_dto - anual_total
                equivalente_mes = round(anual_total / 12)
                return (
                    f'<span class="rp-precio-tachado">{anual_sin_dto}€</span>'
                    f'<span class="rp-precio">{anual_total}€</span>'
                    f'<span class="rp-precio-periodo"> / año</span>'
                    f'<div class="rp-precio-ahorro">Equivale a {equivalente_mes}€/mes · ahorras {ahorro_anual}€ al año</div>'
                )
            return f'<span class="rp-precio">{precio_mensual}€</span><span class="rp-precio-periodo"> / mes</span>'

        def _por_local(clave_plan, datos_plan):
            """Texto '€/local' para reforzar lo barato que sale en planes multi-local."""
            limite = LIMITE_LOCALES_POR_PLAN.get(clave_plan)
            if not limite or limite <= 1:
                return ""
            if es_anual:
                base = round(_precio_anual_total(datos_plan["precio_mensual"]) / 12)
            else:
                base = datos_plan["precio_mensual"]
            return f'<div class="rp-por-local">≈ {base/limite:.1f}€ por local / mes</div>'

        # --- Fila 1: Free + Individual (el negocio suelto) ---
        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
        col_free, col_individual = st.columns(2)

        with col_free:
            free_html = (
                '<div class="rp-card">'
                '<div class="rp-plan-nombre">Free</div>'
                '<div class="rp-plan-target">Para probar antes de decidir</div>'
                '<div class="rp-precio">0€</div>'
                '<div class="rp-precio-periodo">para siempre</div>'
                '<hr style="border-color:#232C42; margin:14px 0;">'
                '<div class="rp-feature">✓ 1 local de prueba</div>'
                f'<div class="rp-feature">✓ {LIMITE_USOS_PLAN_GRATIS} respuestas / mes</div>'
                '<div class="rp-feature">✓ Sin tarjeta de crédito</div>'
                '<div class="rp-feature" style="opacity:0.4;">✗ Marca blanca</div>'
                '<div class="rp-gancho">Ideal para ver la calidad de las respuestas sin compromiso.</div>'
                '</div>'
            )
            st.markdown(free_html, unsafe_allow_html=True)
            with st.popover("Empezar gratis", use_container_width=True):
                st.caption("Crea tu cuenta en 30 segundos. Sin tarjeta.")
                nombre_agencia_free = st.text_input("Nombre de tu agencia o negocio", key="free_nombre_agencia")
                nombre_local_free = st.text_input("Nombre del primer local a probar", key="free_nombre_local")
                nombre_usuario_free = st.text_input("Tu nombre", key="free_nombre_usuario")
                email_free = st.text_input("Email", key="free_email")
                password_free = st.text_input("Contraseña (mín. 8 caracteres)", type="password", key="free_password")
                if st.button("Crear cuenta gratis", key="free_submit", use_container_width=True):
                    if not all([nombre_agencia_free, nombre_local_free, nombre_usuario_free, email_free, password_free]):
                        st.warning("Rellena todos los campos.")
                    else:
                        ok, error = registrar_agencia_gratuita(
                            nombre_agencia_free, nombre_local_free, email_free, password_free, nombre_usuario_free
                        )
                        if ok:
                            st.success("Cuenta creada. Ve a la pestaña 'Ya tengo cuenta' para iniciar sesión.")
                        else:
                            st.error(error)

        with col_individual:
            plan_ind = PLANES_AUTOSERVICIO["individual"]
            features_ind = "".join(f'<div class="rp-feature">✓ {f}</div>' for f in plan_ind["features"])
            individual_html = (
                '<div class="rp-card">'
                '<span class="rp-badge-verde">PARA UN SOLO LOCAL</span>'
                f'<div class="rp-plan-nombre">{plan_ind["nombre"]}</div>'
                f'<div class="rp-plan-target">{plan_ind["target"]}</div>'
                f'{_bloque_precio(plan_ind["precio_mensual"])}'
                '<hr style="border-color:#232C42; margin:14px 0;">'
                f'{features_ind}'
                f'<div class="rp-gancho">{plan_ind["gancho"]}</div>'
                '</div>'
            )
            st.markdown(individual_html, unsafe_allow_html=True)
            if st.button("Empezar con Individual", key="landing_elegir_individual", use_container_width=True, type="primary"):
                price_id_ind = plan_ind["price_ids"]["anual" if es_anual else "mensual"]
                url_pago = crear_sesion_pago_nueva_agencia("individual", price_id_ind)
                if url_pago:
                    redirigir_a_stripe(url_pago)

        # --- Fila 2: planes de agencia ---
        st.markdown("<div style='height:26px'></div>", unsafe_allow_html=True)
        st.markdown('<div class="rp-plan-target" style="font-size:0.95rem; margin-bottom:8px;">¿Gestionas varios locales? Planes para agencias:</div>', unsafe_allow_html=True)
        col_starter, col_growth, col_ent = st.columns(3)

        planes_agencia = [
            ("starter", col_starter, "landing_elegir_starter"),
            ("growth", col_growth, "landing_elegir_growth"),
            ("enterprise", col_ent, "landing_elegir_enterprise"),
        ]
        for clave_plan, columna, boton_key in planes_agencia:
            datos = PLANES_AUTOSERVICIO[clave_plan]
            with columna:
                clase_card = "rp-card rp-card-destacado" if datos.get("destacado") else "rp-card"
                badge = '<span class="rp-badge">MÁS ELEGIDO</span>' if datos.get("destacado") else ""
                features = "".join(f'<div class="rp-feature">✓ {f}</div>' for f in datos["features"])
                tarjeta_html = (
                    f'<div class="{clase_card}">'
                    f'{badge}'
                    f'<div class="rp-plan-nombre">{datos["nombre"]}</div>'
                    f'<div class="rp-plan-target">{datos["target"]}</div>'
                    f'{_bloque_precio(datos["precio_mensual"])}'
                    f'{_por_local(clave_plan, datos)}'
                    f'<hr style="border-color:#232C42; margin:14px 0;">'
                    f'{features}'
                    f'<div class="rp-gancho">{datos["gancho"]}</div>'
                    f'</div>'
                )
                st.markdown(tarjeta_html, unsafe_allow_html=True)
                tipo_boton = "primary" if datos.get("destacado") else "secondary"
                if st.button(f"Elegir {datos['nombre']}", key=boton_key, use_container_width=True, type=tipo_boton):
                    price_id = datos["price_ids"]["anual" if es_anual else "mensual"]
                    url_pago = crear_sesion_pago_nueva_agencia(clave_plan, price_id)
                    if url_pago:
                        redirigir_a_stripe(url_pago)

        st.markdown("""
            <div class="rp-garantia">
                Sin permanencia · Cancela cuando quieras · Al pagar vuelves aquí para crear tu cuenta al instante,
                sin esperas ni llamadas comerciales.
            </div>
        """, unsafe_allow_html=True)

    # -----------------------------------------------------
    # VISTA: LOGIN
    # -----------------------------------------------------
    if mostrar_login:
        st.markdown("Introduce tu email y contraseña personales. Cada usuario de tu agencia tiene su propio acceso.")

        email_usuario = st.text_input("Email de usuario:")
        password_usuario = st.text_input("Contraseña:", type="password")

        if st.button("🚪 Iniciar sesión", use_container_width=True):
            if not email_usuario.strip() or not password_usuario:
                st.warning("Introduce email y contraseña.")
            else:
                email_normalizado = email_usuario.lower().strip()
                with st.spinner("Verificando credenciales..."):
                    try:
                        perfil = cargar_perfil_login(email_normalizado)

                        if perfil is None:
                            st.error("❌ Email o contraseña incorrectos.")
                        elif not verificar_password(password_usuario, perfil["usuario"]["password_hash"]):
                            st.error("❌ Email o contraseña incorrectos.")
                        else:
                            st.session_state.sesion_activa = True
                            st.session_state.usuario_actual = perfil["usuario"]
                            st.session_state.agencia_actual = perfil["agencia"]
                            st.session_state.locales_agencia = perfil["locales"]
                            st.success(f"🔋 Bienvenido, {perfil['usuario']['nombre_usuario']}.")
                            st.rerun()
                    except Exception as e:
                        st.error(redactar_secretos(f"Error de conexión con la base de datos: {e}"))

    st.stop()

# A partir de aquí: sesión válida.
agencia = st.session_state.agencia_actual
usuario = st.session_state.usuario_actual
color_agencia = agencia["color_marca"]

# Si el usuario ha pulsado "Actualizar plan" / "Ver planes de pago", mostramos la
# comparativa de planes dentro del propio panel en vez de saltar directo a Stripe.
if st.session_state.mostrar_pagina_planes:
    render_pagina_planes_upgrade(agencia, color_agencia)
    st.stop()

# =========================================================
# 🎨 CSS DINÁMICO — MARCA BLANCA POR AGENCIA
# =========================================================
st.markdown(f"""
    <style>
    #MainMenu, header, footer, .stAppDeployButton, .viewerBadge_container__1QS1h {{
        display: none !important;
        visibility: hidden !important;
    }}
    div[data-testid="stDecoration"], div[data-testid="stToolbar"],
    div[data-testid="stMainMenu"], div[data-testid="stHeader"] {{
        display: none !important;
        height: 0px !important;
    }}
    div[data-testid="stFormSubmitButton"] button {{
        background-color: {color_agencia} !important;
        border-color: {color_agencia} !important;
        color: #ffffff !important;
        font-weight: bold !important;
    }}
    div[data-testid="stFormSubmitButton"] button:hover {{
        opacity: 0.88 !important;
    }}
    </style>
    """, unsafe_allow_html=True)

# =========================================================
# 🏢 CABECERA DE MARCA BLANCA
# =========================================================
col_logo, col_titulo, col_cuenta = st.columns([1, 3, 1])
with col_logo:
    st.image(agencia["logo_url"], use_container_width=True)
with col_titulo:
    st.markdown(f"<h2 style='margin-bottom:0; padding-top:8px;'>Console | {agencia['nombre_agencia']}</h2>", unsafe_allow_html=True)
with col_cuenta:
    st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)
    if st.button("🔒 Salir"):
        for key in ["sesion_activa", "usuario_actual", "agencia_actual", "locales_agencia", "local_activo"]:
            st.session_state[key] = False if key == "sesion_activa" else None if "actual" in key else []
        st.session_state.vista_landing = "info"
        st.rerun()

st.markdown(f"<hr style='border-top:3px solid {color_agencia}; margin-top:4px;'>", unsafe_allow_html=True)
st.info(f"Sesión activa: **{usuario['nombre_usuario']}** ({usuario['email']}) · Rol: {usuario['rol']}")



# =========================================================
# 🧭 NAVEGACIÓN: GENERAR RESPUESTA / VER ANALÍTICA
# =========================================================
tab_generar, tab_pedir_resenas, tab_seo_extra, tab_analitica = st.tabs(
    ["✨ Generar respuesta", "📣 Pedir reseñas", "📝 Contenido SEO extra", "📊 Analítica de la agencia"]
)

# ---------------------------------------------------------
# PESTAÑA 1: GENERACIÓN DE RESPUESTAS
# ---------------------------------------------------------
with tab_generar:
    locales_disponibles = st.session_state.locales_agencia

    # ---- Añadir un nuevo establecimiento (respetando el límite del plan) ----
    limite_locales = LIMITE_LOCALES_POR_PLAN.get(agencia.get("plan", "growth"))
    texto_limite = "sin límite" if limite_locales is None else f"{len(locales_disponibles)}/{limite_locales}"
    with st.expander(f"➕ Añadir establecimiento ({texto_limite})"):
        nombre_nuevo_local = st.text_input("Nombre del establecimiento", key="nuevo_local_nombre")
        nicho_nuevo_local = st.text_input("Nicho (ej: hotel, restaurante, clínica dental)", key="nuevo_local_nicho")
        keywords_nuevo_local = st.text_input("Palabras clave SEO, separadas por comas", key="nuevo_local_keywords")
        if st.button("Crear establecimiento", key="crear_establecimiento_btn"):
            puede, motivo = puede_agencia_anadir_local(agencia, locales_disponibles)
            if not puede:
                st.error(redactar_secretos(f"⚠️ {motivo}"))
                if st.button("Actualizar plan", key="actualizar_plan_limite_locales"):
                    st.session_state.mostrar_pagina_planes = True
                    st.rerun()
            elif not nombre_nuevo_local.strip() or not nicho_nuevo_local.strip():
                st.warning("Rellena al menos el nombre y el nicho.")
            else:
                try:
                    keywords_lista = [k.strip() for k in keywords_nuevo_local.split(",") if k.strip()]
                    nuevo = supabase.table("locales").insert({
                        "agencia_id": agencia["id"],
                        "nombre": nombre_nuevo_local.strip(),
                        "nicho": nicho_nuevo_local.strip(),
                        "seo_keywords": keywords_lista
                    }).execute()
                    st.session_state.locales_agencia.append(nuevo.data[0])
                    st.success(f"'{nombre_nuevo_local}' añadido correctamente.")
                    st.rerun()
                except Exception as e:
                    st.error(redactar_secretos(f"No se pudo crear el local: {e}"))

    if not locales_disponibles:
        st.info("Añade tu primer establecimiento arriba para empezar a generar respuestas.")
        st.stop()

    nombres_locales = [local["nombre"] for local in locales_disponibles]
    nombre_local_elegido = st.selectbox("🏬 Selecciona el local:", options=nombres_locales, key="selector_local_activo")

    local_activo = next(local for local in locales_disponibles if local["nombre"] == nombre_local_elegido)
    st.session_state.local_activo = local_activo

    st.caption(f"Nicho: **{local_activo['nicho']}** · {len(local_activo['seo_keywords'])} keywords SEO cargadas.")

    plan_actual = agencia.get("plan", "growth")
    limite_usos_plan = LIMITE_USOS_POR_PLAN.get(plan_actual, None)
    if limite_usos_plan is not None:
        usos_hechos = contar_usos_del_mes(agencia["id"])
        restantes = max(0, limite_usos_plan - usos_hechos)
        nombre_plan_legible = PLANES_AUTOSERVICIO.get(plan_actual, {}).get("nombre", plan_actual.capitalize())
        st.info(f"Plan {nombre_plan_legible}: te quedan **{restantes} de {limite_usos_plan}** respuestas este mes.")
    else:
        usos_local_este_mes = contar_usos_del_mes_por_local(local_activo["id"])
        if usos_local_este_mes >= UMBRAL_ACTIVIDAD_INUSUAL_POR_LOCAL:
            st.warning(f"📈 Este local ha generado {usos_local_este_mes} respuestas este mes — un volumen inusualmente alto. Si no es un cliente real de mucho tráfico, te recomendamos revisarlo.")

    with st.form("review_form"):
        nombre_negocio = st.text_input("Nombre del establecimiento", value=local_activo["nombre"], disabled=True)
        resena_cliente = st.text_area("Pega aquí la reseña del cliente", height=150)
        tono = st.select_slider("Tono deseado", options=["Muy formal", "Profesional estándar", "Cercano y cálido"], value="Profesional estándar")
        acepta_terminos = st.checkbox("Acepto los Términos de Uso y el Descargo de Responsabilidad legal.", value=False)
        submit = st.form_submit_button("✨ Generar respuesta profesional", use_container_width=True)

    if submit:
        if not resena_cliente.strip():
            st.warning("Por favor, pega la reseña del cliente.")
        elif not acepta_terminos:
            st.error("⚠️ Es obligatorio aceptar los términos de uso.")
        elif limite_usos_plan is not None and contar_usos_del_mes(agencia["id"]) >= limite_usos_plan:
            st.error(redactar_secretos(f"⚠️ Has usado tus {limite_usos_plan} respuestas de este mes en tu plan actual. Actualiza tu plan para seguir generando sin límite."))
            if st.button("💳 Ver planes de pago", key="ver_planes_limite_usos"):
                st.session_state.mostrar_pagina_planes = True
                st.rerun()
        elif not verificar_velocidad(agencia)["permitido"]:
            st.error(redactar_secretos(f"⚠️ {verificar_velocidad(agencia)['razon']}"))
        else:
            _adv_velocidad = verificar_velocidad(agencia).get("advertencia")
            if _adv_velocidad:
                st.info(_adv_velocidad)
            with st.spinner("Analizando el idioma y el tono de la reseña..."):
                try:
                    nombre_local_final = local_activo["nombre"]
                    nicho_local = local_activo["nicho"]
                    keywords_texto = ", ".join(local_activo["seo_keywords"])

                    guias_de_tono = {
                        "Muy formal": """- Registro protocolario, de "usted" siempre. Frases completas, sin contracciones coloquiales.
- Puedes usar UNA fórmula de cortesía clásica ("Estimado/a cliente,"), pero solo una vez, no la repitas al final.
- Evita cualquier expresión desenfadada, emoji o exclamación. Precisión y corrección ante todo.
- Ejemplo de arranque válido (no lo copies literal, es solo el registro): "Agradecemos que se haya tomado el tiempo de trasladarnos su valoración." """,
                        "Profesional estándar": """- Registro cordial de "usted", pero natural, como hablaría el propio gerente del negocio, no un departamento de atención al cliente.
- Frases de longitud variada: alterna alguna corta con otras más largas, evita que todas midan lo mismo.
- Puedes usar una exclamación puntual si el contexto lo pide, sin abusar.
- Ejemplo de arranque válido (no lo copies literal): "Vaya, sentimos mucho que la cosa fuera así." """,
                        "Cercano y cálido": """- Registro de tú o de un "usted" muy relajado según convenga al nicho, con contracciones naturales del español hablado ("no es lo que", "nos ha sabido mal", "vaya chasco").
- Suena a que lo ha escrito el propio dueño del negocio en dos minutos libres, no un community manager. Frases cortas, directas, alguna incluso de una sola línea.
- Se permite un emoji sutil como mucho, nunca más de uno, y solo si encaja con el nicho (evítalo en clínicas, notarías, funerarias, etc.).
- Ejemplo de arranque válido (no lo copies literal): "Uf, leer esto nos ha sentado fatal, la verdad." """
                    }
                    guia_tono_activa = guias_de_tono.get(tono, guias_de_tono["Profesional estándar"])

                    system_prompt_dinamico = f"""Eres la persona que gestiona de verdad las reseñas de "{nombre_local_final}": el dueño, el gerente o el responsable de sala, escribiendo entre turnos, no un departamento de relaciones públicas. Tu tarea es redactar una respuesta pública a una reseña que puede ser POSITIVA o NEGATIVA, y que suene a una persona real de carne y hueso, no a una plantilla corporativa.

Debes devolver EXCLUSIVAMENTE un objeto JSON válido, sin texto adicional antes ni después, sin bloques de código markdown, con esta estructura exacta:
{{
  "idioma_detectado": "código de idioma ISO de dos letras, ej: es, en, fr",
  "sentimiento": "positivo" o "negativo",
  "respuesta_nativa": "la respuesta redactada en el idioma original de la reseña",
  "traduccion_espanol": "traducción literal al español para el propietario, o null si la reseña ya estaba en español"
}}

NORMAS DE IDIOMA Y CONTEXTO ABSOLUTAS:
- Analiza minuciosamente el idioma de la reseña y responde de forma nativa en ese mismo idioma.
- REGLA FRANCESA CRÍTICA: en francés, usa únicamente fórmulas de cortesía formal ("vous", "votre", "vos"); prohibido tutear.
- CONTROL DE ALUCINACIÓN DE MARCA: el único nombre de establecimiento válido es: {nombre_local_final}. No inventes otro.

GUÍA DE TONO — {tono}:
{guia_tono_activa}

CÓMO SONAR HUMANO Y NO A IA (esto es lo más importante de todo el prompt):
- Elige UN SOLO hilo emocional o UN SOLO detalle concreto de la reseña y desarróllalo con algo de profundidad, en vez de contestar la reseña punto por punto como una checklist ("en cuanto a X... en cuanto a Y... en cuanto a Z..."). Un cliente real no organiza su respuesta por categorías, reacciona a lo que más le ha dolido o alegrado.
- Máximo UNA frase de apertura empática (tipo "lamentamos..." o "nos alegra..."). Prohibido encadenar varias frases de validación emocional seguidas (nada de "lamentamos... entendemos... valoramos..." una detrás de otra). Esa cadencia es la huella más reconocible de un texto generado por IA y hace que suene idéntico al de cualquier otro negocio.
- PROHIBIDO usar estas muletillas de plantilla, están quemadas de tanto verlas en internet: "nuestros estándares de calidad", "lo sucedido", "investigar a fondo", "su opinión es muy valiosa para nosotros/para seguir mejorando", "no reflejan nuestro compromiso habitual", "dista mucho de la experiencia que deseamos ofrecer", "reforzar la formación de nuestro equipo". Si necesitas decir algo parecido, dilo con tus propias palabras, distintas cada vez.
- Varía la longitud de las frases dentro de la misma respuesta: alguna corta y directa, otra más desarrollada. Un texto donde todas las frases miden parecido suena a máquina.
- Referencia al menos un detalle textual y específico de la reseña (una palabra, una situación muy concreta que haya mencionado el cliente) en vez de convertir todo en categorías genéricas ("el servicio", "la comida", "los tiempos"). Ese detalle es lo que hace creíble que alguien ha leído de verdad la reseña.

REGLAS DE REDACCIÓN SEGÚN EL SENTIMIENTO:
1. TONO OBLIGADO: el descrito arriba en GUÍA DE TONO. Siempre educado y constructivo, nunca condescendiente.
2. SI ES POSITIVA: agradecimiento genuino (no genérico), referencia a algo concreto que el cliente mencionó, invitación a volver que no suene copiada y pegada.
3. SI ES NEGATIVA:
   - Inicio dinámico: prohibido empezar siempre con "Gracias por su comentario" o equivalentes; varía la apertura.
   - REGLA DE LA VERDAD QUE NO TIENES: quien escribe esta respuesta no estaba en la cocina ni en la sala esa noche, así que nunca puede confirmar ni negar la causa interna concreta de lo que el cliente describe. Se puede validar por completo su experiencia como algo real y lamentable ("lo que usted describe es serio y lo lamento de verdad"), pero esa validación NUNCA se convierte en una confirmación de causa. Valida la experiencia del cliente, no confirmes el mecanismo interno que la causó — esa línea es la más importante de toda esta sección.
   - PROHIBIDO EXPLÍCITAMENTE, aunque suene humano y hasta bien intencionado (son admisiones legales en toda regla): "es un fallo nuestro", "fue culpa nuestra"/"nuestra culpa", "no fue así", "eso no debería haber pasado" seguido de una causa concreta, "se nos escapó", "fallamos en...", "no llegó el aviso/la información", o cualquier frase en primera persona que confirme qué salió mal por dentro del negocio. Tampoco detalles operativos concretos (tiempos de cocción, temperaturas, protocolos de conservación, cadenas de comunicación interna) — describir con ese nivel de detalle lo que se está "revisando" equivale a admitir dónde estuvo el fallo, aunque no se diga con esas palabras exactas.
   - BLINDAJE JURÍDICO TOTAL: prohibido admitir negligencias, explícita o implícitamente, o usar alertas sanitarias ("higiene alimentaria", "intoxicación", "contaminación"); usa perífrasis suaves y naturales, no siempre las mismas palabras. Ante temas de cobro o facturación, prohibido cualquier palabra que implique intención deshonesta ("engañar", "timar", "cobrar de más a propósito", "así se hace siempre" repetido o validado); habla de "un error en la cuenta" o "un cargo que no debería estar ahí", nunca de intención.
   - Nunca invites al cliente a escribir, contactar o resolverlo por otra vía (nada de "escríbenos", "contáctanos", "cuéntanoslo por privado" ni fórmulas parecidas, ni siquiera suavizadas): la respuesta la gestiona una agencia externa, no el propio negocio, así que abrir esa puerta genera una expectativa de seguimiento que luego nadie puede cumplir. La respuesta se queda siempre en una disculpa sincera y humana.
   - ESCALA DE GRAVEDAD (si el caso encaja en varios niveles, aplica siempre el más alto):
     · LEVE — esperas moderadas, comida fría, ruido, un plato flojo, precio percibido como alto: disculpa cercana y humana, sin más, tono ligero, sin dramatizar.
     · MODERADA — trato brusco o seco sin llegar al insulto, error de comanda, cobro indebido o cargo no explicado: reconoce el malestar del cliente con firmeza, sin implicar intención deshonesta ni validar un patrón, compromiso genérico (no detallado) de revisar el cobro o el proceso.
     · GRAVE — insultos, trato humillante o vejatorio, insectos u otros hallazgos en la comida, sospecha de intoxicación, alérgenos mal gestionados: disculpa mucho más contundente en el reconocimiento del daño emocional o físico, sin confirmar la causa interna ni dar detalle operativo (ver regla de la verdad que no tienes). Si hay un menor implicado, redobla el cuidado: reconoce la gravedad para un niño sin entrar en ningún detalle médico ni de procedimiento interno.
     · Para GRAVE, el cierre invitando a "otra oportunidad" (Reglas de longitud, punto d) pasa a ser OPCIONAL: si pedir que vuelvan sonaría fuera de lugar justo después de ese relato, cierra reconociendo que lo entenderías si no lo hacen, en vez de forzar una invitación que suene insensible. Usa criterio.

REGLAS DE LONGITUD:
- POSITIVA: entre 60 y 100 palabras.
- NEGATIVA: entre 140 y 200 palabras como rango habitual, desarrollando: (a) reconocimiento genuino de UN aspecto concreto, sin confirmar causa interna (ver regla de la verdad que no tienes), (b) validación breve de lo que sintió el cliente, (c) qué se va a hacer al respecto, contado en términos humanos y genéricos, nunca como un procedimiento técnico, (d) cierre cordial invitando a otra oportunidad — omisible en casos GRAVES según la escala de gravedad. Sin frases vacías repetidas.
- EXCEPCIÓN CONTROLADA: si la reseña describe genuinamente varios problemas graves y distintos entre sí (por ejemplo, trato humillante Y un hallazgo en la comida en la misma visita) y resumirlos en 200 palabras obligaría a ignorar alguno o a listarlos de forma fría, se permite ampliar hasta un máximo de 280 palabras — nunca más. Esta excepción es solo para casos que de verdad lo justifiquen, no una invitación a alargar por defecto: si la reseña se puede responder bien en el rango habitual, quédate en el rango habitual.
- Nunca fuerces el límite superior si la reseña es muy breve y no lo justifica.

REGLAS COMUNES:
- Integra el nombre del negocio ({nombre_local_final}) de forma fluida, una sola vez si es posible.
- Sin asteriscos, comillas externas, emojis (salvo lo indicado en la guía de tono) ni encabezados.

REGLAS DE SEO (INVISIBLE PARA EL CLIENTE FINAL):
- Nicho del negocio: {nicho_local}.
- Integra de forma fluida y natural al menos 2-3 de estas palabras clave donde el contexto lo permita: {keywords_texto}.
- Nunca menciones que estás optimizando para SEO ni las enumeres como etiquetas.
- La naturalidad del texto y el sonar humano siempre prevalecen sobre la densidad de keywords: si meter una keyword rompe la naturalidad de la frase, prescinde de ella."""

                    response = client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=1800,
                        system=system_prompt_dinamico,
                        messages=[{"role": "user", "content": f"Nombre del negocio: {nombre_local_final}\nReseña: \"\"\"{resena_cliente}\"\"\""}]
                    )

                    texto_bruto = None
                    for bloque in response.content:
                        if getattr(bloque, "type", None) == "text":
                            texto_bruto = bloque.text.strip()
                            break

                    if texto_bruto is None:
                        raise ValueError("La respuesta del modelo no contenía ningún bloque de texto.")

                    texto_bruto = texto_bruto.strip()
                    if texto_bruto.startswith("```"):
                        texto_bruto = texto_bruto.strip("`")
                        if texto_bruto.lower().startswith("json"):
                            texto_bruto = texto_bruto[4:].strip()

                    datos_respuesta = json.loads(texto_bruto)

                    respuesta_nativa = datos_respuesta.get("respuesta_nativa", "").replace("*", "").replace('"', "")
                    traduccion = datos_respuesta.get("traduccion_espanol")
                    idioma_detectado = datos_respuesta.get("idioma_detectado", "es")
                    sentimiento = datos_respuesta.get("sentimiento", "positivo")

                    st.success("Respuesta generada con éxito:")

                    if traduccion:
                        st.subheader("📋 Texto para copiar y pegar en tu reseña:")
                        st.caption("Respuesta oficial (Nativa) — pasa el ratón por encima para copiar:")
                        st.code(respuesta_nativa, language=None, wrap_lines=True)
                        st.info(f"🌐 **Traducción al español para el propietario:**\n\n{traduccion}")
                    else:
                        st.caption("Copia este texto y pégalo directamente — pasa el ratón por encima para copiar:")
                        st.code(respuesta_nativa, language=None, wrap_lines=True)

                    registrar_respuesta_en_historico(
                        agencia_id=agencia["id"],
                        local_id=local_activo["id"],
                        usuario_id=usuario["id"],
                        sentimiento=sentimiento,
                        idioma_detectado=idioma_detectado,
                        longitud_palabras=len(respuesta_nativa.split()),
                        resena_cliente=resena_cliente,
                        respuesta_generada=respuesta_nativa
                    )

                except json.JSONDecodeError:
                    st.error("El modelo devolvió un formato inesperado. Inténtalo de nuevo.")
                except Exception as e:
                    causa_raiz = log_error_completo("generar respuesta a reseña", e)
                    st.error(redactar_secretos(f"Error al conectar con el servidor: {type(e).__name__}: {e}"))
                    st.caption(f"🔍 Causa raíz (revisa también Manage app → Logs): {causa_raiz}")

# ---------------------------------------------------------
# PESTAÑA: PEDIR RESEÑAS (WhatsApp + QR)
# ---------------------------------------------------------
with tab_pedir_resenas:
    st.subheader("📣 Consigue más reseñas de las que ya tienes")
    st.caption("Genera un mensaje de WhatsApp y un código QR para que el propio negocio pida reseñas a sus clientes satisfechos.")

    locales_disponibles_pr = st.session_state.locales_agencia
    if not locales_disponibles_pr:
        st.info("Esta agencia todavía no tiene locales.")
    else:
        nombre_local_pr = st.selectbox(
            "Local:", options=[l["nombre"] for l in locales_disponibles_pr], key="selector_local_pedir_resenas"
        )
        local_pr = next(l for l in locales_disponibles_pr if l["nombre"] == nombre_local_pr)

        enlace_actual = local_pr.get("enlace_resena_google") or ""
        nuevo_enlace = st.text_input(
            "Enlace directo de Google para dejar una reseña:",
            value=enlace_actual,
            placeholder="https://g.page/r/xxxxxxxxxx/review",
            help="Lo encuentras en Google Business Profile → Solicitar reseñas → Copiar enlace."
        )

        if st.button("💾 Guardar enlace"):
            try:
                supabase.table("locales").update({"enlace_resena_google": nuevo_enlace.strip()}).eq("id", local_pr["id"]).execute()
                local_pr["enlace_resena_google"] = nuevo_enlace.strip()
                st.success("Enlace guardado.")
            except Exception as e:
                st.error(redactar_secretos(f"No se pudo guardar: {e}"))

        if not nuevo_enlace.strip():
            st.warning("Guarda primero el enlace de reseña de Google para generar el mensaje y el QR.")
        else:
            col_wa, col_qr = st.columns(2)
            with col_wa:
                st.markdown("**Mensaje listo para WhatsApp:**")
                enlace_wa = generar_mensaje_whatsapp(nombre_local_pr, nuevo_enlace.strip())
                st.markdown(f'<a href="{enlace_wa}" target="_blank"><button style="background-color:#25D366;color:white;padding:10px 20px;border:none;border-radius:8px;font-weight:bold;cursor:pointer;width:100%;">📲 Abrir en WhatsApp</button></a>', unsafe_allow_html=True)
                st.caption("Se abre con el mensaje ya escrito; solo hay que elegir el contacto.")
            with col_qr:
                st.markdown("**Código QR para imprimir en el local:**")
                png_qr = generar_qr_png(nuevo_enlace.strip())
                st.image(png_qr, width=180)
                st.download_button("⬇️ Descargar QR (PNG)", data=png_qr, file_name=f"qr_resenas_{nombre_local_pr}.png", mime="image/png")

# ---------------------------------------------------------
# PESTAÑA: CONTENIDO SEO EXTRA
# ---------------------------------------------------------
with tab_seo_extra:
    st.subheader("📝 Contenido SEO adicional para el local")
    st.caption("Aprovecha las mismas palabras clave del local para generar contenido más allá de las respuestas a reseñas.")

    locales_disponibles_seo = st.session_state.locales_agencia
    if not locales_disponibles_seo:
        st.info("Esta agencia todavía no tiene locales.")
    else:
        nombre_local_seo = st.selectbox(
            "Local:", options=[l["nombre"] for l in locales_disponibles_seo], key="selector_local_seo_extra"
        )
        local_seo = next(l for l in locales_disponibles_seo if l["nombre"] == nombre_local_seo)

        tipo_contenido = st.radio(
            "Tipo de contenido:",
            ["Publicación de Google Business", "Descripción para redes sociales", "Meta descripción SEO"],
            horizontal=True
        )

        if st.button("✨ Generar contenido", key="generar_seo_extra"):
            with st.spinner("Redactando el contenido..."):
                try:
                    texto_generado = generar_contenido_seo_extra(
                        client, local_seo["nombre"], local_seo["nicho"], local_seo["seo_keywords"], tipo_contenido
                    )
                    st.code(texto_generado, language=None, wrap_lines=True)
                    if tipo_contenido == "Meta descripción SEO":
                        st.caption(f"Longitud: {len(texto_generado)} caracteres (recomendado: máx. 155).")

                    registrar_contenido_seo_generado(
                        agencia_id=agencia["id"],
                        local_id=local_seo["id"],
                        usuario_id=usuario["id"],
                        tipo_contenido=tipo_contenido
                    )
                except Exception as e:
                    causa_raiz = log_error_completo("generar contenido SEO extra", e)
                    st.error(redactar_secretos(f"Error al generar el contenido: {e}"))
                    st.caption(f"🔍 Causa raíz (revisa también Manage app → Logs): {causa_raiz}")

# ---------------------------------------------------------
# PESTAÑA 2: ANALÍTICA DE LA AGENCIA
# ---------------------------------------------------------
with tab_analitica:
    st.subheader("📊 Actividad de tu agencia")

    rango = st.radio("Periodo:", ["Últimos 7 días", "Últimos 30 días", "Todo el histórico"], horizontal=True)
    fecha_hasta_dt = datetime.utcnow()
    if rango == "Últimos 7 días":
        fecha_desde_dt = fecha_hasta_dt - timedelta(days=7)
    elif rango == "Últimos 30 días":
        fecha_desde_dt = fecha_hasta_dt - timedelta(days=30)
    else:
        fecha_desde_dt = datetime(1970, 1, 1)
    fecha_desde = fecha_desde_dt.isoformat()

    if rango == "Todo el histórico":
        periodo_texto = f"Todo el histórico (hasta el {fecha_hasta_dt.strftime('%d/%m/%Y')})"
    else:
        periodo_texto = f"{rango} · {fecha_desde_dt.strftime('%d/%m/%Y')} – {fecha_hasta_dt.strftime('%d/%m/%Y')}"

    try:
        historico = supabase.table("historico_respuestas") \
            .select("*") \
            .eq("agencia_id", agencia["id"]) \
            .gte("creado_en", fecha_desde) \
            .execute().data

        if not historico:
            st.info("Todavía no hay respuestas generadas en este periodo.")
        else:
            total_respuestas = len(historico)
            positivas = sum(1 for r in historico if r["sentimiento"] == "positivo")
            negativas = total_respuestas - positivas

            id_a_nombre_local = {l["id"]: l["nombre"] for l in st.session_state.locales_agencia}

            # Periodo anterior equivalente (para tendencia del score y comparación del informe).
            # No aplica a "Todo el histórico": no hay un "periodo anterior" con sentido.
            historico_anterior = []
            dias_periodo = 30
            if rango != "Todo el histórico":
                dias_periodo = max(1, (fecha_hasta_dt - fecha_desde_dt).days)
                try:
                    duracion = fecha_hasta_dt - fecha_desde_dt
                    fecha_desde_anterior = fecha_desde_dt - duracion
                    historico_anterior = supabase.table("historico_respuestas") \
                        .select("*") \
                        .eq("agencia_id", agencia["id"]) \
                        .gte("creado_en", fecha_desde_anterior.isoformat()) \
                        .lt("creado_en", fecha_desde_dt.isoformat()) \
                        .execute().data
                except Exception:
                    historico_anterior = []
            else:
                # Para "todo el histórico" estimamos los días transcurridos desde la
                # primera respuesta, para que volumen/constancia tengan sentido.
                fechas = [str(r.get("creado_en", ""))[:10] for r in historico if r.get("creado_en")]
                if fechas:
                    try:
                        primera = min(datetime.fromisoformat(f) for f in fechas if f)
                        dias_periodo = max(1, (fecha_hasta_dt - primera).days)
                    except Exception:
                        dias_periodo = 90

            # ---------- 🏆 REVIEWPRO REPUTATION SCORE (protagonista) ----------
            st.markdown("### 🏆 Reputation Score")
            opciones_score = ["Toda la agencia"] + [l["nombre"] for l in st.session_state.locales_agencia]
            local_score_sel = st.selectbox("Calcular para:", opciones_score, key="selector_score")

            if local_score_sel == "Toda la agencia":
                hist_score = historico
                hist_score_ant = historico_anterior
                nombre_ctx = "tu agencia"
            else:
                local_id_sel = next((l["id"] for l in st.session_state.locales_agencia if l["nombre"] == local_score_sel), None)
                hist_score = [r for r in historico if r.get("local_id") == local_id_sel]
                hist_score_ant = [r for r in historico_anterior if r.get("local_id") == local_id_sel]
                nombre_ctx = local_score_sel

            resultado_score = calcular_reputation_score(hist_score, hist_score_ant, dias_periodo)
            interpretacion = generar_interpretacion_score_ia(client, resultado_score, nombre_ctx)
            mostrar_medidor_score(resultado_score, f"Puntuación de {nombre_ctx} · {rango}", interpretacion)

            # ---------- 💶 CALCULADORA DE ROI (score → ingresos) ----------
            with st.expander("💶 Calculadora de retorno: ¿cuánto vale subir de estrellas?", expanded=False):
                st.caption(
                    "Estima cuánto más podría facturar este negocio si mejora su valoración media. "
                    "Basado en el estudio de Harvard: cada estrella de más sube los ingresos un 5-9% en negocios independientes."
                )
                colr1, colr2, colr3 = st.columns(3)
                facturacion = colr1.number_input("Facturación al mes (€)", min_value=0, value=20000, step=1000, key="roi_facturacion")
                estrellas_act = colr2.number_input("Valoración actual (★)", min_value=0.0, max_value=5.0, value=3.8, step=0.1, key="roi_estrellas_act")
                estrellas_obj = colr3.number_input("Valoración objetivo (★)", min_value=0.0, max_value=5.0, value=4.3, step=0.1, key="roi_estrellas_obj")
                roi = calcular_roi_estrellas(facturacion, estrellas_act, estrellas_obj)
                mostrar_calculadora_roi(roi, estrellas_act, estrellas_obj)

            st.divider()
            st.markdown("**Actividad del periodo**")
            col1, col2, col3 = st.columns(3)
            col1.metric("Respuestas generadas", total_respuestas)
            col2.metric("Reseñas positivas", positivas, f"{round(positivas/total_respuestas*100)}%")
            col3.metric("Reseñas negativas", negativas, f"{round(negativas/total_respuestas*100)}%")

            # Actividad por local
            conteo_por_local = {}
            for fila in historico:
                nombre_local = id_a_nombre_local.get(fila["local_id"], "Local desconocido")
                conteo_por_local[nombre_local] = conteo_por_local.get(nombre_local, 0) + 1

            st.markdown("**Actividad por local:**")
            mostrar_barras_simples(conteo_por_local)

            # Actividad por usuario (visibilidad multi-usuario)
            usuarios_de_la_agencia = supabase.table("usuarios").select("id, nombre_usuario").eq("agencia_id", agencia["id"]).execute().data
            id_a_nombre_usuario = {u["id"]: u["nombre_usuario"] for u in usuarios_de_la_agencia}

            conteo_por_usuario = {}
            for fila in historico:
                nombre_usuario_fila = id_a_nombre_usuario.get(fila["usuario_id"], "Usuario eliminado")
                conteo_por_usuario[nombre_usuario_fila] = conteo_por_usuario.get(nombre_usuario_fila, 0) + 1

            st.markdown("**Reparto de trabajo por usuario del equipo:**")
            st.caption("Útil para ver qué gestores de tu agencia están usando más la herramienta.")
            mostrar_barras_simples(conteo_por_usuario, color="#8BD1F7")

            # Contenido SEO generado en el periodo (tabla nueva — si aún no se ha
            # ejecutado la migración, esto falla en silencio y el informe lo indica)
            try:
                contenido_seo_periodo = supabase.table("historico_contenido_seo") \
                    .select("*") \
                    .eq("agencia_id", agencia["id"]) \
                    .gte("creado_en", fecha_desde) \
                    .execute().data
            except Exception:
                contenido_seo_periodo = []

            st.divider()
            st.markdown("**📄 Informe de marca blanca para reenviar a tus clientes:**")
            try:
                # El informe usa siempre el score de toda la agencia (no el del
                # local seleccionado arriba en pantalla).
                score_agencia = calcular_reputation_score(historico, historico_anterior, dias_periodo)

                # Si el usuario ha usado la calculadora de ROI arriba, incluimos ese
                # cálculo en el informe. Los valores viven en session_state por sus keys.
                roi_informe = None
                fact_roi = st.session_state.get("roi_facturacion", 0)
                est_act_roi = st.session_state.get("roi_estrellas_act", 0.0)
                est_obj_roi = st.session_state.get("roi_estrellas_obj", 0.0)
                if fact_roi and est_obj_roi > est_act_roi:
                    roi_informe = calcular_roi_estrellas(fact_roi, est_act_roi, est_obj_roi)

                pdf_bytes = generar_informe_pdf_mensual(
                    agencia, historico, historico_anterior, st.session_state.locales_agencia,
                    id_a_nombre_usuario, contenido_seo_periodo, periodo_texto,
                    cliente_ia=client, resultado_score=score_agencia, dias_periodo=dias_periodo,
                    roi=roi_informe, roi_estrellas_actuales=est_act_roi, roi_estrellas_objetivo=est_obj_roi
                )
                st.download_button(
                    "⬇️ Descargar informe PDF",
                    data=pdf_bytes,
                    file_name=f"informe_{agencia['nombre_agencia'].replace(' ', '_')}_{fecha_hasta_dt.strftime('%Y%m%d')}.pdf",
                    mime="application/pdf"
                )
            except Exception as e:
                causa_raiz = log_error_completo("generar informe PDF", e)
                st.error(redactar_secretos(f"No se pudo generar el informe: {e}"))
                st.caption(f"🔍 Causa raíz (revisa también Manage app → Logs): {causa_raiz}")

    except Exception as e:
        st.error(redactar_secretos(f"No se pudo cargar la analítica: {e}"))

# =========================================================
# 🗑️ ZONA DE BAJA — solo visible para el usuario admin de la agencia
# =========================================================
if usuario.get("rol") == "admin":
    with st.expander("🗑️ Darme de baja / Eliminar todos los datos de mi agencia"):
        st.warning(
            "Esta acción borra permanentemente tu agencia, todos sus usuarios, "
            "todos sus locales y todo el histórico de respuestas generadas. "
            "No se puede deshacer."
        )
        confirmacion = st.text_input(
            f"Escribe exactamente el nombre de tu agencia (\"{agencia['nombre_agencia']}\") para confirmar:",
            key="confirmacion_borrado_agencia"
        )
        if st.button("Eliminar definitivamente mi agencia", type="primary"):
            if confirmacion.strip() != agencia["nombre_agencia"]:
                st.error("El texto no coincide con el nombre de tu agencia. No se ha borrado nada.")
            else:
                try:
                    supabase.table("agencias").delete().eq("id", agencia["id"]).execute()
                    for key in ["sesion_activa", "usuario_actual", "agencia_actual", "locales_agencia", "local_activo"]:
                        st.session_state[key] = False if key == "sesion_activa" else None if "actual" in key else []
                    st.session_state.vista_landing = "info"
                    st.success("Tu agencia y todos sus datos han sido eliminados.")
                    st.rerun()
                except Exception as e:
                    st.error(redactar_secretos(f"No se pudo completar el borrado: {e}"))

st.divider()
st.markdown(f"""
<div style="font-size: 10px; color: #6c757d; text-align: justify; line-height: 1.4;">
    <strong>Aviso Legal y Condiciones de Uso Enterprise (Marca Blanca):</strong> Esta plataforma es una
    herramienta tecnológica de asistencia basada en modelos de Inteligencia Artificial generativa, licenciada
    bajo un contrato B2B a <strong>{agencia['nombre_agencia']}</strong>. El software no presta asesoramiento
    legal, jurídico, ni de relaciones públicas vinculante. La agencia operadora es la única responsable de
    revisar, verificar y autorizar cualquier contenido generado antes de su publicación. Queda expresamente
    prohibida la ingeniería inversa, descompilación o extracción de la lógica de negocio de esta plataforma.
</div>
""", unsafe_allow_html=True)
