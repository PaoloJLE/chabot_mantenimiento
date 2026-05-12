import os
import re
import json
import time
import uuid
from datetime import datetime

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

from google import genai
from google.genai import types
from google.cloud import bigquery
from fracttal_client import login_fracttal, navigate_to_work_requests


# =========================
# Configuración
# =========================
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "spsa-marketing-prd")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
BQ_DATASET = os.environ.get("BQ_DATASET", "mantenimiento_ia")
BQ_TABLE = os.environ.get("BQ_TABLE", "tickets_chatbot")

os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION

client = genai.Client()
bq_client = bigquery.Client(project=PROJECT_ID)

app = Flask(__name__)
CORS(app)

INSTANCE_UUID = str(uuid.uuid4())
HOSTNAME = os.environ.get("HOSTNAME", "unknown-host")

SUPPORTED_AUDIO_MIME_TYPES = {
    "audio/webm",
    "audio/wav",
    "audio/x-wav",
    "audio/mp4",
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
}

MAX_AUDIO_BYTES = 10 * 1024 * 1024  # 10 MB


# =========================
# Utilidades
# =========================
def log_event(data: dict):
    print(json.dumps(data, ensure_ascii=False))


def intentar_parsear_json(texto: str) -> dict:
    texto = (texto or "").strip()
    texto = texto.replace("```json", "").replace("```", "").strip()

    inicio = texto.find("{")
    fin = texto.rfind("}")
    if inicio != -1 and fin != -1 and fin > inicio:
        texto = texto[inicio:fin + 1]

    return json.loads(texto)


def normalizar_estructura(data: dict) -> dict:
    estructura = {
        "es_requerimiento_valido": False,
        "motivo_no_valido": "",
        "informacion_suficiente": False,
        "campos_faltantes": [],
        "tienda": "No identificado",
        "tipo_requerimiento": "Indeterminado",
        "subtipo": "No identificado",
        "activo": "No identificado",
        "ubicacion": "No identificada",
        "descripcion_problema": "No identificado",
        "impacto_operativo": "No claro",
        "afecta_cliente": False,
        "riesgo_seguridad": False,
        "requiere_revision_manual": True,
        "justificacion": "Sin justificación"
    }

    for k in estructura:
        if k in data:
            estructura[k] = data[k]

    if estructura["tipo_requerimiento"] not in [
        "Correctivo", "Preventivo", "Infraestructura", "Indeterminado"
    ]:
        estructura["tipo_requerimiento"] = "Indeterminado"

    if estructura["impacto_operativo"] not in ["Alto", "Medio", "Bajo", "No claro"]:
        estructura["impacto_operativo"] = "No claro"

    estructura["es_requerimiento_valido"] = bool(estructura["es_requerimiento_valido"])
    estructura["informacion_suficiente"] = bool(estructura["informacion_suficiente"])
    estructura["afecta_cliente"] = bool(estructura["afecta_cliente"])
    estructura["riesgo_seguridad"] = bool(estructura["riesgo_seguridad"])
    estructura["requiere_revision_manual"] = bool(estructura["requiere_revision_manual"])

    if not isinstance(estructura["campos_faltantes"], list):
        estructura["campos_faltantes"] = []

    return estructura


def extraer_tienda_desde_texto(texto_usuario: str) -> str:
    texto = (texto_usuario or "").strip()

    patrones = [
        r"en la tienda\s+([^,\.]+)",
        r"en tienda\s+([^,\.]+)",
        r"en el local\s+([^,\.]+)",
        r"en local\s+([^,\.]+)",
        r"de la tienda\s+([^,\.]+)",
        r"del local\s+([^,\.]+)",
        r"soy de la tienda\s+([^,\.]+)",
        r"soy del local\s+([^,\.]+)",
        r"trabajo en la tienda\s+([^,\.]+)",
        r"trabajo en el local\s+([^,\.]+)",
        r"en\s+(plaza vea[^,\.]+)",
        r"en\s+(vivanda[^,\.]+)",
        r"en\s+(makro[^,\.]+)",
    ]

    for patron in patrones:
        match = re.search(patron, texto, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return "No identificado"


def extraer_ubicacion_desde_texto(texto_usuario: str) -> str:
    texto = (texto_usuario or "").strip()

    patrones = [
        r"del\s+([^,\.]+)",
        r"de la\s+([^,\.]+)",
        r"de los\s+([^,\.]+)",
        r"de las\s+([^,\.]+)",
        r"en el área\s+([^,\.]+)",
        r"en la zona\s+([^,\.]+)",
        r"en el muelle\s+([^,\.]+)",
        r"en el almac[eé]n\s+([^,\.]+)",
        r"en almac[eé]n\s+([^,\.]+)",
    ]

    for patron in patrones:
        match = re.search(patron, texto, flags=re.IGNORECASE)
        if match:
            ubicacion = match.group(1).strip()
            ubicacion = re.sub(r"\s+y\s+.*$", "", ubicacion, flags=re.IGNORECASE).strip()
            if ubicacion:
                return ubicacion

    pistas = [
        "muelle", "almacén", "almacen", "área", "area", "caja", "cajas",
        "patio", "zona", "pasillo", "mostrador", "recepción", "recepcion",
        "puerta", "lácteos", "lacteos", "carnes", "abarrotes", "almacén de", "área de"
    ]

    texto_lower = texto.lower()
    for pista in pistas:
        if pista in texto_lower:
            return pista.capitalize()

    return "No identificada"


def rescatar_requerimiento_si_aplica(texto_usuario: str, analisis: dict) -> dict:
    """
    Corrige casos donde Gemini fue demasiado estricto o no llenó bien tienda/ubicación.
    """
    texto = (texto_usuario or "").lower().strip()

    saludos_invalidos = {
        "hola", "buen día", "buenas", "consulta", "necesito ayuda", "oye", "buenos días"
    }

    if texto in saludos_invalidos:
        return analisis

    tiene_tienda = any(x in texto for x in [
        "tienda ", "local ", "plaza vea", "vivanda", "makro"
    ])

    tiene_ubicacion = any(x in texto for x in [
        "área", "area", "muelle", "almacén", "almacen", "caja", "cajas",
        "pasillo", "patio", "zona", "puerta", "mostrador", "recepción",
        "recepcion", "abarrotes", "carnes", "lácteos", "lacteos"
    ])

    tiene_problema = any(x in texto for x in [
        "no ", "falla", "mal", "aver", "roto", "riesgo", "no cierra",
        "no enfría", "no enfria", "no transmite", "no funciona",
        "problema", "compromete", "afecta", "genera", "no está", "no esta"
    ])

    if (analisis.get("tienda") or "").strip().lower() in ["", "no identificado", "no identificado."]:
        tienda_detectada = extraer_tienda_desde_texto(texto_usuario)
        if tienda_detectada != "No identificado":
            analisis["tienda"] = tienda_detectada

    if (analisis.get("ubicacion") or "").strip().lower() in ["", "no identificada", "no identificada."]:
        ubicacion_detectada = extraer_ubicacion_desde_texto(texto_usuario)
        if ubicacion_detectada != "No identificada":
            analisis["ubicacion"] = ubicacion_detectada

    if (analisis.get("descripcion_problema") or "").strip().lower() in ["", "no identificado", "no identificado."]:
        analisis["descripcion_problema"] = texto_usuario

    if tiene_tienda and tiene_problema:
        analisis["es_requerimiento_valido"] = True

    tienda_ok = (analisis.get("tienda") or "").strip().lower() not in ["", "no identificado", "no identificado."]
    ubicacion_ok = (analisis.get("ubicacion") or "").strip().lower() not in ["", "no identificada", "no identificada."]
    descripcion_ok = (analisis.get("descripcion_problema") or "").strip().lower() not in ["", "no identificado", "no identificado."]

    campos_faltantes = []
    if not tienda_ok:
        campos_faltantes.append("tienda")
    if not ubicacion_ok:
        campos_faltantes.append("ubicacion")
    if not descripcion_ok:
        campos_faltantes.append("descripcion_problema")

    analisis["campos_faltantes"] = campos_faltantes

    if tienda_ok and ubicacion_ok and descripcion_ok:
        analisis["es_requerimiento_valido"] = True
        analisis["informacion_suficiente"] = True
        analisis["motivo_no_valido"] = ""
    else:
        analisis["informacion_suficiente"] = False

    return analisis


# =========================
# Gemini
# =========================
def interpretar_requerimiento_con_gemini(texto_usuario: str) -> dict:
    system_instruction = """
Eres el Chatbot de Requerimiento para tiendas retail.

Tu función es ayudar a supervisores de tienda a registrar requerimientos de mantenimiento de manera correcta, clara y eficiente, siempre en español.

## REGLAS GENERALES

- SIEMPRE responde en español.
- Debes entender mensajes escritos en lenguaje natural.
- El usuario NO necesita usar términos técnicos como “correctivo”, “preventivo” o “infraestructura”.
- Tu labor es interpretar el mensaje, validar si realmente corresponde a un requerimiento y determinar si existe información suficiente para generar un ticket.
- NO inventes información.
- Si falta información importante, NO debes generar ticket.
- Si el mensaje no corresponde a un requerimiento real, NO debes generar ticket.
- Debes ser claro, breve, profesional y útil.

## OBJETIVO PRINCIPAL

Analizar el mensaje del supervisor y decidir si:
1. Es un requerimiento válido
2. Tiene información suficiente para registrar un ticket
3. Se puede clasificar internamente
4. Se debe pedir más información antes de continuar

## CAMPOS QUE DEBES INTERPRETAR

Debes intentar extraer estos campos desde el mensaje del usuario:

- tienda: nombre del local o tienda
- ubicacion: ubicación dentro de la tienda
- descripcion_problema: problema observado
- activo: equipo, activo o elemento afectado, si se puede identificar
- tipo_requerimiento: clasificación interna
- subtipo: categoría más específica
- impacto_operativo: Alto, Medio, Bajo o No claro
- afecta_cliente: true o false
- riesgo_seguridad: true o false
- requiere_revision_manual: true o false
- justificacion: breve explicación del análisis

## CAMPOS MÍNIMOS OBLIGATORIOS PARA GENERAR TICKET

Solo se puede considerar que hay información suficiente si se puede identificar razonablemente:
1. tienda
2. ubicacion dentro de la tienda
3. descripcion_problema

El campo activo es importante, pero NO es obligatorio para generar ticket si el problema está suficientemente claro.

## CUÁNDO UN MENSAJE NO ES UN REQUERIMIENTO VÁLIDO

Debes marcar:
- es_requerimiento_valido = false

solo cuando el mensaje sea claramente uno de estos casos:
- un saludo
- una frase sin incidencia concreta
- una intención genérica sin problema real
- un texto ambiguo que no describa ninguna necesidad operativa

Ejemplos de mensajes NO válidos:
- "Hola"
- "Buen día"
- "Necesito ayuda"
- "Consulta"
- "Oye"

IMPORTANTE:
Si el mensaje describe una falla, riesgo, avería, problema operativo o necesidad real dentro de una tienda, entonces sí debe considerarse un requerimiento válido, aunque no esté redactado de forma perfecta.

## CUÁNDO UN MENSAJE SÍ ES UN REQUERIMIENTO VÁLIDO PERO INCOMPLETO

Debes marcar:
- es_requerimiento_valido = true
- informacion_suficiente = false

cuando sí existe una incidencia o necesidad real, pero faltan datos mínimos.

Ejemplos:
- "En la tienda San Isidro hay una falla"
- "El congelador no funciona"
- "La puerta está mal"
- "Hay un problema en caja"

## CUÁNDO UN MENSAJE SÍ TIENE INFORMACIÓN SUFICIENTE

Debes marcar:
- es_requerimiento_valido = true
- informacion_suficiente = true

cuando se pueda identificar razonablemente:
- la tienda
- la ubicación dentro de la tienda
- el problema observado

No exijas una redacción perfecta.
No exijas que el usuario use términos técnicos.
No exijas que el activo esté perfectamente descrito si el problema ya está claro.

Ejemplos de mensajes SUFICIENTES:
- "En la tienda Plaza Vea Higuereta, la puerta del almacén de abarrotes no cierra correctamente y representa un riesgo de seguridad para el turno de noche."
- "En la tienda San Isidro, la cámara de seguridad del muelle 2 no transmite imagen y afecta el monitoreo de ingreso de mercadería."
- "En Vivanda Dos de Mayo, el congelador del área de lácteos no está manteniendo la temperatura adecuada."

## CLASIFICACIÓN INTERNA

Debes clasificar internamente el requerimiento en uno de estos tipos:
- Correctivo
- Preventivo
- Infraestructura
- Indeterminado

Criterios:
- Correctivo: existe falla activa, avería o interrupción de funcionamiento
- Preventivo: revisión, inspección o mantenimiento sin falla activa evidente
- Infraestructura: problema relacionado a ambiente físico, puertas, mobiliario, iluminación, señalización, instalaciones generales
- Indeterminado: no hay suficiente claridad

## FORMATO DE SALIDA

Debes devolver SOLO un JSON válido.
No agregues texto adicional.
No uses markdown.
No expliques fuera del JSON.

Debes devolver exactamente esta estructura:

{
  "es_requerimiento_valido": true,
  "motivo_no_valido": "",
  "informacion_suficiente": true,
  "campos_faltantes": [],
  "tienda": "string",
  "tipo_requerimiento": "Correctivo | Preventivo | Infraestructura | Indeterminado",
  "subtipo": "string",
  "activo": "string",
  "ubicacion": "string",
  "descripcion_problema": "string",
  "impacto_operativo": "Alto | Medio | Bajo | No claro",
  "afecta_cliente": true,
  "riesgo_seguridad": false,
  "requiere_revision_manual": false,
  "justificacion": "string"
}
"""

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=f"Mensaje del supervisor: {texto_usuario}",
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.1,
            max_output_tokens=1000,
        ),
    )

    raw_text = response.text or ""

    log_event({
        "event": "gemini_raw_response",
        "instance_uuid": INSTANCE_UUID,
        "hostname": HOSTNAME,
        "raw_text": raw_text
    })

    try:
        data = intentar_parsear_json(raw_text)
    except Exception:
        data = {
            "es_requerimiento_valido": False,
            "motivo_no_valido": "No se pudo interpretar el mensaje como un requerimiento válido.",
            "informacion_suficiente": False,
            "campos_faltantes": ["tienda", "ubicacion", "descripcion_problema"],
            "tienda": "No identificado",
            "tipo_requerimiento": "Indeterminado",
            "subtipo": "No identificado",
            "activo": "No identificado",
            "ubicacion": "No identificada",
            "descripcion_problema": texto_usuario,
            "impacto_operativo": "No claro",
            "afecta_cliente": False,
            "riesgo_seguridad": False,
            "requiere_revision_manual": True,
            "justificacion": "No se pudo interpretar el requerimiento con suficiente claridad."
        }

    analisis = normalizar_estructura(data)
    analisis = rescatar_requerimiento_si_aplica(texto_usuario, analisis)
    return analisis


def transcribir_audio_con_gemini(audio_bytes: bytes, mime_type: str) -> str:
    prompt = (
        "Transcribe este audio en español de forma fiel. "
        "Devuelve solo la transcripción. "
        "No resumas. No clasifiques. No expliques. "
        "Si una palabra no se entiende, usa [inaudible]."
    )

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            prompt,
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=1000,
        ),
    )

    transcripcion = (response.text or "").strip()

    log_event({
        "event": "audio_transcription_response",
        "instance_uuid": INSTANCE_UUID,
        "hostname": HOSTNAME,
        "mime_type": mime_type,
        "transcripcion": transcripcion
    })

    return transcripcion


# =========================
# Reglas de negocio
# =========================
def evaluar_reglas(analisis: dict) -> dict:
    es_valido = analisis["es_requerimiento_valido"]
    informacion_suficiente = analisis["informacion_suficiente"]
    campos_faltantes = analisis["campos_faltantes"]

    impacto = analisis["impacto_operativo"]
    afecta_cliente = analisis["afecta_cliente"]
    riesgo_seguridad = analisis["riesgo_seguridad"]
    revision_manual = analisis["requiere_revision_manual"]

    tienda = (analisis["tienda"] or "").strip()
    ubicacion = (analisis["ubicacion"] or "").strip()
    descripcion = (analisis["descripcion_problema"] or "").strip()

    if not es_valido:
        motivo = analisis["motivo_no_valido"] or "El mensaje no fue identificado como un requerimiento válido."
        return {
            "generar_ticket": False,
            "prioridad": None,
            "motivo_prioridad": "No aplica",
            "requiere_revision_manual": True,
            "estado_ticket": "No generado",
            "observaciones_reglas": [motivo]
        }

    observaciones = []

    if not informacion_suficiente:
        tienda_ok = tienda.lower() not in ["", "no identificado", "no identificado."]
        ubicacion_ok = ubicacion.lower() not in ["", "no identificada", "no identificada."]
        descripcion_ok = descripcion.lower() not in ["", "no identificado", "no identificado."]

        if not (tienda_ok and ubicacion_ok and descripcion_ok):
            observaciones.append("Falta información mínima para registrar el ticket.")
            for campo in campos_faltantes:
                observaciones.append(f"Falta: {campo}")

            return {
                "generar_ticket": False,
                "prioridad": None,
                "motivo_prioridad": "No se pudo asignar prioridad porque la información es incompleta.",
                "requiere_revision_manual": True,
                "estado_ticket": "No generado",
                "observaciones_reglas": observaciones
            }

    if tienda.lower() in ["", "no identificado", "no identificado."]:
        revision_manual = True
        observaciones.append("No se identificó claramente la tienda.")

    if ubicacion.lower() in ["", "no identificada", "no identificada."]:
        revision_manual = True
        observaciones.append("No se identificó claramente la ubicación dentro de la tienda.")

    if descripcion.lower() in ["", "no identificado", "no identificado."]:
        revision_manual = True
        observaciones.append("La descripción del problema no quedó clara.")

    if observaciones:
        return {
            "generar_ticket": False,
            "prioridad": None,
            "motivo_prioridad": "No se pudo asignar prioridad porque la información todavía requiere precisión.",
            "requiere_revision_manual": True,
            "estado_ticket": "No generado",
            "observaciones_reglas": observaciones
        }

    if riesgo_seguridad:
        prioridad = "Alta"
        motivo_prioridad = "Se detectó un posible riesgo de seguridad."
    elif impacto == "Alto":
        prioridad = "Alta"
        motivo_prioridad = "El impacto operativo fue evaluado como alto."
    elif afecta_cliente and impacto in ["Medio", "Alto"]:
        prioridad = "Alta"
        motivo_prioridad = "El requerimiento afecta la atención al cliente."
    elif impacto == "Medio":
        prioridad = "Media"
        motivo_prioridad = "El impacto operativo fue evaluado como medio."
    elif impacto == "Bajo":
        prioridad = "Baja"
        motivo_prioridad = "El impacto operativo fue evaluado como bajo."
    else:
        prioridad = "Media"
        motivo_prioridad = "No hubo claridad total en el impacto, por lo que se asigna prioridad media por precaución."

    estado = "Pendiente de validación" if revision_manual else "Registrado"

    return {
        "generar_ticket": True,
        "prioridad": prioridad,
        "motivo_prioridad": motivo_prioridad,
        "requiere_revision_manual": revision_manual,
        "estado_ticket": estado,
        "observaciones_reglas": observaciones,
    }


def generar_ticket() -> str:
    ahora = datetime.now()
    return f"TK-{ahora.strftime('%Y%m%d-%H%M%S')}"


def construir_respuesta_natural(analisis: dict, decision: dict, ticket: str | None) -> str:
    if not decision["generar_ticket"]:
        faltantes = analisis.get("campos_faltantes", [])
        detalle_faltantes = "\n".join([f"- {x}" for x in faltantes]) if faltantes else "- más detalle del problema"

        observaciones = decision.get("observaciones_reglas", [])
        detalle_obs = "\n".join([f"- {x}" for x in observaciones]) if observaciones else ""

        return f"""Puedo ayudarte a registrar un requerimiento, pero todavía no tengo información suficiente para generar el ticket.

{detalle_obs}

Por favor, indícame como mínimo:
{detalle_faltantes}

Ejemplo:
En la tienda San Isidro, el congelador del área de carnes no enfría bien y podría comprometer el producto."""

    lineas = [
        "He procesado tu requerimiento.",
        "",
        f"Tienda: {analisis['tienda']}",
        f"Tipo de requerimiento: {analisis['tipo_requerimiento']}",
        f"Subtipo: {analisis['subtipo']}",
        f"Activo o elemento afectado: {analisis['activo']}",
        f"Ubicación dentro de la tienda: {analisis['ubicacion']}",
        f"Descripción del problema: {analisis['descripcion_problema']}",
        f"Prioridad asignada: {decision['prioridad']}",
        f"Estado: {decision['estado_ticket']}",
        "",
        f"Sustento del análisis: {analisis['justificacion']}",
        f"Sustento de prioridad: {decision['motivo_prioridad']}",
        "",
        f"Nro. de ticket: {ticket}"
    ]

    return "\n".join(lineas)


def insertar_ticket_bigquery(row: dict):
    table_id = f"{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"
    errors = bq_client.insert_rows_json(table_id, [row])
    if errors:
        raise Exception(f"Error insertando en BigQuery: {errors}")


def procesar_requerimiento(
    mensaje: str,
    usuario: str,
    canal: str,
    tipo_entrada: str = "texto",
    transcripcion: str | None = None,
    audio_nombre: str | None = None,
    audio_mime: str | None = None,
) -> dict:
    inicio = time.time()

    log_event({
        "event": "request_received",
        "instance_uuid": INSTANCE_UUID,
        "hostname": HOSTNAME,
        "usuario": usuario,
        "canal": canal,
        "tipo_entrada": tipo_entrada,
        "message": mensaje,
        "audio_nombre": audio_nombre,
        "audio_mime": audio_mime,
    })

    analisis = interpretar_requerimiento_con_gemini(mensaje)
    decision = evaluar_reglas(analisis)

    ticket = None
    if decision["generar_ticket"]:
        ticket = generar_ticket()

    respuesta = construir_respuesta_natural(analisis, decision, ticket)

    fracttal_ok = False
    fracttal_message = None
    fracttal_url_final = None

    if decision["generar_ticket"]:
        fracttal_result = navigate_to_work_requests()
        fracttal_ok = bool(fracttal_result.get("ok"))
        fracttal_message = fracttal_result.get("message")
        fracttal_url_final = fracttal_result.get("url_final")

        if fracttal_ok:
            respuesta += (
                "\n\nEstado de integración Fracttal: Conexión exitosa hasta Work Requests."
                f"\nURL final: {fracttal_url_final}"
            )
        else:
            respuesta += (
                "\n\nEstado de integración Fracttal: No se pudo completar la navegación automática."
                f"\nDetalle: {fracttal_message}"
            )

    if decision["generar_ticket"]:
        row = {
            "ticket_id": ticket,
            "fecha_creacion": datetime.utcnow().isoformat(),
            "usuario": usuario,
            "tienda": analisis["tienda"],
            "mensaje_original": mensaje,
            "tipo_requerimiento": analisis["tipo_requerimiento"],
            "subtipo": analisis["subtipo"],
            "activo": analisis["activo"],
            "ubicacion": analisis["ubicacion"],
            "descripcion_problema": analisis["descripcion_problema"],
            "impacto_operativo": analisis["impacto_operativo"],
            "afecta_cliente": analisis["afecta_cliente"],
            "riesgo_seguridad": analisis["riesgo_seguridad"],
            "requiere_revision_manual": decision["requiere_revision_manual"],
            "prioridad": decision["prioridad"],
            "estado_ticket": decision["estado_ticket"],
            "justificacion_modelo": analisis["justificacion"],
            "motivo_prioridad": decision["motivo_prioridad"],
            "canal": canal,
            "instance_uuid": INSTANCE_UUID,
            "hostname": HOSTNAME
        }

        insertar_ticket_bigquery(row)

    duracion = round(time.time() - inicio, 3)

    log_event({
        "event": "request_processed",
        "instance_uuid": INSTANCE_UUID,
        "hostname": HOSTNAME,
        "ticket_id": ticket,
        "duration_seconds": duracion,
        "generar_ticket": decision["generar_ticket"],
        "tipo_entrada": tipo_entrada,
        "fracttal_ok": fracttal_ok,
        "fracttal_url_final": fracttal_url_final,
    })

    return {
        "ticket_id": ticket,
        "tipo_requerimiento": analisis["tipo_requerimiento"],
        "subtipo": analisis["subtipo"],
        "prioridad": decision["prioridad"],
        "activo": analisis["activo"],
        "tienda": analisis["tienda"],
        "ubicacion": analisis["ubicacion"],
        "estado_ticket": decision["estado_ticket"],
        "respuesta_texto": respuesta,
        "generar_ticket": decision["generar_ticket"],
        "tipo_entrada": tipo_entrada,
        "transcripcion": transcripcion if tipo_entrada == "audio" else None,
        "fracttal_ok": fracttal_ok,
        "fracttal_message": fracttal_message,
        "fracttal_url_final": fracttal_url_final,
    }


# =========================
# Rutas
# =========================
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "instance_uuid": INSTANCE_UUID,
        "hostname": HOSTNAME
    })


@app.post("/process-message")
def process_message():
    try:
        data = request.get_json(silent=True) or {}

        mensaje = (data.get("message") or "").strip()
        usuario = (data.get("usuario") or "supervisor_demo").strip()
        canal = (data.get("canal") or "web_chat").strip()

        if not mensaje:
            return jsonify({"error": "message es obligatorio"}), 400

        result = procesar_requerimiento(
            mensaje=mensaje,
            usuario=usuario,
            canal=canal,
            tipo_entrada="texto",
        )
        return jsonify(result)

    except Exception as e:
        log_event({
            "event": "request_error",
            "instance_uuid": INSTANCE_UUID,
            "hostname": HOSTNAME,
            "error": str(e)
        })
        return jsonify({"error": str(e)}), 500


@app.post("/process-audio")
def process_audio():
    try:
        usuario = (request.form.get("usuario") or "supervisor_demo").strip()
        canal = (request.form.get("canal") or "web_chat_audio").strip()
        audio = request.files.get("audio")

        if not audio:
            return jsonify({"error": "audio es obligatorio"}), 400

        audio_nombre = (audio.filename or "audio_sin_nombre").strip()
        audio_mime = (audio.mimetype or "").strip().lower()

        if audio_mime not in SUPPORTED_AUDIO_MIME_TYPES:
            return jsonify({
                "error": f"Formato de audio no soportado: {audio_mime or 'desconocido'}"
            }), 400

        audio_bytes = audio.read()

        if not audio_bytes:
            return jsonify({"error": "El archivo de audio está vacío"}), 400

        if len(audio_bytes) > MAX_AUDIO_BYTES:
            return jsonify({
                "error": f"El audio excede el tamaño máximo permitido de {MAX_AUDIO_BYTES // (1024 * 1024)} MB"
            }), 400

        transcripcion = transcribir_audio_con_gemini(audio_bytes, audio_mime)
        transcripcion = (transcripcion or "").strip()

        if not transcripcion:
            return jsonify({"error": "No se pudo transcribir el audio con claridad"}), 400

        result = procesar_requerimiento(
            mensaje=transcripcion,
            usuario=usuario,
            canal=canal,
            tipo_entrada="audio",
            transcripcion=transcripcion,
            audio_nombre=audio_nombre,
            audio_mime=audio_mime,
        )

        return jsonify(result)

    except Exception as e:
        log_event({
            "event": "audio_request_error",
            "instance_uuid": INSTANCE_UUID,
            "hostname": HOSTNAME,
            "error": str(e)
        })
        return jsonify({"error": str(e)}), 500


@app.post("/fracttal-login")
def fracttal_login_route():
    try:
        result = login_fracttal()
        status_code = 200 if result.get("ok") else 500
        return jsonify(result), status_code
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(error_trace)

        return jsonify({
            "ok": False,
            "message": f"Error ejecutando prueba de login: {str(e)}",
            "trace": error_trace
        }), 500

@app.post("/fracttal-go-to-work-requests")
def fracttal_go_to_work_requests():
    try:
        result = navigate_to_work_requests()
        status_code = 200 if result.get("ok") else 500
        return jsonify(result), status_code
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(error_trace)
        return jsonify({
            "ok": False,
            "message": f"Error ejecutando navegación a Solicitudes de Trabajo: {str(e)}",
            "trace": error_trace
        }), 500