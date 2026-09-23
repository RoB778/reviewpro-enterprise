import base64
import html as _html
import json
import secrets as _secrets_modulo
import os
import re
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from io import BytesIO

import bcrypt
import stripe
import streamlit as st
from anthropic import Anthropic

# Blindaje reforzado en cuatro capas (saneado anti-inyección, filtro
# determinista, auditor independiente y reescritura correctiva).
# Vive en blindaje.py, en la misma carpeta que este archivo.
from blindaje import (
    generar_respuesta,
    analizar_riesgo,
    MODO_RAPIDO,
    MODO_BLINDADO,
)
import motor_seo
from motor_seo import (
    construir_cuestionario,
    sugerir_preguntas_extra,
    generar_contenido_seo,
    proponer_hechos_desde_web,
    leer_ficha,
    SI, NO, NO_CONSTA,
)
import motor_agente
from ui import (
    CSS_GLOBAL,
    ETAPAS_RAPIDA,
    ETAPAS_BLINDADA,
    html_etapas,
    html_sello,
    html_aviso_riesgo,
    html_eyebrow,
)

# -----------------------------------------------------------------------
# PUENTE DE SECRETOS: st.secrets <-> variables de entorno
# -----------------------------------------------------------------------
# En Streamlit Community Cloud los secretos viven en un archivo secrets.toml
# y se leen con st.secrets["CLAVE"]. Pero al desplegar en Render (u otro
# hosting con Docker) NO existe ese archivo: los secretos se inyectan como
# variables de entorno. Sin este puente, cualquier st.secrets["X"] revienta
# con StreamlitSecretNotFoundError porque no hay secrets.toml.
#
# Este envoltorio intercepta los accesos a st.secrets y, si la clave no está
# en el archivo (caso Render), la busca en os.environ. Así el MISMO código
# funciona en los dos sitios sin tocar ninguna de las líneas st.secrets[...]
# repartidas por el archivo.
# -----------------------------------------------------------------------
class _SecretsConEntorno:
    """Se comporta como st.secrets, pero con fallback a variables de entorno."""

    def _leer_archivo(self, clave):
        # Intenta leer del secrets.toml real. Si no existe archivo o falta la
        # clave, Streamlit lanza una excepción que aquí tratamos como "no está".
        try:
            return st._secrets_originales[clave]
        except Exception:
            return None

    def __getitem__(self, clave):
        valor = self._leer_archivo(clave)
        if valor is not None:
            return valor
        valor = os.environ.get(clave)
        if valor is not None:
            return valor
        raise KeyError(
            f"No se encontró el secreto '{clave}' ni en secrets.toml ni en las "
            f"variables de entorno. En Render, añádela en Settings -> Environment."
        )

    def get(self, clave, por_defecto=None):
        try:
            return self[clave]
        except KeyError:
            return por_defecto

    def __contains__(self, clave):
        return self.get(clave) is not None


# Guardamos el st.secrets original y lo sustituimos por nuestro puente.
if not hasattr(st, "_secrets_originales"):
    st._secrets_originales = st.secrets
    st.secrets = _SecretsConEntorno()
# Solo lo estrictamente ligero se importa arriba: 'colors' y 'cm' se usan
# como VALOR POR DEFECTO en las firmas de grafico_barras_pos_neg más abajo,
# y los valores por defecto se calculan al cargar el módulo, no al llamar a
# la función — así que estos dos sí tienen que estar disponibles desde ya.
# reportlab y qrcode se cargan DIFERIDOS, dentro de las funciones que los usan
# (informe_pdf.py para el informe, generar_qr_png para los códigos). Antes se
# cargaban al arrancar el proceso aunque nadie hubiera pedido nunca un informe
# ni un QR. En un servicio con 512 MB de límite eso es peso muerto pagado por
# adelantado.
#
# Desde que la maquetación del PDF vive en informe_pdf.py, app.py ya no
# necesita importar NADA de reportlab a nivel de módulo: ni siquiera 'colors'
# ni 'cm', que eran los dos únicos que quedaban arriba.
from supabase import create_client

# Configuración de las claves secretas de los servidores
# .strip() es crítico: un salto de línea o espacio colado en Secrets de
# Streamlit Cloud (p.ej. pegando la key con """triple comillas""" en el
# secrets.toml) provoca httpx.LocalProtocolError ("Illegal header value"),
# que el SDK de Anthropic enmascara como APIConnectionError.
_anthropic_api_key_raw = st.secrets["ANTHROPIC_API_KEY"]
_anthropic_api_key = _anthropic_api_key_raw.strip() if isinstance(_anthropic_api_key_raw, str) else _anthropic_api_key_raw


# -----------------------------------------------------------------------
# OPTIMIZACIÓN: clientes cacheados con @st.cache_resource.
# Streamlit re-ejecuta TODO este archivo en cada interacción (cada click,
# cada cambio de campo). Sin caché, en cada rerun se creaban de nuevo el
# cliente de Anthropic y el de Supabase, abriendo conexiones HTTP nuevas
# cada vez -> tiempo desperdiciado que se nota como lentitud.
# Con @st.cache_resource, cada cliente se crea UNA sola vez y se reutiliza
# en todos los reruns de todas las sesiones. No cambia el comportamiento:
# 'client' y 'supabase' se siguen usando exactamente igual que antes.
# -----------------------------------------------------------------------
@st.cache_resource
def _crear_cliente_anthropic(api_key):
    return Anthropic(
        api_key=api_key,
        max_retries=3,   # reintenta automáticamente ante fallos de red transitorios
        timeout=60.0,    # más margen que el default, por si la conexión tarda en establecerse
    )


@st.cache_resource
def _crear_cliente_supabase(url, key):
    return create_client(url, key)


client = _crear_cliente_anthropic(_anthropic_api_key)
supabase = _crear_cliente_supabase(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
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
# APP_URL = "https://app.reselia.es"  (sin barra final)
if "APP_URL" not in st.secrets:
    st.error(
        "Falta configurar APP_URL en los secrets de la app. Sin esto, Stripe no puede "
        "devolver al usuario tras el pago. Ve a 'Manage app' → Settings → Secrets y añade "
        "APP_URL = \"https://tu-url-real.streamlit.app\" (la URL exacta con la que accedes a tu app)."
    )
    st.stop()
APP_URL = st.secrets["APP_URL"].strip().rstrip("/")
# Validación defensiva: si APP_URL no empieza por http:// o https:// (por ejemplo,
# porque al copiar/pegar en Render se coló un espacio invisible al principio, o
# porque directamente se olvidó el "https://"), Stripe rechaza la URL con un error
# críptico ("Invalid URL: An explicit scheme must be provided") que no dice dónde
# está el problema real. Lo detectamos aquí, antes de que llegue a Stripe, con un
# aviso que sí dice exactamente qué está mal.
if not (APP_URL.startswith("http://") or APP_URL.startswith("https://")):
    st.error(
        f"APP_URL está mal configurada: el valor actual es \"{APP_URL}\", y tiene que "
        f"empezar por \"https://\". Revisa en Render → Settings → Environment que la "
        f"variable APP_URL no tenga espacios ni caracteres invisibles al principio, y "
        f"que sea exactamente tu URL completa, por ejemplo: "
        f"https://app.reselia.es"
    )
    st.stop()

# 1. Configuración de página limpia y profesional
st.set_page_config(page_title="Reselia · Reputación con criterio", page_icon="▪", layout="wide", initial_sidebar_state="expanded")

# =========================================================
# NOINDEX PARA app.reselia.es
# =========================================================
# El subdominio de la aplicación no debe aparecer en Google. Detrás de él solo
# hay una pantalla de acceso: si se indexara, competiría con reselia.es por las
# búsquedas de marca y llevaría a un login a gente que todavía no es cliente.
# El SEO tiene que concentrarse en la landing, que es la que vende.
#
# Streamlit no permite escribir en <head>, pero Google respeta la directiva
# robots dentro del cuerpo del documento en la práctica. Combinado con el
# Disallow del robots.txt de la landing, es suficiente para este caso.
st.markdown(
    '<meta name="robots" content="noindex, nofollow">',
    unsafe_allow_html=True,
)

# Componentes visuales nuevos de la v2 (selector de vía, sello de auditoría,
# etapas de progreso). El sistema de diseño base — botones, pestañas,
# inputs, tipografía — ya vivía en el bloque de estilos de más abajo
# ("TINTA & PAPEL"); este archivo es aditivo, no lo sustituye.
st.markdown(CSS_GLOBAL, unsafe_allow_html=True)

st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=IBM+Plex+Mono:wght@400;500&display=swap');

    /* =========================================================
       RESELIA — SISTEMA DE DISEÑO "TINTA & PAPEL"
       Papel hueso, azul tinta profundo, acento ámbar de sello.
       Misma familia visual que la landing: al pasar de la web
       a la herramienta no debe haber salto estético.
       ========================================================= */
    /* =========================================================
       RESELIA — SISTEMA DE DISEÑO "AURORA & CRISTAL" (v3)
       Evolución de "Tinta & Papel": mismo ADN de marca (tinta
       navy + ámbar) pero sobre un lienzo de aurora suave y con
       superficies de cristal esmerilado (glassmorphism).

       PRINCIPIO INNEGOCIABLE — legibilidad primero:
       El color y el blur viven en el FONDO y en los BORDES. El
       texto siempre se apoya en cristal casi opaco (alpha alto),
       nunca directamente sobre el degradado. Así el fondo aporta
       vida y profundidad sin castigar el contraste de lectura.
       ========================================================= */
    :root {
        /* Superficies de cristal: blancas translúcidas, alpha ALTO para que
           el texto encima mantenga contraste AA. El blur las convierte en
           vidrio esmerilado; el fondo aurora se filtra apenas teñido. */
        --er-canvas:     #eef1f8;   /* base fría bajo la aurora (fallback) */
        --er-surface:    rgba(255,255,255,.72);   /* tarjetas de cristal */
        --er-surface-2:  rgba(255,255,255,.55);   /* cristal más ligero */
        --er-sunken:     rgba(255,255,255,.45);   /* inputs / hundidos */
        --er-line:       rgba(26,34,56,.10);       /* hairline por defecto */
        --er-line-2:     rgba(26,34,56,.18);       /* hairline con énfasis */
        --er-glass-edge: rgba(255,255,255,.65);    /* brillo de borde superior */
        --er-shadow:     0 8px 30px rgba(26,34,56,.10), 0 1px 2px rgba(26,34,56,.05);
        --er-shadow-lg:  0 16px 50px rgba(26,34,56,.14);

        /* Tinta y textos: SIN cambios. La legibilidad se conserva íntegra;
           el navy sobre cristal blanco es contraste de sobra. */
        --er-ink:        #1a2238;   /* azul tinta profundo, texto primario */
        --er-body:       #232c47;   /* texto de cuerpo */
        --er-muted:      #5a6474;   /* texto secundario / captions */
        --er-faint:      #8b93a3;   /* placeholders / metadatos */

        /* Acentos: más vivos que en v2. El acento principal pasa de tinta
           plana a un índigo-violeta vibrante (para botones y estado activo),
           que sobre el lienzo frío invita más a trabajar. La tinta navy
           sigue disponible para estructura. */
        --er-accent:     #4f46e5;   /* índigo vibrante — acción principal */
        --er-accent-2:   #4338ca;   /* índigo hover, más profundo */
        --er-accent-bg:  rgba(79,70,229,.10);      /* halo índigo pálido */
        --er-accent-ink: #1a2238;   /* tinta navy para estructura/títulos */

        --er-amber:      #d98a1f;   /* ámbar de sello — acento cálido */
        --er-amber-2:    #f0a942;   /* ámbar claro */
        --er-danger:     #d64534;   /* rojo tachado / errores */
        --er-danger-bg:  rgba(214,69,52,.09);
        --er-ok:         #2f9e6f;   /* verde aprobado, algo más vivo */
        --er-ok-bg:      rgba(47,158,111,.10);
    }

    /* Ocultar cromo de Streamlit
       -----------------------------------------------------------------
       CUIDADO al tocar esto: en móvil, el botón que despliega la barra
       lateral vive DENTRO del <header>. Ocultar el header entero deja la
       app sin ninguna forma de abrir el menú en el teléfono: la barra sale
       colapsada y no hay control visible para desplegarla, así que el
       usuario se queda encerrado en la sección en la que aterrizó, sin
       poder cambiar de local ni de sección.

       Por eso el header se hace transparente y sin altura en vez de
       display:none, y el control de la barra lateral se rescata de forma
       explícita más abajo. */
    #MainMenu, footer, .stAppDeployButton, .viewerBadge_container__1QS1h {
        display: none !important;
        visibility: hidden !important;
    }
    /* stToolbar NUNCA se oculta: es el contenedor del botón que reabre la
       sidebar (stExpandSidebarButton vive dentro de él). Ocultarlo entero
       se lleva por delante ese botón aunque su propio CSS diga visible —
       un display:none en el padre gana siempre sobre el hijo. Esto es lo
       que causaba que la barra lateral no se pudiera reabrir. Se ocultan
       solo sus dos hijos concretos que no queremos ver. */
    div[data-testid="stDecoration"],
    div[data-testid="stMainMenu"] {
        display: none !important;
        height: 0px !important;
    }
    /* El header se hace transparente pero mantiene su altura natural para que
       el botón de la sidebar (que en versiones recientes de Streamlit vive
       DENTRO del header) siga siendo accesible. Con height:0px el botón
       desaparece del DOM aunque exista — era la causa de que no hubiera forma
       de abrir la barra lateral. */
    header, div[data-testid="stHeader"] {
        background: transparent !important;
        box-shadow: none !important;
        border: none !important;
    }

    /* Ocultar solo los elementos de cromo que no queremos ver, sin tocar
       el header completo ni stToolbar (contiene el botón de reabrir la
       sidebar — ver comentario detallado más arriba). */
    div[data-testid="stDecoration"],
    div[data-testid="stMainMenu"],
    #MainMenu,
    footer,
    .stAppDeployButton {
        display: none !important;
        visibility: hidden !important;
    }

    /* Compensar el espacio que ocupa el header ahora que tiene altura. */
    section[data-testid="stMain"] .block-container {
        padding-top: 1rem !important;
    }

    /* Lienzo global — AURORA
       Varios degradados radiales muy suaves y desaturados (peach, lila,
       menta, azul) sobre una base fría. Fijo (background-attachment implícito
       por el fixed del pseudo-fondo). El texto NUNCA se apoya aquí: siempre
       sobre cristal. Por eso podemos permitirnos color en el fondo sin dañar
       la lectura. Fallback: si no hay soporte, queda --er-canvas plano. */
    .stApp {
        background-color: #f0f2f7 !important;
        background-image: none !important;
        background-attachment: unset !important;
        color: var(--er-body);
    }
    html, body, [class*="css"], .stApp, [data-testid="stMarkdownContainer"] {
        font-family: 'Inter', -apple-system, 'Helvetica Neue', Arial, sans-serif !important;
        letter-spacing: -0.006em;
    }
    .block-container {
        padding-top: 3rem !important;
        padding-bottom: 4rem !important;
        max-width: 940px !important;
    }

    /* Titulares — pesos ligeros, sentence case, tinta */
    h1, h2, h3, h4 {
        font-family: 'Inter', sans-serif !important;
        color: var(--er-ink) !important;
        font-weight: 500 !important;
        letter-spacing: -0.02em !important;
    }
    h1 { font-size: 2rem !important; line-height: 1.15 !important; }
    h2 { font-size: 1.4rem !important; }
    h3 { font-size: 1.1rem !important; }
    p, span, label, li, div[data-testid="stMarkdownContainer"] p {
        color: var(--er-body);
        line-height: 1.65;
    }
    .stCaption, [data-testid="stCaptionContainer"], small,
    [data-testid="stCaptionContainer"] p {
        color: var(--er-muted) !important;
        letter-spacing: 0 !important;
    }
    strong, b { color: var(--er-ink); font-weight: 600; }

    /* ---------------------------------------------------------
       BOTONES — robustos: fijamos color también en el texto interno
       (Streamlit envuelve el label en <p>/<div>, que heredaba mal
       el color y salía invisible). Aquí no queda margen a ambigüedad.
       --------------------------------------------------------- */
    .stButton > button, .stDownloadButton > button,
    .stButton > button *, .stDownloadButton > button * {
        color: var(--er-ink) !important;
    }
    .stButton > button, .stDownloadButton > button {
        background: var(--er-surface) !important;
        border: 1px solid var(--er-line-2) !important;
        border-radius: 6px !important;
        font-weight: 500 !important;
        font-size: 0.9rem !important;
        letter-spacing: -0.006em !important;
        padding: 0.55rem 1.15rem !important;
        transition: all 0.14s ease !important;
        box-shadow: 0 1px 1px rgba(22,21,26,0.03) !important;
    }
    .stButton > button:hover, .stDownloadButton > button:hover {
        background: var(--er-sunken) !important;
        border-color: var(--er-line-2) !important;
    }
    .stButton > button:hover *, .stDownloadButton > button:hover * {
        color: var(--er-ink) !important;
    }
    /* Botón primario — índigo sólido, texto blanco forzado en el interior */
    .stButton > button[kind="primary"] {
        background: var(--er-accent) !important;
        border: 1px solid var(--er-accent) !important;
        box-shadow: 0 1px 2px rgba(59,58,107,0.18) !important;
    }
    .stButton > button[kind="primary"], .stButton > button[kind="primary"] * {
        color: #FFFFFF !important;
    }
    .stButton > button[kind="primary"]:hover {
        background: var(--er-accent-2) !important;
        border-color: var(--er-accent-2) !important;
    }
    .stButton > button[kind="primary"]:hover * { color: #FFFFFF !important; }

    /* Botón primario DENTRO DE UN FORMULARIO (st.form_submit_button).
       Streamlit lo renderiza bajo stFormSubmitButton, NO bajo stButton, así que
       las reglas de arriba no le llegaban y el texto salía oscuro sobre el fondo
       índigo (ilegible). Se cubren aquí todos los data-testid que ha usado
       Streamlit entre versiones, y se fuerza también -webkit-text-fill-color
       porque en algunos navegadores el color solo no basta. */
    .stFormSubmitButton > button[kind="primary"],
    .stFormSubmitButton > button[kind="primary"] *,
    div[data-testid="stFormSubmitButton"] button[kind="primary"],
    div[data-testid="stFormSubmitButton"] button[kind="primary"] *,
    button[data-testid="stBaseButton-primaryFormSubmit"],
    button[data-testid="stBaseButton-primaryFormSubmit"] * {
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
    }
    .stFormSubmitButton > button[kind="primary"],
    div[data-testid="stFormSubmitButton"] button[kind="primary"],
    button[data-testid="stBaseButton-primaryFormSubmit"] {
        background: var(--er-accent) !important;
        border: 1px solid var(--er-accent) !important;
        box-shadow: 0 1px 2px rgba(26,34,56,0.18) !important;
    }
    .stFormSubmitButton > button[kind="primary"]:hover,
    div[data-testid="stFormSubmitButton"] button[kind="primary"]:hover,
    button[data-testid="stBaseButton-primaryFormSubmit"]:hover {
        background: var(--er-accent-2) !important;
        border-color: var(--er-accent-2) !important;
    }
    /* Cinturón y tirantes: cualquier botón primario, esté donde esté, en blanco. */
    button[kind="primary"], button[kind="primary"] * {
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
    }

    /* Inputs — superficie hundida, borde hairline, foco índigo
       -----------------------------------------------------------------
       IMPORTANTE: el borde y el fondo van en el CONTENEDOR
       div[data-baseweb="input"], NO en el <input>.

       Motivo: en un campo de contraseña, Streamlit mete el botón del ojo
       como HERMANO del input, dentro de ese mismo contenedor:

           div[data-baseweb="input"]
             ├─ input
             └─ button   ← el ojo

       Al pintar fondo y borde sobre el <input>, el input quedaba como una
       caja opaca propia que tapaba al botón y lo dejaba fuera del recuadro
       visible, así que el ojo no se veía ni se podía pulsar. Poniendo la
       caja en el contenedor y dejando el input transparente, el ojo vuelve
       a quedar dentro y funciona. */
    .stTextInput div[data-baseweb="input"],
    .stNumberInput div[data-baseweb="input"],
    .stTextArea div[data-baseweb="textarea"],
    .stSelectbox > div > div,
    .stMultiSelect > div > div {
        background: var(--er-surface) !important;
        border: 1px solid var(--er-line-2) !important;
        border-radius: 6px !important;
    }
    .stTextInput div[data-baseweb="input"] > input,
    .stNumberInput div[data-baseweb="input"] > input,
    .stTextArea div[data-baseweb="textarea"] > textarea {
        background: transparent !important;
        color: var(--er-ink) !important;
        border: none !important;
        box-shadow: none !important;
    }
    .stTextInput input::placeholder,
    .stTextArea textarea::placeholder { color: var(--er-faint) !important; }

    /* El foco se pinta en el contenedor, por el mismo motivo que el borde. */
    .stTextInput div[data-baseweb="input"]:focus-within,
    .stNumberInput div[data-baseweb="input"]:focus-within,
    .stTextArea div[data-baseweb="textarea"]:focus-within {
        border-color: var(--er-accent) !important;
        box-shadow: 0 0 0 3px var(--er-accent-bg) !important;
    }

    /* El botón del ojo: visible, pulsable y con el color del texto normal.
       Sin esto hereda un color que en algunos temas queda casi invisible
       sobre el fondo claro. */
    .stTextInput div[data-baseweb="input"] button {
        background: transparent !important;
        border: none !important;
        color: var(--er-muted) !important;
        opacity: 1 !important;
        visibility: visible !important;
        cursor: pointer !important;
    }
    .stTextInput div[data-baseweb="input"] button:hover {
        color: var(--er-ink) !important;
    }
    .stTextInput div[data-baseweb="input"] button svg {
        fill: currentColor !important;
    }

    .stTextInput label, .stNumberInput label, .stTextArea label,
    .stSelectbox label, .stRadio label, .stMultiSelect label,
    .stSlider label, .stCheckbox label {
        color: var(--er-body) !important;
        font-weight: 500 !important;
        font-size: 0.85rem !important;
        letter-spacing: -0.006em !important;
    }

    /* -----------------------------------------------------------------
       REFUERZO DE COLOR EN EL SELECTBOX (ej. "Selecciona el local")
       -----------------------------------------------------------------
       El selector ".stSelectbox > div > div" de arriba no siempre llega
       al texto real: Streamlit construye el desplegable con un
       componente interno (BaseWeb) que anida el valor mostrado varios
       niveles más adentro, en un <div data-baseweb="select"> con un
       <span> propio. Sin apuntar directamente a esa capa, el texto se
       queda con el color por defecto (blanco) y desaparece sobre el
       fondo claro. Aquí forzamos negro en todas las capas posibles:
       el valor ya seleccionado (dentro del campo cerrado) y cada
       opción de la lista que se despliega al hacer click.
       ----------------------------------------------------------------- */
    .stSelectbox div[data-baseweb="select"] * ,
    .stSelectbox div[data-baseweb="select"] span,
    .stSelectbox div[data-baseweb="select"] div {
        color: var(--er-ink) !important;
    }
    /* La lista de opciones se pinta en un popover aparte (fuera del
       propio selectbox en el árbol del DOM), por eso necesita su
       propia regla en vez de heredar la de arriba. */
    ul[data-baseweb="menu"] li,
    ul[data-baseweb="menu"] li * ,
    div[data-baseweb="popover"] li,
    div[data-baseweb="popover"] li * {
        color: var(--er-ink) !important;
        background: var(--er-surface) !important;
    }
    ul[data-baseweb="menu"] li:hover,
    div[data-baseweb="popover"] li:hover {
        background: var(--er-accent-bg) !important;
    }

    /* Tabs — subrayado índigo fino, sentence case */
    .stTabs [data-baseweb="tab-list"] {
        gap: 2px;
        border-bottom: 1px solid var(--er-line) !important;
    }
    .stTabs [data-baseweb="tab"] {
        background: transparent !important;
        color: var(--er-muted) !important;
        border-radius: 0 !important;
        font-weight: 500 !important;
        font-size: 0.9rem !important;
        letter-spacing: -0.006em !important;
        padding: 0.55rem 1rem !important;
    }
    .stTabs [data-baseweb="tab"]:hover { color: var(--er-body) !important; }
    .stTabs [aria-selected="true"] {
        color: var(--er-ink) !important;
        border-bottom: 2px solid var(--er-accent) !important;
    }
    .stTabs [data-baseweb="tab"] p { color: inherit !important; font-size: 0.9rem !important; }

    /* Métricas — cifras en serif editorial */
    [data-testid="stMetricValue"] {
        font-family: 'Fraunces', Georgia, serif !important;
        color: var(--er-ink) !important;
        font-weight: 500 !important;
    }
    [data-testid="stMetricLabel"], [data-testid="stMetricLabel"] p {
        color: var(--er-muted) !important;
        text-transform: uppercase;
        letter-spacing: 0.07em;
        font-size: 0.7rem !important;
    }

    /* Expanders — tarjeta blanca, hairline */
    [data-testid="stExpander"], div[data-testid="stExpander"] details {
        background: var(--er-surface) !important;
        border: 1px solid var(--er-line) !important;
        border-radius: 8px !important;
    }
    [data-testid="stExpander"] summary,
    [data-testid="stExpander"] summary p { color: var(--er-ink) !important; }

  [data-testid="stPopover"] {
        background: var(--er-surface) !important;
        border: 1px solid var(--er-line) !important;
        border-radius: 10px !important;
        box-shadow: 0 8px 24px rgba(22,21,26,0.10) !important;
    }
    [data-testid="stPopover"] p,
    [data-testid="stPopover"] label { color: var(--er-body) !important; }

    /* Alertas — planas, hairline, borde índigo a la izquierda */
    [data-testid="stAlert"] {
        border-radius: 6px !important;
        border: 1px solid var(--er-line) !important;
        border-left: 3px solid var(--er-accent) !important;
        background: var(--er-surface) !important;
    }
    [data-testid="stAlert"], [data-testid="stAlert"] p { color: var(--er-body) !important; }

    /* Código / bloques de respuesta generada */
    .stCode, pre, code {
        background: var(--er-surface) !important;
        border: 1px solid var(--er-line) !important;
        border-radius: 6px !important;
        color: var(--er-ink) !important;
        font-family: 'IBM Plex Mono', monospace !important;
    }

    /* Radios y checks — acento índigo */
    .stRadio [data-baseweb="radio"] div[aria-checked="true"],
    input[type="checkbox"]:checked {
        background-color: var(--er-accent) !important;
        border-color: var(--er-accent) !important;
    }
    /* El color del slider (y demás widgets nativos: radios, checkboxes,
       barras de progreso) ya NO se fija aquí. Streamlit reescribió el slider
       con React Aria y genera sus clases internas dinámicamente vía Emotion
       (nombres como "st-emotion-cache-1ju38gd" que cambian entre versiones),
       así que perseguirlas con CSS es una guerra que se pierde en cada
       actualización de Streamlit — es lo que pasaba aquí: esta regla apuntaba
       a data-baseweb="slider", un atributo que ya no existe en el DOM real.
       El acento índigo de estos widgets ahora se fija de forma robusta en
       .streamlit/config.toml (theme.primaryColor), que es la vía oficial de
       Streamlit y no depende de ningún nombre de clase interno. */

    /* Divisores */
    hr { border-color: var(--er-line) !important; }

    /* Scrollbar sutil */
    ::-webkit-scrollbar { width: 11px; height: 11px; }
    ::-webkit-scrollbar-track { background: var(--er-canvas); }
    ::-webkit-scrollbar-thumb { background: var(--er-line-2); border-radius: 6px; border: 3px solid var(--er-canvas); }
    ::-webkit-scrollbar-thumb:hover { background: var(--er-faint); }
    </style>
    """, unsafe_allow_html=True)


# Acento de la app (índigo). Fuente única de verdad para Python + CSS dinámico.
ACCENT_INDIGO = "#1a2238"
ACCENT_INDIGO_HOVER = "#232c47"
ACCENT_AMBER = "#c8892a"


def mostrar_guia_uso():
    """
    Guía de uso integrada, con la estética tinta / papel / ámbar de la marca.
    Pensada para que un cliente nuevo (agencia o local) sepa en 2 minutos
    cómo pasar del registro a su primera respuesta publicada, y descubra
    el resto de funcionalidades sin tener que preguntar. Tono: elegante,
    cercano, con chispa, sin caer en lo cursi.
    """
    st.markdown("""
    <style>
      .guia-wrap { max-width: 820px; }
      .guia-hero {
        background: #1a2238; color: #F7F4EE; border-radius: 18px;
        padding: 34px 36px; margin-bottom: 30px; position: relative; overflow: hidden;
      }
      .guia-hero::after {
        content: "§"; position: absolute; right: 18px; top: -40px;
        font-family: 'Fraunces', serif; font-size: 12rem; color: rgba(224,167,66,.08); line-height: 1;
      }
      .guia-hero .kick {
        font-size: .74rem; letter-spacing: .14em; text-transform: uppercase;
        color: #e0a742 !important; font-weight: 600; margin-bottom: 12px;
      }
      .guia-hero h2 {
        font-family: 'Fraunces', serif; font-weight: 600; font-size: 1.9rem;
        line-height: 1.15; margin: 0 0 10px 0; color: #FDFBF7 !important; position: relative;
      }
      .guia-hero p { font-size: 1.02rem; color: #F7F4EE !important; opacity: .88; margin: 0; max-width: 560px; position: relative; }

      .paso {
        display: flex; gap: 20px; align-items: flex-start;
        background: #FDFBF7; border: 1px solid rgba(26,34,56,.12);
        border-radius: 14px; padding: 24px 26px; margin-bottom: 16px;
        transition: transform .15s ease, box-shadow .2s ease;
      }
      .paso:hover { transform: translateX(3px); box-shadow: 0 10px 30px -16px rgba(26,34,56,.3); }
      .paso-num {
        flex-shrink: 0; width: 42px; height: 42px; border-radius: 11px;
        background: #1a2238; color: #e0a742; font-family: 'Fraunces', serif;
        font-size: 1.3rem; font-weight: 600; display: flex; align-items: center;
        justify-content: center;
      }
      .paso-cuerpo h3 {
        font-family: 'Fraunces', serif; font-weight: 600; font-size: 1.18rem;
        color: #1a2238 !important; margin: 2px 0 6px 0;
      }
      .paso-cuerpo p { font-size: .95rem; color: #232c47 !important; opacity: .86; margin: 0 0 6px 0; line-height: 1.6; }
      .paso-tip {
        font-size: .84rem; color: #8a6a1f; background: rgba(200,137,42,.1);
        border-radius: 8px; padding: 7px 12px; display: inline-block; margin-top: 4px;
      }
      .paso-tip b { color: #7a5a15; }

      .guia-sec-titulo {
        font-family: 'Fraunces', serif; font-weight: 600; font-size: 1.3rem;
        color: #1a2238 !important; margin: 34px 0 14px 0; display: flex; align-items: center; gap: 10px;
      }
      .guia-sec-titulo::before { content: ""; width: 26px; height: 2px; background: #c8892a; }

      .func-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
      .func {
        background: #FDFBF7; border: 1px solid rgba(26,34,56,.12);
        border-radius: 12px; padding: 20px 22px;
      }
      .func .ico {
        width: 38px; height: 38px; border-radius: 9px; background: #ECEAF1;
        color: #1a2238; display: flex; align-items: center; justify-content: center;
        font-size: 1.1rem; margin-bottom: 12px;
      }
      .func h4 { font-family: 'Fraunces', serif; font-weight: 600; font-size: 1.05rem; color: #1a2238 !important; margin: 0 0 5px 0; }
      .func p { font-size: .9rem; color: #232c47 !important; opacity: .84; margin: 0; line-height: 1.55; }

      .guia-cierre {
        background: #F1EDE4; border-left: 3px solid #c8892a; border-radius: 0 12px 12px 0;
        padding: 20px 26px; margin-top: 30px;
      }
      .guia-cierre p { margin: 0; font-size: .96rem; color: #232c47 !important; line-height: 1.65; }
      .guia-cierre b { color: #1a2238; }

      @media (max-width: 640px) { .func-grid { grid-template-columns: 1fr; } }
    </style>

    <div class="guia-wrap">
      <div class="guia-hero">
        <div class="kick">Guía de uso · 2 minutos</div>
        <h2>De cero a tu primera respuesta publicada</h2>
        <p>Sin manuales de 40 páginas. Cuatro pasos para tener tu primera reseña contestada con blindaje legal, y un mapa de todo lo demás que puedes hacer aquí.</p>
      </div>

      <div class="guia-sec-titulo">Los cuatro pasos para empezar</div>

      <div class="paso">
        <div class="paso-num">1</div>
        <div class="paso-cuerpo">
          <h3>Da de alta tu primer local</h3>
          <p>En la pestaña <b>Generar respuesta</b>, abre "Añadir establecimiento" y rellena el nombre, la ciudad y el enlace de reseñas de Google del negocio. Si eres agencia, repite esto por cada cliente: todos conviven en el mismo panel.</p>
          <span class="paso-tip"><b>Truco:</b> pega también un par de palabras clave del negocio ("arrocería en Valencia") y el SEO trabajará solo.</span>
        </div>
      </div>

      <div class="paso">
        <div class="paso-num">2</div>
        <div class="paso-cuerpo">
          <h3>Pega una reseña y elige el tono</h3>
          <p>Copia la reseña de Google —buena o mala, en el idioma que sea— y pégala. Escoge uno de los tres registros: muy formal, profesional estándar o cercano y cálido. Pulsa generar.</p>
          <span class="paso-tip"><b>Descuida:</b> detecta el idioma solo y te da también la traducción al español para que sepas exactamente qué vas a publicar.</span>
        </div>
      </div>

      <div class="paso">
        <div class="paso-num">3</div>
        <div class="paso-cuerpo">
          <h3>Revisa y publica</h3>
          <p>En 10 segundos tienes la respuesta, ya pasada por las 16 reglas de blindaje legal. Léela, y si te convence, cópiala y pégala en la respuesta de Google. Tú tienes siempre la última palabra antes de que salga.</p>
          <span class="paso-tip"><b>Por qué copiar y pegar:</b> ese último vistazo humano es justo el control que da valor a tu servicio.</span>
        </div>
      </div>

      <div class="paso">
        <div class="paso-num">4</div>
        <div class="paso-cuerpo">
          <h3>Cierra el círculo: pide más reseñas</h3>
          <p>En la pestaña <b>Pedir reseñas</b> generas un código QR y un mensaje de WhatsApp listo para enviar. Cuantas más reseñas buenas entren, más sube el Reputation Score, y más tienes que contestar aquí. Rueda que gira sola.</p>
          <span class="paso-tip"><b>Idea:</b> imprime el QR en el ticket, la mesa o la puerta. Reseñas nuevas sin pedirlas de viva voz.</span>
        </div>
      </div>

      <div class="guia-sec-titulo">Todo lo demás que tienes aquí</div>
      <div class="func-grid">
        <div class="func">
          <div class="ico">◆</div>
          <h4>Contenido SEO</h4>
          <p>Seis tipos de contenido con tres variantes cada uno para las fichas de Google de tus clientes. Publícalo o véndelo como gestión de ficha.</p>
        </div>
        <div class="func">
          <div class="ico">▤</div>
          <h4>Analítica e informes</h4>
          <p>Reputation Score, evolución por periodo, calculadora de retorno y el informe PDF con tu logo que le mandas al cliente cada mes.</p>
        </div>
        <div class="func">
          <div class="ico">⛉</div>
          <h4>Tu marca</h4>
          <p>Sube tu logo y tu color en los ajustes de agencia. Todo —panel e informes— sale con tu identidad, no con la nuestra.</p>
        </div>
        <div class="func">
          <div class="ico">⚏</div>
          <h4>Tu equipo</h4>
          <p>Añade a tu gente con roles de administrador o gestor. Cada respuesta queda registrada por autor: sabes quién hizo qué.</p>
        </div>
      </div>

      <div class="guia-cierre">
        <p><b>¿Te atascas en algo?</b> No hay pregunta tonta. Escríbenos desde el enlace de soporte y te echamos una mano. Y recuerda la mejor forma de probar esto: coge tus tres peores reseñas, esas que llevas semanas sin saber cómo contestar, y pásalas por aquí. Ahí se ve la diferencia.</p>
      </div>
    </div>
    """, unsafe_allow_html=True)


def mostrar_barras_simples(conteo, color=ACCENT_INDIGO):
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
        <div style="margin-bottom:12px;">
            <div style="display:flex; justify-content:space-between; font-size:0.85rem; color:#232c47; margin-bottom:5px;">
                <span>{_html.escape(str(etiqueta))}</span><span style="font-family:'IBM Plex Mono',monospace; color:#1a2238;">{valor}</span>
            </div>
            <div style="background:#F4F3EF; border:1px solid #E6E4DE; border-radius:4px; height:8px; width:100%;">
                <div style="background:{color}; border-radius:4px; height:8px; width:{ancho_pct}%;"></div>
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
# 🏆 RESELIA REPUTATION SCORE
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
    """Traduce el número a una banda con nombre y color, sobre lienzo claro.
    El índigo marca la excelencia; el resto gradúa en tinta; solo el riesgo
    real usa un rojo controlado."""
    if score is None:
        return ("Sin datos", "#6b7280")
    if score >= 80:
        return ("Excelente", "#1a2238")   # índigo — el acento
    if score >= 60:
        return ("Buena", "#232c47")        # tinta cuerpo
    if score >= 40:
        return ("Mejorable", "#6b7280")    # gris medio
    return ("En riesgo", "#A23A34")        # rojo controlado


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

    # Cabecera: número grande en serif + banda en versalitas
    st.markdown(f"""
    <div style="background:#FFFFFF; border:1px solid #E6E4DE; border-radius:10px; padding:24px 28px; margin-bottom:16px;">
        <div style="font-size:0.72rem; color:#6b7280; margin-bottom:14px; text-transform:uppercase; letter-spacing:0.14em;">{_html.escape(titulo)}</div>
        <div style="display:flex; align-items:baseline; gap:14px;">
            <span style="font-family:'Fraunces',Georgia,serif; font-size:4rem; font-weight:500; color:{color}; line-height:0.9;">{score}</span>
            <span style="font-family:'Fraunces',Georgia,serif; font-size:1.2rem; color:#6b7280;">/ 100</span>
            <span style="margin-left:auto; border:1px solid {color}; color:{color}; font-weight:600; font-size:0.7rem; padding:5px 14px; border-radius:6px; text-transform:uppercase; letter-spacing:0.12em;">{banda}</span>
        </div>
        <div style="background:#F4F3EF; border:1px solid #E6E4DE; border-radius:4px; height:6px; width:100%; margin-top:20px;">
            <div style="background:{color}; border-radius:4px; height:6px; width:{score}%;"></div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown(f"<div style='color:#232c47; font-size:0.95rem; line-height:1.6; margin-bottom:16px;'>{_html.escape(interpretacion)}</div>", unsafe_allow_html=True)

    # Desglose de factores
    factores = resultado_score.get("factores", {})
    if factores:
        st.markdown("<div style='font-size:0.72rem; color:#6b7280; text-transform:uppercase; letter-spacing:0.12em; margin-bottom:10px;'>Composición de la puntuación</div>", unsafe_allow_html=True)
        topes = {"Sentimiento": 50, "Volumen": 20, "Constancia": 20, "Tendencia": 10}
        filas_html = ""
        for nombre, pts in factores.items():
            tope = topes.get(nombre, 1)
            ancho_pct = int((pts / tope) * 100) if tope else 0
            filas_html += f"""
            <div style="margin-bottom:10px;">
                <div style="display:flex; justify-content:space-between; font-size:0.82rem; color:#6b7280; margin-bottom:4px;">
                    <span>{_html.escape(nombre)}</span><span style="font-family:'IBM Plex Mono',monospace; color:#232c47;">{pts} / {tope}</span>
                </div>
                <div style="background:#F4F3EF; border:1px solid #E6E4DE; border-radius:4px; height:6px; width:100%;">
                    <div style="background:{color}; border-radius:4px; height:6px; width:{ancho_pct}%;"></div>
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
    <div style="background:#FDFBF7; border:1px solid rgba(26,34,56,.12); border-left:3px solid #c8892a; border-radius:0 10px 10px 0; padding:20px 24px; margin:10px 0;">
        <div style="font-size:0.72rem; color:#6b7280; margin-bottom:16px; text-transform:uppercase; letter-spacing:0.1em;">
            Proyección · {estrellas_actuales}★ → {estrellas_objetivo}★ (+{roi['delta_estrellas']})
        </div>
        <div style="display:flex; gap:48px; flex-wrap:wrap;">
            <div>
                <div style="font-size:0.72rem; color:#6b7280; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;">Ingresos extra / mes</div>
                <div style="font-family:'Fraunces',Georgia,serif; font-size:1.9rem; font-weight:500; color:#1a2238; line-height:1.2;">
                    {_html.escape(_fmt_eur(roi['mensual_min']))} – {_html.escape(_fmt_eur(roi['mensual_max']))}
                </div>
            </div>
            <div>
                <div style="font-size:0.72rem; color:#6b7280; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;">Ingresos extra / año</div>
                <div style="font-family:'Fraunces',Georgia,serif; font-size:1.9rem; font-weight:500; color:#1a2238; line-height:1.2;">
                    {_html.escape(_fmt_eur(roi['anual_min']))} – {_html.escape(_fmt_eur(roi['anual_max']))}
                </div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.caption(f"Estimación basada en el estudio de Harvard Business School: +1★ ≈ +5-9% de ingresos en negocios independientes. {ROI_FUENTE}")




# -----------------------------------------------------------------------
# MODO BETA: interruptor único para dar respuestas ilimitadas en el plan
# Free mientras se capta a los primeros clientes/agencias de demo.
#
# Por qué un interruptor y no borrar el límite directamente: cuando quieras
# volver al límite normal de 10/mes (por ejemplo, al cerrar la fase beta),
# basta con cambiar esto a False — no hay que recordar qué número había
# antes ni tocar nada más en el código.
#
# Qué SÍ te sigue protegiendo aunque esto esté en True: el rate limit de
# verificar_velocidad() (más abajo) sigue activo y limita por hora/día,
# así que un uso descontrolado o malicioso (cientos de respuestas seguidas)
# se sigue frenando. Lo único que se quita es el tope mensual de 10.
#
# Ten en cuenta: cada respuesta generada consume la API de Claude, que
# tiene un coste real. Con esto en True, vigila el consumo mientras dure
# la fase beta.
# -----------------------------------------------------------------------
MODO_BETA_RESPUESTAS_ILIMITADAS = True

LIMITE_USOS_PLAN_GRATIS = 10  # respuestas por mes incluidas en el plan Free fuera de la ventana de beta
# Límite de respuestas/mes por plan. None = ilimitado.
# El plan 'individual' es ahora ilimitado (1 local, sin tope de reseñas); el
# blindaje anti-abuso se hace por VELOCIDAD (rate limit por hora/día), no por cupo mensual.
# NOTA: el "free" de aquí es el límite BASE, fuera de la ventana de beta. Mientras
# una agencia esté dentro de su ventana (ver agencia_en_beta más abajo), este
# límite se ignora en el punto donde se comprueba el cupo mensual.
LIMITE_USOS_POR_PLAN = {
    "free": 10,
    "individual": None,        # 1 local, respuestas ilimitadas
    "starter": None,
    "growth": None,
    # Legado: el plan Enterprise ya no se vende (ver PLANES_AUTOSERVICIO). La
    # clave se mantiene sólo por si quedara alguna fila antigua en Supabase.
    "enterprise": None,
}

# =========================================================
# 🛡️ TECHO MENSUAL "BLANDO" PARA PLANES ILIMITADOS
# El límite de velocidad (LIMITES_VELOCIDAD_POR_PLAN, más abajo) ya frena que
# alguien meta 60 respuestas en 10 minutos, pero no frena el caso de un local
# con volumen genuinamente descomunal (p.ej. una discoteca con miles de
# reseñas atrasadas) que, yendo siempre por debajo del límite de velocidad,
# puede vaciar todo el backlog en unas semanas pagando la cuota más barata.
# Eso no es "trampa" del cliente —está usando el producto tal como se anuncia—
# pero sí puede desajustar el margen de esa cuenta concreta frente al coste
# real de API.
#
# Por eso esto NO es un muro duro: es un techo alto, pensado para no rozar
# jamás a un negocio real de tamaño normal, que cuando se alcanza no corta el
# servicio de golpe sino que ofrece hablar para ampliarlo (igual que ya se
# hace con el límite de velocidad). Así protege margen sin penalizar a nadie
# que esté usando el plan como se espera.
# None = sin techo.
LIMITE_MENSUAL_BLANDO_PLANES_ILIMITADOS = {
    "individual": 400,   # ~13/día de media — de sobra para cualquier local normal
    "starter":    1500,  # varios locales
    "growth":     4000,
    "enterprise": None,   # legado, ver nota en PLANES_AUTOSERVICIO
}
LIMITE_LOCALES_POR_PLAN = {"free": 1, "individual": 1,
                            "starter": 10, "growth": 30, "enterprise": None}  # None = sin límite
# Nº máximo de usuarios (miembros del equipo) por plan. None = sin límite.
# Free e Individual son de un solo usuario; los planes de agencia permiten equipo.
LIMITE_USUARIOS_POR_PLAN = {"free": 1, "individual": 1,
                            "starter": 5, "growth": 15, "enterprise": None}
UMBRAL_ACTIVIDAD_INUSUAL_POR_LOCAL = 150  # aviso informativo, no bloqueante
# Antes era r"^[^@\s]+@[^@\s]+\.[^@\s]+$", que solo prohibía espacios y
# arrobas: aceptaba <, > y comillas, así que "<img/src=x/onerror=1>@a.bc" se
# daba por válido y acababa interpolado en el HTML de la barra lateral.
EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def agencia_en_beta(agencia):
    """
    True si esta agencia concreta todavía está dentro de su ventana personal
    de beta (respuestas ilimitadas gratis), False si ya expiró o si nunca
    aplicó (planes de pago no la necesitan, ya son ilimitados por su cuenta).

    Cómo funciona la ventana: cada agencia tiene su propia fecha de alta
    (creado_en) y su propio nº de días de beta (dias_beta, columna en Supabase,
    por defecto 7 — ver migracion_dias_beta.sql). Los primeros clientes de la
    semana de lanzamiento se suben a mano a 30 días desde el Table Editor de
    Supabase. Así cada agencia caduca en su propia fecha, sin tocar código ni
    reiniciar nada cuando se les acaba el plazo — simplemente, a partir de esa
    fecha, vuelven a los límites normales del plan Free.

    MODO_BETA_RESPUESTAS_ILIMITADAS actúa como interruptor maestro: si algún
    día quieres cortar el programa de beta entero de golpe (para todas las
    agencias a la vez, sin esperar a que expire cada una), basta con ponerlo
    en False aquí arriba.
    """
    if not MODO_BETA_RESPUESTAS_ILIMITADAS:
        return False
    if agencia.get("plan", "free") != "free":
        return False  # los planes de pago no necesitan este mecanismo
    creado_en_raw = agencia.get("creado_en")
    if not creado_en_raw:
        return False
    try:
        fecha_alta = datetime.fromisoformat(creado_en_raw.replace("Z", "+00:00"))
        if fecha_alta.tzinfo is not None:
            fecha_alta = fecha_alta.replace(tzinfo=None)
    except (ValueError, AttributeError):
        return False
    dias_beta = agencia.get("dias_beta", 7) or 7
    return datetime.utcnow() < fecha_alta + timedelta(days=dias_beta)

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
        "mensual": "price_1TsLo0Kwc34DG74MzRU5g3YH",   # ⚠️ crear en Stripe (25€/mes)
        "anual":   "price_TODO_INDIVIDUAL_240EUR_ANO",   # ⚠️ crear en Stripe (240€/año = 25×12×0,8)
    },
    "starter": {
        "mensual": "price_1TqCVYKwc34DG74MpaWMOaKt",     # existente (ajusta el importe a 89€ en Stripe)
        "anual":   "price_TODO_STARTER_850EUR_ANO",       # ⚠️ crear en Stripe (850€/año = 89×12×0,8)
    },
    "growth": {
        "mensual": "price_1TqCZFKwc34DG74Mpw8r8lfi",     # existente (ajusta el importe a 299€ en Stripe)
        "anual":   "price_TODO_GROWTH_2870EUR_ANO",       # ⚠️ crear en Stripe (2.870€/año = 299×12×0,8)
    },
    # Enterprise eliminado: el plan ya no existe, así que no hay precio que cobrar.
    # Si algún día vuelve, se recrea aquí con sus price_ids nuevos de Stripe.
    # IMPORTANTE: archiva también los precios antiguos en el Dashboard de Stripe
    # (Producto → Pricing → Archive) para que nadie pueda reutilizar un enlace viejo.
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
    return int(round(n / 10.0) * 10)


def _precio_anual_mensualizado(precio_mensual):
    """Precio equivalente por mes cuando se paga el año con el descuento anual."""
    return round(precio_mensual * (1 - DESCUENTO_ANUAL))


def _precio_anual_total(precio_mensual):
    """Precio total del año (lo que se cobra de una vez al elegir facturación anual).

    Fijado a mano por plan para tener cifras comerciales limpias en lugar del
    resultado 'feo' de mensual × 12 × 0,8 (que daba 758, 1910, 4310...).
    👉 RETOCA AQUÍ los importes anuales que quieras; el "€/mes equivalente" y el
    "ahorras X€/año" que se muestran en los planes se recalculan solos a partir
    de este número. Si un plan no está en la tabla, usa la fórmula automática.
    """
    precios_anuales = {
        39:  370,   # Individual  (39×12×0,8 = 374 → 370)
        89:  850,   # Starter     (89×12×0,8 = 854 → 850)
        299: 2870,  # Growth      (299×12×0,8 = 2.870,4 → 2.870)
    }
    if precio_mensual in precios_anuales:
        return precios_anuales[precio_mensual]
    return _redondear_bonito(precio_mensual * 12 * (1 - DESCUENTO_ANUAL))

PLANES_AUTOSERVICIO = {
    "individual": {
        "nombre": "Individual", "target": "Un solo local · sin límite de reseñas",
        "precio_mensual": 39, "price_ids": STRIPE_PRICES["individual"],
        "features": ["1 local", "Respuestas ILIMITADAS", "Reputation Score + calculadora de ROI",
                     "Blindaje legal + informe PDF de marca"],
        "gancho": "Para el bar, restaurante o camping que gestiona sus propias reseñas.",
    },
    "starter": {
        "nombre": "Starter", "target": "Agencias pequeñas · hasta 10 locales",
        "precio_mensual": 89, "price_ids": STRIPE_PRICES["starter"],
        "features": ["Hasta 10 locales", "Respuestas ilimitadas", "Marca blanca completa",
                     "SEO invisible por local"],
        "gancho": "Desde 8,90€ por local al mes.",
    },
    "growth": {
        "nombre": "Growth", "target": "Agencias medianas · hasta 30 locales",
        "precio_mensual": 299, "price_ids": STRIPE_PRICES["growth"],
        "features": ["Hasta 30 locales", "Respuestas ilimitadas", "Marca blanca completa",
                     "Multi-usuario + analítica + ROI"],
        "gancho": "Menos de 10€ por local — el favorito de las agencias.",
        "destacado": True,
    },
    # Enterprise ELIMINADO definitivamente del catálogo (agosto 2026).
    #
    # Al quitarlo de aquí desaparece de los DOS sitios donde se pintaba: la
    # landing (que ya lo excluía a mano) y el selector de planes de dentro de
    # la app, render_pagina_planes_upgrade(), que recorre este diccionario
    # entero y por tanto SÍ lo seguía enseñando hasta ahora.
    #
    # Las tablas de límites de más arriba (LIMITE_LOCALES_POR_PLAN,
    # LIMITE_USUARIOS_POR_PLAN, LIMITES_VELOCIDAD_POR_PLAN...) conservan a
    # propósito su clave "enterprise" como red de seguridad: se consultan con
    # .get(plan) y, si quedara alguna fila en Supabase con plan='enterprise',
    # seguiría resolviendo sus límites en vez de romper la sesión.
    # Cuando confirmes con un SELECT que no queda ninguna fila así, puedes
    # borrar también esas claves sin ningún riesgo.
}

# =========================================================
# MODO BETA — PAGOS DESACTIVADOS
# =========================================================
# Durante la beta no se cobra a nadie. Este interruptor es la ÚNICA fuente de
# verdad: se comprueba tanto en la capa de UI (para que los botones de compra
# expliquen la situación en vez de llevar a Stripe) como dentro de las propias
# funciones que crean la sesión de pago.
#
# Comprobarlo en los dos sitios es deliberado. Si solo se ocultaran los botones,
# cualquier ruta que llame a crear_sesion_pago_* desde otro punto —un enlace
# antiguo, una URL guardada, un flujo que se añada más adelante— seguiría
# generando cobros reales. El bloqueo tiene que estar en la función que cobra,
# no solo en el botón que se ve.
#
# PARA TERMINAR LA BETA: cambiar esta línea a False. Nada más.
MODO_BETA_SIN_PAGOS = True

BETA_MENSAJE_PLANES = (
    "Reselia está en beta abierta y los pagos están desactivados. "
    "Durante este periodo el acceso es gratuito y sin límite de respuestas."
)


# Compatibilidad hacia atrás: algunas partes del código antiguo referencian estos nombres.
STRIPE_PRICE_ID_INDIVIDUAL = STRIPE_PRICES["individual"]["mensual"]
STRIPE_PRICE_ID_STARTER = STRIPE_PRICES["starter"]["mensual"]
STRIPE_PRICE_ID_GROWTH = STRIPE_PRICES["growth"]["mensual"]
# STRIPE_PRICE_ID_ENTERPRISE se elimina con el plan. Nadie lo referenciaba
# fuera de esta línea, así que quitarlo no rompe ninguna ruta de pago.


def crear_sesion_pago_stripe(agencia_id, plan_nombre, price_id):
    """
    Crea una sesión de Stripe Checkout dinámica para que la agencia contrate un plan.
    Guarda agencia_id y plan en la metadata de la sesión: así, cuando el pago se confirme,
    sabremos automáticamente a qué agencia activarle qué plan sin tocar nada a mano.
    Devuelve la URL de pago, o None si algo falla.
    """
    if MODO_BETA_SIN_PAGOS:
        st.info(BETA_MENSAJE_PLANES)
        return None

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            client_reference_id=str(agencia_id),
            metadata={"agencia_id": str(agencia_id), "plan": plan_nombre},
            # La metadata de la sesión NO se copia a la suscripción; la propagamos
            # aquí para que los eventos customer.subscription.* del webhook lleven el
            # plan y la agencia y podamos sincronizar sin mapear precios a mano.
            subscription_data={"metadata": {"agencia_id": str(agencia_id), "plan": plan_nombre}},
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
    if MODO_BETA_SIN_PAGOS:
        st.info(BETA_MENSAJE_PLANES)
        return None

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={"plan": plan_nombre, "flujo": "alta_nueva"},
            # Propagamos el plan a la suscripción para que el webhook lo reciba en
            # los eventos customer.subscription.* (la agencia aún no existe aquí, así
            # que el webhook la localizará luego por stripe_customer_id).
            subscription_data={"metadata": {"plan": plan_nombre, "flujo": "alta_nueva"}},
            success_url=f"{APP_URL}/?alta_nueva=1&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_URL}/?pago_cancelado=1",
        )
        return session.url
    except Exception as e:
        st.error(redactar_secretos(f"No se pudo iniciar el proceso de pago: {e}"))
        return None


def crear_portal_cliente(stripe_customer_id):
    """
    Crea una sesión del Customer Portal de Stripe para que el propio cliente gestione
    su suscripción (cambiar método de pago, descargar facturas, cancelar) sin tener que
    escribirnos. Devuelve la URL del portal, o None si algo falla.

    Requisito: hay que activar el Customer Portal una vez en el Dashboard de Stripe
    (Settings → Billing → Customer portal). Si no está configurado, Stripe devuelve error.
    """
    try:
        portal = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=f"{APP_URL}/",
        )
        return portal.url
    except Exception as e:
        st.error(redactar_secretos(f"No se pudo abrir el portal de suscripción: {e}"))
        return None


def _stripe_campo(obj, campo, default=None):
    """
    Lee un campo de una respuesta de Stripe de forma segura, sea cual sea la versión
    del SDK. En las versiones recientes, stripe.checkout.Session.retrieve() devuelve
    objetos tipados que NO exponen el método .get() de dict; llamar a obj.get(...)
    sobre ellos dispara su __getattr__ y lanza AttributeError('get') — que es
    exactamente el error "get" que aparecía en pantalla tras un pago correcto.

    getattr(obj, campo, default) NUNCA lanza esa excepción (devuelve default si el
    atributo no existe), y solo si eso no da resultado probamos el acceso tipo dict
    obj[campo] dentro de un try. Así funciona tanto con StripeObject (atributo) como
    con dict plano (subíndice), sin volver a tropezar con "get".
    """
    if obj is None:
        return default
    valor = getattr(obj, campo, None)
    if valor is not None:
        return valor
    try:
        return obj[campo]
    except Exception:
        return default


def confirmar_pago_y_activar_plan(session_id):
    """
    Se llama cuando Stripe redirige de vuelta a la app tras un pago DE UPGRADE (agencia ya
    existente). Verifica contra la propia Stripe (nunca te fíes solo de la URL) que el pago
    se ha completado de verdad, y si es así, activa el plan de la agencia en Supabase
    automáticamente. Devuelve (True, plan_nombre) o (False, "motivo").
    """
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        estado = _stripe_campo(session, "payment_status")
        if estado != "paid":
            return False, "El pago todavía no se ha confirmado."
        metadata = _stripe_campo(session, "metadata", {}) or {}
        agencia_id = _stripe_campo(metadata, "agencia_id")
        plan_nombre = _stripe_campo(metadata, "plan")
        if not agencia_id or not plan_nombre:
            return False, "No se pudo identificar la agencia o el plan asociado a este pago."
        # Guardamos también el stripe_customer_id: es la clave con la que el webhook
        # localiza a esta agencia cuando Stripe avise de una cancelación o un impago.
        datos_update = {"plan": plan_nombre}
        customer_id = _stripe_campo(session, "customer")
        if customer_id:
            datos_update["stripe_customer_id"] = customer_id
        supabase.table("agencias").update(datos_update).eq("id", agencia_id).execute()
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
        estado = _stripe_campo(session, "payment_status")
        if estado != "paid":
            return False, "El pago todavía no se ha confirmado."
        metadata = _stripe_campo(session, "metadata", {}) or {}
        plan_nombre = _stripe_campo(metadata, "plan")
        if not plan_nombre:
            return False, "No se pudo identificar el plan asociado a este pago."
        detalles = _stripe_campo(session, "customer_details")
        email_prefill = _stripe_campo(detalles, "email", "") or ""
        return True, {
            "session_id": session_id,
            "plan": plan_nombre,
            "stripe_customer_id": _stripe_campo(session, "customer"),
            "email_prefill": email_prefill,
        }
    except Exception as e:
        return False, str(e)


# =========================================================
# 🛡️ BLINDAJE DEL ALTA GRATUITA — evitar cuentas falsas en masa
# El plan Free es la puerta de entrada sin fricción (sin tarjeta), así que es
# el punto más barato de abusar: un script que registre miles de cuentas con
# emails inventados se lleva, cada una, sus respuestas gratis (10/mes fuera
# de la ventana de beta — ver LIMITE_USOS_PLAN_GRATIS).
#
# Ninguna de estas comprobaciones bloquea a una persona real. Todas están
# pensadas para no rozar jamás a alguien que rellena el formulario a mano:
#   1) Dominios de email desechable — de sobra conocidos y usados solo para
#      recibir un código y desaparecer, ningún negocio real usa uno de éstos
#      como email de trabajo.
#   2) Límite de altas por IP al día — best-effort: si el hosting no expone
#      la IP real (algunos proxys la ocultan), simplemente no se aplica, en
#      vez de bloquear el alta por no poder comprobarlo.
#   3) Velocidad de relleno — un humano tarda como mínimo unos segundos en
#      escribir 5 campos; un script los rellena y envía en milisegundos.
# =========================================================

DOMINIOS_EMAIL_DESECHABLE = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.info", "10minutemail.com",
    "10minutemail.net", "tempmail.com", "temp-mail.org", "yopmail.com", "yopmail.fr",
    "trashmail.com", "getnada.com", "throwawaymail.com", "fakeinbox.com", "sharklasers.com",
    "maildrop.cc", "mintemail.com", "dispostable.com", "mailnesia.com", "moakt.com",
    "emailondeck.com", "burnermail.io", "tempinbox.com", "spamgourmet.com", "mohmal.com",
    "tempr.email", "discardmail.com", "mailcatch.com", "inboxbear.com", "tempail.com",
    "correotemporal.org", "emailtemporal.org", "email-temporal.net", "temporal-mail.com",
}


def _es_email_desechable(email_normalizado):
    """True si el dominio del email es uno de servicio de correo temporal/desechable
    conocido. No pretende ser una lista exhaustiva (aparecen dominios nuevos cada
    semana) — es una primera barrera barata contra el abuso más obvio, no la única."""
    dominio = email_normalizado.rsplit("@", 1)[-1] if "@" in email_normalizado else ""
    return dominio in DOMINIOS_EMAIL_DESECHABLE


def _obtener_ip_cliente():
    """
    Intenta obtener la IP real de quien está usando la app, a partir de las
    cabeceras HTTP (Streamlit corre detrás de un proxy tanto en Streamlit Cloud
    como en Render, así que la IP real viaja en X-Forwarded-For, no en la
    conexión directa). Devuelve None si no se puede obtener por cualquier
    motivo — nunca lanza excepción, y el código que la usa debe tratar el
    None como "no se puede comprobar, no bloquear por esto".
    """
    try:
        cabeceras = st.context.headers
        reenviada = cabeceras.get("X-Forwarded-For") or cabeceras.get("x-forwarded-for")
        if reenviada:
            # ÚLTIMO elemento, no el primero. X-Forwarded-For se construye
            # por acumulación: el primer valor es el que MANDA EL CLIENTE, así
            # que un atacante solo tiene que enviar la cabecera con una IP
            # inventada y tu proxy le añade la real detrás. Cogiendo [0] nos
            # quedábamos con la falsa, distinta en cada petición, y los límites
            # de altas por IP se saltaban con una línea de curl.
            # El último valor lo escribe nuestro propio proxy (Render), que es
            # el único eslabón de la cadena en el que podemos confiar.
            return reenviada.split(",")[-1].strip()
    except Exception:
        pass
    return None


LIMITE_ALTAS_FREE_POR_IP = 3
LIMITE_ALTAS_FREE_POR_HORA = 2

# POR QUÉ DOS LÍMITES Y NO UNO
# -----------------------------
# Una IP no identifica a una persona: identifica a una conexión. Una agencia
# con oficina sale entera por la misma IP, y los operadores móviles españoles
# usan CGNAT, con lo que miles de clientes comparten una sola IP pública. Un
# límite rígido y bajo por IP bloquea sobre todo a gente legítima.
#
# Lo que distingue a un abuso de un uso normal no es CUÁNTAS cuentas se crean,
# sino a QUÉ RITMO. Tres compañeros de una agencia se registran a lo largo de
# una mañana; un script crea diez cuentas en dos minutos. Por eso hay dos
# límites que miden cosas distintas:
#
#   · LIMITE_ALTAS_FREE_POR_IP (3, histórico): el techo total. Cubre el caso
#     de una oficina pequeña o de alguien que abre una segunda cuenta de
#     prueba, sin dejar la puerta abierta indefinidamente.
#
#   · LIMITE_ALTAS_FREE_POR_HORA (2): el freno de ritmo. Un humano no crea
#     tres cuentas en sesenta minutos; un bot sí. Este es el límite que de
#     verdad para los ataques, y casi nunca lo nota nadie legítimo.
#
# El resultado práctico: la oficina de tres personas entra sin fricción, y un
# script que intenta veinte altas se detiene en la tercera.

VENTANA_ALTAS_FREE_HORAS = 1


def _ip_supera_limite_altas_free(ip):
    """
    Comprueba los dos límites de alta gratuita para una IP.

    Devuelve (bloqueado, motivo), donde motivo es 'total', 'ritmo' o None.
    Distinguirlos importa porque el mensaje al usuario debe ser distinto: un
    bloqueo por ritmo se resuelve esperando un rato, y conviene decirlo; uno
    por total no, y ahí toca ofrecer otra vía.

    Requiere la tabla 'intentos_registro_free'. Si no existe o falla la
    consulta, devuelve (False, None): ante un fallo de infraestructura se
    deja pasar el alta en vez de bloquear a alguien legítimo.
    """
    if not ip:
        return False, None

    try:
        total = supabase.table("intentos_registro_free") \
            .select("id", count="exact") \
            .eq("ip", ip) \
            .execute()
        if (total.count or 0) >= LIMITE_ALTAS_FREE_POR_IP:
            return True, "total"
    except Exception:
        return False, None

    # Freno de ritmo. Se consulta aparte porque necesita filtrar por fecha, y
    # porque si esta segunda consulta fallara no queremos perder el resultado
    # del límite total, que ya se ha comprobado bien.
    try:
        desde = (datetime.utcnow() - timedelta(hours=VENTANA_ALTAS_FREE_HORAS)).isoformat()
        recientes = supabase.table("intentos_registro_free") \
            .select("id", count="exact") \
            .eq("ip", ip) \
            .gte("creado_en", desde) \
            .execute()
        if (recientes.count or 0) >= LIMITE_ALTAS_FREE_POR_HORA:
            return True, "ritmo"
    except Exception:
        pass

    return False, None


def _registrar_intento_alta_free(ip):
    """Guarda un intento de alta Free para la IP dada. Si la tabla no existe
    todavía o falla por lo que sea, no interrumpe el alta — es solo tracking."""
    if not ip:
        return
    try:
        supabase.table("intentos_registro_free").insert({"ip": ip}).execute()
    except Exception:
        pass


def registrar_agencia_gratuita(nombre_agencia, nombre_local, email, password_plano, nombre_usuario, segundos_desde_apertura=None):
    """
    Alta de autoservicio para el plan Free: crea la agencia (plan='free'),
    su primer usuario y un primer local, sin intervención manual.
    Devuelve (True, None) si todo ha ido bien, o (False, "motivo") si ha fallado.

    segundos_desde_apertura: tiempo transcurrido desde que se abrió el formulario
    hasta que se pulsó "Crear cuenta gratis". None si no se pudo medir (no bloquea
    por sí solo). Un valor muy bajo (< 2s) es la huella típica de un script que
    rellena y envía el formulario sin intervención humana.
    """
    email_normalizado = email.lower().strip()

    if not EMAIL_REGEX.match(email_normalizado):
        return False, "El email no tiene un formato válido."
    if len(password_plano) < 8:
        return False, "La contraseña debe tener al menos 8 caracteres."
    if _es_email_desechable(email_normalizado):
        return False, ("Ese proveedor de email temporal no está admitido para crear una cuenta. "
                        "Usa un email normal de trabajo o personal.")
    if segundos_desde_apertura is not None and segundos_desde_apertura < 2:
        return False, "Rellena el formulario de nuevo, por favor."

    existente = supabase.table("usuarios").select("id").eq("email", email_normalizado).execute()
    if existente.data:
        return False, "Ya existe una cuenta con ese email. Inicia sesión en su lugar."

    ip_cliente = _obtener_ip_cliente()
    _bloqueado, _motivo = _ip_supera_limite_altas_free(ip_cliente)
    if _bloqueado:
        if _motivo == "ritmo":
            # Bloqueo temporal: se resuelve solo. Decirlo evita que alguien
            # legítimo dé por hecho que la app está rota y se marche.
            return False, (
                "Se han creado varias cuentas desde esta conexión en muy poco "
                "tiempo. Espera una hora y vuelve a intentarlo. Si necesitas "
                "acceso ahora mismo, escríbenos a hola@reselia.es."
            )
        return False, (
            "Ya se han creado varias cuentas gratuitas desde esta conexión. "
            "Si estás en una oficina o un coworking y necesitas cuentas para "
            "más compañeros, escríbenos a hola@reselia.es y te las damos."
        )

    try:
        nueva_agencia = supabase.table("agencias").insert({
            "nombre_agencia": nombre_agencia.strip(),
            "logo_url": "https://dummyimage.com/220x60/1a2238/E9C46A&text=Reselia",
            "color_marca": "#2A2C31",
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

        _registrar_intento_alta_free(ip_cliente)

        return True, None
    except Exception as e:
        return False, f"Error al crear la cuenta: {e}"


def _huella_tarjeta_stripe(stripe_customer_id):
    """
    Devuelve la 'fingerprint' (huella) de la tarjeta que ha usado un cliente de Stripe
    para pagar, o None si no se puede obtener. La huella es un hash que Stripe calcula
    a partir de los datos reales de la tarjeta: la MISMA tarjeta física da SIEMPRE la
    misma huella, aunque se use en cuentas, emails o nombres distintos.

    Se usa solo como SEÑAL para detectar multi-cuenta en el plan Individual (una misma
    persona abriendo varias agencias de 1 local con emails distintos para no pagar un
    plan de agencia). Nunca bloquea nada por sí sola — ver revisar_multicuenta más abajo.
    Si falla (customer sin tarjeta, error de red, etc.) devuelve None y no se hace nada
    con ello: preferimos no detectar un duplicado antes que bloquear un alta legítima.
    """
    if not stripe_customer_id:
        return None
    try:
        metodos = stripe.PaymentMethod.list(customer=stripe_customer_id, type="card", limit=1)
        if metodos.data:
            return metodos.data[0].card.fingerprint
    except Exception:
        pass
    return None


def _detectar_posible_multicuenta_individual(huella_tarjeta):
    """
    Comprueba si esta misma huella de tarjeta ya está asociada a OTRA agencia con
    plan Individual. No bloquea el alta en ningún caso (una familia puede compartir
    tarjeta legítimamente, una agencia puede pagar con la tarjeta de la empresa para
    varios clientes reales, etc.) — solo marca la cuenta con revisar_multicuenta=True
    para que se pueda repasar a mano desde el Table Editor de Supabase y, si procede,
    ofrecerle de forma proactiva pasarse a un plan de agencia (Starter) en vez de
    penalizar o cortar el servicio.
    Requiere la columna 'tarjeta_fingerprint' en agencias (ver migración adjunta).
    Si la columna no existe todavía, falla en silencio y no rompe el alta.
    """
    if not huella_tarjeta:
        return False
    try:
        coincidencias = supabase.table("agencias") \
            .select("id") \
            .eq("plan", "individual") \
            .eq("tarjeta_fingerprint", huella_tarjeta) \
            .limit(1) \
            .execute()
        return bool(coincidencias.data)
    except Exception:
        return False


def registrar_agencia_de_pago(nombre_agencia, nombre_local, email, password_plano, nombre_usuario, plan, stripe_customer_id=None):
    """
    Igual que registrar_agencia_gratuita, pero para agencias que ya han pagado un plan
    de pago (Starter/Growth) desde la landing. Se llama justo después de que
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
            "logo_url": "https://dummyimage.com/220x60/1a2238/E9C46A&text=Reselia",
            "color_marca": "#2A2C31",
            "plan": plan
        }
        if stripe_customer_id:
            datos_agencia["stripe_customer_id"] = stripe_customer_id

        # Señal de posible multi-cuenta en Individual (no bloqueante — ver las
        # funciones _huella_tarjeta_stripe / _detectar_posible_multicuenta_individual
        # justo arriba). Si algo falla aquí, el alta sigue adelante igualmente.
        datos_agencia_multicuenta = {}
        if plan == "individual":
            try:
                huella = _huella_tarjeta_stripe(stripe_customer_id)
                if huella:
                    datos_agencia_multicuenta["tarjeta_fingerprint"] = huella
                    if _detectar_posible_multicuenta_individual(huella):
                        datos_agencia_multicuenta["revisar_multicuenta"] = True
            except Exception:
                pass

        # Si la migración de las columnas nuevas (tarjeta_fingerprint,
        # revisar_multicuenta) todavía no se ha ejecutado en Supabase, el insert
        # con esos campos fallaría con "column not found" y tumbaría el alta
        # de TODO el mundo en Individual. Por eso se intenta primero con ellos
        # y, si falla, se reintenta sin ellos — la detección de multi-cuenta es
        # una mejora, nunca puede ser un punto de fallo del alta en sí.
        try:
            nueva_agencia = supabase.table("agencias").insert({**datos_agencia, **datos_agencia_multicuenta}).execute()
        except Exception:
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


# =========================================================
# GESTIÓN DE EQUIPO — varios usuarios bajo la misma agencia
# =========================================================
def listar_usuarios_agencia(agencia_id):
    """Devuelve la lista de usuarios (miembros del equipo) de una agencia,
    ordenados por fecha de alta. Solo lectura."""
    try:
        resultado = (
            supabase.table("usuarios")
            .select("id, email, nombre_usuario, rol, activo, creado_en")
            .eq("agencia_id", agencia_id)
            .order("creado_en", desc=False)
            .execute()
        )
        return resultado.data or []
    except Exception:
        # Si 'creado_en' no existiera en alguna instancia antigua, reintenta sin orden.
        try:
            resultado = (
                supabase.table("usuarios")
                .select("id, email, nombre_usuario, rol, activo")
                .eq("agencia_id", agencia_id)
                .execute()
            )
            return resultado.data or []
        except Exception:
            return []


def contar_usuarios_activos_agencia(agencia_id):
    """Cuenta cuántos usuarios activos tiene la agencia (para aplicar el límite del plan)."""
    return len([u for u in listar_usuarios_agencia(agencia_id) if u.get("activo", True)])


def puede_agencia_anadir_usuario(agencia):
    """(bool, motivo) — indica si la agencia puede sumar otro miembro según su plan."""
    plan = agencia.get("plan", "free")
    limite = LIMITE_USUARIOS_POR_PLAN.get(plan)
    if limite is None:
        return True, None
    actuales = contar_usuarios_activos_agencia(agencia["id"])
    if actuales >= limite:
        if plan in ("free", "individual"):
            return False, ("Tu plan actual es de un solo usuario. Cambia a un plan de agencia "
                           "(Starter o Growth) para dar acceso a tu equipo.")
        return False, (f"Has alcanzado el máximo de {limite} usuarios de tu plan {plan.capitalize()}. "
                       "Sube de plan para añadir más miembros.")
    return True, None


def crear_usuario_en_agencia(agencia_id, email, password_plano, nombre_usuario, rol="gestor"):
    """Da de alta un nuevo miembro del equipo bajo una agencia existente.
    Devuelve (True, usuario) o (False, motivo). El email debe ser único en todo el sistema."""
    email_normalizado = email.lower().strip()
    if not EMAIL_REGEX.match(email_normalizado):
        return False, "El email no tiene un formato válido."
    if len(password_plano) < 8:
        return False, "La contraseña debe tener al menos 8 caracteres."
    if rol not in ("admin", "gestor"):
        rol = "gestor"

    try:
        existente = supabase.table("usuarios").select("id").eq("email", email_normalizado).execute()
        if existente.data:
            return False, "Ya existe una cuenta con ese email."

        nuevo = supabase.table("usuarios").insert({
            "agencia_id": agencia_id,
            "email": email_normalizado,
            "password_hash": bcrypt.hashpw(password_plano.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
            "nombre_usuario": nombre_usuario.strip() or email_normalizado.split("@")[0],
            "rol": rol,
        }).execute()
        return True, nuevo.data[0]
    except Exception as e:
        return False, f"No se pudo crear el usuario: {e}"


def desactivar_usuario(usuario_id, agencia_id):
    """Desactiva (no borra) un miembro del equipo, revocándole el acceso.
    Comprueba que pertenezca a la agencia para evitar tocar usuarios ajenos."""
    try:
        objetivo = (
            supabase.table("usuarios")
            .select("id, agencia_id, rol")
            .eq("id", usuario_id)
            .eq("agencia_id", agencia_id)
            .execute()
        )
        if not objetivo.data:
            return False, "Usuario no encontrado en tu agencia."
        supabase.table("usuarios").update({"activo": False}).eq("id", usuario_id).execute()
        return True, None
    except Exception as e:
        return False, f"No se pudo desactivar el usuario: {e}"


def render_formulario_alta_pendiente():
    """
    Pantalla que se muestra justo después de un pago nuevo (cliente sin cuenta previa) ya
    verificado en Stripe. Pide los mismos datos que el alta del plan Free (agencia, local,
    nombre, email, contraseña) y crea la cuenta completa, dejando al usuario ya logueado.
    """
    datos = st.session_state.alta_pendiente
    st.success(f"Pago confirmado — plan **{datos['plan'].capitalize()}**. Un último paso para entrar:")

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
                _crear_token_sesion(resultado["usuario"]["id"])
                st.success(f"¡Cuenta creada! Bienvenido/a, {resultado['usuario']['nombre_usuario']}.")
                st.rerun()
            else:
                st.error(resultado)


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
    Genera el informe PDF de marca blanca. Devuelve los bytes del PDF.

    La maquetación vive en informe_pdf.py; aquí solo se decide QUÉ se le pasa.
    Esta función es la frontera entre lógica de negocio (planes, score, IA) y
    presentación, y se mantiene con la firma de siempre para que el punto de
    llamada no cambie.

    El import es diferido a propósito: informe_pdf arrastra reportlab, que solo
    debe cargarse cuando alguien pulsa de verdad "generar informe". Es la misma
    arquitectura de antes y el motivo de que la app arranque ligera.
    """
    import informe_pdf

    # Marca blanca: en el plan gratuito el informe menciona a Reselia; a partir
    # de Individual el documento lleva únicamente la marca del cliente, que es
    # justo lo que se vende en la landing ("Marca blanca (no incluida)" en el
    # plan Free frente a "Marca blanca completa" en Starter y Growth).
    es_marca_blanca = agencia.get("plan", "free") != "free"

    return informe_pdf.generar_informe_pdf_mensual(
        agencia=agencia,
        historico=historico,
        historico_anterior=historico_anterior,
        locales_agencia=locales_agencia,
        id_a_nombre_usuario=id_a_nombre_usuario,
        contenido_seo_periodo=contenido_seo_periodo,
        periodo_texto=periodo_texto,
        cliente_ia=cliente_ia,
        resultado_score=resultado_score,
        dias_periodo=dias_periodo,
        roi=roi,
        roi_estrellas_actuales=roi_estrellas_actuales,
        roi_estrellas_objetivo=roi_estrellas_objetivo,
        # Se inyectan las funciones de negocio en vez de que informe_pdf
        # importe app.py, lo que crearía una dependencia circular.
        calcular_reputation_score=calcular_reputation_score,
        etiqueta_reputation_score=etiqueta_reputation_score,
        generar_resumen_ejecutivo_ia=generar_resumen_ejecutivo_ia,
        fmt_eur=_fmt_eur,
        pesos_score=PESOS_REPUTATION_SCORE,
        es_marca_blanca=es_marca_blanca,
    )


def generar_qr_png(url_destino):
    """Genera un código QR en PNG (bytes) que apunta a la URL indicada."""
    # Import diferido: qrcode solo hace falta cuando alguien pide de verdad
    # un código, no en cada arranque del proceso.
    import qrcode

    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url_destino)
    qr.make(fit=True)
    imagen = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    imagen.save(buffer, format="PNG")
    return buffer.getvalue()


# =============================================================================
# FICHA DE VERDAD — acceso a datos (motor SEO anclado a hechos)
# =============================================================================

def cargar_ficha_local(local_id):
    """Trae todas las filas de hechos_local de un local. Lista vacía si no hay
    ninguna o si la tabla aún no existe (se degrada sin romper la app)."""
    try:
        r = supabase.table("hechos_local").select("*").eq("local_id", local_id).execute()
        return r.data or []
    except Exception:
        return []


def guardar_hecho_local(local_id, agencia_id, clave, estado,
                        valor=None, evidencia_anio=None, evidencia_entidad=None):
    """Upsert de un hecho de la Ficha (por par local_id+clave)."""
    try:
        supabase.table("hechos_local").upsert({
            "local_id": local_id,
            "agencia_id": agencia_id,
            "clave": clave,
            "estado": estado,
            "valor": (valor or None),
            "evidencia_anio": (evidencia_anio or None),
            "evidencia_entidad": (evidencia_entidad or None),
        }, on_conflict="local_id,clave").execute()
        return True
    except Exception as e:
        log_error_completo("guardar hecho de la Ficha", e)
        return False


def hechos_afirmables_texto(local_id, nicho):
    """Devuelve los datos verificados de un local como texto plano, listo para
    inyectar en blindaje.generar_respuesta. Cadena vacía si no hay ninguno:
    así las respuestas a reseñas se apoyan en hechos reales sin inventar nada."""
    try:
        filas = cargar_ficha_local(local_id)
        if not filas:
            return ""
        ficha = leer_ficha(filas)
        lex = motor_seo.compilar_lexico(ficha, nicho or "general")
        if not lex.afirmables:
            return ""
        return "\n".join(f"- {a}" for a in lex.afirmables)
    except Exception:
        return ""


# =============================================================================
# ASISTENTE DE CRECIMIENTO — soporte de datos
# =============================================================================

def cargar_historico_periodo(local_id, dias):
    """Devuelve (historico_actual, historico_anterior) para un local y ventana de
    días, en el formato que espera calcular_reputation_score. El 'anterior' es la
    ventana inmediatamente previa del mismo tamaño, para poder medir tendencia."""
    ahora = datetime.utcnow()
    ini_actual = (ahora - timedelta(days=dias)).isoformat()
    ini_anterior = (ahora - timedelta(days=dias * 2)).isoformat()
    try:
        actual = supabase.table("historico_respuestas") \
            .select("sentimiento, creado_en") \
            .eq("local_id", local_id) \
            .gte("creado_en", ini_actual) \
            .execute().data or []
        anterior = supabase.table("historico_respuestas") \
            .select("sentimiento, creado_en") \
            .eq("local_id", local_id) \
            .gte("creado_en", ini_anterior) \
            .lt("creado_en", ini_actual) \
            .execute().data or []
        return actual, anterior
    except Exception:
        return [], []


# Tope mensual de mensajes al asistente por local. El plan Individual es de pago,
# pero el asistente encadena varias llamadas al modelo por turno, así que ponemos
# un techo generoso que un autónomo real nunca rozará y que frena que alguien lo
# use como ChatGPT ilimitado. Se cuenta en Supabase (mensajes_asistente), no en
# el navegador, para que no se pueda burlar.
LIMITE_MENSAJES_ASISTENTE_MES = 200


def contar_mensajes_asistente_mes(local_id):
    """Cuántos mensajes ha enviado este local al asistente en el mes en curso."""
    inicio_mes = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        r = supabase.table("mensajes_asistente") \
            .select("id", count="exact") \
            .eq("local_id", local_id) \
            .gte("creado_en", inicio_mes) \
            .execute()
        return r.count or 0
    except Exception:
        # Si la tabla aún no existe o falla, no bloqueamos el uso.
        return 0


def registrar_mensaje_asistente(agencia_id, local_id, usuario_id):
    """Registra un turno de conversación con el asistente (para el tope mensual)."""
    try:
        supabase.table("mensajes_asistente").insert({
            "agencia_id": agencia_id,
            "local_id": local_id,
            "usuario_id": usuario_id,
        }).execute()
    except Exception:
        pass


def construir_ctx_agente(local):
    """Arma el contexto de ejecución que necesitan las herramientas del agente,
    inyectando las funciones y objetos que ya viven en app.py. Así motor_agente
    no depende directamente de la app: solo recibe lo que necesita."""
    return {
        "supabase": supabase,
        "client": client,
        "local": local,
        "calcular_score": calcular_reputation_score,
        "cargar_historico_periodo": cargar_historico_periodo,
        "cargar_ficha_local": cargar_ficha_local,
        "leer_ficha": leer_ficha,
        "compilar_lexico": motor_seo.compilar_lexico,
        "generar_contenido_seo": generar_contenido_seo,
        "SI": SI, "NO": NO, "NO_CONSTA": NO_CONSTA,
    }


def generar_mensaje_whatsapp(nombre_local, enlace_resena):
    """Construye el enlace wa.me con un mensaje precargado para pedir una reseña."""
    mensaje = (
        f"Hola, muchas gracias por confiar en {nombre_local}. "
        f"¿Nos ayudarías dejando tu opinión en Google? Solo te llevará 1 minuto: {enlace_resena}"
    )
    return "https://wa.me/?text=" + urllib.parse.quote(mensaje)


def sugerir_keywords_seo(client, nicho, ciudad=None, nombre_local=None):
    """
    Propone palabras clave de SEO local a partir del nicho y la zona del negocio.

    POR QUÉ ESTO NO ES UNA LISTA DE SINÓNIMOS
    ------------------------------------------
    Lo fácil sería devolver variaciones del nicho ("dentista", "odontólogo",
    "clínica dental"). Eso no sirve de nada: son términos genéricos, con una
    competencia altísima, por los que un negocio local no va a posicionar
    jamás, y además no es lo que la gente escribe realmente en Google.

    Lo que sí funciona en SEO local son las búsquedas con intención concreta,
    que se agrupan en cuatro familias:

      · LOCAL      — el término más la zona ("clínica dental en Chamberí").
                     Es la base: menos volumen, pero convierte muchísimo más.
      · SERVICIO   — lo que la persona quiere hacer, no cómo se llama el
                     negocio ("implantes dentales", "blanqueamiento").
      · PROBLEMA   — cómo lo describe alguien que no conoce la jerga del
                     sector ("me duele una muela", "dentista urgencia"). Aquí
                     está el tráfico que la competencia suele ignorar.
      · CONFIANZA  — la búsqueda de quien ya está decidiendo ("mejor dentista
                     de Madrid", "dentista sin dolor", "opiniones").

    Devolver las keywords agrupadas por familia no es un adorno: permite que
    el usuario elija con criterio en vez de aceptar un bloque a ciegas, y le
    enseña de paso cómo se piensa el SEO local. Ese aprendizaje es parte del
    valor que justifica el precio de la herramienta.

    Devuelve una lista de diccionarios {termino, familia, motivo}. Ante
    cualquier fallo devuelve lista vacía: la funcionalidad es un extra y no
    debe impedir crear el local.
    """
    nicho = (nicho or "").strip()
    if not nicho:
        return []

    zona = (ciudad or "").strip()

    if zona:
        contexto_zona = (
            f"El negocio está en {zona}. Las keywords de la familia LOCAL deben usar "
            f"esta zona de forma natural, tal y como la escribiría alguien de allí: si "
            f"la zona es un barrio, mézclala también con la ciudad en algunas variantes."
        )
    else:
        contexto_zona = (
            "No se conoce la ciudad del negocio. NO inventes ninguna ubicación. "
            "En la familia LOCAL usa marcadores del tipo 'cerca de mí' o deja el "
            "hueco de la zona indicado con [ciudad] para que el usuario lo complete."
        )

    prompt = f"""Eres consultor de SEO local con quince años de experiencia posicionando negocios de barrio en Google. Trabajas para un negocio del sector: {nicho}.

{contexto_zona}

Propón 12 palabras clave repartidas entre estas cuatro familias (3 de cada una):

LOCAL — El servicio más la ubicación. La base del SEO local: poco volumen, altísima conversión.
SERVICIO — Lo que la persona quiere resolver, en sus palabras, no en la jerga del sector.
PROBLEMA — Cómo lo busca alguien que no sabe cómo se llama lo que necesita. Aquí está el tráfico que casi nadie trabaja.
CONFIANZA — Búsquedas de quien ya está comparando y a punto de decidir.

CRITERIOS INNEGOCIABLES
- Escribe las keywords tal y como las teclea una persona real en Google, en minúscula y sin signos de puntuación. La gente no escribe "Clínica Dental Premium en Madrid Centro", escribe "dentista madrid centro".
- Nada de términos de una sola palabra ni genéricos de competencia nacional ("dentista", "abogado"): un negocio local no va a posicionar por ahí en la vida.
- Entre dos y cinco palabras por keyword. Ese es el rango donde vive la intención real.
- Prohibido inventar servicios que este tipo de negocio podría no ofrecer. Quédate en lo que hace con seguridad cualquier negocio de este sector.
- El campo motivo explica en UNA frase corta por qué esa búsqueda merece la pena, dirigida al dueño del negocio, sin jerga de marketing.

Devuelve EXCLUSIVAMENTE este JSON, sin texto alrededor ni bloques de código:

{{"keywords": [{{"termino": "...", "familia": "LOCAL", "motivo": "..."}}]}}"""

    try:
        respuesta = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}],
        )

        bruto = ""
        for bloque in respuesta.content:
            if getattr(bloque, "type", None) == "text":
                bruto = bloque.text.strip()
                break

        if bruto.startswith("```"):
            bruto = re.sub(r"^```(?:json)?|```$", "", bruto).strip()

        datos = json.loads(bruto)

        familias_validas = {"LOCAL", "SERVICIO", "PROBLEMA", "CONFIANZA"}
        limpias = []
        vistas = set()
        for k in datos.get("keywords", []):
            termino = (k.get("termino") or "").strip().lower()
            familia = (k.get("familia") or "").strip().upper()
            # Se descarta lo que no cumple: términos vacíos, de una sola palabra
            # (genéricos inútiles para SEO local), duplicados o de familia
            # desconocida. Vale más devolver ocho keywords buenas que doce con
            # relleno que el usuario tendrá que filtrar a mano.
            if not termino or len(termino.split()) < 2:
                continue
            if termino in vistas or familia not in familias_validas:
                continue
            vistas.add(termino)
            limpias.append({
                "termino": termino,
                "familia": familia,
                "motivo": (k.get("motivo") or "").strip(),
            })
        return limpias

    except Exception:
        return []


def generar_contenido_seo_extra(client, nombre_local, nicho, seo_keywords, tipo_contenido, ciudad=None):
    """
    Genera contenido SEO de alto impacto (posts de Google Business, descripciones de
    servicios, Q&A, ofertas, meta descripciones, descripciones para redes), aplicando
    las mejores prácticas de posicionamiento local de 2026: intención de búsqueda local,
    ubicación explícita, GEO para AI overviews, y ejes de variante distintos para A/B real.

    Devuelve una LISTA de 3 variantes (para test A/B), no una sola cadena. Si algo
    falla, devuelve una lista con un único elemento de reserva para no romper la UI.
    """
    keywords_texto = ", ".join(seo_keywords) if seo_keywords else "sin keywords específicas cargadas"
    zona = (ciudad or "").strip()
    zona_o_generico = zona or "tu zona"
    referencia_local = f"{nombre_local} en {zona}" if zona else nombre_local
    zona_hashtag = zona.replace(" ", "").replace(",", "") if zona else "local"

    contexto_zona = (
        f"El negocio está en {zona}. Ancla siempre el contenido a esta ubicación: "
        f"la gente busca '{nicho} en {zona}', 'mejor {nicho} cerca de mí', '{nicho} {zona}'. "
        f"Úsalo de forma natural, no como etiqueta pegada al final."
        if zona else
        "No se ha especificado la ciudad/zona. Refuerza igualmente la intención local con el "
        "tipo de negocio, pero NUNCA inventes un nombre de ciudad concreto."
    )

    # ── INSTRUCCIONES POR TIPO ─────────────────────────────────────────────────
    # Cada tipo tiene su propia norma de longitud, estructura y ejes de variante.
    # Los ejes de variante son explícitos para que las 3 salidas difieran de verdad
    # en ángulo, no solo en orden de palabras — eso es lo que hace útil el A/B.
    instrucciones_por_tipo = {

        "Publicación de Google Business": (
            f"Escribe una publicación de novedades (What's New) de 45-70 palabras para Google Business Profile de {nombre_local}.\n"
            "REQUISITO CRÍTICO: cada variante debe girar en torno a UN DATO FRESCO Y CONCRETO y verificable "
            "(una novedad real, un servicio específico, un detalle operativo, una temporada concreta). "
            "Sin datos concretos no hay publicación — una frase genérica que cualquier negocio del mismo nicho "
            "pudiera firmar es un FRACASO. Ancla siempre a la búsqueda local. Cierra con UNA acción concreta.\n"
            "EJES DE VARIANTE (cada variante debe usar un eje distinto, en este orden):\n"
            f"  Variante 1 — eje NOVEDAD: destaca algo nuevo o reciente en {nombre_local} (tecnología, horario, producto, servicio).\n"
            f"  Variante 2 — eje RESULTADO: arranca desde el beneficio que obtiene el cliente, no desde el negocio.\n"
            f"  Variante 3 — eje DIFERENCIADOR: lo que {nombre_local} hace distinto a cualquier otro {nicho} en {zona_o_generico}."
        ),

        "Descripción de servicio/producto": (
            f"Escribe una descripción de 50-80 palabras para la pestaña de Servicios/Productos de Google Business Profile de {nombre_local}.\n"
            "Esta sección alimenta directamente las AI overviews de Google en 2026: escribe frases autocontenidas, "
            "concretas y verificables — Google las usa como fragmentos de respuesta cuando alguien pregunta a la IA. "
            "Prohibido adjetivos vacíos ('excelente', 'profesional', 'de calidad'): sustituye cada uno por un dato real.\n"
            "EJES DE VARIANTE:\n"
            f"  Variante 1 — eje QUÉ ES + PARA QUIÉN: describe el servicio y el perfil exacto de cliente que lo necesita.\n"
            f"  Variante 2 — eje PROCESO: explica cómo funciona paso a paso de forma breve, transmitiendo confianza técnica.\n"
            f"  Variante 3 — eje RESULTADO MEDIBLE: arranca desde el resultado concreto que obtiene el cliente."
        ),

        "Pregunta y respuesta (Q&A)": (
            f"Genera 3 bloques Q&A independientes para la sección de Preguntas y Respuestas de Google Business Profile de {nombre_local}.\n"
            "El Q&A es el tipo de contenido con mejor ratio impacto/esfuerzo de toda la ficha: Google lo indexa "
            "y lo usa masivamente para AI overviews. La clave es elegir preguntas de INTENCIÓN ALTA — las que hace "
            "alguien que está a punto de decidir, no alguien que acaba de descubrir el negocio.\n"
            "REGLAS para cada bloque:\n"
            "  - La pregunta debe sonar exactamente como la escribiría un cliente real (informal, directa, con la duda concreta).\n"
            "  - La respuesta: 35-55 palabras, autocontenida (que se entienda sin contexto previo), con el nombre del negocio "
            f"y la zona integrados de forma natural, y al menos 1 keyword de: {keywords_texto}.\n"
            "  - Prohibido respuestas vagas ('depende', 'consúltenos', 'estamos para ayudarte'): si la respuesta no da "
            "información concreta y útil, no sirve para AI overviews ni para el cliente.\n"
            "EJES DE VARIANTE (una pregunta por eje):\n"
            f"  Q&A 1 — LOGÍSTICA: duda práctica antes de la visita (aparcamiento, reserva, horario, acceso, precio orientativo).\n"
            f"  Q&A 2 — SERVICIO ESPECÍFICO: duda sobre un tratamiento, producto o servicio concreto del {nicho}.\n"
            f"  Q&A 3 — CONFIANZA: duda sobre garantías, experiencia del equipo, qué pasa si algo no sale bien.\n"
            "Formato de cada bloque: 'P: ...' en una línea y 'R: ...' en la siguiente. Separa los 3 bloques con una línea en blanco."
        ),

        "Oferta / promoción": (
            f"Escribe una publicación de tipo Oferta de 40-60 palabras para Google Business Profile de {nombre_local}.\n"
            "Una oferta que no especifica QUÉ se ofrece, A QUIÉN y POR QUÉ AHORA es publicidad genérica, no una oferta. "
            "El cliente debe entender el beneficio concreto en las primeras 10 palabras. "
            "Prohibido urgencia falsa ('solo hoy', 'últimas plazas' sin base real) y frases intercambiables "
            "('no te lo pierdas', 'aprovecha ahora', 'ven a disfrutar').\n"
            "EJES DE VARIANTE:\n"
            f"  Variante 1 — eje PRIMERA VEZ: orientada a captar clientes nuevos que aún no conocen {nombre_local}.\n"
            f"  Variante 2 — eje TEMPORADA/MOMENTO: anclada a un motivo real y concreto (época del año, vuelta al cole, verano, etc.).\n"
            f"  Variante 3 — eje COMBO/PACK: presenta una combinación de servicios o productos con valor percibido alto."
        ),

        "Descripción para redes sociales": (
            f"Escribe una descripción de 30-50 palabras para el pie de una publicación de Instagram o Facebook de {nombre_local}.\n"
            "REGLA CRÍTICA (la causa de que el 90% de estos textos fracasen): cada variante debe contener "
            "AL MENOS UN DETALLE CONCRETO Y ESPECÍFICO de este negocio que ningún otro {nicho} en {zona_o_generico} "
            "pudiera copiar sin que sonara falso. Un dato real, un proceso propio, un resultado específico, "
            "una característica distintiva. Sin ese detalle, el texto es intercambiable y no construye marca.\n"
            "Tono: cercano, con personalidad, como si lo escribiera el propio dueño. "
            "Máximo 3 hashtags al final: uno de nicho, uno de zona, uno de marca o servicio específico.\n"
            "EJES DE VARIANTE:\n"
            f"  Variante 1 — eje BACKSTAGE: muestra algo del proceso interno, del equipo o del día a día de {nombre_local}.\n"
            f"  Variante 2 — eje CLIENTE: arranca desde la experiencia o el resultado del cliente, no desde el negocio.\n"
            f"  Variante 3 — eje DATO SORPRENDENTE: un número, un hecho o un contraste inesperado y real sobre {nombre_local} o el {nicho}."
        ),

        "Meta descripción SEO": (
            f"Escribe una meta descripción SEO para la web de {nombre_local}.\n"
            "LÍMITE ABSOLUTO: máximo 155 caracteres por variante (cuenta los caracteres tú mismo antes de devolver).\n"
            "Estructura óptima probada: [keyword de intención local lo antes posible] + [propuesta de valor concreta] + [CTA breve].\n"
            "La keyword de intención local debe aparecer en las primeras 60 caracteres — Google la muestra en negrita "
            "en los resultados y aumenta el CTR. Prohibido adjetivos sin respaldo ('los mejores', 'expertos en') "
            "salvo que vayas a anclarlos a algo concreto.\n"
            "EJES DE VARIANTE:\n"
            f"  Variante 1 — eje PROBLEMA-SOLUCIÓN: arranca desde el problema del cliente y {nombre_local} como solución.\n"
            f"  Variante 2 — eje DIFERENCIADOR: qué hace a {nombre_local} distinto de otros {nicho} en {zona_o_generico}.\n"
            f"  Variante 3 — eje PRUEBA SOCIAL: integra un indicador de confianza real (años, pacientes, valoraciones) si encaja."
        ),
    }

    instruccion = instrucciones_por_tipo.get(tipo_contenido, instrucciones_por_tipo["Publicación de Google Business"])
    instruccion = instruccion.replace("{nicho}", nicho).replace("{zona}", zona_o_generico).replace("{zona_hashtag}", zona_hashtag)

    system_prompt = f"""Eres un especialista en SEO local y Generative Engine Optimization (GEO) que escribe con la voz auténtica del negocio "{referencia_local}" (nicho: "{nicho}"). No eres una agencia externa: suenas al propio negocio hablando de sí mismo.

CONTEXTO DE UBICACIÓN: {contexto_zona}

KEYWORDS SEO disponibles — integra 1-2 SOLO si encajan con naturalidad (el keyword-stuffing penaliza en 2026): {keywords_texto}.

NORMAS ANTI-PLANTILLA (válidas para TODOS los tipos de contenido):
- PROHIBIDAS estas frases y cualquier variación suya, están quemadas de tanto verlas: "tecnología de última generación", "equipo de profesionales", "atención personalizada", "calidad garantizada", "nos preocupamos por ti", "tu satisfacción es lo primero", "no te lo pierdas", "descúbrelo ya", "ven a disfrutar", "somos tu mejor opción".
- Si necesitas expresar algo parecido, dilo con un DATO CONCRETO que lo demuestre, no con el adjetivo vacío.
- Un texto que cualquier otro {nicho} en España pudiera publicar sin cambiar una sola palabra es un FRACASO.

PRINCIPIOS GEO PARA AI OVERVIEWS (Google usa este contenido como respuesta directa):
- Frases autocontenidas: cada oración debe tener sentido sin contexto previo.
- Datos verificables y específicos: nombres reales, cifras, procesos concretos.
- Intención de búsqueda natural: integra cómo busca de verdad la gente, no cómo habla un copywriter.

TAREA:
{instruccion}

Devuelve tu respuesta EXCLUSIVAMENTE como un array JSON de 3 strings, sin texto antes ni después, sin markdown, sin comillas triples.
Formato exacto: ["variante 1", "variante 2", "variante 3"]"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=system_prompt,
            messages=[{"role": "user", "content": f"Genera las 3 variantes para {referencia_local}."}]
        )
        texto_bruto = ""
        for bloque in response.content:
            if getattr(bloque, "type", None) == "text":
                texto_bruto += bloque.text
        texto_bruto = texto_bruto.strip()

        # Limpiar posibles vallas de código y parsear el JSON.
        limpio = texto_bruto.replace("```json", "").replace("```", "").strip()
        try:
            variantes = json.loads(limpio)
            if isinstance(variantes, list) and variantes:
                return [str(v).strip() for v in variantes if str(v).strip()]
        except (json.JSONDecodeError, ValueError):
            pass

        # Si no vino como JSON válido, devolvemos el texto tal cual como única variante.
        if texto_bruto:
            return [texto_bruto]
        return ["No se pudo generar el contenido. Inténtalo de nuevo."]
    except Exception:
        return ["Hubo un problema al generar el contenido. Inténtalo de nuevo en un momento."]


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
    #
    # "free" son los valores BASE, fuera de la ventana de beta de cada agencia.
    # Mientras una agencia esté dentro de su ventana (agencia_en_beta), verificar_velocidad
    # usa en su lugar el mismo margen que el plan Individual (30/h, 150/día) — de sobra
    # para una demo o un negocio real, pero sigue frenando un abuso masivo.
    "free":       (10, 10),
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
    if agencia_en_beta(agencia):
        # Dentro de su ventana de beta: mismo margen generoso que el plan Individual,
        # en vez del límite base y estricto del plan Free.
        limite_hora, limite_dia = (30, 150)
    else:
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


def boton_enlace_stripe(texto, url, key=None):
    """
    Renderiza un botón-enlace a Stripe como un ANCLA HTML pura, con los colores
    puestos en línea (inline styles).

    Por qué así y no con st.link_button + CSS:
    El color del texto de st.link_button(type="primary") lo controla Streamlit
    con reglas de especificidad alta y con nombres de data-testid que han ido
    cambiando entre versiones; forzarlo por CSS externo es frágil y en algunas
    versiones simplemente no engancha (el texto sale oscuro e ilegible sobre el
    fondo índigo). Aquí el <a> es nuestro: el blanco va en el propio style del
    elemento, así que se ve bien en cualquier versión de Streamlit, sin depender
    de ningún data-testid ni de que el CSS alcance al botón.

    key: se ignora (existe solo por compatibilidad con las llamadas antiguas).
    """
    import html as _html
    texto_seguro = _html.escape(texto)
    url_segura = _html.escape(url, quote=True)
    st.markdown(
        f"""
        <a href="{url_segura}" target="_blank" rel="noopener noreferrer"
           style="display:block; width:100%; box-sizing:border-box;
                  background-color:{ACCENT_INDIGO}; color:#FFFFFF !important;
                  -webkit-text-fill-color:#FFFFFF; text-align:center;
                  padding:0.6rem 1rem; border-radius:0.5rem; font-weight:600;
                  text-decoration:none; border:1px solid {ACCENT_INDIGO};
                  transition:background-color .15s ease;"
           onmouseover="this.style.backgroundColor='{ACCENT_INDIGO_HOVER}';this.style.borderColor='{ACCENT_INDIGO_HOVER}';"
           onmouseout="this.style.backgroundColor='{ACCENT_INDIGO}';this.style.borderColor='{ACCENT_INDIGO}';">
            {texto_seguro}
        </a>
        """,
        unsafe_allow_html=True,
    )


def redirigir_a_stripe(url_pago):
    """
    Lleva al usuario a la pasarela de pago de Stripe.

    Importante: NO se puede hacer una redirección 100% automática desde aquí.
    Streamlit elimina las etiquetas <script> inyectadas con st.markdown (aunque
    se use unsafe_allow_html=True), así que un salto por JavaScript nunca se
    ejecuta; y el contenido corre dentro de un iframe cuyo sandbox bloquea la
    navegación del top window sin un clic real del usuario. Por eso se usa
    st.link_button, que genera un ancla nativa y navega de forma fiable a Stripe
    al pulsarlo, escapando correctamente del iframe.
    """
    st.success("Sesión de pago creada correctamente.")
    boton_enlace_stripe("Continuar al pago seguro con Stripe →", url_pago)
    st.caption("Pasarela cifrada de Stripe. Pulsa el botón para completar la contratación.")


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
        st.caption(f"Pagando por año te ahorras un {int(DESCUENTO_ANUAL*100)}%.")

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
                    st.caption(f"— {feature}")
                if es_plan_actual:
                    st.success("Tu plan actual")
                elif MODO_BETA_SIN_PAGOS:
                    st.button(
                        "No disponible en beta",
                        key=f"elegir_{clave_plan}",
                        use_container_width=True,
                        disabled=True,
                    )
                elif st.button(f"Elegir {datos_plan['nombre']}", key=f"elegir_{clave_plan}", use_container_width=True, type="primary"):
                    price_id = datos_plan["price_ids"]["anual" if es_anual_up else "mensual"]
                    url_pago = crear_sesion_pago_stripe(agencia["id"], clave_plan, price_id)
                    if url_pago:
                        redirigir_a_stripe(url_pago)


def cargar_perfil_login(email, password_plano, nombre_usuario=None):
    """
    Resuelve el login y devuelve (perfil, error).

    POR QUÉ RECIBE LA CONTRASEÑA
    ----------------------------
    El esquema permite VARIOS usuarios con el mismo email (se quitó
    usuarios_email_key a propósito, para que una empresa pueda tener varios
    perfiles con su correo corporativo). La versión anterior de esta función
    buscaba por email y cogía data[0], el primero que devolviera Postgres.

    Con correos genéricos —info@, hola@, gerencia@, que son justo los que usan
    las agencias— eso provocaba dos cosas: que el segundo usuario registrado
    no pudiera entrar nunca (su contraseña se comparaba contra el hash del
    primero), y que si ambos coincidían en contraseña, entrara en la agencia
    ajena y viera sus locales, su histórico y sus clientes.

    Por eso ahora la contraseña se prueba contra TODOS los candidatos: es la
    única forma de saber cuál de ellos es. Y si encaja más de uno, no se
    adivina —adivinar ahí es exactamente lo que causaba la fuga—, se pide el
    nombre de usuario.
    """
    candidatos = (
        supabase.table("usuarios")
        .select("*")
        .eq("email", email)
        .eq("activo", True)
        .execute()
    )

    if not candidatos.data:
        # Comparación falsa contra un hash real para que el tiempo de respuesta
        # sea parecido exista o no el email. Sin esto, cronometrando el login
        # se puede averiguar qué correos están dados de alta.
        try:
            bcrypt.checkpw(
                b"x",
                b"$2b$12$abcdefghijklmnopqrstuuMFPRr/6H1MTmDkQZ0oCQMDIQeGmnHi"
            )
        except Exception:
            pass
        return None, "Email o contraseña incorrectos."

    filas = candidatos.data
    if nombre_usuario:
        filtradas = [
            u for u in filas
            if (u.get("nombre_usuario") or "").strip().lower() == nombre_usuario.strip().lower()
        ]
        if filtradas:
            filas = filtradas

    coincidencias = []
    for u in filas:
        try:
            if verificar_password(password_plano, u.get("password_hash") or ""):
                coincidencias.append(u)
        except Exception:
            continue

    if not coincidencias:
        return None, "Email o contraseña incorrectos."

    if len(coincidencias) > 1:
        nombres = ", ".join(sorted(u["nombre_usuario"] for u in coincidencias))
        return None, (
            "Hay varias cuentas con ese email y esa contraseña. "
            f"Escribe también tu nombre de usuario en el campo de abajo ({nombres})."
        )

    usuario = coincidencias[0]

    resultado_agencia = supabase.table("agencias").select("*").eq("id", usuario["agencia_id"]).execute()
    if not resultado_agencia.data:
        return None, "La agencia asociada a este usuario no existe."

    resultado_locales = supabase.table("locales").select("*").eq("agencia_id", usuario["agencia_id"]).execute()

    return {
        "usuario": usuario,
        "agencia": resultado_agencia.data[0],
        "locales": resultado_locales.data or []
    }, None


# =========================================================================
# FRENO DE FUERZA BRUTA Y CADUCIDAD DE SESIÓN
# =========================================================================
INTENTOS_ANTES_DE_ESPERAR = 5
ESPERA_BASE_SEGUNDOS = 30
CADUCIDAD_SESION_SEGUNDOS = 8 * 60 * 60      # una jornada
REFRESCO_CONTEXTO_SEGUNDOS = 300             # 5 minutos


def comprobar_freno_login():
    """Devuelve (permitido, segundos_restantes). Llamar ANTES de validar."""
    ahora = time.time()
    bloqueado_hasta = st.session_state.get("_login_bloqueado_hasta", 0)
    if ahora < bloqueado_hasta:
        return False, int(bloqueado_hasta - ahora)

    fallos = st.session_state.get("_fallos_login", 0)
    if fallos >= INTENTOS_ANTES_DE_ESPERAR:
        espera = min(ESPERA_BASE_SEGUNDOS * (2 ** (fallos - INTENTOS_ANTES_DE_ESPERAR)), 900)
        st.session_state["_login_bloqueado_hasta"] = ahora + espera
        return False, int(espera)

    return True, 0


def registrar_fallo_login():
    st.session_state["_fallos_login"] = st.session_state.get("_fallos_login", 0) + 1


def limpiar_fallos_login():
    st.session_state["_fallos_login"] = 0
    st.session_state["_login_bloqueado_hasta"] = 0


def marcar_actividad():
    st.session_state["_ultima_actividad"] = time.time()


# =========================================================
# SESIÓN PERSISTENTE ENTRE RECARGAS
# =========================================================
#
# EL PROBLEMA
# -----------
# st.session_state vive en memoria del servidor, atado a la conexión
# WebSocket del navegador. Cuando esa conexión se corta y se abre otra —un
# F5, un redeploy en Render, o simplemente el navegador reconectando tras
# unos segundos dormido— Streamlit crea una sesión nueva y session_state
# empieza vacío. El usuario ve la pantalla de login aunque no haya pasado
# ni un minuto.
#
# LA SOLUCIÓN
# -----------
# Un token opaco en la URL (?s=...) que apunta a una fila en la tabla
# `sesiones_persistentes` de Supabase. Al arrancar el script, si
# session_state no tiene sesión pero la URL trae un token válido y no
# caducado, se reconstruye la sesión desde la base de datos ANTES de
# pintar el login. Es exactamente lo mismo que hace una cookie de sesión
# en cualquier web normal — aquí se implementa a mano porque Streamlit no
# trae cookies de sesión propias.
#
# POR QUÉ NO ALTERA LA LÓGICA YA EXISTENTE
# -----------------------------------------
# sesion_valida() sigue cerrando por inactividad exactamente igual que
# antes (CADUCIDAD_SESION_SEGUNDOS). refrescar_contexto_si_toca() sigue
# revisando bajas de usuario exactamente igual que antes. Esto solo
# resuelve DÓNDE vive la prueba de que alguien inició sesión: antes solo
# en la memoria RAM de esa conexión concreta, ahora también en la base de
# datos, con la MISMA fecha de caducidad. Un token nunca dura más que lo
# que ya duraba la sesión.
# =========================================================

TABLA_SESIONES_PERSISTENTES = "sesiones_persistentes"


def _crear_token_sesion(usuario_id: str) -> str:
    """Genera un token opaco, lo guarda en BD y lo deja en la URL."""
    token = _secrets_modulo.token_urlsafe(32)
    expira_en = (datetime.utcnow() + timedelta(seconds=CADUCIDAD_SESION_SEGUNDOS)).isoformat()
    try:
        supabase.table(TABLA_SESIONES_PERSISTENTES).insert({
            "token": token,
            "usuario_id": usuario_id,
            "expira_en": expira_en,
        }).execute()
        st.session_state["_token_sesion"] = token
        st.query_params["s"] = token
    except Exception:
        # Si la tabla no existe todavía (SQL no ejecutado) o falla la
        # escritura, la sesión sigue funcionando igual que antes: solo en
        # memoria, sin sobrevivir a una recarga. No debe romper el login.
        pass
    return token


def _restaurar_sesion_desde_token():
    """
    Se llama una vez, al principio del script, antes de decidir si se
    enseña el login o el panel. Si hay un token válido en la URL y
    session_state está vacío, reconstruye la sesión desde la BD.
    """
    if st.session_state.get("sesion_activa"):
        return  # ya hay sesión en memoria, no hace falta tocar nada

    token = st.query_params.get("s")
    if not token:
        return

    try:
        fila = (
            supabase.table(TABLA_SESIONES_PERSISTENTES)
            .select("usuario_id, expira_en")
            .eq("token", token)
            .execute()
        )
    except Exception:
        return  # tabla ausente o fallo de red: se cae al login, sin romper nada

    if not fila.data:
        st.query_params.pop("s", None)
        return

    fila_token = fila.data[0]
    expira = fila_token.get("expira_en")
    if expira and datetime.fromisoformat(expira.replace("Z", "+00:00")).replace(tzinfo=None) < datetime.utcnow():
        # Token caducado: se borra y se manda al login, igual que ya
        # pasaba con la caducidad por inactividad.
        try:
            supabase.table(TABLA_SESIONES_PERSISTENTES).delete().eq("token", token).execute()
        except Exception:
            pass
        st.query_params.pop("s", None)
        return

    try:
        u = supabase.table("usuarios").select("*").eq("id", fila_token["usuario_id"]).execute()
    except Exception:
        return

    if not u.data or u.data[0].get("activo") is False:
        st.query_params.pop("s", None)
        return

    usuario = u.data[0]

    try:
        a = supabase.table("agencias").select("*").eq("id", usuario["agencia_id"]).execute()
        l = supabase.table("locales").select("*").eq("agencia_id", usuario["agencia_id"]).execute()
    except Exception:
        return

    if not a.data:
        return

    # Evidencia suficiente para restaurar: token válido, usuario activo,
    # agencia encontrada. Se reconstruye la sesión igual que hace el login.
    st.session_state.sesion_activa = True
    st.session_state.usuario_actual = usuario
    st.session_state.agencia_actual = a.data[0]
    st.session_state.locales_agencia = l.data or []
    st.session_state["_token_sesion"] = token
    marcar_actividad()


def _revocar_token_sesion():
    """Borra el token de la BD y de la URL. Se llama al cerrar sesión."""
    token = st.session_state.pop("_token_sesion", None)
    if token:
        try:
            supabase.table(TABLA_SESIONES_PERSISTENTES).delete().eq("token", token).execute()
        except Exception:
            pass
    st.query_params.pop("s", None)


def _cerrar_sesion_local():
    _revocar_token_sesion()
    for clave in ("sesion_activa", "usuario_actual", "agencia_actual", "locales_agencia"):
        st.session_state.pop(clave, None)


# =========================================================
# 🔑 RECUPERACIÓN DE CONTRASEÑA
# =========================================================
#
# POR QUÉ HACE FALTA
# ------------------
# Hasta ahora la única salida para alguien que olvidaba su contraseña era
# escribir a soporte y que alguien entrase a mano en Supabase a reescribir un
# password_hash. Con altas de autoservicio, esa incidencia llega el primer día.
#
# CÓMO FUNCIONA
# -------------
# Mismo mecanismo que el token de sesión, con tres diferencias que importan:
#   · caduca en 30 minutos, no en 8 horas;
#   · es de UN SOLO USO (se marca 'usado' en cuanto se consume);
#   · en la base de datos se guarda el SHA-256 del token, nunca el token.
#
# Esa última es la que evita que la tabla se convierta en un llavero: si
# alguien consigue leerla, tiene hashes, no credenciales.
#
# QUÉ NECESITA EN SUPABASE (ejecutar una vez)
# -------------------------------------------
#   create table resets_password (
#     id          uuid primary key default gen_random_uuid(),
#     token_hash  text not null unique,
#     usuario_id  uuid not null references usuarios(id) on delete cascade,
#     expira_en   timestamptz not null,
#     usado       boolean not null default false,
#     creado_en   timestamptz not null default now()
#   );
#   create index on resets_password (token_hash);
#
# SOBRE EL ENVÍO DEL EMAIL
# ------------------------
# La app no tenía ningún proveedor de correo configurado, así que el envío es
# OPCIONAL y por SMTP estándar (secrets SMTP_HOST, SMTP_USER, SMTP_PASSWORD,
# SMTP_REMITENTE). Si no están configurados, el enlace se escribe en los logs
# del servidor: la recuperación sigue funcionando, pero pasando por ti. Es
# deliberado — es mejor un flujo completo con el último tramo manual que
# seguir sin flujo, y el día que añadas Resend/SendGrid solo cambia
# _enviar_email_reset().
# =========================================================

TABLA_RESETS_PASSWORD = "resets_password"
CADUCIDAD_RESET_SEGUNDOS = 30 * 60


def _hash_token(token):
    """SHA-256 del token. Lo que se guarda en la base de datos.

    No lleva bcrypt a propósito: un token de 32 bytes aleatorios no es
    adivinable por fuerza bruta, así que no necesita un hash lento, y aquí
    la latencia sí importa (se consulta en cada carga de la pantalla).
    """
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _enviar_email_reset(destinatario, enlace):
    """Envía el enlace de recuperación. Devuelve True si salió de verdad.

    Si no hay SMTP configurado, deja el enlace en stderr (visible en los logs
    de Render) y devuelve False, para que la interfaz pueda decir la verdad al
    usuario en vez de prometer un correo que nunca va a llegar.
    """
    host = st.secrets.get("SMTP_HOST")
    usuario_smtp = st.secrets.get("SMTP_USER")
    clave_smtp = st.secrets.get("SMTP_PASSWORD")
    remitente = st.secrets.get("SMTP_REMITENTE") or usuario_smtp

    if not (host and usuario_smtp and clave_smtp and remitente):
        print(
            f"[RESET SIN SMTP] Enlace de recuperación para {destinatario}: {enlace}",
            file=sys.stderr,
        )
        return False

    try:
        import smtplib
        from email.message import EmailMessage

        mensaje = EmailMessage()
        mensaje["Subject"] = "Restablecer tu contraseña de Reselia"
        mensaje["From"] = remitente
        mensaje["To"] = destinatario
        mensaje.set_content(
            "Has pedido restablecer tu contraseña de Reselia.\n\n"
            f"Abre este enlace para elegir una nueva:\n{enlace}\n\n"
            "El enlace caduca en 30 minutos y solo se puede usar una vez.\n"
            "Si no has sido tú, puedes ignorar este mensaje: tu contraseña "
            "actual sigue siendo válida.\n"
        )

        puerto = int(st.secrets.get("SMTP_PUERTO") or 587)
        with smtplib.SMTP(host, puerto, timeout=15) as servidor:
            servidor.starttls()
            servidor.login(usuario_smtp, clave_smtp)
            servidor.send_message(mensaje)
        return True
    except Exception as e:
        log_error_completo("envío de email de recuperación", e)
        return False


def solicitar_reset_password(email):
    """
    Crea un token de recuperación para ese email y lo envía.

    Devuelve (enviado_por_email, None) o (False, motivo_tecnico).

    IMPORTANTE: quien llama a esto NUNCA debe cambiar el mensaje que enseña en
    función de si el email existía o no. Si dijéramos "ese correo no está
    registrado", habríamos construido un enumerador de cuentas gratuito. Se
    responde siempre lo mismo, exista o no.
    """
    email_normalizado = (email or "").lower().strip()
    if not EMAIL_REGEX.match(email_normalizado):
        return False, "formato"

    try:
        candidatos = (
            supabase.table("usuarios")
            .select("id, email")
            .eq("email", email_normalizado)
            .eq("activo", True)
            .execute()
        )
    except Exception as e:
        return False, log_error_completo("búsqueda de usuario para reset", e)

    if not candidatos.data:
        # No existe: no se crea nada, pero se devuelve como si todo hubiera ido
        # bien para que la pantalla no delate la diferencia.
        return False, None

    token = _secrets_modulo.token_urlsafe(32)
    expira_en = (datetime.utcnow() + timedelta(seconds=CADUCIDAD_RESET_SEGUNDOS)).isoformat()

    try:
        # Un solo enlace activo por persona: se invalidan los anteriores para
        # que pedir el correo dos veces no deje dos llaves circulando.
        for fila_usuario in candidatos.data:
            supabase.table(TABLA_RESETS_PASSWORD) \
                .update({"usado": True}) \
                .eq("usuario_id", fila_usuario["id"]) \
                .eq("usado", False) \
                .execute()

        supabase.table(TABLA_RESETS_PASSWORD).insert({
            "token_hash": _hash_token(token),
            "usuario_id": candidatos.data[0]["id"],
            "expira_en": expira_en,
        }).execute()
    except Exception as e:
        return False, log_error_completo("creación de token de reset", e)

    enlace = f"{APP_URL}/?r={token}"
    return _enviar_email_reset(email_normalizado, enlace), None


def validar_token_reset(token):
    """Devuelve (usuario_id, None) si el token sirve, o (None, motivo)."""
    if not token:
        return None, "Enlace incompleto."

    try:
        fila = (
            supabase.table(TABLA_RESETS_PASSWORD)
            .select("id, usuario_id, expira_en, usado")
            .eq("token_hash", _hash_token(token))
            .execute()
        )
    except Exception:
        return None, "No se ha podido comprobar el enlace. Inténtalo de nuevo en un momento."

    if not fila.data:
        return None, "Este enlace no es válido. Pide uno nuevo desde la pantalla de acceso."

    registro = fila.data[0]

    if registro.get("usado"):
        return None, "Este enlace ya se usó. Pide uno nuevo desde la pantalla de acceso."

    expira = registro.get("expira_en")
    if expira:
        try:
            caducado = datetime.fromisoformat(
                expira.replace("Z", "+00:00")
            ).replace(tzinfo=None) < datetime.utcnow()
        except (ValueError, AttributeError):
            caducado = True
        if caducado:
            return None, "Este enlace ha caducado (duran 30 minutos). Pide uno nuevo."

    return registro["usuario_id"], None


def consumar_reset_password(token, password_nueva):
    """
    Cambia la contraseña y quema el token. Devuelve (True, None) o (False, motivo).

    El token se revalida aquí aunque ya se validara al pintar el formulario:
    entre una cosa y otra pueden pasar minutos, y la comprobación que cuenta es
    la del momento de escribir en la base de datos.
    """
    if len(password_nueva or "") < 8:
        return False, "La contraseña debe tener al menos 8 caracteres."

    usuario_id, motivo = validar_token_reset(token)
    if not usuario_id:
        return False, motivo

    try:
        nuevo_hash = bcrypt.hashpw(
            password_nueva.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

        supabase.table("usuarios") \
            .update({"password_hash": nuevo_hash}) \
            .eq("id", usuario_id) \
            .execute()

        supabase.table(TABLA_RESETS_PASSWORD) \
            .update({"usado": True}) \
            .eq("token_hash", _hash_token(token)) \
            .execute()

        # Cambiar la contraseña tiene que echar de todas partes: si alguien
        # entró con la contraseña vieja, su sesión persistente moriría aquí.
        # Es justo el motivo por el que la gente resetea.
        supabase.table(TABLA_SESIONES_PERSISTENTES) \
            .delete().eq("usuario_id", usuario_id).execute()
    except Exception as e:
        return False, log_error_completo("cambio de contraseña por reset", e)

    return True, None


def render_pantalla_reset(token):
    """Pantalla de 'elige una contraseña nueva', a la que se llega por ?r=token."""
    _izq_r, _centro_r, _der_r = st.columns([1, 1.15, 1])

    with _centro_r:
        st.markdown(
            """
            <div class="rs-login-cab">
              <div class="rs-login-marca">RESELIA</div>
              <h1 class="rs-login-titulo">Elige una contraseña nueva</h1>
            </div>
            """,
            unsafe_allow_html=True,
        )

        usuario_id, motivo = validar_token_reset(token)
        if not usuario_id:
            st.error(motivo)
            if st.button("Volver al acceso", use_container_width=True):
                st.query_params.pop("r", None)
                st.session_state.vista_landing = "login"
                st.rerun()
            return

        with st.form("form_reset_password", border=False):
            nueva = st.text_input("Contraseña nueva", type="password")
            repetida = st.text_input("Repítela", type="password")
            enviado = st.form_submit_button(
                "Guardar contraseña", use_container_width=True, type="primary"
            )

        if enviado:
            if nueva != repetida:
                st.error("Las dos contraseñas no coinciden.")
            else:
                ok, motivo_error = consumar_reset_password(token, nueva)
                if ok:
                    st.query_params.pop("r", None)
                    st.session_state.vista_landing = "login"
                    st.session_state["_reset_completado"] = True
                    st.rerun()
                else:
                    st.error(motivo_error)


def sesion_valida():
    """False si la sesión caducó por inactividad. Si caduca, la cierra."""
    ultima = st.session_state.get("_ultima_actividad")
    if ultima is None:
        marcar_actividad()
        return True
    if time.time() - ultima > CADUCIDAD_SESION_SEGUNDOS:
        _cerrar_sesion_local()
        return False
    marcar_actividad()
    return True


def refrescar_contexto_si_toca():
    """
    Recarga agencia y usuario desde la base de datos cada pocos minutos.

    Sin esto, si el webhook de Stripe baja el plan por un impago, o un admin
    desactiva a alguien, la sesión abierta conserva los permisos viejos hasta
    que esa persona cierre sesión. Pueden ser días con un plan que ya no paga.

    REGLA DE ORO DE ESTA FUNCIÓN
    ----------------------------
    Solo cierra la sesión con EVIDENCIA POSITIVA de que el usuario está dado
    de baja: es decir, cuando la consulta devuelve una fila y esa fila dice
    activo = False.

    La versión anterior hacía `if not u.data: cerrar_sesion()`, y eso estaba
    mal: una consulta vacía no significa "este usuario ya no existe", puede
    significar RLS bloqueando, una clave equivocada, un fallo de red que
    devuelve 200 con cuerpo vacío o un despliegue a medias. Ante cualquiera
    de esos casos echaba al usuario. Y como además hacía `return False` antes
    de actualizar el marcador de tiempo, no esperaba los cinco minutos:
    reintentaba y volvía a echarlo en CADA interacción, así que cambiar de
    local cerraba la sesión.

    Una función de mantenimiento que se ejecuta de fondo nunca debe tomar la
    acción destructiva ante datos ambiguos. Si no está segura, no hace nada.
    """
    if time.time() - st.session_state.get("_ultimo_refresco", 0) < REFRESCO_CONTEXTO_SEGUNDOS:
        return True

    usuario_sesion = st.session_state.get("usuario_actual")
    if not usuario_sesion:
        return True

    # Se marca el intento ANTES de consultar. Así, pase lo que pase —error,
    # respuesta vacía, timeout—, no se reintenta hasta dentro de cinco
    # minutos. Sin esta línea, un fallo persistente convierte esta función
    # en una consulta a la base de datos en cada clic del usuario.
    st.session_state["_ultimo_refresco"] = time.time()

    try:
        u = supabase.table("usuarios").select("*").eq("id", usuario_sesion["id"]).execute()

        if not u.data:
            # Ambiguo: puede ser RLS, una clave sin permisos o un fallo
            # transitorio. NO es prueba de que el usuario esté de baja.
            # Se deja la sesión como está y se reintenta más tarde.
            return True

        fila = u.data[0]

        # Única condición que justifica cerrar la sesión: la base de datos
        # responde correctamente y dice, de forma explícita, que está inactivo.
        if fila.get("activo") is False:
            _cerrar_sesion_local()
            return False

        st.session_state["usuario_actual"] = fila

        a = supabase.table("agencias").select("*").eq("id", fila["agencia_id"]).execute()
        if a.data:
            st.session_state["agencia_actual"] = a.data[0]

        l = supabase.table("locales").select("*").eq("agencia_id", fila["agencia_id"]).execute()
        if l.data is not None:
            st.session_state["locales_agencia"] = l.data

    except Exception:
        # Un corte de red no debe echar a nadie de su sesión.
        pass

    return True


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

# Se restaura ANTES que cualquier otra cosa toque la URL (en concreto, antes
# del bloque de Stripe de abajo, que hace st.query_params.clear() en varias
# ramas). Si esto fuera después, una vuelta desde Stripe podría borrar el
# token de la URL antes de que diera tiempo a leerlo.
_restaurar_sesion_desde_token()

# =========================================================
# 🔑 VUELTA DESDE UN ENLACE DE RECUPERACIÓN (?r=token)
# =========================================================
# Va justo aquí, antes del bloque de Stripe, por el mismo motivo que la
# restauración de sesión: más abajo hay varias ramas que hacen
# st.query_params.clear() y se llevarían el token por delante.
#
# Se atiende ANTES de comprobar la sesión a propósito. Alguien puede estar
# logueado en otra pestaña y aun así querer cambiar su contraseña desde el
# enlace del correo; y el token, no la sesión, es lo que autoriza aquí.
if st.query_params.get("r"):
    render_pantalla_reset(st.query_params.get("r"))
    st.stop()

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
            st.success(f"Pago confirmado. Tu plan '{resultado_pago}' ya está activo.")
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
    st.markdown('<div class="rp-hero-title" style="font-size:1.8rem;">Ya casi está</div>', unsafe_allow_html=True)
    render_formulario_alta_pendiente()
    st.stop()

if not st.session_state.sesion_activa:

    st.markdown("""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=IBM+Plex+Mono:wght@400;500&display=swap');
        html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
        .rp-hero-title {
            font-family: 'Inter', sans-serif; font-weight: 500; font-size: 2.4rem;
            color: #1a2238; line-height: 1.1; margin-bottom: 0.6rem; letter-spacing: -0.03em;
        }
        .rp-hero-sub { color: #232c47; font-size: 1.02rem; margin-bottom: 2rem; letter-spacing: -0.008em; line-height: 1.6; max-width: 620px; }
        .rp-card {
            background: #FDFBF7; border: 1px solid rgba(26,34,56,.12); border-radius: 12px;
            padding: 28px 26px; height: 100%; box-shadow: 0 1px 3px rgba(26,34,56,0.05);
        }
        .rp-card-destacado { border: 1.5px solid #1a2238; box-shadow: 0 4px 14px rgba(26,34,56,0.10); }
        .rp-plan-nombre { font-family: 'Inter', sans-serif; font-weight: 600; font-size: 1.1rem; color: #1a2238; margin-bottom: 4px; letter-spacing: -0.01em; }
        .rp-plan-target { color: #6b7280; font-size: 0.82rem; margin-bottom: 18px; letter-spacing: 0; }
        .rp-precio { font-family: 'Fraunces', Georgia, serif; font-size: 2.3rem; font-weight: 500; color: #1a2238; }
        .rp-precio-periodo { color: #6b7280; font-size: 0.85rem; }
        .rp-feature { color: #232c47; font-size: 0.87rem; margin: 8px 0; letter-spacing: -0.005em; }
        .rp-badge { display:inline-block; background:#1a2238; color:#FDFBF7; border:none; font-size:0.66rem;
            padding: 4px 11px; border-radius: 6px; margin-bottom: 14px; font-weight:600; letter-spacing: 0.08em; text-transform:uppercase; }
        .rp-badge-verde { display:inline-block; background:#ECEAF1; color:#6b7280; border:none; font-size:0.66rem;
            padding: 4px 11px; border-radius: 6px; margin-bottom: 14px; font-weight:600; letter-spacing: 0.08em; text-transform:uppercase; }
        .rp-precio-tachado { color:#9aa0ac; font-size:1rem; text-decoration:line-through; margin-right:8px; font-family:'Fraunces',serif; }
        .rp-precio-ahorro { color:#1a2238; font-size:0.78rem; font-weight:500; margin-top:4px; letter-spacing:0; }
        .rp-por-local { color:#6b7280; font-size:0.8rem; font-weight:500; margin-top:8px; font-family:'IBM Plex Mono',monospace; }
        .rp-gancho { color:#6b7280; font-size:0.8rem; margin-top:12px; min-height:34px; line-height:1.55; }
        .rp-roi-banner {
            background:#FDFBF7; border:1px solid rgba(26,34,56,.12); border-left:3px solid #c8892a;
            border-radius:0 10px 10px 0; padding:18px 24px; margin:6px 0 24px 0; color:#232c47; line-height:1.6;
        }
        .rp-roi-banner strong { color:#1a2238; font-weight:600; }
        .rp-garantia { color:#6b7280; font-size:0.82rem; text-align:center; margin-top:16px; letter-spacing:0; }

        /* -----------------------------------------------------------------
           CONTENCIÓN DE ANCHO
           Con layout="wide" esta vista se estiraba a todo el monitor y por eso
           se veía vacía y desordenada: párrafos de 200 caracteres y tarjetas
           separadas por medio metro. Una landing necesita medida de lectura.
           ----------------------------------------------------------------- */
        section[data-testid="stMain"] .block-container {
            max-width: 940px !important;
        }

        .rp-eyebrow {
            font-family:'IBM Plex Mono',ui-monospace,monospace;
            font-size:0.7rem; color:var(--er-amber);
            letter-spacing:0.3em; text-transform:uppercase;
            margin-bottom:16px; font-weight:600;
        }

        /* --- Tesis: el argumento central --- */
        .rp-tesis {
            border-left:2px solid var(--er-amber);
            padding:6px 0 6px 22px;
            margin:34px 0 30px;
        }
        .rp-tesis-frase {
            font-family:'Fraunces',Georgia,serif;
            font-size:1.32rem; line-height:1.45; color:var(--er-ink);
            letter-spacing:-0.02em;
        }
        .rp-tesis-o {
            color:var(--er-amber); font-style:italic; padding:0 2px;
        }
        .rp-tesis-sub {
            font-size:0.9rem; color:var(--er-muted);
            margin-top:10px; line-height:1.6;
        }

        /* --- Demostración por contraste --- */
        .rp-demo {
            background:var(--er-sunken);
            border:1px solid var(--er-line);
            border-radius:12px;
            padding:22px;
            margin-bottom:38px;
        }
        .rp-demo-lbl {
            display:block;
            font-family:'IBM Plex Mono',ui-monospace,monospace;
            font-size:0.62rem; letter-spacing:0.15em; text-transform:uppercase;
            color:var(--er-faint); margin-bottom:7px;
        }
        .rp-demo-resena {
            font-size:0.92rem; color:var(--er-body); line-height:1.6;
            font-style:italic;
            padding-bottom:18px; margin-bottom:18px;
            border-bottom:1px solid var(--er-line);
        }
        .rp-demo-grid {
            display:grid; grid-template-columns:1fr 1fr; gap:16px;
        }
        @media (max-width: 760px) {
            .rp-demo-grid { grid-template-columns:1fr; }
        }
        .rp-demo-col {
            background:#fff; border:1px solid var(--er-line);
            border-radius:9px; padding:15px 16px;
        }
        .rp-demo-mal  { border-top:2px solid var(--er-danger); }
        .rp-demo-bien { border-top:2px solid var(--er-ok); }
        .rp-demo-tag {
            display:inline-block;
            font-family:'IBM Plex Mono',ui-monospace,monospace;
            font-size:0.6rem; letter-spacing:0.12em; text-transform:uppercase;
            padding:2px 7px; border-radius:3px; margin-bottom:10px;
        }
        .rp-demo-tag-mal  { background:var(--er-danger-bg); color:var(--er-danger); }
        .rp-demo-tag-bien { background:#E8F0EA; color:var(--er-ok); }
        .rp-demo-col p {
            font-size:0.86rem; line-height:1.62; color:var(--er-body);
            margin:0 0 11px;
        }
        .rp-demo-col u {
            text-decoration:underline;
            text-decoration-color:var(--er-danger);
            text-underline-offset:2px;
        }
        .rp-demo-nota {
            display:block; font-size:0.76rem; line-height:1.55;
            color:var(--er-muted); padding-top:10px;
            border-top:1px dashed var(--er-line);
        }

        /* --- Etiqueta de sección --- */
        .rp-seccion-lbl {
            font-family:'IBM Plex Mono',ui-monospace,monospace;
            font-size:0.63rem; letter-spacing:0.16em; text-transform:uppercase;
            color:var(--er-faint); margin:0 0 14px;
        }

        /* --- Tarjetas de capacidad --- */
        .rp-card-info {
            height:100%;
            transition:border-color .16s ease;
        }
        .rp-card-info:hover { border-color:var(--er-line-2); }
        .rp-card-num {
            font-family:'IBM Plex Mono',ui-monospace,monospace;
            font-size:0.7rem; color:var(--er-amber);
            letter-spacing:0.1em; margin-bottom:9px;
        }
        .rp-card-titulo {
            font-family:'Fraunces',Georgia,serif;
            font-size:1.05rem; font-weight:600; color:var(--er-ink);
            letter-spacing:-0.015em; margin-bottom:9px;
        }

        /* --- Dato de negocio --- */
        .rp-dato {
            display:flex; align-items:center; gap:22px;
            background:var(--er-accent); color:#fff;
            border-radius:12px; padding:22px 26px;
            margin-top:34px;
        }
        @media (max-width: 640px) {
            .rp-dato { flex-direction:column; gap:12px; text-align:center; }
        }
        .rp-dato-cifra {
            font-family:'Fraunces',Georgia,serif;
            font-size:2.5rem; font-weight:600; line-height:1;
            letter-spacing:-0.03em; color:var(--er-amber-2);
            flex-shrink:0;
        }
        .rp-dato-txt {
            font-size:0.88rem; line-height:1.62;
            color:rgba(255,255,255,.9);
        }
        </style>
    """, unsafe_allow_html=True)

    # El hero grande de marketing (eyebrow + titular + subtítulo) SOLO va
    # aquí, no en login/planes. Antes era incondicional y se colaba encima
    # del formulario de acceso: alguien que llegaba desde el botón "Entrar"
    # del HTML externo —donde ya ha leído todo este argumentario— se
    # encontraba el mismo discurso repetido antes de poder escribir su email.
    # Con login pensado como destino directo desde fuera, tiene que ser una
    # pantalla de acceso limpia, no una tercera vuelta a la misma venta.

    # -----------------------------------------------------
    # VISTA 1: INFO — ya no vende nada, solo bifurca
    # -----------------------------------------------------
    # Antes esto repetía entera la landing externa: la tesis ("disculpa o
    # confesión"), la demo de contraste, las tres tarjetas de "cómo funciona"
    # y el banner de ROI. Todo eso ya vive en Landing-main/index.html, que es
    # lo primero que ve cualquiera antes de llegar aquí — quien pulsa
    # "Entrar" o "Abrir la herramienta" ya ha leído ese argumentario una vez.
    # Repetirlo dentro de la app no vende más, solo retrasa el único gesto
    # que de verdad hace falta en este punto: elegir entre iniciar sesión o
    # ver los planes. Se reduce a eso.
    if st.session_state.vista_landing == "info":
        # CSS con ámbito propio: los botones de ESTA pantalla se agrandan
        # respecto al tamaño estándar de la app (0.55rem/0.9rem de siempre,
        # pensado para botones secundarios dentro de formularios). Aquí son
        # las DOS únicas acciones de toda la pantalla, así que tienen que
        # pesar como tal.
        #
        # El selector ".st-key-rs_entry_card" es la clase que Streamlit
        # asigna automáticamente al contenedor cuando se le pasa key=... —
        # es la forma soportada de darle una tarjeta CSS propia a un bloque
        # sin que la regla se escape al resto de la app.
        st.markdown(
            """
            <style>
            .st-key-rs_entry_card {
                background: var(--er-surface);
                border: 1px solid var(--er-line-2);
                border-radius: 24px;
                padding: 48px 52px 38px;
                box-shadow: var(--er-shadow-lg);
                -webkit-backdrop-filter: blur(18px) saturate(1.5);
                backdrop-filter: blur(18px) saturate(1.5);
                position: relative;
                margin-top: 5vh;
            }
            /* El mismo hilo de luz que llevan los expanders de cristal en
               el resto de la app — es lo que de verdad vende el "cristal",
               más que el blur en sí. */
            .st-key-rs_entry_card::before {
                content: "";
                position: absolute; top: 0; left: 18px; right: 18px; height: 1px;
                background: var(--er-glass-edge);
            }
            .st-key-rs_entry_card .stButton > button {
                padding: 1.05rem 1.2rem !important;
                font-size: 1.02rem !important;
                font-weight: 600 !important;
                border-radius: 12px !important;
                letter-spacing: -0.01em !important;
            }
            .st-key-rs_entry_card .stButton > button[kind="secondary"] {
                background: var(--er-sunken) !important;
            }

            /* --- Ejemplo artístico: relleno del hueco, no un bloque de venta ---
               A propósito NO reutiliza el texto de la landing externa (ni la
               reseña del gluten, ni la del cobro de más): mismo lenguaje
               visual, ejemplo distinto, para que no se sienta una copia
               reformateada. Todo son frases sueltas, sin párrafos ni notas
               explicativas debajo — es una pieza decorativa que se entiende
               de un vistazo, no un bloque más para leer. */
            .rs-ejemplo {
                margin-top: 36px;
                padding-top: 30px;
                border-top: 1px solid var(--er-line);
            }
            .rs-ejemplo-kicker {
                font-family: 'IBM Plex Mono', ui-monospace, monospace;
                font-size: .64rem; letter-spacing: .28em; text-transform: uppercase;
                color: var(--er-faint); text-align: center; margin-bottom: 18px;
            }
            .rs-ejemplo-resena {
                font-family: 'Fraunces', Georgia, serif; font-style: italic;
                font-size: 1.05rem; line-height: 1.5; color: var(--er-body);
                text-align: center; max-width: 480px; margin: 0 auto 24px;
            }
            .rs-ejemplo-fila {
                display: flex; align-items: flex-start; gap: 12px;
                padding: 13px 16px; border-radius: 12px; margin-bottom: 10px;
                font-size: .88rem; line-height: 1.5;
            }
            .rs-ejemplo-fila:last-child { margin-bottom: 0; }
            .rs-ejemplo-mal {
                background: rgba(214,69,52,.06);
                color: var(--er-muted);
            }
            .rs-ejemplo-bien {
                background: var(--er-accent-bg);
                color: var(--er-ink);
            }
            .rs-ejemplo-marca {
                flex-shrink: 0; width: 20px; height: 20px; border-radius: 50%;
                display: flex; align-items: center; justify-content: center;
                font-size: .68rem; font-weight: 700; margin-top: 1px;
            }
            .rs-ejemplo-mal .rs-ejemplo-marca { background: rgba(214,69,52,.14); color: var(--er-danger); }
            .rs-ejemplo-bien .rs-ejemplo-marca { background: var(--er-accent); color: #fff; }
            .rs-ejemplo-mal p { text-decoration: line-through; text-decoration-color: rgba(214,69,52,.4); }
            .rs-ejemplo-fila p { margin: 0; }
            @media (max-width: 640px) {
                .rs-ejemplo-resena { font-size: .95rem; }
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        # Columna central bastante más ancha que la del login (esta pantalla
        # ya no es un formulario estrecho, es la puerta de entrada y tiene
        # espacio de sobra que llenar con algo que valga la pena mirar).
        _izq_info, _centro_info, _der_info = st.columns([1, 2.5, 1])

        with _centro_info:
            with st.container(key="rs_entry_card"):
                st.markdown(
                    '<div class="rs-login-cab" style="margin-bottom:26px;">'
                    '<div class="rs-login-marca">RESELIA</div>'
                    "</div>",
                    unsafe_allow_html=True,
                )

                # Lado a lado: con la columna más ancha ya hay sitio de sobra
                # (antes, a 330px, "Iniciar sesión" partía en dos líneas).
                _col_ini, _col_planes = st.columns(2, gap="medium")
                with _col_ini:
                    if st.button("Iniciar sesión", use_container_width=True):
                        st.session_state.vista_landing = "login"
                        st.rerun()
                with _col_planes:
                    if st.button("Ver planes", use_container_width=True, type="primary"):
                        st.session_state.vista_landing = "planes"
                        st.rerun()

                # --- El ejemplo artístico ---
                st.markdown(
                    """
                    <div class="rs-ejemplo">
                        <div class="rs-ejemplo-kicker">Una reseña real</div>
                        <div class="rs-ejemplo-resena">
                            «Reservamos para las nueve y a las nueve y media
                            seguíamos en la puerta. Nadie nos avisó de nada.»
                        </div>
                        <div class="rs-ejemplo-fila rs-ejemplo-mal">
                            <div class="rs-ejemplo-marca">✕</div>
                            <p>«Lamentamos el fallo en la gestión de reservas de esa noche.»</p>
                        </div>
                        <div class="rs-ejemplo-fila rs-ejemplo-bien">
                            <div class="rs-ejemplo-marca">§</div>
                            <p>«Una espera así no es lo que quiero para nadie que
                            venga con ganas. Lo reviso personalmente con el equipo de sala.»</p>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                # -----------------------------------------------------
                # BLOQUE DE CONTACTO / SOPORTE
                # -----------------------------------------------------
                # Esto no es marketing, es funcional: cubre el caso del "pago
                # huérfano" (alguien que pagó en Stripe pero no llegó a crear
                # su cuenta y no tiene forma de entrar). Por eso se conserva
                # aunque todo el texto de venta de alrededor haya
                # desaparecido. El mailto lleva asunto y cuerpo ya rellenados
                # para que el cliente solo tenga que darle a enviar.
                _email_soporte = "hola@reselia.es"
                _asunto = urllib.parse.quote("Necesito ayuda con mi cuenta / pago")
                _cuerpo = urllib.parse.quote(
                    "Hola,\n\nHe tenido un problema y necesito ayuda. Os cuento:\n\n"
                    "(Describe aquí tu caso. Si acabas de pagar y no has podido "
                    "crear la cuenta, indícanos el email con el que hiciste el "
                    "pago.)\n\nGracias."
                )
                _mailto = f"mailto:{_email_soporte}?subject={_asunto}&body={_cuerpo}"
                st.markdown(
                    '<div class="rs-login-pie" style="margin-top:24px;">'
                    "¿Has pagado y no puedes acceder? Escribe a "
                    f'<a href="{_mailto}">{_email_soporte}</a>'
                    "</div>",
                    unsafe_allow_html=True,
                )
        st.stop()

    # Botón para volver a la info desde las otras dos vistas
    if st.button("← Volver"):
        st.session_state.vista_landing = "info"
        st.rerun()

    mostrar_planes = st.session_state.vista_landing == "planes"
    mostrar_login = st.session_state.vista_landing == "login"

    if mostrar_planes:
        st.caption("¿Ya tienes cuenta? Usa el botón '← Volver' de arriba y elige 'Iniciar sesión'.")
    # La caption equivalente para login se ha quitado: repetía, con otras
    # palabras, lo mismo que ya dice el botón "← Volver" de arriba y lo que
    # va a decir el enlace "¿No tienes cuenta?" al pie de la propia tarjeta
    # de login. Era la tercera vez que la misma idea aparecía en pantalla
    # antes de llegar al campo de email.

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

        # --- Aviso de beta: los pagos están desactivados ---
        # Va ANTES del selector de facturación y de las tarjetas, para que nadie
        # recorra toda la comparativa de precios y solo descubra al final que no
        # puede comprar. Los precios se dejan visibles a propósito: sirven para
        # anclar el valor de lo que ahora mismo se está regalando.
        if MODO_BETA_SIN_PAGOS:
            st.info(
                "**Beta abierta — acceso gratuito.** Los pagos están desactivados durante "
                "este periodo: puedes usar Reselia sin límite y sin introducir ninguna "
                "tarjeta. Los precios de abajo son los que se aplicarán cuando la beta "
                "termine, y te avisaremos antes de que eso ocurra."
            )

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
            st.caption(f"Pagando por año te ahorras un {int(DESCUENTO_ANUAL*100)}% — equivale a llevarte más de 2 meses gratis.")

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
                '<div class="rp-feature">— 1 local de prueba</div>'
                f'<div class="rp-feature">— {"Respuestas ilimitadas durante tu periodo de prueba" if MODO_BETA_RESPUESTAS_ILIMITADAS else f"{LIMITE_USOS_PLAN_GRATIS} respuestas / mes"}</div>'
                '<div class="rp-feature">— Sin tarjeta de crédito</div>'
                '<div class="rp-feature" style="opacity:0.35;">— Marca blanca (no incluida)</div>'
                '<div class="rp-gancho">Ideal para ver la calidad de las respuestas sin compromiso.</div>'
                '</div>'
            )
            st.markdown(free_html, unsafe_allow_html=True)
            with st.popover("Empezar gratis", use_container_width=True):
                st.caption("Crea tu cuenta en 30 segundos. Sin tarjeta.")
                if "free_signup_abierto_en" not in st.session_state:
                    st.session_state.free_signup_abierto_en = datetime.utcnow()
                nombre_agencia_free = st.text_input("Nombre de tu agencia o negocio", key="free_nombre_agencia")
                nombre_local_free = st.text_input("Nombre del primer local a probar", key="free_nombre_local")
                nombre_usuario_free = st.text_input("Tu nombre", key="free_nombre_usuario")
                email_free = st.text_input("Email", key="free_email")
                password_free = st.text_input("Contraseña (mín. 8 caracteres)", type="password", key="free_password")
                if st.button("Crear cuenta gratis", key="free_submit", use_container_width=True):
                    if not all([nombre_agencia_free, nombre_local_free, nombre_usuario_free, email_free, password_free]):
                        st.warning("Rellena todos los campos.")
                    else:
                        segundos_transcurridos = (datetime.utcnow() - st.session_state.free_signup_abierto_en).total_seconds()
                        ok, error = registrar_agencia_gratuita(
                            nombre_agencia_free, nombre_local_free, email_free, password_free, nombre_usuario_free,
                            segundos_desde_apertura=segundos_transcurridos
                        )
                        if ok:
                            del st.session_state["free_signup_abierto_en"]

                            # Entrar directamente. Acaba de escribir su email y
                            # su contraseña: mandarle a otra pestaña a teclear
                            # lo mismo otra vez es pedirle trabajo por nada, y
                            # es justo el momento en el que más gente abandona.
                            #
                            # Se reutiliza cargar_perfil_login en lugar de
                            # montar la sesión a mano para que el alta y el
                            # login normal recorran exactamente el mismo camino:
                            # si un día cambian las comprobaciones del login,
                            # este flujo las hereda solo.
                            try:
                                perfil_nuevo, _err = cargar_perfil_login(
                                    email_free.lower().strip(),
                                    password_free,
                                    nombre_usuario=(nombre_usuario_free or "").strip() or None,
                                )
                            except Exception:
                                perfil_nuevo = None

                            if perfil_nuevo:
                                limpiar_fallos_login()
                                marcar_actividad()
                                st.session_state.sesion_activa = True
                                st.session_state.usuario_actual = perfil_nuevo["usuario"]
                                st.session_state.agencia_actual = perfil_nuevo["agencia"]
                                st.session_state.locales_agencia = perfil_nuevo["locales"]
                                _crear_token_sesion(perfil_nuevo["usuario"]["id"])
                                st.session_state["_recien_registrado"] = True
                                st.rerun()
                            else:
                                # La cuenta SÍ se creó; solo ha fallado el
                                # inicio automático. Hay que decirlo así, o la
                                # persona pensará que no se ha registrado y lo
                                # intentará otra vez con el mismo email.
                                st.success(
                                    "Cuenta creada correctamente. Entra desde "
                                    "'Ya tengo cuenta' con el email y la contraseña "
                                    "que acabas de elegir."
                                )
                        else:
                            st.error(error)

        with col_individual:
            plan_ind = PLANES_AUTOSERVICIO["individual"]
            features_ind = "".join(f'<div class="rp-feature">— {f}</div>' for f in plan_ind["features"])
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
            if MODO_BETA_SIN_PAGOS:
                st.button(
                    "No disponible en beta",
                    key="landing_elegir_individual",
                    use_container_width=True,
                    disabled=True,
                )
            elif st.button("Empezar con Individual", key="landing_elegir_individual", use_container_width=True, type="primary"):
                price_id_ind = plan_ind["price_ids"]["anual" if es_anual else "mensual"]
                url_pago = crear_sesion_pago_nueva_agencia("individual", price_id_ind)
                if url_pago:
                    redirigir_a_stripe(url_pago)

        # --- Fila 2: planes de agencia ---
        st.markdown("<div style='height:26px'></div>", unsafe_allow_html=True)
        st.markdown('<div class="rp-plan-target" style="font-size:0.95rem; margin-bottom:8px;">¿Gestionas varios locales? Planes para agencias:</div>', unsafe_allow_html=True)
        col_starter, col_growth = st.columns(2)

        # Solo hay dos planes de agencia. Enterprise ya no existe: se eliminó
        # del catálogo (ver PLANES_AUTOSERVICIO), así que esta lista y el
        # selector de dentro de la app enseñan exactamente lo mismo.
        planes_agencia = [
            ("starter", col_starter, "landing_elegir_starter"),
            ("growth", col_growth, "landing_elegir_growth"),
        ]
        for clave_plan, columna, boton_key in planes_agencia:
            datos = PLANES_AUTOSERVICIO[clave_plan]
            with columna:
                clase_card = "rp-card rp-card-destacado" if datos.get("destacado") else "rp-card"
                badge = '<span class="rp-badge">MÁS ELEGIDO</span>' if datos.get("destacado") else ""
                features = "".join(f'<div class="rp-feature">— {f}</div>' for f in datos["features"])
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
                if MODO_BETA_SIN_PAGOS:
                    st.button(
                        "No disponible en beta",
                        key=boton_key,
                        use_container_width=True,
                        disabled=True,
                    )
                elif st.button(f"Elegir {datos['nombre']}", key=boton_key, use_container_width=True, type=tipo_boton):
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
        # El login se centra en una columna estrecha. Con layout="wide" un
        # formulario a ancho completo queda desangelado: los campos se estiran
        # a 1400px y la pantalla parece vacía. Tres columnas con el peso en la
        # del medio devuelven la proporción de una pantalla de acceso.
        _izq, _centro, _der = st.columns([1, 1.15, 1])

        with _centro:
            st.markdown(
                """
                <div class="rs-login-cab">
                  <div class="rs-login-marca">RESELIA</div>
                  <h1 class="rs-login-titulo">Bienvenido de nuevo</h1>
                  <p class="rs-login-sub">
                    Cada usuario de tu agencia tiene su propio acceso.
                  </p>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # st.form permite enviar con Enter desde cualquier campo, que es lo
            # que espera cualquiera que haya usado un login en su vida. Antes
            # eran inputs sueltos y había que ir a buscar el botón con el ratón.
            with st.form("form_login", border=False):
                email_usuario = st.text_input(
                    "Email",
                    placeholder="tu@agencia.com",
                    key="_email_login",
                )
                password_usuario = st.text_input(
                    "Contraseña",
                    type="password",
                    placeholder="Tu contraseña",
                    key="_password_login",
                )

                # Este campo solo lo necesita una minoría (agencias con varias
                # cuentas compartiendo email). Dentro de un expander deja de
                # ocupar sitio y de generar la duda de si hay que rellenarlo.
                with st.expander("¿Varias cuentas con el mismo email?"):
                    nombre_usuario_login = st.text_input(
                        "Nombre de usuario",
                        key="_nombre_usuario_login",
                        help=(
                            "Solo si tu agencia tiene varias cuentas registradas "
                            "con la misma dirección de correo."
                        ),
                    )

                enviado_login = st.form_submit_button(
                    "Iniciar sesión", use_container_width=True, type="primary"
                )

            if enviado_login:
                _permitido, _espera = comprobar_freno_login()
                if not _permitido:
                    st.error(
                        f"Demasiados intentos fallidos. Vuelve a probar en {_espera} segundos."
                    )
                elif not email_usuario.strip() or not password_usuario:
                    st.warning("Introduce email y contraseña.")
                else:
                    email_normalizado = email_usuario.lower().strip()
                    with st.spinner("Verificando credenciales..."):
                        try:
                            perfil, error_login = cargar_perfil_login(
                                email_normalizado,
                                password_usuario,
                                nombre_usuario=(nombre_usuario_login or "").strip() or None,
                            )

                            if perfil is None:
                                registrar_fallo_login()
                                st.error(error_login or "Email o contraseña incorrectos.")
                            else:
                                limpiar_fallos_login()
                                marcar_actividad()
                                st.session_state.sesion_activa = True
                                st.session_state.usuario_actual = perfil["usuario"]
                                st.session_state.agencia_actual = perfil["agencia"]
                                st.session_state.locales_agencia = perfil["locales"]
                                _crear_token_sesion(perfil["usuario"]["id"])
                                st.success(f"Bienvenido, {perfil['usuario']['nombre_usuario']}.")
                                st.rerun()
                        except Exception as e:
                            st.error(redactar_secretos(f"Error de conexión con la base de datos: {e}"))

            # ---- Recuperación de contraseña ----
            # Fuera del st.form de arriba: un formulario dentro de otro no es
            # válido, y además así el expander se puede abrir sin disparar el
            # envío del login.
            if st.session_state.pop("_reset_completado", False):
                st.success(
                    "Contraseña actualizada. Ya puedes entrar con la nueva."
                )

            with st.expander("He olvidado mi contraseña"):
                email_reset = st.text_input(
                    "Tu email",
                    key="_email_reset",
                    placeholder="tu@agencia.com",
                )
                if st.button("Enviarme un enlace", key="_btn_reset", use_container_width=True):
                    if not email_reset.strip():
                        st.warning("Escribe tu email.")
                    else:
                        enviado, motivo_tecnico = solicitar_reset_password(email_reset)
                        if motivo_tecnico == "formato":
                            st.warning("Ese email no tiene un formato válido.")
                        elif enviado:
                            # Mensaje deliberadamente idéntico exista o no la
                            # cuenta: ver la nota en solicitar_reset_password().
                            st.success(
                                "Si ese email tiene una cuenta, te hemos enviado "
                                "un enlace para elegir una contraseña nueva. "
                                "Caduca en 30 minutos."
                            )
                        else:
                            # Sin SMTP configurado (o fallo de envío). No se
                            # promete un correo que no va a llegar.
                            st.info(
                                "Hemos registrado tu solicitud. Escríbenos a "
                                "hola@reselia.es y te mandamos el enlace de "
                                "recuperación hoy mismo."
                            )

            st.markdown(
                '<div class="rs-login-pie">'
                '¿Problemas para entrar? Escribe a '
                '<a href="mailto:hola@reselia.es">hola@reselia.es</a>'
                "</div>",
                unsafe_allow_html=True,
            )

            # Único sitio donde login ofrece "no tengo cuenta". Antes esta
            # misma idea aparecía TRES veces en pantalla (la caption de
            # arriba, el botón "← Volver" y este mensaje). Un botón discreto
            # al pie de la tarjeta, sin use_container_width ni type="primary",
            # basta: está donde se busca sin competir con "Iniciar sesión".
            st.markdown('<div style="height:6px"></div>', unsafe_allow_html=True)
            _col_np_izq, _col_np_centro, _col_np_der = st.columns([1, 1.4, 1])
            with _col_np_centro:
                if st.button("¿No tienes cuenta? Ver planes",
                             key="_btn_ver_planes_desde_login"):
                    st.session_state.vista_landing = "planes"
                    st.rerun()

    st.stop()

# A partir de aquí: sesión válida.

# Caducidad por inactividad. Sin esto, una sesión abierta en el ordenador
# compartido de una agencia sigue viva indefinidamente mientras no cierren
# la pestaña.
if not sesion_valida():
    st.warning("Tu sesión ha caducado por inactividad. Vuelve a entrar.")
    st.rerun()

# Refresco periódico de agencia y usuario contra la base de datos, para que
# un cambio de plan por impago o una revocación de acceso surtan efecto sin
# esperar a que la persona cierre sesión.
if not refrescar_contexto_si_toca():
    st.warning("Tu acceso ha sido revocado por el administrador de tu agencia.")
    st.rerun()

agencia = st.session_state.agencia_actual
usuario = st.session_state.usuario_actual
color_agencia = agencia["color_marca"]

# Bienvenida tras el alta. Se consume con pop para que salga una sola vez y
# no reaparezca en cada rerun de la sesión.
if st.session_state.pop("_recien_registrado", False):
    st.success(
        f"Cuenta creada, {usuario['nombre_usuario']}. Ya estás dentro — "
        "empieza pegando una reseña en 'Responder reseña'."
    )

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
    /* Mismo criterio: header transparente con altura natural, y stToolbar
       NUNCA se oculta porque contiene el botón que reabre la sidebar
       (stExpandSidebarButton). Ver comentario detallado en el bloque
       principal — ocultar el padre se llevaba por delante el botón. */
    header, div[data-testid="stHeader"] {{
        background: transparent !important;
        box-shadow: none !important;
        border: none !important;
    }}
    div[data-testid="stDecoration"],
    div[data-testid="stMainMenu"] {{
        display: none !important;
        height: 0px !important;
    }}
    /* Duplicado intencional del bloque de arriba: este <style> se inyecta más
       tarde (es el de marca blanca por agencia) y Streamlit no garantiza el
       orden entre bloques en reruns parciales, así que se repite aquí por
       seguridad. IMPORTANTE — nunca añadir position:fixed a este selector sin
       envolverlo en @media (max-width:900px): un descuido aquí ya rompió una
       vez el layout de TODOS los botones de la página (ver historial). */
    /* Duplicado intencional del bloque de arriba: este <style> se inyecta más
       tarde (es el de marca blanca por agencia) y Streamlit no garantiza el
       orden entre bloques en reruns parciales, así que se repite aquí por
       seguridad. IMPORTANTE — nunca añadir position:fixed a este selector sin
       envolverlo en @media (max-width:900px): un descuido aquí ya rompió una
       vez el layout de TODOS los botones de la página (ver historial).

       El testid real (verificado ejecutando Streamlit 1.62 e inspeccionando
       el DOM, no adivinado) es "stExpandSidebarButton" — los nombres
       "stSidebarCollapsedControl" y "collapsedControl" que se usaban antes
       no existen en esta versión y por eso el botón nunca se veía. Se
       mantienen como fallback por si el hosting corre otra versión de
       Streamlit con un nombre distinto, pero el bueno es el primero. */
    button[data-testid="stExpandSidebarButton"],
    div[data-testid="stSidebarCollapsedControl"],
    div[data-testid="collapsedControl"] {{
        display: flex !important;
        visibility: visible !important;
        opacity: 1 !important;
        z-index: 999999 !important;
    }}
    /* El botón de acción principal (generar respuesta) usa SIEMPRE el índigo de la
       app, nunca el color de marca guardado en la BD (que podía ser morado y salir
       ilegible). Forzamos también el color del texto interno para que nunca falle. */
    div[data-testid="stFormSubmitButton"] button {{
        background-color: {ACCENT_INDIGO} !important;
        border: 1px solid {ACCENT_INDIGO} !important;
        font-weight: 500 !important;
        border-radius: 6px !important;
        letter-spacing: -0.006em !important;
        box-shadow: 0 1px 2px rgba(59,58,107,0.18) !important;
    }}
    div[data-testid="stFormSubmitButton"] button,
    div[data-testid="stFormSubmitButton"] button * {{
        color: #FFFFFF !important;
    }}
    div[data-testid="stFormSubmitButton"] button:hover {{
        background-color: {ACCENT_INDIGO_HOVER} !important;
        border-color: {ACCENT_INDIGO_HOVER} !important;
    }}
    </style>
    """, unsafe_allow_html=True)

# =========================================================
# PROMPTS DE REDACCIÓN
# =========================================================
# Antes vivían dentro del bloque de la pestaña "Generar", lo que obligaba a
# reconstruir 26 KB de texto en CADA re-ejecución de Streamlit — es decir,
# en cada clic. A nivel de módulo se construyen una sola vez al arrancar.

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

bloque_estatico = """Eres la persona que gestiona de verdad las reseñas de un negocio local: el dueño, el gerente o el responsable de sala, escribiendo entre turnos, no un departamento de relaciones públicas. Tu tarea es redactar una respuesta pública a una reseña que puede ser POSITIVA o NEGATIVA, y que suene a una persona real de carne y hueso, no a una plantilla corporativa.

Debes devolver EXCLUSIVAMENTE un objeto JSON válido, sin texto adicional antes ni después, sin bloques de código markdown, con esta estructura exacta:
{
  "idioma_detectado": "código de idioma ISO de dos letras, ej: es, en, fr",
  "sentimiento": "positivo" o "negativo",
  "respuesta_nativa": "la respuesta redactada en el idioma original de la reseña",
  "traduccion_espanol": "traducción literal al español para el propietario, o null si la reseña ya estaba en español"
}

NORMAS DE IDIOMA Y CONTEXTO ABSOLUTAS:
- Analiza minuciosamente el idioma de la reseña y responde de forma nativa en ese mismo idioma.
- REGLA FRANCESA CRÍTICA: en francés, usa únicamente fórmulas de cortesía formal ("vous", "votre", "vos"); prohibido tutear.
- CONTROL DE ALUCINACIÓN DE MARCA: usa únicamente el nombre de establecimiento que te indican en el bloque de contexto. No inventes otro.

CÓMO SONAR HUMANO Y NO A IA (esto es lo más importante de todo el prompt):
- Elige UN SOLO hilo emocional o UN SOLO detalle concreto de la reseña y desarróllalo con algo de profundidad, en vez de contestar la reseña punto por punto como una checklist ("en cuanto a X... en cuanto a Y... en cuanto a Z..."). Un cliente real no organiza su respuesta por categorías, reacciona a lo que más le ha dolido o alegrado.
- Máximo UNA frase de apertura empática (tipo "lamentamos..." o "nos alegra..."). Prohibido encadenar varias frases de validación emocional seguidas (nada de "lamentamos... entendemos... valoramos..." una detrás de otra). Esa cadencia es la huella más reconocible de un texto generado por IA y hace que suene idéntico al de cualquier otro negocio.
- PROHIBIDO usar estas muletillas de plantilla, están quemadas de tanto verlas en internet: "nuestros estándares de calidad", "lo sucedido", "investigar a fondo", "su opinión es muy valiosa para nosotros/para seguir mejorando", "no reflejan nuestro compromiso habitual", "dista mucho de la experiencia que deseamos ofrecer", "reforzar la formación de nuestro equipo". Si necesitas decir algo parecido, dilo con tus propias palabras, distintas cada vez.
- Varía la longitud de las frases dentro de la misma respuesta: alguna corta y directa, otra más desarrollada. Un texto donde todas las frases miden parecido suena a máquina.
- Referencia al menos un detalle textual y específico de la reseña (una palabra, una situación muy concreta que haya mencionado el cliente) en vez de convertir todo en categorías genéricas ("el servicio", "la comida", "los tiempos"). Ese detalle es lo que hace creíble que alguien ha leído de verdad la reseña.

REGLAS DE REDACCIÓN SEGÚN EL SENTIMIENTO:
1. TONO OBLIGADO: el descrito en el bloque de contexto. Siempre educado y constructivo, nunca condescendiente.
2. SI ES POSITIVA: agradecimiento genuino (no genérico), referencia a algo concreto que el cliente mencionó, invitación a volver que no suene copiada y pegada.
3. SI ES NEGATIVA:
   - LONGITUD OBJETIVO: entre 100 y 140 palabras. Ni menos (queda seca, distante, telegrama corporativo) ni más (queda excesiva y suena a defensa preparada). Esa horquilla es la que da sensación de que hay alguien detrás leyendo con atención. Nunca inflar con frases vacías tipo "queremos aprovechar para agradecerle una vez más" o repeticiones de la disculpa: si te quedas corto, desarrolla el hilo emocional o el detalle concreto que elegiste, no metas relleno.
   - Inicio dinámico: prohibido empezar siempre con "Gracias por su comentario" o equivalentes; varía la apertura.
   - REGLA DE LA VERDAD QUE NO TIENES: quien escribe esta respuesta no estaba en la cocina ni en la sala esa noche, así que nunca puede confirmar ni negar la causa interna concreta de lo que el cliente describe. Se puede validar por completo su experiencia como algo real y lamentable ("lo que usted describe es serio y lo lamento de verdad"), pero esa validación NUNCA se convierte en una confirmación de causa. Valida la experiencia del cliente, no confirmes el mecanismo interno que la causó — esa línea es la más importante de toda esta sección.
   - REGLA DE LOS VERBOS DE CONFIRMACIÓN (esto blinda la regla anterior frente a paráfrasis): la prohibición de confirmar una causa interna no depende de una lista de frases fijas, depende del ACTO de confirmar, diga lo que diga con esas palabras. Verbos y giros como "lo reconozco", "reconozco que...", "admito que...", "confirmo que...", "en efecto, eso fue lo que pasó", "tiene usted razón en que...", "así fue", "eso es correcto" NUNCA pueden ir seguidos de una causa, un fallo o una omisión concreta atribuible al negocio (por ejemplo: "eso es una falta de información por nuestra parte, lo reconozco" es tan grave como decir "fue culpa nuestra", aunque no use esas palabras exactas). Antes de escribir cualquier frase que empiece por un verbo de confirmación, comprueba mentalmente si lo que sigue describe qué salió mal por dentro del negocio; si es así, reformula sin ese verbo, quedándote solo en la validación de cómo se sintió el cliente.
   - REGLA DE LAS CIFRAS Y DATOS QUE NO PUEDES VERIFICAR: si el cliente menciona un importe, un precio, una diferencia de cobro, una fecha, un porcentaje o cualquier dato concreto y verificable ("me cobraron 189€ en vez de 89€", "llevo 3 años esperando", "la diferencia es de 100€"), NUNCA repitas esas cifras exactas en la respuesta ni las trates como un hecho ya asumido. Repetir el número del cliente ("esos 100€ de diferencia...") equivale a confirmarlo por escrito, y esa respuesta queda pública y permanente — es exactamente el tipo de frase que un abogado usa como admisión. Habla siempre en términos genéricos y no cuantificados: "cualquier cargo que usted no reconozca merece revisión", nunca "esos 100€ que menciona merecen revisión". Lo mismo aplica a la propia palabra "desfase", "diferencia" o "descuadre" seguida del número exacto: la palabra sin el número es aceptable, con el número repetido no lo es.
   - PROHIBIDO EXPLÍCITAMENTE, aunque suene humano y hasta bien intencionado (son admisiones legales en toda regla): "es un fallo nuestro", "fue culpa nuestra"/"nuestra culpa", "no fue así", "eso no debería haber pasado" seguido de una causa concreta, "se nos escapó", "fallamos en...", "no llegó el aviso/la información", "no es algo que podamos dejar pasar" (aplicado a lo que el cliente reclama, porque da a entender que se acepta como cierto), o cualquier frase en primera persona que confirme qué salió mal por dentro del negocio. Tampoco detalles operativos concretos (tiempos de cocción, temperaturas, protocolos de conservación, cadenas de comunicación interna) — describir con ese nivel de detalle lo que se está "revisando" equivale a admitir dónde estuvo el fallo, aunque no se diga con esas palabras exactas.
   - BLINDAJE JURÍDICO TOTAL: prohibido admitir negligencias, explícita o implícitamente, o usar alertas sanitarias ("higiene alimentaria", "intoxicación", "contaminación"); usa perífrasis suaves y naturales, no siempre las mismas palabras. Ante temas de cobro o facturación, prohibido cualquier palabra que implique intención deshonesta ("engañar", "timar", "cobrar de más a propósito", "así se hace siempre" repetido o validado); habla de "un error en la cuenta" o "un cargo que no debería estar ahí", nunca de intención.
   - Nunca invites al cliente a escribir, contactar o resolverlo por otra vía. La prohibición es del ACTO de dejar una puerta abierta a seguimiento, no de una lista de frases: cubre tanto lo literal ("escríbenos", "contáctanos", "cuéntanoslo por privado") como cualquier metáfora o rodeo que signifique lo mismo ("la puerta siempre está abierta", "hablemos en persona", "quedamos a su disposición para lo que necesite", "no dude en decírnoslo"). Antes de cerrar la respuesta, comprueba si la última frase podría interpretarse como una invitación a seguir la conversación por cualquier canal — si es así, elimínala. La respuesta la gestiona una agencia externa, no el propio negocio, así que abrir esa puerta genera una expectativa de seguimiento que luego nadie puede cumplir. La respuesta se queda siempre en una disculpa sincera y humana, cerrada en sí misma.
   - ESCALA DE GRAVEDAD (si el caso encaja en varios niveles, aplica siempre el más alto):
     · LEVE — esperas moderadas, comida fría, ruido, un plato flojo, precio percibido como alto: disculpa cercana y humana, sin más, tono ligero, sin dramatizar.
     · MODERADA — trato brusco o seco sin llegar al insulto, error de comanda, cobro indebido o cargo no explicado: reconoce el malestar del cliente con firmeza, sin implicar intención deshonesta ni validar un patrón, sin repetir ninguna cifra o importe exacto que el cliente haya mencionado (ver regla de las cifras que no puedes verificar), compromiso genérico (no detallado) de revisar el cobro o el proceso.
     · GRAVE — insultos, trato humillante o vejatorio, insectos u otros hallazgos en la comida, sospecha de intoxicación, alérgenos mal gestionados: disculpa mucho más contundente en el reconocimiento del daño emocional o físico, sin confirmar la causa interna ni dar detalle operativo (ver regla de la verdad que no tienes). Si hay un menor implicado, redobla el cuidado: reconoce la gravedad para un niño sin entrar en ningún detalle médico ni de procedimiento interno.
     · Para GRAVE, el cierre invitando a "otra oportunidad" pasa a ser OPCIONAL: si pedir que vuelvan sonaría fuera de lugar justo después de ese relato, cierra reconociendo que lo entenderías si no lo hacen, en vez de forzar una invitación que suene insensible. Usa criterio.

=========================================================
BLINDAJE JURÍDICO AVANZADO — PREVALECE SOBRE CUALQUIER OTRA REGLA
=========================================================
Estas reglas están por encima de todo lo anterior. Si una de ellas choca con la naturalidad, la empatía o la longitud, GANA LA REGLA. Una respuesta algo más sosa es un problema menor; una respuesta que prueba algo contra el negocio es un problema que no tiene arreglo, porque queda publicada y permanente.

PRINCIPIO RECTOR (de aquí se derivan todas las demás):
Hay dos cosas que un cliente pone en una reseña y que se tratan de forma OPUESTA:
  a) Su EXPERIENCIA: lo que sintió, vivió, percibió, esperó, sufrió. → VALÍDALA SIN LÍMITE. Es lo que hace humana la respuesta.
  b) Los HECHOS y CAUSAS que afirma: qué pasó por dentro, por qué, quién falló, si es habitual, si se incumplió algo. → NUNCA los confirmes, ni siquiera de forma condicional, hipotética o implícita.
Casi todos los errores graves nacen de deslizarse de (a) a (b) sin darse cuenta, normalmente en la segunda mitad de una frase que empezó bien.

TEST OBLIGATORIO antes de dar por buena cada frase:
"Si esta frase se imprime y se pone delante de un inspector, un juez o el abogado del cliente, ¿prueba algo en contra del negocio?"
Si la respuesta es SÍ o QUIZÁ, reformula hasta que sea NO. Sin excepciones.

--- R1. REGLA DEL PATRÓN Y LA RECURRENCIA ---
Si el cliente sugiere que lo que cuenta no es aislado ("no es la primera vez", "siempre pasa lo mismo", "a más gente le pasó", "ya me lo habían dicho"), NUNCA confirmes, continúes ni amplíes esa idea de recurrencia. Reconocer que un problema es conocido y repetido es mucho más grave que reconocer un incidente suelto: implica que el negocio lo sabía y no lo corrigió.
✗ PROHIBIDO: "no es la primera vez que alguien se va sin poder sentarse", "sabemos que en horas punta esto ocurre", "es algo que nos han comentado más veces", "entiendo que no sea la primera vez que le pasa".
✓ CORRECTO: "nadie debería irse de una terraza llena sin haber podido sentarse", "lo que usted describe de esa tarde no es la experiencia que queremos dar".
Trata SIEMPRE lo narrado como referido a esa visita concreta, nunca como fenómeno general.
CUIDADO CON LA VERSIÓN DISFRAZADA — prometer que algo DEJE de ser habitual confirma, por elevación, que YA lo era. "Que esto no vuelva a ser la tónica", "que no se convierta en costumbre", "que deje de ser lo normal", "para que esto no sea el patrón" son tan graves como decir "sabemos que pasa a menudo", solo que con la lógica invertida: hablan del futuro pero confirman el pasado. La promesa de mejora va SIEMPRE sobre el caso concreto de este cliente, nunca enmarcada como corrección de una tendencia general.
✗ PROHIBIDO (versión disfrazada): "para que no vuelva a ser la tónica", "que no se repita como viene pasando", "trabajaremos para que deje de ser lo habitual".
✓ CORRECTO: "tomaré nota para revisarlo", "esto lo trasladaré para que se revise", sin ninguna palabra que implique que ya era costumbre.

--- R2. REGLA DEL JUICIO NORMATIVO ---
Prohibido emitir juicios sobre si el HECHO debía o no ocurrir, porque para juzgarlo hay que darlo por cierto. La prohibición no depende de las palabras exactas: cualquier construcción equivalente está igual de prohibida.
✗ PROHIBIDO: "eso no debería haber pasado", "es una situación que no debería haberse dado", "no tiene justificación posible", "es inaceptable que ocurriera", "nada de eso debería ocurrir en nuestro local".
✓ CORRECTO (juicio sobre el SENTIMIENTO, no sobre el hecho): "nadie debería sentirse así al sentarse a comer", "entiendo que irse con esa sensación resulte muy desagradable", "es un mal recuerdo que lamento que se lleve".
La diferencia: se puede decir que un SENTIMIENTO no debería producirse; nunca que un HECHO no debería haber ocurrido.

--- R3. REGLA DEL PERSONAL IDENTIFICABLE ---
Cuando el cliente atribuya una conducta a una persona concreta (por nombre, puesto, turno, sexo o cualquier dato que permita identificarla: "la camarera rubia", "el de seguridad", "la señora que parecía la dueña"), NUNCA des esa conducta por probada ni dirijas ninguna acción hacia esa persona. Solo has oído una versión. Además, anunciar públicamente una medida sobre un trabajador es material utilizable contra el negocio en un conflicto laboral, y potencialmente lesivo para el honor de esa persona.
✗ PROHIBIDO: "tomaré nota de lo ocurrido con la persona de seguridad", "hablaré con quien le atendió sobre su actitud", "esa persona no representa lo que somos", "tomaremos medidas con el responsable de sala".
✓ CORRECTO: "el trato que describe no es el que queremos que nadie reciba aquí", "revisaremos internamente cómo se está atendiendo en sala".
Nunca menciones sanciones, medidas disciplinarias, despidos, formación correctiva ni cambios de puesto de nadie.

--- R4. REGLA DEL INCUMPLIMIENTO NORMATIVO ---
Algunas quejas no son de calidad, son denuncias de incumplimiento de una obligación legal: precios no exhibidos o distintos a los anunciados, exceso de aforo, licencias, horarios, ruido, ocupación de terraza, salidas de emergencia, entrega de ticket o factura, cobro sin justificante. Confirmar el MECANISMO de una de estas equivale a confesar una infracción administrativa por escrito.
Prohibido especialmente la construcción condicional que parece prudente pero no lo es: "si en la carta pone un precio y luego se cobra otro, es lógico que...". Al explicar el mecanismo lo estás dando por plausible y comprometiéndote a corregirlo, que es tanto como admitirlo.
✗ PROHIBIDO: "si figura un precio base y luego se cobra otro según los complementos, el desconcierto es lógico", "entiendo que con la terraza llena la espera se dispare", "es cierto que no siempre se entrega el ticket".
✓ CORRECTO: "cualquier diferencia entre lo que se espera pagar y lo que se cobra merece revisarse, y nos aseguraremos de que la información sea clara", "lamento que la espera le resultara larga".
Regla práctica: habla del EFECTO en el cliente en términos genéricos; nunca reconstruyas ni expliques el mecanismo que lo causó.

--- R5. REGLA DEL DEFECTO SISTÉMICO ---
Nunca admitas que un producto, plato, lote o proceso sale mal de forma habitual. Un incidente aislado es un mal día; un defecto sistémico es un problema de calidad reconocido por escrito, con implicaciones sanitarias y de consumo.
✗ PROHIBIDO: "no me conformo con que salgan así de la cocina", "esos gofres no están saliendo bien últimamente", "ese plato nos está dando problemas", "revisaremos el lote".
✓ CORRECTO: "siento que no le convenciera ni en textura ni en sabor; lo comentaré con cocina".

--- R6. REGLA DE LAS ACUSACIONES DE DISCRIMINACIÓN ---
Si el cliente denuncia trato discriminatorio (racismo, xenofobia, homofobia, machismo, aspecto físico, discapacidad, edad, idioma), aplica el máximo cuidado. Hay TRES salidas prohibidas, y la tercera es la peor:
  1) Confirmarlo (admisión de un hecho especialmente grave).
  2) Negarlo con contundencia o contraatacar (suena defensivo y agrava el conflicto).
  3) JUSTIFICARLO o explicarlo ("había mucha gente", "hubo un malentendido", "seguro que no fue su intención"). Explicar el porqué de una discriminación percibida es la peor respuesta posible: parece que se minimiza.
✓ CORRECTO: reconocer la seriedad de que alguien se sienta así, afirmar en positivo y en general el principio de trato igualitario, y no entrar en el caso. Ejemplo de registro: "Que alguien se marche sintiéndose tratado de forma desigual es algo que me importa mucho. Aquí queremos que cualquier persona que entre por la puerta reciba exactamente el mismo trato, sin excepción. Lamento que usted no lo percibiera así."
Nunca uses las palabras "racismo", "discriminación", "homofobia" ni etiquetas equivalentes en la respuesta: reproducirlas las fija en el hilo público.

--- R7. REGLA DE PROTECCIÓN DE DATOS ---
La respuesta es pública y el negocio no puede revelar datos de un cliente en ella, ni siquiera para defenderse. Prohibido de forma absoluta:
  - Confirmar que esa persona estuvo en el local, con quién, qué día o a qué hora.
  - Mencionar qué consumió, cuánto pagó, si tenía reserva, si hubo incidencia previa.
  - Contradecirle con datos internos ("en realidad usted vino el día X", "consta que se le atendió a las Y", "según nuestro registro pidió Z").
  - Cualquier dato de salud, alergia, embarazo, discapacidad o condición personal que él mismo haya mencionado: aunque lo haya hecho público, tú no lo repites.
  - Nombres propios de clientes o de empleados.
Nunca corrijas la versión del cliente con información del negocio, por muy equivocado que esté. Si su relato es inexacto, la respuesta se limita a lamentar la experiencia sin entrar a rebatir.

--- R8. REGLA DE LESIONES Y DAÑOS FÍSICOS ---
Si alguien resultó herido, se puso enfermo o sufrió un daño material, valida el susto y la preocupación con toda la humanidad posible, pero NUNCA reconozcas los hechos ni su causa. Reconocer responsabilidad por escrito puede además comprometer la cobertura de la póliza de responsabilidad civil del negocio.
✗ PROHIBIDO: "lamentamos el corte que sufrió con el cristal", "sentimos que la comida le sentara mal", "asumimos lo ocurrido", y toda mención a seguros, partes, coberturas, indemnizaciones o responsabilidad.
✓ CORRECTO: "que alguien lo pase mal en un sitio al que ha venido a disfrutar es lo último que queremos, y lamento de veras el mal rato", con la coletilla condicional cuando encaje ("si así fue").

--- R9. REGLA DE ALÉRGENOS, HIGIENE E INTOXICACIÓN ---
La categoría de mayor riesgo. Nunca confirmes ni niegues: la composición de un plato, la presencia o ausencia de un alérgeno, una contaminación cruzada, un hallazgo (insecto, pelo, cuerpo extraño), el estado de conservación de un producto o la existencia de un problema de higiene.
No confirmar es evidente. NEGAR también está prohibido: una negativa categórica puede desmentirse después y convierte un incidente en un engaño.
✓ ÚNICA SALIDA CORRECTA: validar la impresión y el malestar, con matiz condicional, sin pronunciarse sobre el hecho. Ejemplo: "Entiendo perfectamente la impresión que eso le causó, y lamento que se llevara esa imagen del local."
Prohibido usar los términos técnicos "intoxicación", "contaminación", "higiene alimentaria", "cadena de frío", "alérgeno", "sanidad", "inspección" — incluso para negarlos.

--- R10. REGLA DE NO COMPENSAR EN PÚBLICO ---
Nunca ofrezcas ni insinúes devolución, invitación, descuento, obsequio ni compensación de ningún tipo. La respuesta la leen miles de personas: una compensación ofrecida por escrito crea una expectativa exigible y un efecto llamada inmediato.
✗ PROHIBIDO: "le invitamos a la próxima", "le devolveremos el importe", "queremos compensarle", "la próxima visita corre de nuestra cuenta".
Tampoco prometas resultados verificables ni plazos ("esta misma semana", "a partir de mañana ya no ocurrirá"): un compromiso con fecha es un compromiso incumplible que alguien puede reprochar después. Los compromisos van siempre en genérico y sin calendario.

--- R11. REGLA DE TERCEROS ---
Si el cliente cuenta lo que le pasó a otras personas (su acompañante, otras mesas, "más de uno tuvo problemas"), nunca lo confirmes ni lo integres como hecho. Solo puedes responder por la experiencia de quien escribe.
✗ PROHIBIDO: "sentimos que también otras mesas tuvieran que esperar", "lamentamos lo que le ocurrió a su amiga con el personal".
✓ CORRECTO: reconducir a la experiencia del autor de la reseña, con una mención empática genérica si hace falta.

--- R12. REGLA DE MENORES Y ALCOHOL ---
Nunca confirmes, comentes ni des detalle sobre control de edad, acceso de menores, consumo de alcohol por menores o cualquier cuestión de protección de la infancia. Si aparece, la respuesta se limita a un reconocimiento sobrio y muy breve de la seriedad del asunto, sin entrar en absoluto en el fondo, sin describir protocolos y sin prometer medidas concretas.

--- R13. LÉXICO JURÍDICO PROHIBIDO ---
Estas palabras encuadran el intercambio en clave legal y no deben aparecer nunca, ni siquiera para rechazarlas: negligencia, responsabilidad (en sentido jurídico), culpa, indemnización, daños y perjuicios, denuncia, reclamación formal, seguro, póliza, abogado, inspección, sanción, expediente, prueba, testigo.
Habla siempre en lenguaje corriente de hostelería y trato al cliente.

--- R15. REGLA DE LA EXCUSA REGALADA ---
Un cliente que ofrece su propia excusa para lo ocurrido ("entiendo que tuvierais mucho trabajo", "seguro que fue un día complicado", "no os culpo, pero...") es MÁS peligroso que uno hostil, no menos: la excusa suena razonable, agradecerla parece de buena educación, y es fácil deslizarse a confirmarla sin darse cuenta. Da igual lo plausible o halagador que sea el motivo que el cliente proponga (mucho volumen de trabajo, personal reducido, un producto que "tiene sus tiempos", un imprevisto): NUNCA lo confirmes, actúa como si no lo hubiera dicho. Confirmar la excusa es admitir la causa exactamente igual que si el negocio la hubiera dado por iniciativa propia.
✗ PROHIBIDO: "hay días en que la cocina y la sala van a tope", "aunque el ritmo de las brasas tiene sus tiempos", "es cierto que ese día teníamos poco personal", "tiene razón, fue una noche complicada".
✓ CORRECTO: agradecer el tono comprensivo del cliente sin validar el motivo que propone. "Le agradezco que lo cuente con esa comprensión, pero eso no quita que la espera no fuera lo que usted merecía" — nunca "y es verdad que..." a continuación.
Puedes agradecer LA ACTITUD del cliente (que sea comprensivo, que no cargue las tintas); nunca el CONTENIDO de la excusa que ofrece.

--- R16. VERIFICACIÓN FINAL OBLIGATORIA ---
Antes de emitir el JSON, relee tu propia respuesta frase por frase y comprueba:
  1. ¿Confirmo en algún punto QUÉ pasó por dentro, no solo cómo se sintió el cliente? → reformular.
  2. ¿Doy por hecho que algo es habitual o recurrente? (R1) → reformular.
  3. ¿Juzgo el hecho en vez del sentimiento? (R2) → reformular.
  4. ¿Dirijo alguna acción hacia una persona identificable? (R3) → reformular.
  5. ¿Explico el mecanismo de un posible incumplimiento? (R4) → reformular.
  6. ¿Repito alguna cifra, importe, fecha o dato exacto del cliente? → eliminar.
  7. ¿Aparece alguna palabra del léxico jurídico prohibido? (R13) → sustituir.
  8. ¿Ofrezco compensación, plazo o resultado verificable? (R10) → eliminar.
  9. ¿Revelo algún dato del cliente o contradigo su versión con datos internos? (R7) → eliminar.
  10. ¿He confirmado, aceptado o dado por buena alguna excusa u motivo que el propio cliente ofreció? (R15) → reformular.
  11. ¿La última frase (o cualquier otra) podría leerse como una invitación a seguir la conversación por cualquier vía, aunque sea con una metáfora ("puerta abierta", "hablemos en persona")? → eliminar.
Solo cuando las once respuestas sean limpias, emites la respuesta. Esta verificación es interna: no la menciones ni la incluyas en la salida.

REGLAS DE LONGITUD:
- POSITIVA: entre 60 y 100 palabras.
- NEGATIVA: entre 140 y 200 palabras como rango habitual, desarrollando: (a) reconocimiento genuino de UN aspecto concreto, sin confirmar causa interna, (b) validación breve de lo que sintió el cliente, (c) qué se va a hacer al respecto, contado en términos humanos y genéricos, nunca como un procedimiento técnico, (d) cierre cordial invitando a otra oportunidad — omisible en casos GRAVES. Sin frases vacías repetidas.
- EXCEPCIÓN CONTROLADA: si la reseña describe genuinamente varios problemas graves y distintos entre sí y resumirlos en 200 palabras obligaría a ignorar alguno o a listarlos de forma fría, se permite ampliar hasta un máximo de 280 palabras — nunca más. Esta excepción es solo para casos que de verdad lo justifiquen.
- Nunca fuerces el límite superior si la reseña es muy breve y no lo justifica.
- LA BREVEDAD ES BLINDAJE: en los casos que caen bajo las reglas R6 (discriminación), R8 (lesiones), R9 (alérgenos e higiene) o R12 (menores), acorta deliberadamente a 90-140 palabras. Cada frase de más sobre un asunto delicado es superficie de exposición añadida. Una respuesta corta, humana y sobria es SIEMPRE preferible a una larga y bienintencionada: el impulso de explicarse es exactamente lo que produce las frases que comprometen. Di menos.

REGLAS COMUNES:
- Integra el nombre del negocio de forma fluida, una sola vez si es posible.
- Sin asteriscos, comillas externas, emojis (salvo lo indicado en la guía de tono) ni encabezados."""


# =========================================================
# 🏢 CABECERA DE MARCA BLANCA
# =========================================================
# =========================================================
# BARRA LATERAL — navegación, local activo y cuenta
# =========================================================
# Antes todo esto era una cabecera horizontal + cinco pestañas. El problema
# no era estético: con pestañas, el local activo y el plan quedaban fuera de
# vista al cambiar de sección, y no había forma de saber dónde estabas sin
# mirar arriba. Una barra lateral persistente mantiene visible el contexto
# (qué local, qué plan, cuánto te queda) mientras trabajas.

with st.sidebar:
    # ---- Identidad de la agencia ----
    st.image(agencia["logo_url"], use_container_width=True)

    # _html.escape() en todo dato que venga de la base de datos. nombre_agencia
    # lo escribe el propio usuario en el alta y no se valida: sin escapar, un
    # admin de agencia puede inyectar <script> y ejecutarlo en el navegador de
    # todos los gestores de su equipo.
    st.markdown(
        f"<div class='rs-marca'>{_html.escape(agencia['nombre_agencia'])}</div>",
        unsafe_allow_html=True,
    )

    with st.popover("Cambiar logo", use_container_width=True):
        st.caption(
            "PNG o JPG, a poder ser con fondo transparente. Se usará tanto en la "
            "app como en los informes PDF de marca blanca."
        )
        archivo_logo = st.file_uploader(
            "Sube tu logo", type=["png", "jpg", "jpeg"],
            key="uploader_logo_agencia", label_visibility="collapsed",
        )
        if archivo_logo is not None:
            st.image(archivo_logo, width=160, caption="Vista previa")
            if st.button("Guardar logo", key="guardar_logo_agencia", type="primary", use_container_width=True):
                try:
                    extension = archivo_logo.name.rsplit(".", 1)[-1].lower()
                    ruta_storage = f"{agencia['id']}.{extension}"
                    content_type = archivo_logo.type or "image/png"
                    supabase.storage.from_("logos").upload(
                        ruta_storage,
                        archivo_logo.getvalue(),
                        file_options={"content-type": content_type, "upsert": "true"},
                    )
                    resultado_url = supabase.storage.from_("logos").get_public_url(ruta_storage)
                    nueva_url = (
                        resultado_url if isinstance(resultado_url, str)
                        else (resultado_url.get("publicUrl") or resultado_url.get("publicURL"))
                    )
                    nueva_url = f"{nueva_url}?v={int(datetime.utcnow().timestamp())}"
                    supabase.table("agencias").update({"logo_url": nueva_url}).eq("id", agencia["id"]).execute()
                    st.session_state.agencia_actual["logo_url"] = nueva_url
                    st.success("Logo actualizado.")
                    st.rerun()
                except Exception as e:
                    st.error(redactar_secretos(f"No se pudo actualizar el logo: {e}"))

    st.markdown("<div class='rs-sep'></div>", unsafe_allow_html=True)

    # ---- Local activo: visible SIEMPRE, en todas las secciones ----
    _locales_barra = st.session_state.locales_agencia or []
    if _locales_barra:
        st.markdown("<div class='rs-lbl'>Local activo</div>", unsafe_allow_html=True)
        _nombres_barra = [l["nombre"] for l in _locales_barra]
        _elegido_barra = st.selectbox(
            "Local activo",
            options=_nombres_barra,
            key="selector_local_activo",
            label_visibility="collapsed",
        )
        local_activo = next(l for l in _locales_barra if l["nombre"] == _elegido_barra)
        st.session_state.local_activo = local_activo
        st.markdown(
            f"<div class='rs-meta'>{local_activo.get('nicho','—')}"
            + (f" · {local_activo['ciudad']}" if local_activo.get("ciudad") else "")
            + "</div>",
            unsafe_allow_html=True,
        )
    else:
        local_activo = None
        st.info("Crea tu primer establecimiento para empezar.")

    st.markdown("<div class='rs-sep'></div>", unsafe_allow_html=True)

    # ---- Navegación ----
    st.markdown("<div class='rs-lbl'>Secciones</div>", unsafe_allow_html=True)
    _plan_menu = agencia.get("plan", "free")
    if _plan_menu in ("individual", "free"):
        SECCIONES = [
            "Mi asistente",
            "Responder reseña",
            "Pedir reseñas",
            "Contenido SEO",
            "Analítica",
            "Guía de uso",
        ]
    else:
        # Starter, Growth: el flujo diario es responder reseñas.
        # El asistente está disponible pero no ocupa el primer lugar.
        SECCIONES = [
            "Responder reseña",
            "Mi asistente",
            "Pedir reseñas",
            "Contenido SEO",
            "Analítica",
            "Guía de uso",
        ]
    vista_activa = st.radio(
        "Navegación",
        options=SECCIONES,
        key="nav_seccion",
        label_visibility="collapsed",
    )

    st.markdown("<div class='rs-sep'></div>", unsafe_allow_html=True)

    # ---- Estado del plan: siempre a la vista, no escondido en una pestaña ----
    _plan_barra = agencia.get("plan", "free")
    _nombre_plan_barra = PLANES_AUTOSERVICIO.get(_plan_barra, {}).get(
        "nombre", _plan_barra.capitalize()
    )
    _limite_barra = LIMITE_USOS_POR_PLAN.get(_plan_barra)

    # Una sola consulta, reutilizada por el texto y por la barra de progreso.
    # Antes se llamaba a contar_usos_del_mes() dos veces por rerun.
    _usos_barra = None if agencia_en_beta(agencia) else contar_usos_del_mes(agencia["id"])

    if _usos_barra is None:
        _uso_txt = "Beta · sin límite"
    elif _limite_barra is None:
        _uso_txt = f"{_usos_barra} respuestas este mes"
    else:
        _uso_txt = f"{_usos_barra} de {_limite_barra} este mes"

    st.markdown(
        f"<div class='rs-plan'><b>{_nombre_plan_barra}</b><span>{_uso_txt}</span></div>",
        unsafe_allow_html=True,
    )

    if _limite_barra is not None and _usos_barra is not None:
        st.progress(min(1.0, _usos_barra / max(1, _limite_barra)))

    if st.button("Ver planes", use_container_width=True, key="barra_ver_planes"):
        st.session_state.mostrar_pagina_planes = True
        st.rerun()

    st.markdown("<div class='rs-sep'></div>", unsafe_allow_html=True)

    # ---- Cuenta ----
    st.markdown(
        f"<div class='rs-cuenta'>{_html.escape(usuario['nombre_usuario'])}"
        f"<span>{_html.escape(usuario['email'])} · "
        f"{_html.escape(usuario['rol'])}</span></div>",
        unsafe_allow_html=True,
    )

    _customer_id_cuenta = agencia.get("stripe_customer_id")
    if _customer_id_cuenta:
        if st.button("Gestionar suscripción", use_container_width=True, key="barra_stripe"):
            _url_portal = crear_portal_cliente(_customer_id_cuenta)
            if _url_portal:
                boton_enlace_stripe("Ir al portal de Stripe →", _url_portal)

    if st.button("Cerrar sesión", use_container_width=True, key="barra_salir"):
        _revocar_token_sesion()
        for key in ["sesion_activa", "usuario_actual", "agencia_actual", "locales_agencia", "local_activo"]:
            st.session_state[key] = False if key == "sesion_activa" else None if "actual" in key else []
        st.session_state.vista_landing = "info"
        st.rerun()



# =========================================================
# GESTIÓN DE EQUIPO — panel solo para administradores
# =========================================================
if usuario.get("rol") == "admin":
    plan_actual_equipo = agencia.get("plan", "free")
    limite_usuarios = LIMITE_USUARIOS_POR_PLAN.get(plan_actual_equipo)
    miembros = listar_usuarios_agencia(agencia["id"])
    activos = [m for m in miembros if m.get("activo", True)]

    if limite_usuarios is None:
        etiqueta_equipo = f"Equipo · {len(activos)} usuarios (sin límite)"
    else:
        etiqueta_equipo = f"Equipo · {len(activos)} de {limite_usuarios} usuarios"

    with st.expander(etiqueta_equipo, expanded=False):
        if plan_actual_equipo in ("free", "individual"):
            st.info("Tu plan actual es de un solo usuario. Los planes de agencia (Starter, "
                    "y Growth) permiten dar acceso a varias personas del equipo bajo "
                    "la misma cuenta, cada una con su propio email y contraseña.")
            if st.button("Ver planes de agencia", key="equipo_ver_planes"):
                st.session_state.mostrar_pagina_planes = True
                st.rerun()
        else:
            st.caption("Da acceso a las personas de tu agencia. Cada miembro entra con su propio "
                       "email y contraseña, y trabaja sobre los mismos locales y datos.")

            # Lista de miembros actuales
            st.markdown("**Miembros del equipo**")
            for m in activos:
                col_m1, col_m2, col_m3 = st.columns([3, 1.4, 1.2])
                with col_m1:
                    st.markdown(
                        f"{_html.escape(m['nombre_usuario'])}  \n"
                        f"<span style='color:#6b7280; font-size:0.82rem;'>"
                        f"{_html.escape(m['email'])}</span>",
                        unsafe_allow_html=True,
                    )
                with col_m2:
                    es_tu = m["id"] == usuario["id"]
                    etiqueta_rol = "Administrador" if m.get("rol") == "admin" else "Gestor"
                    st.caption(etiqueta_rol + (" · tú" if es_tu else ""))
                with col_m3:
                    # No se puede desactivar a uno mismo ni al último admin.
                    otros_admins = [x for x in activos if x.get("rol") == "admin" and x["id"] != m["id"]]
                    if m["id"] != usuario["id"] and (m.get("rol") != "admin" or otros_admins):
                        if st.button("Quitar acceso", key=f"quitar_{m['id']}"):
                            ok, motivo = desactivar_usuario(m["id"], agencia["id"])
                            if ok:
                                st.success(f"Se ha revocado el acceso a {m['nombre_usuario']}.")
                                st.rerun()
                            else:
                                st.error(redactar_secretos(motivo))

            st.divider()

            # Alta de nuevo miembro
            puede_anadir, motivo_limite = puede_agencia_anadir_usuario(agencia)
            if not puede_anadir:
                st.warning(motivo_limite)
                if st.button("Actualizar plan", key="equipo_upgrade"):
                    st.session_state.mostrar_pagina_planes = True
                    st.rerun()
            else:
                st.markdown("**Añadir un nuevo miembro**")
                nuevo_nombre = st.text_input("Nombre del miembro", key="nuevo_miembro_nombre")
                nuevo_email = st.text_input("Email", key="nuevo_miembro_email", placeholder="persona@tuagencia.com")
                nuevo_pass = st.text_input("Contraseña temporal (mín. 8 caracteres)", type="password", key="nuevo_miembro_pass")
                nuevo_rol = st.radio("Rol", ["Gestor", "Administrador"], horizontal=True, key="nuevo_miembro_rol",
                                     help="Los administradores pueden gestionar el equipo y la facturación; los gestores solo generan respuestas y contenido.")
                if st.button("Añadir miembro", key="crear_miembro", type="primary"):
                    if not nuevo_email.strip() or not nuevo_pass:
                        st.warning("Rellena al menos el email y la contraseña.")
                    else:
                        rol_bd = "admin" if nuevo_rol == "Administrador" else "gestor"
                        ok, resultado = crear_usuario_en_agencia(
                            agencia["id"], nuevo_email, nuevo_pass, nuevo_nombre, rol_bd
                        )
                        if ok:
                            st.success(f"{resultado['nombre_usuario']} ya puede iniciar sesión con su email y la contraseña que le has asignado.")
                            st.rerun()
                        else:
                            st.error(redactar_secretos(resultado))

# =========================================================
# 🧭 NAVEGACIÓN: GENERAR RESPUESTA / VER ANALÍTICA
# =========================================================
# =========================================================
# SECCIONES
# =========================================================
# Antes eran st.tabs. El cambio a navegación lateral no es cosmético: con
# pestañas, Streamlit RENDERIZA EL CONTENIDO DE LAS CINCO en cada
# re-ejecución (las oculta con CSS, pero las calcula). Eso significaba
# lanzar las consultas de analítica y recorrer el histórico aunque
# estuvieras escribiendo una respuesta. Con un if, solo se ejecuta la
# sección que estás viendo: menos memoria, menos consultas, más rápido.

if vista_activa == "Guía de uso":
    mostrar_guia_uso()

# ---------------------------------------------------------
# PESTAÑA 1: GENERACIÓN DE RESPUESTAS
# ---------------------------------------------------------
if vista_activa == "Mi asistente":
    st.subheader("Tu asistente de crecimiento")

    _locales_asist = st.session_state.locales_agencia
    if not _locales_asist:
        st.info(
            "Primero añade tu negocio en **Responder reseña → Añadir establecimiento**. "
            "En cuanto lo tengas, tu asistente podrá leer tus reseñas y ayudarte a crecer."
        )
    else:
        local_asist = st.session_state.local_activo or _locales_asist[0]

        st.caption(
            f"Analizo los datos reales de **{local_asist['nombre']}**: tus reseñas, tu Ficha "
            "verificada y tu reputación. Pregúntame cómo mejorar, qué publicar o cómo atraer "
            "más clientes. Nadie más conoce tu negocio como yo."
        )

        # Estado de la conversación, aislado por local (cambiar de local = hilo nuevo).
        _clave_hist = f"_chat_asist_{local_asist['id']}"
        _clave_brief = f"_brief_asist_{local_asist['id']}"
        if _clave_hist not in st.session_state:
            st.session_state[_clave_hist] = []   # [(rol, texto)] para pintar
        if f"{_clave_hist}_api" not in st.session_state:
            st.session_state[f"{_clave_hist}_api"] = []  # historial en formato API

        _ctx_agente = construir_ctx_agente(local_asist)

        # --- Briefing proactivo: el gancho de "esta semana ha pasado esto" ---
        # Se genera una vez por sesión y local, al abrir. Es lo que hace que el
        # usuario entre cada día aunque no venga con una pregunta concreta.
        if _clave_brief not in st.session_state:
            with st.spinner("Revisando tu negocio..."):
                st.session_state[_clave_brief] = motor_agente.generar_briefing(client, _ctx_agente)

        st.markdown(
            f"<div class='rs-riesgo' style='border-left-color:var(--er-accent);"
            f"background:var(--er-accent-bg);border-color:var(--er-accent)'>"
            f"<b>Esto es lo que veo esta semana:</b><br>{st.session_state[_clave_brief]}</div>",
            unsafe_allow_html=True,
        )

        # --- Sugerencias de arranque (para que no se enfrente a un chat vacío) ---
        if not st.session_state[_clave_hist]:
            st.markdown("**Prueba a preguntarme:**")
            _sugerencias = [
                "¿Cómo va mi reputación este mes?",
                "¿De qué se queja más la gente?",
                "¿Qué debería publicar esta semana?",
                "Créame un post para Google con lo mejor de mi negocio",
            ]
            _cols_sug = st.columns(2)
            for _i, _sug in enumerate(_sugerencias):
                if _cols_sug[_i % 2].button(_sug, key=f"sug_asist_{_i}", use_container_width=True):
                    st.session_state[f"_pregunta_pendiente_{local_asist['id']}"] = _sug
                    st.rerun()

        # --- Pintar el historial de la conversación ---
        for _rol, _txt in st.session_state[_clave_hist]:
            with st.chat_message("user" if _rol == "user" else "assistant"):
                st.markdown(_txt)

        # --- Entrada del usuario (chat_input o botón de sugerencia) ---
        _pregunta = st.chat_input("Escribe tu pregunta...")
        _pendiente_key = f"_pregunta_pendiente_{local_asist['id']}"
        if not _pregunta and _pendiente_key in st.session_state:
            _pregunta = st.session_state.pop(_pendiente_key)

        if _pregunta:
            # Comprobar el tope mensual de mensajes (anti-abuso).
            _usados = contar_mensajes_asistente_mes(local_asist["id"])
            if _usados >= LIMITE_MENSAJES_ASISTENTE_MES:
                st.warning(
                    f"Has alcanzado el límite de {LIMITE_MENSAJES_ASISTENTE_MES} consultas al "
                    "asistente este mes. Se renueva el día 1. Si te quedas corto de forma "
                    "recurrente, escríbenos y lo vemos."
                )
            else:
                # Pintar la pregunta del usuario.
                with st.chat_message("user"):
                    st.markdown(_pregunta)
                st.session_state[_clave_hist].append(("user", _pregunta))

                # Añadir al historial de API y llamar al agente.
                _hist_api = st.session_state[f"{_clave_hist}_api"]
                _hist_api.append({"role": "user", "content": _pregunta})

                with st.chat_message("assistant"):
                    _hueco = st.empty()
                    _hueco.markdown("_Consultando tus datos..._")

                    def _feedback(nombre_tool):
                        _textos = {
                            "ver_resumen_reputacion": "Mirando tu Reputation Score...",
                            "leer_resenas_recientes": "Leyendo tus reseñas...",
                            "detectar_temas": "Buscando patrones en lo que dicen tus clientes...",
                            "ver_ficha_verificada": "Revisando tu Ficha verificada...",
                            "ver_keywords": "Consultando tus palabras clave...",
                            "generar_contenido": "Redactando tu contenido...",
                        }
                        _hueco.markdown(f"_{_textos.get(nombre_tool, 'Trabajando...')}_")

                    _respuesta, _hist_api = motor_agente.responder_agente(
                        client, _hist_api, _ctx_agente, on_tool=_feedback
                    )
                    _hueco.markdown(_respuesta)

                st.session_state[f"{_clave_hist}_api"] = _hist_api
                st.session_state[_clave_hist].append(("assistant", _respuesta))
                registrar_mensaje_asistente(agencia["id"], local_asist["id"], usuario["id"])
                st.rerun()


if vista_activa == "Responder reseña":
    locales_disponibles = st.session_state.locales_agencia

    # ---- Añadir un nuevo establecimiento (respetando el límite del plan) ----
    limite_locales = LIMITE_LOCALES_POR_PLAN.get(agencia.get("plan", "growth"))
    texto_limite = "sin límite" if limite_locales is None else f"{len(locales_disponibles)}/{limite_locales}"
    with st.expander(f"Añadir establecimiento ({texto_limite})"):
        # Si el botón "Añadir seleccionadas" dejó un valor pendiente en el
        # ciclo anterior, se aplica AQUÍ, antes de crear el widget. Streamlit
        # no permite escribir en session_state[key] de un widget después de
        # que ese widget ya se haya instanciado en la misma ejecución — por
        # eso el intento de hacerlo dentro del propio botón (más abajo)
        # lanzaba StreamlitAPIException. Aplicarlo antes de la línea del
        # text_input es la única forma correcta de precargar un valor.
        if "_kw_pendiente_nuevo" in st.session_state:
            st.session_state["nuevo_local_keywords"] = st.session_state.pop("_kw_pendiente_nuevo")

        nombre_nuevo_local = st.text_input("Nombre del establecimiento", key="nuevo_local_nombre")
        nicho_nuevo_local = st.text_input("Nicho (ej: hotel, restaurante, clínica dental)", key="nuevo_local_nicho")
        ciudad_nuevo_local = st.text_input("Ciudad o zona (ej: Sevilla, o Triana, Sevilla)", key="nuevo_local_ciudad",
                                            help="Clave para el SEO local: permite generar contenido tipo 'mejor restaurante en Sevilla'.")
        keywords_nuevo_local = st.text_input("Palabras clave SEO, separadas por comas", key="nuevo_local_keywords")

        # ---- Sugeridor de keywords -------------------------------------
        # Se coloca aquí, y no en el panel de agencia, porque las keywords
        # pertenecen a cada local: una agencia puede llevar una clínica en
        # Madrid y un restaurante en Sevilla, y no comparten ni un término.
        # Este formulario ya pide nicho y ciudad, que es justo el contexto
        # que necesita el modelo para proponer algo útil.
        _nicho_actual = (nicho_nuevo_local or "").strip()
        if st.button("Sugerir palabras clave con IA",
                     key="btn_sugerir_kw_nuevo",
                     use_container_width=True,
                     disabled=not _nicho_actual,
                     help="Escribe primero el nicho del negocio." if not _nicho_actual else None):
            with st.spinner("Analizando el sector…"):
                st.session_state["_kw_sugeridas_nuevo"] = sugerir_keywords_seo(
                    client, _nicho_actual, ciudad_nuevo_local, nombre_nuevo_local
                )
            # Al pedir sugerencias nuevas se limpian las marcas anteriores, o
            # arrastraríamos selecciones de un nicho que ya no aplica.
            for _k in [k for k in st.session_state if k.startswith("_kwsel_nuevo_")]:
                del st.session_state[_k]

        _sugeridas = st.session_state.get("_kw_sugeridas_nuevo") or []
        if _sugeridas:
            st.caption(
                "Marca las que encajen con el negocio y pulsa «Añadir seleccionadas». "
                "Están agrupadas por tipo de búsqueda."
            )
            _explicacion = {
                "LOCAL":     "Servicio + zona. Poco volumen, pero es quien más reserva.",
                "SERVICIO":  "Lo que la persona quiere resolver, en sus palabras.",
                "PROBLEMA":  "Cómo lo busca quien no conoce la jerga del sector.",
                "CONFIANZA": "Búsquedas de quien ya está comparando y a punto de decidir.",
            }
            for _fam in ("LOCAL", "SERVICIO", "PROBLEMA", "CONFIANZA"):
                _grupo = [k for k in _sugeridas if k["familia"] == _fam]
                if not _grupo:
                    continue
                st.markdown(f"**{_fam.capitalize()}** — {_explicacion[_fam]}")
                for _i, _kw in enumerate(_grupo):
                    st.checkbox(
                        _kw["termino"],
                        key=f"_kwsel_nuevo_{_fam}_{_i}",
                        help=_kw["motivo"] or None,
                    )

            if st.button("Añadir seleccionadas", key="btn_aplicar_kw_nuevo", use_container_width=True):
                _marcadas = []
                for _fam in ("LOCAL", "SERVICIO", "PROBLEMA", "CONFIANZA"):
                    _grupo = [k for k in _sugeridas if k["familia"] == _fam]
                    for _i, _kw in enumerate(_grupo):
                        if st.session_state.get(f"_kwsel_nuevo_{_fam}_{_i}"):
                            _marcadas.append(_kw["termino"])

                if not _marcadas:
                    st.warning("No has marcado ninguna.")
                else:
                    # Se respeta lo que el usuario ya hubiera escrito a mano y
                    # se evitan duplicados: el campo es la fuente de verdad, no
                    # la lista de sugerencias.
                    _ya = [k.strip() for k in (keywords_nuevo_local or "").split(",") if k.strip()]
                    _final = _ya + [k for k in _marcadas if k not in _ya]
                    st.session_state["_kw_pendiente_nuevo"] = ", ".join(_final)
                    del st.session_state["_kw_sugeridas_nuevo"]
                    st.rerun()

        if st.button("Crear establecimiento", key="crear_establecimiento_btn"):
            puede, motivo = puede_agencia_anadir_local(agencia, locales_disponibles)
            if not puede:
                st.error(redactar_secretos(motivo))
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
                        "ciudad": ciudad_nuevo_local.strip() or None,
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

    # El selector de local vive ahora en la barra lateral, donde permanece
    # visible en todas las secciones. Aquí solo se recoge lo que ya eligió.
    local_activo = st.session_state.local_activo or locales_disponibles[0]

    # Mismo motivo que en el formulario de creación: hay que aplicar el valor
    # pendiente ANTES de crear el text_input de keywords, o Streamlit lanza
    # StreamlitAPIException al intentar escribirlo desde el botón de abajo.
    # La key pendiente lleva el id del local para no mezclar el valor de un
    # local con el de otro si el usuario cambia de establecimiento entre medias.
    _kw_pendiente_key = f"_kw_pendiente_edit_{local_activo['id']}"
    if _kw_pendiente_key in st.session_state:
        st.session_state[f"edit_keywords_{local_activo['id']}"] = st.session_state.pop(_kw_pendiente_key)

    # --- Editar info del local (ciudad, nicho, keywords) ---
    # Streamlit conserva en el cliente si un expander está abierto, así que
    # pasarle expanded=False tras guardar NO lo cierra. El truco fiable es
    # cambiar la identidad del widget: al alternar un carácter invisible en la
    # etiqueta, Streamlit lo trata como un expander nuevo y lo monta cerrado.
    # Así, al guardar, el panel se cierra solo y el operador no tiene que
    # plegarlo a mano.
    _ver_ficha = st.session_state.get("_ficha_ver", 0)
    _etiqueta_editar = "Editar info del local" + ("\u200b" * _ver_ficha)
    with st.expander(_etiqueta_editar, expanded=False):
        st.caption("Actualiza la ciudad, nicho y palabras clave del local para mejorar la potencia SEO del contenido generado.")
        col_edit1, col_edit2 = st.columns(2)
        with col_edit1:
            nicho_edit = st.text_input("Nicho", value=local_activo.get("nicho", ""), key=f"edit_nicho_{local_activo['id']}")
        with col_edit2:
            ciudad_edit = st.text_input("Ciudad o zona (ej: Sevilla, o Triana, Sevilla)",
                                        value=local_activo.get("ciudad") or "",
                                        key=f"edit_ciudad_{local_activo['id']}",
                                        help="Clave para SEO local: permite generar 'mejor [nicho] en [ciudad]'.")
        keywords_edit = st.text_input("Palabras clave SEO, separadas por comas",
                                      value=", ".join(local_activo.get("seo_keywords", [])),
                                      key=f"edit_keywords_{local_activo['id']}")

        # ---- Sugeridor de keywords (local ya existente) -----------------
        # Las claves de session_state llevan el id del local para que las
        # sugerencias de un local no se mezclen con las de otro al cambiar de
        # establecimiento en la barra lateral.
        _lid = local_activo["id"]
        _nicho_e = (nicho_edit or "").strip()
        if st.button("Sugerir palabras clave con IA",
                     key=f"btn_sugerir_kw_edit_{_lid}",
                     use_container_width=True,
                     disabled=not _nicho_e,
                     help="Rellena primero el nicho." if not _nicho_e else None):
            with st.spinner("Analizando el sector…"):
                st.session_state[f"_kw_sug_edit_{_lid}"] = sugerir_keywords_seo(
                    client, _nicho_e, ciudad_edit, local_activo.get("nombre")
                )
            for _k in [k for k in st.session_state if k.startswith(f"_kwsel_edit_{_lid}_")]:
                del st.session_state[_k]

        _sug_e = st.session_state.get(f"_kw_sug_edit_{_lid}") or []
        if _sug_e:
            _expl_e = {
                "LOCAL":     "Servicio + zona. Poco volumen, pero es quien más reserva.",
                "SERVICIO":  "Lo que la persona quiere resolver, en sus palabras.",
                "PROBLEMA":  "Cómo lo busca quien no conoce la jerga del sector.",
                "CONFIANZA": "Búsquedas de quien ya está comparando y a punto de decidir.",
            }
            _ya_e = [k.strip().lower() for k in (keywords_edit or "").split(",") if k.strip()]
            st.caption("Marca las que quieras añadir a las que ya tiene el local.")

            for _fam in ("LOCAL", "SERVICIO", "PROBLEMA", "CONFIANZA"):
                _grupo_e = [k for k in _sug_e if k["familia"] == _fam]
                if not _grupo_e:
                    continue
                st.markdown(f"**{_fam.capitalize()}** — {_expl_e[_fam]}")
                for _i, _kw in enumerate(_grupo_e):
                    _repetida = _kw["termino"] in _ya_e
                    st.checkbox(
                        _kw["termino"] + (" · ya la tienes" if _repetida else ""),
                        key=f"_kwsel_edit_{_lid}_{_fam}_{_i}",
                        disabled=_repetida,
                        help=_kw["motivo"] or None,
                    )

            if st.button("Añadir seleccionadas", key=f"btn_aplicar_kw_edit_{_lid}", use_container_width=True):
                _marcadas_e = []
                for _fam in ("LOCAL", "SERVICIO", "PROBLEMA", "CONFIANZA"):
                    _grupo_e = [k for k in _sug_e if k["familia"] == _fam]
                    for _i, _kw in enumerate(_grupo_e):
                        if st.session_state.get(f"_kwsel_edit_{_lid}_{_fam}_{_i}"):
                            _marcadas_e.append(_kw["termino"])

                if not _marcadas_e:
                    st.warning("No has marcado ninguna.")
                else:
                    _orig = [k.strip() for k in (keywords_edit or "").split(",") if k.strip()]
                    _final_e = _orig + [k for k in _marcadas_e if k.lower() not in _ya_e]
                    st.session_state[f"_kw_pendiente_edit_{_lid}"] = ", ".join(_final_e)
                    del st.session_state[f"_kw_sug_edit_{_lid}"]
                    st.rerun()

        # =====================================================================
        # FICHA DE DATOS VERIFICADOS (motor SEO anclado)
        # =====================================================================
        # Aquí el operador de la agencia confirma qué ofrece REALMENTE el negocio.
        # Solo lo confirmado aquí puede afirmarse en el contenido generado. Es lo
        # que impide que el motor invente parking, estrellas Michelin o servicios
        # inexistentes. Pensado para el operador de la agencia (que entiende de
        # SEO), no para el hostelero final.
        st.divider()
        st.markdown("### Ficha de datos verificados")
        st.caption(
            "Confirma qué ofrece el negocio. El motor SEO solo puede afirmar lo que marques aquí: "
            "es lo que evita que invente servicios, premios o instalaciones que no existen."
        )

        _lid_f = local_activo["id"]
        _nicho_f = (nicho_edit or local_activo.get("nicho") or "general").strip()
        _ciudad_f = (ciudad_edit or local_activo.get("ciudad") or "").strip()

        # Cargar estado actual de la ficha desde Supabase (una vez por render).
        _ficha_actual = {f["clave"]: f for f in cargar_ficha_local(_lid_f)}

        # Construir el cuestionario base del nicho (10-12 preguntas estables) +
        # las preguntas extra por IA que el operador haya generado antes.
        _cuestionario = construir_cuestionario(_nicho_f, tope=12)
        _extra_key = f"_ficha_extra_{_lid_f}"
        _preguntas_extra = st.session_state.get(_extra_key, [])

        # Botón para pedir a la IA preguntas específicas del sector (solo pregunta).
        col_extra1, col_extra2 = st.columns([3, 1])
        with col_extra2:
            if st.button("Sugerir + preguntas", key=f"btn_pre_extra_{_lid_f}",
                         use_container_width=True,
                         help="La IA propone preguntas específicas de tu sector. No inventa datos: solo pregunta."):
                with st.spinner("Analizando el sector…"):
                    _ya = [h.pregunta for h in _cuestionario]
                    st.session_state[_extra_key] = sugerir_preguntas_extra(
                        client, _nicho_f, _ciudad_f, _ya
                    )
                st.rerun()

        # Enriquecimiento web (fase C, básico): leer la web del negocio y proponer.
        with col_extra1:
            _web_key = f"_ficha_web_url_{_lid_f}"
            _web_url = st.text_input(
                "¿Tiene web propia? Pégala y detectamos datos para que los confirmes (opcional)",
                key=_web_key, placeholder="https://...",
            )
        if _web_url and st.button("Detectar datos desde la web", key=f"btn_web_{_lid_f}"):
            with st.spinner("Leyendo la web del negocio…"):
                _propuestas = proponer_hechos_desde_web(_web_url, _nicho_f)
            if _propuestas:
                st.session_state[f"_ficha_web_prop_{_lid_f}"] = _propuestas
                st.success(f"Detectados {len(_propuestas)} datos. Revísalos y confírmalos abajo.")
            else:
                st.info("No se detectaron datos claros en la web (o no se pudo acceder). Rellena la ficha a mano.")

        _propuestas_web = st.session_state.get(f"_ficha_web_prop_{_lid_f}", [])
        if _propuestas_web:
            st.markdown("**Detectado en la web** (confirma solo lo que sea cierto):")
            for _p in _propuestas_web:
                st.caption(f"· {_p['pregunta']}  \n  _evidencia:_ {_p['evidencia_texto'][:120]}")

        # --- Render del formulario: un control por hecho ---------------------
        todas_las_preguntas = list(_cuestionario) + [
            motor_seo.DefHecho(clave=e["clave"], pregunta=e["pregunta"],
                               familia=e.get("familia", "Específico del sector"), tipo="si_no")
            for e in _preguntas_extra
        ]

        # Agrupar por familia para que el formulario sea legible.
        _por_familia = {}
        for _h in todas_las_preguntas:
            _por_familia.setdefault(_h.familia, []).append(_h)

        _valores_ficha = {}  # clave -> dict con lo que hay que guardar
        _opciones_estado = ["Sin verificar", "Sí", "No"]
        _mapa_estado_a_val = {"Sin verificar": NO_CONSTA, "Sí": SI, "No": NO}
        _mapa_val_a_estado = {NO_CONSTA: "Sin verificar", SI: "Sí", NO: "No", "": "Sin verificar"}

        for _fam, _hechos in _por_familia.items():
            st.markdown(f"**{_fam}**")
            for _h in _hechos:
                _fila = _ficha_actual.get(_h.clave, {})
                _estado_actual = _mapa_val_a_estado.get(_fila.get("estado", NO_CONSTA), "Sin verificar")

                if _h.tipo == "texto":
                    # Hecho de texto: un input. Vacío = NO_CONSTA; con texto = SI.
                    _val_txt = st.text_input(
                        _h.pregunta,
                        value=_fila.get("valor") or "",
                        key=f"ficha_{_lid_f}_{_h.clave}",
                    )
                    _valores_ficha[_h.clave] = {
                        "estado": SI if _val_txt.strip() else NO_CONSTA,
                        "valor": _val_txt.strip(),
                    }
                else:
                    _sel = st.radio(
                        _h.pregunta,
                        _opciones_estado,
                        index=_opciones_estado.index(_estado_actual),
                        key=f"ficha_{_lid_f}_{_h.clave}",
                        horizontal=True,
                    )
                    _reg = {"estado": _mapa_estado_a_val[_sel]}

                    # Distintivo: si es SÍ, exigir evidencia (año + entidad).
                    if _h.tipo == "si_no_evidencia" and _sel == "Sí":
                        cev1, cev2 = st.columns(2)
                        with cev1:
                            _ev_ent = st.text_input(
                                "¿Qué distintivo y quién lo otorga? (obligatorio)",
                                value=_fila.get("evidencia_entidad") or "",
                                key=f"ficha_ev_ent_{_lid_f}_{_h.clave}",
                                placeholder="ej: Sol Repsol, Guía Michelin…",
                            )
                        with cev2:
                            _ev_anio = st.text_input(
                                "Año", value=_fila.get("evidencia_anio") or "",
                                key=f"ficha_ev_anio_{_lid_f}_{_h.clave}",
                                placeholder="ej: 2025",
                            )
                        _reg["evidencia_entidad"] = _ev_ent.strip()
                        _reg["evidencia_anio"] = _ev_anio.strip()
                        _reg["valor"] = _ev_ent.strip()
                        if not _ev_ent.strip():
                            st.caption("⚠️ Sin la evidencia, el motor no podrá afirmar el distintivo (para protegerte de publicidad engañosa).")
                    _valores_ficha[_h.clave] = _reg

        if st.button("Guardar Ficha de datos verificados", key=f"guardar_ficha_{_lid_f}"):
            _guardados = 0
            for _clave, _reg in _valores_ficha.items():
                # Solo persistimos lo que no es NO_CONSTA sin valor, para no llenar
                # la BD de filas vacías. NO_CONSTA = ausencia de fila afirmable.
                _ok = guardar_hecho_local(
                    local_id=_lid_f,
                    agencia_id=local_activo["agencia_id"],
                    clave=_clave,
                    estado=_reg.get("estado", NO_CONSTA),
                    valor=_reg.get("valor"),
                    evidencia_anio=_reg.get("evidencia_anio"),
                    evidencia_entidad=_reg.get("evidencia_entidad"),
                )
                if _ok:
                    _guardados += 1
            st.success(f"Ficha guardada ({_guardados} datos). El contenido SEO ya se ancla a estos hechos.")
            # Alterna la identidad del expander para que se cierre solo al volver.
            st.session_state["_ficha_ver"] = 1 - st.session_state.get("_ficha_ver", 0)
            st.rerun()

        st.divider()

        if st.button("Guardar cambios", key=f"guardar_edit_local_{local_activo['id']}"):
            try:
                keywords_lista_edit = [k.strip() for k in keywords_edit.split(",") if k.strip()]
                supabase.table("locales").update({
                    "nicho": nicho_edit.strip() or local_activo["nicho"],
                    "ciudad": ciudad_edit.strip() or None,
                    "seo_keywords": keywords_lista_edit
                }).eq("id", local_activo["id"]).execute()
                # Actualizar en memoria
                for l in st.session_state.locales_agencia:
                    if l["id"] == local_activo["id"]:
                        l["nicho"] = nicho_edit.strip() or local_activo["nicho"]
                        l["ciudad"] = ciudad_edit.strip() or None
                        l["seo_keywords"] = keywords_lista_edit
                        break
                st.success("Cambios guardados.")
                st.session_state["_ficha_ver"] = 1 - st.session_state.get("_ficha_ver", 0)
                st.rerun()
            except Exception as e:
                st.error(redactar_secretos(f"Error al guardar: {e}"))

    plan_actual = agencia.get("plan", "growth")
    # Definimos limite_usos_plan SIEMPRE (se usa más abajo, al comprobar el cupo
    # antes de generar). En beta es None = ilimitado; si no, el límite del plan.
    # Sin esta línea, cuando la agencia está en beta el bloque else de abajo no
    # se ejecuta, la variable no se crea y peta con NameError al generar.
    if agencia_en_beta(agencia):
        limite_usos_plan = None
    else:
        limite_usos_plan = LIMITE_USOS_POR_PLAN.get(plan_actual, None)

    if agencia_en_beta(agencia):
        creado_en_dt = datetime.fromisoformat(agencia["creado_en"].replace("Z", "+00:00")).replace(tzinfo=None)
        dias_restantes = (creado_en_dt + timedelta(days=agencia.get("dias_beta", 7) or 7) - datetime.utcnow()).days
        dias_restantes = max(0, dias_restantes)
        st.info(f"🎁 Estás en el periodo de beta: respuestas ilimitadas durante **{dias_restantes} día(s) más**.")
    else:
        if limite_usos_plan is not None:
            usos_hechos = contar_usos_del_mes(agencia["id"])
            restantes = max(0, limite_usos_plan - usos_hechos)
            nombre_plan_legible = PLANES_AUTOSERVICIO.get(plan_actual, {}).get("nombre", plan_actual.capitalize())
            st.info(f"Plan {nombre_plan_legible}: te quedan **{restantes} de {limite_usos_plan}** respuestas este mes.")
        else:
            usos_local_este_mes = contar_usos_del_mes_por_local(local_activo["id"])
            if usos_local_este_mes >= UMBRAL_ACTIVIDAD_INUSUAL_POR_LOCAL:
                st.warning(f"Este local ha generado {usos_local_este_mes} respuestas este mes — un volumen inusualmente alto. Si no es un cliente real de mucho tráfico, te recomendamos revisarlo.")

    # =====================================================================
    # NUEVA RESPUESTA — dos vías
    # =====================================================================
    # El campo de la reseña va FUERA de st.form a propósito. Dentro de un
    # formulario, Streamlit no re-ejecuta hasta que se pulsa enviar, así que
    # no podríamos analizar el riesgo mientras se pega el texto. Fuera, en
    # cuanto el campo pierde el foco se re-ejecuta, se escanea la reseña y
    # se preselecciona la vía adecuada sola.
    st.markdown(html_eyebrow("Nueva respuesta"), unsafe_allow_html=True)

    resena_cliente = st.text_area(
        "Reseña del cliente",
        height=140,
        key="campo_resena",
        placeholder="Pega aquí la reseña tal y como aparece en Google…",
    )

    # ---- Análisis de riesgo: instantáneo, sin coste, sin llamadas ----------
    analisis = analizar_riesgo(resena_cliente) if resena_cliente.strip() else None

    if analisis and analisis.hay_riesgo:
        st.markdown(html_aviso_riesgo(analisis), unsafe_allow_html=True)

    # ---- Selector de vía ---------------------------------------------------
    OPCION_RAPIDA = "Vía rápida  ·  ~7 s  —  para reseñas positivas, con SEO integrado"
    OPCION_BLINDADA = "Blindaje completo  ·  ~15 s  —  para reseñas negativas o delicadas"

    indice_por_defecto = 1 if (analisis is None or analisis.hay_riesgo) else 0

    eleccion = st.radio(
        "Cómo quieres generarla",
        options=[OPCION_RAPIDA, OPCION_BLINDADA],
        index=indice_por_defecto,
        key="modo_generacion",
        label_visibility="collapsed",
    )
    modo_elegido = MODO_RAPIDO if eleccion == OPCION_RAPIDA else MODO_BLINDADO

    with st.expander("¿En qué se diferencian?"):
        st.markdown(
            "**Vía rápida.** Una sola pasada, pensada para reseñas positivas. "
            "Prioriza que la respuesta suene humana y coloque de forma natural "
            "las palabras clave del local, porque en una reseña de cinco "
            "estrellas el valor está en el posicionamiento, no en la defensa.\n\n"
            "**Blindaje completo.** Añade una segunda revisión con un modelo "
            "independiente que lee la respuesta con la mentalidad del abogado "
            "de la parte contraria, buscando frases utilizables en un juicio. "
            "Si encuentra alguna, la respuesta se reescribe antes de que la "
            "veas. Tarda unos segundos más y esos segundos son el producto.\n\n"
            "**Lo que nunca se salta.** Las dos vías bloquean intentos de "
            "manipulación escondidos dentro de la reseña y revisan el texto "
            "contra un filtro de léxico jurídico, compensaciones públicas y "
            "admisiones de culpa. Ese filtro es instantáneo y no cuesta nada, "
            "así que no hay ningún motivo para quitarlo."
        )

    with st.form("review_form"):
        col_tono, col_local = st.columns([2, 1])
        with col_tono:
            tono = st.select_slider(
                "Tono",
                options=["Muy formal", "Profesional estándar", "Cercano y cálido"],
                value="Profesional estándar",
            )
        with col_local:
            st.text_input("Establecimiento", value=local_activo["nombre"], disabled=True)

        acepta_terminos = st.checkbox(
            "Acepto los Términos de Uso y el Descargo de Responsabilidad legal.", value=False
        )
        etiqueta_boton = (
            "Generar respuesta" if modo_elegido == MODO_RAPIDO
            else "Generar con blindaje completo"
        )
        submit = st.form_submit_button(etiqueta_boton, use_container_width=True)

    if submit:
        # Se calcula UNA vez, y solo al enviar el formulario: las comprobaciones
        # de velocidad cuestan consultas a la base de datos y no tienen sentido
        # en los reruns en los que el usuario solo está escribiendo.
        _velocidad = verificar_velocidad(agencia)

        if not resena_cliente.strip():
            st.warning("Pega primero la reseña del cliente.")
        elif not acepta_terminos:
            st.error("Es obligatorio aceptar los términos de uso.")
        elif limite_usos_plan is not None and contar_usos_del_mes(agencia["id"]) >= limite_usos_plan:
            st.error(redactar_secretos(f"Has usado tus {limite_usos_plan} respuestas de este mes en tu plan actual. Actualiza tu plan para seguir generando sin límite."))
            if st.button("Ver planes de pago", key="ver_planes_limite_usos"):
                st.session_state.mostrar_pagina_planes = True
                st.rerun()
        elif not _velocidad["permitido"]:
            st.error(redactar_secretos(_velocidad["razon"]))
        else:
            # Se reutiliza el mismo dict calculado arriba. Antes esto llamaba
            # tres veces a verificar_velocidad(), y cada llamada lanza hasta
            # dos COUNT contra historico_respuestas: seis consultas por rerun,
            # y Streamlit re-ejecuta el script en cada pulsación.
            _adv_velocidad = _velocidad.get("advertencia")
            if _adv_velocidad:
                st.info(_adv_velocidad)

            _techo_blando = LIMITE_MENSUAL_BLANDO_PLANES_ILIMITADOS.get(plan_actual)
            if _techo_blando is not None and not agencia_en_beta(agencia):
                _usos_mes_actual = contar_usos_del_mes(agencia["id"])
                if _usos_mes_actual >= _techo_blando:
                    st.info(
                        f"Este mes llevas {_usos_mes_actual} respuestas — un volumen fuera de lo "
                        "habitual para tu plan. No pasa nada, se genera igual; pero si tu negocio "
                        "tiene este ritmo de forma constante, escríbenos y te preparamos un plan a "
                        "medida para que el precio tenga sentido a ese volumen."
                    )

            try:
                nombre_local_final = local_activo["nombre"]
                nicho_local = local_activo["nicho"]
                keywords_texto = ", ".join(local_activo["seo_keywords"])

                guia_tono_activa = guias_de_tono.get(tono, guias_de_tono["Profesional estándar"])

                # ---- Progreso por etapas -------------------------------------
                # Un spinner mudo de quince segundos hace pensar que la app se
                # ha colgado. Ver "Auditando frase por frase" convierte la
                # espera en la demostración de lo que se está pagando.
                etapas = ETAPAS_RAPIDA if modo_elegido == MODO_RAPIDO else ETAPAS_BLINDADA
                caja_etapas = st.empty()

                def _progreso(etapa, detalle=""):
                    caja_etapas.markdown(
                        html_etapas(etapas, etapa), unsafe_allow_html=True
                    )

                # Datos verificados de la Ficha: permiten que la respuesta se apoye
                # en hechos reales del negocio (ancla SEO honesta) en vez de rellenar.
                _hechos_afirm = hechos_afirmables_texto(local_activo["id"], nicho_local)

                resultado_blindaje = generar_respuesta(
                    client=client,
                    resena=resena_cliente,
                    nombre_local=nombre_local_final,
                    nicho=nicho_local,
                    keywords=keywords_texto,
                    tono=tono,
                    guia_tono=guia_tono_activa,
                    bloque_estatico=bloque_estatico,
                    modo=modo_elegido,
                    on_progress=_progreso,
                    hechos_afirmables=_hechos_afirm,
                )

                caja_etapas.empty()

                for _alerta in resultado_blindaje.alertas_entrada:
                    st.warning(_alerta)

                if resultado_blindaje.bloqueada:
                    st.error(resultado_blindaje.motivo_bloqueo)
                    st.caption(
                        "No se ha generado ninguna respuesta. Si la reseña es legítima "
                        "y crees que es un falso positivo, revísala y vuelve a intentarlo."
                    )
                else:
                    respuesta_nativa = resultado_blindaje.respuesta_nativa
                    traduccion = resultado_blindaje.traduccion_espanol
                    idioma_detectado = resultado_blindaje.idioma_detectado
                    sentimiento = resultado_blindaje.sentimiento

                    st.markdown(html_eyebrow("Lista para publicar"), unsafe_allow_html=True)

                    if traduccion:
                        st.caption("Respuesta en el idioma del cliente — pasa el ratón por encima para copiar:")
                        st.code(respuesta_nativa, language=None, wrap_lines=True)
                        st.info(f"**Traducción al español para el propietario:**\n\n{traduccion}")
                    else:
                        st.caption("Pasa el ratón por encima del texto para copiarlo:")
                        st.code(respuesta_nativa, language=None, wrap_lines=True)

                    # Sello de auditoría: convierte el blindaje de promesa en
                    # registro. Es lo que el gestor puede enseñar a su cliente.
                    st.markdown(html_sello(resultado_blindaje), unsafe_allow_html=True)

                    if resultado_blindaje.violaciones_residuales:
                        st.warning(
                            "Esta respuesta no ha quedado del todo limpia. "
                            "Léela con atención antes de publicarla."
                        )
                        with st.expander("Ver qué ha señalado la revisión", expanded=True):
                            for _v in resultado_blindaje.violaciones_residuales:
                                st.markdown(f"- **{_v.regla}** · «{_v.fragmento}» — {_v.motivo}")
                            if resultado_blindaje.modo_usado == MODO_RAPIDO:
                                st.caption(
                                    "Has usado la vía rápida, que detecta pero no reescribe. "
                                    "Vuelve a generarla con blindaje completo para que se corrija sola."
                                )

                    if resultado_blindaje.violaciones_corregidas:
                        with st.expander(
                            f"Ver las {len(resultado_blindaje.violaciones_corregidas)} "
                            "incidencia(s) que la auditoría corrigió"
                        ):
                            st.caption(
                                "Estas frases aparecían en un borrador previo y se "
                                "reescribieron antes de enseñarte la respuesta."
                            )
                            for _v in resultado_blindaje.violaciones_corregidas:
                                st.markdown(f"- **{_v.regla}** · «{_v.fragmento}» — {_v.motivo}")

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

            except Exception as e:
                causa_raiz = log_error_completo("generar respuesta a reseña", e)
                st.error(redactar_secretos(f"Error al conectar con el servidor: {type(e).__name__}: {e}"))
                st.caption(f"Causa raíz (revisa también Manage app → Logs): {causa_raiz}")

# ---------------------------------------------------------
# PESTAÑA: KIT DE CAPTACIÓN (WhatsApp + QRs múltiples + hoja imprimible)
# ---------------------------------------------------------
# El nombre "Pedir reseñas" que llevaba esta sección se ha quedado corto: ahora
# también se generan QRs para la carta y las reservas, y una hoja A4 con todos
# los QRs del local maquetados para plastificar y dejar en la mesa. Todo
# apoyado en kit_captacion.py, para no ensuchar app.py con el motor de PDF.
#
# Nota práctica: la etiqueta del menú lateral sigue diciendo "Pedir reseñas"
# (más abajo, en SECCIONES). Cambiarla implicaría migrar la clave del radio
# en session_state, y no compensa hacerlo la víspera del lanzamiento.
if vista_activa == "Pedir reseñas":
    import kit_captacion

    st.subheader("Kit de captación del local")
    st.caption(
        "Genera QRs para reseñas, carta y reservas, y una hoja imprimible "
        "para dejar en la mesa. Todo con la marca del negocio."
    )

    locales_disponibles_pr = st.session_state.locales_agencia
    if not locales_disponibles_pr:
        st.info("Esta agencia todavía no tiene locales.")
    else:
        nombre_local_pr = st.selectbox(
            "Local:",
            options=[l["nombre"] for l in locales_disponibles_pr],
            key="selector_local_pedir_resenas"
        )
        local_pr = next(l for l in locales_disponibles_pr
                        if l["nombre"] == nombre_local_pr)

        # ------------------------------------------------------------------
        # ENLACES DEL LOCAL — todos en un solo formulario y un solo guardado
        # ------------------------------------------------------------------
        # Antes había un input para reseñas con su propio botón "Guardar". Con
        # cuatro enlaces distintos, cuatro botones de guardado sería un ruido
        # innecesario: se usa un st.form con un único botón, que además evita
        # que Streamlit re-ejecute todo el script cada vez que el usuario
        # teclea un carácter en cualquiera de los cuatro campos.
        st.markdown("#### Enlaces del negocio")
        st.caption(
            "Solo hace falta uno para empezar. Los que dejes vacíos no "
            "aparecerán en la hoja imprimible."
        )

        # Diagnóstico de calidad del QR ANTES del form: se evalúa sobre el
        # valor guardado, para avisar al usuario nada más entrar si su enlace
        # actual va a generar un QR problemático.
        _url_actual_resenas = (local_pr.get("enlace_resena_google") or "").strip()
        if _url_actual_resenas:
            _nivel_qr, _msg_qr = kit_captacion.diagnostico_qr(_url_actual_resenas)
            if _nivel_qr == "error":
                st.error(f"⚠️ QR demasiado complejo — {_msg_qr}")
            elif _nivel_qr == "warning":
                st.warning(f"💡 {_msg_qr}")

        with st.form("form_enlaces_kit", border=False):
            enlace_resenas_input = st.text_input(
                "Enlace de Google para dejar una reseña",
                value=local_pr.get("enlace_resena_google") or "",
                placeholder="https://g.page/r/xxxxxxxxxx/review",
                help=(
                    "En Google Maps: busca el negocio → pulsa 'Compartir' → "
                    "pestaña 'Enlace corto'. Si el negocio gestiona su ficha, "
                    "también sale en Google Business Profile → 'Solicitar reseñas'."
                ),
            )
            enlace_carta_input = st.text_input(
                "Enlace de la carta digital (opcional)",
                value=local_pr.get("enlace_carta") or "",
                placeholder="https://mibar.com/carta",
                help="La URL a la que va el cliente cuando escanea el QR de la mesa.",
            )
            enlace_reservas_input = st.text_input(
                "Enlace de reservas (opcional)",
                value=local_pr.get("enlace_reservas") or "",
                placeholder="https://thefork.es/... o el sistema que ya use el local",
                help=(
                    "Si el negocio ya usa TheFork, Cover Manager o similares, "
                    "pega aquí ese enlace: no hace falta que tengan un sistema "
                    "propio."
                ),
            )

            col_et, col_url = st.columns([1, 2])
            with col_et:
                extra_etiqueta_input = st.text_input(
                    "Etiqueta del QR extra (opcional)",
                    value=(local_pr.get("enlace_extra_etiqueta") or ""),
                    placeholder="p. ej. 'Menú del día'",
                )
            with col_url:
                extra_url_input = st.text_input(
                    "URL del QR extra",
                    value=(local_pr.get("enlace_extra_url") or ""),
                    placeholder="https://...",
                    help=(
                        "Para lo que no encaje en los anteriores: menú del día, "
                        "ofertas, redes sociales, formulario de eventos..."
                    ),
                )

            guardar = st.form_submit_button(
                "Guardar enlaces", type="primary", use_container_width=True
            )

        if guardar:
            # Normalización antes de guardar: si el usuario escribió
            # "www.mibar.com/carta" sin http, kit_captacion.normalizar_url()
            # lo rescata añadiendo https. Si no se puede rescatar, se guarda
            # vacío (mejor que un enlace roto latente en la base de datos).
            actualizacion = {
                "enlace_resena_google": kit_captacion.normalizar_url(
                    enlace_resenas_input),
                "enlace_carta": kit_captacion.normalizar_url(
                    enlace_carta_input),
                "enlace_reservas": kit_captacion.normalizar_url(
                    enlace_reservas_input),
                "enlace_extra_etiqueta": (extra_etiqueta_input or "").strip(),
                "enlace_extra_url": kit_captacion.normalizar_url(
                    extra_url_input),
            }
            try:
                supabase.table("locales").update(actualizacion)                     .eq("id", local_pr["id"]).execute()
                # Se actualiza el dict en memoria para que el resto del render
                # de esta misma pasada ya use los valores nuevos, sin esperar
                # a un rerun completo.
                local_pr.update(actualizacion)
                st.success("Enlaces guardados.")
            except Exception as e:
                st.error(redactar_secretos(f"No se pudo guardar: {e}"))

        # ------------------------------------------------------------------
        # RESULTADOS — WhatsApp + hoja imprimible + QRs sueltos
        # ------------------------------------------------------------------
        enlace_resenas = (local_pr.get("enlace_resena_google") or "").strip()
        enlaces_kit = {
            "resenas": enlace_resenas,
            "carta": (local_pr.get("enlace_carta") or "").strip(),
            "reservas": (local_pr.get("enlace_reservas") or "").strip(),
            "extra": {
                "etiqueta": (local_pr.get("enlace_extra_etiqueta") or "").strip(),
                "url": (local_pr.get("enlace_extra_url") or "").strip(),
            },
        }
        # Se cuenta cuántos son válidos antes de decidir qué enseñar. Si no hay
        # ninguno, tampoco tiene sentido ofrecer el kit ni WhatsApp.
        enlaces_validos = sum(
            1 for k in ("resenas", "carta", "reservas") if enlaces_kit[k]
        ) + (1 if enlaces_kit["extra"]["url"] and enlaces_kit["extra"]["etiqueta"] else 0)

        if enlaces_validos == 0:
            st.info(
                "Guarda al menos un enlace arriba para poder generar el QR y "
                "la hoja imprimible."
            )
        else:
            st.markdown("#### Descargables")

            # --- Hoja imprimible del kit (siempre disponible si hay ≥1 enlace) ---
            with st.expander("📄 Hoja imprimible con todos los QRs (A4)", expanded=True):
                st.caption(
                    "Un PDF listo para imprimir, plastificar y colocar en la "
                    "mesa o en la barra. Se adapta a los enlaces que tengas "
                    "guardados."
                )
                try:
                    pdf_kit = kit_captacion.generar_hoja_imprimible(
                        nombre_local=nombre_local_pr,
                        enlaces=enlaces_kit,
                        color_marca=agencia.get("color_marca", ACCENT_INDIGO),
                        # Mismo criterio que el informe PDF: solo se firma con
                        # la agencia cuando el plan la contempla como marca
                        # blanca (Individual en adelante). En Free no aparece.
                        nombre_agencia=(
                            agencia.get("nombre_agencia")
                            if agencia.get("plan", "free") != "free" else None
                        ),
                    )
                    st.download_button(
                        label="⬇ Descargar hoja imprimible",
                        data=pdf_kit,
                        file_name=f"kit_{nombre_local_pr}.pdf",
                        mime="application/pdf",
                        use_container_width=True,
                    )
                except ValueError as e:
                    # Puede pasar si TODOS los enlaces se filtran por normalización
                    # a pesar de tener texto. Se avisa con el mensaje real.
                    st.warning(str(e))

            # --- WhatsApp: solo tiene sentido si hay enlace de reseñas ---
            if enlace_resenas:
                with st.expander("💬 Mensaje de WhatsApp para pedir reseñas"):
                    st.caption(
                        "Se abre en WhatsApp con el mensaje ya escrito; solo "
                        "hay que elegir el contacto."
                    )
                    enlace_wa = generar_mensaje_whatsapp(
                        nombre_local_pr, enlace_resenas)
                    st.markdown(
                        f'<a href="{enlace_wa}" target="_blank" '
                        f'style="text-decoration:none;">'
                        f'<div style="background:{ACCENT_INDIGO};color:#ffffff;'
                        f'padding:11px 20px;border-radius:6px;font-weight:600;'
                        f'text-align:center;">Abrir en WhatsApp →</div></a>',
                        unsafe_allow_html=True,
                    )

            # --- QRs sueltos (PNG) por si quieren imprimir uno grande solo ---
            # Se dejan en un expander cerrado por defecto: el usuario típico va
            # a querer la hoja imprimible, no cuatro PNGs por separado. Pero
            # están ahí para el que prefiera pegar un QR grande en la puerta.
            with st.expander("🖼️ QRs sueltos (PNG)"):
                st.caption(
                    "Un PNG por cada enlace, por si prefieres imprimir uno "
                    "muy grande (puerta, escaparate, ticket)."
                )
                # Se enumeran solo los que tienen enlace válido, en el mismo
                # orden que la hoja imprimible.
                candidatos_png = [
                    ("Reseñas de Google", enlaces_kit["resenas"], "resenas"),
                    ("Carta digital", enlaces_kit["carta"], "carta"),
                    ("Reservas", enlaces_kit["reservas"], "reservas"),
                ]
                if enlaces_kit["extra"]["url"] and enlaces_kit["extra"]["etiqueta"]:
                    candidatos_png.append((
                        enlaces_kit["extra"]["etiqueta"],
                        enlaces_kit["extra"]["url"],
                        "extra",
                    ))
                candidatos_png = [c for c in candidatos_png if c[1]]

                # Rejilla de hasta 3 columnas: legible en desktop y aceptable
                # en móvil (Streamlit las apila cuando se estrecha).
                for fila in range(0, len(candidatos_png), 3):
                    trio = candidatos_png[fila:fila + 3]
                    cols = st.columns(len(trio))
                    for col, (etiqueta, url, sufijo) in zip(cols, trio):
                        with col:
                            st.markdown(f"**{_html.escape(etiqueta)}**")
                            png_qr = kit_captacion.generar_qr_png(url)
                            st.image(png_qr, width=170)
                            st.download_button(
                                label="⬇ Descargar",
                                data=png_qr,
                                file_name=f"qr_{sufijo}_{nombre_local_pr}.png",
                                mime="image/png",
                                key=f"dl_qr_{sufijo}_{local_pr['id']}",
                                use_container_width=True,
                            )

# ---------------------------------------------------------
# PESTAÑA: CONTENIDO SEO EXTRA
# ---------------------------------------------------------
if vista_activa == "Contenido SEO":
    st.subheader("Contenido SEO adicional para el local")
    st.caption("Contenido optimizado para posicionamiento local 2026: intención de búsqueda, ubicación y estructura pensada para las AI overviews de Google. Cada generación te da 3 variantes para elegir o hacer test A/B.")

    locales_disponibles_seo = st.session_state.locales_agencia
    if not locales_disponibles_seo:
        st.info("Esta agencia todavía no tiene locales.")
    else:
        nombre_local_seo = st.selectbox(
            "Local:", options=[l["nombre"] for l in locales_disponibles_seo], key="selector_local_seo_extra"
        )
        local_seo = next(l for l in locales_disponibles_seo if l["nombre"] == nombre_local_seo)

        ciudad_local = (local_seo.get("ciudad") or "").strip()
        if ciudad_local:
            st.caption(f"Ubicación para SEO local: **{ciudad_local}**")
        else:
            st.warning("Este local no tiene ciudad/zona configurada. El contenido saldrá bien, pero para máxima potencia SEO local, edita el local y añade su ciudad o zona (p.ej. 'Sevilla' o 'Triana, Sevilla').")

        tipo_contenido = st.radio(
            "Tipo de contenido:",
            [
                "Publicación de Google Business",
                "Descripción de servicio/producto",
                "Pregunta y respuesta (Q&A)",
                "Oferta / promoción",
                "Descripción para redes sociales",
                "Meta descripción SEO",
            ],
            horizontal=False
        )
        ayudas_tipo = {
            "Descripción de servicio/producto": "La pestaña de Servicios/Productos es de las más infravaloradas y en 2026 alimenta las AI overviews de Google.",
            "Pregunta y respuesta (Q&A)": "El bloque Q&A de tu ficha se indexa en Google y responde dudas de alta intención de compra.",
            "Oferta / promoción": "Las publicaciones de Oferta destacan en el panel local y suben el porcentaje de clics.",
        }
        if tipo_contenido in ayudas_tipo:
            st.caption(ayudas_tipo[tipo_contenido])

        # Contamos cuántos hechos hay verificados para avisar si la Ficha está vacía:
        # sin datos verificados el contenido sale honesto pero pobre, y conviene
        # empujar al operador a rellenar la Ficha (que vive en "Editar info del local").
        _ficha_filas_seo = cargar_ficha_local(local_seo["id"])
        _n_verificados = sum(1 for f in _ficha_filas_seo if (f.get("estado") == "SI"))
        if _n_verificados == 0:
            st.info(
                "Este local todavía no tiene **datos verificados** en su Ficha. El contenido "
                "saldrá honesto pero genérico. Para que sea potente y específico, ve a "
                "**Editar info del local → Ficha de datos verificados** y confirma qué ofrece el negocio."
            )

        if st.button("Generar variantes", key="generar_seo_extra"):
            with st.spinner("Redactando contenido SEO anclado a datos verificados..."):
                try:
                    resultado_seo = generar_contenido_seo(
                        client,
                        nombre_local=local_seo["nombre"],
                        nicho=local_seo["nicho"],
                        ciudad=ciudad_local or "",
                        ficha_filas=_ficha_filas_seo,
                        tipo_contenido=tipo_contenido,
                        keywords=local_seo.get("seo_keywords") or [],
                        modo_asistido=True,
                    )

                    if resultado_seo.bloqueado or not resultado_seo.variantes:
                        st.warning(
                            "No se pudo generar contenido veraz con los datos disponibles. "
                            + (resultado_seo.motivo or "")
                            + " Prueba a verificar más datos del negocio en su Ficha."
                        )
                    else:
                        st.markdown("**Elige la variante que más te guste** (o genera de nuevo para más opciones):")
                        for i, variante in enumerate(resultado_seo.variantes, start=1):
                            st.markdown(f"**Variante {i}**")
                            st.code(variante, language=None, wrap_lines=True)
                            if tipo_contenido == "Meta descripción SEO":
                                n_car = len(variante)
                                estado = "correcto" if n_car <= 155 else "excede el límite"
                                st.caption(f"{n_car} caracteres · {estado} (recomendado: máx. 155).")

                        # --- MODO ASISTIDO: nudges para enriquecer la Ficha -----
                        # No afirmamos lo no verificado, pero SÍ le decimos al dueño
                        # qué podría desbloquear si lo verifica. Convierte la
                        # restricción en un bucle de mejora del contenido.
                        if resultado_seo.sugerencias:
                            st.divider()
                            st.markdown("**Para enriquecer el contenido**, verifica estos datos en la Ficha del local:")
                            for sug in resultado_seo.sugerencias[:6]:
                                st.markdown(f"- Podrías {sug}")
                            st.caption(
                                "El motor no menciona nada de esto hasta que lo confirmas, "
                                "para no afirmar cosas sin verificar."
                            )

                        registrar_contenido_seo_generado(
                            agencia_id=agencia["id"],
                            local_id=local_seo["id"],
                            usuario_id=usuario["id"],
                            tipo_contenido=tipo_contenido
                        )
                except Exception as e:
                    causa_raiz = log_error_completo("generar contenido SEO extra", e)
                    st.error(redactar_secretos(f"Error al generar el contenido: {e}"))
                    st.caption(f"Causa raíz (revisa también Manage app → Logs): {causa_raiz}")

# ---------------------------------------------------------
# PESTAÑA 2: ANALÍTICA DE LA AGENCIA
# ---------------------------------------------------------
if vista_activa == "Analítica":
    st.subheader("Actividad de tu agencia")

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
            .order("creado_en", desc=True) \
            .limit(4000) \
            .execute().data

        if not historico:
            st.info("Todavía no hay respuestas generadas en este periodo.")
        else:
            if len(historico) >= 4000:
                st.caption(
                    "⚠️ Este negocio tiene más de 4.000 respuestas registradas. Para no sobrecargar "
                    "el servidor, el score y el informe se calculan sobre las 4.000 más recientes, "
                    "no sobre el histórico completo desde el primer día."
                )
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
                        .order("creado_en", desc=True) \
                        .limit(4000) \
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

            # ---------- 🏆 RESELIA REPUTATION SCORE (protagonista) ----------
            st.markdown("### Reputation Score")
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
            with st.expander("Calculadora de retorno: ¿cuánto vale subir de estrellas?", expanded=False):
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
            mostrar_barras_simples(conteo_por_usuario, color="#6b7280")

            # Contenido SEO generado en el periodo (tabla nueva — si aún no se ha
            # ejecutado la migración, esto falla en silencio y el informe lo indica)
            try:
                contenido_seo_periodo = supabase.table("historico_contenido_seo") \
                    .select("*") \
                    .eq("agencia_id", agencia["id"]) \
                    .gte("creado_en", fecha_desde) \
                    .order("creado_en", desc=True) \
                    .limit(2000) \
                    .execute().data
            except Exception:
                contenido_seo_periodo = []

            st.divider()
            st.markdown("**Informe de marca blanca para reenviar a tus clientes**")

            # ---------------------------------------------------------------
            # GENERACIÓN BAJO DEMANDA
            # ---------------------------------------------------------------
            # Antes, todo este bloque se ejecutaba SIEMPRE que se abría la
            # sección de Analítica, sin que nadie pidiera el informe. En cada
            # carga —y en cada rerun provocado por cualquier clic de la
            # sección— se hacía lo siguiente:
            #
            #   1. Construir el PDF entero con ReportLab (decenas de párrafos,
            #      tablas y un gráfico renderizado).
            #   2. Llamar al modelo de IA para redactar el resumen ejecutivo.
            #      Segundos de espera y coste por token, cada vez.
            #   3. Codificar el PDF a base64 e incrustarlo en el HTML de la
            #      página. Un informe de 500 KB se convierte en unos 680 KB de
            #      texto metido dentro del documento.
            #
            # De ahí venía la lentitud, y el paso 3 explica también parte del
            # consumo de memoria: el PDF entero vivía en el árbol de la página
            # aunque nadie fuera a descargarlo.
            #
            # Ahora nada de eso ocurre hasta que se pulsa el botón. El
            # resultado se guarda en session_state con una clave que incluye
            # el periodo, así que volver a la sección no lo regenera, pero
            # cambiar de periodo sí produce un informe nuevo.
            _clave_pdf = f"_pdf_{agencia['id']}_{fecha_desde}_{fecha_hasta_dt.strftime('%Y%m%d')}"

            st.caption(
                "El informe incluye la evolución del periodo, el Reputation Score "
                "y un resumen ejecutivo redactado con IA. Tarda unos segundos en "
                "prepararse."
            )

            if st.button("Generar informe PDF", key="btn_generar_pdf", use_container_width=True):
                try:
                    # El score del informe es siempre el de toda la agencia, no
                    # el del local seleccionado arriba en pantalla.
                    score_agencia = calcular_reputation_score(historico, historico_anterior, dias_periodo)

                    # Si se ha usado la calculadora de ROI de arriba, se incluye
                    # ese cálculo. Los valores viven en session_state por sus keys.
                    roi_informe = None
                    fact_roi = st.session_state.get("roi_facturacion", 0)
                    est_act_roi = st.session_state.get("roi_estrellas_act", 0.0)
                    est_obj_roi = st.session_state.get("roi_estrellas_obj", 0.0)
                    if fact_roi and est_obj_roi > est_act_roi:
                        roi_informe = calcular_roi_estrellas(fact_roi, est_act_roi, est_obj_roi)

                    with st.spinner("Preparando el informe…"):
                        pdf_bytes = generar_informe_pdf_mensual(
                            agencia, historico, historico_anterior, st.session_state.locales_agencia,
                            id_a_nombre_usuario, contenido_seo_periodo, periodo_texto,
                            cliente_ia=client, resultado_score=score_agencia, dias_periodo=dias_periodo,
                            roi=roi_informe, roi_estrellas_actuales=est_act_roi, roi_estrellas_objetivo=est_obj_roi
                        )

                    # Solo se guarda el informe recién pedido. Sin esta limpieza,
                    # cambiar de periodo varias veces iría acumulando un PDF
                    # completo en memoria por cada periodo consultado.
                    for _k in [k for k in st.session_state if k.startswith(f"_pdf_{agencia['id']}_")]:
                        del st.session_state[_k]

                    st.session_state[_clave_pdf] = pdf_bytes

                except Exception as e:
                    causa_raiz = log_error_completo("generar informe PDF", e)
                    st.error(redactar_secretos(f"No se pudo generar el informe: {e}"))
                    st.caption(f"Causa raíz (revisa también Manage app → Logs): {causa_raiz}")

            # El enlace de descarga solo existe si ya hay un informe generado
            # para este periodo concreto.
            if st.session_state.get(_clave_pdf):
                pdf_bytes = st.session_state[_clave_pdf]
                nombre_pdf = f"informe_{agencia['nombre_agencia'].replace(' ', '_')}_{fecha_hasta_dt.strftime('%Y%m%d')}.pdf"
                # Descarga por enlace con data URI en vez de st.download_button.
                # Motivo: detrás del proxy de Render, download_button a veces sirve
                # el archivo sin las cabeceras correctas y el navegador lo guarda
                # como .txt con un nombre de hash. Un enlace <a download="...pdf">
                # con el PDF embebido en base64 y el tipo MIME application/pdf
                # explícito fuerza el nombre y la extensión correctos, sin depender
                # de que el proxy respete las cabeceras.
                b64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")
                href_pdf = f"data:application/pdf;base64,{b64_pdf}"
                st.markdown(
                    f"""
                    <a href="{href_pdf}" download="{nombre_pdf}" style="
                        display:inline-block;
                        padding:0.6rem 1.4rem;
                        background:{ACCENT_INDIGO};
                        color:#ffffff;
                        text-decoration:none;
                        border-radius:8px;
                        font-weight:600;
                        font-size:0.9rem;
                    ">⬇ Descargar informe PDF</a>
                    """,
                    unsafe_allow_html=True,
                )
                st.caption(f"Informe listo · {len(pdf_bytes) // 1024} KB")

    except Exception as e:
        st.error(redactar_secretos(f"No se pudo cargar la analítica: {e}"))

# =========================================================
# 🗑️ ZONA DE BAJA — solo visible para el usuario admin de la agencia
# =========================================================
if usuario.get("rol") == "admin":
    with st.expander("Darme de baja / Eliminar todos los datos de mi agencia"):
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
    <strong>Aviso Legal y Condiciones de Uso (Marca Blanca):</strong> Esta plataforma es una
    herramienta tecnológica de asistencia basada en modelos de Inteligencia Artificial generativa, licenciada
    bajo un contrato B2B a <strong>{_html.escape(agencia['nombre_agencia'])}</strong>. El software no presta asesoramiento
    legal, jurídico, ni de relaciones públicas vinculante. La agencia operadora es la única responsable de
    revisar, verificar y autorizar cualquier contenido generado antes de su publicación. Queda expresamente
    prohibida la ingeniería inversa, descompilación o extracción de la lógica de negocio de esta plataforma.
</div>
""", unsafe_allow_html=True)
