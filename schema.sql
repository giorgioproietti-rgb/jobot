-- Jobot — schema PostgreSQL per Supabase
-- Esegui questo script una sola volta nel SQL Editor di Supabase.

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
