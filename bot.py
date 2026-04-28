import os
import json
import base64
import logging
import httpx
import asyncio
from datetime import datetime
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials
import gspread

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config via env vars ──────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY    = os.environ["GEMINI_API_KEY"]
SPREADSHEET_ID    = os.environ["SPREADSHEET_ID"]
SHEET_NAME        = os.environ.get("SHEET_NAME", "Lançamentos")
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent?key=" + "{key}"
)

# ── Google Sheets ────────────────────────────────────────────────────────────
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(SHEET_NAME, rows=1000, cols=10)
        ws.append_row([
            "Nº", "Data", "Destinatário", "CNPJ/CPF",
            "Descrição", "Pagador", "Instituição",
            "Valor (R$)", "Acumulado (R$)", "Registrado em"
        ])
    return ws


def next_row_number(ws):
    values = ws.col_values(1)
    nums = [v for v in values[1:] if str(v).strip().isdigit()]
    return len(nums) + 1


def append_transaction(data: dict):
    ws = get_sheet()
    n = next_row_number(ws)
    sheet_row = n + 1  # +1 por causa do cabeçalho
    accum_formula = f"=SUM($H$2:H{sheet_row})"
    row = [
        n,
        data.get("data", ""),
        data.get("destinatario", ""),
        data.get("cnpj_cpf", "—"),
        data.get("descricao", ""),
        data.get("pagador", ""),
        data.get("instituicao", ""),
        data.get("valor", 0),
        accum_formula,
        datetime.now().strftime("%d/%m/%Y %H:%M"),
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return n, data.get("valor", 0)


# ── Gemini extraction ────────────────────────────────────────────────────────
PROMPT = """Você é um assistente que extrai dados de comprovantes de transferência bancária brasileiros.
Analise a imagem e retorne APENAS um JSON válido com os campos abaixo. Sem texto extra, sem markdown.
{
  "data": "DD/MM/AAAA",
  "destinatario": "Nome completo do destinatário",
  "cnpj_cpf": "CNPJ ou CPF (ou — se não informado)",
  "descricao": "Descrição ou finalidade (campo Descrição do comprovante, ou vazio se não houver)",
  "pagador": "Nome do pagador/remetente",
  "instituicao": "Banco ou instituição do destinatário",
  "valor": 0.00
}
Use ponto como separador decimal no valor. Se não conseguir extrair algum campo use string vazia ou 0."""


async def extract_with_gemini(image_bytes: bytes, mime_type: str) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode()
    url = GEMINI_URL.format(key=GEMINI_API_KEY)
    payload = {
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": mime_type, "data": b64}}
            ]
        }],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 512}
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())


# ── Telegram handlers ────────────────────────────────────────────────────────
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    bot: Bot = context.bot

    file_obj  = None
    mime_type = None

    if msg.photo:
        file_obj  = await bot.get_file(msg.photo[-1].file_id)
        mime_type = "image/jpeg"
    elif msg.document:
        doc = msg.document
        if doc.mime_type in ("image/jpeg", "image/png", "image/webp"):
            file_obj  = await bot.get_file(doc.file_id)
            mime_type = doc.mime_type
        elif doc.mime_type == "application/pdf":
            file_obj  = await bot.get_file(doc.file_id)
            mime_type = "application/pdf"
        else:
            return

    if not file_obj:
        return

    processing = await msg.reply_text("⏳ Lendo comprovante, aguarde...")

    try:
        file_bytes = bytes(await file_obj.download_as_bytearray())

        # Gemini não suporta PDF diretamente via inline — converte para JPEG
        if mime_type == "application/pdf":
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                page = doc[0]
                pix = page.get_pixmap(dpi=150)
                file_bytes = pix.tobytes("jpeg")
                mime_type  = "image/jpeg"
            except Exception:
                await processing.edit_text("❌ Não consegui ler o PDF. Tente enviar como imagem (foto ou screenshot).")
                return

        data = await extract_with_gemini(file_bytes, mime_type)
        n, valor = append_transaction(data)

        # Formatar valor em padrão brasileiro
        valor_fmt = f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

        reply = (
            f"✅ *Comprovante #{n} registrado!*\n\n"
            f"📅 Data: {data.get('data', '—')}\n"
            f"👤 Destinatário: {data.get('destinatario', '—')}\n"
            f"🏦 Instituição: {data.get('instituicao', '—')}\n"
            f"💰 Valor: *{valor_fmt}*\n"
            f"📝 Descrição: {data.get('descricao', '—') or '—'}\n"
            f"👤 Pagador: {data.get('pagador', '—')}"
        )
        await processing.edit_text(reply, parse_mode="Markdown")

    except Exception as e:
        log.exception("Erro ao processar comprovante")
        await processing.edit_text(
            f"❌ Erro ao processar o comprovante.\n\nDetalhe: {str(e)[:200]}"
        )


async def handle_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.effective_message
    text = (msg.text or "").strip().lower()
    if "/start" in text or "/help" in text or "/ajuda" in text:
        await msg.reply_text(
            "🤖 *Bot de Comprovantes – Casa Quixadá*\n\n"
            "Envie uma *foto* ou *imagem* de um comprovante de transferência "
            "e eu registro automaticamente na planilha Google Sheets.\n\n"
            "📊 Cada lançamento inclui:\n"
            "• Data • Destinatário • Valor\n"
            "• Pagador • Instituição • Acumulado\n\n"
            "_Powered by Google Gemini + Google Sheets_",
            parse_mode="Markdown"
        )


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))
    app.add_handler(MessageHandler(filters.COMMAND, handle_commands))
    log.info("✅ Bot iniciado e aguardando comprovantes...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
