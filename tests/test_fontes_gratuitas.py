import csv
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from prospector import cnpj, osm


def _zip(pasta: Path, nome: str, membro: str, linhas: list[list[str]]):
    buf = io.StringIO()
    csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL).writerows(linhas)
    with zipfile.ZipFile(pasta / nome, "w") as z:
        z.writestr(membro, buf.getvalue().encode("latin-1"))


def _estab(basico, fantasia, situacao, cnae, municipio, ddd, tel, email, uf="PR"):
    r = [""] * 30
    r[0], r[1], r[2] = basico, "0001", "99"
    r[4], r[5], r[11], r[20], r[19] = fantasia, situacao, cnae, municipio, uf
    r[13], r[14], r[15], r[17] = "RUA", "XV DE NOVEMBRO", "100", "CENTRO"
    r[21], r[22], r[27] = ddd, tel, email
    return r


class TestOSM(unittest.TestCase):
    def test_query_padrao_e_categorias(self):
        q = osm.montar_query(3600000001, [])
        self.assertIn("area(id:3600000001)", q)
        self.assertIn('nwr(area.a)["shop"]["name"];', q)
        q = osm.montar_query(1, ["shop=bakery", "craft"])
        self.assertIn('["shop"="bakery"]', q)
        self.assertIn('nwr(area.a)["craft"]["name"];', q)

    def test_para_linha(self):
        el = {
            "type": "node", "id": 42, "lat": -24.7, "lon": -53.7,
            "tags": {"name": "Padaria X", "shop": "bakery", "phone": "+55 45 3333-3333",
                     "contact:instagram": "padaria_x", "addr:street": "Rua A", "addr:housenumber": "10"},
        }
        l = osm.para_linha(el)
        self.assertEqual(l["situacao_site"], "sem_site")
        self.assertEqual(l["instagram"], "@padaria_x")
        self.assertEqual(l["categoria"], "shop=bakery")
        self.assertEqual(l["endereco"], "Rua A 10")
        self.assertEqual(l["fonte"], "https://www.openstreetmap.org/node/42")

    def test_site_instagram_em_website(self):
        el = {"type": "way", "id": 1, "center": {"lat": 1, "lon": 2},
              "tags": {"name": "Loja", "shop": "clothes", "website": "https://instagram.com/loja"}}
        l = osm.para_linha(el)
        self.assertEqual(l["situacao_site"], "instagram")
        self.assertEqual(l["instagram"], "@loja")


class TestCNPJ(unittest.TestCase):
    def test_situacao_email(self):
        self.assertEqual(cnpj.situacao_email("Joao@Gmail.com"), ("email_gratuito", "gmail.com"))
        self.assertEqual(cnpj.situacao_email("contato@padariax.com.br"), ("dominio_proprio", "padariax.com.br"))
        self.assertEqual(cnpj.situacao_email(""), ("sem_email", ""))

    def test_busca_completa(self):
        with tempfile.TemporaryDirectory() as d:
            pasta = Path(d)
            _zip(pasta, "Municipios.zip", "F.MUNICCSV", [["7853", "TOLEDO"], ["9999", "CASCAVEL"]])
            _zip(pasta, "Estabelecimentos0.zip", "F.ESTABELE", [
                _estab("11111111", "PADARIA A", "02", "1091102", "7853", "45", "33330000", "a@gmail.com"),
                _estab("22222222", "PADARIA B", "02", "1091102", "7853", "45", "33331111", "contato@b.com.br"),
                _estab("33333333", "PADARIA C", "08", "1091102", "7853", "45", "3333", "c@gmail.com"),
                _estab("44444444", "OFICINA D", "02", "4520001", "7853", "45", "3334", ""),
                _estab("55555555", "PADARIA E", "02", "1091102", "9999", "45", "3335", "e@gmail.com"),
            ])
            _zip(pasta, "Empresas0.zip", "F.EMPRECSV", [["11111111", "PADARIA A LTDA", "", "", "", "", ""]])

            linhas = list(cnpj.buscar(d, "Toledo", "PR", ["1091"], incluir_dominio_proprio=False))
            self.assertEqual([l["nome_fantasia"] for l in linhas], ["PADARIA A"])
            self.assertEqual(linhas[0]["telefone"], "(45) 33330000")
            self.assertEqual(linhas[0]["cnpj"], "11111111000199")

            cnpj.preencher_razao_social(d, linhas)
            self.assertEqual(linhas[0]["razao_social"], "PADARIA A LTDA")

            todas = list(cnpj.buscar(d, "toledo", None, [], incluir_dominio_proprio=True))
            self.assertEqual({l["nome_fantasia"] for l in todas}, {"PADARIA A", "PADARIA B", "OFICINA D"})


if __name__ == "__main__":
    unittest.main()
