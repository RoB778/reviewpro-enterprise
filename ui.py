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
   SELECTOR DE VÍA — el radio nativo convertido en dos fichas
   Usa las variables --er-* del sistema de diseño existente.
   ========================================================= */
div[role="radiogroup"] { gap:12px !important; }
div[role="radiogroup"] > label {
  flex:1; background:var(--er-surface);
  border:1px solid var(--er-line); border-radius:8px;
  padding:15px 17px !important; margin:0 !important;
  cursor:pointer; transition:border-color .16s ease, background .16s ease;
  align-items:flex-start !important;
}
div[role="radiogroup"] > label:hover { border-color:var(--er-line-2); }
div[role="radiogroup"] > label:has(input:checked) {
  border-color:var(--er-accent); border-width:1.5px;
  background:#fff;
  box-shadow:0 1px 2px rgba(26,34,56,.05);
}
div[role="radiogroup"] > label > div:first-child { margin-top:2px; }
div[role="radiogroup"] p {
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
