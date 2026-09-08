"""Impulsionamentos: série diária de visitas ao perfil e seguidores por post.

Por que este módulo existe
--------------------------
A aba ig_*_posts guarda o ACUMULADO de cada post desde a publicação, e é
reescrita a cada execução. Serve pra saber "quanto esse post já rendeu",
mas não responde "quanto rendeu em julho": a Media Insights API não recorta
por data — passar since/until ou period=day é aceito e ignorado, a resposta
volta sempre com period=lifetime.

A saída daqui é uma linha por (dia, post). Durante o dia as execuções
atualizam a linha do dia; virou a data, abre linha nova. O ganho de um dia
é a diferença entre o fechamento dele e o do dia anterior — que é o número
que o dashboard mostra.

Isso só funciona daqui pra frente: snapshot que não foi tirado não se
reconstrói. Para o período anterior à primeira execução só existe o
acumulado.

De onde sai a lista de posts
----------------------------
Duas fontes, unidas:

1. Meta Ads API — o criativo de um impulsionamento expõe
   `source_instagram_media_id`, que aponta pro post ORIGINAL do perfil.
   Cobre os anúncios sozinho, ativos ou pausados, sem ninguém digitar nada.
   Não confundir com `effective_instagram_media_id`: esse é a CÓPIA que o
   Meta cria pro anúncio (media_product_type=AD), e a Insights API recusa
   profile_visits/follows para ela.

2. A coluna A da aba de entrada — pra forçar um post que a descoberta não
   pegou (conta de anúncio fora da lista, post de parceiro, etc). Colar o
   link basta; o resto das colunas o sync preenche.

Reels ficam sem visitas/seguidores: a API recusa a métrica para
media_product_type=REELS. A linha é gravada mesmo assim, com as células
vazias, pra diferenciar "não temos o dado" de "deu zero".
"""

import logging
import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import config
from .instagram import BRT

log = logging.getLogger(__name__)

_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)")
_TOKEN_RE = re.compile(r"access_token=[^&\s]+")

# Teto de páginas por conta de anúncio. Conta antiga tem milhares de anúncios;
# sem teto a descoberta viraria a parte mais cara do sync pra achar os mesmos
# poucos impulsionamentos, que são sempre os mais recentes.
MAX_PAGINAS_ADS = 12

# O Meta nomeia impulsionamento como "Post do Instagram: <início da legenda>".
# Sem esse filtro, QUALQUER anúncio que reaproveite uma mídia do perfil entra
# na lista (source_instagram_media_id existe em todos eles) e a aba enche de
# post que ninguém turbinou. Anúncio que escape do padrão entra pela coluna A.
_NOME_IMPULSIONAMENTO = ("post do instagram", "instagram post")


def is_impulsionamento(nome: str) -> bool:
    n = (nome or "").strip().lower()
    return any(n.startswith(p) for p in _NOME_IMPULSIONAMENTO)


def _redact(msg: Any) -> str:
    return _TOKEN_RE.sub("access_token=REDACTED", str(msg))


def extract_shortcode(link: str) -> str:
    """Shortcode de um permalink. '' se o texto não for um link de post."""
    m = _SHORTCODE_RE.search((link or "").strip())
    return m.group(1) if m else ""


# ----------------------------------------------------------------------
# Fonte 1: descoberta pelo Meta Ads
# ----------------------------------------------------------------------

def discover_from_ads(token: str) -> Dict[str, str]:
    """{media_id: nome do anúncio} de todo impulsionamento das contas.

    Varre as contas de anúncio do usuário do token e devolve os
    `source_instagram_media_id` distintos. Inclui anúncio pausado: o
    histórico do post continua importando depois que a verba para.

    O nome do anúncio vem junto porque é a única chave que liga este post à
    linha de investimento no `fb_todos` (que não carrega media_id). Sem ele,
    o dashboard teria que casar post e campanha por pedaço de legenda.

    Falha em silêncio (lista vazia) — sem token de ads a aba manual
    continua funcionando, e o sync das outras abas não pode quebrar por
    causa disso.
    """
    if not token:
        return {}

    base = config.IG_BASE_URL
    sess = requests.Session()

    def get(path: str, **params: Any) -> Dict[str, Any]:
        params["access_token"] = token
        try:
            r = sess.get(f"{base}/{path}", params=params, timeout=30)
            return r.json()
        except Exception as exc:  # rede, timeout, json inválido
            log.warning("discover_from_ads: falha em %s: %s", path, _redact(exc))
            return {}

    def get_url(url: str) -> Dict[str, Any]:
        """Segue o paging.next, que já vem com token e cursor embutidos."""
        try:
            return sess.get(url, timeout=30).json()
        except Exception as exc:
            log.warning("discover_from_ads: falha ao paginar: %s", _redact(exc))
            return {}

    accounts = get("me/adaccounts", fields="id", limit=100).get("data", [])
    if not accounts:
        log.warning("discover_from_ads: nenhuma conta de anúncio acessível.")
        return {}

    found: Dict[str, str] = {}
    for acct in accounts:
        # limit alto + campo aninhado faz a Graph responder "reduce the amount
        # of data"; 50 por página passa em todas as contas.
        params = {"fields": "name,creative{source_instagram_media_id}", "limit": 50}
        path = f"{acct['id']}/ads"
        paginas = 0
        while path and paginas < MAX_PAGINAS_ADS:
            data = get(path, **params) if paginas == 0 else get_url(path)
            if not data or "error" in data:
                if data:
                    log.warning(
                        "discover_from_ads: %s: %s",
                        acct["id"], str(data["error"].get("message"))[:120],
                    )
                break
            for ad in data.get("data", []):
                if not is_impulsionamento(ad.get("name", "")):
                    continue
                mid = (ad.get("creative") or {}).get("source_instagram_media_id")
                if mid and mid not in found:
                    found[mid] = (ad.get("name") or "").strip()
            path = data.get("paging", {}).get("next")
            paginas += 1

    log.info(
        "discover_from_ads: %d post(s) impulsionado(s) em %d conta(s) de anúncio.",
        len(found), len(accounts),
    )
    return found


# ----------------------------------------------------------------------
# Fonte 2: links colados na aba
# ----------------------------------------------------------------------

def resolve_shortcodes(client, shortcodes: List[str], max_scan: int = 600) -> Dict[str, str]:
    """{shortcode: media_id} varrendo as mídias da conta.

    A Graph API não resolve shortcode → media_id direto, então a única
    saída é paginar o perfil comparando permalinks. Para assim que acha
    todos os procurados, e o chamador guarda o media_id na planilha — o
    custo é uma vez por post novo, não uma vez por execução.
    """
    alvo = {s for s in shortcodes if s}
    if not alvo:
        return {}

    out: Dict[str, str] = {}
    scanned = 0
    for media in client.iter_media(max_items=max_scan):
        scanned += 1
        sc = extract_shortcode(media.get("permalink", ""))
        if sc in alvo:
            out[sc] = media["id"]
            alvo.discard(sc)
            if not alvo:
                break

    if alvo:
        log.warning(
            "resolve_shortcodes: %d link(s) não encontrado(s) nos %d posts mais "
            "recentes: %s", len(alvo), scanned, ", ".join(sorted(alvo)),
        )
    return out


# ----------------------------------------------------------------------
# Coleta
# ----------------------------------------------------------------------

def collect(client, media_id: str) -> Optional[Dict[str, Any]]:
    """Metadados + insights de um post. None se o post não é da conta.

    Um media_id que veio do Meta Ads pode pertencer à outra conta (metta vs
    tiago) — nesse caso o token atual não enxerga e a resposta é erro. Isso
    é esperado, não é falha: o chamador tenta a outra conta.
    """
    meta = client.get_media_meta(media_id)
    if not meta:
        return None

    ins = client.get_boost_insights(media_id, meta.get("media_product_type", ""))
    return {
        "media_id": media_id,
        "link": meta.get("permalink", ""),
        "legenda": (meta.get("caption") or "").replace("\n", " ")[:120],
        "tipo": meta.get("media_product_type", ""),
        **ins,
    }


def build_rows(
    coletados: List[Dict[str, Any]],
    conta: str,
    dia: date,
    agora: str,
    campanha: str = "",
) -> List[Dict[str, Any]]:
    """Uma linha de histórico por post coletado."""
    return [
        {
            "data": dia,
            "media_id": c["media_id"],
            "conta": conta,
            "campanha": campanha,
            "link": c["link"],
            "legenda": c["legenda"],
            "tipo": c["tipo"],
            "profile_visits": c.get("profile_visits", ""),
            "follows": c.get("follows", ""),
            "reach": c.get("reach", ""),
            "views": c.get("views", ""),
            "atualizado": agora,
        }
        for c in coletados
    ]


def now_brt_hhmm() -> str:
    return datetime.now(BRT).strftime("%H:%M")
