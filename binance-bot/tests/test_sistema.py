"""Testes da interface web (sistema.py) com exchange simulada e feed falso - sem rede."""
import json
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


START = {"mode": "simulate", "symbol": "btcusdt", "buy_trigger": "60000", "quote_amount": "100",
         "stop_loss": {"type": "pct", "value": "2"}, "activation": {"type": "pct", "value": "3"},
         "trail": {"type": "pct", "value": "1"}, "next": [{"symbol": "ETHUSDT", "trigger": "3000"}, {"symbol": "", "trigger": ""}],
         "repeat": False, "sell_on_stop": True}


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
    assert cfg.first.symbol == "BTCUSDT" and len(cfg.next_targets) == 1 and cfg.rules.stop_loss_pct == D("2")
    with pytest.raises(ValueError, match="CONFIRMO"):
        sistema.config_from_payload({**START, "mode": "live"}, tmp_path)
    with pytest.raises(ValueError, match="stop-loss"):
        sistema.config_from_payload({**START, "stop_loss": {"type": "pct", "value": ""}}, tmp_path)
    with pytest.raises(ValueError, match="numerico"):
        sistema.config_from_payload({**START, "buy_trigger": "abc"}, tmp_path)
    cfg2 = sistema.config_from_payload({**START, "quote_amount": "50,5"}, tmp_path)   # virgula decimal aceita
    assert cfg2.quote_amount == D("50.5")


def test_start_feed_stop_cycle(tmp_path):
    runner = make_runner(tmp_path)
    st = runner.start(START)
    assert st["running"] and st["bot"]["phase"] == "WAITING_BUY"
    with pytest.raises(ValueError, match="em andamento"):
        runner.start(START)

    push(runner, "BTCUSDT", "60000")
    assert wait_for(lambda: runner.bot.phase == "HOLDING")

    # alimentar: nova lista de moedas seguintes + regras + valor
    res = runner.feed({"next": [{"symbol": "SOLUSDT", "trigger": "150"}], "next_mode": "replace",
                       "quote_amount": "80", "stop_loss": {"type": "pct", "value": "5"},
                       "activation": {"type": "pct", "value": "4"}, "trail": {"type": "pct", "value": "2"}})
    assert set(res["applied"]) == {"valor por compra", "regras de saida", "moedas seguintes"}
    assert [t.symbol for t in runner.bot.cfg.next_targets] == ["SOLUSDT"]
    assert runner.bot.position.stop_loss_price == D("57000")      # 5% abaixo de 60000
    assert runner.bot.cfg.quote_amount == D("80")

    res = runner.feed({"next": [{"symbol": "ETHUSDT", "trigger": "3000"}], "next_mode": "append"})
    assert [t.symbol for t in runner.bot.cfg.next_targets] == ["SOLUSDT", "ETHUSDT"]

    with pytest.raises(ValueError, match="nada para alimentar"):
        runner.feed({})

    # parar vendendo a posicao
    st = runner.stop(sell_position=True)
    assert not st["running"]
    assert runner.bot.position is None and runner.bot.cycles_done == 1
    with pytest.raises(ValueError, match="nenhuma execucao"):
        runner.stop(sell_position=False)


def test_stop_keeps_position_when_not_selling(tmp_path):
    runner = make_runner(tmp_path)
    runner.start(START)
    push(runner, "BTCUSDT", "60000")
    assert wait_for(lambda: runner.bot.phase == "HOLDING")
    runner.stop(sell_position=False)
    assert runner.bot.position is not None
    assert (tmp_path / "state_simulate.json").is_file()


def test_feed_rejects_other_quote_asset(tmp_path):
    def real_quote_filters(symbol):
        f = bb.SymbolFilters.default(symbol)       # ETHBTC -> cotacao BTC
        f.step_size, f.min_notional = D("0.00001"), D("5")
        return f
    runner = sistema.BotRunner(tmp_path, exchange_factory=lambda cfg, payload: bb.SimulatedExchange(filters_source=real_quote_filters),
                               feed_factory=FakeFeed)
    runner.start(START)
    with pytest.raises(ValueError, match="opera em USDT"):
        runner.feed({"next": [{"symbol": "ETHBTC", "trigger": "0.05"}]})
    runner.stop(sell_position=False)


def test_http_api_roundtrip(tmp_path):
    runner = make_runner(tmp_path)
    memlog = sistema.MemoryLogHandler()
    import logging
    logging.getLogger().addHandler(memlog)
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
        code, body, _ = call("/api/start", {**START, "symbol": ""})
        assert code == 400 and "moeda inicial" in body["error"]
        code, body, _ = call("/api/start", START)
        assert code == 200 and body["running"]
        code, body, _ = call("/api/status")
        assert body["bot"]["first"]["symbol"] == "BTCUSDT" and isinstance(body["log"], list)
        code, body, _ = call("/api/stop", {"sell_position": False})
        assert code == 200 and not body["running"]
    finally:
        server.shutdown()
        server.server_close()
        logging.getLogger().removeHandler(memlog)


def test_payload_money_rules(tmp_path):
    payload = {**START, "symbol": "BTCBRL", "stop_loss": {"type": "money", "value": "2"},
               "activation": {"type": "money", "value": "1,5"}, "trail": {"type": "money", "value": "1"}}
    cfg = sistema.config_from_payload(payload, tmp_path)
    assert cfg.rules.stop_loss_money == D("2") and cfg.rules.activation_money == D("1.5") and cfg.rules.trail_money == D("1")
    with pytest.raises(ValueError, match="unidade"):
        sistema.config_from_payload({**payload, "trail": {"type": "reais", "value": "1"}}, tmp_path)
