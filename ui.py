# -*- coding: utf-8 -*-
"""
=============================================================================
RESELIA — CAPA DE INTERFAZ
=============================================================================

Todo el CSS y los componentes visuales viven aquí, fuera de app.py.

PRINCIPIO DE DISEÑO
-------------------
Streamlit por defecto parece lo que es: un cuadro de mandos interno. Bordes
gruesos, gris azulado, tipografía de sistema, todo con el mismo peso visual.
Funciona, pero no transmite que detrás hay un producto por el que alguien
paga 179 € al mes.

El objetivo aquí no es "poner colores bonitos". Es que la jerarquía visual
cuente la misma historia que el producto:

  · Lo que el usuario va a COPIAR (la respuesta) es lo más contrastado de
    la pantalla. Todo lo demás cede protagonismo.
  · Lo que da CONFIANZA (el sello de auditoría, las reglas superadas) se ve,
    pero en un registro técnico y sobrio: monoespaciada, gris, pequeño. Si
    grita, parece marketing; si susurra, parece un certificado.
  · Lo que es RIESGO (avisos, violaciones) usa el único rojo del sistema, y
    solo ahí. Un color que aparece en todas partes no avisa de nada.

PALETA — "Tinta & Papel"
------------------------
Tinta   #1a2238   Papel  #f7f4ee   Ámbar  #c8892a
Es la misma de la landing, a propósito: el usuario debe reconocer que está
en el mismo producto.
=============================================================================
"""

TINTA = "#1a2238"
TINTA_80 = "#2b3552"
TINTA_60 = "#4a5570"
PAPEL = "#f7f4ee"
PAPEL_PURO = "#fdfbf7"
AMBAR = "#c8892a"
AMBAR_CLARO = "#dfa445"
VERDE = "#2c6349"
ROJO = "#9e2f1d"
GRIS = "#6d7385"


CSS_GLOBAL = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght,SOFT,WONK@0,9..144,400..700,0..100,0..1;1,9..144,400..700,0..100,0..1&family=Inter:wght@400;500;600;700&display=swap');

:root {{
  --tinta:{TINTA}; --tinta-80:{TINTA_80}; --tinta-60:{TINTA_60};
  --papel:{PAPEL}; --papel-puro:{PAPEL_PURO};
  --ambar:{AMBAR}; --ambar-claro:{AMBAR_CLARO};
  --verde:{VERDE}; --rojo:{ROJO}; --gris:{GRIS};
  --linea:rgba(26,34,56,.13);
  --linea-fuerte:rgba(26,34,56,.26);
  --mono:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;
  --r:6px;
}}

/* ---------- Lienzo ---------- */
[data-testid="stAppViewContainer"] {{
  background:var(--papel);
  font-family:'Inter',system-ui,-apple-system,sans-serif;
  color:var(--tinta);
}}
[data-testid="stHeader"] {{ background:transparent; }}
.block-container {{ padding-top:2.2rem; max-width:1080px; }}

/* ---------- Tipografía ---------- */
h1,h2,h3 {{
  font-family:'Fraunces',Georgia,serif !important;
  font-variation-settings:'SOFT' 0,'WONK' 0;
  letter-spacing:-.02em !important;
  color:var(--tinta) !important;
  font-weight:600 !important;
}}
h1 {{ font-size:2.1rem !important; line-height:1.14 !important; }}
h2 {{ font-size:1.5rem !important; }}
h3 {{ font-size:1.15rem !important; }}
p, li, label, .stMarkdown {{ color:var(--tinta-80); }}

/* ---------- Pestañas: de solapas grises a navegación editorial ---------- */
.stTabs [data-baseweb="tab-list"] {{
  gap:26px; border-bottom:1px solid var(--linea);
  background:transparent; padding-bottom:0;
}}
.stTabs [data-baseweb="tab"] {{
  background:transparent !important; border:none !important;
  padding:10px 0 12px !important; font-size:.92rem !important;
  font-weight:500 !important; color:var(--gris) !important;
  border-radius:0 !important;
}}
.stTabs [aria-selected="true"] {{
  color:var(--tinta) !important; font-weight:600 !important;
  box-shadow:inset 0 -2px 0 var(--tinta);
}}
.stTabs [data-baseweb="tab-highlight"],
.stTabs [data-baseweb="tab-border"] {{ display:none !important; }}

/* ---------- Campos ---------- */
[data-testid="stTextArea"] textarea,
[data-testid="stTextInput"] input {{
  background:var(--papel-puro) !important;
  border:1px solid var(--linea) !important;
  border-radius:var(--r) !important;
  color:var(--tinta) !important;
  font-size:.95rem !important;
  padding:12px 14px !important;
}}
[data-testid="stTextArea"] textarea:focus,
[data-testid="stTextInput"] input:focus {{
  border-color:var(--tinta) !important;
  box-shadow:0 0 0 3px rgba(26,34,56,.08) !important;
}}
[data-testid="stWidgetLabel"] label p {{
  font-size:.86rem !important; font-weight:500 !important;
  color:var(--tinta-60) !important;
}}

/* ---------- Botones ---------- */
.stButton button, [data-testid="stFormSubmitButton"] button {{
  background:var(--tinta) !important; color:var(--papel-puro) !important;
  border:1px solid var(--tinta) !important; border-radius:var(--r) !important;
  font-weight:600 !important; font-size:.92rem !important;
  padding:11px 22px !important; letter-spacing:-.004em !important;
  box-shadow:none !important; transition:background .16s ease;
}}
.stButton button:hover, [data-testid="stFormSubmitButton"] button:hover {{
  background:{TINTA_80} !important; border-color:{TINTA_80} !important;
}}
.stButton button p, [data-testid="stFormSubmitButton"] button p {{
  color:var(--papel-puro) !important; font-weight:600 !important;
}}

/* ---------- Formulario: contenedor con aire ---------- */
[data-testid="stForm"] {{
  background:var(--papel-puro);
  border:1px solid var(--linea);
  border-radius:10px; padding:26px 24px;
  box-shadow:0 1px 2px rgba(26,34,56,.04);
}}

/* ---------- Bloques de código: la respuesta a copiar ---------- */
[data-testid="stCode"] {{
  background:var(--papel-puro) !important;
  border:1px solid var(--linea-fuerte) !important;
  border-left:3px solid var(--tinta) !important;
  border-radius:var(--r) !important;
}}
[data-testid="stCode"] pre, [data-testid="stCode"] code {{
  font-family:'Inter',sans-serif !important;
  font-size:1rem !important; line-height:1.66 !important;
  color:var(--tinta) !important; white-space:pre-wrap !important;
}}

/* ---------- Avisos ---------- */
[data-testid="stAlert"] {{
  border-radius:var(--r) !important; border-width:1px !important;
  font-size:.92rem !important;
}}

/* ---------- Expanders ---------- */
[data-testid="stExpander"] {{
  border:1px solid var(--linea) !important; border-radius:var(--r) !important;
  background:var(--papel-puro) !important;
}}
[data-testid="stExpander"] summary {{ font-size:.9rem !important; font-weight:500 !important; }}

/* ---------- Métricas ---------- */
[data-testid="stMetricValue"] {{
  font-family:'Fraunces',Georgia,serif !important;
  font-variation-settings:'SOFT' 0,'WONK' 0;
  font-size:1.9rem !important; color:var(--tinta) !important;
}}
[data-testid="stMetricLabel"] {{
  font-family:var(--mono) !important; font-size:.68rem !important;
  letter-spacing:.1em !important; text-transform:uppercase !important;
  color:var(--gris) !important;
}}

/* =========================================================
   SELECTOR DE VÍA — el radio nativo convertido en dos fichas
   ========================================================= */
div[role="radiogroup"] {{ gap:12px !important; }}
div[role="radiogroup"] > label {{
  flex:1; background:var(--papel-puro);
  border:1px solid var(--linea); border-radius:8px;
  padding:15px 17px !important; margin:0 !important;
  cursor:pointer; transition:border-color .16s ease, background .16s ease;
  align-items:flex-start !important;
}}
div[role="radiogroup"] > label:hover {{ border-color:var(--linea-fuerte); }}
div[role="radiogroup"] > label:has(input:checked) {{
  border-color:var(--tinta); border-width:1.5px;
  background:#fff;
  box-shadow:0 1px 2px rgba(26,34,56,.05);
}}
div[role="radiogroup"] > label > div:first-child {{ margin-top:2px; }}
div[role="radiogroup"] p {{
  font-size:.9rem !important; line-height:1.5 !important; color:var(--tinta) !important;
}}

/* =========================================================
   COMPONENTES PROPIOS
   ========================================================= */

/* Sello de auditoría bajo la respuesta */
.rs-sello {{
  display:inline-flex; align-items:center; gap:8px;
  font-family:var(--mono); font-size:.72rem; letter-spacing:.02em;
  color:var(--gris); background:rgba(26,34,56,.035);
  border:1px solid var(--linea); border-radius:4px;
  padding:6px 11px; margin-top:10px;
}}
.rs-sello.ok {{ color:var(--verde); border-color:rgba(44,99,73,.28); background:rgba(44,99,73,.05); }}
.rs-sello.aviso {{ color:var(--rojo); border-color:rgba(158,47,29,.28); background:rgba(158,47,29,.05); }}

/* Etiqueta de sección */
.rs-eyebrow {{
  font-family:var(--mono); font-size:.68rem; font-weight:500;
  letter-spacing:.14em; text-transform:uppercase; color:var(--ambar);
  display:flex; align-items:center; gap:9px; margin-bottom:8px;
}}
.rs-eyebrow::before {{ content:""; width:18px; height:1px; background:currentColor; opacity:.55; }}

/* Aviso de riesgo detectado en la reseña */
.rs-riesgo {{
  border:1px solid rgba(200,137,42,.35); background:rgba(200,137,42,.07);
  border-left:3px solid var(--ambar); border-radius:var(--r);
  padding:13px 15px; margin:12px 0; font-size:.9rem; color:var(--tinta-80);
}}
.rs-riesgo b {{ color:var(--tinta); }}

/* Progreso por etapas durante la generación */
.rs-etapas {{ margin:6px 0 2px; }}
.rs-etapa {{
  display:flex; align-items:center; gap:11px;
  font-size:.88rem; color:var(--gris); padding:5px 0;
}}
.rs-etapa .punto {{
  width:7px; height:7px; border-radius:50%;
  background:var(--linea-fuerte); flex-shrink:0;
}}
.rs-etapa.activa {{ color:var(--tinta); font-weight:500; }}
.rs-etapa.activa .punto {{
  background:var(--ambar);
  box-shadow:0 0 0 3px rgba(200,137,42,.2);
  animation:rs-latido 1.1s ease-in-out infinite;
}}
.rs-etapa.hecha .punto {{ background:var(--verde); }}
.rs-etapa.hecha {{ color:var(--tinta-60); }}
@keyframes rs-latido {{
  0%,100% {{ box-shadow:0 0 0 3px rgba(200,137,42,.2); }}
  50%     {{ box-shadow:0 0 0 6px rgba(200,137,42,.08); }}
}}
@media (prefers-reduced-motion:reduce) {{
  .rs-etapa.activa .punto {{ animation:none; }}
}}
</style>
"""


# =============================================================================
# COMPONENTES
# =============================================================================

# Las etapas por las que pasa una generación, en orden. La vía rápida se
# salta "auditando" y "corrigiendo".
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
    """
    Dibuja la lista de etapas con la actual resaltada.

    Esto no es decoración. Un spinner mudo de quince segundos hace pensar
    que la app se ha colgado; ver "Auditando frase por frase" convierte la
    espera en la demostración de lo que se está pagando.
    """
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
    """Aviso cuando la reseña tiene señales de riesgo y se ha elegido vía rápida."""
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
