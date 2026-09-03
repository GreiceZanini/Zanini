#!/usr/bin/env python3
"""Automação de download XML NFS-e por competência mensal.

Uso básico:
python scripts/baixar_nfse_xml.py \
  --tipo recebidas \
  --inicio 2024-01-01 \
  --fim 2024-12-31 \
  --download-dir ./downloads \
  --headed

Importante:
- Este script parte do pressuposto de que você já está autenticado no portal.
- Os seletores podem variar conforme atualizações do site e devem ser ajustados.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from playwright.sync_api import BrowserContext, Page, TimeoutError, sync_playwright

TARGET_URL = "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas"


@dataclass(frozen=True)
class MonthWindow:
    start: date
    end: date


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Baixar XML de NFS-e por período mensal")
    parser.add_argument("--tipo", choices=["recebidas", "emitidas"], required=True)
    parser.add_argument("--inicio", required=True, help="Data inicial no formato YYYY-MM-DD")
    parser.add_argument("--fim", required=True, help="Data final no formato YYYY-MM-DD")
    parser.add_argument("--download-dir", required=True, help="Diretório de destino dos XML")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--headed", action="store_true", help="Executa com navegador visível")
    return parser.parse_args()


def date_to_ddmmyyyy(value: date) -> str:
    return value.strftime("%d%m%Y")


def monthly_windows(start: date, end: date) -> Iterable[MonthWindow]:
    if end < start:
        raise ValueError("Data final não pode ser menor que data inicial")

    current = date(start.year, start.month, 1)
    while current <= end:
        next_month = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_end = next_month - timedelta(days=1)
        yield MonthWindow(start=max(current, start), end=min(month_end, end))
        current = next_month


def fill_filters(page: Page, tipo: str, dt_start: date, dt_end: date, timeout_ms: int) -> None:
    # Ajuste os seletores conforme necessário.
    if tipo == "recebidas":
        page.get_by_role("radio", name="Recebidas").check(timeout=timeout_ms)
    else:
        page.get_by_role("radio", name="Emitidas").check(timeout=timeout_ms)

    page.locator("input[placeholder='Data Inicial']").fill(date_to_ddmmyyyy(dt_start), timeout=timeout_ms)
    page.locator("input[placeholder='Data Final']").fill(date_to_ddmmyyyy(dt_end), timeout=timeout_ms)
    page.get_by_role("button", name="Filtrar").click(timeout=timeout_ms)

    # Espera básica pelo recarregamento da listagem.
    page.wait_for_load_state("networkidle", timeout=timeout_ms)


def download_current_page_rows(page: Page, download_dir: Path, timeout_ms: int) -> int:
    downloaded = 0
    rows = page.locator("table tbody tr")
    count = rows.count()

    for idx in range(count):
        row = rows.nth(idx)
        menu_button = row.get_by_role("button", name="...")
        menu_button.click(timeout=timeout_ms)

        with page.expect_download(timeout=timeout_ms) as download_info:
            page.get_by_role("menuitem", name="Download XML").click(timeout=timeout_ms)

        download = download_info.value
        filename = download.suggested_filename
        target = download_dir / filename
        download.save_as(str(target))
        downloaded += 1

    return downloaded


def go_next_page_if_exists(page: Page, timeout_ms: int) -> bool:
    next_btn = page.get_by_role("button", name="Próxima")
    if next_btn.count() == 0:
        return False

    disabled = next_btn.get_attribute("disabled")
    if disabled is not None:
        return False

    next_btn.click(timeout=timeout_ms)
    page.wait_for_load_state("networkidle", timeout=timeout_ms)
    return True


def process_month(page: Page, month: MonthWindow, tipo: str, download_dir: Path, timeout_ms: int) -> int:
    fill_filters(page, tipo, month.start, month.end, timeout_ms)

    total = 0
    while True:
        total += download_current_page_rows(page, download_dir, timeout_ms)
        if not go_next_page_if_exists(page, timeout_ms):
            break

    return total


def build_context(download_dir: Path, headed: bool):
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=not headed)
    context = browser.new_context(accept_downloads=True)
    return playwright, browser, context


def ensure_authenticated(page: Page, timeout_ms: int) -> None:
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(1500)

    # Verificação simples: se existe botão de login, sessão não está autenticada.
    if page.get_by_role("button", name="Entrar").count() > 0:
        raise RuntimeError(
            "Sessão não autenticada. Faça login manualmente e reutilize storage_state, "
            "ou execute o script com perfil já logado."
        )


def main() -> int:
    args = parse_args()
    start = datetime.strptime(args.inicio, "%Y-%m-%d").date()
    end = datetime.strptime(args.fim, "%Y-%m-%d").date()
    download_dir = Path(args.download_dir).expanduser().resolve()
    download_dir.mkdir(parents=True, exist_ok=True)

    playwright, browser, context = build_context(download_dir, args.headed)

    try:
        page = context.new_page()
        ensure_authenticated(page, args.timeout_ms)

        grand_total = 0
        for month in monthly_windows(start, end):
            print(f"[INFO] Processando período: {month.start} -> {month.end}")
            try:
                total_month = process_month(page, month, args.tipo, download_dir, args.timeout_ms)
                grand_total += total_month
                print(f"[INFO] XML baixados no mês: {total_month}")
            except TimeoutError as exc:
                print(f"[ERRO] Timeout no período {month.start} a {month.end}: {exc}")

        print(f"[FIM] Total de XML baixados: {grand_total}")
        return 0
    finally:
        context.close()
        browser.close()
        playwright.stop()


if __name__ == "__main__":
    raise SystemExit(main())
