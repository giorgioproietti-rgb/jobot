# Jobot — Agente intelligente per la ricerca di lavoro

Bot Telegram (@gg_job_bot) che analizza il tuo CV e valuta le offerte di lavoro usando Claude AI.
Tutto locale: nessun cloud, nessuna configurazione complessa. Solo un database SQLite sul tuo PC.

---

## Requisiti

- Python 3.10+
- Un token per il bot Telegram (`@gg_job_bot`, ottenuto via @BotFather)
- Una chiave API Anthropic (https://console.anthropic.com)

---

## Installazione

```bash
git clone https://github.com/tuousername/jobot.git
cd jobot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## Configurazione variabili d'ambiente

Esporta le variabili nella shell prima di avviare, oppure crea un file `.env` (non tracciato da git).

```bash
export TELEGRAM_TOKEN="il_tuo_token_da_botfather"
export ANTHROPIC_API_KEY="sk-ant-..."
```

> **Nota:** non committare mai questi valori su GitHub. Aggiungi `.env` al tuo `.gitignore`.

---

## Avvio

```bash
python bot.py
```

Il bot si connette a Telegram e crea automaticamente il database `jobs.db` nella stessa cartella, con due tabelle:

| Tabella | Contenuto |
|---------|-----------|
| `users` | ID Telegram + testo estratto dal CV PDF |
| `searches` | Ricerche salvate (Ruolo, RAL, Modalità) per utente |

---

## Utilizzo

| Azione | Come |
|--------|------|
| Caricare il CV | Invia un file PDF direttamente in chat |
| Salvare una ricerca | `/salva Sviluppatore Python \| 45.000€ \| Remoto` |
| Analizzare un'offerta | Incolla il testo dell'annuncio in chat |
| Vedere aiuto | `/start` |

---

## Struttura del progetto

```
jobot/
├── bot.py           # Logica principale del bot
├── requirements.txt # Dipendenze Python
├── README.md        # Questo file
└── jobs.db          # Database SQLite (generato al primo avvio, non tracciato)
```

---

## Note

- Il modello usato è `claude-3-5-haiku-20241022` per bilanciare velocità e costo.
- Il CV viene memorizzato come testo estratto da PDF; i file originali non vengono conservati.
- Per resettare un utente, cancella la riga corrispondente da `jobs.db` con qualsiasi client SQLite.
