"""Cliente mínimo da Google Places API (New) - Text Search.

Documentação oficial:
https://developers.google.com/maps/documentation/places/web-service/text-search
"""

import json
import time
import urllib.error
import urllib.request

URL = "https://places.googleapis.com/v1/places:searchText"

CAMPOS = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.googleMapsUri",
        "places.location",
        "places.businessStatus",
        "places.primaryTypeDisplayName",
        "places.rating",
        "places.userRatingCount",
        "nextPageToken",
    ]
)


class ErroPlaces(RuntimeError):
    pass


def buscar_paginas(texto: str, api_key: str, idioma: str = "pt-BR", regiao: str = "BR", extra: dict | None = None):
    """Busca por texto e devolve uma lista de lugares por página (cada página = 1 chamada cobrada).

    A API limita a 60 resultados (3 páginas) por consulta.
    """
    token = None
    while True:
        corpo = {"textQuery": texto, "languageCode": idioma, "regionCode": regiao, "pageSize": 20, **(extra or {})}
        if token:
            corpo["pageToken"] = token
        req = urllib.request.Request(
            URL,
            data=json.dumps(corpo).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": CAMPOS,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                dados = json.load(resp)
        except urllib.error.HTTPError as e:
            raise ErroPlaces(f"HTTP {e.code}: {e.read().decode(errors='replace')}") from e

        yield dados.get("places", [])
        token = dados.get("nextPageToken")
        if not token:
            return
        time.sleep(1)


def retangulo(lat: float, lon: float, raio_m: float) -> dict:
    """Retângulo que envolve o círculo (lat, lon, raio) no formato da API."""
    import math
    dlat = raio_m / 111_320
    dlon = raio_m / (111_320 * max(math.cos(math.radians(lat)), 0.01))
    return {"low": {"latitude": lat - dlat, "longitude": lon - dlon},
            "high": {"latitude": lat + dlat, "longitude": lon + dlon}}


def _dividir(ret: dict) -> list[dict]:
    lo, hi = ret["low"], ret["high"]
    mlat = (lo["latitude"] + hi["latitude"]) / 2
    mlon = (lo["longitude"] + hi["longitude"]) / 2
    return [
        {"low": {"latitude": a, "longitude": b}, "high": {"latitude": c, "longitude": d}}
        for a, c in ((lo["latitude"], mlat), (mlat, hi["latitude"]))
        for b, d in ((lo["longitude"], mlon), (mlon, hi["longitude"]))
    ]


def buscar_area(termo: str, ret: dict, api_key: str, limite_chamadas: int, profundidade: int = 2):
    """Text Search restrito ao retângulo. Se bater o teto de 60, divide a área em 4 e repete.

    Retorna (lugares, chamadas_usadas).
    """
    lugares, chamadas = [], 0
    for pagina in buscar_paginas(termo, api_key, extra={"locationRestriction": {"rectangle": ret}}):
        chamadas += 1
        lugares.extend(pagina)
        if chamadas >= limite_chamadas:
            return lugares, chamadas
    if len(lugares) >= 60 and profundidade > 0:
        for sub in _dividir(ret):
            if chamadas >= limite_chamadas:
                break
            mais, n = buscar_area(termo, sub, api_key, limite_chamadas - chamadas, profundidade - 1)
            lugares.extend(mais)
            chamadas += n
    return lugares, chamadas
