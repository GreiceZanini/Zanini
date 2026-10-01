"""Fonte gratuita: OpenStreetMap (Nominatim + Overpass API).

Políticas de uso oficiais:
- Nominatim: https://operations.osmfoundation.org/policies/nominatim/ (máx. 1 req/s, User-Agent identificável)
- Overpass: https://wiki.openstreetmap.org/wiki/Overpass_API
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from .classificacao import classificar_site, extrair_instagram

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "zanini-prospector/0.2 (uso interno)"

# amenity que não são negócios (escolas públicas, bancos de praça, etc.)
AMENITY_IGNORAR = {
    "bench", "parking", "parking_space", "place_of_worship", "school", "kindergarten",
    "townhall", "police", "fire_station", "post_box", "waste_basket", "waste_disposal",
    "recycling", "toilets", "shelter", "drinking_water", "fountain", "bus_station",
    "grave_yard", "courthouse", "prison", "social_facility", "community_centre",
    "public_building", "university", "college", "library", "bicycle_parking",
    "motorcycle_parking", "atm", "telephone", "clock", "hunting_stand", "vending_machine",
    "taxi", "post_office", "bank", "marketplace",
}
# Para leisure/tourism só valem os valores que são negócios (parques, campos e atrações ficam fora)
LEISURE_NEGOCIO = {"fitness_centre", "sports_centre", "swimming_pool", "dance", "amusement_arcade",
                   "escape_game", "bowling_alley", "sauna", "water_park", "trampoline_park"}
TOURISM_NEGOCIO = {"hotel", "motel", "guest_house", "hostel", "apartment", "camp_site", "chalet"}
OFFICE_IGNORAR = {"government", "ngo", "political_party", "religion", "association", "diplomatic"}

CHAVES_NEGOCIO = ["shop", "craft", "office", "amenity", "healthcare", "tourism", "leisure"]


class ErroOSM(RuntimeError):
    pass


def _get_json(url, dados=None, timeout=180):
    req = urllib.request.Request(url, data=dados, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.URLError as e:
        raise ErroOSM(f"{url}: {e}") from e


def area_id(local: str) -> tuple[int, str]:
    """Converte 'Cidade, UF' no id de área do Overpass (relação OSM + 3600000000)."""
    q = urllib.parse.urlencode({"q": local, "format": "jsonv2", "limit": 5, "countrycodes": "br"})
    for r in _get_json(f"{NOMINATIM}?{q}", timeout=30):
        if r.get("osm_type") == "relation":
            return 3600000000 + int(r["osm_id"]), r.get("display_name", local)
    raise ErroOSM(f"Local não encontrado como área no OpenStreetMap: {local}")


def montar_query(area: int, categorias: list[str]) -> str:
    if categorias:
        filtros = []
        for c in categorias:
            chave, _, valor = c.partition("=")
            filtros.append(f'nwr(area.a)["{chave}"="{valor}"]["name"];' if valor else f'nwr(area.a)["{chave}"]["name"];')
    else:
        filtros = [f'nwr(area.a)["{k}"]["name"];' for k in CHAVES_NEGOCIO]
    return f"[out:json][timeout:170];area(id:{area})->.a;({''.join(filtros)});out center tags;"


def _categoria(tags):
    for k in CHAVES_NEGOCIO:
        if k in tags:
            return f"{k}={tags[k]}"
    return ""


def _primeiro(tags, *chaves):
    for k in chaves:
        if tags.get(k):
            return tags[k]
    return ""


def para_linha(el):
    tags = el.get("tags", {})
    site = _primeiro(tags, "website", "contact:website", "url")
    insta = _primeiro(tags, "contact:instagram", "instagram")
    if insta and not insta.startswith("@") and "instagram.com" not in insta:
        insta = "@" + insta
    elif "instagram.com" in insta:
        insta = extrair_instagram(insta) or insta
    centro = el.get("center", el)
    endereco = ", ".join(
        p for p in [
            " ".join(p for p in [tags.get("addr:street", ""), tags.get("addr:housenumber", "")] if p),
            tags.get("addr:suburb", ""),
            tags.get("addr:city", ""),
        ] if p
    )
    return {
        "nome": tags.get("name", ""),
        "categoria": _categoria(tags),
        "telefone": _primeiro(tags, "phone", "contact:phone", "contact:mobile", "mobile"),
        "whatsapp": _primeiro(tags, "contact:whatsapp", "whatsapp"),
        "email": _primeiro(tags, "email", "contact:email"),
        "instagram": insta or extrair_instagram(site),
        "facebook": _primeiro(tags, "contact:facebook", "facebook"),
        "situacao_site": classificar_site(site),
        "link_cadastrado": site,
        "endereco": endereco,
        "lat": centro.get("lat", ""),
        "lon": centro.get("lon", ""),
        "fonte": f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
    }


def eh_negocio(tags) -> bool:
    """Descarta locais públicos e não comerciais quando nenhuma categoria foi informada."""
    if "shop" in tags or "craft" in tags or "healthcare" in tags:
        return True
    if "office" in tags:
        return tags["office"] not in OFFICE_IGNORAR
    if "amenity" in tags:
        return tags["amenity"] not in AMENITY_IGNORAR
    if "leisure" in tags:
        return tags["leisure"] in LEISURE_NEGOCIO
    if "tourism" in tags:
        return tags["tourism"] in TOURISM_NEGOCIO
    return False


def buscar(local: str, categorias: list[str] | None = None):
    aid, _ = area_id(local)
    query = montar_query(aid, categorias or [])
    dados = _get_json(OVERPASS, urllib.parse.urlencode({"data": query}).encode())
    for el in dados.get("elements", []):
        tags = el.get("tags", {})
        if not categorias and not eh_negocio(tags):
            continue
        yield para_linha(el)
