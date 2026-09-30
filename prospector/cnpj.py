"""Fonte gratuita: Dados Abertos do CNPJ (Receita Federal).

Leiaute oficial: https://www.gov.br/receitafederal/dados/cnpj-metadados.pdf
Arquivos: Estabelecimentos*.zip, Empresas*.zip e Municipios.zip (CSV ';', latin-1, sem cabeçalho).

A base NÃO informa se a empresa tem site. O indício usado é o e-mail cadastrado:
- e-mail gratuito (gmail, hotmail...) -> provável sem site;
- e-mail com domínio próprio -> o domínio pode ser testado com --verificar-dominio.
"""

import csv
import io
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

# Índices do leiaute ESTABELECIMENTOS
CNPJ_BASICO, CNPJ_ORDEM, CNPJ_DV = 0, 1, 2
NOME_FANTASIA, SITUACAO = 4, 5
DATA_INICIO, CNAE_PRINCIPAL = 10, 11
TIPO_LOGR, LOGRADOURO, NUMERO, COMPLEMENTO, BAIRRO, CEP, UF, MUNICIPIO = 13, 14, 15, 16, 17, 18, 19, 20
DDD1, TEL1, DDD2, TEL2 = 21, 22, 23, 24
EMAIL = 27
ATIVA = "02"

PROVEDORES_GRATUITOS = {
    "gmail.com", "hotmail.com", "hotmail.com.br", "outlook.com", "outlook.com.br", "live.com",
    "yahoo.com", "yahoo.com.br", "ymail.com", "bol.com.br", "uol.com.br", "terra.com.br",
    "ig.com.br", "icloud.com", "me.com", "msn.com", "globo.com", "globomail.com", "r7.com",
    "zipmail.com.br", "protonmail.com", "proton.me", "gmx.com",
}


def normalizar(texto: str) -> str:
    s = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return " ".join(s.upper().split())


def _arquivos(pasta: Path, trecho: str):
    return sorted(p for p in pasta.iterdir() if trecho in p.name.upper() and p.is_file())


def _linhas(caminho: Path):
    """Lê CSV da Receita direto do .zip ou do arquivo extraído."""
    if caminho.suffix.lower() == ".zip":
        with zipfile.ZipFile(caminho) as z:
            for membro in z.namelist():
                with z.open(membro) as f:
                    yield from csv.reader(io.TextIOWrapper(f, encoding="latin-1", newline=""), delimiter=";")
    else:
        with open(caminho, encoding="latin-1", newline="") as f:
            yield from csv.reader(f, delimiter=";")


def codigos_municipio(pasta: Path, nome: str) -> set[str]:
    alvo = normalizar(nome)
    codigos = {cod for cod, desc, *_ in _linhas_de(pasta, "MUNIC") if normalizar(desc) == alvo}
    if not codigos:
        raise SystemExit(f"Município não encontrado na tabela da Receita: {nome}")
    return codigos


def _linhas_de(pasta: Path, trecho: str):
    arquivos = _arquivos(pasta, trecho)
    if not arquivos:
        raise SystemExit(f"Nenhum arquivo contendo '{trecho}' em {pasta}")
    for a in arquivos:
        yield from _linhas(a)


def situacao_email(email: str) -> tuple[str, str]:
    """Retorna (situacao_site, dominio)."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return "sem_email", ""
    dominio = email.rsplit("@", 1)[1]
    if dominio in PROVEDORES_GRATUITOS:
        return "email_gratuito", dominio
    return "dominio_proprio", dominio


def dominio_tem_site(dominio: str, timeout: float = 6) -> bool:
    for url in (f"https://{dominio}", f"http://{dominio}", f"https://www.{dominio}"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status < 400:
                    return True
        except Exception:
            continue
    return False


def _telefone(ddd, tel):
    ddd, tel = (ddd or "").strip(), (tel or "").strip()
    return f"({ddd}) {tel}" if ddd and tel else tel


def buscar(pasta: str, municipio: str, uf: str | None, cnaes: list[str], incluir_dominio_proprio: bool):
    pasta = Path(pasta)
    codigos = codigos_municipio(pasta, municipio)
    uf = (uf or "").upper()
    for r in _linhas_de(pasta, "ESTABELE"):
        if len(r) < 28 or r[MUNICIPIO] not in codigos or r[SITUACAO] != ATIVA:
            continue
        if uf and r[UF] != uf:
            continue
        if cnaes and not any(r[CNAE_PRINCIPAL].startswith(c) for c in cnaes):
            continue
        tel1 = _telefone(r[DDD1], r[TEL1])
        situacao, dominio = situacao_email(r[EMAIL])
        if situacao == "dominio_proprio" and not incluir_dominio_proprio:
            continue
        yield {
            "cnpj": f"{r[CNPJ_BASICO]}{r[CNPJ_ORDEM]}{r[CNPJ_DV]}",
            "razao_social": "",
            "nome_fantasia": r[NOME_FANTASIA],
            "cnae_principal": r[CNAE_PRINCIPAL],
            "telefone": tel1,
            "telefone2": _telefone(r[DDD2], r[TEL2]),
            "email": r[EMAIL].lower(),
            "situacao_site": situacao,
            "dominio": dominio,
            "endereco": " ".join(p for p in [r[TIPO_LOGR], r[LOGRADOURO], r[NUMERO], r[COMPLEMENTO]] if p).strip(),
            "bairro": r[BAIRRO],
            "cep": r[CEP],
            "uf": r[UF],
            "inicio_atividade": r[DATA_INICIO],
        }


def preencher_razao_social(pasta: str, linhas: list[dict]):
    """Segunda passada nos arquivos Empresas para trazer a razão social."""
    por_basico = {}
    for l in linhas:
        por_basico.setdefault(l["cnpj"][:8], []).append(l)
    if not por_basico or not _arquivos(Path(pasta), "EMPRE"):
        return
    for r in _linhas_de(Path(pasta), "EMPRE"):
        if r and r[0] in por_basico:
            for l in por_basico[r[0]]:
                l["razao_social"] = r[1]
