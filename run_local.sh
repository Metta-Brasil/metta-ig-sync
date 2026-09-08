#!/bin/zsh
# Job local (Mac) da coleta de impulsionados — modo BOOSTED_ONLY.
# Existe porque o Instagram rejeita a sessão web vinda do IP do runner do
# GitHub; daqui, mesmo IP do navegador, ela passa. Segredos vêm de ~/.mcp-env
# e nunca são impressos.
set -euo pipefail
cd "$(dirname "$0")"
envval() { grep -m1 "^$2=" "$HOME/.mcp-env/$1" | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

export INSTAGRAM_ACCESS_TOKEN="$(envval ig_tiago.env INSTAGRAM_ACCESS_TOKEN)"
export TIAGO_INSTAGRAM_USER_ID="$(envval ig_tiago.env INSTAGRAM_USER_ID)"
export METTA_INSTAGRAM_USER_ID="$(envval ig_metta.env INSTAGRAM_USER_ID)"
export META_ADS_ACCESS_TOKEN="$(envval meta-ads.env META_ACCESS_TOKEN)"
export IG_SESSIONID_TIAGO="$(envval ig_web.env IG_SESSIONID_TIAGO)"
export IG_SESSIONID_METTA="$(envval ig_web.env IG_SESSIONID_METTA)"
export IG_COOKIES_TIAGO="$(envval ig_web.env IG_COOKIES_TIAGO)"
export IG_COOKIES_METTA="$(envval ig_web.env IG_COOKIES_METTA)"
export GOOGLE_OAUTH_CREDENTIALS_FILE="$HOME/.google_workspace_mcp/credentials/alisson.oliveira@mettabrasil.com.br.json"
export BOOSTED_ONLY=true
export DRY_RUN="${DRY_RUN:-false}"

mkdir -p logs
exec .venv/bin/python -m src.main >> "logs/local-$(date +%Y%m%d).log" 2>&1
