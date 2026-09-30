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
        "places.businessStatus",
        "places.primaryTypeDisplayName",
        "places.rating",
        "places.userRatingCount",
        "nextPageToken",
    ]
)


class ErroPlaces(RuntimeError):
    pass


def buscar_paginas(texto: str, api_key: str, idioma: str = "pt-BR", regiao: str = "BR"):
    """Busca por texto e devolve uma lista de lugares por página (cada página = 1 chamada cobrada).

    A API limita a 60 resultados (3 páginas) por consulta.
    """
    token = None
    while True:
        corpo = {"textQuery": texto, "languageCode": idioma, "regionCode": regiao, "pageSize": 20}
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
