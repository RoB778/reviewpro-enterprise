import json
from datetime import datetime, timedelta

import bcrypt
import streamlit as st
from anthropic import Anthropic
from supabase import create_client

# Configuración de las claves secretas de los servidores
client = Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
supabase = create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

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


def verificar_password(password_plano, password_hash):
    """Compara una contraseña en texto plano contra su hash bcrypt almacenado."""
    try:
        return bcrypt.checkpw(password_plano.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, AttributeError):
        return False


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


def registrar_respuesta_en_historico(agencia_id, local_id, usuario_id, sentimiento, idioma_detectado, longitud_palabras):
    """Guarda una fila en historico_respuestas cada vez que se genera una respuesta con éxito."""
    try:
        supabase.table("historico_respuestas").insert({
            "agencia_id": agencia_id,
            "local_id": local_id,
            "usuario_id": usuario_id,
            "sentimiento": sentimiento,
            "idioma_detectado": idioma_detectado,
            "longitud_palabras": longitud_palabras
        }).execute()
    except Exception:
        # Si falla el registro de analítica, no debe romper la generación de la respuesta.
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

# =========================================================
# 🔑 PANTALLA DE LOGIN — EMAIL + CONTRASEÑA (MULTI-USUARIO)
# =========================================================
if not st.session_state.sesion_activa:
    st.title("🔑 Acceso Corporativo - ReviewPro Enterprise")
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
                    st.error(f"Error de conexión con la base de datos: {e}")

    st.divider()
    st.caption("¿Tu agencia todavía no tiene acceso? Contacta con nosotros para el alta Enterprise.")
    st.stop()

# A partir de aquí: sesión válida.
agencia = st.session_state.agencia_actual
usuario = st.session_state.usuario_actual
color_agencia = agencia["color_marca"]

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
        st.rerun()

st.markdown(f"<hr style='border-top:3px solid {color_agencia}; margin-top:4px;'>", unsafe_allow_html=True)
st.info(f"Sesión activa: **{usuario['nombre_usuario']}** ({usuario['email']}) · Rol: {usuario['rol']}")

# =========================================================
# 🧭 NAVEGACIÓN: GENERAR RESPUESTA / VER ANALÍTICA
# =========================================================
tab_generar, tab_analitica = st.tabs(["✨ Generar respuesta", "📊 Analítica de la agencia"])

# ---------------------------------------------------------
# PESTAÑA 1: GENERACIÓN DE RESPUESTAS
# ---------------------------------------------------------
with tab_generar:
    locales_disponibles = st.session_state.locales_agencia

    if not locales_disponibles:
        st.error("⚠️ Esta agencia no tiene ningún local registrado en su cartera. Contacta con soporte.")
        st.stop()

    nombres_locales = [local["nombre"] for local in locales_disponibles]
    nombre_local_elegido = st.selectbox("🏬 Selecciona el local:", options=nombres_locales, key="selector_local_activo")

    local_activo = next(local for local in locales_disponibles if local["nombre"] == nombre_local_elegido)
    st.session_state.local_activo = local_activo

    st.caption(f"Nicho: **{local_activo['nicho']}** · {len(local_activo['seo_keywords'])} keywords SEO cargadas.")

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
        else:
            with st.spinner("ReviewPro Enterprise está redactando tu respuesta estratégica..."):
                try:
                    nombre_local_final = local_activo["nombre"]
                    nicho_local = local_activo["nicho"]
                    keywords_texto = ", ".join(local_activo["seo_keywords"])

                    system_prompt_dinamico = f"""Eres un consultor senior de gestión de reputación online con 15 años de experiencia en relaciones públicas y hostelería internacional. Tu tarea es redactar una respuesta pública a una reseña que puede ser POSITIVA o NEGATIVA.

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

REGLAS DE REDACCIÓN SEGÚN EL SENTIMIENTO:
1. TONO OBLIGADO: {tono}. Educado, profesional y constructivo.
2. SI ES POSITIVA: agradecimiento genuino, referencia a los puntos fuertes, invitación a volver.
3. SI ES NEGATIVA:
   - Inicio dinámico: prohibido empezar siempre con "Gracias por su comentario" o equivalentes; varía la apertura.
   - BLINDAJE JURÍDICO TOTAL: prohibido admitir negligencias o usar alertas sanitarias ("higiene alimentaria", "intoxicación"); usa perífrasis suaves ("nuestros estándares de calidad", "lo sucedido").
   - Filtro de gravedad: si describe algo grave (salubridad severa, insectos, insultos), invita a resolverlo por vía privada. Si es un fallo leve (esperas, comida fría, precios), discúlpate cercano y humano, sin exigir contacto privado.

REGLAS DE LONGITUD:
- POSITIVA: entre 60 y 100 palabras.
- NEGATIVA: entre 140 y 200 palabras, desarrollando: (a) reconocimiento genuino, (b) breve contextualización con perífrasis seguras, (c) qué se está haciendo al respecto, (d) cierre cordial invitando a otra oportunidad. Sin frases vacías repetidas.
- Nunca fuerces el límite superior si la reseña es muy breve y no lo justifica.

REGLAS COMUNES:
- Integra el nombre del negocio ({nombre_local_final}) de forma fluida.
- Sin asteriscos, comillas externas, emojis ni encabezados.

REGLAS DE SEO (INVISIBLE PARA EL CLIENTE FINAL):
- Nicho del negocio: {nicho_local}.
- Integra de forma fluida y natural al menos 2-3 de estas palabras clave donde el contexto lo permita: {keywords_texto}.
- Nunca menciones que estás optimizando para SEO ni las enumeres como etiquetas.
- La naturalidad del texto siempre prevalece sobre la densidad de keywords."""

                    response = client.messages.create(
                        model="claude-sonnet-5",
                        max_tokens=1000,
                        system=system_prompt_dinamico,
                        messages=[{"role": "user", "content": f"Nombre del negocio: {nombre_local_final}\nReseña: \"\"\"{resena_cliente}\"\"\""}]
                    )

                    texto_bruto = response.content[0].text.strip()
                    datos_respuesta = json.loads(texto_bruto)

                    respuesta_nativa = datos_respuesta.get("respuesta_nativa", "").replace("*", "").replace('"', "")
                    traduccion = datos_respuesta.get("traduccion_espanol")
                    idioma_detectado = datos_respuesta.get("idioma_detectado", "es")
                    sentimiento = datos_respuesta.get("sentimiento", "positivo")

                    st.success("Respuesta generada con éxito:")

                    if traduccion:
                        st.subheader("📋 Texto para copiar y pegar en tu reseña:")
                        st.text_area("Respuesta oficial (Nativa):", value=respuesta_nativa, height=150)
                        st.info(f"🌐 **Traducción al español para el propietario:**\n\n{traduccion}")
                    else:
                        st.text_area("Copia este texto y pégalo directamente:", value=respuesta_nativa, height=180)

                    registrar_respuesta_en_historico(
                        agencia_id=agencia["id"],
                        local_id=local_activo["id"],
                        usuario_id=usuario["id"],
                        sentimiento=sentimiento,
                        idioma_detectado=idioma_detectado,
                        longitud_palabras=len(respuesta_nativa.split())
                    )

                except json.JSONDecodeError:
                    st.error("El modelo devolvió un formato inesperado. Inténtalo de nuevo.")
                except Exception as e:
                    st.error(f"Error al conectar con el servidor: {e}")

# ---------------------------------------------------------
# PESTAÑA 2: ANALÍTICA DE LA AGENCIA
# ---------------------------------------------------------
with tab_analitica:
    st.subheader("📊 Actividad de tu agencia")

    rango = st.radio("Periodo:", ["Últimos 7 días", "Últimos 30 días", "Todo el histórico"], horizontal=True)
    if rango == "Últimos 7 días":
        fecha_desde = (datetime.utcnow() - timedelta(days=7)).isoformat()
    elif rango == "Últimos 30 días":
        fecha_desde = (datetime.utcnow() - timedelta(days=30)).isoformat()
    else:
        fecha_desde = "1970-01-01T00:00:00"

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

            col1, col2, col3 = st.columns(3)
            col1.metric("Respuestas generadas", total_respuestas)
            col2.metric("Reseñas positivas", positivas, f"{round(positivas/total_respuestas*100)}%")
            col3.metric("Reseñas negativas", negativas, f"{round(negativas/total_respuestas*100)}%")

            # Actividad por local
            conteo_por_local = {}
            id_a_nombre_local = {l["id"]: l["nombre"] for l in st.session_state.locales_agencia}
            for fila in historico:
                nombre_local = id_a_nombre_local.get(fila["local_id"], "Local desconocido")
                conteo_por_local[nombre_local] = conteo_por_local.get(nombre_local, 0) + 1

            st.markdown("**Actividad por local:**")
            st.bar_chart(conteo_por_local)

            # Actividad por usuario (visibilidad multi-usuario)
            conteo_por_usuario = {}
            for fila in historico:
                conteo_por_usuario[fila["usuario_id"]] = conteo_por_usuario.get(fila["usuario_id"], 0) + 1

            st.markdown("**Reparto de trabajo por usuario del equipo:**")
            st.caption("Útil para ver qué gestores de tu agencia están usando más la herramienta.")
            st.bar_chart(conteo_por_usuario)

    except Exception as e:
        st.error(f"No se pudo cargar la analítica: {e}")

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
