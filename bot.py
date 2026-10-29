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
from job_search import (
    deduplicate, linkedin_search_url,
    fetch_indeed, fetch_infojobs, fetch_jooble, fetch_remoteok,
    fetch_adzuna, fetch_talent, fetch_remotive, fetch_weworkremotely,
    fetch_monster, fetch_linkedin,
)
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
ADZUNA_APP_ID  = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
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
            ("current_ral",   "INTEGER DEFAULT 0"),
            ("current_role",  "TEXT DEFAULT ''"),
            ("goal",          "TEXT DEFAULT ''"),
            ("linkedin_email","TEXT DEFAULT ''"),
            ("linkedin_pw",   "TEXT DEFAULT ''"),
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
            "SELECT current_ral, current_role, goal, linkedin_email, linkedin_pw "
            "FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    if not row:
        return {"ral": 0, "role": "", "goal": "", "linkedin_email": "", "linkedin_pw": ""}
    return {
        "ral": row[0] or 0,
        "role": row[1] or "",
        "goal": row[2] or "",
        "linkedin_email": row[3] or "",
        "linkedin_pw": row[4] or "",
    }


def save_linkedin_creds(telegram_id: int, email: str, pw: str) -> None:
    update_profile(telegram_id, linkedin_email=email, linkedin_pw=pw)


def get_user_ral(telegram_id: int) -> int:
    return get_user_profile(telegram_id)["ral"]


def update_profile(telegram_id: int, **fields) -> None:
    """Aggiorna uno o più campi del profilo utente."""
    allowed = {"current_ral", "current_role", "goal", "linkedin_email", "linkedin_pw"}
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


def _parse_score(score_str: str) -> int:
    try:
        return int(str(score_str).replace("%", "").strip())
    except Exception:
        return 0


def quick_analyze_offer(cv_text: str, offer_text: str, current_ral: int = 0) -> dict:
    """Analisi per /cerca. Restituisce summary, ral, variazione %, score, motivazioni."""
    ral_ctx = f"RAL attuale candidato: {current_ral:,}€/anno. ".replace(",", ".") if current_ral > 0 else ""
    system = (
        "Sei un recruiter senior. Analizza l'offerta rispetto al CV. "
        "Rispondi SOLO con JSON valido, nessun testo fuori:\n"
        '{"summary":"max 300 caratteri sull\'offerta","ral_min":0,"ral_max":0,"score":"X%",'
        '"motivazioni":"2-3 bullet points concisi sui punti di forza del candidato per questa offerta, '
        'separati da \\n"}\n'
        "ral_min e ral_max sono interi in €. Stimali dal mercato italiano se non indicati."
    )
    user = f"{ral_ctx}CV:\n{cv_text[:1400]}\n\nOFFERTA:\n{offer_text[:1400]}"
    raw = _call_groq(system, user, max_tokens=320)
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
        data["score_int"] = _parse_score(data.get("score", "0"))
        return data
    except Exception:
        return {"summary": raw[:300], "ral": "N/D", "score": "N/D", "score_int": 0}


def generate_market_report(profile: dict, num_offers: int, ral_values: list[int]) -> dict:
    """Genera il report strategico di mercato tramite Groq."""
    current_ral = profile.get("ral", 0)
    role = profile.get("role", "")
    goal = profile.get("goal", "")

    avg_ral = int(sum(ral_values) / len(ral_values)) if ral_values else 0
    ral_min_market = min(ral_values) if ral_values else 0
    ral_max_market = max(ral_values) if ral_values else 0

    ctx = (
        f"Ruolo analizzato: {role}\n"
        f"RAL attuale candidato: {current_ral:,}€/anno\n".replace(",", ".")
        if current_ral else f"Ruolo analizzato: {role}\nRAL attuale: non indicata\n"
    )
    if avg_ral:
        ctx += (
            f"RAL media di mercato: {avg_ral:,}€/anno\n"
            f"Range mercato: {ral_min_market:,}–{ral_max_market:,}€\n"
        ).replace(",", ".")
    ctx += f"Offerte attive trovate: {num_offers}\n"
    if goal:
        ctx += f"Obiettivo candidato: {goal}\n"

    system = (
        "Sei un career strategist. Analizza la posizione di mercato del candidato "
        "e dai una raccomandazione chiara. Rispondi SOLO con JSON valido:\n"
        '{"posizione":"sotto mercato/in linea/sopra mercato",'
        '"delta":"es. +27% o -5% o N/D",'
        '"domanda":"alta/media/bassa",'
        '"segnale":"CAMBIA ORA/PREPARATI/RIMANI",'
        '"emoji":"🟢/🟡/🔴",'
        '"analisi":"2-3 frasi di analisi strategica in italiano",'
        '"azioni":["azione concreta 1","azione concreta 2","azione concreta 3"]}'
    )
    raw = _call_groq(system, ctx, max_tokens=400)
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except Exception:
        return {
            "posizione": "N/D", "delta": "N/D", "domanda": "N/D",
            "segnale": "N/D", "emoji": "⚪",
            "analisi": raw[:300], "azioni": [],
        }


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
        "• `/mercato` — analisi del tuo valore di mercato e raccomandazione strategica.\n"
        "• `/connetti linkedin` — collega il tuo account LinkedIn per più risultati.\n"
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

    cv_status   = "✅ caricato" if cv_text else "❌ non caricato"
    ral_status  = f"{profile['ral']:,}€/anno".replace(",", ".") if profile["ral"] else "non impostata"
    role_status = profile["role"] or "non impostato"
    goal_status = profile["goal"] or "non impostato"
    li_status   = f"✅ {profile['linkedin_email']}" if profile.get("linkedin_email") else "❌ non collegato"

    await update.message.reply_text(
        f"👤 <b>Il tuo profilo</b>\n\n"
        f"📄 CV: {cv_status}\n"
        f"💼 Ruolo attuale: {role_status}\n"
        f"💰 RAL attuale: {ral_status}\n"
        f"🎯 Obiettivo: {goal_status}\n"
        f"🔗 LinkedIn: {li_status}\n"
        f"🔍 Ricerche salvate: {len(searches)}\n\n"
        f"<i>Usa /aggiorna per modificare il profilo.\n"
        f"Usa /connetti linkedin per collegare il tuo account.</i>",
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

    sources = [
        "Indeed", "InfoJobs", "Jooble", "RemoteOK",
        "Adzuna", "Talent.com", "Remotive", "WeWorkRemotely",
        "Monster", "LinkedIn",
    ]
    counts = {s: 0 for s in sources}
    all_offers: list[dict] = []

    await update.message.reply_text(
        f"🔍 Cerco su {len(sources)} piattaforme in parallelo…"
    )

    loop = asyncio.get_event_loop()
    for role, _ral, mode, _ in searches[:3]:
        loc = "" if "remoto" in mode.lower() else "Italia"

        # Tutti i fetch in parallelo per questa ricerca
        results = await asyncio.gather(
            loop.run_in_executor(None, fetch_indeed,        role, loc),
            loop.run_in_executor(None, fetch_infojobs,      role),
            loop.run_in_executor(None, fetch_jooble,        role, loc, JOOBLE_API_KEY),
            loop.run_in_executor(None, fetch_remoteok,      role),
            loop.run_in_executor(None, fetch_adzuna,        role, loc, ADZUNA_APP_ID, ADZUNA_APP_KEY),
            loop.run_in_executor(None, fetch_talent,        role, loc or "Italy"),
            loop.run_in_executor(None, fetch_remotive,      role),
            loop.run_in_executor(None, fetch_weworkremotely, role),
            loop.run_in_executor(None, fetch_monster,       role),
            loop.run_in_executor(None, fetch_linkedin, role, loc, profile.get("linkedin_email",""), profile.get("linkedin_pw","")),
        )
        for source, batch in zip(sources, results):
            counts[source] += len(batch)
            all_offers.extend(batch[:5])

    all_offers = deduplicate(all_offers)[:12]
    total = sum(counts.values())
    to_analyze = min(5, len(all_offers))

    # Report: sorgenti con risultati in evidenza, le vuote compatte
    active = [f"• {s}: <b>{counts[s]}</b>" for s in sources if counts[s] > 0]
    empty  = [s for s in sources if counts[s] == 0]
    report = "\n".join(active)
    if empty:
        report += f"\n• Nessun risultato: <i>{', '.join(empty)}</i>"

    sources_report = (
        f"📊 <b>Risultati ricerca:</b>\n{report}\n"
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

    await update.message.reply_text(f"⚙️ Analizzo {len(all_offers)} offerte in parallelo…")

    # Analisi in parallelo di tutte le offerte
    loop = asyncio.get_event_loop()
    analyses = await asyncio.gather(*[
        loop.run_in_executor(
            None, quick_analyze_offer, cv_text,
            f"Titolo: {o['title']}\nAzienda: {o.get('company','')}\nDescrizione: {o.get('description','')}",
            current_ral,
        )
        for o in all_offers
    ])

    # Abbina offerta → analisi e ordina per score
    scored = sorted(
        zip(all_offers, analyses),
        key=lambda x: x[1].get("score_int", 0),
        reverse=True,
    )

    # ── Lista compatta di tutte le offerte ──────────────────────────────────
    lines = []
    for i, (offer, res) in enumerate(scored, 1):
        url = offer.get("url", "")
        score = res.get("score", "?")
        company = offer.get("company", "")
        source = offer.get("source", "")
        title_part = f'<a href="{url}">{offer["title"]}</a>' if url else offer["title"]
        company_part = f" — {company}" if company else ""
        lines.append(f'{i}. {title_part}{company_part} <i>[{source}]</i> 🎯{score}')

    # Suddividi in blocchi da max 30 offerte per messaggio
    BLOCK = 30
    for start in range(0, len(lines), BLOCK):
        chunk = lines[start:start + BLOCK]
        header = f"📋 <b>Tutte le offerte ({len(scored)})</b>\n\n" if start == 0 else ""
        await update.message.reply_text(
            header + "\n".join(chunk),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    # ── Top 5 con score ≥ 50% e motivazioni ────────────────────────────────
    top5 = [(o, r) for o, r in scored if r.get("score_int", 0) >= 50][:5]

    if top5:
        await update.message.reply_text(f"🏆 <b>Top {len(top5)} per il tuo profilo:</b>", parse_mode="HTML")
        for offer, res in top5:
            url = offer.get("url", "")
            ral_change = (
                f"{res.get('ral_icon','📊')} <b>{res['ral_change']}</b> vs RAL attuale\n"
                if "ral_change" in res else ""
            )
            motivazioni = res.get("motivazioni", "").strip()
            mot_block = f"\n✅ <b>Perché fa per te:</b>\n{motivazioni}\n" if motivazioni else ""
            msg = (
                f"📌 <b>{offer['title']}</b>\n"
                f"🏢 {offer.get('company','N/D')} — <i>{offer.get('source','')}</i>\n\n"
                f"{res.get('summary','')}\n\n"
                f"💰 RAL: <b>{res.get('ral','N/D')}</b> {ral_change}"
                f"🎯 Successo: <b>{res.get('score','N/D')}</b>"
                f"{mot_block}\n"
                f"🔗 {url}"
            )
            await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await update.message.reply_text(
            "Nessuna offerta ha superato il 50% di compatibilità. "
            "Prova ad aggiornare le ricerche o il CV."
        )

    # ── Link LinkedIn precompilati ──────────────────────────────────────────
    li_lines = [
        f'• <a href="{linkedin_search_url(role, mode)}">{role}</a> ({mode or "Tutti"})'
        for role, _ral, mode, _ in searches[:3]
    ]
    await update.message.reply_text(
        "🔗 <b>Cerca anche su LinkedIn</b> (ultima settimana):\n\n" + "\n".join(li_lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_connetti(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Collega un servizio esterno all'account utente. Es: /connetti linkedin"""
    telegram_id = update.effective_user.id
    args = context.args

    if not args or args[0].lower() != "linkedin":
        await update.message.reply_text(
            "Servizi disponibili:\n• <code>/connetti linkedin</code>",
            parse_mode="HTML",
        )
        return

    profile = get_user_profile(telegram_id)
    status = f"Account attuale: <b>{profile['linkedin_email']}</b>\n\n" if profile.get("linkedin_email") else ""

    context.user_data["connetti_step"] = "linkedin_email"
    await update.message.reply_text(
        f"🔗 <b>Collega LinkedIn</b>\n\n"
        f"{status}"
        "Scrivi la tua <b>email LinkedIn</b>:\n\n"
        "<i>Le credenziali vengono salvate solo nel database locale del bot "
        "e non vengono mai condivise o trasmesse a terzi.</i>",
        parse_mode="HTML",
    )


async def cmd_mercato(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analisi del valore di mercato e raccomandazione strategica."""
    telegram_id = update.effective_user.id
    profile = get_user_profile(telegram_id)

    if not profile["role"] and not get_cv(telegram_id):
        await update.message.reply_text(
            "Carica prima il tuo CV e imposta il ruolo attuale con /aggiorna ruolo."
        )
        return

    role = profile["role"] or "professionista"
    current_ral = profile["ral"]

    if not current_ral:
        context.user_data["aggiorna_step"] = "ral"
        context.user_data["pending_mercato"] = True
        await update.message.reply_text(
            "💰 Per l'analisi di mercato ho bisogno della tua <b>RAL attuale</b>.\n\n"
            "Scrivi l'importo (es. <code>45000</code>) oppure <code>0</code> per saltare.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        f"📡 Scansiono il mercato per <b>{role}</b>…",
        parse_mode="HTML",
    )

    # Ricerca flash sul ruolo attuale su tutte le piattaforme in parallelo
    loop = asyncio.get_event_loop()
    sources_results = await asyncio.gather(
        loop.run_in_executor(None, fetch_jooble,        role, "Italia", JOOBLE_API_KEY),
        loop.run_in_executor(None, fetch_adzuna,        role, "Italia", ADZUNA_APP_ID, ADZUNA_APP_KEY),
        loop.run_in_executor(None, fetch_remotive,      role),
        loop.run_in_executor(None, fetch_talent,        role, "Italy"),
        loop.run_in_executor(None, fetch_remoteok,      role),
        loop.run_in_executor(None, fetch_linkedin, role, "Italia", profile.get("linkedin_email",""), profile.get("linkedin_pw","")),
    )

    all_offers = deduplicate([o for batch in sources_results for o in batch])
    num_offers = len(all_offers)

    if not all_offers:
        await update.message.reply_text(
            "⚠️ Nessuna offerta trovata per questo ruolo al momento. "
            "Il mercato potrebbe essere molto di nicchia o le piattaforme non disponibili."
        )
        return

    await update.message.reply_text(
        f"🔢 Trovate <b>{num_offers}</b> offerte. Calcolo il valore di mercato…",
        parse_mode="HTML",
    )

    # Estraggo RAL da un campione di offerte (max 8) in parallelo
    sample = all_offers[:8]
    quick_analyses = await asyncio.gather(*[
        loop.run_in_executor(
            None, quick_analyze_offer,
            "", f"Titolo: {o['title']}\nDescrizione: {o.get('description','')}", 0,
        )
        for o in sample
    ])

    ral_values = []
    for a in quick_analyses:
        ral_min = a.get("ral_min") or 0
        ral_max = a.get("ral_max") or 0
        if isinstance(ral_min, (int, float)) and isinstance(ral_max, (int, float)):
            if ral_min > 0 and ral_max > 0:
                ral_values.append(int((ral_min + ral_max) / 2))

    # Genera il report strategico
    report = await loop.run_in_executor(
        None, generate_market_report, profile, num_offers, ral_values
    )

    avg_ral = int(sum(ral_values) / len(ral_values)) if ral_values else 0
    ral_min_m = min(ral_values) if ral_values else 0
    ral_max_m = max(ral_values) if ral_values else 0

    # Riga RAL attuale
    ral_line = f"💰 La tua RAL:     <b>{current_ral:,}€</b>\n".replace(",", ".") if current_ral else ""
    market_ral_line = (
        f"📊 Media mercato:  <b>{avg_ral:,}€</b> ({report.get('delta','N/D')})\n"
        f"📉 Range mercato:  {ral_min_m:,}€ – {ral_max_m:,}€\n"
    ).replace(",", ".") if avg_ral else ""

    domanda_emoji = {"alta": "🔥", "media": "📈", "bassa": "❄️"}.get(
        report.get("domanda", "").lower(), "📊"
    )

    azioni = report.get("azioni", [])
    azioni_block = "\n".join(f"  • {a}" for a in azioni) if azioni else ""

    segnale = report.get("segnale", "N/D")
    emoji = report.get("emoji", "⚪")

    msg = (
        f"📊 <b>REPORT DI MERCATO — {role.upper()}</b>\n\n"
        f"{ral_line}"
        f"{market_ral_line}"
        f"🔢 Offerte attive:  <b>{num_offers}</b>\n"
        f"{domanda_emoji} Domanda:         <b>{report.get('domanda','N/D').upper()}</b>\n"
        f"📍 Posizione:       <b>{report.get('posizione','N/D')}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} <b>SEGNALE: {segnale}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{report.get('analisi','')}\n\n"
        f"⚡ <b>Azioni consigliate:</b>\n{azioni_block}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


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

    # Flusso collegamento LinkedIn (/connetti linkedin)
    if context.user_data.get("connetti_step"):
        step = context.user_data["connetti_step"]

        if step == "linkedin_email":
            context.user_data["linkedin_email_tmp"] = offer_text.strip()
            context.user_data["connetti_step"] = "linkedin_pw"
            await update.message.reply_text(
                "🔑 Ora scrivi la tua <b>password LinkedIn</b>.\n\n"
                "⚠️ <i>Il messaggio verrà eliminato subito dopo la lettura.</i>",
                parse_mode="HTML",
            )
            return

        if step == "linkedin_pw":
            context.user_data.pop("connetti_step", None)
            email = context.user_data.pop("linkedin_email_tmp", "")
            pw = offer_text.strip()
            # Elimina il messaggio con la password dalla chat
            try:
                await update.message.delete()
            except Exception:
                pass
            if email and pw:
                save_linkedin_creds(telegram_id, email, pw)
                await update.message.reply_text(
                    f"✅ LinkedIn collegato come <b>{email}</b>.\n"
                    "Il messaggio con la password è stato eliminato dalla chat.",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text("Credenziali non valide. Riprova con /connetti linkedin.")
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
                await update.message.reply_text("Perfetto! Avvio la ricerca…")
                await cmd_cerca(update, context)
                return
            if context.user_data.pop("pending_mercato", False):
                await update.message.reply_text("Perfetto! Avvio l'analisi di mercato…")
                await cmd_mercato(update, context)
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
    app.add_handler(CommandHandler("connetti", cmd_connetti))
    app.add_handler(CommandHandler("mercato", cmd_mercato))
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
