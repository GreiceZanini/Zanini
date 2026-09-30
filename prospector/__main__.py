"""Busca empresas por localização e lista as que não têm site próprio.

Fontes:
    osm     OpenStreetMap - gratuito, sem cadastro
    cnpj    Dados Abertos do CNPJ (Receita Federal) - gratuito, arquivos baixados
    google  Google Places API - exige chave com faturamento (tem cota mensal gratuita)

Exemplos:
    python -m prospector osm --local "Toledo, PR"
    python -m prospector osm --local "Toledo, PR" --categoria shop=bakery --categoria amenity=restaurant
    python -m prospector cnpj --dados ./cnpj --municipio Toledo --uf PR --cnae 1091 --cnae 9602
    python -m prospector google --tipo padaria --local "Toledo, PR" --max-buscas 50
"""

import argparse
import csv
import os
import sys

from . import cnpj, osm
from .classificacao import SEM_SITE, SITE_PROPRIO, classificar_site, extrair_instagram
from .google_places import ErroPlaces, buscar_paginas


def salvar_csv(caminho, linhas, colunas=None):
    colunas = colunas or (list(linhas[0].keys()) if linhas else ["nome"])
    # utf-8-sig para abrir corretamente no Excel
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=colunas, delimiter=";")
        w.writeheader()
        w.writerows(linhas)


# ---------------------------------------------------------------- Google

COLUNAS_GOOGLE = [
    "nome", "categoria", "telefone", "telefone_internacional", "situacao_site", "link_cadastrado",
    "instagram", "endereco", "avaliacao", "qtd_avaliacoes", "google_maps", "termo_busca",
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


def filtrar_site(linhas, somente_sem_link, exigir_telefone):
    for l in linhas:
        if l["situacao_site"] == SITE_PROPRIO:
            continue
        if somente_sem_link and l["situacao_site"] != SEM_SITE:
            continue
        if exigir_telefone and not l["telefone"]:
            continue
        yield l


def cmd_google(args):
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        sys.exit("Defina a variável de ambiente GOOGLE_MAPS_API_KEY.")
    vistos, brutas, chamadas = set(), [], 0
    for consulta in montar_consultas(args.tipo, args.local, args.bairro):
        print(f"Buscando: {consulta}", file=sys.stderr)
        try:
            for pagina in buscar_paginas(consulta, api_key):
                chamadas += 1
                for place in pagina:
                    if place.get("id") in vistos or place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
                        continue
                    vistos.add(place.get("id"))
                    brutas.append(para_linha(place, consulta))
                if chamadas >= args.max_buscas:
                    break
        except ErroPlaces as e:
            sys.exit(f"Erro na API do Google: {e}")
        if chamadas >= args.max_buscas:
            print(f"Limite de {args.max_buscas} chamadas atingido.", file=sys.stderr)
            break
    linhas = list(filtrar_site(brutas, args.somente_sem_link, args.exigir_telefone))
    salvar_csv(args.saida, linhas, COLUNAS_GOOGLE)
    print(f"{len(brutas)} empresas, {len(linhas)} sem site próprio, {chamadas} chamadas -> {args.saida}", file=sys.stderr)


# ---------------------------------------------------------------- OSM

def cmd_osm(args):
    try:
        brutas = list(osm.buscar(args.local, args.categoria))
    except osm.ErroOSM as e:
        sys.exit(f"Erro no OpenStreetMap: {e}")
    linhas = list(filtrar_site(brutas, args.somente_sem_link, args.exigir_telefone))
    salvar_csv(args.saida, linhas)
    print(f"{len(brutas)} estabelecimentos no OSM, {len(linhas)} sem site próprio -> {args.saida}", file=sys.stderr)


# ---------------------------------------------------------------- CNPJ

def cmd_cnpj(args):
    linhas = []
    for l in cnpj.buscar(args.dados, args.municipio, args.uf, args.cnae, args.verificar_dominio):
        if args.exigir_telefone and not l["telefone"]:
            continue
        linhas.append(l)
    if args.verificar_dominio:
        print("Verificando domínios de e-mail próprios...", file=sys.stderr)
        cache = {}
        for l in linhas:
            if l["situacao_site"] == "dominio_proprio":
                d = l["dominio"]
                if d not in cache:
                    cache[d] = cnpj.dominio_tem_site(d)
                l["situacao_site"] = SITE_PROPRIO if cache[d] else "dominio_sem_site"
        linhas = [l for l in linhas if l["situacao_site"] != SITE_PROPRIO]
    if not args.sem_razao_social:
        print("Buscando razão social nos arquivos Empresas...", file=sys.stderr)
        cnpj.preencher_razao_social(args.dados, linhas)
    salvar_csv(args.saida, linhas)
    print(f"{len(linhas)} empresas ativas sem indício de site -> {args.saida}", file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(prog="prospector", description="Empresas sem site próprio por localização.")
    sub = p.add_subparsers(dest="fonte", required=True)

    def comuns(sp, saida):
        sp.add_argument("--saida", default=saida, help="Arquivo CSV de saída")
        sp.add_argument("--exigir-telefone", action="store_true", help="Descarta empresas sem telefone")

    g = sub.add_parser("google", help="Google Places API (chave com faturamento; cota mensal gratuita)")
    g.add_argument("--tipo", action="append", required=True, help="Tipo de negócio (repetível)")
    g.add_argument("--local", required=True, help='Cidade/UF, ex.: "Toledo, PR"')
    g.add_argument("--bairro", action="append", default=[], help="Bairro (repetível) para passar de 60 resultados")
    g.add_argument("--somente-sem-link", action="store_true", help="Exclui quem tem Instagram/Facebook cadastrado")
    g.add_argument("--max-buscas", type=int, default=100,
                   help="Máximo de chamadas à API nesta execução (cota gratuita Enterprise: 1.000/mês)")
    comuns(g, "google_sem_site.csv")
    g.set_defaults(func=cmd_google)

    o = sub.add_parser("osm", help="OpenStreetMap (gratuito)")
    o.add_argument("--local", required=True, help='Cidade/UF, ex.: "Toledo, PR"')
    o.add_argument("--categoria", action="append", default=[],
                   help="Tag OSM (repetível), ex.: shop=bakery, amenity=restaurant, craft")
    o.add_argument("--somente-sem-link", action="store_true", help="Exclui quem tem Instagram/Facebook cadastrado")
    comuns(o, "osm_sem_site.csv")
    o.set_defaults(func=cmd_osm)

    c = sub.add_parser("cnpj", help="Dados Abertos do CNPJ (gratuito, arquivos baixados)")
    c.add_argument("--dados", required=True, help="Pasta com Estabelecimentos*.zip, Empresas*.zip e Municipios.zip")
    c.add_argument("--municipio", required=True, help="Nome do município, ex.: Toledo")
    c.add_argument("--uf", help="UF, ex.: PR (evita municípios homônimos)")
    c.add_argument("--cnae", action="append", default=[], help="Prefixo do CNAE principal (repetível), ex.: 4721")
    c.add_argument("--verificar-dominio", action="store_true",
                   help="Inclui e-mails com domínio próprio e testa se o domínio tem site no ar")
    c.add_argument("--sem-razao-social", action="store_true", help="Pula a leitura dos arquivos Empresas (mais rápido)")
    comuns(c, "cnpj_sem_site.csv")
    c.set_defaults(func=cmd_cnpj)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
