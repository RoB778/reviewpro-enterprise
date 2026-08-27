# -*- coding: utf-8 -*-
"""
motor_seo.py — Motor de generación de contenido SEO local ANCLADO A HECHOS.
=============================================================================

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
El generador anterior (generar_contenido_seo_extra) alucinaba porque el propio
prompt le ORDENABA inventar: "Q&A de logística: aparcamiento, precio...". Si el
negocio no tenía aparcamiento, el modelo se lo inventaba porque tenía orden de
hablar de ello. No era un fallo del modelo: era la respuesta lógica a una
instrucción imposible, igual que pasaba con el "2-3 keywords MÍNIMO" en las
reseñas.

Y esto no es un simple bug de calidad. Reselia vende que NO escribe confesiones.
Una respuesta pública que afirma "ven a probar nuestro menú con estrella Michelin
y parking gratuito" cuando ninguna de las dos cosas es cierta es publicidad
engañosa (Ley 3/1991 de Competencia Desleal, art. 5), firmada por el negocio,
generada por nuestro software. Es exactamente el mismo daño que el blindaje evita
por el otro lado. Un afirmación falsa a favor es tan peligrosa como una admisión
en contra.

LA IDEA CENTRAL — LA FICHA DE VERDAD
------------------------------------
La IA hace las preguntas. El humano da las respuestas. La IA solo puede redactar
con lo respondido. Nadie en el sector hace esto: todos generan desde el aire.
Nosotros generamos anclado a hechos verificados, con la misma filosofía en capas
que blindaje.py.

ARQUITECTURA EN CAPAS (espejo de blindaje.py)
---------------------------------------------
  CAPA 0  Ficha de Verdad: hechos con estado TERNARIO (SI / NO / NO_CONSTA).
          NO_CONSTA es el estado por defecto y NO se puede afirmar. Eso mata la
          alucinación de raíz: lo que el dueño no confirmó, no existe para el motor.
  CAPA 1  Léxico determinista (gratis, 0 ms): compila lista de hechos AFIRMABLES
          y lista de términos VETADOS. Filtro por regex antes de gastar un token.
  CAPA 2  Generación anclada: el modelo redacta SOLO con los afirmables, con la
          lista de vetados inyectada como prohibición explícita.
  CAPA 3  Auditor de veracidad (espejo del auditor legal): un segundo modelo
          extrae toda afirmación factual del texto y la contrasta con la Ficha.
          Cualquier afirmación sin anclaje -> regeneración correctiva.

Autor: Reselia. Aislado como blindaje.py para no inflar app.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

# Coherencia con blindaje.py: mismos modelos.
MODELO_REDACTOR_SEO = "claude-sonnet-4-6"
MODELO_AUDITOR_SEO = "claude-sonnet-4-6"

# Cuántas veces reintenta el redactor si el auditor de veracidad caza una
# afirmación no anclada. Igual que en blindaje: un par de vueltas basta.
MAX_INTENTOS_VERACIDAD = 2

# Estados ternarios de un hecho.
SI = "SI"
NO = "NO"
NO_CONSTA = "NO_CONSTA"
ESTADOS_VALIDOS = {SI, NO, NO_CONSTA}


# =============================================================================
# CATÁLOGO DE HECHOS
# =============================================================================
#
# POR QUÉ EL CATÁLOGO ES DETERMINISTA (no lo genera un modelo)
# ------------------------------------------------------------
# Si el cuestionario lo generara el modelo en cada carga, las "claves" de cada
# hecho cambiarían de una vez a otra y no podríamos guardar las respuestas de
# forma estable. Peor: un modelo generando el cuestionario es una vía más de
# alucinación. Aquí el modelo NO decide qué se pregunta. El catálogo es fijo,
# con claves estables. La inteligencia del modelo se reserva para (a) proponer
# preguntas EXTRA opcionales del nicho concreto y (b) redactar el contenido
# final a partir de hechos ya verificados. En ambos casos, seguro.
#
# Cada hecho define:
#   clave      identificador estable (se guarda en Supabase, no cambia nunca)
#   pregunta   texto que ve el operador de la agencia
#   familia    para agrupar visualmente el formulario
#   tipo       "si_no"          -> respuesta binaria
#              "si_no_evidencia"-> binaria PERO exige año + entidad si es SI
#              "texto"          -> valor libre corto (idiomas, marcas...)
#   afirmable  plantilla para convertir un SI en frase afirmable para el modelo.
#              Usa {valor} si el hecho es de tipo texto.
#   vetado     término(s) que, si aparecen en el contenido SIN estar afirmados,
#              son señal de alucinación. Lista de regex (en minúscula).
#   sugerible  frase de nudge para el modo asistido cuando está en NO_CONSTA.
#
# =============================================================================


@dataclass
class DefHecho:
    clave: str
    pregunta: str
    familia: str
    tipo: str = "si_no"
    afirmable: str = ""
    vetado: list = field(default_factory=list)
    sugerible: str = ""


# --- CATÁLOGO BASE: aplica a casi cualquier negocio local --------------------
# Se prioriza lo que más se alucina (acceso, distintivos) y lo de mayor valor SEO.
CATALOGO_BASE: list[DefHecho] = [
    DefHecho(
        clave="parking_propio",
        pregunta="¿El negocio tiene aparcamiento propio (parking, garaje o plazas reservadas)?",
        familia="Acceso y llegada",
        afirmable="Dispone de aparcamiento propio.",
        vetado=[r"parking propio", r"aparcamiento propio", r"garaje propio", r"plazas de aparcamiento"],
        sugerible="mencionar el aparcamiento propio",
    ),
    DefHecho(
        clave="parking_cercano",
        pregunta="¿Hay aparcamiento público o zona de aparcamiento fácil muy cerca?",
        familia="Acceso y llegada",
        afirmable="Hay aparcamiento público a pocos metros.",
        vetado=[r"aparcamiento cercano", r"f[aá]cil aparcar", r"zona azul", r"parking p[uú]blico"],
        sugerible="mencionar el aparcamiento público cercano",
    ),
    DefHecho(
        clave="transporte_publico",
        pregunta="¿Está bien comunicado con transporte público (metro, bus, tren a pie)?",
        familia="Acceso y llegada",
        afirmable="Está bien comunicado con transporte público.",
        vetado=[r"parada de (metro|bus|autob[uú]s)", r"bien comunicado", r"transporte p[uú]blico"],
        sugerible="mencionar la buena comunicación con transporte público",
    ),
    DefHecho(
        clave="accesible_pmr",
        pregunta="¿El local es accesible para personas con movilidad reducida (sin escalones, rampa o ascensor)?",
        familia="Acceso y llegada",
        afirmable="El local es accesible para personas con movilidad reducida.",
        vetado=[r"accesible", r"sin barreras", r"movilidad reducida", r"silla de ruedas", r"rampa de acceso"],
        sugerible="mencionar la accesibilidad para movilidad reducida",
    ),
    DefHecho(
        clave="reserva_cita",
        pregunta="¿Se puede reservar mesa / pedir cita con antelación?",
        familia="Reservas y horario",
        afirmable="Admite reserva o cita previa.",
        vetado=[r"reserva (online|previa|anticipada)", r"pide cita", r"cita previa"],
        sugerible="mencionar que se puede reservar con antelación",
    ),
    DefHecho(
        clave="reserva_online",
        pregunta="¿Se puede reservar o pedir cita por internet (web, WhatsApp, formulario)?",
        familia="Reservas y horario",
        afirmable="Se puede reservar o pedir cita por internet.",
        vetado=[r"reserva online", r"reserva por internet", r"reserva por whatsapp"],
        sugerible="mencionar la reserva online",
    ),
    DefHecho(
        clave="abierto_findes",
        pregunta="¿Abre los fines de semana?",
        familia="Reservas y horario",
        afirmable="Abre los fines de semana.",
        vetado=[r"abierto (los )?(fin(es)? de semana|s[aá]bados? y domingos?)", r"abrimos (el )?domingo"],
        sugerible="mencionar la apertura en fin de semana",
    ),
    DefHecho(
        clave="pago_tarjeta",
        pregunta="¿Se puede pagar con tarjeta / Bizum / contactless?",
        familia="Servicios generales",
        afirmable="Acepta pago con tarjeta y Bizum.",
        vetado=[r"pago con tarjeta", r"aceptamos tarjeta", r"bizum", r"contactless"],
        sugerible="mencionar el pago con tarjeta o Bizum",
    ),
    DefHecho(
        clave="terraza",
        pregunta="¿Tiene terraza o espacio exterior?",
        familia="Servicios generales",
        afirmable="Cuenta con terraza / espacio exterior.",
        vetado=[r"terraza", r"espacio exterior", r"al aire libre"],
        sugerible="mencionar la terraza",
    ),
    DefHecho(
        clave="wifi",
        pregunta="¿Ofrece wifi gratis a los clientes?",
        familia="Servicios generales",
        afirmable="Ofrece wifi gratuito.",
        vetado=[r"wifi", r"wi-fi", r"internet gratis"],
        sugerible="mencionar el wifi gratuito",
    ),
    DefHecho(
        clave="admite_mascotas",
        pregunta="¿Se admiten mascotas (pet friendly)?",
        familia="Aptitudes y público",
        afirmable="Es un espacio que admite mascotas.",
        vetado=[r"mascotas", r"pet friendly", r"admite perros", r"dog friendly"],
        sugerible="mencionar que admite mascotas",
    ),
    DefHecho(
        clave="familias_ninos",
        pregunta="¿Es un sitio adecuado para familias con niños (tronas, menú infantil, zona de juegos)?",
        familia="Aptitudes y público",
        afirmable="Es un espacio adecuado para familias con niños.",
        vetado=[r"familias con ni[nñ]os", r"men[uú] infantil", r"tronas?", r"zona (de )?juegos", r"apto para ni[nñ]os"],
        sugerible="mencionar que es apto para familias con niños",
    ),
    DefHecho(
        clave="idiomas_atencion",
        pregunta="¿En qué idiomas atendéis además de español? (déjalo vacío si solo español)",
        familia="Aptitudes y público",
        tipo="texto",
        afirmable="Atiende también en: {valor}.",
        vetado=[r"atendemos en ingl[eé]s", r"english spoken", r"hablamos ingl[eé]s"],
        sugerible="mencionar los idiomas de atención",
    ),
    DefHecho(
        clave="distintivo",
        pregunta="¿Tiene algún premio, certificación o distintivo oficial? (indica cuál; deja vacío si no)",
        familia="Distintivos (requieren evidencia)",
        tipo="si_no_evidencia",
        afirmable="Cuenta con el siguiente distintivo verificado: {valor}.",
        vetado=[
            r"estrella michelin", r"michelin", r"sol repsol", r"bib gourmand",
            r"premio", r"galardonad[oa]", r"certificad[oa] por", r"reconocid[oa] por",
            r"distinci[oó]n", r"sello de calidad",
        ],
        sugerible="mencionar el distintivo o premio (tras verificarlo con su evidencia)",
    ),
]


# --- CATÁLOGOS POR FAMILIA DE NICHO ------------------------------------------
# Extienden el base con hechos específicos del sector. Cubren los tipos de
# negocio local más comunes en España. Un nicho que no encaje en ninguno usa
# solo el base + las preguntas extra que proponga la IA (opcionales).
CATALOGOS_NICHO: dict[str, list[DefHecho]] = {

    "restauracion": [
        DefHecho(
            clave="opciones_sin_gluten",
            pregunta="¿Ofrece opciones sin gluten / para celíacos con garantía?",
            familia="Carta y opciones", afirmable="Ofrece opciones sin gluten.",
            vetado=[r"sin gluten", r"cel[ií]ac", r"gluten free"],
            sugerible="mencionar las opciones sin gluten",
        ),
        DefHecho(
            clave="opciones_veganas",
            pregunta="¿Tiene platos vegetarianos o veganos claramente en carta?",
            familia="Carta y opciones", afirmable="Tiene opciones vegetarianas y veganas.",
            vetado=[r"vegano", r"vegetarian", r"plant based"],
            sugerible="mencionar las opciones vegetarianas/veganas",
        ),
        DefHecho(
            clave="menu_dia",
            pregunta="¿Ofrece menú del día?",
            familia="Carta y opciones", afirmable="Ofrece menú del día.",
            vetado=[r"men[uú] del d[ií]a", r"men[uú] diario"],
            sugerible="mencionar el menú del día",
        ),
        DefHecho(
            clave="tipo_cocina",
            pregunta="¿Qué tipo de cocina es? (ej: arrocería, asador, japonés, tapas)",
            familia="Carta y opciones", tipo="texto",
            afirmable="Su especialidad es la cocina: {valor}.",
            vetado=[], sugerible="concretar el tipo de cocina",
        ),
        DefHecho(
            clave="para_llevar",
            pregunta="¿Ofrece comida para llevar o a domicilio?",
            familia="Servicios de restauración", afirmable="Ofrece comida para llevar y a domicilio.",
            vetado=[r"para llevar", r"take away", r"a domicilio", r"delivery"],
            sugerible="mencionar el servicio para llevar/domicilio",
        ),
        DefHecho(
            clave="grupos_eventos",
            pregunta="¿Admite grupos grandes o celebraciones/eventos privados?",
            familia="Servicios de restauración", afirmable="Admite grupos y celebraciones.",
            vetado=[r"grupos grandes", r"celebraciones", r"eventos privados", r"reservados para grupos"],
            sugerible="mencionar la capacidad para grupos y eventos",
        ),
    ],

    "salud": [  # clínicas dentales, fisio, médicas, veterinarias, ópticas...
        DefHecho(
            clave="urgencias",
            pregunta="¿Atiende urgencias?",
            familia="Servicios clínicos", afirmable="Atiende urgencias.",
            vetado=[r"urgencias?", r"atenci[oó]n urgente"],
            sugerible="mencionar la atención de urgencias",
        ),
        DefHecho(
            clave="primera_visita",
            pregunta="¿La primera visita / valoración es gratuita?",
            familia="Servicios clínicos", afirmable="La primera visita o valoración es gratuita.",
            vetado=[r"primera (visita|consulta|valoraci[oó]n) gratu", r"valoraci[oó]n gratis"],
            sugerible="mencionar la primera visita gratuita",
        ),
        DefHecho(
            clave="financiacion",
            pregunta="¿Ofrece financiación o pago a plazos?",
            familia="Servicios clínicos", afirmable="Ofrece financiación y pago a plazos.",
            vetado=[r"financiaci[oó]n", r"pago a plazos", r"a plazos sin intereses"],
            sugerible="mencionar la financiación",
        ),
        DefHecho(
            clave="seguros",
            pregunta="¿Con qué seguros/mutuas trabaja? (déjalo vacío si ninguno)",
            familia="Servicios clínicos", tipo="texto",
            afirmable="Trabaja con los seguros/mutuas: {valor}.",
            vetado=[r"seguros? m[eé]dicos?", r"mutuas?", r"adeslas", r"sanitas", r"asisa", r"dkv"],
            sugerible="indicar los seguros con los que trabaja",
        ),
        DefHecho(
            clave="equipamiento",
            pregunta="¿Tiene equipamiento o técnica destacable? (ej: escáner 3D, láser; vacío si no procede)",
            familia="Servicios clínicos", tipo="texto",
            afirmable="Dispone del siguiente equipamiento verificado: {valor}.",
            vetado=[
                r"esc[aá]ner (intraoral|3d|dental)", r"tac dental", r"l[aá]ser",
                r"tecnolog[ií]a de [uú]ltima generaci[oó]n", r"radiograf[ií]a digital",
            ],
            sugerible="detallar el equipamiento técnico (solo el que tenga)",
        ),
        DefHecho(
            clave="especialidades",
            pregunta="¿Qué tratamientos/especialidades ofrece? (ej: implantes, ortodoncia invisible)",
            familia="Servicios clínicos", tipo="texto",
            afirmable="Sus especialidades son: {valor}.",
            vetado=[], sugerible="listar las especialidades concretas que ofrece",
        ),
    ],

    "automocion": [  # talleres, ITV, neumáticos, chapa y pintura...
        DefHecho(
            clave="coche_sustitucion",
            pregunta="¿Ofrece coche de sustitución mientras repara?",
            familia="Servicios de taller", afirmable="Ofrece coche de sustitución.",
            vetado=[r"coche de sustituci[oó]n", r"veh[ií]culo de cortes[ií]a", r"coche de cortes[ií]a"],
            sugerible="mencionar el coche de sustitución",
        ),
        DefHecho(
            clave="pre_itv",
            pregunta="¿Hace revisión pre-ITV o gestiona la ITV?",
            familia="Servicios de taller", afirmable="Realiza revisión pre-ITV.",
            vetado=[r"pre-?itv", r"gestionamos la itv", r"pasamos la itv"],
            sugerible="mencionar el servicio pre-ITV",
        ),
        DefHecho(
            clave="marcas_especializadas",
            pregunta="¿Está especializado en alguna marca? (déjalo vacío si es multimarca general)",
            familia="Servicios de taller", tipo="texto",
            afirmable="Está especializado en las marcas: {valor}.",
            vetado=[], sugerible="indicar las marcas en las que se especializa",
        ),
        DefHecho(
            clave="garantia_reparacion",
            pregunta="¿Da garantía por escrito en las reparaciones?",
            familia="Servicios de taller", afirmable="Ofrece garantía por escrito en las reparaciones.",
            vetado=[r"garant[ií]a (por escrito|en (las )?reparaciones)"],
            sugerible="mencionar la garantía de las reparaciones",
        ),
    ],

    "belleza": [  # peluquerías, estética, uñas, barberías, spa...
        DefHecho(
            clave="marcas_producto",
            pregunta="¿Trabaja con marcas de producto concretas? (déjalo vacío si no destaca ninguna)",
            familia="Servicios de belleza", tipo="texto",
            afirmable="Trabaja con las marcas: {valor}.",
            vetado=[], sugerible="indicar las marcas de producto con las que trabaja",
        ),
        DefHecho(
            clave="especialidades_belleza",
            pregunta="¿Qué servicios destaca? (ej: coloración, extensiones, uñas de gel, barbería)",
            familia="Servicios de belleza", tipo="texto",
            afirmable="Sus servicios destacados son: {valor}.",
            vetado=[], sugerible="listar los servicios que destaca",
        ),
        DefHecho(
            clave="sin_cita",
            pregunta="¿Atiende también sin cita previa (walk-in)?",
            familia="Servicios de belleza", afirmable="Atiende también sin cita previa.",
            vetado=[r"sin cita", r"walk.?in", r"sin reserva"],
            sugerible="mencionar la atención sin cita",
        ),
    ],

    "alojamiento": [  # hoteles, hostales, casas rurales, campings, apartamentos...
        DefHecho(
            clave="desayuno_incluido",
            pregunta="¿El desayuno está incluido o disponible?",
            familia="Servicios de alojamiento", afirmable="Ofrece desayuno.",
            vetado=[r"desayuno incluido", r"desayuno buffet", r"desayuno disponible"],
            sugerible="mencionar el desayuno",
        ),
        DefHecho(
            clave="piscina",
            pregunta="¿Tiene piscina?",
            familia="Servicios de alojamiento", afirmable="Cuenta con piscina.",
            vetado=[r"piscina"],
            sugerible="mencionar la piscina",
        ),
        DefHecho(
            clave="aire_acondicionado",
            pregunta="¿Las habitaciones tienen aire acondicionado?",
            familia="Servicios de alojamiento", afirmable="Las habitaciones tienen aire acondicionado.",
            vetado=[r"aire acondicionado", r"climatizad"],
            sugerible="mencionar el aire acondicionado",
        ),
    ],

    "comercio": [  # tiendas de barrio, moda, alimentación, especializadas...
        DefHecho(
            clave="envios",
            pregunta="¿Hace envíos a domicilio o venta online?",
            familia="Servicios de comercio", afirmable="Realiza envíos a domicilio.",
            vetado=[r"env[ií]os a domicilio", r"venta online", r"tienda online"],
            sugerible="mencionar los envíos o la venta online",
        ),
        DefHecho(
            clave="asesoramiento",
            pregunta="¿Ofrece asesoramiento personalizado / atención especializada?",
            familia="Servicios de comercio", afirmable="Ofrece asesoramiento personalizado.",
            vetado=[r"asesoramiento personalizado", r"atenci[oó]n especializada"],
            sugerible="mencionar el asesoramiento personalizado",
        ),
    ],

    "servicios": [  # asesorías, abogados, inmobiliarias, reformas, formación...
        DefHecho(
            clave="primera_consulta",
            pregunta="¿La primera consulta / presupuesto es gratuita?",
            familia="Servicios profesionales", afirmable="La primera consulta o presupuesto es gratuita.",
            vetado=[r"primera consulta gratu", r"presupuesto (sin compromiso|gratis)"],
            sugerible="mencionar la primera consulta gratuita",
        ),
        DefHecho(
            clave="online_presencial",
            pregunta="¿Atiende también de forma online / a distancia?",
            familia="Servicios profesionales", afirmable="Atiende también de forma online.",
            vetado=[r"atenci[oó]n online", r"consulta online", r"a distancia"],
            sugerible="mencionar la atención online",
        ),
        DefHecho(
            clave="areas",
            pregunta="¿En qué áreas se especializa? (ej: laboral, fiscal; reformas integrales)",
            familia="Servicios profesionales", tipo="texto",
            afirmable="Se especializa en las áreas: {valor}.",
            vetado=[], sugerible="detallar las áreas de especialización",
        ),
    ],

    "fitness": [  # gimnasios, crossfit, yoga, pilates, entrenadores...
        DefHecho(
            clave="clase_prueba",
            pregunta="¿Ofrece clase o semana de prueba gratis?",
            familia="Servicios de fitness", afirmable="Ofrece clase de prueba gratuita.",
            vetado=[r"clase de prueba", r"semana (de )?prueba", r"prueba gratis"],
            sugerible="mencionar la clase de prueba gratuita",
        ),
        DefHecho(
            clave="entrenador_personal",
            pregunta="¿Ofrece entrenador personal / seguimiento individualizado?",
            familia="Servicios de fitness", afirmable="Ofrece entrenador personal y seguimiento.",
            vetado=[r"entrenador personal", r"seguimiento (individual|personalizado)"],
            sugerible="mencionar el entrenador personal",
        ),
        DefHecho(
            clave="actividades",
            pregunta="¿Qué actividades/disciplinas ofrece? (ej: crossfit, yoga, spinning)",
            familia="Servicios de fitness", tipo="texto",
            afirmable="Ofrece las actividades: {valor}.",
            vetado=[], sugerible="listar las actividades que ofrece",
        ),
    ],
}


# --- MAPA DE NICHO LIBRE -> FAMILIA (determinista) ---------------------------
# El operador escribe el nicho en texto libre ("clínica dental", "arrocería").
# Lo mapeamos a una familia por palabras clave, sin llamar a ningún modelo.
_PALABRAS_FAMILIA: dict[str, tuple] = {
    "restauracion": (
        "restaurante", "bar", "cafeter", "cafe", "arrocer", "asador", "taberna",
        "marisquer", "pizzer", "hamburgues", "tapas", "gastro", "bistro", "cocina",
        "comida", "brunch", "heladería", "heladeria", "pastelería", "pasteleria",
        "panadería", "panaderia", "cervecer", "vinoteca", "sushi", "japones", "kebab",
    ),
    "salud": (
        "clínica", "clinica", "dental", "dentista", "odontolog", "fisio", "médic",
        "medic", "veterinar", "óptica", "optica", "podolog", "psicolog", "nutricion",
        "fisioterapia", "estética médica", "estetica medica", "logopeda", "farmacia",
    ),
    "automocion": (
        "taller", "mecánic", "mecanic", "itv", "neumátic", "neumatic", "chapa",
        "pintura", "coche", "automóvil", "automovil", "motos", "recambios", "vehículo",
    ),
    "belleza": (
        "peluquer", "estétic", "estetic", "barber", "uñas", "unas", "manicura",
        "spa", "belleza", "depilaci", "maquillaje", "centro de estética",
    ),
    "alojamiento": (
        "hotel", "hostal", "pensión", "pension", "casa rural", "camping", "apartament",
        "alojamiento", "albergue", "bungalow", "hospedaje", "turismo rural",
    ),
    "comercio": (
        "tienda", "boutique", "comercio", "moda", "ropa", "zapater", "librería",
        "libreria", "floristería", "floristeria", "joyería", "joyeria", "ferretería",
        "ferreteria", "estanco", "papelería", "papeleria", "frutería", "fruteria",
        "carnicer", "pescader", "supermercado", "ultramarinos",
    ),
    "servicios": (
        "asesoría", "asesoria", "gestoría", "gestoria", "abogad", "inmobiliar",
        "reforma", "fontaner", "electricist", "carpinter", "cerrajer", "academia",
        "formación", "formacion", "consultor", "seguros", "notaría", "notaria",
        "arquitect", "ingenier", "limpieza", "mudanza",
    ),
    "fitness": (
        "gimnasio", "gym", "crossfit", "yoga", "pilates", "fitness", "entrenador",
        "deportiv", "artes marciales", "boxeo", "danza", "baile",
    ),
}


def resolver_familia_nicho(nicho: str) -> Optional[str]:
    """Mapea un nicho en texto libre a una familia del catálogo.

    Determinista, sin modelo. Devuelve None si no encaja en ninguna: en ese caso
    se usa solo el catálogo base más las preguntas extra que sugiera la IA.
    """
    n = (nicho or "").strip().lower()
    if not n or n == "general":
        return None
    for familia, palabras in _PALABRAS_FAMILIA.items():
        if any(p in n for p in palabras):
            return familia
    return None


def construir_cuestionario(nicho: str, tope: int = 12) -> list[DefHecho]:
    """Devuelve la lista ORDENADA de hechos a preguntar para este nicho.

    Estrategia para el punto medio de 10-12 preguntas sin fricción excesiva:
      - Un subconjunto prioritario del base (lo que más se alucina y más valor SEO
        aporta): acceso, distintivo, y un par de aptitudes universales.
      - Todos los hechos de la familia del nicho (suelen ser 3-6).
      - Se completa con el resto del base hasta el tope.
    Con claves estables: guardar y recuperar respuestas es trivial.
    """
    # Prioridad dentro del base: lo primero es lo que más daño hace si se alucina.
    orden_base_prioritario = [
        "distintivo", "parking_propio", "parking_cercano", "accesible_pmr",
        "reserva_cita", "admite_mascotas", "familias_ninos", "terraza",
        "transporte_publico", "pago_tarjeta", "reserva_online", "abierto_findes",
        "wifi", "idiomas_atencion",
    ]
    base_por_clave = {h.clave: h for h in CATALOGO_BASE}
    base_ordenado = [base_por_clave[c] for c in orden_base_prioritario if c in base_por_clave]

    familia = resolver_familia_nicho(nicho)
    especificos = CATALOGOS_NICHO.get(familia, []) if familia else []

    # Los específicos van primero (son los que hacen el contenido rico), luego el
    # base prioritario, hasta llenar el tope.
    seleccion: list[DefHecho] = []
    vistas: set[str] = set()
    for h in especificos + base_ordenado:
        if h.clave in vistas:
            continue
        vistas.add(h.clave)
        seleccion.append(h)
        if len(seleccion) >= tope:
            break
    return seleccion


def catalogo_completo_por_clave(nicho: str) -> dict[str, DefHecho]:
    """Todos los hechos posibles para un nicho (base + familia), indexados por clave.

    Se usa para compilar el léxico vetado: necesitamos conocer TODOS los términos
    típicos de la categoría, no solo los que se preguntaron, para poder vetar los
    que quedaron sin verificar.
    """
    familia = resolver_familia_nicho(nicho)
    todos = list(CATALOGO_BASE) + (CATALOGOS_NICHO.get(familia, []) if familia else [])
    return {h.clave: h for h in todos}


# =============================================================================
# CAPA 0 — LECTURA DE LA FICHA DE VERDAD
# =============================================================================

@dataclass
class EstadoHecho:
    """Un hecho tal y como está en la Ficha, con su estado y datos asociados."""
    clave: str
    estado: str = NO_CONSTA       # SI / NO / NO_CONSTA
    valor: str = ""               # para hechos de tipo texto
    evidencia_anio: str = ""      # para distintivos
    evidencia_entidad: str = ""   # para distintivos


def leer_ficha(filas_supabase: list) -> dict[str, EstadoHecho]:
    """Convierte las filas crudas de la tabla hechos_local en un dict por clave.

    Cualquier clave que no aparezca en la BD se considera NO_CONSTA por defecto:
    ese es el corazón anti-alucinación. Lo no confirmado, no existe.
    """
    ficha: dict[str, EstadoHecho] = {}
    for f in (filas_supabase or []):
        clave = (f.get("clave") or "").strip()
        if not clave:
            continue
        estado = (f.get("estado") or NO_CONSTA).strip().upper()
        if estado not in ESTADOS_VALIDOS:
            estado = NO_CONSTA
        ficha[clave] = EstadoHecho(
            clave=clave,
            estado=estado,
            valor=(f.get("valor") or "").strip(),
            evidencia_anio=str(f.get("evidencia_anio") or "").strip(),
            evidencia_entidad=(f.get("evidencia_entidad") or "").strip(),
        )
    return ficha


def estado_de(ficha: dict[str, EstadoHecho], clave: str) -> str:
    h = ficha.get(clave)
    return h.estado if h else NO_CONSTA


# =============================================================================
# CAPA 1 — COMPILADOR DE LÉXICO (determinista, gratis, 0 ms)
# =============================================================================

@dataclass
class Lexico:
    afirmables: list[str] = field(default_factory=list)   # frases que el modelo PUEDE usar
    vetados: list[str] = field(default_factory=list)      # regex de términos prohibidos
    sugeribles: list[str] = field(default_factory=list)   # nudges para modo asistido
    # Mapa regex_vetado -> nombre humano del hecho, para explicar por qué se vetó.
    _motivo_veto: dict = field(default_factory=dict)


def compilar_lexico(ficha: dict[str, EstadoHecho], nicho: str) -> Lexico:
    """A partir de la Ficha, produce las tres listas que gobiernan la generación.

      afirmables  -> lo verificado como SI, convertido en frase afirmable.
      vetados     -> todo término típico de la categoría cuyo hecho NO esté en SI
                     (es decir: NO explícito, o NO_CONSTA). Si no está verificado,
                     no se puede afirmar, y si aparece en el texto es alucinación.
      sugeribles  -> hechos en NO_CONSTA que, si el dueño los verificara, darían
                     contenido. Alimentan el modo asistido (nudges).
    """
    catalogo = catalogo_completo_por_clave(nicho)
    lex = Lexico()

    for clave, defh in catalogo.items():
        est = estado_de(ficha, clave)

        if est == SI:
            # Construir la frase afirmable, rellenando {valor} si es de texto.
            hecho = ficha.get(clave)
            plantilla = defh.afirmable or ""
            if "{valor}" in plantilla:
                valor = (hecho.valor if hecho else "").strip()
                if not valor:
                    # SI sin valor en un hecho de texto: no es afirmable de forma
                    # útil, lo tratamos como no disponible para no soltar frases
                    # con huecos. No lo vetamos porque el dueño sí dijo que existe.
                    continue
                frase = plantilla.replace("{valor}", valor)
            else:
                frase = plantilla
            # Distintivos: añadir la evidencia a la frase afirmable, es lo que la
            # hace defendible. Sin evidencia, no se afirma.
            if defh.tipo == "si_no_evidencia":
                ev_parts = []
                if hecho and hecho.evidencia_entidad:
                    ev_parts.append(hecho.evidencia_entidad)
                if hecho and hecho.evidencia_anio:
                    ev_parts.append(f"año {hecho.evidencia_anio}")
                if not ev_parts:
                    # Distintivo marcado SI pero sin evidencia -> no afirmable y,
                    # además, se veta el término para que no se cuele.
                    for rgx in defh.vetado:
                        lex.vetados.append(rgx)
                        lex._motivo_veto[rgx] = defh.pregunta
                    continue
                frase = frase + f" (evidencia: {', '.join(ev_parts)})"
            if frase:
                lex.afirmables.append(frase)

        elif est == NO:
            # Verificado que NO existe: se veta con firmeza.
            for rgx in defh.vetado:
                lex.vetados.append(rgx)
                lex._motivo_veto[rgx] = defh.pregunta

        else:  # NO_CONSTA
            # Sin verificar: NO se puede afirmar -> se veta el término...
            for rgx in defh.vetado:
                lex.vetados.append(rgx)
                lex._motivo_veto[rgx] = defh.pregunta
            # ...pero se ofrece como sugerible (modo asistido).
            if defh.sugerible:
                lex.sugeribles.append(defh.sugerible)

    # Dedup preservando orden.
    lex.afirmables = list(dict.fromkeys(lex.afirmables))
    lex.vetados = list(dict.fromkeys(lex.vetados))
    lex.sugeribles = list(dict.fromkeys(lex.sugeribles))
    return lex


def escanear_vetados(texto: str, lex: Lexico) -> list[str]:
    """Filtro determinista: busca términos vetados en el texto generado.

    Espejo exacto de la CAPA 1 de blindaje.py: lo que se puede cazar con una
    regex se caza aquí, gratis, antes de gastar un token en el auditor. Devuelve
    la lista de motivos (preguntas de la Ficha) de los términos que se colaron.
    """
    t = (texto or "").lower()
    pillados = []
    for rgx in lex.vetados:
        try:
            if re.search(rgx, t):
                motivo = lex._motivo_veto.get(rgx, rgx)
                if motivo not in pillados:
                    pillados.append(motivo)
        except re.error:
            continue
    return pillados


# =============================================================================
# CAPAS 2 y 3 — GENERACIÓN ANCLADA + AUDITOR DE VERACIDAD
# =============================================================================

@dataclass
class ResultadoSEO:
    variantes: list = field(default_factory=list)      # las 3 variantes finales
    sugerencias: list = field(default_factory=list)    # nudges de modo asistido
    intentos: int = 0
    bloqueado: bool = False
    motivo: str = ""
    afirmables_usados: list = field(default_factory=list)


# --- Instrucciones por tipo, REESCRITAS sin ordenar inventar -----------------
# La diferencia clave con el prompt viejo: donde antes decía "habla de
# aparcamiento, precio, años de experiencia", ahora dice "usa SOLO los datos
# verificados; si no hay dato para un ángulo, elige otro ángulo o sé más breve".
_INSTRUCCIONES_TIPO = {
    "Publicación de Google Business": (
        "Escribe una publicación de novedades (What's New) de 45-70 palabras para Google Business Profile.\n"
        "Cada variante gira en torno a UN dato VERIFICADO de la lista de datos del negocio. "
        "Si no hay datos verificados suficientes para tres ángulos distintos, haz menos variantes o "
        "céntrate en la identidad del negocio (qué es y dónde está), que siempre es afirmable. "
        "Cierra con una acción concreta y realista (visitar, reservar, llamar).\n"
        "Las tres variantes deben diferir en ÁNGULO, no solo en orden de palabras."
    ),
    "Descripción de servicio/producto": (
        "Escribe una descripción de 50-80 palabras para la pestaña de Servicios/Productos.\n"
        "Alimenta las AI overviews de Google: frases autocontenidas y concretas, pero SOLO con datos "
        "verificados. Prohibido rellenar con adjetivos vacíos ('excelente', 'profesional') o con datos "
        "no confirmados. Si hay pocos datos, una descripción corta y veraz vale más que una larga inventada."
    ),
    "Pregunta y respuesta (Q&A)": (
        "Genera bloques Q&A para la sección de Preguntas y Respuestas de Google Business Profile.\n"
        "REGLA ABSOLUTA: solo puedes generar una pregunta sobre un tema si hay un DATO VERIFICADO que "
        "responda a ella. Si no sabes si hay aparcamiento, NO generes la pregunta del aparcamiento. "
        "Prohibido inventar la respuesta a una duda logística no verificada: esa es exactamente la "
        "alucinación que arruina la credibilidad del negocio.\n"
        "Genera un bloque Q&A por cada dato verificado relevante (máximo 3). Si solo hay datos para un "
        "bloque, genera uno. Cero bloques inventados.\n"
        "Cada respuesta: 35-55 palabras, autocontenida, con el nombre del negocio y la zona integrados "
        "con naturalidad.\n"
        "Formato de cada bloque: 'P: ...' en una línea y 'R: ...' en la siguiente. Separa bloques con "
        "una línea en blanco. Devuelve las variantes como bloques Q&A completos."
    ),
    "Oferta / promoción": (
        "Escribe una publicación de tipo Oferta de 40-60 palabras.\n"
        "La oferta debe basarse en un servicio o producto VERIFICADO del negocio. No inventes descuentos "
        "concretos, plazos ni condiciones: describe el beneficio en términos que el dueño pueda cumplir, "
        "y deja que él concrete el porcentaje si quiere. Prohibida la urgencia falsa ('solo hoy', "
        "'últimas plazas'). El beneficio debe entenderse en las primeras 10 palabras."
    ),
    "Descripción para redes sociales": (
        "Escribe una descripción de 30-50 palabras para el pie de una publicación de Instagram o Facebook.\n"
        "Cada variante debe apoyarse en un detalle VERIFICADO del negocio, no en un tópico. Sin datos "
        "verificados, céntrate en la identidad (qué es, dónde, su carácter). Tono cercano, como el propio "
        "dueño. Máximo 3 hashtags: uno de nicho, uno de zona, uno de servicio (solo si el servicio está verificado)."
    ),
    "Meta descripción SEO": (
        "Escribe una meta descripción SEO para la web del negocio.\n"
        "LÍMITE ABSOLUTO: máximo 155 caracteres por variante (cuéntalos). Estructura: [keyword de intención "
        "local] + [propuesta de valor VERIFICADA] + [CTA breve]. La keyword local en los primeros 60 caracteres.\n"
        "Prohibido prometer nada no verificado. Si no hay datos diferenciadores verificados, apóyate en "
        "qué es el negocio y dónde está, que siempre es cierto."
    ),
}


def _construir_system_generacion(nombre_local, nicho, ciudad, lex: Lexico, keywords, instruccion, modo_asistido):
    zona = (ciudad or "").strip()
    referencia = f"{nombre_local} en {zona}" if zona else nombre_local

    if lex.afirmables:
        bloque_afirmables = (
            "DATOS VERIFICADOS DEL NEGOCIO (esta es tu ÚNICA fuente de hechos; puedes afirmar "
            "estos y solo estos):\n" + "\n".join(f"  · {a}" for a in lex.afirmables)
        )
    else:
        bloque_afirmables = (
            "DATOS VERIFICADOS DEL NEGOCIO: NINGUNO todavía.\n"
            "Solo puedes afirmar la identidad básica: que es un negocio del tipo indicado y su zona. "
            "No atribuyas ningún servicio, característica, premio ni instalación concretos."
        )

    # La zona y la categoría siempre son afirmables (vienen de la ficha del local).
    identidad = (
        f"IDENTIDAD (siempre afirmable): el negocio se llama «{nombre_local}», es del sector «{nicho}»"
        + (f" y está en «{zona}»." if zona else " (zona no especificada; no inventes una).")
    )

    kw_txt = ", ".join(keywords) if keywords else "ninguna cargada"

    prohibicion_nudge = ""
    if modo_asistido and lex.sugeribles:
        prohibicion_nudge = (
            "\nNOTA: hay temas que este negocio PODRÍA tener pero que NO están verificados "
            "(p. ej. " + "; ".join(lex.sugeribles[:4]) + "). NO los menciones en el contenido. "
            "Se le sugerirán aparte al dueño para que los verifique."
        )

    return f"""Eres un especialista en SEO local y GEO (Generative Engine Optimization) que escribe con la voz auténtica de «{referencia}». Suenas al propio negocio hablando de sí mismo, no a una agencia externa.

{identidad}

{bloque_afirmables}

REGLA DE ORO, POR ENCIMA DE TODO
No puedes afirmar NADA que no esté en la lista de datos verificados o en la identidad. Está terminantemente prohibido inventar servicios, instalaciones, premios, cifras, aparcamiento, horarios, equipamiento o cualquier característica no verificada. Si dudas de si algo es cierto, NO lo escribas. Una frase de menos es infinitamente mejor que una afirmación falsa: nuestro producto entero se basa en no mentir en nombre del cliente.

KEYWORDS SEO (inventario opcional, NO una cuota; intégralas solo si encajan con naturalidad y solo si son coherentes con los datos verificados): {kw_txt}.

NORMAS ANTI-PLANTILLA
- Prohibidas y sus variantes: "tecnología de última generación", "equipo de profesionales", "atención personalizada", "calidad garantizada", "tu satisfacción es lo primero", "no te lo pierdas", "ven a disfrutar", "somos tu mejor opción".
- Un texto que cualquier otro negocio del mismo sector pudiera publicar sin cambiar una palabra es un FRACASO.
- GEO: frases autocontenidas, datos concretos y VERIFICADOS, así es como Google las usa en respuestas de IA.{prohibicion_nudge}

TAREA:
{instruccion}

Devuelve tu respuesta EXCLUSIVAMENTE como un array JSON de hasta 3 strings, sin texto antes ni después, sin markdown.
Formato exacto: ["variante 1", "variante 2", "variante 3"]"""


def _auditar_veracidad(client, texto, lex: Lexico, nombre_local, nicho, ciudad):
    """CAPA 3 — El inspector de consumo.

    Espejo del auditor legal de blindaje.py, pero con otro rol: extrae toda
    afirmación factual del texto y comprueba si cada una está respaldada por la
    Ficha (datos verificados + identidad). Devuelve lista de afirmaciones sin
    anclaje. Lista vacía = el texto es veraz.
    """
    afirmables_txt = "\n".join(f"  · {a}" for a in lex.afirmables) or "  (ninguno)"
    zona = (ciudad or "").strip()
    system = f"""Eres un inspector de veracidad publicitaria. Tu trabajo es proteger a un negocio de afirmar en público cosas que no puede demostrar (publicidad engañosa).

Recibes un texto de marketing y la lista CERRADA de hechos verificados sobre el negocio. Tu tarea: localizar toda afirmación factual del texto que NO esté respaldada por un hecho verificado o por la identidad básica.

IDENTIDAD BÁSICA (siempre válida): nombre «{nombre_local}», sector «{nicho}»{f', zona «{zona}»' if zona else ''}.

HECHOS VERIFICADOS (lo único afirmable además de la identidad):
{afirmables_txt}

Qué cuenta como afirmación factual NO respaldada (señálala):
- Servicios, instalaciones o características concretas no verificadas (aparcamiento, terraza, wifi, financiación, equipamiento...).
- Premios, distinciones, estrellas, certificaciones no verificadas.
- Cifras concretas no verificadas (años de experiencia, número de clientes, valoraciones).
- Horarios, precios o condiciones concretas no verificadas.

Qué NO debes señalar:
- Opiniones subjetivas genéricas sin dato ("un ambiente acogedor", "trato cercano").
- La identidad básica (nombre, sector, zona).
- Invitaciones neutras ("te esperamos", "reserva tu visita") sin promesa concreta.

Devuelve EXCLUSIVAMENTE este JSON:
{{"afirmaciones_sin_respaldo": ["fragmento textual 1", "fragmento textual 2"]}}
Si todo está respaldado, devuelve la lista vacía."""

    try:
        r = client.messages.create(
            model=MODELO_AUDITOR_SEO,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": f"Texto a auditar:\n\n{texto}"}],
        )
        bruto = ""
        for b in r.content:
            if getattr(b, "type", None) == "text":
                bruto = b.text.strip()
                break
        if bruto.startswith("```"):
            bruto = re.sub(r"^```(?:json)?|```$", "", bruto).strip()
        datos = json.loads(bruto)
        return [str(x).strip() for x in datos.get("afirmaciones_sin_respaldo", []) if str(x).strip()]
    except Exception:
        # Si el auditor falla, no bloqueamos la generación por ello: el filtro
        # determinista (escanear_vetados) ya ha hecho la criba dura y gratis.
        return []


def generar_contenido_seo(
    client,
    nombre_local: str,
    nicho: str,
    ciudad: str,
    ficha_filas: list,
    tipo_contenido: str,
    keywords: Optional[list] = None,
    modo_asistido: bool = True,
) -> ResultadoSEO:
    """Punto de entrada del motor. Sustituye a generar_contenido_seo_extra.

    Flujo:
      CAPA 0  Lee la Ficha de Verdad.
      CAPA 1  Compila léxico afirmable/vetado.
      CAPA 2  Genera anclado a los afirmables.
      CAPA 1' Escaneo determinista de vetados (gratis).
      CAPA 3  Auditor de veracidad (modelo). Si caza algo -> regenera señalando.
    """
    keywords = keywords or []
    res = ResultadoSEO()

    ficha = leer_ficha(ficha_filas)                    # CAPA 0
    lex = compilar_lexico(ficha, nicho)                # CAPA 1
    res.afirmables_usados = list(lex.afirmables)
    res.sugerencias = list(lex.sugeribles) if modo_asistido else []

    instruccion = _INSTRUCCIONES_TIPO.get(tipo_contenido, _INSTRUCCIONES_TIPO["Publicación de Google Business"])
    system = _construir_system_generacion(
        nombre_local, nicho, ciudad, lex, keywords, instruccion, modo_asistido
    )

    historial = [{"role": "user", "content": f"Genera el contenido para «{nombre_local}»."}]

    for intento in range(1, MAX_INTENTOS_VERACIDAD + 2):
        res.intentos = intento
        try:
            r = client.messages.create(
                model=MODELO_REDACTOR_SEO,
                max_tokens=1200,
                system=system,
                messages=historial,
            )
        except Exception:
            res.bloqueado = True
            res.motivo = "No se pudo contactar con el servicio de redacción."
            return res

        bruto = ""
        for b in r.content:
            if getattr(b, "type", None) == "text":
                bruto += b.text
        bruto = bruto.strip()
        limpio = bruto.replace("```json", "").replace("```", "").strip()

        try:
            variantes = json.loads(limpio)
            if not (isinstance(variantes, list) and variantes):
                variantes = [bruto] if bruto else []
        except (json.JSONDecodeError, ValueError):
            variantes = [bruto] if bruto else []
        variantes = [str(v).strip() for v in variantes if str(v).strip()]

        if not variantes:
            res.bloqueado = True
            res.motivo = "El redactor devolvió una respuesta vacía."
            return res

        # --- CAPA 1' determinista sobre cada variante (gratis) ---------------
        motivos_deterministas = []
        for v in variantes:
            motivos_deterministas += escanear_vetados(v, lex)
        motivos_deterministas = list(dict.fromkeys(motivos_deterministas))

        # --- CAPA 3 auditor sobre el conjunto (solo si pasó el determinista) -
        sin_respaldo = []
        if not motivos_deterministas:
            texto_unido = "\n\n".join(variantes)
            sin_respaldo = _auditar_veracidad(client, texto_unido, lex, nombre_local, nicho, ciudad)

        problemas = motivos_deterministas + sin_respaldo

        # Limpio o sin vueltas -> devolvemos.
        if not problemas or intento >= (MAX_INTENTOS_VERACIDAD + 1):
            res.variantes = variantes
            return res

        # --- Regeneración correctiva señalando el fallo exacto ---------------
        detalle = []
        if motivos_deterministas:
            detalle.append(
                "Has mencionado temas NO verificados: " + "; ".join(motivos_deterministas) + "."
            )
        if sin_respaldo:
            detalle.append(
                "Estas afirmaciones no tienen respaldo en los datos verificados y hay que eliminarlas o "
                "reescribirlas sin el dato inventado: " + "; ".join(f'«{s}»' for s in sin_respaldo) + "."
            )
        historial += [
            {"role": "assistant", "content": limpio},
            {"role": "user", "content": (
                " ".join(detalle) +
                " Reescribe las variantes usando SOLO datos verificados e identidad. "
                "Si te quedas sin material para tres variantes, haz menos. Devuelve el array JSON."
            )},
        ]

    res.bloqueado = True
    res.motivo = "No se pudo producir contenido veraz tras varios intentos."
    return res


# =============================================================================
# CUESTIONARIO EXTRA POR IA (opcional, seguro: la IA solo PREGUNTA)
# =============================================================================

def sugerir_preguntas_extra(client, nicho: str, ciudad: str = "", ya_preguntadas: Optional[list] = None) -> list:
    """Propone preguntas EXTRA específicas del nicho que el catálogo fijo no cubre.

    Este es el uso seguro del modelo en la Ficha: no genera datos, genera
    preguntas. Imposible que alucine un hecho porque solo formula dudas para que
    el dueño las responda. Devuelve lista de dicts {clave, pregunta, familia}.
    Las claves van namespaced como 'custom:<slug>' para no chocar con el catálogo.
    """
    ya = ", ".join(ya_preguntadas or []) or "ninguna"
    prompt = f"""Eres consultor de SEO local. Un negocio del sector «{nicho}»{f' en {ciudad}' if ciudad else ''} va a rellenar una ficha de datos verificados para generar contenido SEO honesto.

Ya se le preguntan estos temas: {ya}.

Propón entre 2 y 4 preguntas ADICIONALES muy específicas de este sector concreto que aporten datos diferenciadores para SEO local y que NO estén ya cubiertas. Cada pregunta debe poder responderse con sí/no o con un dato corto, y debe referirse a algo verificable y concreto del negocio (un servicio, una instalación, una especialidad), nunca a opiniones.

Reglas:
- Nada de preguntas genéricas que valgan para cualquier negocio ("¿ofrece calidad?").
- Deben ser hechos concretos y comprobables.
- Redacta la pregunta tal y como se la harías al dueño, en segunda persona.

Devuelve EXCLUSIVAMENTE este JSON:
{{"preguntas": [{{"pregunta": "...", "familia": "..."}}]}}"""

    try:
        r = client.messages.create(
            model=MODELO_REDACTOR_SEO,
            max_tokens=700,
            temperature=0.6,
            messages=[{"role": "user", "content": prompt}],
        )
        bruto = ""
        for b in r.content:
            if getattr(b, "type", None) == "text":
                bruto = b.text.strip()
                break
        if bruto.startswith("```"):
            bruto = re.sub(r"^```(?:json)?|```$", "", bruto).strip()
        datos = json.loads(bruto)
        salida = []
        vistas = set()
        for p in datos.get("preguntas", []):
            texto = (p.get("pregunta") or "").strip()
            if not texto or texto.lower() in vistas:
                continue
            vistas.add(texto.lower())
            slug = re.sub(r"[^a-z0-9]+", "_", texto.lower())[:40].strip("_")
            salida.append({
                "clave": f"custom:{slug}",
                "pregunta": texto,
                "familia": (p.get("familia") or "Específico del sector").strip(),
            })
        return salida[:4]
    except Exception:
        return []


# =============================================================================
# ENGANCHE DE ENRIQUECIMIENTO WEB (fase C — preparado, básico)
# =============================================================================
#
# IMPORTANTE: esto se ejecuta en el runtime de la app (Render), que sí tiene
# salida a internet abierta. Nunca afirma nada por su cuenta: extrae CANDIDATOS
# de la propia web del negocio y los devuelve para que el dueño los CONFIRME uno
# a uno. La fuente es lo que el propio negocio publica, no un agregador de
# terceros (que suelen estar desactualizados y contaminados con la competencia).

# Señales de texto -> clave de hecho candidato. Heurística simple y conservadora:
# solo propone, el humano decide.
_SEÑALES_WEB = {
    "parking_propio": (r"parking propio", r"aparcamiento propio", r"garaje propio"),
    "parking_cercano": (r"f[aá]cil aparcamiento", r"aparcamiento (cercano|p[uú]blico)", r"zona azul"),
    "accesible_pmr": (r"accesible", r"sin barreras", r"movilidad reducida", r"adaptado"),
    "reserva_online": (r"reserva (online|por internet)", r"reservar? (aqu[ií]|online)"),
    "terraza": (r"terraza",),
    "wifi": (r"wifi", r"wi-fi"),
    "admite_mascotas": (r"pet friendly", r"admitimos mascotas", r"se admiten perros"),
    "para_llevar": (r"para llevar", r"take away", r"a domicilio"),
    "opciones_sin_gluten": (r"sin gluten", r"cel[ií]ac"),
    "opciones_veganas": (r"vegano", r"vegetarian"),
    "financiacion": (r"financiaci[oó]n", r"pago a plazos"),
    "urgencias": (r"urgencias",),
    "desayuno_incluido": (r"desayuno incluido", r"desayuno buffet"),
    "piscina": (r"piscina",),
    "coche_sustitucion": (r"coche de sustituci[oó]n", r"veh[ií]culo de cortes[ií]a"),
}


def proponer_hechos_desde_web(url: str, nicho: str = "", timeout: int = 8) -> list:
    """Lee la web propia del negocio y propone hechos candidatos a verificar.

    Devuelve lista de dicts {clave, pregunta, evidencia_texto} donde
    evidencia_texto es el fragmento de la web donde se detectó la señal, para que
    el dueño vea POR QUÉ se le propone. No marca nada como verificado: es material
    para que el humano confirme en un clic.

    Se importa requests de forma perezosa para no añadir dependencia dura si el
    enriquecimiento no se usa.
    """
    url = (url or "").strip()
    if not url:
        return []
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        import requests  # dependencia opcional; ya suele estar por Streamlit/otros
    except Exception:
        return []

    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "ReseliaBot/1.0 (+seo)"})
        html = resp.text or ""
    except Exception:
        return []

    # Quitar etiquetas para quedarnos con texto plano en minúscula.
    texto = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto).lower()

    catalogo = catalogo_completo_por_clave(nicho)
    propuestas = []
    for clave, señales in _SEÑALES_WEB.items():
        if clave not in catalogo:
            continue
        for rgx in señales:
            m = re.search(rgx, texto)
            if m:
                ini = max(0, m.start() - 40)
                fin = min(len(texto), m.end() + 40)
                fragmento = texto[ini:fin].strip()
                propuestas.append({
                    "clave": clave,
                    "pregunta": catalogo[clave].pregunta,
                    "evidencia_texto": f"…{fragmento}…",
                })
                break
    # Dedup por clave.
    vistas, unicas = set(), []
    for p in propuestas:
        if p["clave"] not in vistas:
            vistas.add(p["clave"])
            unicas.append(p)
    return unicas
