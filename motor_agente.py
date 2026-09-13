# -*- coding: utf-8 -*-
"""
motor_agente.py — EL ASISTENTE DE CRECIMIENTO LOCAL de Reselia
=============================================================================

QUÉ ES Y POR QUÉ EXISTE
-----------------------
Este es el foso comercial del plan Individual. La objeción número uno al vender
a un autónomo es "esto ya lo hago con ChatGPT". Y tienen razón: un chat genérico
con un buen prompt de SEO NO es defendible, porque ese conocimiento ya está en
ChatGPT gratis.

El foso no es lo que el agente SABE (SEO genérico, imitable). El foso es lo que
el agente VE: las reseñas reales de ESE negocio, su Ficha de Verdad verificada,
su Reputation Score, sus keywords, su histórico. ChatGPT no tiene acceso a nada
de eso. Este agente sí.

El pitch: "El único asistente que se ha leído todas tus reseñas."

FILOSOFÍA
---------
No es un chatbot que responde preguntas. Es un empleado digital de marketing que:
  1. VE los datos reales del negocio (mediante herramientas).
  2. DETECTA oportunidades concretas ("8 reseñas mencionan tu terraza").
  3. PROPONE acciones ancladas a hechos verificados (nunca inventa).
  4. GENERA el contenido listo para usar (reutiliza el motor SEO anclado).

DOMINIO ACOTADO — la frontera que protege la marca
--------------------------------------------------
Reselia vende que NO genera responsabilidad legal. Un asistente que opine sobre
despidos, impuestos o si un tratamiento clínico se puede publicitar crearía
exactamente el riesgo que la marca promete evitar. Por eso el agente tiene un
dominio DURO:

  DENTRO:  reputación online, reseñas, SEO local y GEO, redes sociales,
           captación de clientes, contenido, cómo vender más en términos de
           marketing y presencia.
  FUERA:   laboral, fiscal, contable, jurídico, sanitario/clínico, seguridad
           alimentaria (APPCC), y cualquier cosa que exija un profesional
           colegiado. Ante estas, redirige con naturalidad y elegancia — sin
           sonar a robot que se niega — y reconduce a lo que sí puede hacer.

Aislado como blindaje.py y motor_seo.py para no inflar app.py.
"""

from __future__ import annotations

import re

# COSTE: el agente encadena varias llamadas por turno (herramientas + reenvío
# del historial completo en cada vuelta, así funciona tool use en la API), así
# que el volumen de tokens por conversación es alto. Con Sonnet en las
# primeras pruebas salía a ~10 céntimos por consulta — insostenible con 200
# mensajes/mes por local. Haiku 4.5 cuesta aprox 1/3 de Sonnet en input y
# output, y aquí es suficiente: las tareas son leer datos, detectar patrones
# simples y redactar en tono cercano — no exige el razonamiento fino que sí
# necesitan blindaje.py y motor_seo.py (esos SIGUEN en Sonnet a propósito:
# ahí un error cuesta caro de verdad, blindaje legal y anti-alucinación).
MODELO_AGENTE = "claude-haiku-4-5-20251001"

# Presupuesto de la conversación: el agente puede encadenar varias llamadas a
# herramientas por turno (leer reseñas -> analizar temas -> generar). Acotamos
# las vueltas para que un turno no se dispare en coste.
MAX_VUELTAS_HERRAMIENTAS = 6

# Tope de tokens por respuesta del modelo.
MAX_TOKENS_AGENTE = 1500


# =============================================================================
# SYSTEM PROMPT — la personalidad y las reglas del agente
# =============================================================================

def construir_system_prompt(nombre_local, nicho, ciudad):
    zona = (ciudad or "").strip()
    ref = f"{nombre_local} ({nicho}" + (f", {zona}" if zona else "") + ")"

    return f"""Eres el asistente de crecimiento de Reselia: un consultor de negocio y marketing con criterio, útil de verdad, al estilo de un buen asistente de IA general pero con una ventaja que ningún chat genérico tiene — ves los datos reales de {ref}. Hablas de forma cercana, directa y práctica, nunca como un chatbot que se escuda en lo que "no le corresponde".

CON QUIÉN HABLAS
Puede que hable contigo el propio dueño del negocio, o un gestor / agencia de marketing que administra este negocio (y probablemente varios más) usando Reselia. No siempre sabrás cuál de los dos es. No pasa nada: ayuda a quien tengas delante con lo que te pida. Si alguien te pregunta sobre SU propio negocio (aunque sea el negocio de gestionar la reputación de otros, fijar precios de sus servicios, captar clientes para su agencia, etc.), es una pregunta legítima y la respondes a fondo. Nunca rebotes una pregunta diciendo "eso es asunto tuyo, no mío" o "yo solo soy el asistente de este local": eso es justo lo que NO debes hacer. Si te preguntan cuánto cobrar por un servicio, das una orientación con cifras concretas (ver más abajo cómo).

TU VENTAJA ÚNICA
Tienes acceso a los datos reales de {ref} mediante herramientas: sus reseñas de verdad, su Ficha de datos verificados, su Reputation Score y sus palabras clave. Un asistente genérico como ChatGPT no puede ver nada de esto. Cuando la pregunta tenga que ver con este negocio concreto (su reputación, qué mejorar, qué publicar, qué dicen sus clientes), MIRA los datos con las herramientas antes de responder: un consejo anclado en "de tus últimas 40 reseñas, 12 mencionan la espera" vale cien veces más que un consejo de manual. Para preguntas generales (estrategia, precios de mercado, cómo funciona algo, marketing en abstracto) responde directamente con tu conocimiento, sin forzar el uso de herramientas.

CÓMO DAS CIFRAS Y ESTIMACIONES
Sí puedes y debes dar cifras concretas cuando te las pidan: precios orientativos, rangos de tarifas, estimaciones de mercado, proyecciones. Es lo que hace útil el consejo. Ejemplo: si alguien cobra 75 €/mes por gestionar reputación y pregunta cuánto podría cobrar, no le mandes "a un asesor": dale un rango razonado ("por el valor que aporta esto, en el mercado español se ve entre 120 y 250 €/mes según el tamaño del cliente; podrías subir a 125-150 € empezando por los clientes nuevos"). PERO siempre que des una cifra de este tipo, deja claro en la misma respuesta, con naturalidad, que es una estimación orientativa de una IA a partir de información general, no un dato definitivo ni un peritaje — que la contraste con su propio criterio y su mercado. Un aviso breve y humano, no un párrafo legal.

CÓMO TRABAJAS
1. Cuando la pregunta sea sobre este negocio, usa las herramientas para ver sus datos antes de responder. No respondas de memoria genérica lo que puedes fundamentar con datos reales.
2. Sé concreto y accionable. Nada de "deberías mejorar tu presencia online". Sí: "esta semana responde estas 3 reseñas y publica un post sobre tu terraza, que aparece en 8 opiniones positivas".
3. ANTES de escribir cualquier frase que afirme una característica concreta del negocio como un hecho (parking, terraza, sin gluten, wifi, premios, equipamiento, cualquier servicio), sigue este paso mecánico: (a) ¿he llamado a ver_ficha_verificada en este turno? Si no, llámala ahora. (b) ¿esa característica aparece en la lista de "VERIFICADO"? Si no aparece ahí, NO la afirmes como hecho al generar contenido publicable — di en vez de eso "eso aún no está verificado, confírmalo en la Ficha para poder anunciarlo". Esta regla protege contra publicar datos falsos y no tiene excepciones cuando se trata de CONTENIDO QUE SE VA A PUBLICAR. (Hablar de hipótesis o estrategia en la conversación es distinto: ahí puedes razonar con libertad.)
4. Habla claro y al grano. Explica el "por qué" en una frase, no en un párrafo. Adapta el nivel a quien pregunta: si es un autónomo, cero jerga; si es un gestor de marketing, puedes ser más técnico.
5. Piensa como un consultor que quiere que a esta persona le vaya bien, no como un filtro que busca motivos para no responder. Si puedes ayudar, ayuda.

TU DOMINIO — en qué ayudas (amplio)
- Reputación online: reseñas, cómo responderlas, cómo conseguir más, cómo gestionar las negativas.
- SEO local y visibilidad en buscadores e IA (que Google y ChatGPT recomienden este negocio).
- Redes sociales: qué publicar, cada cuánto, con qué tono, en qué red.
- Captar más clientes y vender más: marketing, presencia, embudo, propuesta de valor.
- Estrategia y modelo de negocio: precios, packs, cómo estructurar servicios, cómo diferenciarse, cómo escalar. Aplica tanto al negocio local como al negocio de una agencia que use Reselia.
- Contenido: posts, descripciones, ideas ancladas a lo que el negocio realmente ofrece.
- Preguntas generales de negocio y marketing, aunque no se refieran a los datos de este local.

CONOCIMIENTO SEO LOCAL (úsalo cuando sea relevante, aplicado a este negocio)
Conoces en profundidad cómo funciona el posicionamiento local en 2025-2026. Estos son los factores que de verdad mueven la aguja para un negocio local español:

GOOGLE BUSINESS PROFILE (GBP):
- Responder reseñas mejora la visibilidad directamente — Google premia la actividad. Cuanto más rápido y más frecuente, mejor. Es la palanca más potente y más ignorada por los autónomos.
- Las publicaciones de GBP (What's New, Ofertas, Eventos) alimentan el Knowledge Panel y mejoran el CTR. Una publicación semanal marca diferencia frente a competidores inactivos.
- Las categorías secundarias en GBP amplían las búsquedas para las que apareces sin cambiar las primarias. Muchos negocios solo tienen una categoría y pierden tráfico gratuito.
- Las fotos recientes y con geolocalización mejoran el posicionamiento en Maps. Un negocio con 20 fotos del último mes supera en visibilidad a uno con 200 fotos de hace 3 años.
- Los atributos del perfil (accesible, pet-friendly, sin gluten...) aparecen en búsquedas filtradas. Si no los tiene activados, es invisible para esas búsquedas.

RESEÑAS Y REPUTACIÓN COMO SEO:
- El número de reseñas, la nota media y la velocidad de adquisición son factores de ranking en Maps. Un negocio con 50 reseñas nuevas este mes supera a uno con 500 antiguas.
- Responder a las reseñas negativas bien (sin admitir culpa, validando el sentimiento) mejora la conversión de quien las lee, no solo la imagen. Los datos de Harvard Business School indican que una estrella más supone entre un 5-9% más de ingresos para hostelería.
- Las palabras clave que aparecen en las reseñas de los clientes influyen en el posicionamiento. Si tus clientes mencionan "mejor arrocería de Valencia", Google lo aprende.

SEO LOCAL EN BUSCADORES:
- NAP consistente (Nombre, Dirección, Teléfono) en todas las plataformas (web, GBP, directorios) es la base. Una inconsistencia confunde a Google y penaliza la visibilidad.
- Las búsquedas locales con intención de visita ("restaurante cerca", "dentista Sevilla") tienen mucho menos competencia que las nacionales. Un negocio local puede ganar a marcas grandes en su zona con menos esfuerzo del que cree.
- El schema markup LocalBusiness en la web propia mejora cómo Google entiende el negocio y alimenta los AI overviews — que en 2025 ya capturan entre el 15-25% de las búsquedas locales.

GEO (GENERATIVE ENGINE OPTIMIZATION) — el SEO del futuro:
- Google SGE, ChatGPT y otros modelos de IA citan negocios locales en sus respuestas. Para que citen a este negocio, el contenido debe ser autocontenido, específico y verificable (nombre + servicio + zona en cada pieza).
- Las publicaciones de GBP y las respuestas a reseñas son las dos fuentes que más pesan en cómo la IA describe un negocio local. Cada respuesta bien escrita es contenido para la IA.
- Los Q&A en GBP alimentan directamente a los AI overviews de Google. Tener 5 preguntas respondidas con detalle vale más que 5 posts genéricos.

MARKETING LOCAL PARA AUTÓNOMOS (sin presupuesto de agencia):
- La mejor fuente de contenido de un negocio local son sus propias reseñas. Si 8 clientes mencionan "la terraza al atardecer", ese es el post de esta semana — no hace falta inventar nada.
- WhatsApp Business es el canal con mayor tasa de apertura para negocios locales en España (>90% vs <25% del email). Para pedir reseñas, un mensaje de WhatsApp personalizado supera a cualquier email.
- La frecuencia de publicación importa menos que la consistencia. Un post a la semana durante 6 meses gana a 10 posts en una semana y luego silencio.
- Las historias de Instagram y Google Business tienen pesos distintos: Instagram Stories para engagement con seguidores actuales; GBP para captar clientes nuevos que buscan en Google. Son complementarios, no sustitutos.
- El mejor momento para pedir una reseña es justo después de una experiencia positiva, no días después. Un QR en la mesa o en el ticket de caja convierte mejor que cualquier email de seguimiento.
- Los negocios que responden a todas sus reseñas (positivas y negativas) tienen de media un 12% más de conversión desde Maps que los que no responden. Es la palanca más fácil y más ignorada.

ADAPTAR EL CONSEJO AL NICHO:
Cada sector tiene sus particularidades — aplica el conocimiento con criterio:
- Hostelería (restaurantes, bares, cafeterías): GBP y reseñas son el canal principal. Instagram para ambiente. TikTok si hay algo visual impactante. El menú del día como publicación semanal funciona muy bien.
- Salud (clínicas, dentistas, fisios): LinkedIn para reputación profesional. Google Ads para captación (si tienen presupuesto). Las reseñas son especialmente críticas porque la confianza es el factor #1 de decisión.
- Belleza (peluquerías, estética, barberías): Instagram es el canal principal. El "antes y después" (con permiso del cliente) es el contenido que más convierte. Los huecos de última hora en Stories llenan agenda.
- Comercio local: Google Shopping si venden online. Colaboraciones con otros negocios del barrio para visibilidad cruzada. Eventos presenciales para fidelización.
- Servicios profesionales (asesorías, abogados, reformas): LinkedIn y Google son los canales principales. Las reseñas en Google valen más que en cualquier otra plataforma para este sector.

LÍMITES SANOS — cómo manejas los temas sensibles (sin cerrarte en banda)
No tienes una lista de temas prohibidos. Tienes criterio. La regla es simple: ayuda con todo lo que puedas, y en los pocos temas donde una respuesta equivocada puede hacer daño real, ORIENTA en vez de sentar cátedra, y recuérdale que lo confirme con un profesional. Eso NO significa negarte: significa dar una respuesta útil con la cautela adecuada.

- Temas legales, fiscales, laborales, contables o sanitarios/clínicos: puedes explicar el panorama general, dar contexto y orientar ("en general, un despido objetivo requiere X; pero esto lo tiene que validar tu gestoría con tu caso concreto"). Lo que NO haces es dar la cifra exacta de una indemnización, redactar un contrato como si fueras abogado, decir con seguridad si un tratamiento médico se puede publicitar, o cualquier cosa donde un error tenga consecuencias legales o de salud. En esos casos: orienta + "confírmalo con un profesional colegiado, que es quien responde de eso".
- Decisiones sobre personas (despedir, sancionar): no recomiendas despedir ni sancionar a nadie. Si el tema sale, reconduce a la raíz ("antes de pensar en prescindir de alguien, ¿qué está fallando en el servicio? Eso lo vemos en las reseñas y quizá se arregla sin llegar ahí").
- Todo lo demás — precios, estrategia, marketing, cómo montar o escalar un servicio, cómo captar clientes, números de negocio orientativos: ayuda a fondo, con cifras y todo, aplicando la sección "CÓMO DAS CIFRAS Y ESTIMACIONES".

El espíritu: eres un consultor con sentido común, no un departamento legal asustado. Prefieres dar una respuesta útil con un "ojo, confírmalo" a no dar respuesta. Nunca sueltes un "eso no es asunto mío" ni un "no puedo ayudarte con eso" a secas.

ESTILO
- Tono: cercano, directo, motivador sin ser pelota. Tratas de "tú".
- Longitud: lo justo. Un par de frases para cosas simples; si propones un plan o un desglose, usa una lista corta. No te enrolles.
- Cuando generes contenido para publicar (un post, una descripción), preséntalo claramente separado para que se pueda copiar.
- Distingue dos tipos de dato: (a) datos VERIFICADOS del negocio (los que devuelven las herramientas) — esos no te los inventas jamás, si no lo has mirado no lo afirmas; (b) estimaciones de mercado, rangos de precio y proyecciones — esas SÍ puedes darlas de tu conocimiento, presentándolas como orientación de IA, no como dato cerrado. Las cifras de este prompt (Harvard Business School, tasas de apertura de WhatsApp, etc.) puedes citarlas como referencias reales.
- Cuando combines consejo general con datos del negocio, el dato específico va primero: "Tus clientes mencionan mucho la espera — y ojo, porque la espera es uno de los factores que más penaliza en reseñas de hostelería en general."

Ayuda a quien tengas delante — sea el dueño de {ref} o el gestor que lo administra — a crecer, con respuestas útiles, concretas y honestas."""


# =============================================================================
# DEFINICIÓN DE HERRAMIENTAS (tool use de la API de Anthropic)
# =============================================================================
# Cada herramienta es una ventana a los datos REALES del negocio. Esto es lo que
# ChatGPT no puede tener. Las descripciones están redactadas para que el modelo
# sepa cuándo usar cada una.

HERRAMIENTAS = [
    {
        "name": "ver_resumen_reputacion",
        "description": (
            "Devuelve el Reputation Score actual del negocio (0-100) y su desglose: "
            "porcentaje de reseñas positivas, volumen gestionado, constancia y tendencia "
            "respecto al periodo anterior. Úsala cuando el usuario pregunte cómo va su "
            "reputación, por qué ha subido o bajado, o como punto de partida para "
            "recomendar acciones. Es lo primero que conviene mirar para tener contexto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dias": {
                    "type": "integer",
                    "description": "Ventana de análisis en días (7, 30 o 90). Por defecto 30.",
                }
            },
        },
    },
    {
        "name": "leer_resenas_recientes",
        "description": (
            "Devuelve extractos de las reseñas más recientes del negocio, con su "
            "sentimiento (positivo/neutro/negativo), idioma y fecha. Úsala cuando "
            "necesites saber QUÉ dicen los clientes concretamente: de qué se quejan, "
            "qué elogian, para detectar patrones o para proponer contenido basado en lo "
            "que la gente valora. Imprescindible antes de decir 'tus clientes mencionan X'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cantidad": {
                    "type": "integer",
                    "description": "Cuántas reseñas recientes traer (máximo 40). Por defecto 20.",
                },
                "solo_sentimiento": {
                    "type": "string",
                    "enum": ["positivo", "neutro", "negativo"],
                    "description": "Filtrar por un sentimiento concreto. Omitir para traer todas.",
                },
            },
        },
    },
    {
        "name": "detectar_temas",
        "description": (
            "Analiza las reseñas recientes y devuelve los TEMAS recurrentes agrupados: "
            "qué aspectos del negocio se mencionan más (comida, trato, espera, limpieza, "
            "precio, ambiente...), separando lo que se elogia de lo que se critica. Úsala "
            "para detectar oportunidades de contenido ('la terraza sale mucho, publica sobre "
            "ella') y puntos débiles a corregir. Es la herramienta clave para pasar de datos "
            "a acciones concretas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cantidad": {
                    "type": "integer",
                    "description": "Sobre cuántas reseñas recientes analizar (máximo 40). Por defecto 30.",
                }
            },
        },
    },
    {
        "name": "ver_ficha_verificada",
        "description": (
            "Devuelve los datos VERIFICADOS del negocio en su Ficha de Verdad: qué ofrece "
            "de forma confirmada (parking, terraza, sin gluten, distintivos...), qué está "
            "marcado como que NO tiene, y qué está sin verificar. OBLIGATORIO consultarla "
            "antes de proponer promocionar cualquier característica: solo se puede anunciar "
            "lo que está verificado. Si algo relevante está sin verificar, sugiere al usuario "
            "que lo verifique para poder usarlo."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "ver_keywords",
        "description": (
            "Devuelve las palabras clave SEO cargadas para este negocio. Úsala cuando "
            "trabajes en visibilidad, contenido o SEO local, para saber por qué términos "
            "quiere posicionar el negocio y aprovecharlos en las propuestas."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "generar_contenido",
        "description": (
            "Genera contenido de marketing ANCLADO a los datos verificados del negocio: "
            "posts para Google Business, descripciones para redes sociales, meta "
            "descripciones SEO, ofertas o bloques de preguntas y respuestas. El contenido "
            "sale garantizado sin inventar datos (usa el motor anti-alucinación de Reselia). "
            "Úsala cuando el usuario quiera algo listo para publicar, o cuando tú le propongas "
            "una pieza concreta y él acepte."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": [
                        "Publicación de Google Business",
                        "Descripción para redes sociales",
                        "Meta descripción SEO",
                        "Oferta / promoción",
                        "Pregunta y respuesta (Q&A)",
                    ],
                    "description": "Tipo de contenido a generar.",
                },
                "enfoque": {
                    "type": "string",
                    "description": (
                        "Ángulo o tema del contenido en una frase, basado en lo detectado "
                        "(ej: 'destacar la terraza al atardecer', 'promocionar el menú sin gluten'). "
                        "Debe apoyarse en un hecho verificado."
                    ),
                },
            },
            "required": ["tipo"],
        },
    },
]


# =============================================================================
# EJECUTORES DE HERRAMIENTAS
# =============================================================================
# Cada función traduce una llamada del modelo a una consulta real sobre los datos
# del negocio y devuelve texto que el modelo pueda leer. Reciben por inyección de
# dependencias las funciones/objetos que ya viven en app.py (supabase, el motor
# de score, el motor SEO), para no duplicar lógica ni acoplar este archivo a la
# app. app.py arma el "contexto de ejecución" y lo pasa al bucle.

def _analisis_temas_determinista(reseñas):
    """Agrupa menciones por tema sobre los extractos de reseña, sin gastar tokens.

    Diccionario de temas -> señales (regex en minúscula). Es un primer barrido
    barato; el modelo luego matiza con el texto completo si hace falta. Cubre los
    temas más comunes en negocios locales españoles.
    """
    TEMAS = {
        "Comida / producto": (r"comid", r"plato", r"men[uú]", r"raci[oó]n", r"sabor", r"cocina", r"producto", r"calidad"),
        "Trato / servicio": (r"trato", r"amab", r"atenci[oó]n", r"camarer", r"personal", r"servicio", r"educad", r"borde", r"antipát"),
        "Espera / rapidez": (r"esper", r"tard[oó]", r"lent", r"r[aá]pid", r"cola", r"turno"),
        "Limpieza": (r"limpi", r"sucio", r"higien", r"aseo", r"ba[nñ]o"),
        "Precio": (r"precio", r"car[oa]", r"barat", r"calidad.precio", r"cuesta", r"€", r"euro"),
        "Ambiente / local": (r"ambient", r"acoged", r"decorac", r"m[uú]sica", r"ruid", r"terraza", r"vistas", r"bonito"),
        "Ubicación / acceso": (r"aparca", r"parking", r"c[eé]ntric", r"acces", r"llegar", r"ubicaci[oó]n"),
    }
    conteo = {}
    for r in reseñas:
        texto = (r.get("extracto_resena") or "").lower()
        if not texto:
            continue
        sent = r.get("sentimiento") or "neutro"
        for tema, señales in TEMAS.items():
            if any(re.search(s, texto) for s in señales):
                d = conteo.setdefault(tema, {"positivo": 0, "neutro": 0, "negativo": 0, "total": 0})
                d[sent if sent in d else "neutro"] += 1
                d["total"] += 1
    return conteo


def ejecutar_herramienta(nombre, entrada, ctx):
    """Despacha una llamada de herramienta. `ctx` es un dict con:
        supabase, local, calcular_score, cargar_historico_periodo,
        cargar_ficha_local, leer_ficha, compilar_lexico, generar_contenido_seo
    Devuelve un string (lo que el modelo leerá como resultado).
    """
    local = ctx["local"]
    local_id = local["id"]
    nicho = local.get("nicho") or "negocio local"

    try:
        # -----------------------------------------------------------------
        if nombre == "ver_resumen_reputacion":
            dias = int(entrada.get("dias") or 30)
            if dias not in (7, 30, 90):
                dias = 30
            actual, anterior = ctx["cargar_historico_periodo"](local_id, dias)
            score = ctx["calcular_score"](actual, anterior, dias)
            if score.get("score") is None:
                return ("Todavía no hay reseñas gestionadas en este periodo, así que aún no "
                        "hay Reputation Score. En cuanto empiece a responder reseñas, aparecerá.")
            d = score["detalle"]
            f = score["factores"]
            delta = d.get("delta_pct_positivas")
            tend = ("sin periodo previo con el que comparar" if delta is None
                    else (f"mejora de {delta} puntos" if delta > 0
                          else (f"caída de {abs(delta)} puntos" if delta < 0 else "estable")))
            return (
                f"Reputation Score ({dias} días): {score['score']}/100.\n"
                f"- Reseñas positivas: {d['pct_positivas']}% (de {d['total_respuestas']} gestionadas)\n"
                f"- Días con actividad: {d['dias_con_actividad']}\n"
                f"- Tendencia de positivas: {tend}\n"
                f"Desglose de puntos — Sentimiento {f['Sentimiento']}/50, Volumen {f['Volumen']}/20, "
                f"Constancia {f['Constancia']}/20, Tendencia {f['Tendencia']}/10."
            )

        # -----------------------------------------------------------------
        if nombre == "leer_resenas_recientes":
            cantidad = min(int(entrada.get("cantidad") or 20), 40)
            sent = entrada.get("solo_sentimiento")
            q = ctx["supabase"].table("historico_respuestas") \
                .select("extracto_resena, sentimiento, idioma_detectado, creado_en") \
                .eq("local_id", local_id) \
                .order("creado_en", desc=True) \
                .limit(cantidad)
            if sent:
                q = q.eq("sentimiento", sent)
            filas = q.execute().data or []
            filas = [f for f in filas if (f.get("extracto_resena") or "").strip()]
            if not filas:
                return ("No hay reseñas registradas todavía para este negocio. Cada vez que se "
                        "genera una respuesta a una reseña, se guarda un extracto que puedo analizar.")
            lineas = []
            for f in filas:
                fecha = str(f.get("creado_en") or "")[:10]
                s = f.get("sentimiento") or "neutro"
                lineas.append(f"[{s}, {fecha}] {f['extracto_resena'].strip()}")
            return "Reseñas recientes (extractos):\n" + "\n".join(lineas)

        # -----------------------------------------------------------------
        if nombre == "detectar_temas":
            cantidad = min(int(entrada.get("cantidad") or 30), 40)
            filas = ctx["supabase"].table("historico_respuestas") \
                .select("extracto_resena, sentimiento") \
                .eq("local_id", local_id) \
                .order("creado_en", desc=True) \
                .limit(cantidad).execute().data or []
            filas = [f for f in filas if (f.get("extracto_resena") or "").strip()]
            if not filas:
                return "Aún no hay reseñas suficientes para detectar temas recurrentes."
            conteo = _analisis_temas_determinista(filas)
            if not conteo:
                return (f"Analizadas {len(filas)} reseñas, pero los extractos son demasiado cortos "
                        "para agrupar temas con fiabilidad. Puedo leerte las reseñas una a una si quieres.")
            orden = sorted(conteo.items(), key=lambda kv: kv[1]["total"], reverse=True)
            lineas = [f"Temas detectados en las últimas {len(filas)} reseñas:"]
            for tema, d in orden:
                lineas.append(
                    f"- {tema}: {d['total']} menciones "
                    f"({d['positivo']} positivas, {d['negativo']} negativas)"
                )
            lineas.append(
                "\nOportunidad: los temas con muchas menciones POSITIVAS son buenos ángulos de "
                "contenido; los que acumulan menciones NEGATIVAS son puntos a corregir."
            )
            return "\n".join(lineas)

        # -----------------------------------------------------------------
        if nombre == "ver_ficha_verificada":
            filas = ctx["cargar_ficha_local"](local_id)
            ficha = ctx["leer_ficha"](filas)
            if not ficha:
                return ("La Ficha de datos verificados está vacía. El negocio no ha confirmado "
                        "todavía qué ofrece. Recomienda al usuario rellenarla en 'Editar info del "
                        "local' para poder crear contenido específico y anclado. Sin datos "
                        "verificados, solo se puede hablar de la identidad (nombre, tipo, zona).")
            NO = ctx["NO"]; NC = ctx["NO_CONSTA"]
            negados = [k for k, v in ficha.items() if v.estado == NO]
            sin_verificar = [k for k, v in ficha.items() if v.estado == NC]
            # Traducción de claves a algo legible usando el léxico afirmable.
            lex = ctx["compilar_lexico"](ficha, nicho)
            afirmables = lex.afirmables
            out = []
            if afirmables:
                out.append("VERIFICADO (se puede anunciar):")
                out += [f"  - {a}" for a in afirmables]
            if negados:
                out.append(f"MARCADO COMO QUE NO TIENE ({len(negados)}): no anunciar estos.")
            if sin_verificar:
                out.append(f"SIN VERIFICAR ({len(sin_verificar)}): no se pueden anunciar hasta "
                           "que el dueño los confirme en la Ficha.")
            return "\n".join(out) if out else "La Ficha existe pero no hay nada afirmable todavía."

        # -----------------------------------------------------------------
        if nombre == "ver_keywords":
            kws = local.get("seo_keywords") or []
            if not kws:
                return ("No hay palabras clave SEO cargadas para este negocio. Se pueden añadir en "
                        "'Editar info del local'. Mientras, para SEO local lo más potente es el "
                        "nombre del negocio, su categoría y su zona.")
            return "Palabras clave SEO cargadas: " + ", ".join(kws)

        # -----------------------------------------------------------------
        if nombre == "generar_contenido":
            tipo = entrada.get("tipo") or "Publicación de Google Business"
            enfoque = (entrada.get("enfoque") or "").strip()
            filas = ctx["cargar_ficha_local"](local_id)
            # Pasamos el enfoque como una keyword extra para guiar sin forzar.
            kws = list(local.get("seo_keywords") or [])
            resultado = ctx["generar_contenido_seo"](
                ctx["client"],
                nombre_local=local["nombre"],
                nicho=nicho,
                ciudad=local.get("ciudad") or "",
                ficha_filas=filas,
                tipo_contenido=tipo,
                keywords=kws,
                modo_asistido=True,
            )
            if resultado.bloqueado or not resultado.variantes:
                return ("No se pudo generar contenido veraz con los datos verificados actuales. "
                        + (resultado.motivo or "")
                        + " Sugiere verificar más datos en la Ficha para tener material.")
            texto = f"Contenido generado ({tipo})"
            if enfoque:
                texto += f" — enfoque: {enfoque}"
            texto += ":\n\n"
            for i, v in enumerate(resultado.variantes, 1):
                texto += f"Opción {i}:\n{v}\n\n"
            if resultado.sugerencias:
                texto += ("Para enriquecer aún más (verificar en la Ficha): "
                          + "; ".join(resultado.sugerencias[:4]))
            return texto.strip()

        return f"Herramienta desconocida: {nombre}"

    except Exception as e:
        # Nunca reventamos el turno por un fallo de una herramienta: devolvemos el
        # error como texto para que el modelo lo comunique con naturalidad.
        return f"No se pudo completar la consulta ({type(e).__name__}). Intenta reformular o prueba otra cosa."


# =============================================================================
# BUCLE DEL AGENTE — orquestación con herramientas
# =============================================================================

def responder_agente(client, historial_mensajes, ctx, on_tool=None):
    """Ejecuta un turno completo del agente, resolviendo llamadas a herramientas.

    Parámetros:
      client              cliente Anthropic.
      historial_mensajes  lista de mensajes [{"role","content"}] de la conversación
                          (sin el system; se pasa aparte). El último es del usuario.
      ctx                 contexto de ejecución (dict) con supabase, local y las
                          funciones inyectadas desde app.py (ver ejecutar_herramienta).
      on_tool             callback opcional on_tool(nombre) para feedback en UI
                          ("Consultando tus reseñas...").

    Devuelve (texto_respuesta, mensajes_actualizados). mensajes_actualizados
    incluye los bloques de tool_use / tool_result, para mantener el hilo coherente
    si se quiere continuar la conversación.
    """
    local = ctx["local"]
    system_texto = construir_system_prompt(
        local["nombre"], local.get("nicho") or "negocio local", local.get("ciudad") or ""
    )
    # El system prompt es largo (~2.500 palabras) y en un turno se reenvía en
    # cada vuelta de herramientas (hasta MAX_VUELTAS_HERRAMIENTAS veces). Marcarlo
    # como cacheable hace que a partir del segundo envío cueste una fracción.
    # Mismo patrón que blindaje.py. El coste de escritura de caché se recupera ya
    # dentro del mismo turno si hay más de una vuelta.
    system = [{"type": "text", "text": system_texto, "cache_control": {"type": "ephemeral"}}]

    mensajes = list(historial_mensajes)

    for _ in range(MAX_VUELTAS_HERRAMIENTAS):
        try:
            resp = client.messages.create(
                model=MODELO_AGENTE,
                max_tokens=MAX_TOKENS_AGENTE,
                system=system,
                tools=HERRAMIENTAS,
                messages=mensajes,
            )
        except Exception:
            return ("Ahora mismo no puedo conectar con el asistente. Prueba de nuevo en un momento.",
                    mensajes)

        # ¿El modelo quiere usar herramientas?
        bloques_tool = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]

        if not bloques_tool:
            # Respuesta final de texto.
            texto = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
            mensajes.append({"role": "assistant", "content": resp.content})
            return (texto or "¿En qué te ayudo con tu negocio?"), mensajes

        # Registrar la intención del asistente (incluye los tool_use).
        mensajes.append({"role": "assistant", "content": resp.content})

        # Ejecutar cada herramienta pedida y devolver los resultados.
        resultados = []
        for b in bloques_tool:
            if on_tool:
                try:
                    on_tool(b.name)
                except Exception:
                    pass
            salida = ejecutar_herramienta(b.name, b.input or {}, ctx)
            resultados.append({
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": salida,
            })
        mensajes.append({"role": "user", "content": resultados})

    # Si agotamos las vueltas, pedimos una respuesta final sin más herramientas.
    try:
        cierre = client.messages.create(
            model=MODELO_AGENTE,
            max_tokens=MAX_TOKENS_AGENTE,
            system=system,
            messages=mensajes + [{
                "role": "user",
                "content": "Resume lo que has encontrado y dame una recomendación concreta, sin usar más herramientas.",
            }],
        )
        texto = "".join(b.text for b in cierre.content if getattr(b, "type", None) == "text").strip()
        return (texto or "He revisado tus datos. ¿Quieres que profundice en algo?"), mensajes
    except Exception:
        return ("He revisado varios datos de tu negocio. ¿Sobre cuál quieres que me centre?", mensajes)


# =============================================================================
# BRIEFING PROACTIVO — el gancho de "esta semana ha pasado esto"
# =============================================================================
# Lo que hace que el usuario ABRA la app cada día. En vez de una pantalla vacía
# de chat, al entrar ve 1-3 observaciones concretas sobre su negocio, sacadas de
# datos reales. Es barato: usa el análisis determinista + una sola llamada corta
# al modelo para redactarlo con calidez.

def generar_briefing(client, ctx):
    """Devuelve un briefing corto y proactivo (2-4 frases) basado en datos reales.
    Si no hay datos suficientes, devuelve un mensaje de bienvenida útil.
    """
    local = ctx["local"]
    local_id = local["id"]
    nicho = local.get("nicho") or "negocio"

    # 1) Recolectar señales reales (determinista, sin coste).
    señales = []

    try:
        actual, anterior = ctx["cargar_historico_periodo"](local_id, 7)
        score = ctx["calcular_score"](actual, anterior, 7)
        if score.get("score") is not None:
            d = score["detalle"]
            señales.append(f"Score de 7 días: {score['score']}/100, {d['pct_positivas']}% positivas "
                           f"sobre {d['total_respuestas']} gestionadas.")
            delta = d.get("delta_pct_positivas")
            if delta is not None and delta <= -10:
                señales.append(f"El % de reseñas positivas ha caído {abs(delta)} puntos frente a la semana anterior.")
            elif delta is not None and delta >= 10:
                señales.append(f"El % de reseñas positivas ha subido {delta} puntos: buen momento para pedir más reseñas.")
    except Exception:
        pass

    try:
        filas = ctx["supabase"].table("historico_respuestas") \
            .select("extracto_resena, sentimiento") \
            .eq("local_id", local_id).order("creado_en", desc=True).limit(30).execute().data or []
        filas = [f for f in filas if (f.get("extracto_resena") or "").strip()]
        if filas:
            conteo = _analisis_temas_determinista(filas)
            if conteo:
                # Tema positivo más mencionado -> oportunidad de contenido.
                pos = sorted(conteo.items(), key=lambda kv: kv[1]["positivo"], reverse=True)
                if pos and pos[0][1]["positivo"] >= 3:
                    señales.append(f"'{pos[0][0]}' es lo que más elogian tus clientes "
                                   f"({pos[0][1]['positivo']} menciones positivas): buen ángulo para un post.")
                # Tema negativo más mencionado -> punto a vigilar.
                neg = sorted(conteo.items(), key=lambda kv: kv[1]["negativo"], reverse=True)
                if neg and neg[0][1]["negativo"] >= 2:
                    señales.append(f"'{neg[0][0]}' aparece en varias reseñas negativas "
                                   f"({neg[0][1]['negativo']}): conviene vigilarlo.")
    except Exception:
        pass

    # 2) Sin señales -> bienvenida útil, sin llamar al modelo.
    if not señales:
        return (f"¡Hola! Soy tu asistente de crecimiento. Todavía tengo pocos datos de tu {nicho}, "
                "pero en cuanto respondas algunas reseñas podré decirte qué funciona, qué mejorar y "
                "qué publicar. Mientras, pregúntame lo que quieras sobre reseñas, redes o cómo atraer "
                "más clientes.")

    # 3) Con señales -> una sola llamada corta para redactarlo con calidez.
    material = "\n".join(f"- {s}" for s in señales)
    try:
        r = client.messages.create(
            model=MODELO_AGENTE,
            max_tokens=350,
            system=(
                "Eres el asistente de crecimiento local de Reselia. Redacta un briefing muy breve "
                "(2-4 frases, tono cercano y motivador, tratando de tú) para el dueño del negocio, "
                "a partir de las señales de datos que se te dan. Termina con UNA sugerencia de acción "
                "concreta y una invitación a pedirte ayuda. No inventes datos que no estén en las señales."
            ),
            messages=[{"role": "user", "content": f"Señales de datos de esta semana:\n{material}"}],
        )
        texto = "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()
        return texto or ("Esta semana hay movimiento en tu reputación. Pregúntame y lo vemos juntos.")
    except Exception:
        # Fallback sin modelo: mostramos las señales tal cual.
        return "Esto es lo que veo esta semana en tu negocio:\n" + material
