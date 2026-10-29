"""
Jobot — Agente intelligente per la ricerca di lavoro
Bot Telegram (@gg_job_bot) + Claude 3.5 Haiku + SQLite locale
"""

import os
import io
import re
import json
import logging
import sqlite3
import asyncio

from groq import Groq
from pypdf import PdfReader
from job_search import search_all, deduplicate, fetch_indeed, fetch_infojobs, fetch_jooble, fetch_remoteok, linkedin_search_url
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
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
JOOBLE_API_KEY = os.environ.get("JOOBLE_API_KEY", "")
DB_PATH = "jobs.db"
GROQ_MODEL = "qwen/qwen3.8-27b"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Crea le tabelle se non esistono già."""
    with sqlite3.connect(DB_PATH) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id  INTEGER PRIMARY KEY,
                cv_text      TEXT NOT NULL,
                current_ral  INTEGER DEFAULT 0,
                current_role TEXT DEFAULT '',
                goal         TEXT DEFAULT '',
                updated_at   TEXT DEFAULT (datetime('now'))
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
        for col, definition in [
            ("current_ral",  "INTEGER DEFAULT 0"),
            ("current_role", "TEXT DEFAULT ''"),
            ("goal",         "TEXT DEFAULT ''"),
        ]:
            try:
                con.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass


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


def save_ral(telegram_id: int, ral: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE users SET current_ral = ?, updated_at = datetime('now') WHERE telegram_id = ?",
            (ral, telegram_id),
        )


def get_user_profile(telegram_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT current_ral, current_role, goal FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    if not row:
        return {"ral": 0, "role": "", "goal": ""}
    return {"ral": row[0] or 0, "role": row[1] or "", "goal": row[2] or ""}


def get_user_ral(telegram_id: int) -> int:
    return get_user_profile(telegram_id)["ral"]


def update_profile(telegram_id: int, **fields) -> None:
    """Aggiorna uno o più campi del profilo utente."""
    allowed = {"current_ral", "current_role", "goal"}
    cols = {k: v for k, v in fields.items() if k in allowed}
    if not cols:
        return
    set_clause = ", ".join(f"{k} = ?" for k in cols)
    values = list(cols.values()) + [telegram_id]
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            f"UPDATE users SET {set_clause}, updated_at = datetime('now') WHERE telegram_id = ?",
            values,
        )


def parse_ral(text: str) -> int:
    """Converte input utente in RAL intera €/anno. Es: '45.000€' → 45000, '45k' → 45000."""
    t = text.strip().lower()
    t = t.replace(".", "").replace(",", "").replace("€", "").replace(" ", "")
    t = t.replace("/anno", "").replace("anno", "").replace("ral", "")
    if t.endswith("k"):
        try:
            return int(float(t[:-1]) * 1000)
        except ValueError:
            return 0
    digits = re.sub(r"[^\d]", "", t)
    val = int(digits) if digits else 0
    # Sanity check: RAL ragionevole tra 10k e 500k
    return val if 10_000 <= val <= 500_000 else 0


def delete_user_data(telegram_id: int) -> None:
    """Elimina CV e tutte le ricerche dell'utente."""
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM searches WHERE telegram_id = ?", (telegram_id,))
        con.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))

# ---------------------------------------------------------------------------
# Helpers Claude
# ---------------------------------------------------------------------------

def _call_groq(system: str, user: str, max_tokens: int = 512) -> str:
    """Wrapper sincrono per una singola chiamata a Groq."""
    msg = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return msg.choices[0].message.content.strip()


def suggest_searches(cv_text: str, profile: dict | None = None) -> str:
    profile = profile or {}
    ctx_parts = []
    if profile.get("role"):
        ctx_parts.append(f"Ruolo attuale: {profile['role']}.")
    if profile.get("ral"):
        ctx_parts.append(
            f"RAL attuale: {profile['ral']:,}€/anno. Suggerisci ruoli con RAL pari o superiore.".replace(",", ".")
        )
    if profile.get("goal"):
        ctx_parts.append(f"Obiettivo dichiarato: {profile['goal']}.")
    ctx = (" ".join(ctx_parts) + "\n") if ctx_parts else ""
    system = (
        "Sei un career coach. Analizza il CV e suggerisci esattamente 3 ricerche di lavoro "
        "in italiano. Formato compatto:\n"
        "1. Ruolo: … | RAL: …€ | Modalità: …\n"
        "2. …\n3. …\nNessun testo aggiuntivo."
    )
    user = f"{ctx}CV (estratto):\n{cv_text[:3000]}"
    return _call_groq(system, user, max_tokens=256)


def quick_analyze_offer(cv_text: str, offer_text: str, current_ral: int = 0) -> dict:
    """Analisi rapida per /cerca. Restituisce dict con summary, ral, variazione %, score."""
    ral_ctx = f"RAL attuale candidato: {current_ral:,}€/anno. ".replace(",", ".") if current_ral > 0 else ""
    system = (
        "Sei un recruiter. Analizza l'offerta rispetto al CV. "
        "Rispondi SOLO con JSON valido, nessun testo fuori:\n"
        '{"summary":"max 350 caratteri","ral_min":0,"ral_max":0,"score":"X%"}\n'
        "ral_min e ral_max sono interi in €. Se non indicati nell'offerta, stimali dal ruolo e dal mercato italiano."
    )
    user = f"{ral_ctx}CV:\n{cv_text[:1500]}\n\nOFFERTA:\n{offer_text[:1500]}"
    raw = _call_groq(system, user, max_tokens=220)
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        data = json.loads(raw[start:end])
        ral_min = int(data.get("ral_min") or 0)
        ral_max = int(data.get("ral_max") or 0)
        if ral_min and ral_max:
            data["ral"] = f"{ral_min:,}–{ral_max:,}€".replace(",", ".")
        elif ral_min:
            data["ral"] = f"{ral_min:,}€".replace(",", ".")
        else:
            data["ral"] = "N/D"
        if current_ral > 0 and ral_min and ral_max:
            mid = (ral_min + ral_max) / 2
            pct = ((mid - current_ral) / current_ral) * 100
            data["ral_change"] = f"{pct:+.0f}%"
            data["ral_icon"] = "📈" if pct >= 0 else "📉"
        return data
    except Exception:
        return {"summary": raw[:350], "ral": "N/D", "score": "N/D"}


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
    return _call_groq(system, user, max_tokens=512)

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
        "• `/cerca` — cerca offerte dalle tue ricerche salvate.\n"
        "• `/profilo` — visualizza la tua situazione attuale.\n"
        "• `/aggiorna` — aggiorna RAL, ruolo o obiettivo.\n"
        "• `/reset` — cancella tutti i tuoi dati.\n\n"
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

        context.user_data["awaiting_ral"] = True
        await update.message.reply_text(
            "✅ CV salvato!\n\n"
            "💰 Qual è la tua *RAL attuale* (€/anno)?\n"
            "Es: `45.000` oppure `45k`\n\n"
            "_Scrivi_ `0` _per saltare questo passaggio._",
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


async def cmd_profilo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mostra la situazione attuale dell'utente."""
    telegram_id = update.effective_user.id
    cv_text = get_cv(telegram_id)
    profile = get_user_profile(telegram_id)
    searches = get_searches(telegram_id)

    cv_status = "✅ caricato" if cv_text else "❌ non caricato"
    ral_status = f"{profile['ral']:,}€/anno".replace(",", ".") if profile["ral"] else "non impostata"
    role_status = profile["role"] if profile["role"] else "non impostato"
    goal_status = profile["goal"] if profile["goal"] else "non impostato"

    await update.message.reply_text(
        f"👤 <b>Il tuo profilo</b>\n\n"
        f"📄 CV: {cv_status}\n"
        f"💼 Ruolo attuale: {role_status}\n"
        f"💰 RAL attuale: {ral_status}\n"
        f"🎯 Obiettivo: {goal_status}\n"
        f"🔍 Ricerche salvate: {len(searches)}\n\n"
        f"<i>Usa /aggiorna per modificare ruolo, RAL o obiettivo.</i>",
        parse_mode="HTML",
    )


async def cmd_aggiorna(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Aggiorna ruolo, RAL o obiettivo.
    Sintassi diretta: /aggiorna ral 50000 | /aggiorna ruolo Senior Dev | /aggiorna obiettivo ...
    Senza argomenti: avvia flusso guidato.
    """
    telegram_id = update.effective_user.id

    if not get_cv(telegram_id):
        await update.message.reply_text("Carica prima il tuo CV in PDF!")
        return

    args = context.args
    if not args:
        # Flusso guidato: prima step
        context.user_data["aggiorna_step"] = "ral"
        profile = get_user_profile(telegram_id)
        ral_str = f"{profile['ral']:,}€/anno".replace(",", ".") if profile["ral"] else "non impostata"
        await update.message.reply_text(
            f"🔄 <b>Aggiorna il tuo profilo</b>\n\n"
            f"<b>Step 1/3 — RAL attuale</b>\n"
            f"Valore attuale: {ral_str}\n\n"
            f"Scrivi la nuova RAL (es. <code>48000</code> o <code>48k</code>)\n"
            f"oppure <code>skip</code> per saltare.",
            parse_mode="HTML",
        )
        return

    # Sintassi diretta: /aggiorna ral 50000
    sub = args[0].lower()
    value = " ".join(args[1:]).strip()

    if sub == "ral":
        ral = parse_ral(value)
        if not ral:
            await update.message.reply_text("RAL non valida. Es: /aggiorna ral 45000")
            return
        update_profile(telegram_id, current_ral=ral)
        await update.message.reply_text(f"✅ RAL aggiornata: {ral:,}€/anno".replace(",", "."))

    elif sub == "ruolo":
        if not value:
            await update.message.reply_text("Specifica il ruolo. Es: /aggiorna ruolo Senior Developer")
            return
        update_profile(telegram_id, current_role=value)
        await update.message.reply_text(f"✅ Ruolo aggiornato: {value}")

    elif sub == "obiettivo":
        if not value:
            await update.message.reply_text("Specifica l'obiettivo. Es: /aggiorna obiettivo aumentare RAL del 20%")
            return
        update_profile(telegram_id, goal=value)
        await update.message.reply_text(f"✅ Obiettivo aggiornato: {value}")

    else:
        await update.message.reply_text(
            "Campi disponibili: <code>ral</code>, <code>ruolo</code>, <code>obiettivo</code>\n"
            "Oppure usa /aggiorna senza argomenti per il flusso guidato.",
            parse_mode="HTML",
        )


async def cmd_cerca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cerca offerte dalle fonti RSS e Jooble in base alle ricerche salvate."""
    telegram_id = update.effective_user.id

    cv_text = get_cv(telegram_id)
    if not cv_text:
        await update.message.reply_text("Carica prima il tuo CV in PDF!")
        return

    profile = get_user_profile(telegram_id)
    current_ral = profile["ral"]

    if not current_ral:
        context.user_data["aggiorna_step"] = "ral"
        context.user_data["pending_cerca"] = True
        await update.message.reply_text(
            "💰 Per una ricerca più precisa ho bisogno della tua <b>RAL attuale</b>.\n\n"
            "Scrivi l'importo (es. <code>45000</code> o <code>45k</code>)\n"
            "oppure <code>0</code> per saltare e cercare comunque.",
            parse_mode="HTML",
        )
        return

    searches = get_searches(telegram_id)
    if not searches:
        await update.message.reply_text(
            "Non hai ricerche salvate.\n"
            "Usa `/salva Ruolo | RAL | Modalità` per aggiungerne una.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"🔍 Cerco offerte per {len(searches)} ricerca/e salvata/e…"
    )

    counts = {"Indeed": 0, "InfoJobs": 0, "Jooble": 0, "RemoteOK": 0}
    all_offers: list[dict] = []

    for role, _ral, mode, _ in searches[:3]:
        location = "" if "remoto" in mode.lower() else "Italia"

        indeed    = await asyncio.get_event_loop().run_in_executor(None, fetch_indeed,    role, location)
        infojobs  = await asyncio.get_event_loop().run_in_executor(None, fetch_infojobs,  role)
        jooble    = await asyncio.get_event_loop().run_in_executor(None, fetch_jooble,    role, location, JOOBLE_API_KEY)
        remoteok  = await asyncio.get_event_loop().run_in_executor(None, fetch_remoteok,  role)

        counts["Indeed"]   += len(indeed)
        counts["InfoJobs"] += len(infojobs)
        counts["Jooble"]   += len(jooble)
        counts["RemoteOK"] += len(remoteok)

        all_offers.extend(indeed[:5] + infojobs[:5] + jooble[:5] + remoteok[:5])

    all_offers = deduplicate(all_offers)[:8]
    total = sum(counts.values())
    to_analyze = min(3, len(all_offers))

    sources_report = (
        f"📊 <b>Risultati ricerca:</b>\n"
        f"• Indeed: {counts['Indeed']} offerte\n"
        f"• InfoJobs: {counts['InfoJobs']} offerte\n"
        f"• Jooble: {counts['Jooble']} offerte\n"
        f"• RemoteOK: {counts['RemoteOK']} offerte\n"
        f"─────────────────\n"
        f"• Totale trovate: <b>{total}</b> ({len(all_offers)} uniche)\n"
        f"• Analizzate con AI: <b>{to_analyze}</b>"
    )
    await update.message.reply_text(sources_report, parse_mode="HTML")

    if not all_offers:
        await update.message.reply_text(
            "Nessuna offerta trovata. Prova a modificare le ricerche salvate con /salva o riprova più tardi."
        )
        return

    await update.message.reply_text(f"Analizzo le {to_analyze} offerte più rilevanti…")

    sent = 0
    for offer in all_offers:
        if sent >= 3:
            break
        offer_text = (
            f"Titolo: {offer['title']}\n"
            f"Azienda: {offer.get('company', 'N/D')}\n"
            f"Descrizione: {offer.get('description', '')}"
        )
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, quick_analyze_offer, cv_text, offer_text, current_ral
            )
            url = offer.get("url", "")
            ral_change = (
                f"{result.get('ral_icon', '📊')} Variazione RAL: <b>{result['ral_change']}</b>\n"
                if "ral_change" in result else ""
            )
            msg = (
                f"📌 <b>{offer['title']}</b>\n"
                f"🏢 {offer.get('company', 'N/D')} — <i>{offer.get('source', '')}</i>\n\n"
                f"{result.get('summary', '')}\n\n"
                f"💰 RAL stimata: <b>{result.get('ral', 'N/D')}</b>\n"
                f"{ral_change}"
                f"🎯 Possibilità di successo: <b>{result.get('score', 'N/D')}</b>\n"
                f"🔗 {url}"
            )
            await update.message.reply_text(msg, parse_mode="HTML")
            sent += 1
        except Exception:
            continue

    if sent == 0:
        await update.message.reply_text(
            "Non sono riuscito ad analizzare le offerte trovate. Riprova più tardi."
        )

    # Link LinkedIn precompilati per ogni ricerca salvata
    linkedin_lines = []
    for role, _ral, mode, _ in searches[:3]:
        url = linkedin_search_url(role, mode)
        linkedin_lines.append(f"• <a href=\"{url}\">{role}</a> ({mode or 'Tutti'})")

    if linkedin_lines:
        await update.message.reply_text(
            "🔗 <b>Cerca anche su LinkedIn</b> (ultima settimana):\n\n"
            + "\n".join(linkedin_lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Interpreta ogni messaggio di testo come un'offerta di lavoro da analizzare,
    oppure gestisce la conferma del reset se in attesa."""
    telegram_id = update.effective_user.id
    offer_text = update.message.text.strip()

    # Gestione RAL dopo upload CV
    if context.user_data.get("awaiting_ral"):
        context.user_data["awaiting_ral"] = False
        ral = parse_ral(offer_text)
        if ral > 0:
            update_profile(telegram_id, current_ral=ral)
            await update.message.reply_text(
                f"✅ RAL attuale salvata: <b>{ral:,}€/anno</b>\n\nCalcolo i suggerimenti…".replace(",", "."),
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("RAL non salvata. Calcolo i suggerimenti…")
        cv_text = get_cv(telegram_id)
        if cv_text:
            profile = get_user_profile(telegram_id)
            suggestions = await asyncio.get_event_loop().run_in_executor(
                None, suggest_searches, cv_text, profile
            )
            await update.message.reply_text(
                f"🎯 *Suggerimenti di ricerca strategici:*\n\n{suggestions}\n\n"
                "Usa `/salva Ruolo | RAL | Modalità` per registrarne una.",
                parse_mode="Markdown",
            )
        return

    # Flusso guidato /aggiorna (step RAL → ruolo → obiettivo)
    if context.user_data.get("aggiorna_step"):
        step = context.user_data.pop("aggiorna_step")
        pending_cerca = context.user_data.pop("pending_cerca", False)
        val = offer_text.strip()

        if step == "ral":
            if val.lower() != "skip":
                ral = parse_ral(val)
                if ral:
                    update_profile(telegram_id, current_ral=ral)
                    await update.message.reply_text(
                        f"✅ RAL: <b>{ral:,}€/anno</b>".replace(",", "."), parse_mode="HTML"
                    )
                elif val != "0":
                    await update.message.reply_text("RAL non riconosciuta, rimane invariata.")
            if pending_cerca:
                # Ritorna al /cerca dopo aver raccolto la RAL
                await update.message.reply_text("Perfetto! Avvio la ricerca…")
                await cmd_cerca(update, context)
                return
            # Continua con step ruolo se in flusso guidato
            profile = get_user_profile(telegram_id)
            role_str = profile["role"] or "non impostato"
            context.user_data["aggiorna_step"] = "ruolo"
            await update.message.reply_text(
                f"<b>Step 2/3 — Ruolo attuale</b>\nValore attuale: {role_str}\n\n"
                "Scrivi il tuo ruolo (es. <code>Data Analyst</code>) o <code>skip</code>.",
                parse_mode="HTML",
            )

        elif step == "ruolo":
            if val.lower() != "skip" and val:
                update_profile(telegram_id, current_role=val)
                await update.message.reply_text(f"✅ Ruolo: <b>{val}</b>", parse_mode="HTML")
            profile = get_user_profile(telegram_id)
            goal_str = profile["goal"] or "non impostato"
            context.user_data["aggiorna_step"] = "obiettivo"
            await update.message.reply_text(
                f"<b>Step 3/3 — Obiettivo</b>\nValore attuale: {goal_str}\n\n"
                "Es: <code>aumentare RAL del 20%</code>, <code>passare a remoto</code>, "
                "<code>ruolo manageriale</code>\nOppure <code>skip</code>.",
                parse_mode="HTML",
            )

        elif step == "obiettivo":
            if val.lower() != "skip" and val:
                update_profile(telegram_id, goal=val)
                await update.message.reply_text(f"✅ Obiettivo: <b>{val}</b>", parse_mode="HTML")
            await update.message.reply_text(
                "🎉 Profilo aggiornato! Usa /profilo per vederlo o /cerca per trovare offerte.",
            )
        return

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
    asyncio.set_event_loop(asyncio.new_event_loop())
    init_db()
    log.info("Database inizializzato: %s", DB_PATH)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("salva", cmd_salva))
    app.add_handler(CommandHandler("ricerche", cmd_ricerche))
    app.add_handler(CommandHandler("cerca", cmd_cerca))
    app.add_handler(CommandHandler("profilo", cmd_profilo))
    app.add_handler(CommandHandler("aggiorna", cmd_aggiorna))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    # I messaggi di testo puri vengono trattati come offerte o come conferma reset
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Jobot avviato — in ascolto su @gg_job_bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
