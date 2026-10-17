#!/usr/bin/env python3
"""
binance_bot.py - Automacao de compra e venda SPOT na Binance, com varias moedas em paralelo.

Cada moeda tem o seu proprio plano:
  - gatilho de compra (preco) e valor da compra em REAIS (convertido pela cotacao da moeda de
    cotacao do par, ex.: USDTBRL, quando o par nao e em BRL);
  - stop-loss: em R$ (perda maxima na operacao), em % ou em preco;
  - minimo para venda na alta: preco alvo, lucro minimo em R$ ou %;
  - trailing: recuo a partir da maxima, em % ou R$, que dispara a venda; nunca vende abaixo do
    minimo para venda (o minimo e o piso do trailing).
Todas as moedas sao observadas ao mesmo tempo; pode haver varias posicoes abertas em paralelo.
Depois de vender, a moeda volta a esperar o gatilho de compra, ate o bot ser desligado.

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
ONE = Decimal("1")
DEFAULT_STEP = Decimal("0.00000001")
LEVEL_PRECISION = Decimal("1e-12")   # arredondamento dos niveis calculados (stop, minimo, trailing)
PERIODS = {"1h": 3600, "4h": 4 * 3600, "1d": 86400, "15d": 15 * 86400}   # variacao da carteira por periodo
EQUITY_SAMPLE_SECONDS = 60
BRL = "BRL"
KNOWN_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "BNB", "BRL", "EUR", "TRY")


# --------------------------------------------------------------------------- utilidades
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def to_dec(value) -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"valor numerico invalido: {value!r}") from exc


def dec_str(value: Optional[Decimal]) -> str:
    """Representacao decimal simples (sem notacao cientifica), sem zeros a direita."""
    if value is None:
        return "-"
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def brl_str(value: Optional[Decimal]) -> str:
    if value is None:
        return "-"
    return "R$ " + dec_str(value.quantize(Decimal("0.01")))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Arredonda para baixo ao multiplo de `step` (LOT_SIZE.stepSize / PRICE_FILTER.tickSize)."""
    if step <= ZERO:
        return value
    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return (steps * step).quantize(step)


def normalize_symbol(text: str) -> str:
    """'s/usdt' -> 'SUSDT'; aceita barra, hifen e espacos."""
    return "".join(ch for ch in str(text).upper() if ch.isalnum())


def split_symbol(symbol: str):
    for q in KNOWN_QUOTES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    return symbol[:-4], symbol[-4:]


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


# --------------------------------------------------------------------------- modelos de mercado
@dataclass
class SymbolFilters:
    symbol: str
    base_asset: str
    quote_asset: str
    step_size: Decimal = DEFAULT_STEP
    min_qty: Decimal = ZERO
    tick_size: Decimal = DEFAULT_STEP
    min_notional: Decimal = ZERO
    quote_precision: int = 8

    @classmethod
    def from_exchange_info(cls, info: dict) -> "SymbolFilters":
        f = cls(symbol=info["symbol"], base_asset=info["baseAsset"], quote_asset=info["quoteAsset"],
                quote_precision=int(info.get("quoteAssetPrecision", 8)))
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


class OrderError(Exception):
    pass


# --------------------------------------------------------------------------- plano por moeda
@dataclass
class Rule:
    """Uma regra com unidade e valor. Unidades:
    pct   -> percentual (do preco de compra; no trailing, da maxima)
    money -> perda maxima / lucro minimo em REAIS sobre a operacao inteira
    total -> valor TOTAL da posicao em REAIS (ex.: comprou R$50, stop quando valer R$48,50)
    price -> preco unitario da moeda (apenas no minimo de venda, ex.: vender a partir de 0,071)
    """
    unit: str
    value: Decimal

    @classmethod
    def parse(cls, text: str) -> "Rule":
        if ":" not in str(text):
            raise ValueError(f"regra invalida '{text}'. Use unidade:valor, ex.: money:1 | pct:0.8 | price:0.071")
        unit, _, val = str(text).partition(":")
        return cls(unit.strip().lower(), to_dec(val))

    def to_dict(self) -> dict:
        return {"unit": self.unit, "value": dec_str(self.value)}

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        return cls(str(d.get("unit", "")).lower(), to_dec(d.get("value")))


@dataclass
class ExitRules:
    stop: Rule          # pct | money | price
    activation: Rule    # pct | money | price   (minimo para venda na alta)
    trail: Rule         # pct | money           (recuo a partir da maxima)

    def validate(self) -> None:
        allowed = {"stop": ("pct", "money", "total"), "activation": ("pct", "money", "price", "total"), "trail": ("pct", "money")}
        names = {"stop": "stop-loss", "activation": "minimo para venda na alta", "trail": "recuo do trailing"}
        for key, units in allowed.items():
            r = getattr(self, key)
            if r.unit not in units:
                raise ValueError(f"{names[key]}: unidade '{r.unit}' invalida (use {', '.join(units)})")
            if r.value <= ZERO:
                raise ValueError(f"{names[key]} deve ser positivo")
            if r.unit == "pct" and key != "activation" and r.value >= 100:
                raise ValueError(f"{names[key]} percentual deve ser menor que 100")

    def describe(self) -> str:
        def one(r: Rule) -> str:
            return {"pct": f"{dec_str(r.value)}%", "money": brl_str(r.value), "price": f"preco {dec_str(r.value)}",
                    "total": f"total {brl_str(r.value)}"}[r.unit]
        return f"stop={one(self.stop)} minimo-venda={one(self.activation)} trailing={one(self.trail)}"

    def to_dict(self) -> dict:
        return {"stop": self.stop.to_dict(), "activation": self.activation.to_dict(), "trail": self.trail.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> "ExitRules":
        return cls(Rule.from_dict(d["stop"]), Rule.from_dict(d["activation"]), Rule.from_dict(d["trail"]))


@dataclass
class CoinPlan:
    symbol: str
    trigger: Decimal
    amount_brl: Decimal
    rules: ExitRules
    direction: str = "auto"     # auto | below | above

    def validate(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        if not self.symbol:
            raise ValueError("informe o simbolo da moeda (ex.: BTCBRL)")
        if self.trigger <= ZERO:
            raise ValueError(f"{self.symbol}: gatilho de compra deve ser positivo")
        if self.amount_brl <= ZERO:
            raise ValueError(f"{self.symbol}: valor por compra (R$) deve ser positivo")
        if self.direction not in ("auto", "below", "above"):
            raise ValueError(f"{self.symbol}: direcao deve ser auto, below ou above")
        self.rules.validate()
        if self.rules.stop.unit == "total" and self.rules.stop.value >= self.amount_brl:
            raise ValueError(f"{self.symbol}: stop-loss em valor total ({brl_str(self.rules.stop.value)}) precisa ficar ABAIXO do "
                             f"valor da compra ({brl_str(self.amount_brl)}); para perda maxima use 'R$ na operacao'")
        if self.rules.stop.unit == "money" and self.rules.stop.value >= self.amount_brl:
            raise ValueError(f"{self.symbol}: perda maxima ({brl_str(self.rules.stop.value)}) nao pode ser maior ou igual ao "
                             f"valor da compra ({brl_str(self.amount_brl)})")
        if self.rules.activation.unit == "price" and self.rules.activation.value <= self.trigger:
            raise ValueError(f"{self.symbol}: minimo para venda em preco ({dec_str(self.rules.activation.value)}) precisa ficar "
                             f"ACIMA do gatilho de compra ({dec_str(self.trigger)})")
        if self.rules.activation.unit == "total" and self.rules.activation.value <= self.amount_brl:
            raise ValueError(f"{self.symbol}: minimo para venda em valor total ({brl_str(self.rules.activation.value)}) precisa "
                             f"ficar ACIMA do valor da compra ({brl_str(self.amount_brl)})")

    def describe(self) -> str:
        return (f"{self.symbol} gatilho={dec_str(self.trigger)} compra={brl_str(self.amount_brl)} "
                f"{self.rules.describe()}")

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "trigger": dec_str(self.trigger), "amount_brl": dec_str(self.amount_brl),
                "direction": self.direction, "rules": self.rules.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> "CoinPlan":
        p = cls(symbol=normalize_symbol(d.get("symbol", "")), trigger=to_dec(d.get("trigger")),
                amount_brl=to_dec(d.get("amount_brl")), rules=ExitRules.from_dict(d["rules"]),
                direction=str(d.get("direction") or "auto").lower())
        p.validate()
        return p

    @classmethod
    def parse(cls, text: str) -> "CoinPlan":
        """Formato da linha de comando:
        "SUSDT trigger=0.067 amount=50 stop=money:1 activation=price:0.071 trail=pct:0.8 [direction=auto]"
        """
        parts = str(text).split()
        if len(parts) < 2:
            raise ValueError(f"plano invalido '{text}'")
        kv = {}
        for p in parts[1:]:
            if "=" not in p:
                raise ValueError(f"plano '{parts[0]}': parametro invalido '{p}' (use chave=valor)")
            k, _, v = p.partition("=")
            kv[k.strip().lower()] = v.strip()
        missing = [k for k in ("trigger", "amount", "stop", "activation", "trail") if k not in kv]
        if missing:
            raise ValueError(f"plano '{parts[0]}': faltam {', '.join(missing)}")
        plan = cls(symbol=normalize_symbol(parts[0]), trigger=to_dec(kv["trigger"]), amount_brl=to_dec(kv["amount"]),
                   rules=ExitRules(Rule.parse(kv["stop"]), Rule.parse(kv["activation"]), Rule.parse(kv["trail"])),
                   direction=kv.get("direction", "auto").lower())
        plan.validate()
        return plan


@dataclass
class Position:
    symbol: str
    qty: Decimal
    entry_price: Decimal
    entry_quote: Decimal          # gasto na moeda de cotacao
    entry_brl: Decimal            # gasto em reais
    rate: Decimal                 # cotacao (quote -> BRL) usada na compra; 1 para pares em BRL
    entry_time: datetime
    stop_loss_price: Decimal
    activation_price: Decimal     # minimo para venda na alta (piso do trailing)
    highest: Decimal
    trailing_armed: bool = False
    trailing_stop_price: Optional[Decimal] = None
    buy_order_id: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "qty": dec_str(self.qty), "entry_price": dec_str(self.entry_price),
            "entry_quote": dec_str(self.entry_quote), "entry_brl": dec_str(self.entry_brl), "rate": dec_str(self.rate),
            "entry_time": self.entry_time.isoformat(), "stop_loss_price": dec_str(self.stop_loss_price),
            "activation_price": dec_str(self.activation_price), "highest": dec_str(self.highest),
            "trailing_armed": self.trailing_armed,
            "trailing_stop_price": dec_str(self.trailing_stop_price) if self.trailing_stop_price is not None else None,
            "buy_order_id": self.buy_order_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            symbol=d["symbol"], qty=to_dec(d["qty"]), entry_price=to_dec(d["entry_price"]),
            entry_quote=to_dec(d["entry_quote"]), entry_brl=to_dec(d.get("entry_brl", d["entry_quote"])),
            rate=to_dec(d.get("rate", "1")), entry_time=datetime.fromisoformat(d["entry_time"]),
            stop_loss_price=to_dec(d["stop_loss_price"]), activation_price=to_dec(d["activation_price"]),
            highest=to_dec(d["highest"]), trailing_armed=bool(d.get("trailing_armed")),
            trailing_stop_price=to_dec(d["trailing_stop_price"]) if d.get("trailing_stop_price") else None,
            buy_order_id=d.get("buy_order_id", ""),
        )


class SlotPhase:
    WAITING = "WAITING_BUY"
    HOLDING = "HOLDING"
    ERROR = "ERROR"


@dataclass
class Slot:
    """Estado de uma moeda: esperando gatilho ou em posicao."""
    plan: CoinPlan
    phase: str = SlotPhase.WAITING
    position: Optional[Position] = None
    resolved: Optional[str] = None     # below => compra quando preco <= gatilho; above => >= gatilho
    buy_failures: int = 0
    next_attempt: float = 0.0
    last_price: Optional[Decimal] = None
    error: Optional[str] = None
    day_open: Optional[Decimal] = None        # abertura das ultimas 24h (miniTicker), para a variacao
    trades: int = 0                           # ordens executadas (compras + vendas) desde o inicio
    realized_brl: Decimal = ZERO              # resultado realizado desta moeda
    invested_brl: Decimal = ZERO              # soma dos valores comprados (base do percentual)

    def check_trigger(self, price: Decimal) -> bool:
        t = self.plan
        if self.resolved is None:
            if t.direction in ("below", "above"):
                self.resolved = t.direction
            elif price > t.trigger:
                self.resolved = "below"
            elif price < t.trigger:
                self.resolved = "above"
            else:
                return True
            log.info("%s: gatilho %s, preco atual %s -> compra quando preco %s gatilho", t.symbol,
                     dec_str(t.trigger), dec_str(price), "<=" if self.resolved == "below" else ">=")
        return price <= t.trigger if self.resolved == "below" else price >= t.trigger


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
    """Ordens reais via python-binance (producao ou Spot Testnet)."""

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
    FIELDS = ["timestamp_utc", "timestamp_local", "mode", "symbol", "side", "reason", "qty", "avg_price",
              "quote_total", "quote_asset", "rate_brl", "total_brl", "commission", "commission_asset", "order_id",
              "pnl_quote", "pnl_brl", "pnl_pct"]

    def __init__(self, path: Path, mode: str):
        self.path = path
        self.mode = mode
        new = not path.exists() or path.stat().st_size == 0
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=self.FIELDS)
        if new:
            self._w.writeheader()
            self._fh.flush()

    def record(self, fill: Fill, reason: str, quote_asset: str, rate: Decimal, pnl_quote: Optional[Decimal] = None,
               pnl_brl: Optional[Decimal] = None, pnl_pct: Optional[Decimal] = None) -> None:
        self._w.writerow({
            "timestamp_utc": fill.time.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
            "timestamp_local": fmt_ts(fill.time), "mode": self.mode, "symbol": fill.symbol, "side": fill.side,
            "reason": reason, "qty": dec_str(fill.qty), "avg_price": dec_str(fill.price),
            "quote_total": dec_str(fill.quote), "quote_asset": quote_asset, "rate_brl": dec_str(rate),
            "total_brl": dec_str((fill.quote * rate).quantize(Decimal("0.01"))),
            "commission": dec_str(fill.commission), "commission_asset": fill.commission_asset, "order_id": fill.order_id,
            "pnl_quote": dec_str(pnl_quote) if pnl_quote is not None else "",
            "pnl_brl": dec_str(pnl_brl.quantize(Decimal("0.01"))) if pnl_brl is not None else "",
            "pnl_pct": dec_str(pnl_pct.quantize(Decimal("0.0001"))) if pnl_pct is not None else "",
        })
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------- estrategia
@dataclass
class BotConfig:
    mode: str                          # simulate | testnet | live
    plans: List[CoinPlan]
    sell_on_stop: bool = True
    order_retry_seconds: int = 10
    max_buy_failures: int = 3
    state_file: Optional[Path] = None

    def validate(self) -> None:
        if not self.plans:
            raise ValueError("informe pelo menos uma moeda")
        seen = set()
        for p in self.plans:
            p.validate()
            if p.symbol in seen:
                raise ValueError(f"simbolo repetido: {p.symbol} (cada moeda aparece uma vez)")
            seen.add(p.symbol)


class TradingBot:
    """Varias moedas em paralelo. Recebe precos via handle_price(); executa ordens pela exchange injetada."""

    def __init__(self, cfg: BotConfig, exchange, trade_log: Optional[TradeLog] = None,
                 on_symbols_changed: Optional[Callable[[List[str]], None]] = None):
        self.cfg = cfg
        self.ex = exchange
        self.trades = trade_log
        self.on_symbols_changed = on_symbols_changed or (lambda symbols: None)
        self.slots: Dict[str, Slot] = {}
        self.filters: Dict[str, SymbolFilters] = {}
        self.rates: Dict[str, Decimal] = {BRL: ONE}      # moeda de cotacao -> cotacao em BRL
        self.last_prices: Dict[str, Decimal] = {}
        self.realized_brl = ZERO
        self.sales = 0
        self.phase = "RUNNING"
        self.error_message: Optional[str] = None
        self._rate_warned: set = set()
        self.started_at = time.time()
        self.equity_history: List[tuple] = []     # (timestamp, valor da carteira em R$)
        self._last_equity_sample = 0.0
        self.equity_file: Optional[Path] = (cfg.state_file.parent / f"equity_{cfg.mode}.csv") if cfg.state_file else None

    # -- preparacao -------------------------------------------------------------
    def rate_symbols(self) -> List[str]:
        quotes = {self.filters[s].quote_asset for s in self.slots if s in self.filters}
        return sorted(f"{q}{BRL}" for q in quotes if q != BRL)

    def watched_symbols(self) -> List[str]:
        return list(dict.fromkeys([*self.slots.keys(), *self.rate_symbols()]))

    def _load_filters(self, symbol: str) -> SymbolFilters:
        if symbol not in self.filters:
            self.filters[symbol] = self.ex.filters(symbol)
            f = self.filters[symbol]
            log.info("filtros %s: cotacao=%s step=%s minQty=%s minNotional=%s", symbol, f.quote_asset,
                     dec_str(f.step_size), dec_str(f.min_qty), dec_str(f.min_notional))
        return self.filters[symbol]

    def _ensure_rate(self, quote: str) -> None:
        if quote == BRL or quote in self.rates:
            return
        sym = f"{quote}{BRL}"
        try:
            self.rates[quote] = self.ex.last_price(sym)
            log.info("cotacao %s: %s", sym, dec_str(self.rates[quote]))
        except Exception as exc:  # noqa: BLE001
            log.warning("sem cotacao inicial de %s (%s); compras em %s aguardam a cotacao chegar pelo stream",
                        sym, exc, quote)

    def prepare(self) -> None:
        self.cfg.validate()
        for plan in self.cfg.plans:
            f = self._load_filters(plan.symbol)
            self._ensure_rate(f.quote_asset)
            self.slots[plan.symbol] = Slot(plan=plan)
            log.info("plano: %s", plan.describe())
        self._load_equity_history()
        self.on_symbols_changed(self.watched_symbols())

    # -- conversao de moeda ------------------------------------------------------------
    def rate_for(self, symbol: str) -> Optional[Decimal]:
        return self.rates.get(self.filters[symbol].quote_asset)

    def brl_to_quote(self, symbol: str, brl: Decimal) -> Decimal:
        rate = self.rate_for(symbol)
        if rate is None or rate <= ZERO:
            raise OrderError(f"sem cotacao {self.filters[symbol].quote_asset}{BRL} para converter R$")
        return brl / rate

    # -- niveis ------------------------------------------------------------------------
    @staticmethod
    def _level(rule: Rule, entry: Decimal, qty: Decimal, rate: Decimal, below: bool) -> Decimal:
        if rule.unit == "price":
            return rule.value
        if rule.unit == "pct":
            level = entry * (1 - rule.value / 100) if below else entry * (1 + rule.value / 100)
        elif rule.unit == "total":
            level = (rule.value / rate) / qty          # valor total da posicao em R$ -> preco unitario
        else:
            dist = (rule.value / rate) / qty           # R$ -> quote -> por unidade
            level = entry - dist if below else entry + dist
        return level.quantize(LEVEL_PRECISION)

    @staticmethod
    def _trail_distance(rule: Rule, highest: Decimal, qty: Decimal, rate: Decimal) -> Decimal:
        if rule.unit == "pct":
            return highest * rule.value / 100
        return (rule.value / rate) / qty

    def _trailing_stop(self, pos: Position, rules: ExitRules) -> Decimal:
        """Recuo a partir da maxima, nunca abaixo do minimo para venda (piso)."""
        raw = (pos.highest - self._trail_distance(rules.trail, pos.highest, pos.qty, pos.rate)).quantize(LEVEL_PRECISION)
        return max(raw, pos.activation_price)

    def _apply_levels(self, pos: Position, rules: ExitRules) -> None:
        pos.stop_loss_price = self._level(rules.stop, pos.entry_price, pos.qty, pos.rate, below=True)
        pos.activation_price = self._level(rules.activation, pos.entry_price, pos.qty, pos.rate, below=False)
        if pos.trailing_armed:
            pos.trailing_stop_price = self._trailing_stop(pos, rules)
        if pos.stop_loss_price >= pos.entry_price:
            log.warning("%s: stop-loss (%s) esta acima ou igual ao preco de compra (%s); vende no proximo tick",
                        pos.symbol, dec_str(pos.stop_loss_price), dec_str(pos.entry_price))
        if pos.activation_price <= pos.entry_price:
            log.warning("%s: minimo para venda (%s) esta abaixo ou igual ao preco de compra (%s); o trailing "
                        "arma de imediato e pode vender sem lucro", pos.symbol, dec_str(pos.activation_price),
                        dec_str(pos.entry_price))

    # -- loop de precos -----------------------------------------------------------
    def handle_price(self, symbol: str, price: Decimal, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.last_prices[symbol] = price
        if hasattr(self.ex, "update_price"):
            self.ex.update_price(symbol, price)
        if symbol.endswith(BRL) and len(symbol) > 3:
            quote = symbol[:-3]
            if quote in {self.filters[s].quote_asset for s in self.slots if s in self.filters}:
                self.rates[quote] = price
        slot = self.slots.get(symbol)
        if not slot or self.phase != "RUNNING":
            return
        slot.last_price = price
        if slot.phase == SlotPhase.WAITING:
            if slot.check_trigger(price):
                self._try_buy(slot, price, now)
        elif slot.phase == SlotPhase.HOLDING and slot.position:
            self._manage(slot, price, now)
        self.record_equity(now)

    def handle_ticker(self, symbol: str, day_open: Decimal, last: Optional[Decimal] = None) -> None:
        """Estatisticas de 24h (stream miniTicker): abertura para calcular a variacao como a Binance mostra."""
        slot = self.slots.get(symbol)
        if slot:
            slot.day_open = day_open
            if last is not None and slot.last_price is None:
                slot.last_price = last

    # -- carteira ---------------------------------------------------------------------
    def allocated_brl(self) -> Decimal:
        return sum((s.plan.amount_brl for s in self.slots.values()), ZERO)

    def unrealized_brl(self) -> Decimal:
        total = ZERO
        for s in self.slots.values():
            if s.position and s.last_price is not None:
                rate = self.rate_for(s.plan.symbol) or s.position.rate
                total += s.position.qty * s.last_price * rate - s.position.entry_brl
        return total

    def wallet_value(self) -> Decimal:
        """Capital alocado (soma dos valores por compra) + resultado realizado + resultado das posicoes abertas."""
        return self.allocated_brl() + self.realized_brl + self.unrealized_brl()

    def record_equity(self, now: float, force: bool = False) -> None:
        if not force and now - self._last_equity_sample < EQUITY_SAMPLE_SECONDS:
            return
        self._last_equity_sample = now
        value = self.wallet_value().quantize(Decimal("0.01"))
        self.equity_history.append((now, value))
        cutoff = now - PERIODS["15d"] - 3600
        while self.equity_history and self.equity_history[0][0] < cutoff:
            self.equity_history.pop(0)
        if self.equity_file:
            try:
                with open(self.equity_file, "a", encoding="utf-8") as fh:
                    fh.write(f"{int(now)},{dec_str(value)}\n")
            except OSError as exc:
                log.debug("nao foi possivel gravar historico da carteira: %s", exc)

    def _load_equity_history(self) -> None:
        if not self.equity_file or not self.equity_file.is_file():
            return
        cutoff = time.time() - PERIODS["15d"] - 3600
        try:
            for line in self.equity_file.read_text(encoding="utf-8").splitlines():
                ts, _, val = line.partition(",")
                if ts and val and float(ts) >= cutoff:
                    self.equity_history.append((float(ts), to_dec(val)))
        except (OSError, ValueError) as exc:
            log.warning("historico da carteira ignorado: %s", exc)

    def wallet_changes(self, now: Optional[float] = None) -> Dict[str, dict]:
        now = time.time() if now is None else now
        current = self.wallet_value()
        out = {}
        for name, secs in PERIODS.items():
            ref = None
            since_start = False
            for ts, val in reversed(self.equity_history):
                if ts <= now - secs:
                    ref = val
                    break
            if ref is None and self.equity_history:
                ref = self.equity_history[0][1]
                since_start = True
            if ref is None or ref == ZERO:
                out[name] = {"pct": None, "brl": None, "since_start": True}
            else:
                out[name] = {"pct": dec_str(((current - ref) / ref * 100).quantize(Decimal("0.01"))),
                             "brl": dec_str((current - ref).quantize(Decimal("0.01"))), "since_start": since_start}
        return out

    # -- compra ---------------------------------------------------------------------
    def _try_buy(self, slot: Slot, price: Decimal, now: float) -> None:
        if now < slot.next_attempt:
            return
        plan, f = slot.plan, self.filters[slot.plan.symbol]
        try:
            rate = self.rate_for(plan.symbol)
            if rate is None:
                if plan.symbol not in self._rate_warned:
                    self._rate_warned.add(plan.symbol)
                    log.warning("%s: aguardando cotacao %s%s para converter R$ em %s", plan.symbol, f.quote_asset, BRL,
                                f.quote_asset)
                slot.next_attempt = now + 2
                return
            quote_amount = floor_to_step(self.brl_to_quote(plan.symbol, plan.amount_brl),
                                         Decimal(1).scaleb(-f.quote_precision))
            if f.min_notional > ZERO and quote_amount < f.min_notional:
                raise OrderError(f"{brl_str(plan.amount_brl)} = {dec_str(quote_amount)} {f.quote_asset}, abaixo do "
                                 f"minimo da Binance ({dec_str(f.min_notional)} {f.quote_asset})")
            log.info("COMPRA %s a mercado: gatilho=%s preco=%s valor=%s (%s %s, cotacao %s)", plan.symbol,
                     dec_str(plan.trigger), dec_str(price), brl_str(plan.amount_brl), dec_str(quote_amount),
                     f.quote_asset, dec_str(rate))
            if self.cfg.mode != "simulate":
                free = self.ex.free_balance(f.quote_asset)
                if free < quote_amount:
                    raise OrderError(f"saldo insuficiente em {f.quote_asset}: {dec_str(free)} < {dec_str(quote_amount)}")
            fill = self.ex.market_buy_quote(f, quote_amount)
        except Exception as exc:  # noqa: BLE001 - qualquer falha de ordem deve ser registrada
            slot.buy_failures += 1
            slot.next_attempt = now + self.cfg.order_retry_seconds
            log.error("falha na compra de %s (%d/%d): %s", plan.symbol, slot.buy_failures, self.cfg.max_buy_failures, exc)
            if slot.buy_failures >= self.cfg.max_buy_failures:
                slot.phase = SlotPhase.ERROR
                slot.error = f"compra falhou {slot.buy_failures} vezes: {exc}"
                log.error("%s desativada apos falhas seguidas; as outras moedas continuam", plan.symbol)
                self.save_state()
            return
        slot.buy_failures = 0
        pos = Position(symbol=fill.symbol, qty=fill.qty, entry_price=fill.price, entry_quote=fill.quote,
                       entry_brl=fill.quote * rate, rate=rate, entry_time=fill.time, stop_loss_price=ZERO,
                       activation_price=ZERO, highest=fill.price, buy_order_id=fill.order_id)
        self._apply_levels(pos, plan.rules)
        slot.position, slot.phase = pos, SlotPhase.HOLDING
        log.info("ORDEM EXECUTADA | COMPRA %s | qtd=%s | preco medio=%s | total=%s %s (%s) | taxa=%s %s | id=%s | %s",
                 fill.symbol, dec_str(fill.qty), dec_str(fill.price), dec_str(fill.quote), f.quote_asset,
                 brl_str(pos.entry_brl), dec_str(fill.commission), fill.commission_asset, fill.order_id, fmt_ts(fill.time))
        log.info("niveis %s: stop-loss=%s | minimo para venda=%s | trailing=%s a partir da maxima (nunca abaixo do minimo)",
                 fill.symbol, dec_str(pos.stop_loss_price), dec_str(pos.activation_price),
                 plan.rules.describe().split("trailing=")[1])
        slot.trades += 1
        slot.invested_brl += pos.entry_brl
        if self.trades:
            self.trades.record(fill, f"gatilho {dec_str(plan.trigger)}", f.quote_asset, rate)
        self.save_state()
        self.record_equity(now, force=True)

    # -- gestao da posicao -----------------------------------------------------------
    def _manage(self, slot: Slot, price: Decimal, now: float) -> None:
        pos, rules = slot.position, slot.plan.rules
        if price <= pos.stop_loss_price:
            self._try_sell(slot, price, "STOP_LOSS", now)
            return
        if not pos.trailing_armed and price >= pos.activation_price:
            pos.trailing_armed = True
            pos.highest = price
            pos.trailing_stop_price = self._trailing_stop(pos, rules)
            log.info("TRAILING ARMADO %s: preco=%s atingiu o minimo para venda (%s). Venda se recuar ate %s",
                     pos.symbol, dec_str(price), dec_str(pos.activation_price), dec_str(pos.trailing_stop_price))
            self.save_state()
        if pos.trailing_armed:
            if price > pos.highest:
                pos.highest = price
                new_stop = self._trailing_stop(pos, rules)
                if pos.trailing_stop_price is None or new_stop > pos.trailing_stop_price:
                    pos.trailing_stop_price = new_stop
                    log.info("nova maxima %s: %s -> trailing stop em %s", pos.symbol, dec_str(price), dec_str(new_stop))
                    self.save_state()
            elif price < pos.highest and pos.trailing_stop_price is not None and price <= pos.trailing_stop_price:
                # vende apenas apos uma reversao (preco abaixo da maxima): nunca no tick em que o minimo e atingido
                reason = "TRAILING_STOP" if pos.trailing_stop_price > pos.activation_price else "MINIMO_VENDA"
                self._try_sell(slot, price, reason, now)

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

    def _try_sell(self, slot: Slot, price: Decimal, reason: str, now: float) -> None:
        if now < slot.next_attempt:
            return
        pos, f = slot.position, self.filters[slot.plan.symbol]
        log.info("VENDA %s a mercado por %s: preco=%s stop-loss=%s minimo=%s trailing=%s maxima=%s", pos.symbol, reason,
                 dec_str(price), dec_str(pos.stop_loss_price), dec_str(pos.activation_price),
                 dec_str(pos.trailing_stop_price), dec_str(pos.highest))
        try:
            qty = self._sell_quantity(pos, price)
            fill = self.ex.market_sell(f, qty)
        except Exception as exc:  # noqa: BLE001
            slot.next_attempt = now + self.cfg.order_retry_seconds
            log.error("FALHA NA VENDA de %s (%s). Nova tentativa em %ss. POSICAO CONTINUA ABERTA. Erro: %s",
                      pos.symbol, reason, self.cfg.order_retry_seconds, exc)
            return
        rate = self.rate_for(pos.symbol) or pos.rate
        pnl_quote = fill.quote - pos.entry_quote
        pnl_brl = fill.quote * rate - pos.entry_brl
        pnl_pct = pnl_quote / pos.entry_quote * 100 if pos.entry_quote else ZERO
        self.realized_brl += pnl_brl
        self.sales += 1
        log.info("ORDEM EXECUTADA | VENDA %s (%s) | qtd=%s | preco medio=%s | total=%s %s (%s) | taxa=%s %s | id=%s | %s",
                 fill.symbol, reason, dec_str(fill.qty), dec_str(fill.price), dec_str(fill.quote), f.quote_asset,
                 brl_str(fill.quote * rate), dec_str(fill.commission), fill.commission_asset, fill.order_id, fmt_ts(fill.time))
        log.info("RESULTADO %s: %s (%s%%) | acumulado: %s em %d venda(s)", fill.symbol, brl_str(pnl_brl),
                 dec_str(pnl_pct.quantize(Decimal("0.01"))), brl_str(self.realized_brl), self.sales)
        if self.trades:
            self.trades.record(fill, reason, f.quote_asset, rate, pnl_quote, pnl_brl, pnl_pct)
        slot.trades += 1
        slot.realized_brl += pnl_brl
        slot.position, slot.phase, slot.resolved = None, SlotPhase.WAITING, None
        log.info("%s volta a observar o gatilho %s", pos.symbol, dec_str(slot.plan.trigger))
        self.save_state()
        self.record_equity(now, force=True)

    # -- atualizacoes em execucao (alimentar) ---------------------------------------------
    def update_plans(self, plans: List[CoinPlan], mode: str = "upsert") -> List[str]:
        """mode=upsert: inclui novas moedas e atualiza as existentes; mode=replace: a lista passa a ser esta.
        Uma moeda com posicao aberta nao e removida: a posicao continua sendo gerida ate vender."""
        for p in plans:
            p.validate()
        symbols = [p.symbol for p in plans]
        if len(set(symbols)) != len(symbols):
            raise ValueError("simbolo repetido na lista")
        for p in plans:
            self._load_filters(p.symbol)
        changed: List[str] = []
        if mode == "replace":
            for sym in list(self.slots):
                if sym not in symbols:
                    if self.slots[sym].position:
                        log.warning("%s tem posicao aberta e nao foi removida; sera encerrada quando vender", sym)
                        continue
                    del self.slots[sym]
                    changed.append(f"{sym} removida")
        for p in plans:
            slot = self.slots.get(p.symbol)
            if slot is None:
                self.slots[p.symbol] = Slot(plan=p)
                self._ensure_rate(self.filters[p.symbol].quote_asset)
                changed.append(f"{p.symbol} incluida")
            else:
                old_trigger = slot.plan.trigger
                slot.plan = p
                if slot.phase == SlotPhase.ERROR:
                    slot.phase, slot.error, slot.buy_failures = SlotPhase.WAITING, None, 0
                if old_trigger != p.trigger:
                    slot.resolved = None
                if slot.position:
                    self._apply_levels(slot.position, p.rules)
                    log.info("niveis recalculados %s: stop-loss=%s minimo=%s trailing=%s", p.symbol,
                             dec_str(slot.position.stop_loss_price), dec_str(slot.position.activation_price),
                             dec_str(slot.position.trailing_stop_price))
                changed.append(f"{p.symbol} atualizada")
        self.cfg.plans = [s.plan for s in self.slots.values()]
        for line in changed:
            log.info("plano: %s", line)
        self.save_state()
        self.on_symbols_changed(self.watched_symbols())
        return changed

    def remove_plans(self, symbols: List[str]) -> List[str]:
        removed = []
        for sym in [normalize_symbol(s) for s in symbols]:
            slot = self.slots.get(sym)
            if not slot:
                raise ValueError(f"{sym} nao esta na lista")
            if slot.position:
                raise ValueError(f"{sym} tem posicao aberta; venda pela aba Parar ou aguarde a venda antes de remover")
            del self.slots[sym]
            removed.append(sym)
            log.info("plano: %s removida", sym)
        self.cfg.plans = [s.plan for s in self.slots.values()]
        self.save_state()
        self.on_symbols_changed(self.watched_symbols())
        return removed

    def open_positions(self) -> List[Position]:
        return [s.position for s in self.slots.values() if s.position]

    def snapshot(self) -> dict:
        now = time.time()
        q2 = Decimal("0.01")
        coins = []
        for sym, s in self.slots.items():
            f = self.filters.get(sym)
            price = s.last_price
            rate = (self.rate_for(sym) if f else None)
            pos = s.position.to_dict() if s.position else None
            value_brl = None
            if s.position and price is not None:
                p = s.position
                r = rate or p.rate
                value_brl = (p.qty * price * r).quantize(q2)
                pos["value_brl"] = dec_str(value_brl)
                pos["pnl_brl"] = dec_str((value_brl - p.entry_brl).quantize(q2))
                pos["pnl_pct"] = dec_str(((price / p.entry_price - 1) * 100).quantize(q2))
                pos["stop_pct"] = dec_str(((p.stop_loss_price / p.entry_price - 1) * 100).quantize(q2))
                pos["activation_pct"] = dec_str(((p.activation_price / p.entry_price - 1) * 100).quantize(q2))
                pos["trailing_pct_from_high"] = (dec_str(((p.trailing_stop_price / p.highest - 1) * 100).quantize(q2))
                                                 if p.trailing_armed and p.trailing_stop_price else None)
            change_24h = None
            if s.day_open and price is not None and s.day_open > ZERO:
                change_24h = dec_str(((price / s.day_open - 1) * 100).quantize(q2))
            coins.append({
                "symbol": sym, "phase": s.phase, "quote_asset": f.quote_asset if f else None,
                "rate": dec_str(rate) if rate is not None else None,
                "price": dec_str(price) if price is not None else None, "change_24h_pct": change_24h,
                "plan": s.plan.to_dict(), "resolved": s.resolved, "position": pos, "error": s.error,
                "trades": s.trades, "realized_brl": dec_str(s.realized_brl.quantize(q2)),
                "realized_pct": dec_str((s.realized_brl / s.invested_brl * 100).quantize(q2)) if s.invested_brl else None,
                "balance_brl": dec_str(value_brl) if value_brl is not None else "0",
            })
        coins.sort(key=lambda c: (0 if c["phase"] == SlotPhase.HOLDING else 1 if c["phase"] == SlotPhase.WAITING else 2))
        wallet_value = self.wallet_value().quantize(q2)
        return {"phase": self.phase, "mode": self.cfg.mode, "coins": coins, "watched": self.watched_symbols(),
                "rates": {k: dec_str(v) for k, v in self.rates.items()},
                "realized_brl": dec_str(self.realized_brl.quantize(q2)), "sales": self.sales,
                "open_positions": len(self.open_positions()), "error": self.error_message,
                "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
                "wallet": {"value": dec_str(wallet_value), "allocated": dec_str(self.allocated_brl().quantize(q2)),
                           "realized": dec_str(self.realized_brl.quantize(q2)),
                           "unrealized": dec_str(self.unrealized_brl().quantize(q2)),
                           "changes": self.wallet_changes(now), "samples": len(self.equity_history)}}

    # -- parada segura --------------------------------------------------------------------
    def shutdown(self, sell_positions: bool) -> None:
        if self.cfg.mode != "simulate":
            for sym in list(self.slots):
                try:
                    n = self.ex.cancel_open_orders(sym)
                    if n:
                        log.warning("%s: %d ordem(ns) pendente(s) cancelada(s)", sym, n)
                except Exception as exc:  # noqa: BLE001
                    log.error("falha ao verificar/cancelar ordens pendentes de %s: %s", sym, exc)
        for slot in self.slots.values():
            if slot.position and sell_positions:
                price = slot.last_price
                if price is None:
                    try:
                        price = self.ex.last_price(slot.plan.symbol)
                    except Exception as exc:  # noqa: BLE001
                        log.error("sem preco para vender %s na parada: %s", slot.plan.symbol, exc)
                if price is not None:
                    slot.next_attempt = 0.0
                    self._try_sell(slot, price, "STOP_MANUAL", time.time())
            if slot.position:
                p = slot.position
                log.warning("POSICAO ABERTA MANTIDA: %s qtd=%s entrada=%s stop-loss=%s minimo=%s. Sem o bot rodando, "
                            "NAO ha protecao de stop. Reinicie com --resume ou feche manualmente.", p.symbol,
                            dec_str(p.qty), dec_str(p.entry_price), dec_str(p.stop_loss_price), dec_str(p.activation_price))
        self.save_state()

    # -- estado em disco -------------------------------------------------------------------
    def save_state(self) -> None:
        if not self.cfg.state_file:
            return
        state = {
            "saved_at": utcnow().isoformat(), "mode": self.cfg.mode,
            "realized_brl": dec_str(self.realized_brl), "sales": self.sales,
            "started_at": self.started_at,
            "slots": [{"plan": s.plan.to_dict(), "phase": s.phase, "position": s.position.to_dict() if s.position else None,
                       "trades": s.trades, "realized_brl": dec_str(s.realized_brl), "invested_brl": dec_str(s.invested_brl)}
                      for s in self.slots.values()],
        }
        tmp = self.cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.cfg.state_file)

    def resume(self) -> int:
        """Restaura posicoes abertas salvas para as moedas presentes no plano atual. Devolve quantas."""
        if not self.cfg.state_file or not self.cfg.state_file.is_file():
            log.warning("--resume informado, mas nao ha arquivo de estado em %s", self.cfg.state_file)
            return 0
        state = json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        if state.get("mode") != self.cfg.mode:
            raise ValueError(f"estado salvo e do modo '{state.get('mode')}', diferente de '{self.cfg.mode}'")
        self.realized_brl = to_dec(state.get("realized_brl", "0"))
        self.sales = int(state.get("sales", 0))
        restored = 0
        for item in state.get("slots", []):
            pos = item.get("position")
            if not pos:
                continue
            sym = pos["symbol"]
            if sym not in self.slots:
                plan = CoinPlan.from_dict(item["plan"])
                self._load_filters(sym)
                self._ensure_rate(self.filters[sym].quote_asset)
                self.slots[sym] = Slot(plan=plan)
                self.cfg.plans.append(plan)
                log.info("%s nao estava no plano informado; recuperada do estado salvo", sym)
            slot = self.slots[sym]
            slot.position = Position.from_dict(pos)
            slot.phase = SlotPhase.HOLDING
            slot.trades = int(item.get("trades", 0))
            slot.realized_brl = to_dec(item.get("realized_brl", "0"))
            slot.invested_brl = to_dec(item.get("invested_brl", "0"))
            self._apply_levels(slot.position, slot.plan.rules)
            restored += 1
            log.info("posicao retomada: %s qtd=%s entrada=%s (salvo em %s)", sym, dec_str(slot.position.qty),
                     dec_str(slot.position.entry_price), state.get("saved_at"))
        self.on_symbols_changed(self.watched_symbols())
        return restored

    def status_line(self) -> str:
        parts = []
        for sym, s in self.slots.items():
            price = dec_str(s.last_price) if s.last_price is not None else "-"
            if s.position:
                p = s.position
                parts.append(f"{sym} EM POSICAO preco={price} entrada={dec_str(p.entry_price)} stop={dec_str(p.stop_loss_price)} "
                             f"min={dec_str(p.activation_price)} trailing={dec_str(p.trailing_stop_price) if p.trailing_armed else 'nao armado'}")
            elif s.phase == SlotPhase.ERROR:
                parts.append(f"{sym} ERRO")
            else:
                parts.append(f"{sym} aguardando gatilho {dec_str(s.plan.trigger)} preco={price}")
        return " | ".join(parts) + f" | acumulado={brl_str(self.realized_brl)}"


# --------------------------------------------------------------------------- feed de precos (WebSocket)
class PriceFeed:
    """Stream <symbol>@trade via python-binance ThreadedWebsocketManager -> fila thread-safe.

    A cada mudanca de simbolos (ou reconexao) o gerenciador e recriado por completo: a python-binance
    identifica sockets pelo caminho dos streams e reabrir o mesmo caminho logo apos stop_socket()
    pode deixar dois ouvintes ativos. Mudancas sao raras, entao recriar e simples e seguro.
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
            elif data.get("e") == "24hrMiniTicker" and "o" in data and "s" in data:
                self._q.put(("ticker", data["s"], to_dec(data["o"]), to_dec(data.get("c", data["o"]))))
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
            symbols = list(symbols)
            if self._enabled and self._twm is not None and symbols == self._symbols:
                return
            self._symbols = symbols
            if not self._enabled:
                return
            self._close()
            if not symbols:
                return
            twm = self._twm_cls(testnet=self._testnet)
            twm.daemon = True
            twm.start()
            streams = [f"{s.lower()}@trade" for s in symbols] + [f"{s.lower()}@miniTicker" for s in symbols]
            twm.start_multiplex_socket(callback=self._on_message, streams=streams)
            self._twm = twm
            log.info("websocket assinado: %s", ", ".join(streams))

    def restart(self) -> None:
        log.warning("reconectando websocket...")
        with self._lock:
            self._close()
        self.set_symbols(self._symbols)

    def stop(self) -> None:
        with self._lock:
            self._enabled = False
            self._close()


# --------------------------------------------------------------------------- CLI
PLAN_HELP = ('plano de uma moeda, entre aspas, repita para varias: "SIMBOLO trigger=PRECO amount=REAIS '
             'stop=UNIDADE:VALOR activation=UNIDADE:VALOR trail=UNIDADE:VALOR [direction=auto|below|above]". '
             'Unidades: money (R$ na operacao), pct (%%), total (valor total da posicao em R$), price (so no minimo de venda). '
             'Ex.: "SUSDT trigger=0.067 amount=50 stop=money:1 activation=price:0.071 trail=pct:0.8"')


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bot spot Binance: varias moedas em paralelo, compra por gatilho, stop-loss, minimo de venda e trailing.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--mode", choices=["simulate", "testnet", "live"], default="simulate",
                   help="simulate: sem ordens reais; testnet: Spot Testnet da Binance; live: dinheiro real")
    p.add_argument("--plan", action="append", default=[], metavar="PLANO", help=PLAN_HELP)
    p.add_argument("--config", metavar="ARQUIVO.json",
                   help='arquivo JSON com {"plans": [...]} no formato salvo pela pagina Sistema')
    o = p.add_argument_group("operacao")
    o.add_argument("--no-sell-on-stop", action="store_true",
                   help="ao parar (Ctrl+C), NAO vende as posicoes abertas (padrao: vende a mercado)")
    o.add_argument("--resume", action="store_true", help="retoma as posicoes salvas no arquivo de estado")
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
                   help="(apenas simulate) le precos de um CSV 'SIMBOLO,PRECO' em vez do websocket; sem rede. "
                        "Inclua linhas USDTBRL,<cotacao> para pares em USDT")
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


def plans_from_json(path: Path) -> List[CoinPlan]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data["plans"] if isinstance(data, dict) else data
    return [CoinPlan.from_dict(d) for d in items]


def config_from_args(args) -> BotConfig:
    plans = [CoinPlan.parse(t) for t in args.plan]
    if args.config:
        plans.extend(plans_from_json(Path(args.config)))
    cfg = BotConfig(mode=args.mode, plans=plans, sell_on_stop=not args.no_sell_on_stop,
                    order_retry_seconds=args.order_retry_seconds,
                    state_file=Path(args.data_dir) / f"state_{args.mode}.json")
    cfg.validate()
    return cfg


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
    for p in cfg.plans:
        print("  " + p.describe())
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
            bot.handle_price(normalize_symbol(row[0]), to_dec(row[1]), now=float(i) * 1000)
            if bot.phase != "RUNNING":
                break
    log.info("replay encerrado | acumulado: %s | vendas: %d | posicoes abertas: %d", brl_str(bot.realized_brl),
             bot.sales, len(bot.open_positions()))


def run_live_loop(bot: TradingBot, feed: PriceFeed, q: "queue.Queue", args, stop_event: threading.Event) -> None:
    last_msg = time.time()
    last_status = 0.0
    feed.start()
    feed.set_symbols(bot.watched_symbols())
    while not stop_event.is_set() and bot.phase == "RUNNING":
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
            elif item[0] == "ticker":
                bot.handle_ticker(item[1], item[2], item[3])
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
    except (ValueError, OSError, KeyError) as exc:
        log.error("parametros invalidos: %s", exc)
        return 2

    log.info("=== binance_bot | modo=%s | %d moeda(s) | vender ao parar=%s ===", cfg.mode.upper(), len(cfg.plans),
             cfg.sell_on_stop)
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
        bot.shutdown(sell_positions=cfg.sell_on_stop and stop_event.is_set())
        trade_log.close()
        log.info("encerrado | acumulado=%s | vendas=%d | posicoes abertas=%d | log=%s | operacoes=%s",
                 brl_str(bot.realized_brl), bot.sales, len(bot.open_positions()), data_dir / "bot.log",
                 data_dir / f"trades_{cfg.mode}.csv")
        if bot.phase == "ERROR":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
