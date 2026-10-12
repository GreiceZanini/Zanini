"""Testes da logica de negociacao, sem rede e sem a biblioteca python-binance."""
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import binance_bot as bb  # noqa: E402
from binance_bot import (BotConfig, ExitRules, Fill, Phase, SimulatedExchange, SymbolFilters, Target,  # noqa: E402
                         TradingBot, floor_to_step, dec_str, parse_order_response)


def filters(symbol):
    return SymbolFilters(symbol=symbol, base_asset=symbol[:-4], quote_asset="USDT",
                         step_size=D("0.00001"), min_qty=D("0.00001"), tick_size=D("0.01"), min_notional=D("5"))


def rules(sl="2", act="3", trail="1"):
    return ExitRules(stop_loss_pct=D(sl), activation_pct=D(act), trail_pct=D(trail))


def make_bot(nexts=("ETHUSDT:3000", "SOLUSDT:150"), repeat=False, fee="0", r=None, state_file=None,
             direction="auto", symbols_log=None):
    cfg = BotConfig(mode="simulate", first=Target.parse("BTCUSDT:60000", direction),
                    next_targets=[Target.parse(n, direction) for n in nexts], quote_amount=D("100"),
                    rules=r or rules(), repeat=repeat, state_file=state_file)
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


def test_target_parse_and_auto_direction():
    t = Target.parse("ethusdt: 3000")
    assert t.symbol == "ETHUSDT" and t.trigger == D("3000")
    assert t.check(D("3100")) is False and t.resolved == "below"   # preco acima -> compra quando cair ate o gatilho
    assert t.check(D("3000")) is True
    t2 = Target.parse("ETHUSDT:3000")
    assert t2.check(D("2900")) is False and t2.resolved == "above"  # preco abaixo -> compra quando subir ate o gatilho
    assert t2.check(D("3000.5")) is True
    t3 = Target.parse("ETHUSDT:3000")
    assert t3.check(D("3000")) is True                              # ja no gatilho -> compra imediata
    with pytest.raises(ValueError):
        Target.parse("ETHUSDT")


def test_explicit_direction():
    t = Target.parse("ETHUSDT:3000", "below")
    assert t.check(D("2900")) is True   # explicito: abaixo do gatilho compra de imediato


def test_rules_validation():
    with pytest.raises(ValueError):
        ExitRules(stop_loss_pct=D("2"), stop_loss_abs=D("10"), activation_pct=D("3"), trail_pct=D("1")).validate()
    with pytest.raises(ValueError):
        ExitRules(stop_loss_pct=D("2"), activation_pct=D("3")).validate()
    r = ExitRules(stop_loss_abs=D("500"), activation_abs=D("1000"), trail_abs=D("300"))
    r.validate()
    assert r.stop_loss_price(D("60000")) == D("59500")
    assert r.activation_price(D("60000")) == D("61000")
    assert r.trailing_stop_price(D("62000")) == D("61700")


# ----------------------------------------------------------------------------- fluxo principal
def test_buy_on_trigger_then_stop_loss():
    bot = make_bot()
    bot.handle_price("BTCUSDT", D("61000"))
    assert bot.phase == Phase.WAITING_BUY
    bot.handle_price("BTCUSDT", D("60000"))
    assert bot.phase == Phase.HOLDING
    pos = bot.position
    assert pos.entry_price == D("60000")
    assert pos.stop_loss_price == D("58800")
    assert pos.activation_price == D("61800")
    bot.handle_price("BTCUSDT", D("59000"))       # acima do stop: mantem
    assert bot.phase == Phase.HOLDING
    bot.handle_price("BTCUSDT", D("58800"))       # toca o stop-loss: vende
    assert bot.phase == Phase.WATCHING_NEXT
    assert bot.position is None
    assert bot.realized_pnl < 0


def test_trailing_stop_sells_on_reversal_not_on_first_target():
    bot = make_bot()
    bot.handle_price("BTCUSDT", D("60000"))
    assert bot.phase == Phase.HOLDING
    bot.handle_price("BTCUSDT", D("61800"))       # lucro minimo (3%) -> arma trailing, nao vende
    assert bot.phase == Phase.HOLDING and bot.position.trailing_armed
    assert bot.position.trailing_stop_price == D("61800") * D("0.99")
    bot.handle_price("BTCUSDT", D("63000"))       # nova maxima: trailing sobe
    assert bot.position.trailing_stop_price == D("63000") * D("0.99")
    bot.handle_price("BTCUSDT", D("62500"))       # recuo < 1%: mantem
    assert bot.phase == Phase.HOLDING
    bot.handle_price("BTCUSDT", D("62370"))       # recuo de 1% da maxima: vende
    assert bot.phase == Phase.WATCHING_NEXT
    assert bot.realized_pnl > 0


def test_trailing_stop_never_moves_down():
    bot = make_bot()
    bot.handle_price("BTCUSDT", D("60000"))
    bot.handle_price("BTCUSDT", D("63000"))
    stop = bot.position.trailing_stop_price
    bot.handle_price("BTCUSDT", D("62800"))
    assert bot.position.trailing_stop_price == stop and bot.position.highest == D("63000")


def test_stop_loss_wins_when_hit_first():
    bot = make_bot()
    bot.handle_price("BTCUSDT", D("60000"))
    bot.handle_price("BTCUSDT", D("57000"))       # gap abaixo do stop: vende por STOP_LOSS
    assert bot.phase == Phase.WATCHING_NEXT


def test_next_list_buys_first_to_hit_and_ignores_others():
    log_syms = []
    bot = make_bot(symbols_log=log_syms)
    bot.handle_price("BTCUSDT", D("60000"))
    bot.handle_price("BTCUSDT", D("50000"))       # stop-loss
    assert bot.phase == Phase.WATCHING_NEXT
    assert log_syms[-1] == ["ETHUSDT", "SOLUSDT"]
    bot.handle_price("ETHUSDT", D("3200"))        # acima do gatilho: espera cair
    bot.handle_price("SOLUSDT", D("140"))         # abaixo do gatilho: espera subir
    assert bot.phase == Phase.WATCHING_NEXT
    bot.handle_price("SOLUSDT", D("150"))         # SOL atinge primeiro
    assert bot.phase == Phase.HOLDING and bot.position.symbol == "SOLUSDT"
    assert log_syms[-1] == ["SOLUSDT"]
    bot.handle_price("ETHUSDT", D("2000"))        # ETH ignorada enquanto em posicao
    assert bot.position.symbol == "SOLUSDT"
    bot.handle_price("SOLUSDT", D("160"))         # arma trailing (3% = 154.5)
    bot.handle_price("SOLUSDT", D("158"))         # recuo de 1.25%: vende
    assert bot.phase == Phase.FINISHED            # ciclo completo sem --repeat
    assert bot.cycles_done == 2


def test_repeat_keeps_watching_list():
    bot = make_bot(repeat=True)
    bot.handle_price("BTCUSDT", D("60000"))
    bot.handle_price("BTCUSDT", D("50000"))
    bot.handle_price("ETHUSDT", D("3000"))
    bot.handle_price("ETHUSDT", D("2000"))
    assert bot.phase == Phase.WATCHING_NEXT
    # direcoes sao reavaliadas a cada nova fase de observacao
    assert all(t.resolved is None for t in bot.cfg.next_targets)


def test_no_next_list_finishes_after_first_sale():
    bot = make_bot(nexts=())
    bot.handle_price("BTCUSDT", D("60000"))
    bot.handle_price("BTCUSDT", D("50000"))
    assert bot.phase == Phase.FINISHED


def test_fee_reduces_quantity_and_pnl():
    bot = make_bot(fee="0.1")
    bot.handle_price("BTCUSDT", D("60000"))
    pos = bot.position
    gross = floor_to_step(D("100") / D("60000"), D("0.00001"))
    assert pos.qty == gross - gross * D("0.001")
    bot.handle_price("BTCUSDT", D("61800"))
    bot.handle_price("BTCUSDT", D("61000"))       # recuo de ~1.3%
    assert bot.phase == Phase.WATCHING_NEXT
    # vendido acima da entrada, mas as taxas reduzem o lucro
    assert D("0") < bot.realized_pnl < D("100") * D("0.0167")


def test_sell_failure_retries_and_keeps_position(monkeypatch):
    bot = make_bot()
    bot.handle_price("BTCUSDT", D("60000"), now=0)
    calls = {"n": 0}

    def failing_sell(f, qty):
        calls["n"] += 1
        raise bb.OrderError("rede caiu")
    monkeypatch.setattr(bot.ex, "market_sell", failing_sell)
    bot.handle_price("BTCUSDT", D("50000"), now=100)
    assert bot.phase == Phase.HOLDING and calls["n"] == 1
    bot.handle_price("BTCUSDT", D("50000"), now=105)   # dentro do intervalo de espera: nao tenta
    assert calls["n"] == 1
    bot.handle_price("BTCUSDT", D("50000"), now=111)   # tenta de novo
    assert calls["n"] == 2
    monkeypatch.undo()
    bot.handle_price("BTCUSDT", D("50000"), now=130)
    assert bot.phase == Phase.WATCHING_NEXT


def test_buy_failures_stop_bot(monkeypatch):
    bot = make_bot()

    def failing_buy(f, q):
        raise bb.OrderError("saldo insuficiente")
    monkeypatch.setattr(bot.ex, "market_buy_quote", failing_buy)
    for t in (0, 20, 40):
        bot.handle_price("BTCUSDT", D("60000"), now=t)
    assert bot.phase == Phase.ERROR


def test_min_notional_validation():
    cfg = BotConfig(mode="simulate", first=Target.parse("BTCUSDT:60000"), next_targets=[],
                    quote_amount=D("3"), rules=rules())
    bot = TradingBot(cfg, SimulatedExchange(filters_source=filters))
    with pytest.raises(ValueError, match="abaixo do minimo"):
        bot.prepare()


def test_state_save_and_resume(tmp_path):
    state = tmp_path / "state.json"
    bot = make_bot(state_file=state)
    bot.handle_price("BTCUSDT", D("60000"))
    bot.handle_price("BTCUSDT", D("63000"))
    assert state.is_file()
    bot2 = make_bot(state_file=state)
    assert bot2.resume() is True
    assert bot2.phase == Phase.HOLDING
    assert bot2.position.trailing_armed and bot2.position.highest == D("63000")
    bot2.handle_price("BTCUSDT", D("62000"))      # abaixo do trailing stop salvo: vende
    assert bot2.phase == Phase.WATCHING_NEXT


def test_shutdown_sell_on_stop():
    bot = make_bot()
    bot.handle_price("BTCUSDT", D("60000"))
    bot.shutdown(sell_position=True)
    assert bot.position is None and bot.cycles_done == 1


def test_shutdown_keeps_position_without_flag():
    bot = make_bot()
    bot.handle_price("BTCUSDT", D("60000"))
    bot.shutdown(sell_position=False)
    assert bot.position is not None


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


def test_cli_config_parsing():
    args = bb.build_parser().parse_args(["--symbol", "btcusdt", "--buy-trigger", "60000", "--quote-amount", "50",
                                         "--stop-loss-pct", "2", "--profit-activation-pct", "3", "--trail-pct", "1",
                                         "--next", "ETHUSDT:3000", "--next", "SOLUSDT:150"])
    cfg = bb.config_from_args(args)
    assert cfg.first.symbol == "BTCUSDT" and len(cfg.next_targets) == 2 and cfg.mode == "simulate"
    bad = bb.build_parser().parse_args(["--symbol", "BTCUSDT", "--buy-trigger", "60000", "--quote-amount", "50",
                                        "--stop-loss-pct", "2", "--profit-activation-pct", "3"])
    with pytest.raises(ValueError):
        bb.config_from_args(bad)


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
    bot = make_bot(nexts=(), symbols_log=None)
    bot.on_symbols_changed = feed.set_symbols
    for p in ("61000", "60000", "61800", "63000", "62000"):
        q.put(("price", "BTCUSDT", D(p), 0.0))
    q.put(("price", "ETHUSDT", D("1"), 0.0))      # simbolo nao observado: ignorado
    args = types.SimpleNamespace(watchdog_seconds=3600, status_seconds=3600)
    bb.run_live_loop(bot, feed, q, args, threading.Event())
    assert bot.phase == Phase.FINISHED
    assert subscribed[0] == ["BTCUSDT"] and subscribed[-1] == [] and "restart" not in subscribed
