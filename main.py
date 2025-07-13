import logging
import os
import json
import httpx
import re

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

import firebase_admin
from firebase_admin import credentials, firestore

# --- 1. CONFIGURACIÓN INICIAL ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FIREBASE_APP_ID = os.getenv("FIREBASE_APP_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") # Se usará solo en el servidor

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

try:
    cred = credentials.Certificate("firebase-credentials.json")
    firebase_app = firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Firebase conectado exitosamente.")
except Exception as e:
    logger.error(f"Error al conectar con Firebase: {e}. El bot funcionará sin base de datos.")
    db = None

# --- 2. CONSTANTES Y CONTENIDO ---
BOT_NAME = "Mentor Matemático"
EXERCISE_TOPICS = [
    'Polinomios', 'Funciones trigonométricas', 'Funciones exponenciales',
    'Funciones logarítmicas', 'Regla de la cadena', 'Límites',
    'Funciones Hiperbólicas', 'Regla del Producto', 'Regla del cociente'
]

CUADERNO_CONTEXT = """
Eres un tutor experto en Cálculo Diferencial y estás ayudando a un estudiante a completar su "Cuaderno Digital: Mentor matemático con IA".
Tu objetivo es responder sus dudas basándote en la estructura y contenido de este cuaderno.

**Descripción General del Cuaderno:**
El cuaderno busca que el estudiante comprenda y aplique el concepto de derivada a través de ejercicios y reflexión, usando una IA como herramienta. Está estructurado en tres bloques.

**Bloque 1: Concepto Matemático de Derivada**
- **Objetivo:** Comprender la derivada gráfica, numérica y algebraicamente.
- **Dudas Frecuentes:**
    - Sobre la pendiente en f(x)=x^2: La pendiente (inclinación) es negativa para x<0, cero en x=0, y positiva para x>0.
    - Sobre el cociente de diferencias: Es una aproximación a la pendiente de la recta tangente. A medida que h→0, se acerca a la derivada.
    - Sobre rectas secante y tangente: La secante corta en dos puntos, la tangente en uno. La tangente es el límite de la secante cuando los dos puntos se unen.
    - Sobre comparar el límite y el valor exacto en f(x)=√x: El valor exacto se calcula con la regla de la potencia. El valor del límite de la tabla es una aproximación numérica; pueden existir pequeñas diferencias.

**Bloque 2: Ejercicios de Derivación**
- **Objetivo:** Aplicar reglas básicas de derivación.
- **Dudas Frecuentes:**
    - Para f(x)=(x²+1)/x: Se puede simplificar a f(x) = x + x⁻¹ y usar la regla de la potencia, o usar la regla del cociente directamente.
    - Para f(x)=e^x ⋅ cos(x): Se usa la regla del producto (u'v + uv').
    - Error común: La derivada de un producto NO es el producto de las derivadas.

**Bloque 3: Aplicaciones de la Derivada**
- **Objetivo:** Usar la derivada para resolver problemas de optimización y análisis.
- **Dudas Frecuentes:**
    - Problema de la lata cilíndrica: Se busca minimizar el área superficial (material) para un volumen fijo. Se usan las fórmulas de área y volumen del cilindro.
    - Rol de la derivada en optimización: Ayuda a encontrar puntos críticos (donde f'(x)=0), que son candidatos a ser máximos o mínimos.
    - Intervalos de crecimiento/decrecimiento: Se encuentra f'(x), se iguala a cero para hallar puntos críticos. El signo de f'(x) en los intervalos resultantes determina si la función crece (f'>0) o decrece (f'<0).
    - Descripción de máximos y mínimos: Un máximo relativo ocurre si la función cambia de creciente a decreciente. Un mínimo, si cambia de decreciente a creciente.
"""

MAIN_MENU_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("« Volver al Menú Principal", callback_data="main_menu")
]])

# --- 3. FUNCIONES AUXILIARES ---
async def call_gemini_api(prompt: str, is_structured: bool = False, schema: dict = None) -> str | dict | None:
    if not GEMINI_API_KEY:
        logger.error("No se encontró la clave de API de Gemini.")
        return "Error: La conexión con la IA no está configurada."
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if is_structured and schema:
        payload["generationConfig"] = {"responseMimeType": "application/json", "responseSchema": schema}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(api_url, json=payload, timeout=60.0)
            response.raise_for_status()
            result = response.json()
        if text_content := result.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text"):
            return json.loads(text_content) if is_structured else text_content
        else:
            logger.error(f"Respuesta inesperada de Gemini: {result}")
            return "Lo siento, no pude generar una respuesta en este momento."
    except httpx.HTTPStatusError as e:
        logger.error(f"Error en la API de Gemini (HTTP): {e.response.text}")
        return "Hubo un problema con la IA. Inténtalo de nuevo más tarde."
    except Exception as e:
        logger.error(f"Error al llamar a la API de Gemini: {e}")
        return "Hubo un problema conectando con la IA. Inténtalo de nuevo."

async def get_user_state(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> dict:
    if not db: 
        if not context.user_data:
            context.user_data.update({"bot_state": "idle", "last_asesoria_topic": None, "current_exercise": None})
        return context.user_data
    doc_ref = db.collection("user_states").document(str(user_id))
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()
    return {"bot_state": "idle", "last_asesoria_topic": None, "current_exercise": None}

async def set_user_state(user_id: int, state: dict, context: ContextTypes.DEFAULT_TYPE):
    if not db:
        context.user_data.update(state)
        return
    doc_ref = db.collection("user_states").document(str(user_id))
    doc_ref.set(state)

async def save_interaction(user_id: int, interaction_type: str, data: dict):
    if not db or not FIREBASE_APP_ID: return
    collection_path = f"artifacts/{FIREBASE_APP_ID}/users/{user_id}/bot_interactions"
    interactions_collection = db.collection(collection_path)
    interaction_data = {"type": interaction_type, "userId": user_id, "timestamp": firestore.SERVER_TIMESTAMP, **data}
    interactions_collection.add(interaction_data)
    logger.info(f"Interacción '{interaction_type}' guardada para el usuario {user_id}")

async def send_long_message(message_or_query, text: str):
    MAX_LENGTH = 4096
    reply_method = message_or_query.message.reply_text if hasattr(message_or_query, 'message') else message_or_query.reply_text
    if len(text) <= MAX_LENGTH:
        await reply_method(text)
        return
    parts = []
    current_part = ""
    for line in text.split('\n'):
        if len(current_part) + len(line) + 1 > MAX_LENGTH:
            parts.append(current_part)
            current_part = line
        else:
            current_part += '\n' + line
    parts.append(current_part)
    for part in parts:
        if part.strip():
            await reply_method(part.strip())

async def generate_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict):
    topic = state['current_exercise']['topic']
    difficulty = state['current_exercise']['difficulty']
    schema = {"type": "OBJECT", "properties": {"problem": {"type": "STRING"}, "solution": {"type": "STRING"}}}
    prompt = f"Crea un ejercicio de Cálculo Diferencial sobre '{topic}' con dificultad {difficulty}/5. Devuelve JSON con claves 'problem' y 'solution'. Usa texto plano."
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    exercise_data = await call_gemini_api(prompt, is_structured=True, schema=schema)
    if exercise_data and "problem" in exercise_data:
        state["bot_state"] = "waiting_for_exercise_answer"
        state["current_exercise"].update(exercise_data)
        await send_long_message(update.effective_message, f"Aquí tienes:\n\n{exercise_data['problem']}\n\nEscribe tu respuesta.")
    else:
        await update.effective_message.reply_text("No pude generar un ejercicio. Inténtalo de nuevo con /prueba.")
        state["bot_state"] = "idle"
    await set_user_state(update.effective_user.id, state, context)


# --- 4. MANEJADORES DE COMANDOS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await set_user_state(user.id, {"bot_state": "idle", "last_asesoria_topic": None, "current_exercise": None}, context)
    welcome_text = (
        "¡Hola! Soy tu compañero de estudio para Cálculo Diferencial. Tengo las siguientes funciones:\n\n"
        "📚 `/asesoria`: Dudas específicas sobre temas.\n"
        "💡 `/ejemplo`: Un ejemplo práctico de un tema.\n"
        "🧠 `/prueba`: Pon a prueba tus conocimientos.\n"
        "📖 `/dudas`: Apoyo para el Cuaderno Digital.\n"
        "📊 `/encuesta`: Ayúdame a mejorar.\n\n"
        "Estoy para ayudarte."
    )
    await update.message.reply_text(welcome_text)

async def asesoria_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = await get_user_state(update.effective_user.id, context)
    state["bot_state"] = "waiting_for_doubt"
    await set_user_state(update.effective_user.id, state, context)
    await update.message.reply_text("Por favor, dime tu duda y el nivel de dificultad. \nEj: '¿Qué es la derivada? nivel Fácil'")

async def ejemplo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = await get_user_state(update.effective_user.id, context)
    state["bot_state"] = "waiting_for_example_topic"
    await set_user_state(update.effective_user.id, state, context)
    await update.message.reply_text("¡Claro! ¿Sobre qué tema de Cálculo Diferencial te gustaría un ejemplo?")

async def prueba_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = await get_user_state(update.effective_user.id, context)
    state["bot_state"] = "waiting_for_exercise_topic"
    await set_user_state(update.effective_user.id, state, context)
    keyboard = [[InlineKeyboardButton(topic, callback_data=f"topic_{topic}")] for topic in EXERCISE_TOPICS]
    await update.message.reply_text("¡Excelente! Elige el tema:", reply_markup=InlineKeyboardMarkup(keyboard))

async def dudas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = await get_user_state(update.effective_user.id, context)
    state["bot_state"] = "waiting_for_duda_cuaderno"
    await set_user_state(update.effective_user.id, state, context)
    await update.message.reply_text(
        "Has entrado a la sección de ayuda para el Cuaderno Digital.\n\n"
        "Por favor, escribe tu pregunta sobre cualquier parte del cuaderno y te ayudaré a resolverla."
    )

async def encuesta_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Gracias por ayudarme a mejorar! Tu opinión es muy valiosa.\n\n"
        "Por favor, completa la siguiente encuesta:\nhttps://forms.gle/dtyB5o2FncCA7zMy7"
    )
    await save_interaction(update.effective_user.id, 'encuesta_link_sent', {'link': 'https://forms.gle/dtyB5o2FncCA7zMy7'})

# --- 5. MANEJADOR DE MENSAJES DE TEXTO ---
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    state = await get_user_state(user_id, context)
    bot_state = state.get("bot_state", "idle")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    if bot_state == "waiting_for_duda_cuaderno":
        prompt = f"{CUADERNO_CONTEXT}\n\n---\n\nBasado en el contexto anterior del 'Cuaderno Digital', responde la siguiente duda del estudiante de la manera más clara y útil posible:\n\nPREGUNTA DEL ESTUDIANTE: '{text}'"
        bot_response = await call_gemini_api(prompt)
        await send_long_message(update.message, bot_response)
        await save_interaction(user_id, 'duda_cuaderno', {'pregunta': text, 'respuesta': bot_response})
        await update.message.reply_text("¿Tienes alguna otra duda sobre el cuaderno?", reply_markup=MAIN_MENU_KEYBOARD)
        
    elif bot_state == "waiting_for_doubt":
        match = re.search(r'(.+)\s+nivel\s*(fácil|intermedio|avanzado)', text, re.IGNORECASE)
        if match:
            doubt, difficulty = match.groups()
            difficulty_map = {'fácil': 'básico', 'intermedio': 'detallado', 'avanzado': 'experto'}
            prompt = f'Eres un tutor experto en Cálculo Diferencial. Explica: "{doubt.strip()}". Nivel: {difficulty_map[difficulty.lower()]}. Usa texto plano (ej: x^2).'
            bot_response = await call_gemini_api(prompt)
            await send_long_message(update.message, bot_response)
            await save_interaction(user_id, 'asesoria', {'query': doubt, 'difficulty': difficulty, 'response': bot_response})
            state.update({"bot_state": "waiting_for_deepen_topic", "last_asesoria_topic": doubt})
            keyboard = [[InlineKeyboardButton("No, gracias", callback_data="deepen_no")]]
            await update.message.reply_text("¿Quieres profundizar en algo más?", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text("Formato incorrecto. Ejemplo: '¿Qué es la derivada? nivel Fácil'")
            
    elif bot_state == "waiting_for_deepen_topic":
        prompt = f'Eres un tutor experto. Profundiza en "{text.strip()}" en el contexto de "{state.get("last_asesoria_topic", "Cálculo Diferencial")}".'
        bot_response = await call_gemini_api(prompt)
        await send_long_message(update.message, bot_response)
        await save_interaction(user_id, 'profundizar_asesoria', {'original': state.get("last_asesoria_topic"), 'deepen': text.strip(), 'response': bot_response})
        keyboard = [[InlineKeyboardButton("No, gracias", callback_data="deepen_no")]]
        await update.message.reply_text("¿Quieres profundizar en algo más?", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif bot_state == "waiting_for_example_topic":
        prompt = f'Proporciona un ejemplo práctico y resuelto sobre "{text.strip()}" en Cálculo Diferencial. Explícalo paso a paso. Usa texto plano.'
        bot_response = await call_gemini_api(prompt)
        await send_long_message(update.message, bot_response)
        await save_interaction(user_id, 'ejemplo', {'topic': text.strip(), 'response': bot_response})
        state["bot_state"] = "idle"
        await update.message.reply_text("Espero que el ejemplo haya sido útil.", reply_markup=MAIN_MENU_KEYBOARD)

    elif bot_state == "waiting_for_exercise_answer":
        exercise = state.get("current_exercise", {})
        prompt = f"""Evalúa esta respuesta de un estudiante de Cálculo.
        - Problema: "{exercise.get('problem')}"
        - Solución Correcta: "{exercise.get('solution')}"
        - Respuesta del Estudiante: "{text.strip()}"
        Si es correcta, felicita y ASEGÚRATE de incluir la palabra "correcto". Si no, da una pista SIN revelar la solución. Usa texto plano."""
        verification = await call_gemini_api(prompt)
        await send_long_message(update.message, verification)
        await save_interaction(user_id, 'verificacion_ejercicio', {**exercise, 'user_answer': text.strip(), 'verification': verification})
        positive_keywords = ['correcto', 'exacto', 'perfecto', 'muy bien', 'excelente', 'felicidades']
        if any(keyword in verification.lower() for keyword in positive_keywords):
            keyboard = [[InlineKeyboardButton("Otro ejercicio similar", callback_data="next_action_similar")], [InlineKeyboardButton("Regresar al menú principal", callback_data="main_menu")]]
            await update.message.reply_text("¿Qué te gustaría hacer ahora?", reply_markup=InlineKeyboardMarkup(keyboard))
            state["bot_state"] = "waiting_for_next_action"
        else:
            keyboard = [[InlineKeyboardButton("Intentar de nuevo", callback_data="resolution_retry")], [InlineKeyboardButton("Ver la solución", callback_data="resolution_solve")]]
            await update.message.reply_text("¿Qué quieres hacer?", reply_markup=InlineKeyboardMarkup(keyboard))
            state["bot_state"] = "waiting_for_exercise_resolution"

    await set_user_state(user_id, state, context)

# --- 6. MANEJADOR DE CLICS EN BOTONES ---
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    state = await get_user_state(user_id, context)
    action = query.data

    if action.startswith("topic_"):
        topic = action.split("_", 1)[1]
        state.update({"bot_state": "waiting_for_exercise_difficulty", "current_exercise": {"topic": topic}})
        keyboard = [[InlineKeyboardButton(str(i), callback_data=f"diff_{i}") for i in range(1, 6)]]
        await query.edit_message_text(f"Tema: {topic}. Elige dificultad:", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif action.startswith("diff_"):
        difficulty = int(action.split("_", 1)[1])
        state["current_exercise"]["difficulty"] = difficulty
        await query.edit_message_text(f"OK. Generando ejercicio de {state['current_exercise']['topic']} (Nivel {difficulty})...")
        await generate_exercise(update, context, state)

    elif action == "deepen_no" or action == "main_menu":
        state.update({"bot_state": "idle", "last_asesoria_topic": None})
        await query.edit_message_text("De acuerdo, volviendo al menú principal. Usa /start para ver las opciones.")
    
    elif action == "next_action_similar":
        await query.edit_message_text("¡Perfecto! Generando otro ejercicio...")
        await generate_exercise(update, context, state) 

    elif action == "resolution_retry":
        state["bot_state"] = "waiting_for_exercise_answer"
        await query.edit_message_text("¡Claro! Tómate tu tiempo y escribe tu nueva respuesta.")
        
    elif action == "resolution_solve":
        state["bot_state"] = "idle"
        solution = state.get("current_exercise", {}).get("solution", "No se encontró la solución.")
        await query.edit_message_text(f"La solución es:\n\n{solution}", reply_markup=MAIN_MENU_KEYBOARD)

    await set_user_state(user_id, state, context)

# --- 7. MANEJADOR DE ERRORES ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción al manejar una actualización:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Lo siento, ocurrió un error inesperado. He notificado a mi desarrollador.")
        except Exception as e:
            logger.error(f"No se pudo enviar el mensaje de error al usuario: {e}")

# --- 8. FUNCIÓN PRINCIPAL ---
def main():
    """Configura y ejecuta el bot, adaptándose al entorno."""
    if not TELEGRAM_TOKEN:
        logger.critical("No se encontró el TELEGRAM_TOKEN. El bot no puede iniciar.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Registra todos los manejadores
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("asesoria", asesoria_command))
    application.add_handler(CommandHandler("ejemplo", ejemplo_command))
    application.add_handler(CommandHandler("prueba", prueba_command))
    application.add_handler(CommandHandler("dudas", dudas_command)) 
    application.add_handler(CommandHandler("encuesta", encuesta_command))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_error_handler(error_handler)

    # Elige el modo de ejecución basado en el entorno
    if WEBHOOK_URL:
        # Modo Webhook (para el servidor de Render)
        port = int(os.environ.get("PORT", 8443))
        logger.info(f"Iniciando bot en modo WEBHOOK en el puerto {port}...")
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}"
        )
    else:
        # Modo Polling (para pruebas locales)
        logger.warning("No se encontró WEBHOOK_URL. Iniciando bot en modo POLLING para pruebas locales.")
        application.run_polling(drop_pending_updates=True)
#agregar comentario
if __name__ == "__main__":
    main()