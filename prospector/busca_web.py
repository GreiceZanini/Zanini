"""Segunda etapa: procura a empresa na web.

Com chave da Brave Search API (https://api-dashboard.search.brave.com) as buscas são feitas
automaticamente. Sem chave, o sistema devolve links de busca prontos para abrir no navegador.
"""

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request

from .classificacao import classificar_site, extrair_instagram

BRAVE = "https://api.search.brave.com/res/v1/web/search"
BRAVE_LLM = "https://api.search.brave.com/res/v1/llm/context"

# Domínios que não são o site da própria empresa (listas, guias, buscadores de CNPJ...)
AGREGADORES = (
    "google.", "facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "youtube.com",
    "wa.me", "whatsapp.com", "ifood.com.br", "tripadvisor.", "guiamais.", "apontador.",
    "telelistas.", "solutudo.", "cnpj.", "econodata.", "casadosdados.", "serasa", "jusbrasil.",
    "reclameaqui.", "mercadolivre.", "olx.", "linktr.ee", "yelp.", "foursquare.", "waze.",
    "receita", "gov.br", "empresascnpj.", "consultasocio.", "cnpja.", "infoplex.",
)


def links_manuais(nome: str, cidade: str) -> dict:
    """Links de busca para abrir no navegador (sempre disponíveis, sem chave)."""
    base = f'"{nome}" {cidade}'.strip()
    g = lambda q: "https://www.google.com/search?q=" + urllib.parse.quote(q)
    return {
        "Google": g(base),
        "Site oficial": g(f"{base} site oficial"),
        "Instagram": g(f"{base} site:instagram.com"),
        "Facebook": g(f"{base} site:facebook.com"),
        "LinkedIn": g(f"{base} site:linkedin.com/company"),
        "CNPJ": g(f"{base} CNPJ"),
        "Serasa / reputação": g(f"{base} serasa OR reclameaqui"),
        "Google Maps": "https://www.google.com/maps/search/" + urllib.parse.quote(base),
    }


class ErroBusca(RuntimeError):
    pass


# Qual produto Brave a chave atende ("web" = Web Search, "llm" = LLM Context). Descoberto na 1ª chamada.
_modo = {"atual": None}


def _get(url: str, chave: str) -> dict:
    req = urllib.request.Request(url, headers={"X-Subscription-Token": chave, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ErroBusca(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e


def _web(q: str, chave: str, n: int) -> list[dict]:
    url = BRAVE + "?" + urllib.parse.urlencode({"q": q, "count": n, "country": "BR", "search_lang": "pt-br"})
    return [
        {"titulo": x.get("title", ""), "url": x.get("url", ""), "trecho": x.get("description", "")}
        for x in _get(url, chave).get("web", {}).get("results", [])
    ]


def _llm(q: str, chave: str, n: int) -> list[dict]:
    """Brave LLM Context: traz trechos do conteúdo das páginas (bom para achar telefone, e-mail, CNPJ)."""
    url = BRAVE_LLM + "?" + urllib.parse.urlencode({
        "q": q, "count": n, "country": "BR", "search_lang": "pt-br",
        "maximum_number_of_urls": n, "maximum_number_of_tokens": 2048,
    })
    g = _get(url, chave).get("grounding", {}) or {}
    itens = list(g.get("generic") or []) + list(g.get("map") or [])
    if g.get("poi"):
        itens.append(g["poi"])
    return [
        {"titulo": x.get("title") or x.get("name", ""), "url": x.get("url", ""), "trecho": " ".join(x.get("snippets") or [])}
        for x in itens
    ]


def _brave(q: str, chave: str, n: int = 10) -> list[dict]:
    """Usa Web Search; se a chave for só do produto LLM Context, troca automaticamente."""
    if _modo["atual"] == "llm":
        return _llm(q, chave, n)
    try:
        r = _web(q, chave, n)
        _modo["atual"] = "web"
        return r
    except ErroBusca as e:
        if _modo["atual"] == "web" or not any(c in str(e) for c in ("HTTP 401", "HTTP 403", "HTTP 404", "HTTP 422")):
            raise
        r = _llm(q, chave, n)
        _modo["atual"] = "llm"
        return r


RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
RE_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
RE_FONE = re.compile(r"\(?\b\d{2}\)?\s?9?\d{4}[-.\s]?\d{4}\b")


def extrair_contatos(links: list[dict]) -> dict:
    """E-mails, telefones e CNPJs que aparecem nos trechos, com a página de origem."""
    achados = {"emails": {}, "telefones": {}, "cnpjs": {}}
    for l in links:
        texto = l.get("trecho", "")
        for m in RE_EMAIL.findall(texto):
            m = m.lower().rstrip(".")
            if not m.endswith((".png", ".jpg", ".webp", ".gif")):
                achados["emails"].setdefault(m, l["url"])
        for m in RE_CNPJ.findall(texto):
            d = re.sub(r"\D", "", m)
            achados["cnpjs"].setdefault(f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}", l["url"])
        for m in RE_FONE.findall(texto):
            d = re.sub(r"\D", "", m)
            if len(d) in (10, 11) and not re.sub(r"\D", "", texto).count(d) > 3:
                achados["telefones"].setdefault(f"({d[:2]}) {d[2:-4]}-{d[-4:]}", l["url"])
    return {k: [{"valor": v, "fonte": u} for v, u in d.items()] for k, d in achados.items()}


def _eh_agregador(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return any(a in host for a in AGREGADORES)


def _nome_bate(nome: str, texto: str) -> bool:
    """Pelo menos 60% das palavras relevantes do nome (sem acento) aparecem no texto."""
    from .cnpj import normalizar
    palavras = [p for p in re.findall(r"[A-Z0-9]+", normalizar(nome)) if len(p) > 2]
    if not palavras:
        return False
    texto = set(re.findall(r"[A-Z0-9]+", normalizar(texto)))  # palavras inteiras ("OLE" não casa com "TOLEDO")
    return sum(p in texto for p in palavras) >= max(1, math.ceil(len(palavras) * 0.6))


def pesquisar(nome: str, cidade: str, chave: str) -> dict:
    """Busca automática. Retorna site provável, redes sociais e todos os links encontrados."""
    consultas = [
        f'"{nome}" {cidade}',
        f'"{nome}" {cidade} site:instagram.com',
        f'"{nome}" {cidade} site:linkedin.com',
        f'"{nome}" {cidade} CNPJ',
    ]
    vistos, links = set(), []
    for q in consultas:
        try:
            for r in _brave(q, chave):
                if r["url"] not in vistos and _nome_bate(nome, r["titulo"] + " " + r["trecho"] + " " + r["url"]):
                    vistos.add(r["url"])
                    links.append(r)
        except Exception as e:  # uma consulta falhar não derruba as outras
            links.append({"titulo": f"Erro na busca: {e}", "url": "", "trecho": q})

    res = {"site": "", "instagram": "", "facebook": "", "linkedin": "", "links": links,
           "contatos": extrair_contatos([l for l in links if l["url"]]), "modo": _modo["atual"]}
    for r in links:
        u = r["url"]
        tipo = classificar_site(u)
        if tipo == "instagram" and not res["instagram"]:
            res["instagram"] = extrair_instagram(u) or u
        elif tipo == "facebook" and not res["facebook"]:
            res["facebook"] = u
        elif tipo == "linkedin" and not res["linkedin"]:
            res["linkedin"] = u
        elif tipo == "site_proprio" and not res["site"] and u and not _eh_agregador(u):
            res["site"] = u
    return res
