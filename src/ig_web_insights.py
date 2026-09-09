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
import os
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


def jar_path(conta: str) -> str:
    return os.path.expanduser(f"~/.mcp-env/ig_web_cookies_{conta}.json")


def load_jar(conta: str) -> Dict[str, str]:
    """Cookies salvos do run anterior. {} se ainda não existe."""
    try:
        with open(jar_path(conta)) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_jar(conta: str, cookies: Dict[str, str]) -> None:
    """Sessão ROLANTE: o Instagram renova sessionid/rur/csrftoken em uso e
    o navegador guarda a renovação. Sem isso, o cookie copiado à mão vale
    até a primeira rotação e cai. Arquivo com permissão 600."""
    path = jar_path(conta)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cookies, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class WebSession:
    """Sessão autenticada por cookie, com os tokens que o GraphQL exige."""

    def __init__(self, sessionid: str, ds_user_id: str = "", csrftoken: str = "",
                 cookie_header: str = "", saved: Optional[Dict[str, str]] = None):
        # O secret do GitHub pode vir com aspas ou espaço/quebra de linha
        # colados no copy-paste do DevTools — isso quebra o cookie sem
        # sinal nenhum de erro (o servidor só trata como sessão inválida).
        def _clean(v: str) -> str:
            return (v or "").strip().strip('"').strip("'").strip()

        # Sem sessionid avulso mas com a linha completa de cookies: pega o
        # sessionid de dentro dela. A linha completa e a fonte preferida.
        if not _clean(sessionid) and cookie_header:
            for par in _clean(cookie_header).split(";"):
                k, _, v = par.strip().partition("=")
                if k.strip() == "sessionid":
                    sessionid = v.strip()
                    break
        bruto = sessionid or ""
        sessionid = _clean(sessionid)
        # Diagnóstico de FORMATO (nunca o valor): o servidor devolveu 302 e um
        # sessionid novo já no primeiro GET, o que é assinatura de cookie
        # rejeitado. Um sessionid válido é "<id>%3A<...>%3A<...>": prefixo
        # numérico, separadores codificados, 60–120 chars, sem espaço/aspas.
        segs = sessionid.replace("%3A", ":").split(":")
        log.info(
            "WebSession: formato do sessionid: len=%d segs=%d prefixo_num=%s "
            "encoded=%s raw_colon=%s limpo_mudou=%s",
            len(sessionid), len(segs), segs[0].isdigit() if segs else False,
            "%3A" in sessionid, ":" in sessionid, bruto != sessionid,
        )
        # Se veio decodificado (":"), recodifica como o navegador envia.
        if ":" in sessionid and "%3A" not in sessionid:
            sessionid = sessionid.replace(":", "%3A")
        ds_user_id = _clean(ds_user_id)
        csrftoken = _clean(csrftoken)
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
            "X-IG-WWW-Claim": "0",
        })
        # O sessionid começa com o id do usuário ("<id>%3A..." ou
        # "<id>:..." — os dois formatos aparecem dependendo de onde o
        # cookie foi copiado). Sem esse id, `av` fica "0" mas falta o
        # ds_user_id que o navegador SEMPRE manda junto do sessionid — a
        # ausência dele é o que faz o backend tratar a sessão como
        # deslogada mesmo com o sessionid presente.
        self.uid = ds_user_id or sessionid.split("%3A")[0].split(":")[0]
        ds_user_id = ds_user_id or self.uid
        for k, v in (("sessionid", sessionid), ("ds_user_id", ds_user_id),
                     ("csrftoken", csrftoken)):
            if v:
                self.s.cookies.set(k, v, domain=".instagram.com", path="/")
        # NADA de forçar um header Cookie manual aqui: http.cookiejar só
        # monta o header sozinho quando a requisição AINDA não tem um
        # (`if not request.has_header("Cookie")`). O "reforço" da versão
        # anterior travava esse header no snapshot do __init__ (só
        # sessionid+ds_user_id) e por isso PISAVA no cookiejar em toda
        # chamada seguinte — datr/mid/ig_did/csrftoken, que só entram no
        # jar depois do primeiro GET (via Set-Cookie), nunca eram
        # mandados. É provavelmente por isso que o /explore/ passou a
        # vir com a casca deslogada (487KB) a partir da run 475: faltava
        # exatamente o conjunto de cookies que o Instagram usa pra
        # confiar no cliente. Deixa o requests.Session cuidar disso.
        # Conjunto COMPLETO de cookies do navegador, quando fornecido. Só o
        # sessionid passa uma vez e cai em seguida: o Instagram amarra a
        # sessão aos cookies de dispositivo (ig_did, mid, datr, rur...). Com o
        # header inteiro a requisição é indistinguível da do navegador.
        cookie_header = _clean(cookie_header)
        if cookie_header and cookie_header != "PREENCHER":
            n = 0
            for par in cookie_header.split(";"):
                if "=" not in par:
                    continue
                k, v = par.split("=", 1)
                k, v = k.strip(), v.strip()
                if k:
                    self.s.cookies.set(k, v, domain=".instagram.com", path="/")
                    n += 1
            log.info("WebSession: %d cookies carregados do header completo "
                     "(%s)", n, ",".join(sorted(c.name for c in self.s.cookies)))
            sid = self.s.cookies.get("sessionid") or sessionid
            self.uid = (self.s.cookies.get("ds_user_id")
                        or sid.split("%3A")[0].split(":")[0])
        # Jar salvo do run anterior tem prioridade: é a versão mais recente da
        # sessão, já com as rotações que o Instagram fez em uso.
        if saved:
            for k, v in saved.items():
                self.s.cookies.set(k, v, domain=".instagram.com", path="/")
            log.info("WebSession: %d cookies restaurados do jar salvo.", len(saved))
        self.dtsg = ""
        self.lsd = ""
        self._diag_done = False  # loga diagnóstico completo só na 1ª chamada

    # O fb_dtsg aparece em formatos diferentes conforme o bundle servido.
    # Em vez de um padrão só, tenta vários e registra qual casou — foi o que
    # travou a primeira versão, que só conhecia "DTSGInitialData".
    _DTSG_PATTERNS = (
        r'"DTSGInitialData",\[\],\{"token":"([^"]+)"',
        r'"dtsg":\s*\{"token":"([^"]+)"',
        r'\\"dtsg\\":\s*\{\\"token\\":\\"([^\\"]+)\\"',
        r'"fb_dtsg"\s*:\s*"([^"]+)"',
        r'name="fb_dtsg"\s+value="([^"]+)"',
        r'(NAf[A-Za-z0-9_-]{10,}:\d+:\d+)',
    )
    # Páginas candidatas: a home nem sempre traz o token para uma sessão
    # buscada fora do navegador. /accounts/edit/ saiu da lista: devolve 429
    # (rate limit) e não agrega nada que a home/explore não deem.
    _PAGES = ("/", "/explore/")

    @staticmethod
    def _setcookie_names(r: "requests.Response") -> List[str]:
        """Nomes (só nomes) dos cookies que o servidor tentou setar/expirar,
        varrendo TODA a cadeia de redirect (r.history + resposta final) —
        um Set-Cookie que limpa sessionid pode vir num 30x intermediário
        e `r.raw` sozinho só enxerga a última resposta.

        Serve pra diagnosticar sem logar valor: se o servidor manda de
        volta `sessionid=; Max-Age=0`, a sessão foi invalidada no backend
        e não tem o que iterar no cliente.
        """
        names = set()
        for resp in list(getattr(r, "history", []) or []) + [r]:
            try:
                raw = resp.raw.headers.getlist("Set-Cookie")
            except Exception:
                sc = resp.headers.get("Set-Cookie", "")
                raw = [sc] if sc else []
            names.update(c.split("=", 1)[0].strip() for c in raw if c)
        return sorted(names)

    def preparar(self) -> None:
        """Pega fb_dtsg/lsd/csrftoken de uma página logada."""
        log.info("WebSession: cookies no jar antes do GET: %s",
                 sorted(self.s.cookies.keys()))
        doc_headers = {
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
            "X-IG-WWW-Claim": "0",
        }
        diag = []
        for page in self._PAGES:
            try:
                r = self.s.get(IG + page, timeout=60, headers=doc_headers)
            except Exception as exc:
                diag.append(f"{page}:erro")
                continue
            html = r.text
            # Critério de "logado de verdade": a home de uma sessão válida
            # traz DTSGInitialData com um token ~84 chars. is_logged_in/
            # viewer aparecem também em respostas deslogadas (falso
            # positivo visto no run 474) — não servem sozinhos.
            tem_dtsg_init = "DTSGInitialData" in html
            achou = None
            for i, pat in enumerate(self._DTSG_PATTERNS):
                m = re.search(pat, html)
                if m:
                    self.dtsg = m.group(1)
                    achou = i
                    break
            logado_real = tem_dtsg_init and len(self.dtsg) >= 70
            m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', html)
            if m:
                self.lsd = m.group(1)
            if not self.s.cookies.get("csrftoken"):
                m = re.search(r'"csrf_token":"([^"]+)"', html)
                if m:
                    self.s.cookies.set("csrftoken", m.group(1),
                                       domain=".instagram.com")
            setcookie_names = self._setcookie_names(r)
            redirects = [h.status_code for h in r.history]
            diag.append(
                f"{page}:{r.status_code} bytes={len(html)} "
                f"logado_real={logado_real} dtsg_init={tem_dtsg_init} "
                f"dtsg={'p%d(%d)' % (achou, len(self.dtsg)) if achou is not None else 'nao'} "
                f"sid_no_jar={'sessionid' in self.s.cookies} "
                f"jar={sorted(self.s.cookies.keys())} "
                f"set_cookie={setcookie_names} redirects={redirects}"
            )
            if self.dtsg:
                log.info("WebSession: token obtido em %s (%s)", page,
                         " | ".join(diag))
                return
        raise WebInsightsError("não achei fb_dtsg — " + " | ".join(diag))

    def cookies_dict(self) -> Dict[str, str]:
        return {c.name: c.value for c in self.s.cookies}

    def _graphql(self, doc_id: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        if not self.dtsg:
            self.preparar()
        # av/__user=0: é o que o fetch do navegador logado manda de fato
        # (confirmado por captura). Usar o id numérico do usuário aqui
        # fazia a chamada ser tratada como não-autenticada. __comet_req
        # também não existe na captura real — tirado.
        body = {
            "av": "0", "__d": "www", "__user": "0", "__a": "1",
            "__req": "z",
            "dpr": "2", "fb_dtsg": self.dtsg, "lsd": self.lsd,
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
            "X-IG-WWW-Claim": "0",
            "X-ASBD-ID": "359341",
            "X-Requested-With": "XMLHttpRequest",
            "X-FB-Friendly-Name": "PolarisMediaInsights",
            "Origin": IG,
            "Referer": IG + "/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if not self._diag_done:
            cookie_names = sorted(self.s.cookies.get_dict().keys())
            log.info(
                "WebSession diag: cookies=%s dtsg_len=%d lsd_len=%d "
                "csrftoken_presente=%s",
                cookie_names, len(self.dtsg), len(self.lsd),
                bool(self.s.cookies.get("csrftoken")),
            )
        r = self.s.post(IG + "/api/graphql", data=body, headers=headers, timeout=60)
        if not self._diag_done:
            log.info(
                "WebSession diag: POST status=%d set_cookie=%s resp[:300]=%r",
                r.status_code, self._setcookie_names(r), r.text[:300],
            )
            self._diag_done = True
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
      try:
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
      except WebInsightsError as exc:
        # O total já veio; anúncio indisponível não pode apagar o total.
        log.warning("insights de anúncio %s indisponíveis: %s", ad_id, exc)

    return out
