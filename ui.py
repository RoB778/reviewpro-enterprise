# -*- coding: utf-8 -*-
"""
=============================================================================
RESELIA — CAPA DE INTERFAZ (componentes nuevos de la v2)
=============================================================================

HISTORIAL DE ESTE ARCHIVO — por qué es tan corto
--------------------------------------------------
La primera versión de este archivo redefinía un sistema de diseño entero:
variables --tinta/--papel, botones, pestañas, inputs, bloques de código.
Parecía razonable hasta que se comprobó contra app.py: ahí ya existía un
sistema COMPLETO y probado, "Tinta & Papel" (variables --er-*), con la
MISMA paleta, ya resuelto para las inconsistencias de versión de Streamlit
en los botones (el comentario original explica que hubo que cubrir varios
data-testid porque Streamlit los ha ido cambiando entre versiones).

Con los dos sistemas inyectados, ninguno gana con seguridad — Streamlit no
garantiza el orden de los bloques <style> entre re-renders parciales (los
que disparan formularios y widgets), así que unas veces ganaba el fondo de
uno y el texto del otro. De ahí el bug: botones sin fondo con texto
forzado en blanco.

La solución no es apilar un tercer parche. Es que este archivo deje de
competir: aquí solo viven los componentes GENUINAMENTE NUEVOS de la v2 (el
selector de vía, el sello de auditoría, el aviso de riesgo, las etapas de
progreso), y todos usan las variables --er-* que ya existen, en vez de
inventar una paleta paralela. Todo lo demás — botones, pestañas, inputs,
tipografía — lo sigue gobernando el sistema que ya estaba en app.py, sin
tocar.
=============================================================================
"""

CSS_GLOBAL = """
<style>
/* =========================================================
   CAPA DE GLASSMORPHISM  (v3 · "Aurora & Cristal")
   ---------------------------------------------------------
   Este bloque NO define colores ni fondos de las superficies:
   eso lo hacen las variables --er-* de app.py (que ya son
   translúcidas en v3). Aquí solo se añade lo que convierte una
   superficie translúcida en CRISTAL: el desenfoque del fondo
   (backdrop-filter), la sombra flotante y el brillo de borde.
   Así no compite con app.py — lo complementa.

   Legibilidad: el blur actúa sobre el FONDO, no sobre el texto.
   El texto se apoya en cristal de alpha alto (.72), contraste AA.

   Rendimiento: backdrop-filter es caro. Se degrada con @supports
   y se aligera en móvil (donde ya hubo problemas de rendimiento).
   ========================================================= */

/* --- Tarjetas de cristal: expanders, contenedores con borde --- */
[data-testid="stExpander"],
[data-testid="stForm"],
section[data-testid="stMain"] div[data-testid="stVerticalBlockBorderWrapper"] {
  -webkit-backdrop-filter: blur(18px) saturate(1.5);
  backdrop-filter: blur(18px) saturate(1.5);
  box-shadow: var(--er-shadow);
  border-radius: 16px !important;
  transition: box-shadow .2s ease, transform .2s ease, border-color .2s ease;
}

/* Brillo sutil en el borde superior — el detalle que "vende" el cristal.
   Un hilo de luz blanca arriba, como un canto biselado. */
[data-testid="stExpander"] {
  position: relative;
  border: 1px solid var(--er-line) !important;
}
[data-testid="stExpander"]::before {
  content: "";
  position: absolute; inset: 0 0 auto 0; height: 1px;
  background: linear-gradient(90deg,
    transparent, var(--er-glass-edge) 20%, var(--er-glass-edge) 80%, transparent);
  border-radius: 16px 16px 0 0;
  pointer-events: none;
}

/* Elevación al pasar por encima: el cristal "flota" un poco más. */
[data-testid="stExpander"]:hover {
  box-shadow: var(--er-shadow-lg);
  border-color: var(--er-line-2) !important;
}

/* --- Inputs de cristal --- */
.stTextInput input, .stTextArea textarea, .stSelectbox div[data-baseweb="select"] > div,
.stNumberInput input, [data-baseweb="input"], [data-baseweb="select"] {
  -webkit-backdrop-filter: blur(8px);
  backdrop-filter: blur(8px);
}

/* --- Sombra de foco de acento (índigo) para inputs --- */
.stTextInput input:focus, .stTextArea textarea:focus,
.stNumberInput input:focus {
  box-shadow: 0 0 0 3px var(--er-accent-bg) !important;
}

/* --- Botones: acento índigo con degradado sutil y sombra suave --- */
section[data-testid="stMain"] .stButton > button,
section[data-testid="stMain"] .stFormSubmitButton > button {
  border-radius: 12px !important;
  box-shadow: 0 2px 8px rgba(79,70,229,.14) !important;
  transition: transform .14s ease, box-shadow .2s ease, filter .2s ease !important;
}
section[data-testid="stMain"] .stButton > button:hover,
section[data-testid="stMain"] .stFormSubmitButton > button:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 18px rgba(79,70,229,.24) !important;
  filter: brightness(1.05);
}
section[data-testid="stMain"] .stButton > button:active,
section[data-testid="stMain"] .stFormSubmitButton > button:active {
  transform: translateY(0);
}

/* =========================================================
   FALLBACK — navegadores sin soporte de backdrop-filter
   Sin blur, las superficies translúcidas dejarían ver la aurora
   a través del texto. Se rellenan con blanco casi sólido para
   garantizar la legibilidad SIEMPRE.
   ========================================================= */
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
  :root {
    --er-surface:   rgba(255,255,255,.95) !important;
    --er-surface-2: rgba(255,255,255,.92) !important;
    --er-sunken:    rgba(255,255,255,.90) !important;
  }
}

/* =========================================================
   MÓVIL — aligerar el cristal (rendimiento)
   backdrop-filter en muchas capas castiga la GPU del móvil, donde
   ya hubo problemas. En pantallas estrechas se reduce el blur y se
   sube el alpha del cristal para compensar el menor desenfoque.
   ========================================================= */
@media (max-width: 900px) {
  :root {
    --er-surface:   rgba(255,255,255,.90);
    --er-surface-2: rgba(255,255,255,.86);
    --er-sunken:    rgba(255,255,255,.82);
  }
  [data-testid="stExpander"],
  [data-testid="stForm"],
  section[data-testid="stMain"] div[data-testid="stVerticalBlockBorderWrapper"] {
    -webkit-backdrop-filter: blur(10px);
    backdrop-filter: blur(10px);
  }
}

/* =========================================================
   PANTALLA DE LOGIN
   ========================================================= */
.rs-login-cab {
  text-align:center;
  margin:clamp(24px, 6vh, 64px) 0 26px;
}
.rs-login-marca {
  font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:.68rem; letter-spacing:.34em; text-transform:uppercase;
  color:var(--er-amber); margin-bottom:18px;
}
.rs-login-titulo {
  font-family:'Fraunces',Georgia,serif;
  font-size:1.85rem; font-weight:600; color:var(--er-ink);
  letter-spacing:-.025em; line-height:1.15;
  margin:0 0 10px;
}
.rs-login-sub {
  font-size:.9rem; line-height:1.6; color:var(--er-muted);
  margin:0 auto; max-width:340px;
}
.rs-login-pie {
  text-align:center; margin-top:26px;
  font-size:.8rem; color:var(--er-faint);
}
.rs-login-pie a {
  color:var(--er-muted); text-decoration:underline;
  text-underline-offset:2px;
}
.rs-login-pie a:hover { color:var(--er-ink); }

/* El expander de "varias cuentas" es secundario: se atenúa para que no
   compita con los dos campos que de verdad importan. */
.rs-login-cab ~ div [data-testid="stExpander"] summary p {
  font-size:.8rem !important; color:var(--er-faint) !important;
}

/* =========================================================
   LAYOUT ANCHO — contener la medida de lectura
   Con layout="wide", Streamlit deja el contenido ocupar toda la
   pantalla. En un monitor de 27" eso da líneas de 200 caracteres,
   ilegibles. Se limita el ancho del área principal, no de la app.
   ========================================================= */
section[data-testid="stMain"] .block-container {
  max-width:1120px !important;
  padding-top:2.4rem !important;
  padding-bottom:4rem !important;
}

/* =========================================================
   BARRA LATERAL
   ========================================================= */
section[data-testid="stSidebar"] {
  background:var(--er-surface-2) !important;
  -webkit-backdrop-filter: blur(24px) saturate(1.6);
  backdrop-filter: blur(24px) saturate(1.6);
  border-right:1px solid var(--er-glass-edge);
  box-shadow: 1px 0 30px rgba(26,34,56,.06);
  width:290px !important;
}
section[data-testid="stSidebar"] .block-container { padding-top:1.6rem; }

/* Nombre de la agencia bajo el logo */
.rs-marca {
  font-family:'Fraunces',Georgia,serif;
  font-size:1.05rem; font-weight:600; color:var(--er-ink);
  letter-spacing:-.02em; margin:10px 0 2px; line-height:1.25;
}

/* Separador fino entre bloques de la barra */
.rs-sep {
  height:1px; background:var(--er-line);
  margin:18px 0 14px;
}

/* Etiquetita de sección dentro de la barra */
.rs-lbl {
  font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:.63rem; letter-spacing:.14em; text-transform:uppercase;
  color:var(--er-faint); margin-bottom:7px;
}

/* Metadatos del local activo */
.rs-meta {
  font-size:.78rem; color:var(--er-muted); margin-top:5px;
}

/* Bloque de plan y consumo */
.rs-plan {
  display:flex; flex-direction:column; gap:2px;
  font-size:.82rem; color:var(--er-ink); margin-bottom:8px;
}
.rs-plan span { font-size:.75rem; color:var(--er-muted); }

/* Bloque de cuenta */
.rs-cuenta {
  display:flex; flex-direction:column; gap:2px;
  font-size:.83rem; font-weight:500; color:var(--er-ink); margin-bottom:10px;
}
.rs-cuenta span {
  font-size:.72rem; font-weight:400; color:var(--er-faint);
  word-break:break-all; line-height:1.4;
}

/* Navegación: el radio de la barra convertido en menú */
section[data-testid="stSidebar"] div[role="radiogroup"] {
  gap:1px !important;
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label {
  background:transparent !important; border:none !important;
  border-radius:5px !important; padding:8px 10px !important;
  align-items:center !important;
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
  background:rgba(79,70,229,.06) !important;
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) {
  background:var(--er-accent-bg) !important;
  box-shadow:inset 3px 0 0 var(--er-accent), 0 1px 6px rgba(79,70,229,.10);
}
section[data-testid="stSidebar"] div[role="radiogroup"] p {
  font-size:.87rem !important; color:var(--er-body) !important;
}
section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) p {
  color:var(--er-accent) !important; font-weight:600 !important;
}
/* El círculo del radio sobra en un menú de navegación */
section[data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-child {
  display:none !important;
}

/* Barra de progreso del consumo */
section[data-testid="stSidebar"] [data-testid="stProgress"] > div > div {
  background:var(--er-accent) !important;
}

/* =========================================================
   SELECTOR DE VÍA — el radio nativo convertido en dos fichas
   (solo en el área principal, no en la barra lateral)
   ========================================================= */
section[data-testid="stMain"] div[role="radiogroup"] { gap:12px !important; }
section[data-testid="stMain"] div[role="radiogroup"] > label {
  flex:1; background:var(--er-surface-2);
  -webkit-backdrop-filter: blur(14px) saturate(1.4);
  backdrop-filter: blur(14px) saturate(1.4);
  border:1px solid var(--er-line); border-radius:14px;
  padding:15px 17px !important; margin:0 !important;
  cursor:pointer; transition:border-color .16s ease, background .16s ease, box-shadow .2s ease, transform .16s ease;
  align-items:flex-start !important;
  box-shadow: var(--er-shadow);
}
section[data-testid="stMain"] div[role="radiogroup"] > label:hover {
  border-color:var(--er-line-2);
  transform: translateY(-1px);
  box-shadow: var(--er-shadow-lg);
}
section[data-testid="stMain"] div[role="radiogroup"] > label:has(input:checked) {
  border-color:var(--er-accent); border-width:1.5px;
  background:rgba(255,255,255,.82);
  box-shadow:0 4px 16px rgba(79,70,229,.16);
}
section[data-testid="stMain"] div[role="radiogroup"] > label > div:first-child {
  margin-top:2px;
}
section[data-testid="stMain"] div[role="radiogroup"] p {
  font-size:.9rem !important; line-height:1.5 !important; color:var(--er-ink) !important;
}

/* =========================================================
   COMPONENTES NUEVOS DE LA V2
   ========================================================= */

/* Sello de auditoría bajo la respuesta */
.rs-sello {
  display:inline-flex; align-items:center; gap:8px;
  font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:.72rem; letter-spacing:.02em;
  color:var(--er-muted); background:rgba(26,34,56,.035);
  border:1px solid var(--er-line); border-radius:4px;
  padding:6px 11px; margin-top:10px;
}
.rs-sello.ok {
  color:var(--er-ok); border-color:rgba(47,107,79,.28); background:rgba(47,107,79,.05);
}
.rs-sello.aviso {
  color:var(--er-danger); border-color:rgba(168,50,31,.28); background:rgba(168,50,31,.05);
}

/* Etiqueta de sección (eyebrow) */
.rs-eyebrow {
  font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:.68rem; font-weight:500;
  letter-spacing:.14em; text-transform:uppercase; color:var(--er-amber);
  display:flex; align-items:center; gap:9px; margin-bottom:8px;
}
.rs-eyebrow::before { content:""; width:18px; height:1px; background:currentColor; opacity:.55; }

/* Aviso de riesgo detectado en la reseña */
.rs-riesgo {
  border:1px solid rgba(200,137,42,.35); background:rgba(200,137,42,.07);
  border-left:3px solid var(--er-amber); border-radius:6px;
  padding:13px 15px; margin:12px 0; font-size:.9rem; color:var(--er-body);
}
.rs-riesgo b { color:var(--er-ink); }

/* Progreso por etapas durante la generación */
.rs-etapas { margin:6px 0 2px; }
.rs-etapa {
  display:flex; align-items:center; gap:11px;
  font-size:.88rem; color:var(--er-muted); padding:5px 0;
}
.rs-etapa .punto {
  width:7px; height:7px; border-radius:50%;
  background:var(--er-line-2); flex-shrink:0;
}
.rs-etapa.activa { color:var(--er-ink); font-weight:500; }
.rs-etapa.activa .punto {
  background:var(--er-amber);
  box-shadow:0 0 0 3px rgba(200,137,42,.2);
  animation:rs-latido 1.1s ease-in-out infinite;
}
.rs-etapa.hecha .punto { background:var(--er-ok); }
.rs-etapa.hecha { color:var(--er-muted); }
@keyframes rs-latido {
  0%,100% { box-shadow:0 0 0 3px rgba(200,137,42,.2); }
  50%     { box-shadow:0 0 0 6px rgba(200,137,42,.08); }
}
@media (prefers-reduced-motion:reduce) {
  .rs-etapa.activa .punto { animation:none; }
}
</style>
"""


# =============================================================================
# HELPERS (sin cambios de lógica respecto a la versión anterior)
# =============================================================================

ETAPAS_RAPIDA = [
    ("leyendo", "Comprobando la reseña"),
    ("redactando", "Redactando la respuesta"),
    ("listo", "Lista"),
]

ETAPAS_BLINDADA = [
    ("leyendo", "Comprobando la reseña"),
    ("redactando", "Redactando la respuesta"),
    ("auditando", "Auditando frase por frase"),
    ("listo", "Lista"),
]


def html_etapas(etapas, etapa_actual):
    """Dibuja la lista de etapas con la actual resaltada."""
    claves = [c for c, _ in etapas]
    try:
        i_actual = claves.index(etapa_actual)
    except ValueError:
        i_actual = 0

    filas = []
    for i, (clave, texto) in enumerate(etapas):
        if i < i_actual:
            estado = "hecha"
        elif i == i_actual:
            estado = "activa"
        else:
            estado = ""
        filas.append(f'<div class="rs-etapa {estado}"><span class="punto"></span>{texto}</div>')

    return f'<div class="rs-etapas">{"".join(filas)}</div>'


def html_sello(resultado):
    """Sello de auditoría que va bajo la respuesta."""
    if resultado.violaciones_residuales:
        clase = "aviso"
    elif resultado.modo_usado == "blindado":
        clase = "ok"
    else:
        clase = ""
    return f'<div class="rs-sello {clase}">{resultado.sello}</div>'


def html_aviso_riesgo(analisis):
    """Aviso cuando la reseña tiene señales de riesgo."""
    senales = ", ".join(analisis.senales)
    return (
        f'<div class="rs-riesgo">'
        f'<b>Esta reseña parece delicada.</b> Hemos visto que {senales}. '
        f'Con el blindaje completo cada frase pasa por una auditoría legal '
        f'independiente antes de que la veas. Tarda unos segundos más.'
        f'</div>'
    )


def html_eyebrow(texto):
    return f'<div class="rs-eyebrow">{texto}</div>'
