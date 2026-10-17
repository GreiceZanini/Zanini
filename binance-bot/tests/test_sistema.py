"""Testes da interface web (sistema.py) com exchange simulada e feed falso - sem rede."""
import json
import logging
import sys
import threading
import time
import urllib.request
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import binance_bot as bb  # noqa: E402
import sistema  # noqa: E402
from tests.test_bot import filters  # noqa: E402


class FakeFeed:
    def __init__(self, q, testnet=False):
        self.q, self.symbols, self.stopped = q, [], False

    def start(self): pass
    def set_symbols(self, s): self.symbols = list(s)
    def restart(self): pass
    def stop(self): self.stopped = True


def make_runner(tmp_path):
    return sistema.BotRunner(tmp_path, exchange_factory=lambda cfg, payload: bb.SimulatedExchange(filters_source=filters),
                             feed_factory=FakeFeed)


PLAN_S = {"symbol": "s/usdt", "trigger": "0,067", "amount_brl": "50", "direction": "auto",
          "stop": {"unit": "money", "value": "1"}, "activation": {"unit": "price", "value": "0.071"},
          "trail": {"unit": "pct", "value": "0.8"}}
PLAN_ETH = {"symbol": "ETHBRL", "trigger": "20000", "amount_brl": "50",
            "stop": {"unit": "money", "value": "1"}, "activation": {"unit": "money", "value": "0.5"},
            "trail": {"unit": "pct", "value": "0.5"}}
START = {"mode": "simulate", "sell_on_stop": True, "plans": [PLAN_S, PLAN_ETH, {"symbol": "", "trigger": ""}]}


def push(runner, sym, price):
    runner.queue.put(("price", sym, D(price), time.time()))


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_config_from_payload_validation(tmp_path):
    cfg = sistema.config_from_payload(START, tmp_path)
    assert [p.symbol for p in cfg.plans] == ["SUSDT", "ETHBRL"]
    assert cfg.plans[0].trigger == D("0.067") and cfg.plans[0].rules.stop.unit == "money"
    with pytest.raises(ValueError, match="CONFIRMO"):
        sistema.config_from_payload({**START, "mode": "live"}, tmp_path)
    with pytest.raises(ValueError, match="stop-loss"):
        sistema.config_from_payload({**START, "plans": [{**PLAN_S, "stop": {"unit": "money", "value": ""}}]}, tmp_path)
    with pytest.raises(ValueError, match="numericos"):
        sistema.config_from_payload({**START, "plans": [{**PLAN_S, "trigger": "abc"}]}, tmp_path)
    with pytest.raises(ValueError, match="pelo menos uma"):
        sistema.config_from_payload({**START, "plans": []}, tmp_path)
    with pytest.raises(ValueError, match="unidade"):
        sistema.config_from_payload({**START, "plans": [{**PLAN_S, "trail": {"unit": "price", "value": "1"}}]}, tmp_path)


def test_start_feed_stop_cycle(tmp_path):
    runner = make_runner(tmp_path)
    st = runner.start(START)
    assert st["running"] and [c["symbol"] for c in st["bot"]["coins"]] == ["SUSDT", "ETHBRL"]
    assert st["bot"]["watched"] == ["SUSDT", "ETHBRL", "USDTBRL"]
    with pytest.raises(ValueError, match="em andamento"):
        runner.start(START)

    push(runner, "USDTBRL", "5.60")
    push(runner, "SUSDT", "0.067")
    push(runner, "ETHBRL", "20000")
    assert wait_for(lambda: len(runner.bot.open_positions()) == 2)

    # alimentar: atualiza ETH (recalcula niveis), inclui SOL
    res = runner.feed({"apply": "upsert", "plans": [{**PLAN_ETH, "stop": {"unit": "money", "value": "2"}},
                                                    {"symbol": "SOLBRL", "trigger": "900", "amount_brl": "30",
                                                     "stop": {"unit": "pct", "value": "2"}, "activation": {"unit": "pct", "value": "1"},
                                                     "trail": {"unit": "pct", "value": "0.5"}}]})
    assert "ETHBRL atualizada" in res["applied"] and "SOLBRL incluida" in res["applied"]
    pos = runner.bot.slots["ETHBRL"].position
    assert pos.stop_loss_price == (D("20000") - D("2") / pos.qty).quantize(D("1e-12"))
    assert runner.bot.watched_symbols() == ["SUSDT", "ETHBRL", "SOLBRL", "USDTBRL"]

    res = runner.feed({"remove": ["SOLBRL"]})
    assert res["applied"] == ["SOLBRL removida"]
    with pytest.raises(ValueError, match="posicao aberta"):
        runner.feed({"remove": ["ETHBRL"]})
    with pytest.raises(ValueError, match="nada para alimentar"):
        runner.feed({})

    # parar vendendo as posicoes
    st = runner.stop(sell_positions=True)
    assert not st["running"]
    assert runner.bot.open_positions() == [] and runner.bot.sales == 2
    with pytest.raises(ValueError, match="nenhuma execucao"):
        runner.stop(sell_positions=False)


def test_stop_keeps_positions_when_not_selling(tmp_path):
    runner = make_runner(tmp_path)
    runner.start(START)
    push(runner, "ETHBRL", "20000")
    assert wait_for(lambda: runner.bot.slots["ETHBRL"].position is not None)
    runner.stop(sell_positions=False)
    assert len(runner.bot.open_positions()) == 1
    assert (tmp_path / "state_simulate.json").is_file()


def test_http_api_roundtrip(tmp_path):
    runner = make_runner(tmp_path)
    memlog = sistema.MemoryLogHandler()
    logging.getLogger().addHandler(memlog)
    old_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.INFO)
    server = sistema.ThreadingHTTPServer(("127.0.0.1", 0), sistema.make_handler(runner, memlog))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        def call(path, data=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if data is not None else "GET",
                                         data=json.dumps(data).encode() if data is not None else None,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    ctype = r.headers.get("Content-Type", "")
                    raw = r.read().decode()
                    return r.status, (json.loads(raw) if "json" in ctype else raw), ctype
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode()), ""

        code, html, ctype = call("/")
        assert code == 200 and "text/html" in ctype and "Iniciar" in html and "Alimentar" in html and "Parar" in html
        code, body, _ = call("/api/start", {**START, "plans": [{**PLAN_S, "symbol": ""}]})
        assert code == 400 and "simbolo" in body["error"]
        code, body, _ = call("/api/start", START)
        assert code == 200 and body["running"]
        code, body, _ = call("/api/status")
        assert body["bot"]["coins"][0]["symbol"] == "SUSDT" and isinstance(body["log"], list)
        assert "wallet" in body["bot"] and body["log_total"] >= len(body["log"])
        code, text, ctype = call("/api/log")
        assert code == 200 and "text/plain" in ctype and "plano: SUSDT" in text
        code, body, _ = call("/api/stop", {"sell_positions": False})
        assert code == 200 and not body["running"]
    finally:
        server.shutdown()
        server.server_close()
        logging.getLogger().removeHandler(memlog)
        logging.getLogger().setLevel(old_level)
