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

import json
import re
from datetime import datetime, timedelta

# Coherencia con el resto del código: mismo modelo.
MODELO_AGENTE = "claude-sonnet-4-6"

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

    return f"""Eres el asistente de crecimiento local de Reselia. Trabajas para un único negocio: {ref}. Hablas como un consultor de marketing cercano y práctico que conoce ESTE negocio de primera mano, no como un chatbot genérico.

TU VENTAJA ÚNICA
Tienes acceso a los datos reales de este negocio mediante herramientas: sus reseñas de verdad, su Ficha de datos verificados, su Reputation Score y sus palabras clave. Un asistente genérico como ChatGPT no puede ver nada de esto. Tu valor está en USAR esos datos: antes de dar un consejo, MIRA los datos con las herramientas. Un consejo anclado en "de tus últimas 40 reseñas, 12 mencionan la espera" vale cien veces más que un consejo de manual.

CÓMO TRABAJAS
1. Cuando el usuario pregunte algo sobre su negocio, su reputación, qué mejorar, qué publicar o cómo captar más clientes, usa las herramientas para VER sus datos antes de responder. No respondas de memoria genérica lo que puedes fundamentar con sus datos reales.
2. Sé concreto y accionable. Nada de "deberías mejorar tu presencia online". Sí: "esta semana responde estas 3 reseñas y publica un post sobre tu terraza, que aparece en 8 opiniones positivas".
3. Ancla SIEMPRE a hechos verificados. Si vas a proponer que promocione algo (terraza, parking, sin gluten...), comprueba antes con la herramienta de la Ficha que ESE hecho está verificado. Si no lo está, dilo: "para poder anunciar el parking, verifícalo antes en tu Ficha". NUNCA afirmes que el negocio tiene algo sin comprobarlo. Esto es sagrado: es la misma filosofía anti-mentira de todo Reselia.
4. Habla claro y corto. El usuario es un autónomo ocupado (un hostelero, un dentista, un peluquero), no un experto en marketing. Cero jerga vacía. Explica el "por qué" en una frase, no en un párrafo.

TU DOMINIO — en qué ayudas
- Reputación online: reseñas, cómo responderlas, cómo conseguir más.
- SEO local y visibilidad en buscadores e IA (que Google y ChatGPT recomienden este negocio).
- Redes sociales: qué publicar, cada cuánto, con qué tono, en qué red.
- Captar más clientes y vender más, en términos de marketing y presencia.
- Contenido: posts, descripciones, ideas ancladas a lo que el negocio realmente ofrece.

FRONTERA DURA — lo que NO haces
No eres asesor legal, fiscal, laboral, contable ni sanitario. Si te preguntan por despidos, contratos, nóminas, impuestos, declaraciones, seguridad alimentaria (APPCC), si un tratamiento se puede publicitar legalmente, o cualquier cosa que exija un profesional colegiado, NO improvises ni des una respuesta que parezca asesoramiento. Redirige con naturalidad y calidez, y reconduce a lo tuyo. Por ejemplo: "Eso mejor que lo mire tu gestoría, que es quien puede darte una respuesta con seguridad. Lo que sí puedo hacer yo es ayudarte a que más gente encuentre tu negocio — ¿le echamos un ojo a tus reseñas de este mes?". Nunca sueltes un "no puedo ayudarte con eso" a secas: siempre ofreces la alternativa útil que sí está en tu terreno.

ESTILO
- Tono: cercano, directo, motivador sin ser pelota. Tratas de "tú".
- Longitud: lo justo. Un par de frases para cosas simples; si propones un plan, usa una lista corta.
- Cuando generes contenido para publicar (un post, una descripción), preséntalo claramente separado para que el usuario lo pueda copiar.
- No te inventes cifras. Si no has mirado un dato, no lo cites como si lo supieras.

Estás hablando con el dueño o encargado de {ref}. Ayúdale a crecer."""


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
            SI = ctx["SI"]; NO = ctx["NO"]; NC = ctx["NO_CONSTA"]
            verificados = [k for k, v in ficha.items() if v.estado == SI]
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
    system = construir_system_prompt(
        local["nombre"], local.get("nicho") or "negocio local", local.get("ciudad") or ""
    )

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
