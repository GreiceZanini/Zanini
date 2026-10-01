"""Classifica o link cadastrado no Google como site próprio ou não."""

from urllib.parse import urlparse

# Domínios que NÃO contam como site próprio (redes sociais, agregadores, links curtos).
DOMINIOS_NAO_PROPRIOS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "m.facebook.com": "facebook",
    "wa.me": "whatsapp",
    "whatsapp.com": "whatsapp",
    "api.whatsapp.com": "whatsapp",
    "linktr.ee": "linktree",
    "linkin.bio": "linktree",
    "beacons.ai": "linktree",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "linkedin.com": "linkedin",
    "x.com": "twitter",
    "twitter.com": "twitter",
    "ifood.com.br": "ifood",
    "goo.gl": "link_curto",
    "g.page": "google",
    "business.site": "google",
    "negocio.site": "google",
    "sites.google.com": "google",
    "bit.ly": "link_curto",
}

SEM_SITE = "sem_site"
SITE_PROPRIO = "site_proprio"


def _dominio(url: str) -> str:
    if "://" not in url:
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def classificar_site(url: str | None) -> str:
    """Retorna 'sem_site', 'site_proprio' ou o tipo de rede (ex.: 'instagram')."""
    if not url or not url.strip():
        return SEM_SITE
    host = _dominio(url.strip())
    if not host:
        return SEM_SITE
    for dominio, tipo in DOMINIOS_NAO_PROPRIOS.items():
        if host == dominio or host.endswith("." + dominio):
            return tipo
    return SITE_PROPRIO


def extrair_instagram(url: str | None) -> str:
    """Extrai o @perfil de um link do Instagram, se houver."""
    if not url or classificar_site(url) != "instagram":
        return ""
    if "://" not in url:
        url = "http://" + url
    partes = [p for p in urlparse(url).path.split("/") if p]
    if not partes or partes[0] in {"p", "reel", "explore", "stories"}:
        return ""
    return "@" + partes[0]
