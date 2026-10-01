"""Servidor local do sistema de prospecção (abre a página HTML e faz as consultas).

Uso:
    python -m prospector servidor            # abre http://localhost:8765
As chaves ficam em dados/config.json, só nesta máquina (nunca vão para o navegador).
"""

import json
import math
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import busca_web, cnpj_indice, osm
from .classificacao import classificar_site, extrair_instagram
from .google_places import ErroPlaces, buscar_area, buscar_paginas, retangulo

RAIZ = Path(__file__).resolve().parent
PAGINA = RAIZ / "web" / "index.html"
CONFIG = RAIZ.parent / "dados" / "config.json"

TERMOS_PADRAO = [
    "loja", "comércio", "restaurante", "lanchonete", "padaria", "mercado", "salão de beleza",
    "barbearia", "oficina mecânica", "clínica", "consultório", "escritório", "academia",
    "pet shop", "farmácia", "material de construção", "prestador de serviços", "estética",
]


def ler_config() -> dict:
    cfg = {}
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg.setdefault("google_key", os.environ.get("GOOGLE_MAPS_API_KEY", ""))
    cfg.setdefault("brave_key", os.environ.get("BRAVE_SEARCH_API_KEY", ""))
    return cfg


def salvar_config(novos: dict):
    cfg = ler_config()
    for k in ("google_key", "brave_key"):
        if k in novos:
            cfg[k] = (novos[k] or "").strip()
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def testar_chaves(corpo: dict) -> dict:
    """Testa as chaves recém-salvas com 1 consulta cada, para o erro aparecer na hora."""
    res = {}
    if corpo.get("google_key"):
        try:
            next(buscar_paginas("padaria São Paulo", corpo["google_key"].strip()), None)
            res["google"] = "ok"
        except Exception as e:
            res["google"] = f"erro: {e}"[:400]
    if corpo.get("brave_key"):
        try:
            busca_web._brave("padaria São Paulo", corpo["brave_key"].strip(), 1)
            res["brave"] = f"ok ({busca_web._modo['atual']})"
        except Exception as e:
            res["brave"] = f"erro: {e}"[:400]
    return res


def _distancia(lat1, lon1, lat2, lon2):
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _empresa_google(p: dict) -> dict:
    site = p.get("websiteUri", "")
    loc = p.get("location", {})
    links = [{"titulo": "Google Maps", "url": p.get("googleMapsUri", ""), "origem": "Google Maps"}]
    if site:
        links.append({"titulo": "Link cadastrado no Google", "url": site, "origem": "Google Maps"})
    return {
        "id": "g:" + p.get("id", ""),
        "nome": p.get("displayName", {}).get("text", ""),
        "categoria": p.get("primaryTypeDisplayName", {}).get("text", ""),
        "telefone": p.get("nationalPhoneNumber", ""),
        "email": "", "whatsapp": "", "facebook": "", "linkedin": "", "cnpj": "",
        "site": site if classificar_site(site) == "site_proprio" else "",
        "situacao_site": classificar_site(site),
        "instagram": extrair_instagram(site),
        "endereco": p.get("formattedAddress", ""),
        "lat": loc.get("latitude"), "lon": loc.get("longitude"),
        "avaliacao": p.get("rating", ""), "qtd_avaliacoes": p.get("userRatingCount", ""),
        "fonte": "Google Maps", "links": links,
    }


def _empresa_osm(l: dict) -> dict:
    links = [{"titulo": "OpenStreetMap", "url": l["fonte"], "origem": "OpenStreetMap"}]
    if l["link_cadastrado"]:
        links.append({"titulo": "Link cadastrado no OSM", "url": l["link_cadastrado"], "origem": "OpenStreetMap"})
    return {
        "id": "o:" + l["fonte"].rsplit("/", 2)[-2] + l["fonte"].rsplit("/", 1)[-1],
        "nome": l["nome"], "categoria": l["categoria"], "telefone": l["telefone"],
        "email": l["email"], "whatsapp": l["whatsapp"], "facebook": l["facebook"], "linkedin": "", "cnpj": "",
        "site": l["link_cadastrado"] if l["situacao_site"] == "site_proprio" else "",
        "situacao_site": l["situacao_site"], "instagram": l["instagram"],
        "endereco": l["endereco"], "lat": l["lat"], "lon": l["lon"],
        "avaliacao": "", "qtd_avaliacoes": "", "fonte": "OpenStreetMap", "links": links,
    }


def buscar(dados: dict) -> dict:
    endereco = (dados.get("endereco") or "").strip()
    raio = max(100, min(int(dados.get("raio") or 1000), 20_000))
    termos = [t.strip() for t in dados.get("termos") or [] if t.strip()] or TERMOS_PADRAO
    max_chamadas = max(1, min(int(dados.get("max_chamadas") or 150), 1000))
    centro = osm.geocodificar(endereco)
    cfg = ler_config()
    avisos, empresas, chamadas = [], {}, 0

    if cfg["google_key"]:
        ret = retangulo(centro["lat"], centro["lon"], raio)
        for termo in termos:
            if chamadas >= max_chamadas:
                faltam = termos[termos.index(termo):]
                avisos.append(f"Limite de {max_chamadas} consultas ao Google atingido. Não buscados: {', '.join(faltam)}.")
                break
            try:
                lugares, n = buscar_area(termo, ret, cfg["google_key"], max_chamadas - chamadas)
            except ErroPlaces as e:
                avisos.append(f"Google: {e}")
                break
            chamadas += n
            for p in lugares:
                if p.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
                    continue
                e = _empresa_google(p)
                if e["lat"] is not None and _distancia(centro["lat"], centro["lon"], e["lat"], e["lon"]) > raio:
                    continue
                empresas.setdefault(e["id"], e)
        fonte = "Google Maps"
    else:
        avisos.append("Sem chave do Google: usando OpenStreetMap (gratuito, cobertura menor).")
        try:
            for l in osm.buscar_raio(centro["lat"], centro["lon"], raio):
                e = _empresa_osm(l)
                empresas.setdefault(e["id"], e)
        except osm.ErroOSM as e:
            avisos.append(f"OpenStreetMap indisponível no momento: {e}")
        fonte = "OpenStreetMap"

    return {"centro": centro, "raio": raio, "fonte": fonte, "chamadas_google": chamadas,
            "avisos": avisos, "empresas": list(empresas.values())}


def ficha_google(nome: str, cidade: str, uf: str, chave: str, lat=None, lon=None):
    """Uma chamada ao Google: busca a empresa pelo nome e devolve a ficha se o nome bater
    e, quando a posição é conhecida, se estiver a até 1 km dela (evita filiais de outras cidades)."""
    extra = None
    if lat is not None and lon is not None:
        extra = {"locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": 1000.0}}}
    try:
        pagina = next(buscar_paginas(f"{nome} {cidade} {uf}".strip(), chave, extra=extra), [])
    except (ErroPlaces, StopIteration):
        return None
    for p in pagina[:5]:
        e = _empresa_google(p)
        if not busca_web._nome_bate(nome, e["nome"]):
            continue
        if lat is not None and e["lat"] is not None and _distancia(lat, lon, e["lat"], e["lon"]) > 1000:
            continue
        return e
    return None


def enriquecer(dados: dict) -> dict:
    nome, cidade = dados.get("nome", ""), dados.get("cidade", "")
    uf = dados.get("uf", "")
    cfg = ler_config()
    res = {"links_manuais": busca_web.links_manuais(nome, f"{cidade} {uf}".strip()),
           "google": None, "web": None, "cnpj": None}
    # Empresa veio do OpenStreetMap: procura a ficha dela no Google Maps (site, telefone)
    if cfg["google_key"] and dados.get("fonte") != "Google Maps":
        res["google"] = ficha_google(nome, cidade, uf, cfg["google_key"], dados.get("lat"), dados.get("lon"))
    if cfg["brave_key"]:
        res["web"] = busca_web.pesquisar(nome, f"{cidade} {uf}".strip(), cfg["brave_key"])
    if cnpj_indice.disponivel():
        res["cnpj"] = cnpj_indice.localizar(nome, cidade, dados.get("endereco", ""))
    return res


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        corpo = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _corpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            corpo = PAGINA.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        elif self.path == "/api/config":
            cfg = ler_config()
            self._json({"google": bool(cfg["google_key"]), "brave": bool(cfg["brave_key"]),
                        "cnpj": cnpj_indice.disponivel()})
        else:
            self._json({"erro": "não encontrado"}, 404)

    def do_POST(self):
        try:
            if self.path == "/api/buscar":
                self._json(buscar(self._corpo()))
            elif self.path == "/api/enriquecer":
                self._json(enriquecer(self._corpo()))
            elif self.path == "/api/config":
                corpo = self._corpo()
                salvar_config(corpo)
                self._json({"ok": True, "testes": testar_chaves(corpo)})
            else:
                self._json({"erro": "não encontrado"}, 404)
        except Exception as e:
            self._json({"erro": str(e)}, 500)

    def log_message(self, fmt, *args):
        pass


def iniciar(porta: int = 8765, abrir: bool = True):
    # Só escuta na própria máquina: as chaves não ficam expostas na rede
    srv = ThreadingHTTPServer(("127.0.0.1", porta), Handler)
    url = f"http://localhost:{porta}"
    print(f"Sistema rodando em {url}  (Ctrl+C para encerrar)")
    if abrir:
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
