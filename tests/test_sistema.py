import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prospector import busca_web, cnpj_indice, google_places, servidor


class TestGoogleArea(unittest.TestCase):
    def test_retangulo_e_divisao(self):
        r = google_places.retangulo(-24.72, -53.74, 1000)
        self.assertLess(r["low"]["latitude"], -24.72)
        self.assertGreater(r["high"]["longitude"], -53.74)
        partes = google_places._dividir(r)
        self.assertEqual(len(partes), 4)

    def test_divide_quando_bate_60(self):
        chamadas = []

        def falso(texto, key, extra=None):
            chamadas.append(extra)
            if len(chamadas) == 1:  # área inteira: 3 páginas cheias = teto de 60
                for _ in range(3):
                    yield [{"id": "x"}] * 20
            else:  # cada quadrante: 1 página com 5
                yield [{"id": "y"}] * 5

        with mock.patch.object(google_places, "buscar_paginas", side_effect=falso):
            lugares, usadas = google_places.buscar_area("loja", google_places.retangulo(0, 0, 1000), "k", 100, 1)
        self.assertEqual(usadas, 3 + 4)
        self.assertEqual(len(lugares), 60 + 4 * 5)
        self.assertTrue(all("locationRestriction" in c for c in chamadas))

    def test_respeita_limite(self):
        def falso(texto, key, extra=None):
            for _ in range(3):
                yield [{"id": "x"}] * 20

        with mock.patch.object(google_places, "buscar_paginas", side_effect=lambda *a, **k: falso(*a, **k)):
            _, usadas = google_places.buscar_area("loja", google_places.retangulo(0, 0, 1000), "k", 2)
        self.assertEqual(usadas, 2)


class TestEmpresaGoogle(unittest.TestCase):
    def test_conversao(self):
        e = servidor._empresa_google({
            "id": "abc", "displayName": {"text": "Padaria X"}, "websiteUri": "https://instagram.com/padx",
            "nationalPhoneNumber": "(45) 3333-3333", "location": {"latitude": 1, "longitude": 2},
            "googleMapsUri": "https://maps.google.com/?cid=1",
        })
        self.assertEqual(e["site"], "")
        self.assertEqual(e["instagram"], "@padx")
        self.assertEqual(e["links"][0]["origem"], "Google Maps")


class TestBuscaWeb(unittest.TestCase):
    def test_links_manuais(self):
        l = busca_web.links_manuais("Padaria X", "Toledo PR")
        self.assertIn("Instagram", l)
        self.assertIn("site%3Ainstagram.com", l["Instagram"])

    def test_pesquisar_classifica(self):
        resultados = [
            {"titulo": "Padaria X - Toledo", "url": "https://www.padariax.com.br/", "trecho": ""},
            {"titulo": "Padaria X (@padariax)", "url": "https://www.instagram.com/padariax/", "trecho": ""},
            {"titulo": "Padaria X | LinkedIn", "url": "https://br.linkedin.com/company/padariax", "trecho": ""},
            {"titulo": "Padaria X CNPJ", "url": "https://cnpj.biz/123", "trecho": ""},
            {"titulo": "Outra empresa", "url": "https://outra.com.br", "trecho": ""},
        ]
        with mock.patch.object(busca_web, "_brave", return_value=resultados):
            r = busca_web.pesquisar("Padaria X", "Toledo PR", "chave")
        self.assertEqual(r["site"], "https://www.padariax.com.br/")
        self.assertEqual(r["instagram"], "@padariax")
        self.assertIn("linkedin.com", r["linkedin"])
        self.assertNotIn("https://outra.com.br", [x["url"] for x in r["links"]])


class TestIndiceCNPJ(unittest.TestCase):
    def test_localizar(self):
        with tempfile.TemporaryDirectory() as d:
            arq = Path(d) / "c.sqlite"
            con = sqlite3.connect(arq)
            con.execute("""CREATE TABLE est (cnpj TEXT, basico TEXT, razao TEXT, fantasia TEXT, chave TEXT,
                municipio TEXT, uf TEXT, cnae TEXT, telefone TEXT, telefone2 TEXT, email TEXT,
                endereco TEXT, bairro TEXT, cep TEXT, inicio TEXT)""")
            con.execute("INSERT INTO est VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "11111111000199", "11111111", "SABOR DO PAO PANIFICADORA LTDA", "SABOR DO PAO",
                cnpj_indice._chave("SABOR DO PAO"), "TOLEDO", "PR", "1091102", "(45) 30548857", "",
                "sabor@gmail.com", "RUA PIRATINI 1551", "CENTRO", "85900000", "20100101"))
            con.commit()
            con.close()
            r = cnpj_indice.localizar("Sabor do Pão", "Toledo", arquivo=arq)
            self.assertEqual(r["cnpj"], "11111111000199")
            self.assertIsNone(cnpj_indice.localizar("Mecânica Silva", "Toledo", arquivo=arq))
            self.assertIsNone(cnpj_indice.localizar("Sabor do Pão", "Cascavel", arquivo=arq))


if __name__ == "__main__":
    unittest.main()


class TestFichaGoogle(unittest.TestCase):
    def test_nome_bate_palavra_inteira(self):
        self.assertTrue(busca_web._nome_bate("OLÉ Futebol Society", "Olé Futebol Society - Passo Fundo-RS"))
        self.assertFalse(busca_web._nome_bate("OLÉ Futebol Society", "Toledo Futebol Clube"))

    def test_descarta_longe(self):
        perto = {"id": "1", "displayName": {"text": "Padaria X"}, "location": {"latitude": 0.001, "longitude": 0},
                 "websiteUri": "https://padariax.com.br"}
        longe = dict(perto, id="2", location={"latitude": 1, "longitude": 1})

        def falso(pagina):
            return lambda *a, **k: iter([pagina])

        with mock.patch.object(servidor, "buscar_paginas", side_effect=falso([longe])):
            self.assertIsNone(servidor.ficha_google("Padaria X", "Toledo", "PR", "k", 0, 0))
        with mock.patch.object(servidor, "buscar_paginas", side_effect=falso([longe, perto])):
            self.assertEqual(servidor.ficha_google("Padaria X", "Toledo", "PR", "k", 0, 0)["site"], "https://padariax.com.br")


class TestBraveModos(unittest.TestCase):
    def setUp(self):
        busca_web._modo["atual"] = None

    def test_cai_para_llm_context_quando_chave_e_de_outro_produto(self):
        def falso_get(url, chave):
            if "/web/search" in url:
                raise busca_web.ErroBusca("HTTP 403: plano não inclui este produto")
            return {"grounding": {"generic": [{
                "url": "https://olesociety.com.br/contato", "title": "Olé Futebol Society - Contato",
                "snippets": ["Olé Futebol Society. Telefone (54) 99931-3505. E-mail contato@olesociety.com.br"],
            }]}}

        with mock.patch.object(busca_web, "_get", side_effect=falso_get):
            r = busca_web.pesquisar("Olé Futebol Society", "Passo Fundo RS", "k")
        self.assertEqual(r["modo"], "llm")
        self.assertEqual(r["site"], "https://olesociety.com.br/contato")
        self.assertEqual(r["contatos"]["emails"][0]["valor"], "contato@olesociety.com.br")
        self.assertEqual(r["contatos"]["telefones"][0]["valor"], "(54) 99931-3505")

    def test_erro_real_nao_troca_de_modo(self):
        with mock.patch.object(busca_web, "_get", side_effect=busca_web.ErroBusca("HTTP 429: limite")):
            with self.assertRaises(busca_web.ErroBusca):
                busca_web._brave("x", "k")
        self.assertIsNone(busca_web._modo["atual"])

    def test_extrair_cnpj_formata(self):
        c = busca_web.extrair_contatos([{"url": "u", "trecho": "CNPJ 12345678000190"}])
        self.assertEqual(c["cnpjs"][0]["valor"], "12.345.678/0001-90")
