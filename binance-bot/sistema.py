#!/usr/bin/env python3
"""
sistema.py - Interface web local (pagina "Sistema") para iniciar, alimentar e parar o binance_bot.

    python sistema.py                 # abre em http://127.0.0.1:8080
    python sistema.py --port 9000

A pagina (sistema.html) e servida por este processo; o bot roda em uma thread interna.
Sem autenticacao: por padrao escuta apenas em 127.0.0.1 (a propria maquina). Nao exponha na internet.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import queue
import signal
import sys
import threading
import types
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, List, Optional

import binance_bot as bb

log = logging.getLogger("sistema")
HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- log em memoria para a pagina
class MemoryLogHandler(logging.Handler):
    def __init__(self, maxlen: int = 400):
        super().__init__()
        self.lines = collections.deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- montagem de parametros
def _rule(item, label: str) -> bb.Rule:
    if not isinstance(item, dict):
        raise ValueError(f"campo '{label}' em formato invalido")
    raw = item.get("value")
    if raw in (None, ""):
        raise ValueError(f"informe o campo '{label}'")
    try:
        value = bb.to_dec(raw)
    except ValueError:
        raise ValueError(f"campo '{label}' deve ser numerico (recebido: {raw!r})")
    return bb.Rule(str(item.get("unit") or "pct").lower(), value)


def plan_from_payload(item: dict, i: int) -> bb.CoinPlan:
    if not isinstance(item, dict):
        raise ValueError(f"moeda #{i} em formato invalido")
    sym = bb.normalize_symbol(item.get("symbol", ""))
    if not sym:
        raise ValueError(f"moeda #{i}: informe o simbolo (ex.: BTCBRL)")
    try:
        trigger = bb.to_dec(item.get("trigger"))
        amount = bb.to_dec(item.get("amount_brl"))
    except ValueError as exc:
        raise ValueError(f"{sym}: gatilho e valor por compra devem ser numericos ({exc})")
    plan = bb.CoinPlan(symbol=sym, trigger=trigger, amount_brl=amount,
                       rules=bb.ExitRules(_rule(item.get("stop"), f"{sym}: stop-loss"),
                                          _rule(item.get("activation"), f"{sym}: minimo para venda na alta"),
                                          _rule(item.get("trail"), f"{sym}: recuo do trailing")),
                       direction=str(item.get("direction") or "auto").lower())
    plan.validate()
    return plan


def plans_from_payload(items) -> List[bb.CoinPlan]:
    plans = []
    for i, item in enumerate(items or [], 1):
        if isinstance(item, dict) and not str(item.get("symbol", "")).strip() and item.get("trigger") in (None, ""):
            continue  # linha vazia do formulario
        plans.append(plan_from_payload(item, i))
    return plans


def config_from_payload(payload: dict, data_dir: Path) -> bb.BotConfig:
    mode = str(payload.get("mode") or "simulate").lower()
    if mode not in ("simulate", "testnet", "live"):
        raise ValueError("modo deve ser simulate, testnet ou live")
    if mode == "live" and str(payload.get("confirm", "")).strip() != "CONFIRMO":
        raise ValueError("modo live exige digitar CONFIRMO no campo de confirmacao")
    plans = plans_from_payload(payload.get("plans"))
    retry = int(payload.get("order_retry_seconds") or 10)
    cfg = bb.BotConfig(mode=mode, plans=plans, sell_on_stop=bool(payload.get("sell_on_stop", True)),
                       order_retry_seconds=max(1, retry), state_file=data_dir / f"state_{mode}.json")
    cfg.validate()
    return cfg


# --------------------------------------------------------------------------- execucao do bot em thread
class BotRunner:
    """Controla uma execucao do bot: iniciar, alimentar (comandos na thread do bot) e parar."""

    def __init__(self, data_dir: Path, exchange_factory: Optional[Callable] = None,
                 feed_factory: Optional[Callable] = None):
        self.data_dir = data_dir
        self._exchange_factory = exchange_factory or self._default_exchange
        self._feed_factory = feed_factory or (lambda q, testnet: bb.PriceFeed(q, testnet=testnet))
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.queue: "queue.Queue" = queue.Queue()
        self.bot: Optional[bb.TradingBot] = None
        self.cfg: Optional[bb.BotConfig] = None
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.sell_on_stop = True
        self.last_error: Optional[str] = None
        self.watchdog_seconds = 90

    @staticmethod
    def _default_exchange(cfg: bb.BotConfig, payload: dict):
        args = types.SimpleNamespace(replay=None, sim_fee_pct=str(payload.get("sim_fee_pct") or "0.1"),
                                     sim_slippage_pct=str(payload.get("sim_slippage_pct") or "0"))
        return bb.make_exchange(cfg, args)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, payload: dict) -> dict:
        with self._lock:
            if self.running:
                raise ValueError("ja existe uma execucao em andamento; pare-a antes de iniciar outra")
            cfg = config_from_payload(payload, self.data_dir)
            self.watchdog_seconds = max(10, int(payload.get("watchdog_seconds") or 90))
            exchange = self._exchange_factory(cfg, payload)
            self.queue = queue.Queue()
            feed = self._feed_factory(self.queue, cfg.mode == "testnet")
            trade_log = bb.TradeLog(self.data_dir / f"trades_{cfg.mode}.csv", cfg.mode)
            bot = bb.TradingBot(cfg, exchange, trade_log, on_symbols_changed=feed.set_symbols)
            bot.prepare()  # valida simbolos/filtros antes de confirmar o inicio
            if payload.get("resume"):
                bot.resume()
            self.bot, self.cfg = bot, cfg
            self.sell_on_stop = cfg.sell_on_stop
            self.last_error = None
            self.finished_at = None
            self._stop_event = threading.Event()
            self.started_at = datetime.now(timezone.utc)
            self._thread = threading.Thread(target=self._run, args=(bot, feed, trade_log, self._stop_event),
                                            name="bot-loop", daemon=True)
            self._thread.start()
            log.info("execucao iniciada pela pagina Sistema (modo %s, %d moeda(s))", cfg.mode, len(cfg.plans))
            return self.status()

    def _run(self, bot: bb.TradingBot, feed, trade_log: bb.TradeLog, stop_event: threading.Event) -> None:
        args = types.SimpleNamespace(watchdog_seconds=self.watchdog_seconds, status_seconds=60)
        try:
            bb.run_live_loop(bot, feed, self.queue, args, stop_event)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.exception("erro fatal na execucao: %s", exc)
        finally:
            try:
                feed.stop()
            finally:
                bot.shutdown(sell_positions=self.sell_on_stop and stop_event.is_set())
                trade_log.close()
                self.finished_at = datetime.now(timezone.utc)
                log.info("execucao encerrada | acumulado=%s | posicoes abertas=%d", bb.brl_str(bot.realized_brl),
                         len(bot.open_positions()))

    def _command(self, fn: Callable[[bb.TradingBot], object], timeout: float = 15):
        done = threading.Event()
        result = {"value": None, "error": None}

        def command(b: bb.TradingBot) -> None:
            try:
                result["value"] = fn(b)
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)
            finally:
                done.set()

        self.queue.put(("command", command))
        if not done.wait(timeout=timeout):
            raise ValueError("o bot nao processou o comando em 15s (websocket travado?)")
        if result["error"]:
            raise ValueError(result["error"])
        return result["value"]

    def feed(self, payload: dict) -> dict:
        with self._lock:
            if not self.running or not self.bot:
                raise ValueError("nenhuma execucao em andamento para alimentar")
            plans = plans_from_payload(payload.get("plans")) if payload.get("plans") is not None else []
            remove = [bb.normalize_symbol(s) for s in (payload.get("remove") or []) if str(s).strip()]
            mode = (payload.get("apply") or "upsert").lower()
            if mode not in ("upsert", "replace"):
                raise ValueError("apply deve ser upsert ou replace")
            if not plans and not remove and mode != "replace":
                raise ValueError("nada para alimentar: informe moedas para incluir/atualizar ou remover")
            changes: List[str] = []
            if remove:
                changes += [f"{s} removida" for s in self._command(lambda b: b.remove_plans(remove))]
            if plans or mode == "replace":
                changes += self._command(lambda b: b.update_plans(plans, mode))
            return {"applied": changes, "status": self.status()}

    def stop(self, sell_positions: bool, wait_seconds: float = 30) -> dict:
        with self._lock:
            if not self.running:
                raise ValueError("nenhuma execucao em andamento")
            self.sell_on_stop = bool(sell_positions)
            log.warning("parada solicitada pela pagina Sistema (vender posicoes: %s)", self.sell_on_stop)
            self._stop_event.set()
            thread = self._thread
        thread.join(timeout=wait_seconds)
        return self.status()

    def status(self) -> dict:
        bot = self.bot
        return {
            "running": self.running,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "last_error": self.last_error,
            "sell_on_stop": self.sell_on_stop,
            "bot": bot.snapshot() if bot else None,
            "server_time": datetime.now(timezone.utc).isoformat(),
        }


# --------------------------------------------------------------------------- servidor HTTP
def make_handler(runner: BotRunner, memlog: MemoryLogHandler):
    page = HERE / "sistema.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "SistemaBot/2.0"

        def log_message(self, fmt, *args):  # silencia o log padrao de cada requisicao
            log.debug("http %s", fmt % args)

        def _json(self, code: int, data) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError("corpo da requisicao muito grande")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON invalido: {exc}")
            if not isinstance(data, dict):
                raise ValueError("corpo deve ser um objeto JSON")
            return data

        def do_GET(self):
            if self.path in ("/", "/sistema", "/sistema.html"):
                if not page.is_file():
                    return self._json(500, {"error": f"arquivo {page.name} nao encontrado"})
                body = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/status"):
                data = runner.status()
                data["log"] = list(memlog.lines)[-200:]
                self._json(200, data)
            else:
                self._json(404, {"error": "nao encontrado"})

        def do_POST(self):
            try:
                payload = self._read_json()
                if self.path == "/api/start":
                    return self._json(200, runner.start(payload))
                if self.path == "/api/feed":
                    return self._json(200, runner.feed(payload))
                if self.path == "/api/stop":
                    return self._json(200, runner.stop(bool(payload.get("sell_positions", True))))
                return self._json(404, {"error": "nao encontrado"})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                log.exception("erro na API: %s", exc)
                self._json(500, {"error": f"erro interno: {exc}"})

    return Handler


def main(argv: Optional[List[str]] = None) -> int:
    bb.load_dotenv()
    p = argparse.ArgumentParser(description="Pagina Sistema: iniciar, alimentar e parar o bot pela web (uso local).")
    p.add_argument("--host", default="127.0.0.1", help="endereco de escuta (padrao: apenas esta maquina)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    args = p.parse_args(argv)

    data_dir = Path(args.data_dir)
    bb.setup_logging(data_dir, args.log_level)
    memlog = MemoryLogHandler()
    logging.getLogger().addHandler(memlog)
    if args.host not in ("127.0.0.1", "localhost"):
        log.warning("escutando em %s SEM autenticacao: qualquer pessoa na rede podera operar o bot", args.host)

    runner = BotRunner(data_dir)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(runner, memlog))
    server.daemon_threads = True

    def _on_signal(signum, _frame):
        log.warning("sinal %s recebido; encerrando servidor e bot...", signal.Signals(signum).name)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    log.info("Pagina Sistema disponivel em http://%s:%d/  (Ctrl+C para encerrar)", args.host, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if runner.running:
            try:
                runner.stop(sell_positions=runner.sell_on_stop)
            except Exception as exc:  # noqa: BLE001
                log.error("falha ao parar o bot: %s", exc)
        log.info("servidor encerrado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
