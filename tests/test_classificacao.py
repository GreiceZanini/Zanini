import unittest

from prospector.__main__ import montar_consultas, para_linha
from prospector.classificacao import classificar_site, extrair_instagram


class TestClassificacao(unittest.TestCase):
    def test_sem_site(self):
        self.assertEqual(classificar_site(None), "sem_site")
        self.assertEqual(classificar_site("  "), "sem_site")

    def test_redes_sociais(self):
        self.assertEqual(classificar_site("https://www.instagram.com/padaria_x/"), "instagram")
        self.assertEqual(classificar_site("https://m.facebook.com/lojax"), "facebook")
        self.assertEqual(classificar_site("https://wa.me/5545999999999"), "whatsapp")
        self.assertEqual(classificar_site("linktr.ee/loja"), "linktree")
        self.assertEqual(classificar_site("https://loja.business.site/"), "google")

    def test_site_proprio(self):
        self.assertEqual(classificar_site("https://www.padariax.com.br"), "site_proprio")
        self.assertEqual(classificar_site("https://notinstagram.com"), "site_proprio")

    def test_extrair_instagram(self):
        self.assertEqual(extrair_instagram("https://instagram.com/padaria_x?igsh=abc"), "@padaria_x")
        self.assertEqual(extrair_instagram("https://instagram.com/p/xyz"), "")
        self.assertEqual(extrair_instagram("https://padariax.com.br"), "")

    def test_consultas(self):
        self.assertEqual(montar_consultas(["padaria"], "Toledo, PR", []), ["padaria em Toledo, PR"])
        self.assertEqual(len(montar_consultas(["a", "b"], "X", ["b1", "b2"])), 4)

    def test_para_linha(self):
        place = {
            "displayName": {"text": "Padaria X"},
            "nationalPhoneNumber": "(45) 99999-9999",
            "websiteUri": "https://instagram.com/padaria_x",
        }
        linha = para_linha(place, "padaria em Toledo, PR")
        self.assertEqual(linha["situacao_site"], "instagram")
        self.assertEqual(linha["instagram"], "@padaria_x")


if __name__ == "__main__":
    unittest.main()
