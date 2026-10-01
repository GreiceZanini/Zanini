"""Segunda etapa: procura a empresa na web.

Com chave da Brave Search API (https://api-dashboard.search.brave.com) as buscas são feitas
automaticamente. Sem chave, o sistema devolve links de busca prontos para abrir no navegador.
"""

import json
import math
import re
import urllib.parse
import urllib.request

from .classificacao import classificar_site, extrair_instagram

BRAVE = "https://api.search.brave.com/res/v1/web/search"

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


def _brave(q: str, chave: str, n: int = 10) -> list[dict]:
    url = BRAVE + "?" + urllib.parse.urlencode({"q": q, "count": n, "country": "BR", "search_lang": "pt-br"})
    req = urllib.request.Request(url, headers={"X-Subscription-Token": chave, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        dados = json.load(r)
    return [
        {"titulo": x.get("title", ""), "url": x.get("url", ""), "trecho": x.get("description", "")}
        for x in dados.get("web", {}).get("results", [])
    ]


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

    res = {"site": "", "instagram": "", "facebook": "", "linkedin": "", "links": links}
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
