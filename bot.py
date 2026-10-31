"""
Jobot — Agente intelligente per la ricerca di lavoro
Bot Telegram (@gg_job_bot) + Groq + Supabase (PostgreSQL)
Webhook mode per Google Cloud Run; polling per sviluppo locale.
"""

import os
import io
import re
import json
import logging
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import asyncpg
from groq import Groq
from pypdf import PdfReader
from job_search import (
    deduplicate, linkedin_search_url,
    fetch_indeed, fetch_infojobs, fetch_jooble, fetch_remoteok,
    fetch_adzuna, fetch_talent, fetch_remotive, fetch_weworkremotely,
    fetch_monster, fetch_linkedin, fetch_market_insights,
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY    = os.environ["GROQ_API_KEY"]
DATABASE_URL    = os.environ["DATABASE_URL"]
JOOBLE_API_KEY  = os.environ.get("JOOBLE_API_KEY", "")
ADZUNA_APP_ID   = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY  = os.environ.get("ADZUNA_APP_KEY", "")
WEBHOOK_URL     = os.environ.get("WEBHOOK_URL", "")   # https://your-cloud-run-url (senza slash finale)
PORT            = int(os.environ.get("PORT", "8080"))

GROQ_MODEL      = "qwen/qwen3.8-27b"
CACHE_TTL_HOURS = 4

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)

# ---------------------------------------------------------------------------
# Database — asyncpg pool
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def init_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id    BIGINT PRIMARY KEY,
                cv_text        TEXT NOT NULL DEFAULT '',
                current_ral    INTEGER DEFAULT 0,
                user_role      TEXT DEFAULT '',
                goal           TEXT DEFAULT '',
                linkedin_email TEXT DEFAULT '',
                linkedin_pw    TEXT DEFAULT '',
                alert_days     INTEGER DEFAULT 0,
                alert_last     TEXT DEFAULT '',
                updated_at     TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS searches (
                id          BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                role        TEXT NOT NULL,
                ral         TEXT DEFAULT '',
                mode        TEXT DEFAULT '',
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS search_cache (
                cache_key   TEXT PRIMARY KEY,
                role        TEXT NOT NULL,
                location    TEXT NOT NULL,
                offers_json TEXT NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
        """)
    log.info("Database inizializzato")


async def _ensure_user(conn: asyncpg.Connection, telegram_id: int) -> None:
    await conn.execute("""
        INSERT INTO users (telegram_id, cv_text) VALUES ($1, '')
        ON CONFLICT (telegram_id) DO NOTHING
    """, telegram_id)


async def save_cv(telegram_id: int, cv_text: str) -> None:
    pool = await get_pool()
    await pool.execute("""
        INSERT INTO users (telegram_id, cv_text)
        VALUES ($1, $2)
        ON CONFLICT (telegram_id) DO UPDATE SET
            cv_text    = EXCLUDED.cv_text,
            updated_at = NOW()
    """, telegram_id, cv_text)


async def get_cv(telegram_id: int) -> str | None:
    pool = await get_pool()
    val = await pool.fetchval(
        "SELECT cv_text FROM users WHERE telegram_id = $1", telegram_id
    )
    return val or None


async def save_search(telegram_id: int, role: str, ral: str, mode: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure_user(conn, telegram_id)
        await conn.execute(
            "INSERT INTO searches (telegram_id, role, ral, mode) VALUES ($1, $2, $3, $4)",
            telegram_id, role, ral, mode,
        )


async def get_searches(telegram_id: int) -> list[tuple]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT role, ral, mode, created_at FROM searches "
        "WHERE telegram_id = $1 ORDER BY created_at DESC",
        telegram_id,
    )
    return [(r["role"], r["ral"] or "", r["mode"] or "", str(r["created_at"])) for r in rows]


async def get_user_profile(telegram_id: int) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT current_ral, user_role, goal, linkedin_email, linkedin_pw, alert_days, alert_last "
        "FROM users WHERE telegram_id = $1",
        telegram_id,
    )
    if not row:
        return {
            "ral": 0, "role": "", "goal": "",
            "linkedin_email": "", "linkedin_pw": "",
            "alert_days": 0, "alert_last": "",
        }
    return {
        "ral": row["current_ral"] or 0,
        "role": row["user_role"] or "",
        "goal": row["goal"] or "",
        "linkedin_email": row["linkedin_email"] or "",
        "linkedin_pw": row["linkedin_pw"] or "",
        "alert_days": row["alert_days"] or 0,
        "alert_last": row["alert_last"] or "",
    }


async def update_profile(telegram_id: int, **fields) -> None:
    allowed = {
        "current_ral", "user_role", "goal",
        "linkedin_email", "linkedin_pw", "alert_days", "alert_last",
    }
    cols = {k: v for k, v in fields.items() if k in allowed}
    if not cols:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure_user(conn, telegram_id)
        keys = list(cols.keys())
        vals = list(cols.values())
        set_clause = ", ".join(f"{k} = ${i + 1}" for i, k in enumerate(keys))
        await conn.execute(
            f"UPDATE users SET {set_clause}, updated_at = NOW() WHERE telegram_id = ${len(keys) + 1}",
            *vals, telegram_id,
        )


async def save_linkedin_creds(telegram_id: int, email: str, pw: str) -> None:
    await update_profile(telegram_id, linkedin_email=email, linkedin_pw=pw)


async def delete_user_data(telegram_id: int) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM users WHERE telegram_id = $1", telegram_id)


# ---------------------------------------------------------------------------
# Cache offerte
# ---------------------------------------------------------------------------

def _cache_key(role: str, location: str) -> str:
    return hashlib.md5(f"{role.lower().strip()}|{location.lower().strip()}".encode()).hexdigest()


async def get_cached_offers(role: str, location: str) -> list[dict] | None:
    key = _cache_key(role, location)
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT offers_json, created_at FROM search_cache WHERE cache_key = $1", key
    )
    if not row:
        return None
    try:
        created = row["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(tz=timezone.utc) - created > timedelta(hours=CACHE_TTL_HOURS):
            return None
        return json.loads(row["offers_json"])
    except Exception:
        return None


async def set_cached_offers(role: str, location: str, offers: list[dict]) -> None:
    key = _cache_key(role, location)
    pool = await get_pool()
    await pool.execute("""
        INSERT INTO search_cache (cache_key, role, location, offers_json)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (cache_key) DO UPDATE SET
            offers_json = EXCLUDED.offers_json,
            created_at  = NOW()
    """, key, role, location, json.dumps(offers))


# ---------------------------------------------------------------------------
# Helpers RAL
# ---------------------------------------------------------------------------

def parse_ral(text: str) -> int:
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
    return val if 10_000 <= val <= 500_000 else 0


# ---------------------------------------------------------------------------
# Helpers Groq (sync — chiamati via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _call_groq(system: str, user: str, max_tokens: int = 512) -> str:
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
    return _call_groq(system, f"{ctx}CV (estratto):\n{cv_text[:3000]}", max_tokens=256)


def _parse_score(score_str: str) -> int:
    try:
        return int(str(score_str).replace("%", "").strip())
    except Exception:
        return 0


def quick_analyze_offer(cv_text: str, offer_text: str, current_ral: int = 0) -> dict:
    ral_ctx = f"RAL attuale candidato: {current_ral:,}€/anno. ".replace(",", ".") if current_ral > 0 else ""
    system = (
        "Sei un recruiter senior. Analizza l'offerta rispetto al CV. "
        "Rispondi SOLO con JSON valido, nessun testo fuori:\n"
        '{"summary":"max 300 caratteri sull\'offerta","ral_min":0,"ral_max":0,"score":"X%",'
        '"motivazioni":"2-3 bullet points concisi sui punti di forza del candidato per questa offerta, '
        'separati da \\n"}\n'
        "ral_min e ral_max sono interi in €. Stimali dal mercato italiano se non indicati."
    )
    raw = _call_groq(system, f"{ral_ctx}CV:\n{cv_text[:1400]}\n\nOFFERTA:\n{offer_text[:1400]}", max_tokens=320)
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
# Alert — scheduling
# ---------------------------------------------------------------------------

def _schedule_alert(job_queue, telegram_id: int, days: int) -> None:
    interval = timedelta(days=days)
    job_queue.run_repeating(
        _send_alert_job,
        interval=interval,
        first=interval,
        name=f"alert_{telegram_id}",
        data={"telegram_id": telegram_id},
    )
    log.info("Alert schedulato per utente %d ogni %d giorni", telegram_id, days)


async def _send_alert_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = context.job.data["telegram_id"]
    try:
        profile  = await get_user_profile(telegram_id)
        searches = await get_searches(telegram_id)
        cv_text  = await get_cv(telegram_id)

        if not searches or not cv_text:
            return

        all_offers: list[dict] = []

        for role, _ral, mode, _ in searches[:3]:
            loc = "" if "remoto" in mode.lower() else "Italia"

            cached = await get_cached_offers(role, loc)
            if cached is not None:
                all_offers.extend(cached[:5])
                log.info("Alert: cache hit per '%s'", role)
                continue

            results = await asyncio.gather(
                asyncio.to_thread(fetch_indeed,         role, loc),
                asyncio.to_thread(fetch_infojobs,       role),
                asyncio.to_thread(fetch_jooble,         role, loc, JOOBLE_API_KEY),
                asyncio.to_thread(fetch_remoteok,       role),
                asyncio.to_thread(fetch_adzuna,         role, loc, ADZUNA_APP_ID, ADZUNA_APP_KEY),
                asyncio.to_thread(fetch_talent,         role, loc or "Italy"),
                asyncio.to_thread(fetch_remotive,       role),
                asyncio.to_thread(fetch_weworkremotely, role),
                asyncio.to_thread(fetch_monster,        role),
                asyncio.to_thread(fetch_linkedin, role, loc,
                                  profile.get("linkedin_email", ""), profile.get("linkedin_pw", "")),
            )
            batch_offers = [o for sub in results for o in sub]
            await set_cached_offers(role, loc, batch_offers)
            all_offers.extend(batch_offers[:5])

        all_offers = deduplicate(all_offers)[:8]
        if not all_offers:
            return

        current_ral = profile.get("ral", 0)
        sem = asyncio.Semaphore(3)

        async def analyze_one(offer: dict) -> dict:
            offer_text = (
                f"Titolo: {offer['title']}\n"
                f"Azienda: {offer.get('company', '')}\n"
                f"Descrizione: {offer.get('description', '')}"
            )
            async with sem:
                return await asyncio.to_thread(quick_analyze_offer, cv_text, offer_text, current_ral)

        to_analyze = min(5, len(all_offers))
        analyses = await asyncio.gather(*[analyze_one(o) for o in all_offers[:to_analyze]])

        scored = sorted(
            zip(all_offers[:to_analyze], analyses),
            key=lambda x: x[1].get("score_int", 0),
            reverse=True,
        )
        top = [(o, r) for o, r in scored if r.get("score_int", 0) >= 40][:3] or list(scored[:2])
        if not top:
            return

        freq_label = {1: "Giornaliero", 3: "Ogni 3 giorni", 7: "Settimanale"}.get(
            profile.get("alert_days", 0), ""
        )
        await context.bot.send_message(
            chat_id=telegram_id,
            text=f"🔔 <b>Alert {freq_label} — Le migliori offerte per te</b>",
            parse_mode="HTML",
        )
        for offer, res in top:
            url = offer.get("url", "")
            ral_change = (
                f"{res.get('ral_icon','📊')} <b>{res['ral_change']}</b> vs RAL attuale\n"
                if "ral_change" in res else ""
            )
            msg = (
                f"📌 <b>{offer['title']}</b>\n"
                f"🏢 {offer.get('company','N/D')} — <i>{offer.get('source','')}</i>\n\n"
                f"{res.get('summary','')}\n\n"
                f"💰 RAL: <b>{res.get('ral','N/D')}</b> {ral_change}"
                f"🎯 Successo: <b>{res.get('score','N/D')}</b>\n\n"
                f"🔗 {url}"
            )
            await context.bot.send_message(
                chat_id=telegram_id,
                text=msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        await update_profile(telegram_id, alert_last=datetime.now(tz=timezone.utc).isoformat())
        log.info("Alert inviato a %d: %d offerte", telegram_id, len(top))

    except Exception as e:
        log.warning("Alert job error per utente %d: %s", telegram_id, e)


async def _restore_alert_jobs(app: Application) -> None:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT telegram_id, alert_days FROM users WHERE alert_days > 0"
    )
    for row in rows:
        _schedule_alert(app.job_queue, row["telegram_id"], row["alert_days"])
        log.info("Alert ripristinato per utente %d (%d giorni)", row["telegram_id"], row["alert_days"])


# ---------------------------------------------------------------------------
# Handler Telegram
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Benvenuto su <b>Jobot</b>!\n\n"
        "Ecco cosa puoi fare:\n"
        "• 📄 Invia il tuo <b>CV in PDF</b> — lo analizzo e ti suggerisco 3 ricerche strategiche.\n"
        "• 💼 Incolla il testo di un'<b>offerta</b> — la confronto con il tuo CV.\n"
        "• /salva <code>Ruolo | RAL | Modalità</code> — salva una ricerca.\n"
        "• /ricerche — ricerche salvate.\n"
        "• /cerca — cerca offerte su 10 piattaforme in parallelo.\n"
        "• /mercato — analisi del tuo valore di mercato.\n"
        "• /analisi_cv — analisi approfondita CV vs mercato italiano.\n"
        "• /alert — configura alert automatici (giornaliero / 3 giorni / settimanale).\n"
        "• /connetti linkedin — collega il tuo account LinkedIn.\n"
        "• /profilo — visualizza la tua situazione attuale.\n"
        "• /aggiorna — aggiorna RAL, ruolo o obiettivo.\n"
        "• /reset — cancella tutti i tuoi dati.\n\n"
        "Inizia inviando il tuo CV!",
        parse_mode="HTML",
    )


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        cv_text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()

        if not cv_text:
            await update.message.reply_text(
                "Non sono riuscito a estrarre testo dal PDF. "
                "Prova con un PDF non scansionato (testo selezionabile)."
            )
            return

        await save_cv(telegram_id, cv_text)
        log.info("CV salvato per utente %d (%d caratteri)", telegram_id, len(cv_text))

        context.user_data["awaiting_ral"] = True
        await update.message.reply_text(
            "✅ CV salvato!\n\n"
            "💰 Qual è la tua <b>RAL attuale</b> (€/anno)?\n"
            "Es: <code>45.000</code> oppure <code>45k</code>\n\n"
            "<i>Scrivi</i> <code>0</code> <i>per saltare questo passaggio.</i>",
            parse_mode="HTML",
        )

    except Exception as e:
        log.exception("Errore nella gestione del PDF")
        await update.message.reply_text(f"Errore durante l'elaborazione: {e}")


async def cmd_salva(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    args_text = " ".join(context.args).strip()

    parts = [p.strip() for p in args_text.split("|")]
    if len(parts) < 1 or not parts[0]:
        await update.message.reply_text(
            "Formato: <code>/salva Ruolo | RAL | Modalità</code>\n"
            "Esempio: <code>/salva Sviluppatore Python | 45.000€ | Remoto</code>",
            parse_mode="HTML",
        )
        return

    role = parts[0]
    ral  = parts[1] if len(parts) > 1 else ""
    mode = parts[2] if len(parts) > 2 else ""

    await save_search(telegram_id, role, ral, mode)

    rows = await get_searches(telegram_id)
    lista = "\n".join(f"• {r[0]} | {r[1]} | {r[2]}" for r in rows)
    await update.message.reply_text(
        f"✅ Ricerca salvata!\n\n<b>Le tue ricerche:</b>\n{lista}",
        parse_mode="HTML",
    )


async def cmd_ricerche(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    rows = await get_searches(telegram_id)

    if not rows:
        await update.message.reply_text(
            "Non hai ancora ricerche salvate.\n"
            "Usa <code>/salva Ruolo | RAL | Modalità</code> per aggiungerne una.",
            parse_mode="HTML",
        )
        return

    lines = [f"{i+1}. <b>{r[0]}</b> | {r[1]} | {r[2]}" for i, r in enumerate(rows)]
    await update.message.reply_text(
        "📋 <b>Le tue ricerche salvate:</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    cv_exists = await get_cv(telegram_id) is not None
    searches  = await get_searches(telegram_id)

    if not cv_exists and not searches:
        await update.message.reply_text("Non ho dati da cancellare per te.")
        return

    riepilogo = []
    if cv_exists:
        riepilogo.append("il tuo CV")
    if searches:
        riepilogo.append(f"{len(searches)} ricerca/e salvata/e")

    context.user_data["reset_pending"] = True
    await update.message.reply_text(
        f"⚠️ Stai per cancellare {' e '.join(riepilogo)}.\n\n"
        "Rispondi <b>SI</b> per confermare o qualsiasi altra cosa per annullare.",
        parse_mode="HTML",
    )


async def cmd_profilo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    cv_text  = await get_cv(telegram_id)
    profile  = await get_user_profile(telegram_id)
    searches = await get_searches(telegram_id)

    cv_status   = "✅ caricato" if cv_text else "❌ non caricato"
    ral_status  = f"{profile['ral']:,}€/anno".replace(",", ".") if profile["ral"] else "non impostata"
    role_status = profile["role"] or "non impostato"
    goal_status = profile["goal"] or "non impostato"
    li_status   = f"✅ {profile['linkedin_email']}" if profile.get("linkedin_email") else "❌ non collegato"
    alert_label = {0: "🔕 disattivato", 1: "🔔 ogni giorno", 3: "🔔 ogni 3 giorni", 7: "🔔 ogni settimana"}
    alert_status = alert_label.get(profile.get("alert_days", 0), "🔕 disattivato")

    await update.message.reply_text(
        f"👤 <b>Il tuo profilo</b>\n\n"
        f"📄 CV: {cv_status}\n"
        f"💼 Ruolo attuale: {role_status}\n"
        f"💰 RAL attuale: {ral_status}\n"
        f"🎯 Obiettivo: {goal_status}\n"
        f"🔗 LinkedIn: {li_status}\n"
        f"🔍 Ricerche salvate: {len(searches)}\n"
        f"🔔 Alert: {alert_status}\n\n"
        f"<i>Usa /aggiorna per modificare il profilo.\n"
        f"Usa /connetti linkedin per collegare il tuo account.</i>",
        parse_mode="HTML",
    )


async def cmd_aggiorna(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id

    if not await get_cv(telegram_id):
        await update.message.reply_text("Carica prima il tuo CV in PDF!")
        return

    args = context.args
    if not args:
        context.user_data["aggiorna_step"] = "ral"
        profile = await get_user_profile(telegram_id)
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

    sub   = args[0].lower()
    value = " ".join(args[1:]).strip()

    if sub == "ral":
        ral = parse_ral(value)
        if not ral:
            await update.message.reply_text("RAL non valida. Es: /aggiorna ral 45000")
            return
        await update_profile(telegram_id, current_ral=ral)
        await update.message.reply_text(f"✅ RAL aggiornata: {ral:,}€/anno".replace(",", "."))

    elif sub == "ruolo":
        if not value:
            await update.message.reply_text("Specifica il ruolo. Es: /aggiorna ruolo Senior Developer")
            return
        await update_profile(telegram_id, user_role=value)
        await update.message.reply_text(f"✅ Ruolo aggiornato: {value}")

    elif sub == "obiettivo":
        if not value:
            await update.message.reply_text("Specifica l'obiettivo. Es: /aggiorna obiettivo aumentare RAL del 20%")
            return
        await update_profile(telegram_id, goal=value)
        await update.message.reply_text(f"✅ Obiettivo aggiornato: {value}")

    else:
        await update.message.reply_text(
            "Campi disponibili: <code>ral</code>, <code>ruolo</code>, <code>obiettivo</code>\n"
            "Oppure usa /aggiorna senza argomenti per il flusso guidato.",
            parse_mode="HTML",
        )


async def cmd_cerca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id

    cv_text = await get_cv(telegram_id)
    if not cv_text:
        await update.message.reply_text("Carica prima il tuo CV in PDF!")
        return

    profile     = await get_user_profile(telegram_id)
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

    searches = await get_searches(telegram_id)
    if not searches:
        await update.message.reply_text(
            "Non hai ricerche salvate.\n"
            "Usa <code>/salva Ruolo | RAL | Modalità</code> per aggiungerne una.",
            parse_mode="HTML",
        )
        return

    sources = [
        "Indeed", "InfoJobs", "Jooble", "RemoteOK",
        "Adzuna", "Talent.com", "Remotive", "WeWorkRemotely",
        "Monster", "LinkedIn",
    ]
    counts     = {s: 0 for s in sources}
    all_offers: list[dict] = []

    await update.message.reply_text(f"🔍 Cerco su {len(sources)} piattaforme in parallelo…")

    for role, _ral, mode, _ in searches[:3]:
        loc = "" if "remoto" in mode.lower() else "Italia"

        cached = await get_cached_offers(role, loc)
        if cached is not None:
            log.info("Cache hit per '%s' in '%s'", role, loc)
            for offer in cached[:5]:
                src = offer.get("source", "")
                if src in counts:
                    counts[src] += 1
            all_offers.extend(cached[:5])
            continue

        results = await asyncio.gather(
            asyncio.to_thread(fetch_indeed,         role, loc),
            asyncio.to_thread(fetch_infojobs,       role),
            asyncio.to_thread(fetch_jooble,         role, loc, JOOBLE_API_KEY),
            asyncio.to_thread(fetch_remoteok,       role),
            asyncio.to_thread(fetch_adzuna,         role, loc, ADZUNA_APP_ID, ADZUNA_APP_KEY),
            asyncio.to_thread(fetch_talent,         role, loc or "Italy"),
            asyncio.to_thread(fetch_remotive,       role),
            asyncio.to_thread(fetch_weworkremotely, role),
            asyncio.to_thread(fetch_monster,        role),
            asyncio.to_thread(fetch_linkedin, role, loc,
                              profile.get("linkedin_email", ""), profile.get("linkedin_pw", "")),
        )
        batch_all = []
        for source, batch in zip(sources, results):
            counts[source] += len(batch)
            all_offers.extend(batch[:5])
            batch_all.extend(batch)
        await set_cached_offers(role, loc, batch_all)

    all_offers = deduplicate(all_offers)[:12]
    total      = sum(counts.values())
    to_analyze = min(5, len(all_offers))

    active = [f"• {s}: <b>{counts[s]}</b>" for s in sources if counts[s] > 0]
    empty  = [s for s in sources if counts[s] == 0]
    report = "\n".join(active)
    if empty:
        report += f"\n• Nessun risultato: <i>{', '.join(empty)}</i>"

    await update.message.reply_text(
        f"📊 <b>Risultati ricerca:</b>\n{report}\n"
        f"─────────────────\n"
        f"• Totale trovate: <b>{total}</b> ({len(all_offers)} uniche)\n"
        f"• Analizzate con AI: <b>{to_analyze}</b>",
        parse_mode="HTML",
    )

    if not all_offers:
        await update.message.reply_text(
            "Nessuna offerta trovata. Prova a modificare le ricerche salvate con /salva o riprova più tardi."
        )
        return

    to_analyze    = min(8, len(all_offers))
    progress_msg  = await update.message.reply_text(f"⚙️ Analizzo {to_analyze} offerte con AI… 0/{to_analyze}")
    sem           = asyncio.Semaphore(3)
    completed     = [0]

    async def analyze_one(offer: dict) -> dict:
        offer_text = (
            f"Titolo: {offer['title']}\n"
            f"Azienda: {offer.get('company','')}\n"
            f"Descrizione: {offer.get('description','')}"
        )
        async with sem:
            result = await asyncio.to_thread(quick_analyze_offer, cv_text, offer_text, current_ral)
        completed[0] += 1
        try:
            await progress_msg.edit_text(
                f"⚙️ Analizzo {to_analyze} offerte con AI… {completed[0]}/{to_analyze}"
            )
        except Exception:
            pass
        return result

    analyses = await asyncio.gather(*[analyze_one(o) for o in all_offers[:to_analyze]])
    await progress_msg.edit_text(f"✅ Analisi completata — {to_analyze} offerte valutate.")

    scored = sorted(
        zip(all_offers, analyses),
        key=lambda x: x[1].get("score_int", 0),
        reverse=True,
    )

    lines = []
    for i, (offer, res) in enumerate(scored, 1):
        url        = offer.get("url", "")
        score      = res.get("score", "?")
        company    = offer.get("company", "")
        source     = offer.get("source", "")
        title_part = f'<a href="{url}">{offer["title"]}</a>' if url else offer["title"]
        company_part = f" — {company}" if company else ""
        lines.append(f'{i}. {title_part}{company_part} <i>[{source}]</i> 🎯{score}')

    BLOCK = 30
    for start in range(0, len(lines), BLOCK):
        chunk  = lines[start:start + BLOCK]
        header = f"📋 <b>Tutte le offerte ({len(scored)})</b>\n\n" if start == 0 else ""
        await update.message.reply_text(
            header + "\n".join(chunk),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    top5 = [(o, r) for o, r in scored if r.get("score_int", 0) >= 50][:5]

    if top5:
        await update.message.reply_text(f"🏆 <b>Top {len(top5)} per il tuo profilo:</b>", parse_mode="HTML")
        for offer, res in top5:
            url        = offer.get("url", "")
            ral_change = (
                f"{res.get('ral_icon','📊')} <b>{res['ral_change']}</b> vs RAL attuale\n"
                if "ral_change" in res else ""
            )
            motivazioni = res.get("motivazioni", "").strip()
            mot_block   = f"\n✅ <b>Perché fa per te:</b>\n{motivazioni}\n" if motivazioni else ""
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
    telegram_id = update.effective_user.id
    args = context.args

    if not args or args[0].lower() != "linkedin":
        await update.message.reply_text(
            "Servizi disponibili:\n• <code>/connetti linkedin</code>",
            parse_mode="HTML",
        )
        return

    profile = await get_user_profile(telegram_id)
    status  = f"Account attuale: <b>{profile['linkedin_email']}</b>\n\n" if profile.get("linkedin_email") else ""

    context.user_data["connetti_step"] = "linkedin_email"
    await update.message.reply_text(
        f"🔗 <b>Collega LinkedIn</b>\n\n"
        f"{status}"
        "Scrivi la tua <b>email LinkedIn</b>:\n\n"
        "<i>Le credenziali vengono salvate solo nel database e non vengono mai condivise o trasmesse a terzi.</i>",
        parse_mode="HTML",
    )


async def cmd_mercato(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    profile     = await get_user_profile(telegram_id)

    if not profile["role"] and not await get_cv(telegram_id):
        await update.message.reply_text(
            "Carica prima il tuo CV e imposta il ruolo attuale con /aggiorna ruolo."
        )
        return

    role        = profile["role"] or "professionista"
    current_ral = profile["ral"]

    if not current_ral:
        context.user_data["aggiorna_step"]   = "ral"
        context.user_data["pending_mercato"] = True
        await update.message.reply_text(
            "💰 Per l'analisi di mercato ho bisogno della tua <b>RAL attuale</b>.\n\n"
            "Scrivi l'importo (es. <code>45000</code>) oppure <code>0</code> per saltare.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(f"📡 Scansiono il mercato per <b>{role}</b>…", parse_mode="HTML")

    sources_results = await asyncio.gather(
        asyncio.to_thread(fetch_jooble,   role, "Italia", JOOBLE_API_KEY),
        asyncio.to_thread(fetch_adzuna,   role, "Italia", ADZUNA_APP_ID, ADZUNA_APP_KEY),
        asyncio.to_thread(fetch_remotive, role),
        asyncio.to_thread(fetch_talent,   role, "Italy"),
        asyncio.to_thread(fetch_remoteok, role),
        asyncio.to_thread(fetch_linkedin, role, "Italia",
                          profile.get("linkedin_email", ""), profile.get("linkedin_pw", "")),
    )

    all_offers = deduplicate([o for batch in sources_results for o in batch])
    num_offers = len(all_offers)

    if not all_offers:
        await update.message.reply_text(
            "⚠️ Nessuna offerta trovata per questo ruolo al momento. "
            "Il mercato potrebbe essere molto di nicchia o le piattaforme non disponibili."
        )
        return

    sample           = all_offers[:6]
    mercato_progress = await update.message.reply_text(
        f"🔢 Trovate <b>{num_offers}</b> offerte. Calcolo RAL di mercato… 0/{len(sample)}",
        parse_mode="HTML",
    )

    sem_m  = asyncio.Semaphore(3)
    done_m = [0]

    async def analyze_market_offer(offer: dict) -> dict:
        offer_text = f"Titolo: {offer['title']}\nDescrizione: {offer.get('description','')}"
        async with sem_m:
            result = await asyncio.to_thread(quick_analyze_offer, "", offer_text, 0)
        done_m[0] += 1
        try:
            await mercato_progress.edit_text(
                f"🔢 Trovate <b>{num_offers}</b> offerte. Calcolo RAL di mercato… {done_m[0]}/{len(sample)}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return result

    quick_analyses = await asyncio.gather(*[analyze_market_offer(o) for o in sample])
    await mercato_progress.edit_text("✅ Campione analizzato — genero il report strategico…", parse_mode="HTML")

    ral_values = []
    for a in quick_analyses:
        ral_min = a.get("ral_min") or 0
        ral_max = a.get("ral_max") or 0
        if isinstance(ral_min, (int, float)) and isinstance(ral_max, (int, float)):
            if ral_min > 0 and ral_max > 0:
                ral_values.append(int((ral_min + ral_max) / 2))

    report  = await asyncio.to_thread(generate_market_report, profile, num_offers, ral_values)
    avg_ral = int(sum(ral_values) / len(ral_values)) if ral_values else 0
    ral_min_m = min(ral_values) if ral_values else 0
    ral_max_m = max(ral_values) if ral_values else 0

    ral_line = f"💰 La tua RAL:     <b>{current_ral:,}€</b>\n".replace(",", ".") if current_ral else ""
    market_ral_line = (
        f"📊 Media mercato:  <b>{avg_ral:,}€</b> ({report.get('delta','N/D')})\n"
        f"📉 Range mercato:  {ral_min_m:,}€ – {ral_max_m:,}€\n"
    ).replace(",", ".") if avg_ral else ""

    domanda_emoji = {"alta": "🔥", "media": "📈", "bassa": "❄️"}.get(
        report.get("domanda", "").lower(), "📊"
    )
    azioni       = report.get("azioni", [])
    azioni_block = "\n".join(f"  • {a}" for a in azioni) if azioni else ""
    segnale      = report.get("segnale", "N/D")
    emoji        = report.get("emoji", "⚪")

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


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id

    if not await get_cv(telegram_id):
        await update.message.reply_text("Carica prima il tuo CV in PDF!")
        return

    searches = await get_searches(telegram_id)
    if not searches:
        await update.message.reply_text(
            "Non hai ricerche salvate.\n"
            "Usa <code>/salva Ruolo | RAL | Modalità</code> per aggiungerne una prima di attivare gli alert.",
            parse_mode="HTML",
        )
        return

    profile = await get_user_profile(telegram_id)
    days    = profile.get("alert_days", 0)
    label   = {1: "ogni giorno", 3: "ogni 3 giorni", 7: "ogni settimana"}.get(days, "")
    status  = f"🔔 Attivo: <b>{label}</b>" if days else "🔕 <b>Disattivato</b>"

    keyboard = [
        [
            InlineKeyboardButton("📅 Ogni giorno",    callback_data="alert_1"),
            InlineKeyboardButton("📅 Ogni 3 giorni",  callback_data="alert_3"),
        ],
        [
            InlineKeyboardButton("📅 Ogni settimana", callback_data="alert_7"),
            InlineKeyboardButton("❌ Disattiva",       callback_data="alert_0"),
        ],
    ]
    await update.message.reply_text(
        f"🔔 <b>Gestione Alert</b>\n\n"
        f"Stato attuale: {status}\n\n"
        f"Con l'alert ricevi automaticamente le migliori offerte trovate "
        f"per le tue <b>{len(searches)}</b> ricerche salvate.\n\n"
        f"Scegli la frequenza:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def callback_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    telegram_id = update.effective_user.id
    days        = int(query.data.split("_")[1])

    for job in context.job_queue.get_jobs_by_name(f"alert_{telegram_id}"):
        job.schedule_removal()

    await update_profile(telegram_id, alert_days=days, alert_last="")

    if days > 0:
        _schedule_alert(context.job_queue, telegram_id, days)
        labels = {1: "ogni giorno", 3: "ogni 3 giorni", 7: "ogni settimana"}
        await query.edit_message_text(
            f"✅ Alert impostato: <b>{labels[days]}</b>\n\n"
            f"Riceverai le migliori offerte automaticamente.\n"
            f"Usa /alert per modificare o disattivare.",
            parse_mode="HTML",
        )
    else:
        await query.edit_message_text(
            "🔕 Alert disattivato.\n\nUsa /alert per riattivarlo quando vuoi."
        )


async def cmd_analisi_cv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    cv_text     = await get_cv(telegram_id)

    if not cv_text:
        await update.message.reply_text("Carica prima il tuo CV in PDF!")
        return

    profile = await get_user_profile(telegram_id)
    role    = profile.get("role") or "professionista"

    progress = await update.message.reply_text(
        f"🔬 <b>Analisi CV vs Mercato</b>\n\n"
        f"📡 Raccogliendo offerte e dati osservatori per <b>{role}</b>…",
        parse_mode="HTML",
    )

    (job_results, insights) = await asyncio.gather(
        asyncio.gather(
            asyncio.to_thread(fetch_jooble,   role, "Italia", JOOBLE_API_KEY),
            asyncio.to_thread(fetch_adzuna,   role, "Italia", ADZUNA_APP_ID, ADZUNA_APP_KEY),
            asyncio.to_thread(fetch_remotive, role),
            asyncio.to_thread(fetch_talent,   role, "Italy"),
            asyncio.to_thread(fetch_remoteok, role),
        ),
        asyncio.to_thread(fetch_market_insights, role),
    )

    all_offers = deduplicate([o for batch in job_results for o in batch])[:10]
    num_offers = len(all_offers)

    await progress.edit_text(
        f"🔬 <b>Analisi CV vs Mercato</b>\n\n"
        f"✅ {num_offers} offerte raccolte\n"
        f"✅ {len(insights)} articoli osservatori\n"
        f"⚙️ Confronto CV con requisiti di mercato…",
        parse_mode="HTML",
    )

    offers_text = "\n---\n".join(
        f"Titolo: {o['title']}\nDescrizione: {o.get('description','')[:600]}"
        for o in all_offers[:6]
    ) if all_offers else "Nessuna offerta disponibile."

    insights_text = "\n".join(
        f"- [{item.get('source','')}] {item['title']}: {item.get('description','')[:200]}"
        for item in insights[:5]
    ) if insights else "Nessun dato osservatori disponibile."

    system = (
        "Sei un career coach esperto del mercato del lavoro italiano. "
        "Analizza il CV rispetto al mercato generale del ruolo specificato in Italia. "
        "IMPORTANTE: lo score e la RAL devono riflettere il mercato GENERALE italiano per quel ruolo, "
        "non le offerte specifiche fornite (che potrebbero essere non pertinenti). "
        "Usa la tua conoscenza del mercato italiano + i dati degli osservatori forniti. "
        "Rispondi SOLO con JSON valido, nessun testo fuori:\n"
        '{"score_mercato":"X%",'
        '"score_spiegazione":"2-3 frasi: cosa significa questo score, su cosa si basa (seniority, skill, domanda di mercato) e cosa implica per la carriera",'
        '"ral_range":"es. 35.000–55.000€",'
        '"ral_spiegazione":"2-3 frasi: contesto RAL per questo ruolo in Italia, variazioni per settore/città/seniority, confronto con il profilo del candidato",'
        '"forze":["punto di forza 1","punto di forza 2","punto di forza 3"],'
        '"lacune":["skill mancante 1","skill mancante 2","skill mancante 3"],'
        '"keywords_ats":["keyword1","keyword2","keyword3","keyword4","keyword5"],'
        '"trend":"2-3 frasi sui trend del ruolo in Italia: domanda, settori in crescita, tecnologie richieste",'
        '"azioni_30gg":["azione concreta 1","azione concreta 2","azione concreta 3"]}'
    )
    user_content = (
        f"RUOLO TARGET: {role}\n\n"
        f"CV (estratto):\n{cv_text[:2000]}\n\n"
        f"OFFERTE TROVATE ({num_offers}, usa solo come contesto secondario):\n{offers_text[:2000]}\n\n"
        f"DATI OSSERVATORI ITALIANI (priorità alta per score e trend):\n{insights_text}"
    )

    raw = await asyncio.to_thread(_call_groq, system, user_content, 700)

    try:
        start = raw.index("{")
        end   = raw.rindex("}") + 1
        data  = json.loads(raw[start:end])
    except Exception:
        data = {
            "score_mercato": "N/D", "score_spiegazione": "",
            "ral_range": "N/D", "ral_spiegazione": "",
            "forze": [], "lacune": [], "keywords_ats": [],
            "trend": raw[:300], "azioni_30gg": [],
        }

    score             = data.get("score_mercato", "N/D")
    score_spiegazione = data.get("score_spiegazione", "")
    ral_range         = data.get("ral_range", "N/D")
    ral_spiegazione   = data.get("ral_spiegazione", "")
    forze    = data.get("forze", [])
    lacune   = data.get("lacune", [])
    keywords = data.get("keywords_ats", [])
    trend    = data.get("trend", "")
    azioni   = data.get("azioni_30gg", [])

    forze_block    = "\n".join(f"  ✅ {f}" for f in forze)  if forze  else "  N/D"
    lacune_block   = "\n".join(f"  ⚠️ {l}" for l in lacune) if lacune else "  Nessuna lacuna rilevante"
    keywords_block = " · ".join(f"<code>{k}</code>" for k in keywords) if keywords else "N/D"
    azioni_block   = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(azioni)) if azioni else "  N/D"

    sources_lines = []
    seen_sources: set[str] = set()
    for item in insights[:5]:
        src = item.get("source", "")
        url = item.get("url", "")
        if src and src not in seen_sources:
            seen_sources.add(src)
            sources_lines.append(f'<a href="{url}">{src}</a>' if url else src)
    sources_block = "  📚 <i>Fonti: " + " · ".join(sources_lines) + "</i>" if sources_lines else ""

    score_block = f"\n<i>{score_spiegazione}</i>" if score_spiegazione else ""
    ral_block   = f"\n<i>{ral_spiegazione}</i>"   if ral_spiegazione   else ""

    msg = (
        f"🔬 <b>ANALISI CV vs MERCATO — {role.upper()}</b>\n\n"
        f"📊 <b>Score di mercato: {score}</b>{score_block}\n\n"
        f"💰 <b>Range RAL: {ral_range}</b>{ral_block}\n\n"
        f"🔢 Offerte di riferimento: {num_offers}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>Punti di forza:</b>\n{forze_block}\n\n"
        f"⚠️ <b>Lacune da colmare:</b>\n{lacune_block}\n\n"
        f"🔑 <b>Keyword ATS per il CV:</b>\n{keywords_block}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>Trend di mercato:</b>\n  {trend}\n"
        f"{sources_block}\n\n"
        f"⚡ <b>Piano d'azione 30 giorni:</b>\n{azioni_block}"
    )
    await progress.edit_text(msg, parse_mode="HTML")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    offer_text  = update.message.text.strip()

    # RAL dopo upload CV
    if context.user_data.get("awaiting_ral"):
        context.user_data["awaiting_ral"] = False
        ral = parse_ral(offer_text)
        if ral > 0:
            await update_profile(telegram_id, current_ral=ral)
            await update.message.reply_text(
                f"✅ RAL attuale salvata: <b>{ral:,}€/anno</b>\n\nCalcolo i suggerimenti…".replace(",", "."),
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("RAL non salvata. Calcolo i suggerimenti…")
        cv_text = await get_cv(telegram_id)
        if cv_text:
            profile     = await get_user_profile(telegram_id)
            suggestions = await asyncio.to_thread(suggest_searches, cv_text, profile)
            await update.message.reply_text(
                f"🎯 <b>Suggerimenti di ricerca strategici:</b>\n\n{suggestions}\n\n"
                "Usa <code>/salva Ruolo | RAL | Modalità</code> per registrarne una.",
                parse_mode="HTML",
            )
        return

    # Flusso collegamento LinkedIn
    if context.user_data.get("connetti_step"):
        step = context.user_data["connetti_step"]

        if step == "linkedin_email":
            context.user_data["linkedin_email_tmp"] = offer_text.strip()
            context.user_data["connetti_step"]      = "linkedin_pw"
            await update.message.reply_text(
                "🔑 Ora scrivi la tua <b>password LinkedIn</b>.\n\n"
                "⚠️ <i>Il messaggio verrà eliminato subito dopo la lettura.</i>",
                parse_mode="HTML",
            )
            return

        if step == "linkedin_pw":
            context.user_data.pop("connetti_step", None)
            email = context.user_data.pop("linkedin_email_tmp", "")
            pw    = offer_text.strip()
            try:
                await update.message.delete()
            except Exception:
                pass
            if email and pw:
                await save_linkedin_creds(telegram_id, email, pw)
                await update.message.reply_text(
                    f"✅ LinkedIn collegato come <b>{email}</b>.\n"
                    "Il messaggio con la password è stato eliminato dalla chat.",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text("Credenziali non valide. Riprova con /connetti linkedin.")
            return

    # Flusso guidato /aggiorna
    if context.user_data.get("aggiorna_step"):
        step          = context.user_data.pop("aggiorna_step")
        pending_cerca = context.user_data.pop("pending_cerca", False)
        val           = offer_text.strip()

        if step == "ral":
            if val.lower() != "skip":
                ral = parse_ral(val)
                if ral:
                    await update_profile(telegram_id, current_ral=ral)
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
            profile  = await get_user_profile(telegram_id)
            role_str = profile["role"] or "non impostato"
            context.user_data["aggiorna_step"] = "ruolo"
            await update.message.reply_text(
                f"<b>Step 2/3 — Ruolo attuale</b>\nValore attuale: {role_str}\n\n"
                "Scrivi il tuo ruolo (es. <code>Data Analyst</code>) o <code>skip</code>.",
                parse_mode="HTML",
            )

        elif step == "ruolo":
            if val.lower() != "skip" and val:
                await update_profile(telegram_id, user_role=val)
                await update.message.reply_text(f"✅ Ruolo: <b>{val}</b>", parse_mode="HTML")
            profile  = await get_user_profile(telegram_id)
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
                await update_profile(telegram_id, goal=val)
                await update.message.reply_text(f"✅ Obiettivo: <b>{val}</b>", parse_mode="HTML")
            await update.message.reply_text(
                "🎉 Profilo aggiornato! Usa /profilo per vederlo o /cerca per trovare offerte."
            )
        return

    # Conferma reset
    if context.user_data.get("reset_pending"):
        context.user_data["reset_pending"] = False
        if offer_text.strip().upper() == "SI":
            await delete_user_data(telegram_id)
            await update.message.reply_text("✅ Dati cancellati. Puoi inviare un nuovo CV quando vuoi.")
        else:
            await update.message.reply_text("Operazione annullata. I tuoi dati sono al sicuro.")
        return

    if len(offer_text) < 80:
        await update.message.reply_text(
            "Testo troppo corto per essere un'offerta. "
            "Incolla il testo completo dell'annuncio."
        )
        return

    cv_text = await get_cv(telegram_id)
    if not cv_text:
        await update.message.reply_text("Non ho ancora il tuo CV. Inviamelo prima come PDF!")
        return

    await update.message.reply_text("🔍 Analizzo l'offerta con il tuo CV…")

    try:
        analysis = await asyncio.to_thread(analyze_offer, cv_text, offer_text)
        await update.message.reply_text(analysis, parse_mode="Markdown")
    except Exception as e:
        log.exception("Errore nell'analisi dell'offerta")
        await update.message.reply_text(f"Errore durante l'analisi: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _post_init(app: Application) -> None:
    await init_db()
    await _restore_alert_jobs(app)


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("salva",      cmd_salva))
    app.add_handler(CommandHandler("ricerche",   cmd_ricerche))
    app.add_handler(CommandHandler("cerca",      cmd_cerca))
    app.add_handler(CommandHandler("connetti",   cmd_connetti))
    app.add_handler(CommandHandler("mercato",    cmd_mercato))
    app.add_handler(CommandHandler("profilo",    cmd_profilo))
    app.add_handler(CommandHandler("aggiorna",   cmd_aggiorna))
    app.add_handler(CommandHandler("reset",      cmd_reset))
    app.add_handler(CommandHandler("alert",      cmd_alert))
    app.add_handler(CommandHandler("analisi_cv", cmd_analisi_cv))
    app.add_handler(CallbackQueryHandler(callback_alert, pattern=r"^alert_\d+$"))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if WEBHOOK_URL:
        log.info("Jobot in modalità webhook — porta %d", PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("Jobot in modalità polling (locale)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
