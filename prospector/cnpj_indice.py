"""Índice local (SQLite) dos Dados Abertos do CNPJ para localizar o CNPJ pelo nome + cidade.

Criação (uma vez por UF, leva alguns minutos):
    python -m prospector indexar-cnpj --dados ./cnpj --uf PR
"""

import difflib
import sqlite3
from pathlib import Path

from . import cnpj as rf

ARQUIVO = Path(__file__).resolve().parent.parent / "dados" / "cnpj.sqlite"

PALAVRAS_IGNORAR = {"LTDA", "ME", "EPP", "EIRELI", "S/A", "SA", "DE", "DA", "DO", "DOS", "DAS", "E", "&", "-"}


def _chave(nome: str) -> str:
    return " ".join(p for p in rf.normalizar(nome).replace(".", " ").split() if p not in PALAVRAS_IGNORAR)


def criar(pasta: str, uf: str, destino: Path = ARQUIVO):
    pasta = Path(pasta)
    uf = uf.upper()
    destino.parent.mkdir(parents=True, exist_ok=True)
    municipios = {cod: rf.normalizar(desc) for cod, desc, *_ in rf._linhas_de(pasta, "MUNIC")}
    con = sqlite3.connect(destino)
    con.executescript("""
        DROP TABLE IF EXISTS est;
        CREATE TABLE est (cnpj TEXT, basico TEXT, razao TEXT, fantasia TEXT, chave TEXT,
                          municipio TEXT, uf TEXT, cnae TEXT, telefone TEXT, telefone2 TEXT,
                          email TEXT, endereco TEXT, bairro TEXT, cep TEXT, inicio TEXT);
    """)
    lote = []
    for r in rf._linhas_de(pasta, "ESTABELE"):
        if len(r) < 28 or r[rf.UF] != uf or r[rf.SITUACAO] != rf.ATIVA:
            continue
        lote.append((
            r[0] + r[1] + r[2], r[0], "", r[rf.NOME_FANTASIA], _chave(r[rf.NOME_FANTASIA]),
            municipios.get(r[rf.MUNICIPIO], ""), r[rf.UF], r[rf.CNAE_PRINCIPAL],
            rf._telefone(r[rf.DDD1], r[rf.TEL1]), rf._telefone(r[rf.DDD2], r[rf.TEL2]), r[rf.EMAIL].lower(),
            " ".join(p for p in [r[rf.TIPO_LOGR], r[rf.LOGRADOURO], r[rf.NUMERO], r[rf.COMPLEMENTO]] if p),
            r[rf.BAIRRO], r[rf.CEP], r[rf.DATA_INICIO],
        ))
        if len(lote) >= 50_000:
            con.executemany("INSERT INTO est VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lote)
            lote.clear()
    con.executemany("INSERT INTO est VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lote)
    con.execute("CREATE INDEX i_basico ON est(basico)")
    con.commit()

    # Razão social vem do arquivo Empresas
    basicos = {b for (b,) in con.execute("SELECT DISTINCT basico FROM est")}
    atual = []
    for r in rf._linhas_de(pasta, "EMPRE"):
        if r and r[0] in basicos:
            atual.append((r[1], r[0]))
            if len(atual) >= 50_000:
                con.executemany("UPDATE est SET razao=? WHERE basico=?", atual)
                atual.clear()
    con.executemany("UPDATE est SET razao=? WHERE basico=?", atual)
    con.execute("CREATE INDEX i_mun ON est(municipio)")
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM est").fetchone()[0]
    con.close()
    return total


def disponivel(arquivo: Path = ARQUIVO) -> bool:
    return arquivo.exists()


def localizar(nome: str, cidade: str, endereco: str = "", arquivo: Path = ARQUIVO, minimo: float = 0.75):
    """Procura o CNPJ mais parecido pelo nome (fantasia ou razão social) dentro do município."""
    if not arquivo.exists():
        return None
    alvo = _chave(nome)
    if not alvo:
        return None
    con = sqlite3.connect(arquivo)
    con.row_factory = sqlite3.Row
    primeira = alvo.split()[0]
    linhas = con.execute(
        "SELECT * FROM est WHERE municipio=? AND (chave LIKE ? OR razao LIKE ?)",
        (rf.normalizar(cidade), f"%{primeira}%", f"%{primeira}%"),
    ).fetchall()
    con.close()
    melhor, nota = None, 0.0
    end = rf.normalizar(endereco)
    for l in linhas:
        n = max(
            difflib.SequenceMatcher(None, alvo, l["chave"]).ratio(),
            difflib.SequenceMatcher(None, alvo, _chave(l["razao"])).ratio(),
        )
        if end and l["endereco"] and rf.normalizar(l["endereco"]).split()[-1:] == end.split()[-1:]:
            n += 0.1  # mesmo número/trecho final de endereço
        if n > nota:
            melhor, nota = l, n
    if not melhor or nota < minimo:
        return None
    d = dict(melhor)
    d["confianca"] = round(min(nota, 1.0), 2)
    return d
