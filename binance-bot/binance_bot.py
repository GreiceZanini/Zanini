#!/usr/bin/env python3
"""
binance_bot.py - Automacao de compra e venda SPOT na Binance.

Fluxo:
  1. Aguarda o preco da moeda inicial atingir o gatilho de compra -> compra a mercado.
  2. Em posicao, monitora via WebSocket:
       - stop-loss fixo abaixo do preco de compra;
       - trailing stop: armado quando o lucro minimo e atingido; acompanha a maxima
         e vende quando o preco recua um percentual/valor a partir dela.
     Vende pelo que acionar primeiro.
  3. Apos a venda, observa a lista de moedas seguintes (cada uma com seu gatilho)
     e compra a primeira que atingir o gatilho, ignorando as demais.
  4. Repete o ciclo de saida. Ao final (ou com --repeat, indefinidamente) encerra.

Modos: simulate (padrao, sem ordens reais), testnet (Spot Testnet) e live (dinheiro real).
Credenciais: variaveis de ambiente BINANCE_API_KEY / BINANCE_API_SECRET
(ou BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET para a testnet).

Biblioteca: python-binance (https://github.com/sammchardy/python-binance).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger("binance_bot")

ZERO = Decimal("0")
DEFAULT_STEP = Decimal("0.00000001")


# --------------------------------------------------------------------------- utilidades
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def to_dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"valor numerico invalido: {value!r}") from exc


def dec_str(value: Decimal) -> str:
    """Representacao decimal simples (sem notacao cientifica), sem zeros a direita."""
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Arredonda para baixo ao multiplo de `step` (LOT_SIZE.stepSize / PRICE_FILTER.tickSize)."""
    if step <= ZERO:
        return value
    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return (steps * step).quantize(step)


def load_dotenv(path: Path = Path(".env")) -> None:
    """Carrega KEY=VALUE de um arquivo .env sem sobrescrever variaveis ja definidas."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


# --------------------------------------------------------------------------- modelos
@dataclass
class SymbolFilters:
    symbol: str
    base_asset: str
    quote_asset: str
    step_size: Decimal = DEFAULT_STEP
    min_qty: Decimal = ZERO
    tick_size: Decimal = DEFAULT_STEP
    min_notional: Decimal = ZERO

    @classmethod
    def from_exchange_info(cls, info: dict) -> "SymbolFilters":
        f = cls(symbol=info["symbol"], base_asset=info["baseAsset"], quote_asset=info["quoteAsset"])
        for flt in info.get("filters", []):
            t = flt.get("filterType")
            if t == "LOT_SIZE":
                f.step_size = to_dec(flt["stepSize"])
                f.min_qty = to_dec(flt["minQty"])
            elif t == "PRICE_FILTER":
                f.tick_size = to_dec(flt["tickSize"])
            elif t in ("NOTIONAL", "MIN_NOTIONAL"):
                f.min_notional = to_dec(flt.get("minNotional", "0"))
        return f

    @classmethod
    def default(cls, symbol: str) -> "SymbolFilters":
        """Filtros genericos (apenas para replay offline)."""
        base, quote = split_symbol(symbol)
        return cls(symbol=symbol, base_asset=base, quote_asset=quote)


def split_symbol(symbol: str):
    for q in ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "BNB", "BRL", "EUR", "TRY"):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    return symbol[:-4], symbol[-4:]


@dataclass
class Fill:
    symbol: str
    side: str                 # BUY | SELL
    qty: Decimal              # BUY: quantidade liquida recebida; SELL: quantidade vendida
    price: Decimal            # preco medio
    quote: Decimal            # BUY: total gasto; SELL: total liquido recebido (apos taxas em quote)
    commission: Decimal
    commission_asset: str
    order_id: str
    time: datetime


@dataclass
class Target:
    symbol: str
    trigger: Decimal
    direction: str = "auto"           # auto | below | above
    resolved: Optional[str] = None    # below => compra quando preco <= gatilho; above => >= gatilho

    def check(self, price: Decimal) -> bool:
        if self.resolved is None:
            if self.direction in ("below", "above"):
                self.resolved = self.direction
            elif price > self.trigger:
                self.resolved = "below"
            elif price < self.trigger:
                self.resolved = "above"
            else:
                return True
            log.info("%s: gatilho %s, preco atual %s -> compra quando preco %s gatilho",
                     self.symbol, dec_str(self.trigger), dec_str(price),
                     "<=" if self.resolved == "below" else ">=")
        return price <= self.trigger if self.resolved == "below" else price >= self.trigger

    @classmethod
    def parse(cls, text: str, direction: str = "auto") -> "Target":
        if ":" not in text:
            raise ValueError(f"formato invalido '{text}'. Use SIMBOLO:PRECO (ex.: ETHUSDT:3000)")
        sym, _, price = text.partition(":")
        trigger = to_dec(price.strip())
        if trigger <= ZERO:
            raise ValueError(f"gatilho deve ser positivo: {text}")
        return cls(symbol=sym.strip().upper(), trigger=trigger, direction=direction)


@dataclass
class ExitRules:
    """Regras de saida. Para cada item, usa-se pct OU abs (nunca ambos)."""
    stop_loss_pct: Optional[Decimal] = None
    stop_loss_abs: Optional[Decimal] = None
    activation_pct: Optional[Decimal] = None
    activation_abs: Optional[Decimal] = None
    trail_pct: Optional[Decimal] = None
    trail_abs: Optional[Decimal] = None

    def validate(self) -> None:
        pairs = [("stop-loss", self.stop_loss_pct, self.stop_loss_abs),
                 ("ativacao do lucro", self.activation_pct, self.activation_abs),
                 ("trailing", self.trail_pct, self.trail_abs)]
        for name, pct, ab in pairs:
            if (pct is None) == (ab is None):
                raise ValueError(f"informe exatamente um valor para {name} (percentual ou absoluto)")
            v = pct if pct is not None else ab
            if v <= ZERO:
                raise ValueError(f"{name} deve ser positivo")
        if self.stop_loss_pct is not None and self.stop_loss_pct >= 100:
            raise ValueError("stop-loss percentual deve ser menor que 100")

    def stop_loss_price(self, entry: Decimal) -> Decimal:
        if self.stop_loss_pct is not None:
            return entry * (1 - self.stop_loss_pct / 100)
        return entry - self.stop_loss_abs

    def activation_price(self, entry: Decimal) -> Decimal:
        if self.activation_pct is not None:
            return entry * (1 + self.activation_pct / 100)
        return entry + self.activation_abs

    def trailing_stop_price(self, highest: Decimal) -> Decimal:
        if self.trail_pct is not None:
            return highest * (1 - self.trail_pct / 100)
        return highest - self.trail_abs

    def describe(self) -> str:
        def one(pct, ab):
            return f"{dec_str(pct)}%" if pct is not None else dec_str(ab)
        return (f"stop-loss={one(self.stop_loss_pct, self.stop_loss_abs)} "
                f"ativacao-lucro={one(self.activation_pct, self.activation_abs)} "
                f"trailing={one(self.trail_pct, self.trail_abs)}")


@dataclass
class Position:
    symbol: str
    qty: Decimal
    entry_price: Decimal
    entry_quote: Decimal
    entry_time: datetime
    stop_loss_price: Decimal
    activation_price: Decimal
    highest: Decimal
    trailing_armed: bool = False
    trailing_stop_price: Optional[Decimal] = None
    buy_order_id: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "qty": dec_str(self.qty), "entry_price": dec_str(self.entry_price),
            "entry_quote": dec_str(self.entry_quote), "entry_time": self.entry_time.isoformat(),
            "stop_loss_price": dec_str(self.stop_loss_price), "activation_price": dec_str(self.activation_price),
            "highest": dec_str(self.highest), "trailing_armed": self.trailing_armed,
            "trailing_stop_price": dec_str(self.trailing_stop_price) if self.trailing_stop_price is not None else None,
            "buy_order_id": self.buy_order_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            symbol=d["symbol"], qty=to_dec(d["qty"]), entry_price=to_dec(d["entry_price"]),
            entry_quote=to_dec(d["entry_quote"]), entry_time=datetime.fromisoformat(d["entry_time"]),
            stop_loss_price=to_dec(d["stop_loss_price"]), activation_price=to_dec(d["activation_price"]),
            highest=to_dec(d["highest"]), trailing_armed=bool(d.get("trailing_armed")),
            trailing_stop_price=to_dec(d["trailing_stop_price"]) if d.get("trailing_stop_price") else None,
            buy_order_id=d.get("buy_order_id", ""),
        )


class Phase:
    WAITING_BUY = "WAITING_BUY"
    HOLDING = "HOLDING"
    WATCHING_NEXT = "WATCHING_NEXT"
    FINISHED = "FINISHED"
    ERROR = "ERROR"


class OrderError(Exception):
    pass


# --------------------------------------------------------------------------- exchanges
def parse_order_response(resp: dict, filters: SymbolFilters, side: str) -> Fill:
    executed = to_dec(resp.get("executedQty", "0"))
    cum_quote = to_dec(resp.get("cummulativeQuoteQty", "0"))
    if executed <= ZERO:
        raise OrderError(f"ordem nao executada (status={resp.get('status')}): {resp}")
    fills = resp.get("fills", [])
    comm_base = sum((to_dec(f["commission"]) for f in fills if f.get("commissionAsset") == filters.base_asset), ZERO)
    comm_quote = sum((to_dec(f["commission"]) for f in fills if f.get("commissionAsset") == filters.quote_asset), ZERO)
    comm_other = [(f["commission"], f["commissionAsset"]) for f in fills
                  if f.get("commissionAsset") not in (filters.base_asset, filters.quote_asset)]
    avg_price = cum_quote / executed
    ts = datetime.fromtimestamp(int(resp.get("transactTime", time.time() * 1000)) / 1000, tz=timezone.utc)
    if side == "BUY":
        qty_net = executed - comm_base
        commission, asset = (comm_base, filters.base_asset) if comm_base > ZERO else (comm_quote, filters.quote_asset)
        if comm_other:
            commission, asset = to_dec(comm_other[0][0]), comm_other[0][1]  # ex.: taxa paga em BNB
        return Fill(filters.symbol, "BUY", qty_net, avg_price, cum_quote, commission, asset, str(resp.get("orderId")), ts)
    quote_net = cum_quote - comm_quote
    commission, asset = (comm_quote, filters.quote_asset)
    if comm_other:
        commission, asset = to_dec(comm_other[0][0]), comm_other[0][1]
    return Fill(filters.symbol, "SELL", executed, avg_price, quote_net, commission, asset, str(resp.get("orderId")), ts)


class BinanceExchange:
    """Ordens reais via python-binance (produção ou Spot Testnet)."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        from binance.client import Client  # import tardio: testes nao precisam da biblioteca
        self.testnet = testnet
        self.client = Client(api_key, api_secret, testnet=testnet)

    def filters(self, symbol: str) -> SymbolFilters:
        info = self.client.get_symbol_info(symbol)
        if not info:
            raise ValueError(f"simbolo desconhecido na Binance: {symbol}")
        if info.get("status") != "TRADING":
            raise ValueError(f"{symbol} nao esta em negociacao (status={info.get('status')})")
        return SymbolFilters.from_exchange_info(info)

    def last_price(self, symbol: str) -> Decimal:
        return to_dec(self.client.get_symbol_ticker(symbol=symbol)["price"])

    def free_balance(self, asset: str) -> Decimal:
        bal = self.client.get_asset_balance(asset=asset)
        return to_dec(bal["free"]) if bal else ZERO

    def market_buy_quote(self, filters: SymbolFilters, quote_amount: Decimal) -> Fill:
        resp = self.client.create_order(symbol=filters.symbol, side="BUY", type="MARKET",
                                        quoteOrderQty=dec_str(quote_amount), newOrderRespType="FULL")
        log.debug("resposta BUY: %s", resp)
        return parse_order_response(resp, filters, "BUY")

    def market_sell(self, filters: SymbolFilters, qty: Decimal) -> Fill:
        resp = self.client.create_order(symbol=filters.symbol, side="SELL", type="MARKET",
                                        quantity=dec_str(qty), newOrderRespType="FULL")
        log.debug("resposta SELL: %s", resp)
        return parse_order_response(resp, filters, "SELL")

    def cancel_open_orders(self, symbol: str) -> int:
        n = 0
        for order in self.client.get_open_orders(symbol=symbol):
            self.client.cancel_order(symbol=symbol, orderId=order["orderId"])
            log.warning("ordem pendente cancelada: %s id=%s", symbol, order["orderId"])
            n += 1
        return n


class SimulatedExchange:
    """Nao envia ordens. Preenche ao ultimo preco conhecido, aplicando taxa e slippage simulados."""

    def __init__(self, fee_pct: Decimal = Decimal("0.1"), slippage_pct: Decimal = ZERO,
                 filters_source: Optional[Callable[[str], SymbolFilters]] = None,
                 price_source: Optional[Callable[[str], Decimal]] = None):
        self.fee = fee_pct / 100
        self.slip = slippage_pct / 100
        self._filters_source = filters_source
        self._price_source = price_source
        self.prices: Dict[str, Decimal] = {}
        self._order_seq = 0

    def update_price(self, symbol: str, price: Decimal) -> None:
        self.prices[symbol] = price

    def filters(self, symbol: str) -> SymbolFilters:
        if self._filters_source:
            return self._filters_source(symbol)
        return SymbolFilters.default(symbol)

    def last_price(self, symbol: str) -> Decimal:
        if symbol in self.prices:
            return self.prices[symbol]
        if self._price_source:
            return self._price_source(symbol)
        raise OrderError(f"sem preco conhecido para {symbol}")

    def free_balance(self, asset: str) -> Decimal:
        return Decimal("1e12")

    def _next_id(self) -> str:
        self._order_seq += 1
        return f"SIM-{self._order_seq}"

    def market_buy_quote(self, filters: SymbolFilters, quote_amount: Decimal) -> Fill:
        price = self.last_price(filters.symbol) * (1 + self.slip)
        qty = floor_to_step(quote_amount / price, filters.step_size)
        if qty <= ZERO:
            raise OrderError("quantidade simulada zero (valor em quote muito baixo para o stepSize)")
        commission = qty * self.fee
        return Fill(filters.symbol, "BUY", qty - commission, price, qty * price, commission,
                    filters.base_asset, self._next_id(), utcnow())

    def market_sell(self, filters: SymbolFilters, qty: Decimal) -> Fill:
        price = self.last_price(filters.symbol) * (1 - self.slip)
        gross = qty * price
        commission = gross * self.fee
        return Fill(filters.symbol, "SELL", qty, price, gross - commission, commission,
                    filters.quote_asset, self._next_id(), utcnow())

    def cancel_open_orders(self, symbol: str) -> int:
        return 0


# --------------------------------------------------------------------------- registro de operacoes
class TradeLog:
    FIELDS = ["timestamp_utc", "timestamp_local", "mode", "phase", "symbol", "side", "reason", "qty",
              "avg_price", "quote_total", "commission", "commission_asset", "order_id", "pnl_quote", "pnl_pct"]

    def __init__(self, path: Path, mode: str):
        self.path = path
        self.mode = mode
        new = not path.exists() or path.stat().st_size == 0
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=self.FIELDS)
        if new:
            self._w.writeheader()
            self._fh.flush()

    def record(self, fill: Fill, phase: str, reason: str, pnl_quote: Optional[Decimal] = None,
               pnl_pct: Optional[Decimal] = None) -> None:
        self._w.writerow({
            "timestamp_utc": fill.time.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
            "timestamp_local": fmt_ts(fill.time), "mode": self.mode, "phase": phase,
            "symbol": fill.symbol, "side": fill.side, "reason": reason, "qty": dec_str(fill.qty),
            "avg_price": dec_str(fill.price), "quote_total": dec_str(fill.quote),
            "commission": dec_str(fill.commission), "commission_asset": fill.commission_asset,
            "order_id": fill.order_id,
            "pnl_quote": dec_str(pnl_quote) if pnl_quote is not None else "",
            "pnl_pct": dec_str(pnl_pct.quantize(Decimal("0.0001"))) if pnl_pct is not None else "",
        })
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------- estrategia
@dataclass
class BotConfig:
    mode: str                          # simulate | testnet | live
    first: Target
    next_targets: List[Target]
    quote_amount: Decimal
    rules: ExitRules
    repeat: bool = False
    sell_on_stop: bool = False
    order_retry_seconds: int = 10
    max_buy_failures: int = 3
    state_file: Optional[Path] = None


class TradingBot:
    """Logica de decisao. Recebe precos via handle_price(); executa ordens pela exchange injetada."""

    def __init__(self, cfg: BotConfig, exchange, trade_log: Optional[TradeLog] = None,
                 on_symbols_changed: Optional[Callable[[List[str]], None]] = None):
        self.cfg = cfg
        self.ex = exchange
        self.trades = trade_log
        self.on_symbols_changed = on_symbols_changed or (lambda symbols: None)
        self.phase = Phase.WAITING_BUY
        self.position: Optional[Position] = None
        self.filters: Dict[str, SymbolFilters] = {}
        self.last_prices: Dict[str, Decimal] = {}
        self.realized_pnl = ZERO
        self.cycles_done = 0
        self._buy_failures = 0
        self._next_order_attempt = 0.0
        self.error_message: Optional[str] = None

    # -- preparacao -------------------------------------------------------------
    def all_symbols(self) -> List[str]:
        seen, out = set(), []
        for t in [self.cfg.first, *self.cfg.next_targets]:
            if t.symbol not in seen:
                seen.add(t.symbol)
                out.append(t.symbol)
        return out

    def prepare(self) -> None:
        self.cfg.rules.validate()
        for sym in self.all_symbols():
            f = self.ex.filters(sym)
            self.filters[sym] = f
            if f.min_notional > ZERO and self.cfg.quote_amount < f.min_notional:
                raise ValueError(f"{sym}: valor por operacao {dec_str(self.cfg.quote_amount)} abaixo do minimo "
                                 f"da Binance ({dec_str(f.min_notional)} {f.quote_asset})")
            log.info("filtros %s: step=%s minQty=%s tick=%s minNotional=%s", sym, dec_str(f.step_size),
                     dec_str(f.min_qty), dec_str(f.tick_size), dec_str(f.min_notional))
        quotes = {self.filters[s].quote_asset for s in self.all_symbols()}
        if len(quotes) > 1:
            raise ValueError(f"todos os simbolos devem usar a mesma moeda de cotacao; encontrados: {sorted(quotes)}")
        r = self.cfg.rules
        if (r.trail_pct is not None and r.activation_pct is not None and r.trail_pct >= r.activation_pct) or \
           (r.trail_abs is not None and r.activation_abs is not None and r.trail_abs >= r.activation_abs):
            log.warning("ATENCAO: o recuo do trailing e maior ou igual ao lucro minimo de ativacao; "
                        "a venda pelo trailing pode ocorrer abaixo do preco de compra.")

    def watched_symbols(self) -> List[str]:
        if self.phase == Phase.WAITING_BUY:
            return [self.cfg.first.symbol]
        if self.phase == Phase.HOLDING and self.position:
            return [self.position.symbol]
        if self.phase == Phase.WATCHING_NEXT:
            return list(dict.fromkeys(t.symbol for t in self.cfg.next_targets))
        return []

    def _set_phase(self, phase: str) -> None:
        old = self.phase
        self.phase = phase
        log.info("fase: %s -> %s", old, phase)
        self.save_state()
        self.on_symbols_changed(self.watched_symbols())

    # -- loop de precos -----------------------------------------------------------
    def handle_price(self, symbol: str, price: Decimal, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.last_prices[symbol] = price
        if hasattr(self.ex, "update_price"):
            self.ex.update_price(symbol, price)

        if self.phase == Phase.WAITING_BUY:
            if symbol == self.cfg.first.symbol and self.cfg.first.check(price):
                self._try_buy(self.cfg.first, price, now)
        elif self.phase == Phase.HOLDING:
            if self.position and symbol == self.position.symbol:
                self._manage_position(price, now)
        elif self.phase == Phase.WATCHING_NEXT:
            for target in self.cfg.next_targets:
                if target.symbol == symbol and target.check(price):
                    log.info("%s atingiu o gatilho %s (preco %s); demais candidatas ignoradas",
                             symbol, dec_str(target.trigger), dec_str(price))
                    self._try_buy(target, price, now)
                    break

    # -- compra ---------------------------------------------------------------------
    def _try_buy(self, target: Target, price: Decimal, now: float) -> None:
        if now < self._next_order_attempt:
            return
        f = self.filters[target.symbol]
        log.info("COMPRA %s a mercado: gatilho=%s preco=%s valor=%s %s", target.symbol, dec_str(target.trigger),
                 dec_str(price), dec_str(self.cfg.quote_amount), f.quote_asset)
        try:
            if self.cfg.mode != "simulate":
                free = self.ex.free_balance(f.quote_asset)
                if free < self.cfg.quote_amount:
                    raise OrderError(f"saldo insuficiente em {f.quote_asset}: {dec_str(free)} < {dec_str(self.cfg.quote_amount)}")
            fill = self.ex.market_buy_quote(f, self.cfg.quote_amount)
        except Exception as exc:  # noqa: BLE001 - qualquer falha de ordem deve ser registrada
            self._buy_failures += 1
            self._next_order_attempt = now + self.cfg.order_retry_seconds
            log.error("falha na compra de %s (%d/%d): %s", target.symbol, self._buy_failures,
                      self.cfg.max_buy_failures, exc)
            if self._buy_failures >= self.cfg.max_buy_failures:
                self._fail(f"compra de {target.symbol} falhou {self._buy_failures} vezes; bot interrompido")
            return
        self._buy_failures = 0
        rules = self.cfg.rules
        self.position = Position(
            symbol=fill.symbol, qty=fill.qty, entry_price=fill.price, entry_quote=fill.quote, entry_time=fill.time,
            stop_loss_price=rules.stop_loss_price(fill.price), activation_price=rules.activation_price(fill.price),
            highest=fill.price, buy_order_id=fill.order_id,
        )
        log.info("ORDEM EXECUTADA | COMPRA %s | qtd=%s | preco medio=%s | total=%s %s | taxa=%s %s | id=%s | %s",
                 fill.symbol, dec_str(fill.qty), dec_str(fill.price), dec_str(fill.quote), f.quote_asset,
                 dec_str(fill.commission), fill.commission_asset, fill.order_id, fmt_ts(fill.time))
        log.info("niveis: stop-loss=%s | ativacao do trailing=%s | recuo do trailing=%s",
                 dec_str(self.position.stop_loss_price), dec_str(self.position.activation_price),
                 f"{dec_str(rules.trail_pct)}%" if rules.trail_pct is not None else dec_str(rules.trail_abs))
        if self.trades:
            self.trades.record(fill, self.phase, f"gatilho {dec_str(target.trigger)}")
        self._set_phase(Phase.HOLDING)

    # -- gestao da posicao -----------------------------------------------------------
    def _manage_position(self, price: Decimal, now: float) -> None:
        pos = self.position
        if price <= pos.stop_loss_price:
            self._try_sell(price, "STOP_LOSS", now)
            return
        if not pos.trailing_armed and price >= pos.activation_price:
            pos.trailing_armed = True
            pos.highest = price
            pos.trailing_stop_price = self.cfg.rules.trailing_stop_price(price)
            log.info("TRAILING ARMADO %s: preco=%s (lucro minimo atingido). Venda se recuar ate %s",
                     pos.symbol, dec_str(price), dec_str(pos.trailing_stop_price))
            self.save_state()
        if pos.trailing_armed:
            if price > pos.highest:
                pos.highest = price
                new_stop = self.cfg.rules.trailing_stop_price(price)
                if pos.trailing_stop_price is None or new_stop > pos.trailing_stop_price:
                    pos.trailing_stop_price = new_stop
                    log.info("nova maxima %s: %s -> trailing stop em %s", pos.symbol, dec_str(price), dec_str(new_stop))
                    self.save_state()
            elif pos.trailing_stop_price is not None and price <= pos.trailing_stop_price:
                self._try_sell(price, "TRAILING_STOP", now)

    def _sell_quantity(self, pos: Position, price: Decimal) -> Decimal:
        f = self.filters[pos.symbol]
        qty = pos.qty
        if self.cfg.mode != "simulate":
            free = self.ex.free_balance(f.base_asset)
            if free < qty:
                log.warning("saldo livre de %s (%s) menor que a posicao registrada (%s); vendendo o saldo livre",
                            f.base_asset, dec_str(free), dec_str(qty))
                qty = free
        qty = floor_to_step(qty, f.step_size)
        if qty < f.min_qty or qty <= ZERO:
            raise OrderError(f"quantidade {dec_str(qty)} abaixo do minimo {dec_str(f.min_qty)}")
        if f.min_notional > ZERO and qty * price < f.min_notional:
            raise OrderError(f"valor da venda {dec_str(qty * price)} abaixo do minimo {dec_str(f.min_notional)}")
        return qty

    def _try_sell(self, price: Decimal, reason: str, now: float) -> None:
        if now < self._next_order_attempt:
            return
        pos = self.position
        f = self.filters[pos.symbol]
        log.info("VENDA %s a mercado por %s: preco=%s stop-loss=%s trailing=%s maxima=%s", pos.symbol, reason,
                 dec_str(price), dec_str(pos.stop_loss_price),
                 dec_str(pos.trailing_stop_price) if pos.trailing_stop_price is not None else "-", dec_str(pos.highest))
        try:
            qty = self._sell_quantity(pos, price)
            fill = self.ex.market_sell(f, qty)
        except Exception as exc:  # noqa: BLE001
            self._next_order_attempt = now + self.cfg.order_retry_seconds
            log.error("FALHA NA VENDA de %s (%s). Nova tentativa em %ss. POSICAO CONTINUA ABERTA. Erro: %s",
                      pos.symbol, reason, self.cfg.order_retry_seconds, exc)
            return
        pnl = fill.quote - pos.entry_quote
        pnl_pct = pnl / pos.entry_quote * 100 if pos.entry_quote else ZERO
        self.realized_pnl += pnl
        self.cycles_done += 1
        log.info("ORDEM EXECUTADA | VENDA %s (%s) | qtd=%s | preco medio=%s | total=%s %s | taxa=%s %s | id=%s | %s",
                 fill.symbol, reason, dec_str(fill.qty), dec_str(fill.price), dec_str(fill.quote), f.quote_asset,
                 dec_str(fill.commission), fill.commission_asset, fill.order_id, fmt_ts(fill.time))
        log.info("RESULTADO %s: %s %s (%s%%) | acumulado: %s %s", fill.symbol, dec_str(pnl), f.quote_asset,
                 dec_str(pnl_pct.quantize(Decimal("0.01"))), dec_str(self.realized_pnl), f.quote_asset)
        if self.trades:
            self.trades.record(fill, self.phase, reason, pnl, pnl_pct)
        self.position = None
        self._after_sell()

    def _after_sell(self) -> None:
        if not self.cfg.next_targets:
            self._set_phase(Phase.FINISHED)
            return
        if self.cycles_done >= 2 and not self.cfg.repeat:
            log.info("ciclo completo (moeda inicial + moeda seguinte). Use --repeat para continuar observando a lista.")
            self._set_phase(Phase.FINISHED)
            return
        for t in self.cfg.next_targets:
            t.resolved = None
        log.info("observando moedas seguintes: %s",
                 ", ".join(f"{t.symbol}@{dec_str(t.trigger)}" for t in self.cfg.next_targets))
        self._set_phase(Phase.WATCHING_NEXT)

    def _fail(self, message: str) -> None:
        self.error_message = message
        log.error(message)
        self._set_phase(Phase.ERROR)

    # -- atualizacoes em execucao (alimentar) ---------------------------------------------
    def update_next_targets(self, targets: List[Target], append: bool = False) -> None:
        """Substitui ou acrescenta moedas seguintes. Valida filtros e moeda de cotacao antes de aplicar."""
        quote = self.filters[self.cfg.first.symbol].quote_asset
        for t in targets:
            if t.symbol not in self.filters:
                f = self.ex.filters(t.symbol)
                if f.quote_asset != quote:
                    raise ValueError(f"{t.symbol} usa {f.quote_asset}; a rodada opera em {quote}")
                if f.min_notional > ZERO and self.cfg.quote_amount < f.min_notional:
                    raise ValueError(f"{t.symbol}: valor por operacao abaixo do minimo {dec_str(f.min_notional)}")
                self.filters[t.symbol] = f
        if append:
            existing = {t.symbol for t in self.cfg.next_targets}
            self.cfg.next_targets.extend(t for t in targets if t.symbol not in existing)
        else:
            self.cfg.next_targets = list(targets)
        log.info("moedas seguintes atualizadas: %s",
                 ", ".join(f"{t.symbol}@{dec_str(t.trigger)}" for t in self.cfg.next_targets) or "(nenhuma)")
        self.save_state()
        if self.phase == Phase.WATCHING_NEXT:
            self.on_symbols_changed(self.watched_symbols())

    def update_rules(self, rules: ExitRules) -> None:
        """Troca as regras de saida; recalcula os niveis da posicao aberta a partir da entrada e da maxima."""
        rules.validate()
        self.cfg.rules = rules
        pos = self.position
        if pos:
            pos.stop_loss_price = rules.stop_loss_price(pos.entry_price)
            pos.activation_price = rules.activation_price(pos.entry_price)
            if pos.trailing_armed:
                pos.trailing_stop_price = rules.trailing_stop_price(pos.highest)
            log.info("regras atualizadas (%s): stop-loss=%s ativacao=%s trailing=%s", rules.describe(),
                     dec_str(pos.stop_loss_price), dec_str(pos.activation_price),
                     dec_str(pos.trailing_stop_price) if pos.trailing_stop_price is not None else "nao armado")
        else:
            log.info("regras atualizadas para as proximas compras: %s", rules.describe())
        self.save_state()

    def update_quote_amount(self, amount: Decimal) -> None:
        if amount <= ZERO:
            raise ValueError("valor por compra deve ser positivo")
        for sym, f in self.filters.items():
            if f.min_notional > ZERO and amount < f.min_notional:
                raise ValueError(f"{sym}: valor {dec_str(amount)} abaixo do minimo {dec_str(f.min_notional)}")
        self.cfg.quote_amount = amount
        log.info("valor por compra atualizado: %s", dec_str(amount))
        self.save_state()

    def snapshot(self) -> dict:
        """Estado atual em formato serializavel (usado pela interface web)."""
        return {
            "phase": self.phase, "mode": self.cfg.mode, "quote_amount": dec_str(self.cfg.quote_amount),
            "rules": self.cfg.rules.describe(), "repeat": self.cfg.repeat,
            "first": {"symbol": self.cfg.first.symbol, "trigger": dec_str(self.cfg.first.trigger)},
            "next": [{"symbol": t.symbol, "trigger": dec_str(t.trigger)} for t in self.cfg.next_targets],
            "watched": self.watched_symbols(),
            "prices": {k: dec_str(v) for k, v in self.last_prices.items()},
            "position": self.position.to_dict() if self.position else None,
            "realized_pnl": dec_str(self.realized_pnl), "cycles_done": self.cycles_done,
            "error": self.error_message, "status_line": self.status_line(),
        }

    # -- parada segura --------------------------------------------------------------------
    def shutdown(self, sell_position: bool) -> None:
        if self.cfg.mode != "simulate":
            for sym in self.all_symbols():
                try:
                    n = self.ex.cancel_open_orders(sym)
                    if n:
                        log.warning("%s: %d ordem(ns) pendente(s) cancelada(s)", sym, n)
                except Exception as exc:  # noqa: BLE001
                    log.error("falha ao verificar/cancelar ordens pendentes de %s: %s", sym, exc)
        if self.position and sell_position:
            price = self.last_prices.get(self.position.symbol)
            if price is None:
                try:
                    price = self.ex.last_price(self.position.symbol)
                except Exception as exc:  # noqa: BLE001
                    log.error("sem preco para vender %s na parada: %s", self.position.symbol, exc)
            if price is not None:
                self._next_order_attempt = 0.0
                self._try_sell(price, "STOP_MANUAL", time.time())
        if self.position:
            p = self.position
            log.warning("POSICAO ABERTA MANTIDA: %s qtd=%s entrada=%s stop-loss=%s trailing=%s. "
                        "Sem o bot rodando, NAO ha protecao de stop. Reinicie com --resume ou feche manualmente.",
                        p.symbol, dec_str(p.qty), dec_str(p.entry_price), dec_str(p.stop_loss_price),
                        dec_str(p.trailing_stop_price) if p.trailing_stop_price is not None else "nao armado")
        self.save_state()

    # -- estado em disco -------------------------------------------------------------------
    def save_state(self) -> None:
        if not self.cfg.state_file:
            return
        state = {
            "saved_at": utcnow().isoformat(), "mode": self.cfg.mode, "phase": self.phase,
            "position": self.position.to_dict() if self.position else None,
            "realized_pnl": dec_str(self.realized_pnl), "cycles_done": self.cycles_done,
            "first": {"symbol": self.cfg.first.symbol, "trigger": dec_str(self.cfg.first.trigger)},
            "next": [{"symbol": t.symbol, "trigger": dec_str(t.trigger)} for t in self.cfg.next_targets],
        }
        tmp = self.cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.cfg.state_file)

    def resume(self) -> bool:
        if not self.cfg.state_file or not self.cfg.state_file.is_file():
            log.warning("--resume informado, mas nao ha arquivo de estado em %s", self.cfg.state_file)
            return False
        state = json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        if state.get("mode") != self.cfg.mode:
            raise ValueError(f"estado salvo e do modo '{state.get('mode')}', diferente de '{self.cfg.mode}'")
        phase = state.get("phase")
        self.realized_pnl = to_dec(state.get("realized_pnl", "0"))
        self.cycles_done = int(state.get("cycles_done", 0))
        if phase == Phase.HOLDING and state.get("position"):
            self.position = Position.from_dict(state["position"])
            if self.position.symbol not in self.filters:
                self.filters[self.position.symbol] = self.ex.filters(self.position.symbol)
            self.phase = Phase.HOLDING
            log.info("estado retomado: posicao em %s qtd=%s entrada=%s (salvo em %s)", self.position.symbol,
                     dec_str(self.position.qty), dec_str(self.position.entry_price), state.get("saved_at"))
        elif phase == Phase.WATCHING_NEXT:
            self.phase = Phase.WATCHING_NEXT
            log.info("estado retomado: observando moedas seguintes (salvo em %s)", state.get("saved_at"))
        else:
            log.info("estado salvo na fase %s; iniciando do comeco", phase)
            return False
        self.on_symbols_changed(self.watched_symbols())
        return True

    def status_line(self) -> str:
        if self.phase == Phase.HOLDING and self.position:
            p = self.position
            price = self.last_prices.get(p.symbol)
            pnl = f"{dec_str(((price / p.entry_price - 1) * 100).quantize(Decimal('0.01')))}%" if price else "-"
            return (f"[{self.phase}] {p.symbol} preco={dec_str(price) if price else '-'} entrada={dec_str(p.entry_price)} "
                    f"({pnl}) stop={dec_str(p.stop_loss_price)} "
                    f"trailing={'armado em ' + dec_str(p.trailing_stop_price) if p.trailing_armed else 'ativa em ' + dec_str(p.activation_price)} "
                    f"maxima={dec_str(p.highest)}")
        targets = [self.cfg.first] if self.phase == Phase.WAITING_BUY else self.cfg.next_targets
        parts = [f"{t.symbol}={dec_str(self.last_prices[t.symbol]) if t.symbol in self.last_prices else '-'}"
                 f"(gatilho {dec_str(t.trigger)})" for t in targets]
        return f"[{self.phase}] " + " ".join(parts)


# --------------------------------------------------------------------------- feed de precos (WebSocket)
class PriceFeed:
    """Stream <symbol>@trade via python-binance ThreadedWebsocketManager -> fila thread-safe.

    A cada mudanca de simbolos (ou reconexao) o gerenciador e recriado por completo: a python-binance
    identifica sockets pelo caminho dos streams e reabrir o mesmo caminho logo apos stop_socket()
    pode deixar dois ouvintes ativos. Mudancas de fase sao raras, entao recriar e simples e seguro.
    A propria biblioteca ja reconecta quedas curtas; o watchdog do loop principal cobre o restante.
    """

    def __init__(self, out_queue: "queue.Queue", testnet: bool = False):
        from binance import ThreadedWebsocketManager
        self._twm_cls = ThreadedWebsocketManager
        self._testnet = testnet
        self._q = out_queue
        self._twm = None
        self._symbols: List[str] = []
        self._lock = threading.Lock()
        self._enabled = False

    def start(self) -> None:
        self._enabled = True

    def _on_message(self, msg: dict) -> None:
        try:
            data = (msg.get("data") or {}) if "stream" in msg else msg
            if data.get("e") == "error":
                self._q.put(("error", str(data.get("m") or data)))
                return
            if data.get("e") == "trade" and "p" in data and "s" in data:
                self._q.put(("price", data["s"], to_dec(data["p"]), time.time()))
        except Exception as exc:  # noqa: BLE001
            self._q.put(("error", f"mensagem invalida do websocket: {exc}"))

    def _close(self) -> None:
        if self._twm is not None:
            try:
                self._twm.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("erro ao parar websocket: %s", exc)
            self._twm = None

    def set_symbols(self, symbols: List[str]) -> None:
        with self._lock:
            self._symbols = list(symbols)
            if not self._enabled:
                return
            self._close()
            if not symbols:
                return
            twm = self._twm_cls(testnet=self._testnet)
            twm.daemon = True
            twm.start()
            streams = [f"{s.lower()}@trade" for s in symbols]
            twm.start_multiplex_socket(callback=self._on_message, streams=streams)
            self._twm = twm
            log.info("websocket assinado: %s", ", ".join(streams))

    def restart(self) -> None:
        log.warning("reconectando websocket...")
        self.set_symbols(self._symbols)

    def stop(self) -> None:
        with self._lock:
            self._enabled = False
            self._close()


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bot spot Binance: compra por gatilho, stop-loss fixo + trailing stop, recompra em lista de moedas.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--mode", choices=["simulate", "testnet", "live"], default="simulate",
                   help="simulate: sem ordens reais; testnet: Spot Testnet da Binance; live: dinheiro real")
    p.add_argument("--symbol", required=True, help="moeda inicial, ex.: BTCUSDT")
    p.add_argument("--buy-trigger", required=True, help="preco de gatilho da compra inicial")
    p.add_argument("--buy-direction", choices=["auto", "below", "above"], default="auto",
                   help="auto: compra quando o preco ATINGE o gatilho vindo de onde estiver; "
                        "below: compra quando preco <= gatilho; above: quando preco >= gatilho")
    p.add_argument("--quote-amount", required=True, help="valor por compra na moeda de cotacao (ex.: 50 = 50 USDT)")
    p.add_argument("--next", action="append", default=[], metavar="SIMBOLO:PRECO",
                   help="moeda seguinte com gatilho de recompra; repita a opcao para varias (ex.: --next ETHUSDT:3000)")
    p.add_argument("--repeat", action="store_true",
                   help="apos vender a moeda seguinte, volta a observar a lista indefinidamente")
    g = p.add_argument_group("regras de saida (informe pct OU abs em cada grupo)")
    g.add_argument("--stop-loss-pct", help="stop-loss em %% abaixo do preco de compra (ex.: 2)")
    g.add_argument("--stop-loss-abs", help="stop-loss em valor absoluto abaixo do preco de compra")
    g.add_argument("--profit-activation-pct", help="lucro minimo em %% que arma o trailing stop (ex.: 3)")
    g.add_argument("--profit-activation-abs", help="lucro minimo em valor absoluto que arma o trailing")
    g.add_argument("--trail-pct", help="recuo em %% a partir da maxima que dispara a venda (ex.: 1)")
    g.add_argument("--trail-abs", help="recuo em valor absoluto a partir da maxima que dispara a venda")
    o = p.add_argument_group("operacao")
    o.add_argument("--sell-on-stop", action="store_true",
                   help="ao parar (Ctrl+C), vende a mercado a posicao aberta antes de encerrar")
    o.add_argument("--resume", action="store_true", help="retoma a posicao/fase salva no arquivo de estado")
    o.add_argument("--yes", action="store_true", help="nao pedir confirmacao no modo live")
    o.add_argument("--watchdog-seconds", type=int, default=90,
                   help="sem mensagens do websocket por este tempo -> consulta REST e reconecta")
    o.add_argument("--status-seconds", type=int, default=60, help="intervalo da linha de status no log")
    o.add_argument("--order-retry-seconds", type=int, default=10, help="espera entre tentativas apos falha de ordem")
    o.add_argument("--data-dir", default="data", help="pasta para log, trades.csv e estado")
    o.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    s = p.add_argument_group("simulacao")
    s.add_argument("--sim-fee-pct", default="0.1", help="taxa simulada por ordem em %%")
    s.add_argument("--sim-slippage-pct", default="0", help="slippage simulado em %%")
    s.add_argument("--replay", metavar="ARQUIVO",
                   help="(apenas simulate) le precos de um CSV 'SIMBOLO,PRECO' em vez do websocket; sem rede")
    return p


def setup_logging(data_dir: Path, level: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))
    for h in list(root.handlers):
        root.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(data_dir / "bot.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("binance").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def opt_dec(v: Optional[str]) -> Optional[Decimal]:
    return to_dec(v) if v is not None else None


def config_from_args(args) -> BotConfig:
    rules = ExitRules(
        stop_loss_pct=opt_dec(args.stop_loss_pct), stop_loss_abs=opt_dec(args.stop_loss_abs),
        activation_pct=opt_dec(args.profit_activation_pct), activation_abs=opt_dec(args.profit_activation_abs),
        trail_pct=opt_dec(args.trail_pct), trail_abs=opt_dec(args.trail_abs),
    )
    rules.validate()
    quote_amount = to_dec(args.quote_amount)
    if quote_amount <= ZERO:
        raise ValueError("--quote-amount deve ser positivo")
    first = Target.parse(f"{args.symbol}:{args.buy_trigger}", args.buy_direction)
    nexts = [Target.parse(n, args.buy_direction) for n in args.next]
    data_dir = Path(args.data_dir)
    return BotConfig(mode=args.mode, first=first, next_targets=nexts, quote_amount=quote_amount, rules=rules,
                     repeat=args.repeat, sell_on_stop=args.sell_on_stop, order_retry_seconds=args.order_retry_seconds,
                     state_file=data_dir / f"state_{args.mode}.json")


def make_exchange(cfg: BotConfig, args):
    if cfg.mode == "simulate":
        public = None
        if not args.replay:
            from binance.client import Client
            public = Client()  # endpoints publicos, sem chave
        return SimulatedExchange(
            fee_pct=to_dec(args.sim_fee_pct), slippage_pct=to_dec(args.sim_slippage_pct),
            filters_source=(lambda s: SymbolFilters.from_exchange_info(public.get_symbol_info(s) or _unknown(s))) if public else None,
            price_source=(lambda s: to_dec(public.get_symbol_ticker(symbol=s)["price"])) if public else None,
        )
    testnet = cfg.mode == "testnet"
    if testnet:
        key = os.environ.get("BINANCE_TESTNET_API_KEY") or os.environ.get("BINANCE_API_KEY")
        secret = os.environ.get("BINANCE_TESTNET_API_SECRET") or os.environ.get("BINANCE_API_SECRET")
    else:
        key, secret = os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET")
    if not key or not secret:
        raise SystemExit("Defina BINANCE_API_KEY e BINANCE_API_SECRET no ambiente (ou no arquivo .env). "
                         "Nunca coloque as chaves no codigo.")
    return BinanceExchange(key, secret, testnet=testnet)


def _unknown(symbol: str):
    raise ValueError(f"simbolo desconhecido na Binance: {symbol}")


def confirm_live(cfg: BotConfig) -> None:
    print("\n" + "=" * 78)
    print("MODO LIVE: ORDENS REAIS SERAO ENVIADAS COM DINHEIRO REAL.")
    print(f"  compra inicial : {cfg.first.symbol} gatilho {dec_str(cfg.first.trigger)}")
    print(f"  valor por ordem: {dec_str(cfg.quote_amount)}")
    print(f"  saida          : {cfg.rules.describe()}")
    print(f"  seguintes      : {', '.join(f'{t.symbol}@{dec_str(t.trigger)}' for t in cfg.next_targets) or '(nenhuma)'}")
    print("=" * 78)
    answer = input("Digite CONFIRMO para continuar: ").strip()
    if answer != "CONFIRMO":
        raise SystemExit("operacao cancelada pelo usuario")


def run_replay(bot: TradingBot, path: Path) -> None:
    """Executa a logica contra um CSV de precos (SIMBOLO,PRECO) - util para validar a estrategia sem rede."""
    log.info("REPLAY de %s (sem rede, sem websocket)", path)
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.reader(fh), 1):
            if not row or row[0].strip().startswith("#"):
                continue
            if len(row) < 2:
                log.warning("linha %d ignorada: %s", i, row)
                continue
            bot.handle_price(row[0].strip().upper(), to_dec(row[1].strip()), now=float(i) * 1000)
            if bot.phase in (Phase.FINISHED, Phase.ERROR):
                break
    log.info("replay encerrado na fase %s | PnL realizado: %s | ciclos: %d", bot.phase,
             dec_str(bot.realized_pnl), bot.cycles_done)


def run_live_loop(bot: TradingBot, feed: PriceFeed, q: "queue.Queue", args, stop_event: threading.Event) -> None:
    last_msg = time.time()
    last_status = 0.0
    feed.start()
    feed.set_symbols(bot.watched_symbols())
    while not stop_event.is_set() and bot.phase not in (Phase.FINISHED, Phase.ERROR):
        try:
            item = q.get(timeout=1.0)
        except queue.Empty:
            item = None
        now = time.time()
        if item:
            if item[0] == "price":
                last_msg = now
                _, symbol, price, _ts = item
                if symbol in bot.watched_symbols():
                    bot.handle_price(symbol, price, now)
            elif item[0] == "error":
                log.error("websocket: %s", item[1])
            elif item[0] == "command":
                try:
                    item[1](bot)
                except Exception as exc:  # noqa: BLE001
                    log.error("comando rejeitado: %s", exc)
        if now - last_msg > args.watchdog_seconds:
            log.warning("sem precos ha %ds; consultando REST e reconectando", int(now - last_msg))
            for sym in bot.watched_symbols():
                try:
                    bot.handle_price(sym, bot.ex.last_price(sym), now)
                except Exception as exc:  # noqa: BLE001
                    log.error("REST %s falhou: %s", sym, exc)
            feed.restart()
            last_msg = now
        if now - last_status >= args.status_seconds:
            log.info(bot.status_line())
            last_status = now


def main(argv: Optional[List[str]] = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    data_dir = Path(args.data_dir)
    setup_logging(data_dir, args.log_level)
    if args.replay and args.mode != "simulate":
        log.error("--replay so funciona com --mode simulate")
        return 2
    try:
        cfg = config_from_args(args)
    except ValueError as exc:
        log.error("parametros invalidos: %s", exc)
        return 2

    log.info("=== binance_bot | modo=%s | %s gatilho=%s | valor=%s | %s | seguintes=%s | repeat=%s ===",
             cfg.mode.upper(), cfg.first.symbol, dec_str(cfg.first.trigger), dec_str(cfg.quote_amount),
             cfg.rules.describe(), ", ".join(f"{t.symbol}@{dec_str(t.trigger)}" for t in cfg.next_targets) or "-",
             cfg.repeat)
    if cfg.mode == "live" and not args.yes:
        confirm_live(cfg)

    try:
        exchange = make_exchange(cfg, args)
    except SystemExit as exc:
        log.error("%s", exc)
        return 2
    trade_log = TradeLog(data_dir / f"trades_{cfg.mode}.csv", cfg.mode)

    q: "queue.Queue" = queue.Queue()
    feed: Optional[PriceFeed] = None
    if not args.replay:
        feed = PriceFeed(q, testnet=(cfg.mode == "testnet"))
    bot = TradingBot(cfg, exchange, trade_log, on_symbols_changed=(feed.set_symbols if feed else None))

    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        log.warning("sinal %s recebido; encerrando com seguranca...", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    exit_code = 0
    try:
        bot.prepare()
        if args.resume:
            bot.resume()
        if args.replay:
            run_replay(bot, Path(args.replay))
        else:
            run_live_loop(bot, feed, q, args, stop_event)
    except KeyboardInterrupt:
        stop_event.set()
    except Exception as exc:  # noqa: BLE001
        log.exception("erro fatal: %s", exc)
        exit_code = 1
    finally:
        if feed:
            feed.stop()
        bot.shutdown(sell_position=cfg.sell_on_stop and stop_event.is_set())
        trade_log.close()
        log.info("encerrado | fase=%s | PnL realizado=%s | ciclos=%d | log=%s | operacoes=%s", bot.phase,
                 dec_str(bot.realized_pnl), bot.cycles_done, data_dir / "bot.log", data_dir / f"trades_{cfg.mode}.csv")
        if bot.phase == Phase.ERROR:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
