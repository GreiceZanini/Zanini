"""Testes da logica de negociacao (varias moedas em paralelo), sem rede e sem a biblioteca python-binance."""
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import binance_bot as bb  # noqa: E402
from binance_bot import (BotConfig, CoinPlan, ExitRules, Rule, SimulatedExchange, SlotPhase, SymbolFilters,  # noqa: E402
                         TradingBot, floor_to_step, dec_str, parse_order_response)


def filters(symbol):
    base, quote = bb.split_symbol(symbol)
    return SymbolFilters(symbol=symbol, base_asset=base, quote_asset=quote, step_size=D("0.00001"),
                         min_qty=D("0.00001"), tick_size=D("0.0001"), min_notional=D("5"))


def rules(stop=("money", "1"), act=("price", "0.071"), trail=("pct", "0.8")):
    return ExitRules(Rule(stop[0], D(stop[1])), Rule(act[0], D(act[1])), Rule(trail[0], D(trail[1])))


def plan(symbol="SUSDT", trigger="0.067", amount="50", r=None, direction="auto"):
    return CoinPlan(symbol=symbol, trigger=D(trigger), amount_brl=D(amount), rules=r or rules(), direction=direction)


def make_bot(plans=None, fee="0", state_file=None, symbols_log=None, mode="simulate"):
    cfg = BotConfig(mode=mode, plans=plans or [plan()], state_file=state_file)
    ex = SimulatedExchange(fee_pct=D(fee), filters_source=filters)
    bot = TradingBot(cfg, ex, on_symbols_changed=(symbols_log.append if symbols_log is not None else None))
    bot.prepare()
    return bot


# ----------------------------------------------------------------------------- utilidades
def test_floor_to_step_and_dec_str():
    assert floor_to_step(D("0.0016789"), D("0.00001")) == D("0.00167")
    assert dec_str(floor_to_step(D("0.0016789"), D("0.00001"))) == "0.00167"
    assert dec_str(D("1E-7")) == "0.0000001"
    assert dec_str(D("100.000")) == "100"
    assert bb.normalize_symbol("s/usdt ") == "SUSDT"
    assert bb.to_dec("0,071") == D("0.071")


def test_plan_parse_cli():
    p = CoinPlan.parse("s/usdt trigger=0,067 amount=50 stop=money:1 activation=price:0.071 trail=pct:0.8")
    assert p.symbol == "SUSDT" and p.trigger == D("0.067") and p.amount_brl == D("50")
    assert p.rules.stop.unit == "money" and p.rules.activation.unit == "price" and p.rules.trail.value == D("0.8")
    with pytest.raises(ValueError, match="faltam"):
        CoinPlan.parse("SUSDT trigger=0.067")
    with pytest.raises(ValueError, match="unidade"):
        CoinPlan.parse("SUSDT trigger=0.067 amount=50 stop=reais:1 activation=price:0.071 trail=pct:0.8")
    with pytest.raises(ValueError, match="trailing"):
        CoinPlan.parse("SUSDT trigger=0.067 amount=50 stop=money:1 activation=price:0.071 trail=price:0.07")


def test_config_rejects_duplicate_symbol():
    with pytest.raises(ValueError, match="repetido"):
        BotConfig(mode="simulate", plans=[plan(), plan()]).validate()


# ----------------------------------------------------------------------------- conversao de reais e niveis
def test_brl_amount_converted_by_usdt_rate_and_money_levels():
    bot = make_bot()
    bot.handle_price("USDTBRL", D("5.60"))          # cotacao chega pelo stream
    bot.handle_price("SUSDT", D("0.0690"))          # acima do gatilho: espera cair
    assert bot.slots["SUSDT"].phase == SlotPhase.WAITING
    bot.handle_price("SUSDT", D("0.0670"))          # gatilho
    pos = bot.slots["SUSDT"].position
    assert pos is not None
    quote_spent = D("50") / D("5.60")               # R$50 em USDT
    assert abs(pos.entry_quote - quote_spent) < D("0.0001")
    assert abs(pos.entry_brl - D("50")) < D("0.001")
    # stop em R$1: distancia = (1 / 5.60) / qty
    expected_stop = pos.entry_price - (D("1") / D("5.60")) / pos.qty
    assert abs(pos.stop_loss_price - expected_stop) < D("1e-12")
    assert pos.activation_price == D("0.071")       # minimo em preco


def test_buy_waits_for_rate_when_missing():
    bot = make_bot()
    bot.handle_price("SUSDT", D("0.0670"), now=0)   # sem USDTBRL ainda
    assert bot.slots["SUSDT"].position is None
    bot.handle_price("USDTBRL", D("5.60"), now=5)
    bot.handle_price("SUSDT", D("0.0670"), now=5)
    assert bot.slots["SUSDT"].position is not None


def test_trailing_floor_never_sells_below_minimum():
    """Minimo 0,071 e recuo de 0,8%: a 0,0712 o recuo daria 0,07063 (< minimo) -> vende no minimo 0,071."""
    bot = make_bot()
    bot.handle_price("USDTBRL", D("5.60"))
    bot.handle_price("SUSDT", D("0.0670"))
    pos = bot.slots["SUSDT"].position
    bot.handle_price("SUSDT", D("0.0710"))          # atinge o minimo -> arma
    assert pos.trailing_armed and pos.trailing_stop_price == D("0.071")
    bot.handle_price("SUSDT", D("0.0712"))          # maxima; 0,8% abaixo = 0,0706304 < minimo -> piso 0,071
    assert pos.trailing_stop_price == D("0.071")
    bot.handle_price("SUSDT", D("0.07105"))         # recuo de 0,07%: ainda acima do minimo, mantem
    assert bot.slots["SUSDT"].phase == SlotPhase.HOLDING
    bot.handle_price("SUSDT", D("0.0710"))          # tocou o minimo -> vende
    assert bot.slots["SUSDT"].phase == SlotPhase.WAITING
    assert bot.sales == 1 and bot.realized_brl > 0


def test_trailing_above_floor_uses_percentage():
    bot = make_bot()
    bot.handle_price("USDTBRL", D("5.60"))
    bot.handle_price("SUSDT", D("0.0670"))
    pos = bot.slots["SUSDT"].position
    bot.handle_price("SUSDT", D("0.0800"))          # bem acima do minimo: 0,8% abaixo = 0,07936 > 0,071
    assert pos.trailing_stop_price == D("0.0800") * (1 - D("0.008"))
    bot.handle_price("SUSDT", D("0.0795"))          # recuo < 0,8%: mantem
    assert bot.slots["SUSDT"].phase == SlotPhase.HOLDING
    bot.handle_price("SUSDT", D("0.07930"))         # recuo > 0,8%: vende acima do minimo
    assert bot.slots["SUSDT"].phase == SlotPhase.WAITING


def test_stop_loss_in_reais():
    bot = make_bot(plans=[plan(symbol="ETHBRL", trigger="20000", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5")))])
    bot.handle_price("ETHBRL", D("20000"))
    pos = bot.slots["ETHBRL"].position
    assert pos.rate == D("1") and pos.entry_brl == D("50")
    qty = pos.qty
    assert pos.stop_loss_price == D("20000") - D("1") / qty      # perder no maximo R$1
    assert pos.activation_price == D("20000") + D("0.5") / qty   # lucro minimo R$0,50
    bot.handle_price("ETHBRL", pos.stop_loss_price)
    assert bot.slots["ETHBRL"].phase == SlotPhase.WAITING
    assert abs(bot.realized_brl - D("-1")) < D("0.0001")


def test_activation_profit_in_reais_and_pct_stop():
    bot = make_bot(plans=[plan(symbol="ETHBRL", trigger="20000", r=rules(("pct", "2"), ("money", "2"), ("pct", "1")))])
    bot.handle_price("ETHBRL", D("20000"))
    pos = bot.slots["ETHBRL"].position
    assert pos.stop_loss_price == D("19600")
    bot.handle_price("ETHBRL", pos.activation_price)             # lucro de R$2 -> arma
    assert pos.trailing_armed
    assert pos.qty * (pos.activation_price - pos.entry_price) == D("2")


# ----------------------------------------------------------------------------- varias moedas em paralelo
def test_parallel_positions_and_requeue_after_sale():
    log_syms = []
    bot = make_bot(plans=[plan(), plan(symbol="ETHBRL", trigger="20000", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5")))],
                   symbols_log=log_syms)
    assert log_syms[-1] == ["SUSDT", "ETHBRL", "USDTBRL"]     # moedas + par de cotacao
    bot.handle_price("USDTBRL", D("5.60"))
    bot.handle_price("SUSDT", D("0.0670"))
    bot.handle_price("ETHBRL", D("20000"))
    assert len(bot.open_positions()) == 2                       # duas posicoes ao mesmo tempo
    bot.handle_price("ETHBRL", D("20300"))                      # ETH arma e sobe
    bot.handle_price("ETHBRL", D("20150"))                      # recuo 0,74% > 0,5%: vende ETH
    assert bot.slots["ETHBRL"].phase == SlotPhase.WAITING and bot.slots["SUSDT"].phase == SlotPhase.HOLDING
    # ETH volta a esperar o gatilho: preco acima do gatilho -> compra quando cair ate 20000 de novo
    bot.handle_price("ETHBRL", D("20100"))
    assert bot.slots["ETHBRL"].resolved == "below" and bot.slots["ETHBRL"].position is None
    bot.handle_price("ETHBRL", D("20000"))
    assert bot.slots["ETHBRL"].position is not None             # recomprou
    assert bot.sales == 1


def test_requeue_after_stop_loss_buys_when_price_climbs_back():
    bot = make_bot(plans=[plan(symbol="ETHBRL", trigger="20000", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5")))])
    bot.handle_price("ETHBRL", D("20000"))
    bot.handle_price("ETHBRL", D("19000"))                      # stop-loss, preco abaixo do gatilho
    assert bot.slots["ETHBRL"].position is None
    bot.handle_price("ETHBRL", D("19500"))                      # resolve: compra quando subir ate 20000
    assert bot.slots["ETHBRL"].resolved == "above"
    bot.handle_price("ETHBRL", D("20000"))
    assert bot.slots["ETHBRL"].position is not None


def test_update_plans_upsert_replace_remove():
    bot = make_bot(plans=[plan(symbol="ETHBRL", trigger="20000", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5")))])
    bot.handle_price("ETHBRL", D("20000"))
    changed = bot.update_plans([plan(symbol="ETHBRL", trigger="20000", r=rules(("money", "2"), ("money", "1"), ("pct", "0.5"))),
                                plan(symbol="SOLBRL", trigger="900", r=rules(("money", "1"), ("money", "1"), ("pct", "1")))])
    assert "ETHBRL atualizada" in changed and "SOLBRL incluida" in changed
    pos = bot.slots["ETHBRL"].position
    assert pos.stop_loss_price == (D("20000") - D("2") / pos.qty).quantize(D("1e-12"))   # niveis recalculados na posicao aberta
    with pytest.raises(ValueError, match="posicao aberta"):
        bot.remove_plans(["ETHBRL"])
    assert bot.remove_plans(["SOLBRL"]) == ["SOLBRL"]
    bot.update_plans([plan(symbol="BTCBRL", trigger="500000")], mode="replace")
    assert "ETHBRL" in bot.slots and "BTCBRL" in bot.slots      # ETH com posicao nao e removida
    assert [p.symbol for p in bot.cfg.plans] == ["ETHBRL", "BTCBRL"]


def test_sell_failure_retries_and_keeps_position(monkeypatch):
    bot = make_bot(plans=[plan(symbol="ETHBRL", trigger="20000", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5")))])
    bot.handle_price("ETHBRL", D("20000"), now=0)
    calls = {"n": 0}

    def failing_sell(f, qty):
        calls["n"] += 1
        raise bb.OrderError("rede caiu")
    monkeypatch.setattr(bot.ex, "market_sell", failing_sell)
    bot.handle_price("ETHBRL", D("19000"), now=100)
    assert bot.slots["ETHBRL"].phase == SlotPhase.HOLDING and calls["n"] == 1
    bot.handle_price("ETHBRL", D("19000"), now=105)
    assert calls["n"] == 1
    bot.handle_price("ETHBRL", D("19000"), now=111)
    assert calls["n"] == 2
    monkeypatch.undo()
    bot.handle_price("ETHBRL", D("19000"), now=130)
    assert bot.slots["ETHBRL"].phase == SlotPhase.WAITING


def test_buy_failures_disable_only_that_coin(monkeypatch):
    bot = make_bot(plans=[plan(symbol="ETHBRL", trigger="20000", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5"))),
                          plan(symbol="SOLBRL", trigger="900", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5")))])

    def failing_buy(f, q):
        raise bb.OrderError("saldo insuficiente")
    monkeypatch.setattr(bot.ex, "market_buy_quote", failing_buy)
    for t in (0, 20, 40):
        bot.handle_price("ETHBRL", D("20000"), now=t)
    assert bot.slots["ETHBRL"].phase == SlotPhase.ERROR and bot.phase == "RUNNING"
    monkeypatch.undo()
    bot.handle_price("SOLBRL", D("900"), now=50)
    assert bot.slots["SOLBRL"].position is not None


def test_min_notional_validation_on_buy():
    bot = make_bot(plans=[plan(symbol="ETHBRL", trigger="20000", amount="3", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5")))])
    bot.handle_price("ETHBRL", D("20000"), now=0)
    assert bot.slots["ETHBRL"].position is None and bot.slots["ETHBRL"].buy_failures == 1


def test_state_save_and_resume(tmp_path):
    state = tmp_path / "state.json"
    p = plan(symbol="ETHBRL", trigger="20000", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5")))
    bot = make_bot(plans=[p], state_file=state)
    bot.handle_price("ETHBRL", D("20000"))
    bot.handle_price("ETHBRL", D("20300"))
    assert state.is_file()
    bot2 = make_bot(plans=[plan(symbol="SOLBRL", trigger="900")], state_file=state)   # plano novo sem ETH
    assert bot2.resume() == 1
    assert "ETHBRL" in bot2.slots and bot2.slots["ETHBRL"].position.trailing_armed
    bot2.handle_price("ETHBRL", D("20100"))      # abaixo do trailing salvo: vende
    assert bot2.slots["ETHBRL"].phase == SlotPhase.WAITING


def test_shutdown_sells_all_or_keeps():
    bot = make_bot(plans=[plan(symbol="ETHBRL", trigger="20000", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5"))),
                          plan(symbol="SOLBRL", trigger="900", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5")))])
    bot.handle_price("ETHBRL", D("20000"))
    bot.handle_price("SOLBRL", D("900"))
    bot.shutdown(sell_positions=False)
    assert len(bot.open_positions()) == 2
    bot.shutdown(sell_positions=True)
    assert len(bot.open_positions()) == 0 and bot.sales == 2


def test_snapshot_contains_coins_and_brl_values():
    bot = make_bot()
    bot.handle_price("USDTBRL", D("5.60"))
    bot.handle_price("SUSDT", D("0.0670"))
    snap = bot.snapshot()
    coin = snap["coins"][0]
    assert coin["symbol"] == "SUSDT" and coin["phase"] == "HOLDING" and coin["rate"] == "5.6"
    assert coin["position"]["value_brl"] == "50"
    assert snap["watched"] == ["SUSDT", "USDTBRL"]


# ----------------------------------------------------------------------------- parse da resposta real
def test_parse_order_response_buy_with_base_commission():
    resp = {"orderId": 123, "status": "FILLED", "executedQty": "0.00166000", "cummulativeQuoteQty": "99.60000000",
            "transactTime": 1700000000000,
            "fills": [{"price": "60000.00", "qty": "0.00100000", "commission": "0.00000100", "commissionAsset": "BTC"},
                      {"price": "60000.00", "qty": "0.00066000", "commission": "0.00000066", "commissionAsset": "BTC"}]}
    fill = parse_order_response(resp, filters("BTCUSDT"), "BUY")
    assert fill.qty == D("0.00166000") - D("0.00000166")
    assert fill.price == D("60000")
    assert fill.quote == D("99.6") and fill.order_id == "123"


def test_parse_order_response_sell_with_quote_commission():
    resp = {"orderId": 9, "status": "FILLED", "executedQty": "0.00165", "cummulativeQuoteQty": "102.3",
            "fills": [{"price": "62000", "qty": "0.00165", "commission": "0.1023", "commissionAsset": "USDT"}]}
    fill = parse_order_response(resp, filters("BTCUSDT"), "SELL")
    assert fill.quote == D("102.3") - D("0.1023")
    assert fill.qty == D("0.00165")


def test_parse_order_response_not_filled():
    with pytest.raises(bb.OrderError):
        parse_order_response({"status": "EXPIRED", "executedQty": "0", "cummulativeQuoteQty": "0"}, filters("BTCUSDT"), "BUY")


def test_cli_config_parsing(tmp_path):
    args = bb.build_parser().parse_args(["--plan", "SUSDT trigger=0.067 amount=50 stop=money:1 activation=price:0.071 trail=pct:0.8",
                                         "--plan", "ETHBRL trigger=20000 amount=50 stop=money:1 activation=money:2 trail=pct:0.8",
                                         "--data-dir", str(tmp_path)])
    cfg = bb.config_from_args(args)
    assert [p.symbol for p in cfg.plans] == ["SUSDT", "ETHBRL"] and cfg.sell_on_stop
    cfg_json = tmp_path / "p.json"
    cfg_json.write_text('{"plans": [' + __import__("json").dumps(cfg.plans[0].to_dict()) + ']}', encoding="utf-8")
    args2 = bb.build_parser().parse_args(["--config", str(cfg_json), "--no-sell-on-stop", "--data-dir", str(tmp_path)])
    cfg2 = bb.config_from_args(args2)
    assert cfg2.plans[0].symbol == "SUSDT" and not cfg2.sell_on_stop
    with pytest.raises(ValueError, match="pelo menos uma"):
        bb.config_from_args(bb.build_parser().parse_args(["--data-dir", str(tmp_path)]))


# ----------------------------------------------------------------------------- feed e loop principal
def test_price_feed_message_parsing():
    import queue
    q = queue.Queue()
    feed = bb.PriceFeed.__new__(bb.PriceFeed)
    feed._q = q
    feed._on_message({"stream": "btcusdt@trade", "data": {"e": "trade", "s": "BTCUSDT", "p": "60000.10", "T": 1}})
    feed._on_message({"e": "error", "type": "ConnectionClosedError", "m": "closed"})
    feed._on_message({"stream": "btcusdt@trade", "data": {"e": "trade", "s": "BTCUSDT"}})   # sem preco: ignorada
    kind, sym, price, _ = q.get_nowait()
    assert (kind, sym, price) == ("price", "BTCUSDT", D("60000.10"))
    assert q.get_nowait()[0] == "error"
    assert q.empty()


def test_run_live_loop_with_fake_feed():
    import queue, threading, types
    q = queue.Queue()
    subscribed = []

    class FakeFeed:
        def start(self): pass
        def set_symbols(self, s): subscribed.append(list(s))
        def restart(self): subscribed.append("restart")
        def stop(self): pass

    feed = FakeFeed()
    bot = make_bot(plans=[plan(symbol="ETHBRL", trigger="20000", r=rules(("money", "1"), ("money", "0.5"), ("pct", "0.5")))])
    bot.on_symbols_changed = feed.set_symbols
    for p in ("20100", "20000", "20300", "20150"):
        q.put(("price", "ETHBRL", D(p), 0.0))
    q.put(("price", "XRPBRL", D("1"), 0.0))      # simbolo nao observado: ignorado
    stop = threading.Event()
    q.put(("command", lambda b: stop.set()))      # comando processado na thread do loop encerra o teste
    args = types.SimpleNamespace(watchdog_seconds=3600, status_seconds=3600)
    bb.run_live_loop(bot, feed, q, args, stop)
    assert bot.sales == 1 and bot.slots["ETHBRL"].phase == SlotPhase.WAITING
    assert subscribed[0] == ["ETHBRL"] and "restart" not in subscribed
