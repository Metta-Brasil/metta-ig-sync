"""Insights de post pela sessão web do Instagram.

Por que existe: a Graph API não entrega o que a operação precisa.

  - Reels: `follows` e `profile_visits` são RECUSADOS por tipo de mídia
    ("does not support ... for this media product type"). Testado em 22 de
    22 reels, em v18 a v25, com token de usuário e de Página, e via campo
    aninhado. Não é permissão nem versão: não existe.
  - FEED: existe, mas subconta. No post DcEntkdhaR9 a Graph API devolve 329
    visitas ao perfil contra 2.894 da tela do Instagram, e 131 seguidores
    contra 147.
  - Atribuição ao anúncio: a Graph API não tem. A tela tem, na aba
    "Anúncio", e é exatamente o ganho do período impulsionado.

A tela do Instagram (instagram.com/insights/media/<pk>/) lê duas queries
GraphQL que respondem para QUALQUER tipo de mídia, Reels incluídos. Este
módulo chama as mesmas duas, autenticado pelo cookie de sessão.

Custo da escolha: sessão de usuário expira e não renova sozinha. O coletor
falha alto quando isso acontece; a série da Graph API continua nas colunas
antigas, então a aba nunca fica sem dado.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

IG = "https://www.instagram.com"
APP_ID = "936619743392459"

# doc_id das duas queries da tela de insights (capturadas do próprio app).
# Se o Instagram publicar outra versão do bundle, elas mudam e a chamada
# passa a responder erro — por isso o coletor levanta em vez de zerar.
DOC_TOTAL = "27638524275843653"   # PolarisMediaInsightsTotalResultsContainerQuery
DOC_AD = "38169366549343958"      # PolarisMediaInsightsAdResultsContainerQuery

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


class WebInsightsError(Exception):
    """Falha irrecuperável falando com a sessão web do Instagram."""


def shortcode_to_pk(shortcode: str) -> str:
    """Shortcode do permalink -> id numérico interno (pk) usado pela tela.

    O shortcode é o pk em base64 com alfabeto próprio. Conferido contra a
    URL real de insights em dois posts (um FEED, um REELS).
    """
    n = 0
    for ch in shortcode:
        i = _B64.find(ch)
        if i < 0:
            raise WebInsightsError(f"shortcode inválido: {shortcode!r}")
        n = n * 64 + i
    return str(n)


def pk_from_link(link: str) -> str:
    m = _SHORTCODE_RE.search(link or "")
    if not m:
        raise WebInsightsError(f"link sem shortcode: {link!r}")
    return shortcode_to_pk(m.group(1))


def _jazoest(dtsg: str) -> str:
    return "2" + str(sum(ord(c) for c in dtsg))


class WebSession:
    """Sessão autenticada por cookie, com os tokens que o GraphQL exige."""

    def __init__(self, sessionid: str, ds_user_id: str = "", csrftoken: str = ""):
        if not sessionid:
            raise WebInsightsError("IG_SESSIONID ausente")
        self.s = requests.Session()
        # Headers do navegador. Sem eles o GraphQL responde "Your Request
        # Couldn't be Processed" mesmo com a sessão válida — a chamada é
        # aceita pelo conjunto (app id + asbd + origem), não só pelo cookie.
        self.s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/140.0.0.0 Safari/537.36"),
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        })
        # O sessionid começa com o id do usuário ("<id>%3A..."): serve pro
        # campo `av`, que o app manda preenchido.
        self.uid = ds_user_id or sessionid.split("%3A")[0].split(":")[0]
        for k, v in (("sessionid", sessionid), ("ds_user_id", ds_user_id),
                     ("csrftoken", csrftoken)):
            if v:
                self.s.cookies.set(k, v, domain=".instagram.com")
        self.dtsg = ""
        self.lsd = ""

    def preparar(self) -> None:
        """Pega fb_dtsg/lsd/csrftoken de uma página logada qualquer."""
        # Requisição de DOCUMENTO. Com headers de XHR (X-Requested-With,
        # Accept */*) o Instagram devolve outra resposta e o fb_dtsg não vem.
        r = self.s.get(IG + "/", timeout=60, headers={
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        })
        html = r.text
        if "DTSGInitialData" not in html and '"dtsg"' not in html:
            raise WebInsightsError(
                "sessão web não autenticada (cookie expirado ou inválido)"
            )
        m = (re.search(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"', html)
             or re.search(r'"dtsg":\{"token":"([^"]+)"', html))
        if not m:
            raise WebInsightsError("não achei fb_dtsg na página")
        self.dtsg = m.group(1)
        m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', html)
        self.lsd = m.group(1) if m else ""
        if not self.s.cookies.get("csrftoken"):
            m = re.search(r'"csrf_token":"([^"]+)"', html)
            if m:
                self.s.cookies.set("csrftoken", m.group(1), domain=".instagram.com")

    def _graphql(self, doc_id: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        if not self.dtsg:
            self.preparar()
        body = {
            "av": self.uid, "__d": "www", "__user": self.uid, "__a": "1",
            "__req": "z", "__comet_req": "7",
            "dpr": "1", "fb_dtsg": self.dtsg, "lsd": self.lsd,
            "jazoest": _jazoest(self.dtsg),
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "PolarisMediaInsights",
            "variables": json.dumps(variables, separators=(",", ":")),
            "server_timestamps": "true", "doc_id": doc_id,
        }
        # Requisição XHR: aqui sim o conjunto de headers do app.
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "*/*",
            "X-CSRFToken": self.s.cookies.get("csrftoken") or "",
            "X-FB-LSD": self.lsd,
            "X-IG-App-ID": APP_ID,
            "X-ASBD-ID": "359341",
            "X-Requested-With": "XMLHttpRequest",
            "X-FB-Friendly-Name": "PolarisMediaInsights",
            "Origin": IG,
            "Referer": IG + "/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        r = self.s.post(IG + "/api/graphql", data=body, headers=headers, timeout=60)
        txt = r.text
        if txt.startswith("for (;;);"):
            txt = txt[len("for (;;);"):]
        try:
            data = json.loads(txt)
        except ValueError:
            raise WebInsightsError(f"resposta não-JSON ({r.status_code})")
        if data.get("error") or data.get("errors"):
            raise WebInsightsError(
                f"GraphQL recusou: {str(data.get('errorSummary') or data.get('errors'))[:120]}"
            )
        return data


def _total_value(node: Any) -> Optional[int]:
    try:
        v = node["value"]["results"][0]["total_value"]
        return int(v) if v is not None else None
    except Exception:
        return None


def _ad_value(node: Any) -> Optional[int]:
    try:
        v = node["results"][0]["value"]
        return int(v) if v is not None else None
    except Exception:
        return None


def coletar(sess: "WebSession", pk: str, ad_id: str = "") -> Dict[str, Any]:
    """Números do post pela tela: total (orgânico+pago) e só do anúncio.

    Funciona para FEED, carrossel e REELS — é a diferença em relação à
    Graph API, que recusa reels.
    """
    out: Dict[str, Any] = {
        "web_profile_visits": None, "web_follows": None, "web_reach": None,
        "ad_profile_visits": None, "ad_follows": None, "ad_reach": None,
        "ad_views": None,
    }

    tot = sess._graphql(DOC_TOTAL, {
        "is_creator_viewing_insights": True,
        "query_params": {"access_token": "", "id": pk},
    })
    media = (tot.get("data") or {}).get("media") or {}
    out["web_follows"] = _total_value(media.get("umapi_follows_from_impressions_count"))
    out["web_profile_visits"] = _total_value(
        media.get("umapi_profile_views_from_impressions_count"))
    out["web_reach"] = _total_value(media.get("umapi_foa_people_based_reach"))

    if ad_id:
        ad = sess._graphql(DOC_AD, {
            "adgroup_id": str(ad_id),
            "has_fb_placements_for_media": True,
            "is_eligible_for_web_media_id_insights_migration": True,
            "query_params": {"access_token": "", "id": pk},
        })
        w = (((ad.get("data") or {}).get("media") or {})
             .get("ig_insights_ad_metadata_wrapper") or {})
        out["ad_follows"] = _ad_value(w.get("umapi_ad_instagram_profile_follows"))
        out["ad_profile_visits"] = _ad_value(w.get("umapi_ad_profile_visits"))
        out["ad_reach"] = _ad_value(w.get("umapi_ad_reach"))
        out["ad_views"] = _ad_value(w.get("umapi_ad_views"))

    return out
