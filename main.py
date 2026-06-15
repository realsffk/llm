import os
import logging
import threading
import httpx
import time
import uuid
import aiosqlite
import asyncio
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from openai import AsyncOpenAI

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
LOFYPAY_SECRET = os.getenv("LOFYPAY_SECRET") # Ex: sk_live_... ou sk_test_...

# Variável global para instruções do sistema
SYSTEM_INSTRUCTION = ""

# Planos configurados
PLANOS = {
    "mensal": {"nome": "Plano Mensal (30 dias)", "valor": 19.90, "dias": 30},
    "trimestre": {"nome": "Plano Trimestral (90 dias)", "valor": 49.90, "dias": 90},
    "vitalicio": {"nome": "Plano Vitalício (Para sempre)", "valor": 149.90, "dias": 36500}
}

# Local do Banco de Dados
DB_PATH = os.getenv("DB_PATH", "database.db")

# Inicialização do Banco de Dados Local (SQLite)
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Tabela de Usuários (assinantes)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                expiration_date TEXT
            )
        ''')
        # Tabela de Transações pendentes (para conciliação)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY,
                user_id INTEGER,
                plano TEXT,
                status TEXT,
                created_at TEXT
            )
        ''')
        await db.commit()

async def is_vip(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT expiration_date FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        return False

    expiration_date = datetime.fromisoformat(row[0])
    return datetime.now() < expiration_date

# 1. Configurando o cliente da OpenAI para a API do OpenRouter
client = None

async def cmd_planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe as opções de planos."""
    keyboard = [
        [InlineKeyboardButton("Plano Mensal - R$ 19,90", callback_data="buy_mensal")],
        [InlineKeyboardButton("Plano Trimestral - R$ 49,90", callback_data="buy_trimestre")],
        [InlineKeyboardButton("Plano Vitalício - R$ 149,90", callback_data="buy_vitalicio")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(
            "Oi! Para conversar comigo sem limites, você precisa de um passe VIP. Escolha um plano abaixo para gerar o seu PIX: 👇",
            reply_markup=reply_markup
        )

async def generate_pix(user_id, plano_key):
    """Gera um PIX na LofyPay."""
    if not LOFYPAY_SECRET:
        logger.error("LOFYPAY_SECRET não está configurado!")
        return None

    plano = PLANOS[plano_key]
    external_ref = f"USR-{user_id}-{int(time.time())}"

    payload = {
        "amount": plano["valor"],
        "method": "pix",
        "external_reference": external_ref,
        "client": {
            "name": f"User {user_id}",
            "document": "00000000000" # Placeholder já que o bot não coleta CPF
        }
    }

    headers = {
        "Authorization": f"Bearer {LOFYPAY_SECRET}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as http_client:
        try:
            res = await http_client.post("https://app.lofypay.com/api/v1/gateway", json=payload, headers=headers)
            res.raise_for_status()
            data = res.json()

            if data.get("status") in ["success", "OK"]:
                transaction_id = data.get("idTransaction")
                payment_code = data.get("paymentCode")

                # Salvar no DB como pendente
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute('''
                        INSERT INTO transactions (transaction_id, user_id, plano, status, created_at)
                        VALUES (?, ?, ?, 'PENDING', ?)
                    ''', (transaction_id, user_id, plano_key, datetime.now().isoformat()))
                    await db.commit()

                return transaction_id, payment_code

        except Exception as e:
            logger.error(f"Erro ao gerar PIX LofyPay: {e}")

    return None

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trata cliques nos botões."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data.startswith("buy_"):
        plano_key = data.split("_")[1]

        await query.edit_message_text(f"Gerando o seu PIX para o {PLANOS[plano_key]['nome']}... Aguarde um segundo ⏳")

        pix_data = await generate_pix(user_id, plano_key)

        if pix_data:
            transaction_id, payment_code = pix_data

            keyboard = [[InlineKeyboardButton("✅ Já paguei! Verificar pagamento", callback_data=f"check_{transaction_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"Aqui está o seu PIX Copia e Cola para o **{PLANOS[plano_key]['nome']}**:\n\n"
                f"`{payment_code}`\n\n"
                f"Copie o código acima, pague no seu banco e depois clique no botão abaixo para eu liberar o seu acesso! 🥰",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            await query.edit_message_text("Poxa, deu um errinho ao gerar seu PIX. Tente novamente mais tarde! 💔")

    elif data.startswith("check_"):
        transaction_id = data.split("_")[1]

        await query.edit_message_text("Estou verificando com o banco... Só um momento! 🕵️‍♀️")

        headers = {
            "Authorization": f"Bearer {LOFYPAY_SECRET}",
            "Content-Type": "application/json"
        }

        payload = {"idtransaction": transaction_id}

        async with httpx.AsyncClient() as http_client:
            try:
                res = await http_client.post("https://app.lofypay.com/api/v1/status", json=payload, headers=headers)
                status_data = res.json()

                if status_data.get("status") == "PAID_OUT":
                    # Processamento atômico do pagamento
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute('SELECT plano, status FROM transactions WHERE transaction_id = ?', (transaction_id,)) as cursor:
                            row = await cursor.fetchone()

                        if not row:
                            await query.edit_message_text("Transação não encontrada no nosso sistema. Se o dinheiro saiu da sua conta, chame o suporte! 💔")
                            return

                        if row[1] != 'PAID':
                            plano_key = row[0]
                            dias = PLANOS[plano_key]["dias"]

                            # Calcula nova data VIP
                            async with db.execute('SELECT expiration_date FROM users WHERE user_id = ?', (user_id,)) as cursor:
                                vip_row = await cursor.fetchone()

                            if vip_row and datetime.now() < datetime.fromisoformat(vip_row[0]):
                                current_exp = datetime.fromisoformat(vip_row[0])
                            else:
                                current_exp = datetime.now()

                            new_exp = current_exp + timedelta(days=dias)

                            # Atualiza a transação e o VIP dentro da mesma conexão e commit
                            await db.execute("UPDATE transactions SET status = 'PAID' WHERE transaction_id = ?", (transaction_id,))

                            await db.execute('''
                                INSERT INTO users (user_id, expiration_date)
                                VALUES (?, ?)
                                ON CONFLICT(user_id)
                                DO UPDATE SET expiration_date = ?
                            ''', (user_id, new_exp.isoformat(), new_exp.isoformat()))

                            await db.commit()

                            await query.edit_message_text(f"Eba!! 🎉 Pagamento do {PLANOS[plano_key]['nome']} confirmado! Seu acesso VIP está liberado. Me manda um 'Oi' para a gente começar a conversar! 🥰")
                        else:
                            await query.edit_message_text("Esse pagamento já foi processado! Você já tem acesso VIP. Pode mandar mensagem! 😘")
                else:
                    keyboard = [[InlineKeyboardButton("🔄 Verificar de novo", callback_data=f"check_{transaction_id}")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text("Hum... O pagamento ainda não confirmou aqui pra mim. Se você já pagou, pode demorar alguns segundinhos. Tente novamente! ⏳", reply_markup=reply_markup)

            except Exception as e:
                logger.error(f"Erro ao verificar PIX: {e}")
                await query.edit_message_text("Deu um erro ao tentar verificar... Me desculpe! Tente de novo. 💔")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    4. Recebe a mensagem do usuário. Se for VIP, conversa com o OpenRouter.
       Se não, mostra a tabela de planos.
    """
    if not update.message or not update.message.text:
        return

    user_id = update.message.from_user.id
    user_text = update.message.text

    # Verifica se tem assinatura ativa
    is_user_vip = await is_vip(user_id)
    if not is_user_vip:
        await cmd_planos(update, context)
        return

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

def main():
    # Inicializa o DB usando asyncio (já que a função é async)
    asyncio.run(init_db())

    # Referencia as variáveis globais
    global client
    global SYSTEM_INSTRUCTION

    # Verifica se as variáveis de ambiente necessárias estão configuradas
    if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY or not MODEL_NAME:
        logger.error("Faltam variáveis de ambiente. Certifique-se de definir TELEGRAM_TOKEN, OPENROUTER_API_KEY e MODEL_NAME.")
    else:
        # Inicializa o cliente da OpenAI depois de verificar as credenciais
        client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )

        # 3. Carrega instruções do sistema de um arquivo Markdown
        try:
            with open("instructions.md", "r", encoding="utf-8") as f:
                SYSTEM_INSTRUCTION = f.read()
            logger.info("Instruções do sistema carregadas de instructions.md")
        except FileNotFoundError:
            logger.warning("Arquivo instructions.md não encontrado. O bot rodará sem instruções de sistema.")
        except Exception as e:
            logger.error(f"Erro ao ler instructions.md: {e}")

        logger.info("Iniciando o bot...")
        application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

        # Adiciona o handler para comandos e mensagens
        application.add_handler(CommandHandler("planos", cmd_planos))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        # Servidor web dummy para o Render Web Service (Free Tier)
        class DummyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Bot is running!")

        def run_dummy_server():
            port = int(os.environ.get("PORT", 10000))
            server = HTTPServer(("0.0.0.0", port), DummyHandler)
            logger.info(f"Dummy web server rodando na porta {port} (para manter o Render Web Service Free ativo)...")
            server.serve_forever()

        # Inicia o servidor web em uma thread separada
        server_thread = threading.Thread(target=run_dummy_server, daemon=True)
        server_thread.start()

        logger.warning(
            "⚠️ AVISO PARA DEPLOY NO RENDER FREE: O Render apaga os arquivos locais a cada restart. "
            "Para não perder os usuários VIP, você precisa configurar um Banco de Dados Remoto, "
            "ou adicionar um 'Disk' persistente no Render e apontar a variável DB_PATH para ele."
        )

        logger.info("Bot está rodando em modo polling (Web Service).")
        application.run_polling()

if __name__ == '__main__':
    main()
