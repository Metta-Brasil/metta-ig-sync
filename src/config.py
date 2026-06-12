"""Centralized configuration for metta-ig-sync."""

import os
from typing import Any, Dict, List

SPREADSHEET_ID = "1m_oCjgeMPfEaplFNvK0oLRwYR9yoS3AkYzWkO3uWXvY"

IG_BASE_URL = "https://graph.facebook.com/v21.0"

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

ACCOUNTS: List[Dict[str, Any]] = [
    {
        "name": "metta",
        "user_id": os.environ.get("METTA_INSTAGRAM_USER_ID", "17841448769635737"),
        "sheet_profile": "ig_metta_perfil",
        "sheet_posts": "ig_metta_posts",
    },
    {
        "name": "tiago",
        "user_id": os.environ.get("TIAGO_INSTAGRAM_USER_ID", "17841410183183165"),
        "sheet_profile": "ig_tiago_perfil",
        "sheet_posts": "ig_tiago_posts",
    },
]

# Profile snapshot columns
# Colunas F-H adicionadas em 2026-06 — dashboard continua lendo A:E sem quebrar
PROFILE_COLUMNS: List[Dict[str, str]] = [
    {"header": "Data",                    "key": "date",                   "format": "dd/MM/yyyy"},
    {"header": "Seguidores",              "key": "followers",              "format": "0"},
    {"header": "Seguindo",               "key": "following",              "format": "0"},
    {"header": "Posts",                  "key": "posts",                  "format": "0"},
    {"header": "Alcance 28d",            "key": "reach_28d",              "format": "0"},
    {"header": "Alcance Dia",            "key": "alcance_dia",            "format": "0"},
    {"header": "Contas Engajadas 28d",   "key": "contas_engajadas_28d",   "format": "0"},
    {"header": "Interações 28d",         "key": "interacoes_totais_28d",  "format": "0"},
]

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
]

RETRY_BASE_SECONDS = 5
RETRY_MAX_ATTEMPTS = 4
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
