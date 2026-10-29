"""
Jobot — Agente intelligente per la ricerca di lavoro
Bot Telegram (@gg_job_bot) + Claude 3.5 Haiku + SQLite locale
"""

import os
import io
import logging
import sqlite3
import asyncio

import anthropic
from pypdf import PdfReader
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_PATH = "jobs.db"
CLAUDE_MODEL = "claude-3-5-haiku-20241022"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Crea le tabelle se non esistono già."""
    with sqlite3.connect(DB_PATH) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                cv_text     TEXT NOT NULL,
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS searches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                role        TEXT NOT NULL,
                ral         TEXT,
                mode        TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            );
        """)


def save_cv(telegram_id: int, cv_text: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO users (telegram_id, cv_text)
            VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                cv_text    = excluded.cv_text,
                updated_at = datetime('now')
        """, (telegram_id, cv_text))


def get_cv(telegram_id: int) -> str | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT cv_text FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row[0] if row else None


def save_search(telegram_id: int, role: str, ral: str, mode: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO searches (telegram_id, role, ral, mode) VALUES (?, ?, ?, ?)",
            (telegram_id, role, ral, mode),
        )


def get_searches(telegram_id: int) -> list[tuple]:
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            "SELECT role, ral, mode, created_at FROM searches WHERE telegram_id = ? ORDER BY created_at DESC",
            (telegram_id,),
        ).fetchall()


def delete_user_data(telegram_id: int) -> None:
    """Elimina CV e tutte le ricerche dell'utente."""
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM searches WHERE telegram_id = ?", (telegram_id,))
        con.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))

# ---------------------------------------------------------------------------
# Helpers Claude
# ---------------------------------------------------------------------------

def _call_claude(system: str, user: str, max_tokens: int = 512) -> str:
    """Wrapper sincrono per una singola chiamata a Claude."""
    msg = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text.strip()


def suggest_searches(cv_text: str) -> str:
    system = (
        "Sei un career coach. Analizza il CV e suggerisci esattamente 3 ricerche di lavoro "
        "in italiano. Formato compatto:\n"
        "1. Ruolo: … | RAL: …€ | Modalità: …\n"
        "2. …\n3. …\nNessun testo aggiuntivo."
    )
    # Limitiamo il CV a 3000 caratteri per ottimizzare i token
    user = f"CV (estratto):\n{cv_text[:3000]}"
    return _call_claude(system, user, max_tokens=256)


def analyze_offer(cv_text: str, offer_text: str) -> str:
    system = (
        "Sei un recruiter esperto. Confronta il CV con l'offerta e rispondi in italiano "
        "con questo schema esatto (usa i titoli in grassetto):\n\n"
        "**Percentuale di successo:** X%\n"
        "**Criteri soddisfatti:** elenco puntato\n"
        "**Aree da migliorare / mancanze:** elenco puntato\n"
        "**Consiglio pratico:** una frase d'azione concreta\n\n"
        "Sii diretto e sintetico."
    )
    user = (
        f"CV (estratto, max 2500 car.):\n{cv_text[:2500]}\n\n"
        f"OFFERTA DI LAVORO:\n{offer_text[:2000]}"
    )
    return _call_claude(system, user, max_tokens=512)

# ---------------------------------------------------------------------------
# Handler Telegram
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Benvenuto su *Jobot*!\n\n"
        "Ecco cosa puoi fare:\n"
        "• 📄 Inviami il tuo *CV in PDF* — lo analizzo e ti suggerisco 3 ricerche strategiche.\n"
        "• 💼 Incolla il *testo di un'offerta* — la confronto con il tuo CV.\n"
        "• `/salva Ruolo | RAL | Modalità` — salva una ricerca nel database.\n"
        "• `/ricerche` — mostra tutte le ricerche salvate.\n"
        "• `/reset` — cancella il tuo CV e tutte le ricerche.\n\n"
        "Inizia inviando il tuo CV!",
        parse_mode="Markdown",
    )


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Riceve un PDF, estrae il testo, salva nel DB, chiede a Claude i suggerimenti."""
    telegram_id = update.effective_user.id
    doc = update.message.document

    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("Per favore invia un file PDF.")
        return

    await update.message.reply_text("⏳ Sto leggendo il tuo CV…")

    try:
        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = await file.download_as_bytearray()

        reader = PdfReader(io.BytesIO(bytes(pdf_bytes)))
        cv_text = "\n".join(
            page.extract_text() or "" for page in reader.pages
        ).strip()

        if not cv_text:
            await update.message.reply_text(
                "Non sono riuscito a estrarre testo dal PDF. "
                "Prova con un PDF non scansionato (testo selezionabile)."
            )
            return

        save_cv(telegram_id, cv_text)
        log.info("CV salvato per utente %d (%d caratteri)", telegram_id, len(cv_text))

        await update.message.reply_text("✅ CV salvato! Chiedo a Claude i suggerimenti…")

        # Chiamata bloccante in un thread separato per non bloccare l'event loop
        suggestions = await asyncio.get_event_loop().run_in_executor(
            None, suggest_searches, cv_text
        )

        await update.message.reply_text(
            f"🎯 *Suggerimenti di ricerca strategici:*\n\n{suggestions}\n\n"
            "Usa `/salva Ruolo | RAL | Modalità` per registrarne una.",
            parse_mode="Markdown",
        )

    except Exception as e:
        log.exception("Errore nella gestione del PDF")
        await update.message.reply_text(f"Errore durante l'elaborazione: {e}")


async def cmd_salva(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sintassi: /salva Ruolo | RAL | Modalità"""
    telegram_id = update.effective_user.id
    args_text = " ".join(context.args).strip()

    parts = [p.strip() for p in args_text.split("|")]
    if len(parts) < 1 or not parts[0]:
        await update.message.reply_text(
            "Formato: `/salva Ruolo | RAL | Modalità`\n"
            "Esempio: `/salva Sviluppatore Python | 45.000€ | Remoto`",
            parse_mode="Markdown",
        )
        return

    role = parts[0]
    ral = parts[1] if len(parts) > 1 else ""
    mode = parts[2] if len(parts) > 2 else ""

    save_search(telegram_id, role, ral, mode)

    rows = get_searches(telegram_id)
    lista = "\n".join(
        f"• {r[0]} | {r[1]} | {r[2]}" for r in rows
    )
    await update.message.reply_text(
        f"✅ Ricerca salvata!\n\n*Le tue ricerche:*\n{lista}",
        parse_mode="Markdown",
    )


async def cmd_ricerche(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mostra tutte le ricerche salvate dall'utente."""
    telegram_id = update.effective_user.id
    rows = get_searches(telegram_id)

    if not rows:
        await update.message.reply_text(
            "Non hai ancora ricerche salvate.\n"
            "Usa `/salva Ruolo | RAL | Modalità` per aggiungerne una.",
            parse_mode="Markdown",
        )
        return

    lines = [f"{i+1}. *{r[0]}* | {r[1]} | {r[2]}" for i, r in enumerate(rows)]
    await update.message.reply_text(
        "📋 *Le tue ricerche salvate:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancella CV e ricerche dell'utente dopo conferma."""
    telegram_id = update.effective_user.id

    # Controlla se c'è qualcosa da cancellare
    cv_exists = get_cv(telegram_id) is not None
    searches = get_searches(telegram_id)

    if not cv_exists and not searches:
        await update.message.reply_text("Non ho dati da cancellare per te.")
        return

    riepilogo = []
    if cv_exists:
        riepilogo.append("il tuo CV")
    if searches:
        riepilogo.append(f"{len(searches)} ricerca/e salvata/e")

    # Salva lo stato in attesa di conferma nel context.user_data
    context.user_data["reset_pending"] = True
    await update.message.reply_text(
        f"⚠️ Stai per cancellare {' e '.join(riepilogo)}.\n\n"
        "Rispondi *SI* per confermare o qualsiasi altra cosa per annullare.",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Interpreta ogni messaggio di testo come un'offerta di lavoro da analizzare,
    oppure gestisce la conferma del reset se in attesa."""
    telegram_id = update.effective_user.id
    offer_text = update.message.text.strip()

    # Gestione conferma reset
    if context.user_data.get("reset_pending"):
        context.user_data["reset_pending"] = False
        if offer_text.strip().upper() == "SI":
            delete_user_data(telegram_id)
            await update.message.reply_text(
                "✅ Dati cancellati. Puoi inviare un nuovo CV quando vuoi."
            )
        else:
            await update.message.reply_text("Operazione annullata. I tuoi dati sono al sicuro.")
        return

    if len(offer_text) < 80:
        await update.message.reply_text(
            "Testo troppo corto per essere un'offerta. "
            "Incolla il testo completo dell'annuncio."
        )
        return

    cv_text = get_cv(telegram_id)
    if not cv_text:
        await update.message.reply_text(
            "Non ho ancora il tuo CV. Inviamelo prima come PDF!"
        )
        return

    await update.message.reply_text("🔍 Analizzo l'offerta con il tuo CV…")

    try:
        analysis = await asyncio.get_event_loop().run_in_executor(
            None, analyze_offer, cv_text, offer_text
        )
        await update.message.reply_text(analysis, parse_mode="Markdown")

    except Exception as e:
        log.exception("Errore nell'analisi dell'offerta")
        await update.message.reply_text(f"Errore durante l'analisi: {e}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    init_db()
    log.info("Database inizializzato: %s", DB_PATH)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("salva", cmd_salva))
    app.add_handler(CommandHandler("ricerche", cmd_ricerche))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    # I messaggi di testo puri vengono trattati come offerte o come conferma reset
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Jobot avviato — in ascolto su @gg_job_bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
