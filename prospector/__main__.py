"""Busca empresas por localização e lista as que não têm site próprio.

Uso:
    export GOOGLE_MAPS_API_KEY=...
    python -m prospector --tipo "padaria" --tipo "salão de beleza" --local "Toledo, PR"
    python -m prospector --tipo "oficina mecânica" --local "Cascavel, PR" --bairro Centro --bairro "Santa Cruz"
"""

import argparse
import csv
import os
import sys

from .classificacao import SEM_SITE, SITE_PROPRIO, classificar_site, extrair_instagram
from .google_places import ErroPlaces, buscar

COLUNAS = [
    "nome",
    "categoria",
    "telefone",
    "telefone_internacional",
    "situacao_site",
    "link_cadastrado",
    "instagram",
    "endereco",
    "avaliacao",
    "qtd_avaliacoes",
    "google_maps",
    "termo_busca",
]


def montar_consultas(tipos, local, bairros):
    if not bairros:
        return [f"{t} em {local}" for t in tipos]
    return [f"{t} em {b}, {local}" for t in tipos for b in bairros]


def para_linha(place, consulta):
    link = place.get("websiteUri", "")
    return {
        "nome": place.get("displayName", {}).get("text", ""),
        "categoria": place.get("primaryTypeDisplayName", {}).get("text", ""),
        "telefone": place.get("nationalPhoneNumber", ""),
        "telefone_internacional": place.get("internationalPhoneNumber", ""),
        "situacao_site": classificar_site(link),
        "link_cadastrado": link,
        "instagram": extrair_instagram(link),
        "endereco": place.get("formattedAddress", ""),
        "avaliacao": place.get("rating", ""),
        "qtd_avaliacoes": place.get("userRatingCount", ""),
        "google_maps": place.get("googleMapsUri", ""),
        "termo_busca": consulta,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Empresas sem site próprio por localização (Google Places API).")
    p.add_argument("--tipo", action="append", required=True, help="Tipo de negócio (repetível)")
    p.add_argument("--local", required=True, help='Cidade/UF, ex.: "Toledo, PR"')
    p.add_argument("--bairro", action="append", default=[], help="Bairro (repetível) para ampliar além de 60 resultados")
    p.add_argument("--saida", default="empresas_sem_site.csv", help="Arquivo CSV de saída")
    p.add_argument("--somente-sem-link", action="store_true",
                   help="Lista apenas quem não tem nenhum link (por padrão, quem só tem Instagram/Facebook/WhatsApp também entra)")
    p.add_argument("--exigir-telefone", action="store_true", help="Descarta empresas sem telefone")
    args = p.parse_args(argv)

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        sys.exit("Defina a variável de ambiente GOOGLE_MAPS_API_KEY.")

    vistos, linhas, total = set(), [], 0
    for consulta in montar_consultas(args.tipo, args.local, args.bairro):
        print(f"Buscando: {consulta}", file=sys.stderr)
        try:
            for place in buscar(consulta, api_key):
                pid = place.get("id")
                if pid in vistos:
                    continue
                vistos.add(pid)
                total += 1
                if place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
                    continue
                linha = para_linha(place, consulta)
                situacao = linha["situacao_site"]
                if situacao == SITE_PROPRIO:
                    continue
                if args.somente_sem_link and situacao != SEM_SITE:
                    continue
                if args.exigir_telefone and not linha["telefone"]:
                    continue
                linhas.append(linha)
        except ErroPlaces as e:
            sys.exit(f"Erro na API do Google: {e}")

    # utf-8-sig para abrir corretamente no Excel
    with open(args.saida, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS, delimiter=";")
        w.writeheader()
        w.writerows(linhas)

    print(f"{total} empresas encontradas, {len(linhas)} sem site próprio -> {args.saida}", file=sys.stderr)


if __name__ == "__main__":
    main()
