"""Invariante: cada uno de los 6 idiomas soportados renderiza un borrador
GDPR válido — tanto el inicial (capa 5 del resolver) como el follow-up
(seguimiento tras 30 días sin respuesta).

El test no llama a la red; solo verifica que las plantillas se completan
sin KeyError y que contienen los marcadores esperados por idioma.
"""
from unittest.mock import patch
from .helpers import IsolatedTestCase


LANGS = ("en", "es", "ru", "pt-BR", "fr", "de")

# Marcadores únicos por idioma para verificar que NO mezclamos textos.
EXPECT_INITIAL = {
    "en":    ["Article 17", "GDPR", "right to erasure"],
    "es":    ["Artículo 17", "supresión"],
    "ru":    ["статьёй 17", "удалить"],
    "pt-BR": ["Artigo 17", "exclusão"],
    "fr":    ["article 17", "effacement"],
    "de":    ["Artikel 17", "Löschung"],
}
EXPECT_FOLLOWUP = {
    "en":    ["Follow-up", "Article 12(3)"],
    "es":    ["Seguimiento", "Artículo 12.3"],
    "ru":    ["Напоминание", "статье 12(3)"],
    "pt-BR": ["Acompanhamento"],
    "fr":    ["Relance", "12.3"],
    "de":    ["Erinnerung", "Art. 12"],
}


class TestGDPRInitialTemplates(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import resolver
        self.resolver = resolver

    def test_todas_las_plantillas_renderizan(self):
        """layer_gdpr produce email_to/subject/body sin errores para cada
        idioma soportado."""
        for lang in LANGS:
            with patch.object(self.resolver, "_find_privacy_emails", return_value=[]):
                res = self.resolver.layer_gdpr(
                    host="example.com", identifier="alice@example.com", lang=lang)
            self.assertEqual(res.kind, "email_draft", f"[{lang}] kind")
            self.assertEqual(res.language, lang,       f"[{lang}] language")
            self.assertTrue(res.email_subject,         f"[{lang}] subject vacío")
            self.assertTrue(res.email_body,            f"[{lang}] body vacío")
            for marker in EXPECT_INITIAL[lang]:
                self.assertIn(marker, res.email_body,
                              f"[{lang}] body no contiene {marker!r}")
            # Inserta host y identifier
            self.assertIn("example.com", res.email_body, f"[{lang}] no incluye host")
            self.assertIn("alice@example.com", res.email_body, f"[{lang}] no incluye identifier")

    def test_idioma_desconocido_cae_a_ingles(self):
        with patch.object(self.resolver, "_find_privacy_emails", return_value=[]):
            res = self.resolver.layer_gdpr("example.com", "alice", lang="xx")
        # No revienta y produce el borrador en inglés (fallback)
        self.assertIn("Article 17", res.email_body)
        self.assertIn("GDPR", res.email_body)

    def test_detect_language_por_tld(self):
        """La detección por TLD cubre los 6 idiomas + cae a 'en' por defecto."""
        cases = {
            "amazon.de": "de", "wikipedia.fr": "fr", "lemonde.it": "it",
            "ya.ru": "ru", "globo.com.br": "pt-BR", "infojobs.es": "es",
            "google.com": "en", "unknown.xyz": "en",
        }
        for host, lang in cases.items():
            got = self.resolver.detect_language(host)
            # 'it' está en _TLD_LANG pero no en EXPECT (no tenemos plantilla GDPR)
            if lang == "it":
                self.assertEqual(got, "it")
            else:
                self.assertEqual(got, lang, f"{host} → esperado {lang}, got {got}")


class TestGDPRFollowupTemplates(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        from rastrillo import config, db
        from rastrillo.server import _FOLLOWUP_PREFIX
        self.config = config
        self.db = db
        self.FOLLOWUP = _FOLLOWUP_PREFIX
        db.init()

    def test_followup_prefix_existe_para_los_6_idiomas(self):
        for lang in LANGS:
            self.assertIn(lang, self.FOLLOWUP,
                          f"falta plantilla follow-up para {lang}")
            subject, body_prefix = self.FOLLOWUP[lang]
            self.assertTrue(subject, f"[{lang}] subject follow-up vacío")
            self.assertTrue(body_prefix, f"[{lang}] body prefix vacío")

    def test_followup_endpoint_compone_con_prefacio_y_body_original(self):
        """El endpoint /followup-draft prepende el prefacio localizado al
        body original guardado en action_meta."""
        import json
        import time
        from .helpers import auth_client

        client = auth_client()
        # Para cada idioma, sembramos una cuenta con su Resolution original
        # y verificamos que el followup tiene los marcadores del idioma.
        for lang in LANGS:
            aid = self.db.upsert_account(
                f"site_{lang}", f"id_{lang}", source="sherlock",
                source_site=f"site-{lang}.example",
                display_name=f"Site {lang}",
                status="user_done", owned=1,
            )
            meta = {
                "kind":"email_draft","layer":"gdpr",
                "title":"Solicitud original",
                "notes":"",
                "url":f"https://site-{lang}.example/",
                "email_to":"privacy@site.example",
                "email_subject":"ORIGINAL_SUBJECT",
                "email_body": f"ORIGINAL_BODY_{lang}",
                "language": lang,
            }
            self.db.update_account(
                aid,
                action_meta=json.dumps(meta, ensure_ascii=False),
                sent_at=time.time() - 45 * 86400,
            )

            r = client.get(f"/api/accounts/{aid}/followup-draft")
            self.assertEqual(r.status_code, 200, f"[{lang}] {r.text}")
            data = r.json()
            self.assertEqual(data["language"], lang)
            self.assertGreaterEqual(data["days_since_sent"], 45)
            # El body del followup contiene PRIMERO el prefacio localizado
            # y al final el body original.
            self.assertIn(f"ORIGINAL_BODY_{lang}", data["email_body"])
            for marker in EXPECT_FOLLOWUP[lang]:
                self.assertIn(
                    marker, data["email_subject"] + " " + data["email_body"],
                    f"[{lang}] follow-up no incluye marcador {marker!r}")
