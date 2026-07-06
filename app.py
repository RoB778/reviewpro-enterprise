import json
import re
import urllib.parse
from datetime import datetime, timedelta
from io import BytesIO

import bcrypt
import qrcode
import requests
import streamlit as st
from anthropic import Anthropic
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
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


LIMITE_USOS_PLAN_GRATIS = 10  # respuestas por mes incluidas en el plan Free
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Enlaces de pago de Stripe (Payment Links) — sustituye estas URLs por las reales
# que generes en tu Dashboard de Stripe para cada plan.
ENLACE_PAGO_STARTER = "https://buy.stripe.com/tu-enlace-starter"
ENLACE_PAGO_GROWTH = "https://buy.stripe.com/tu-enlace-growth"
ENLACE_PAGO_ENTERPRISE = "https://buy.stripe.com/tu-enlace-enterprise"


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


def generar_informe_pdf_mensual(agencia, historico, locales_agencia, id_a_nombre_usuario, periodo_texto):
    """
    Genera un informe PDF de marca blanca con el logo y color de la agencia,
    resumiendo la actividad de un periodo concreto. Devuelve los bytes del PDF.
    """
    buffer = BytesIO()
    color_hex = agencia.get("color_marca", "#635BFF").lstrip("#")
    color_rl = colors.HexColor(f"#{color_hex}")

    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    estilos = getSampleStyleSheet()
    estilo_titulo = ParagraphStyle("TituloInforme", parent=estilos["Title"], textColor=color_rl, fontSize=20)
    estilo_subtitulo = ParagraphStyle("Subtitulo", parent=estilos["Normal"], textColor=colors.grey, fontSize=11)
    estilo_seccion = ParagraphStyle("Seccion", parent=estilos["Heading2"], textColor=color_rl, spaceBefore=14)

    story = []

    # Logo (si se puede descargar; si falla, se omite sin romper el informe)
    try:
        resp_logo = requests.get(agencia["logo_url"], timeout=5)
        imagen_logo = RLImage(BytesIO(resp_logo.content), width=4 * cm, height=1.2 * cm)
        story.append(imagen_logo)
        story.append(Spacer(1, 10))
    except Exception:
        pass

    story.append(Paragraph(f"Informe de reputación online", estilo_titulo))
    story.append(Paragraph(f"{agencia['nombre_agencia']} · {periodo_texto}", estilo_subtitulo))
    story.append(Spacer(1, 16))

    total = len(historico)
    positivas = sum(1 for r in historico if r["sentimiento"] == "positivo")
    negativas = total - positivas
    pct_positivas = round(positivas / total * 100) if total else 0

    story.append(Paragraph("Resumen del periodo", estilo_seccion))
    tabla_resumen = Table([
        ["Respuestas generadas", "Reseñas positivas", "Reseñas negativas", "% positivas"],
        [str(total), str(positivas), str(negativas), f"{pct_positivas}%"]
    ], colWidths=[4 * cm, 4 * cm, 4 * cm, 3 * cm])
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
    story.append(Spacer(1, 16))

    story.append(Paragraph("Actividad por local", estilo_seccion))
    id_a_nombre_local = {l["id"]: l["nombre"] for l in locales_agencia}
    conteo_local = {}
    for fila in historico:
        nombre = id_a_nombre_local.get(fila["local_id"], "Local desconocido")
        conteo_local[nombre] = conteo_local.get(nombre, 0) + 1

    filas_tabla_local = [["Local", "Respuestas generadas"]] + [[nombre, str(n)] for nombre, n in conteo_local.items()]
    tabla_locales = Table(filas_tabla_local, colWidths=[10 * cm, 5 * cm])
    tabla_locales.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A3448")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(tabla_locales)
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

    system_prompt = f"""Eres un redactor senior de marketing local especializado en SEO. Vas a escribir contenido corto para el negocio "{nombre_local}", cuyo nicho es "{nicho}".

Integra de forma natural, sin forzar, al menos 1-2 de estas palabras clave si el contexto lo permite: {keywords_texto}.

Instrucción específica para este contenido: {instrucciones_por_tipo[tipo_contenido]}

Devuelve EXCLUSIVAMENTE el texto final, sin comillas, sin explicaciones, sin encabezados, sin markdown."""

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=300,
        system=system_prompt,
        messages=[{"role": "user", "content": f"Genera el contenido para {nombre_local}."}]
    )

    for bloque in response.content:
        if getattr(bloque, "type", None) == "text":
            return bloque.text.strip().strip('"')
    return ""


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
if "vista_landing" not in st.session_state:
    st.session_state.vista_landing = "info"

# =========================================================
# 🔑 LANDING: PLANES Y PRECIOS + LOGIN
# =========================================================
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
        col_free, col_starter, col_growth, col_ent = st.columns(4)

        with col_free:
            st.markdown(f"""
                <div class="rp-card">
                    <div class="rp-plan-nombre">Free</div>
                    <div class="rp-plan-target">Para probar antes de decidir</div>
                    <div class="rp-precio">0€</div>
                    <div class="rp-precio-periodo">para siempre</div>
                    <hr style="border-color:#232C42; margin:14px 0;">
                    <div class="rp-feature">✓ 1 local de prueba</div>
                    <div class="rp-feature">✓ {LIMITE_USOS_PLAN_GRATIS} respuestas / mes</div>
                    <div class="rp-feature">✓ Sin tarjeta de crédito</div>
                    <div class="rp-feature" style="opacity:0.4;">✗ Marca blanca</div>
                    <div class="rp-feature" style="opacity:0.4;">✗ Multi-usuario</div>
                </div>
            """, unsafe_allow_html=True)
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

        with col_starter:
            st.markdown(f"""
                <div class="rp-card">
                    <div class="rp-plan-nombre">Starter</div>
                    <div class="rp-plan-target">Agencias pequeñas · hasta 10 locales</div>
                    <div class="rp-precio">69€</div>
                    <div class="rp-precio-periodo">/ mes</div>
                    <hr style="border-color:#232C42; margin:14px 0;">
                    <div class="rp-feature">✓ Hasta 10 locales</div>
                    <div class="rp-feature">✓ Respuestas ilimitadas</div>
                    <div class="rp-feature">✓ Marca blanca completa</div>
                    <div class="rp-feature">✓ SEO invisible por local</div>
                </div>
            """, unsafe_allow_html=True)
            st.markdown(f'<a href="{ENLACE_PAGO_STARTER}" target="_blank" style="text-decoration:none;"><div style="background:#FFB454;color:#0B1120;text-align:center;padding:10px;border-radius:8px;font-weight:600;margin-top:8px;">Elegir Starter</div></a>', unsafe_allow_html=True)

        with col_growth:
            st.markdown(f"""
                <div class="rp-card rp-card-destacado">
                    <span class="rp-badge">MÁS ELEGIDO</span>
                    <div class="rp-plan-nombre">Growth</div>
                    <div class="rp-plan-target">Agencias medianas · hasta 30 locales</div>
                    <div class="rp-precio">169€</div>
                    <div class="rp-precio-periodo">/ mes</div>
                    <hr style="border-color:#232C42; margin:14px 0;">
                    <div class="rp-feature">✓ Hasta 30 locales</div>
                    <div class="rp-feature">✓ Respuestas ilimitadas</div>
                    <div class="rp-feature">✓ Marca blanca completa</div>
                    <div class="rp-feature">✓ Multi-usuario + analítica</div>
                </div>
            """, unsafe_allow_html=True)
            st.markdown(f'<a href="{ENLACE_PAGO_GROWTH}" target="_blank" style="text-decoration:none;"><div style="background:#FFB454;color:#0B1120;text-align:center;padding:10px;border-radius:8px;font-weight:600;margin-top:8px;">Elegir Growth</div></a>', unsafe_allow_html=True)

        with col_ent:
            st.markdown(f"""
                <div class="rp-card">
                    <div class="rp-plan-nombre">Enterprise</div>
                    <div class="rp-plan-target">Agencias grandes · locales ilimitados</div>
                    <div class="rp-precio">269€+</div>
                    <div class="rp-precio-periodo">/ mes</div>
                    <hr style="border-color:#232C42; margin:14px 0;">
                    <div class="rp-feature">✓ Locales ilimitados</div>
                    <div class="rp-feature">✓ Soporte prioritario</div>
                    <div class="rp-feature">✓ Marca blanca completa</div>
                    <div class="rp-feature">✓ Multi-usuario + analítica</div>
                </div>
            """, unsafe_allow_html=True)
            st.markdown(f'<a href="{ENLACE_PAGO_ENTERPRISE}" target="_blank" style="text-decoration:none;"><div style="background:#FFB454;color:#0B1120;text-align:center;padding:10px;border-radius:8px;font-weight:600;margin-top:8px;">Elegir Enterprise</div></a>', unsafe_allow_html=True)

        st.caption("Tras pagar en Stripe, recibirás tus credenciales de acceso en un plazo máximo de 24h mientras completamos tu alta.")

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
                        st.error(f"Error de conexión con la base de datos: {e}")

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

    if not locales_disponibles:
        st.error("⚠️ Esta agencia no tiene ningún local registrado en su cartera. Contacta con soporte.")
        st.stop()

    nombres_locales = [local["nombre"] for local in locales_disponibles]
    nombre_local_elegido = st.selectbox("🏬 Selecciona el local:", options=nombres_locales, key="selector_local_activo")

    local_activo = next(local for local in locales_disponibles if local["nombre"] == nombre_local_elegido)
    st.session_state.local_activo = local_activo

    st.caption(f"Nicho: **{local_activo['nicho']}** · {len(local_activo['seo_keywords'])} keywords SEO cargadas.")

    if agencia.get("plan") == "free":
        usos_hechos = contar_usos_del_mes(agencia["id"])
        restantes = max(0, LIMITE_USOS_PLAN_GRATIS - usos_hechos)
        st.info(f"🎁 Plan Free: te quedan **{restantes} de {LIMITE_USOS_PLAN_GRATIS}** respuestas este mes.")

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
        elif agencia.get("plan") == "free" and contar_usos_del_mes(agencia["id"]) >= LIMITE_USOS_PLAN_GRATIS:
            st.error(f"⚠️ Has usado tus {LIMITE_USOS_PLAN_GRATIS} respuestas gratuitas de este mes. Actualiza tu plan para seguir generando sin límite.")
            st.markdown(f'<a href="{ENLACE_PAGO_STARTER}" target="_blank"><button style="background-color:{color_agencia};color:white;padding:10px 20px;border:none;border-radius:8px;font-weight:bold;cursor:pointer;">💳 Ver planes de pago</button></a>', unsafe_allow_html=True)
        else:
            with st.spinner("Analizando el idioma y el tono de la reseña..."):
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
                        longitud_palabras=len(respuesta_nativa.split())
                    )

                except json.JSONDecodeError:
                    st.error("El modelo devolvió un formato inesperado. Inténtalo de nuevo.")
                except Exception as e:
                    st.error(f"Error al conectar con el servidor: {e}")

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
                st.error(f"No se pudo guardar: {e}")

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
                except Exception as e:
                    st.error(f"Error al generar el contenido: {e}")

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
            usuarios_de_la_agencia = supabase.table("usuarios").select("id, nombre_usuario").eq("agencia_id", agencia["id"]).execute().data
            id_a_nombre_usuario = {u["id"]: u["nombre_usuario"] for u in usuarios_de_la_agencia}

            conteo_por_usuario = {}
            for fila in historico:
                nombre_usuario_fila = id_a_nombre_usuario.get(fila["usuario_id"], "Usuario eliminado")
                conteo_por_usuario[nombre_usuario_fila] = conteo_por_usuario.get(nombre_usuario_fila, 0) + 1

            st.markdown("**Reparto de trabajo por usuario del equipo:**")
            st.caption("Útil para ver qué gestores de tu agencia están usando más la herramienta.")
            st.bar_chart(conteo_por_usuario)

            st.divider()
            st.markdown("**📄 Informe de marca blanca para reenviar a tus clientes:**")
            try:
                pdf_bytes = generar_informe_pdf_mensual(
                    agencia, historico, st.session_state.locales_agencia, id_a_nombre_usuario, rango
                )
                st.download_button(
                    "⬇️ Descargar informe PDF",
                    data=pdf_bytes,
                    file_name=f"informe_{agencia['nombre_agencia'].replace(' ', '_')}.pdf",
                    mime="application/pdf"
                )
            except Exception as e:
                st.error(f"No se pudo generar el informe: {e}")

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
