import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from openai import AsyncOpenAI

# 3. Variável global para instruções do sistema
SYSTEM_INSTRUCTION = ""

# 5. Configuração de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 2. Puxando as credenciais de forma segura
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME")

# 1. Configurando o cliente da OpenAI para a API do OpenRouter
client = None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    4. Recebe a mensagem do usuário, envia para o OpenRouter com o contexto
       da SYSTEM_INSTRUCTION e devolve a resposta.
    """
    if not update.message or not update.message.text:
        return

    user_text = update.message.text

    try:
        messages = []
        if SYSTEM_INSTRUCTION:
            messages.append({"role": "system", "content": SYSTEM_INSTRUCTION})

        messages.append({"role": "user", "content": user_text})

        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages
        )

        ai_response = response.choices[0].message.content

        # Devolve a resposta idêntica usando parse_mode="Markdown"
        await update.message.reply_text(text=ai_response, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Erro ao comunicar com OpenRouter ou enviar mensagem: {e}")

if __name__ == '__main__':
    # Verifica se as variáveis de ambiente necessárias estão configuradas
    if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY or not MODEL_NAME:
        logger.error("Faltam variáveis de ambiente. Certifique-se de definir TELEGRAM_TOKEN, OPENROUTER_API_KEY e MODEL_NAME.")
    else:
        # Inicializa o cliente da OpenAI depois de verificar as credenciais
        client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )

        logger.info("Iniciando o bot...")
        application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

        # Adiciona o handler para todas as mensagens de texto
        application.add_handler(MessageHandler(filters.TEXT, handle_message))

        logger.info("Bot está rodando em modo polling (Background Worker).")
        application.run_polling()
