"""Endurecimiento del bucle IA: presupuesto de tokens + heurística de éxito.

No usa Anthropic real. Mockea `_ask_agent` para devolver acciones canned y
una `FakePage` con `url`/`inner_text` para que la heurística pueda mirar.
"""
import os
from unittest.mock import patch
from .helpers import IsolatedTestCase


class FakePage:
    """Page mínima para el agente. URL e inner_text cambian sincronizados
    con la acción que ejecuta (el test controla el escenario)."""
    def __init__(self):
        self.url = "https://example.com/settings"
        self._body = "Settings page. Delete account button below."
        self.calls = []

    def click(self, selector, timeout=None):
        self.calls.append(("click", selector))
        # Simulamos navegación tras "delete final": URL cambia + body con keyword.
        if "confirm" in (selector or "").lower() or "final" in (selector or "").lower():
            self.url = "https://example.com/closed"
            self._body = "Your account has been deleted. Thanks."

    def fill(self, selector, value):
        self.calls.append(("fill", selector))

    def wait_for_selector(self, selector, timeout=None):
        self.calls.append(("wait", selector))

    def inner_text(self, selector="body"):
        return self._body

    def screenshot(self, path=None):
        pass

    @property
    def accessibility(self):
        # No usamos el snapshot real en tests; ai_assist._snapshot maneja excepción.
        raise RuntimeError("no accessibility en FakePage")

    def title(self):
        return "T"


class TestAIAgentLoop(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        # Habilitamos la rama "available()=True" del módulo IA. Anthropic SDK
        # está instalado en el venv y la key se usa solo dentro de _ask_agent
        # (que vamos a mockear). Forzamos la clave en el módulo importado:
        from rastrillo import config, ai_assist
        config.ANTHROPIC_API_KEY = "fake-key-for-tests"
        # available() chequea anthropic instalado + key
        assert ai_assist.available(), "el test requiere anthropic instalado"
        self.ai = ai_assist

    def test_heuristica_exito_cierra_sin_done_explicito(self):
        """Tras una acción que cambia URL + body con keyword, el bucle marca
        done en el SIGUIENTE turno sin esperar a que el modelo responda."""
        page = FakePage()
        # Secuencia mockeada: 1er turno click confirm; 2º turno NO debería invocarse
        sequence = iter([
            ({"action":"click", "selector":"#confirm-final", "reason":"go"}, 100),
        ])
        def fake_ask(client, goal, snap, history):
            try:
                return next(sequence)
            except StopIteration:
                # Si el bucle pide otro turno, fallamos: significa que la
                # heurística NO cortó.
                self.fail("la heurística debería haber cerrado tras el click")

        with patch.object(self.ai, "_ask_agent", side_effect=fake_ask):
            result = self.ai.run_agent(page, goal="borrar cuenta", max_iters=5)

        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["result_status"], "deleted")
        self.assertIn("URL cambió", result["reason"])
        # El log debe tener la acción ejecutada + la entrada auto_done
        actions = [e.get("action", {}).get("action") for e in result["log"]]
        self.assertIn("click", actions)
        self.assertIn("auto_done", actions)

    def test_presupuesto_tokens_agotado(self):
        """Si los tokens acumulados superan el budget, el bucle corta con
        outcome='exhausted_tokens' sin pedir otro turno."""
        page = FakePage()
        # Cada llamada devuelve un wait benigno con 6000 tokens "consumidos".
        def fake_ask(client, goal, snap, history):
            return ({"action":"wait", "selector":"#x", "reason":"x"}, 6000)

        with patch.object(self.ai, "_ask_agent", side_effect=fake_ask):
            result = self.ai.run_agent(page, goal="x", max_iters=10,
                                       token_budget=10000)
        # Tras 2 turnos (6000+6000=12000 > 10000), el 3er turno aborta.
        self.assertEqual(result["outcome"], "exhausted_tokens")
        self.assertGreaterEqual(result["tokens_used"], 10000)

    def test_done_explicito_del_modelo(self):
        page = FakePage()
        def fake_ask(client, goal, snap, history):
            return ({"action":"done", "outcome":"deleted", "reason":"listo"}, 50)
        with patch.object(self.ai, "_ask_agent", side_effect=fake_ask):
            result = self.ai.run_agent(page, goal="x", max_iters=3)
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["result_status"], "deleted")

    def test_need_user_corta_el_bucle(self):
        page = FakePage()
        def fake_ask(client, goal, snap, history):
            return ({"action":"need_user", "reason":"captcha detectado"}, 80)
        with patch.object(self.ai, "_ask_agent", side_effect=fake_ask):
            result = self.ai.run_agent(page, goal="x", max_iters=8)
        self.assertEqual(result["outcome"], "need_user")
        self.assertIn("captcha", result["reason"])

    def test_sin_api_key_devuelve_no_ai(self):
        from rastrillo import config
        config.ANTHROPIC_API_KEY = None
        result = self.ai.run_agent(FakePage(), goal="x")
        self.assertEqual(result["outcome"], "no_ai")
        self.assertEqual(result["tokens_used"], 0)
        self.assertEqual(result["log"], [])
