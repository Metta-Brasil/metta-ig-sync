"""Centralized configuration for metta-ig-sync."""

import os
from datetime import date
from typing import Any, Dict, List

SPREADSHEET_ID = "1m_oCjgeMPfEaplFNvK0oLRwYR9yoS3AkYzWkO3uWXvY"

IG_BASE_URL = "https://graph.facebook.com/v23.0"

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

ACCOUNTS: List[Dict[str, Any]] = [
    {
        "name": "metta",
        "user_id": os.environ.get("METTA_INSTAGRAM_USER_ID", "17841448769635737"),
        "sheet_profile": "ig_metta_perfil",
        "sheet_posts": "ig_metta_posts",
        "sheet_demographics": "ig_metta_demograficos",
        "sheet_stories": "ig_metta_stories",
    },
    {
        "name": "tiago",
        "user_id": os.environ.get("TIAGO_INSTAGRAM_USER_ID", "17841410183183165"),
        "sheet_profile": "ig_tiago_perfil",
        "sheet_posts": "ig_tiago_posts",
        "sheet_demographics": "ig_tiago_demograficos",
        "sheet_stories": "ig_tiago_stories",
    },
]

# Optional account scope: SYNC_ONLY="metta" runs just that account (used to roll
# out a change to one profile before the other). Empty → all accounts.
SYNC_ONLY = os.environ.get("SYNC_ONLY", "").strip().lower()

# Profile snapshot columns
# Colunas F-I são métricas DIÁRIAS da conta (não 28d). Headers G/H corrigidos e
# coluna I (Views) adicionada em 2026-06 via PROFILE_HEADER_RENAMES — dashboard
# continua lendo posicionalmente sem quebrar.
PROFILE_COLUMNS: List[Dict[str, str]] = [
    {"header": "Data",                    "key": "date",                   "format": "dd/MM/yyyy"},
    {"header": "Seguidores",              "key": "followers",              "format": "0"},
    {"header": "Seguindo",               "key": "following",              "format": "0"},
    {"header": "Posts",                  "key": "posts",                  "format": "0"},
    {"header": "Alcance 28d",            "key": "reach_28d",              "format": "0"},
    {"header": "Alcance Dia",            "key": "alcance_dia",            "format": "0"},
    {"header": "Contas Engajadas",       "key": "contas_engajadas_dia",   "format": "0"},
    {"header": "Interações",             "key": "interacoes_dia",         "format": "0"},
    {"header": "Views",                  "key": "views_dia",              "format": "0"},
]

# Header renames applied in-place by ensure_headers when migrating an existing
# sheet. Maps the OLD header text → the NEW header text. Lets the first run of
# the new code rename G/H and append I with no manual sheet edit and no broken
# cron window. Any divergence outside this map still raises (corruption guard).
PROFILE_HEADER_RENAMES: Dict[str, str] = {
    "Contas Engajadas 28d": "Contas Engajadas",
    "Interações 28d": "Interações",
}

# Daily-metric column keys (F-I), written by both the hourly partial upsert and
# the backfill. Order matters: matches columns F, G, H, I.
PROFILE_DAILY_KEYS: List[str] = [
    "alcance_dia",
    "contas_engajadas_dia",
    "interacoes_dia",
    "views_dia",
]

# Backfill ranges (proved via live API probes, both accounts):
#   - reach daily series available from 2025-01-01 (API keeps ~2 years)
#   - views / total_interactions non-zero from ~aug/2025
#   - accounts_engaged non-zero from ~nov/2025 (handled by sanitize, not a hard date)
BACKFILL_REACH_START = date(2025, 1, 1)
BACKFILL_TV_START = date(2025, 8, 1)

# Posts columns
# Coluna P (Hora) adicionada em 2026-06 — dashboard continua lendo A:O sem quebrar
POSTS_COLUMNS: List[Dict[str, str]] = [
    {"header": "Post ID",                "key": "post_id",            "format": "@"},
    {"header": "Data",                   "key": "date",               "format": "dd/MM/yyyy"},
    {"header": "Tipo",                   "key": "media_type",         "format": "@"},
    {"header": "Legenda",                "key": "caption",            "format": "@"},
    {"header": "Permalink",              "key": "permalink",          "format": "@"},
    {"header": "Thumbnail URL",          "key": "thumbnail_url",      "format": "@"},
    {"header": "Curtidas",               "key": "likes",              "format": "0"},
    {"header": "Comentarios",            "key": "comments",           "format": "0"},
    {"header": "Views",                  "key": "views",              "format": "0"},
    {"header": "Alcance",                "key": "reach",              "format": "0"},
    {"header": "Salvamentos",            "key": "saved",              "format": "0"},
    {"header": "Compartilhamentos",      "key": "shares",             "format": "0"},
    {"header": "Repostagens",            "key": "reposts",            "format": "0"},
    {"header": "Skip Rate %",            "key": "skip_rate",          "format": "0.00"},
    {"header": "Taxa Engajamento %",     "key": "engagement_rate",    "format": "0.00"},
    {"header": "Hora",                   "key": "hora",               "format": "@"},
    # Cols Q-R (2026-06): só FEED (IMAGE/CAROUSEL). Reels não suportam essas
    # métricas por mídia → 0. Dashboard continua lendo A:P sem quebrar.
    {"header": "Visitas Perfil",         "key": "profile_visits",     "format": "0"},
    {"header": "Seguidores",             "key": "follows",            "format": "0"},
]

# Demografia de seguidores (aba ig_*_demograficos, overwrite diário).
# Uma linha por (dimensao, chave). dimensao ∈ {idade_genero, cidade, pais}.
# chave de idade_genero é "<faixa>|<genero>" (ex. "25-34|F").
DEMOGRAPHICS_COLUMNS: List[Dict[str, str]] = [
    {"header": "Dimensao",    "key": "dimensao",    "format": "@"},
    {"header": "Chave",       "key": "chave",       "format": "@"},
    {"header": "Seguidores",  "key": "seguidores",  "format": "0"},
    {"header": "Coletado Em",  "key": "coletado_em",  "format": "dd/MM/yyyy"},
]

# Stories (aba ig_*_stories). APPEND-ONLY / UPSERT por Story ID — NUNCA limpa a
# aba: a API só devolve stories ativos (~24h), então sobrescrever perderia o
# histórico ao expirar. O sync horário pega cada story ~24x na janela; o upsert
# atualiza os insights enquanto o story está vivo e mantém a linha pra sempre.
# Retroativo não existe (stories expirados somem da API) — histórico começa na
# 1ª coleta.
STORIES_COLUMNS: List[Dict[str, str]] = [
    {"header": "Story ID",            "key": "story_id",            "format": "@"},
    {"header": "Data",                "key": "date",                "format": "dd/MM/yyyy"},
    {"header": "Hora",                "key": "hora",                "format": "@"},
    {"header": "Tipo",                "key": "media_type",          "format": "@"},
    {"header": "Permalink",           "key": "permalink",           "format": "@"},
    {"header": "Thumbnail URL",       "key": "thumbnail_url",       "format": "@"},
    {"header": "Views",               "key": "views",               "format": "0"},
    {"header": "Alcance",             "key": "reach",               "format": "0"},
    {"header": "Navegacao",           "key": "navigation",          "format": "0"},
    {"header": "Respostas",           "key": "replies",             "format": "0"},
    {"header": "Compartilhamentos",   "key": "shares",              "format": "0"},
    {"header": "Interacoes",          "key": "total_interactions",  "format": "0"},
    {"header": "Seguidores",          "key": "follows",             "format": "0"},
    {"header": "Visitas Perfil",      "key": "profile_visits",      "format": "0"},
    {"header": "Coletado Em",         "key": "coletado_em",         "format": "dd/MM/yyyy"},
]

RETRY_BASE_SECONDS = 5
RETRY_MAX_ATTEMPTS = 4
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
