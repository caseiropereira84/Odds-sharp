"""
scrape_loab_playwright.py

Versão com Playwright (browser sem interface) do scraper do
List Of All Bookmakers. Necessária porque a tabela de bookmakers
é preenchida via JavaScript mesmo nas páginas por país — o
`requests` simples não vê essas linhas.

Uso:
    python scrape_loab_playwright.py --countries pt fr ee lv lt
"""

import argparse
import csv
import logging
import re
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, fields
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://listofallbookmakers.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scrape_loab_pw")

RATING_LABELS = {
    "rating_confianca": ["Trust & Fairness", "Trust and Fairness"],
    "rating_jogos_software": ["Games & Software", "Games and Software"],
    "rating_bonus": ["Bonuses & Offers", "Bonuses and Offers"],
    "rating_suporte": ["Customer Support"],
}


@dataclass
class Bookmaker:
    pais: str = ""
    bookmaker_name: str = ""
    promo: str = ""
    turnover: str = ""
    min_odds: str = ""
    rating_confianca: Optional[float] = None
    rating_jogos_software: Optional[float] = None
    rating_bonus: Optional[float] = None
    rating_suporte: Optional[float] = None
    url_review: str = ""
    url_visit: str = ""


def check_robots_allowed(paths: List[str]) -> bool:
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urljoin(BASE_URL, "/robots.txt")
    try:
        rp.set_url(robots_url)
        rp.read()
    except Exception as e:
        log.warning("Não foi possível ler robots.txt: %s — a assumir bloqueio.", e)
        return False
    ok = True
    for path in paths:
        allowed = rp.can_fetch(USER_AGENT, urljoin(BASE_URL, path))
        log.info("robots.txt: %s -> %s", path, "permitido" if allowed else "BLOQUEADO")
        ok = ok and allowed
    return ok


def parse_summary_table(soup: BeautifulSoup, country_code: str) -> dict:
    results = {}
    table = soup.find("table")
    if table is None:
        log.warning("Nenhuma <table> encontrada (mesmo após render JS) para %s.", country_code)
        return results

    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        name_cell = cells[0]
        links = name_cell.find_all("a", href=True)
        name_link = next((a for a in links if "/visit/" not in a["href"] and a.get_text(strip=True)), None)
        if not name_link:
            continue

        name = name_link.get_text(strip=True)
        if not name:
            continue

        bk = Bookmaker(pais=country_code, bookmaker_name=name)
        bk.url_review = urljoin(BASE_URL, name_link["href"])

        visit_link = next((a for a in name_cell.find_all("a", href=True) if "/visit/" in a["href"]), None)
        if not visit_link:
            visit_link = next((a for a in row.find_all("a", href=True) if "/visit/" in a["href"]), None)
        if visit_link:
            bk.url_visit = urljoin(BASE_URL, visit_link["href"])

        text_cells = [c.get_text(" ", strip=True) for c in cells]
        for cell_text in text_cells:
            low = cell_text.lower()
            if "x" in low and re.match(r"^\d", low.strip()) and not bk.turnover:
                bk.turnover = cell_text
            elif re.match(r"^\d+(\.\d+)?$", cell_text.strip()) and not bk.min_odds:
                bk.min_odds = cell_text
        promo_candidates = sorted(text_cells, key=len, reverse=True)
        if promo_candidates:
            bk.promo = promo_candidates[0][:300]

        results[name.lower()] = bk

    return results


def parse_rating_breakdowns(soup: BeautifulSoup) -> dict:
    results = {}
    heading_pattern = re.compile(r"(.+?)\s+Rating Breakdown", re.IGNORECASE)
    headings = soup.find_all(string=heading_pattern)

    for heading_text in headings:
        m = heading_pattern.search(str(heading_text))
        if not m:
            continue
        bookmaker_name = m.group(1).strip()

        container = heading_text.parent
        search_scope = container
        for _ in range(4):
            if search_scope is None:
                break
            scope_text = search_scope.get_text(" ", strip=True)
            if "Trust & Fairness" in scope_text or "Customer Support" in scope_text:
                break
            search_scope = search_scope.parent

        if search_scope is None:
            continue

        entry = {}
        scope_text = search_scope.get_text("\n", strip=True)
        for field_name, labels in RATING_LABELS.items():
            for label in labels:
                m2 = re.search(rf"{re.escape(label)}\D*(\d(?:\.\d)?)", scope_text)
                if m2:
                    entry[field_name] = float(m2.group(1))
                    break

        if entry:
            results[bookmaker_name.lower()] = entry

    return results


def scrape_country(page, country_code: str) -> List[Bookmaker]:
    url = f"{BASE_URL}/{country_code}/"
    log.info("A processar país: %s (%s)", country_code, url)

    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        # espera extra para garantir que tabelas dinâmicas carregam
        page.wait_for_timeout(2000)
        # tenta clicar em "Load More" se existir, para apanhar tudo
        for _ in range(5):
            load_more = page.locator("text=Load More").first
            if load_more.count() > 0 and load_more.is_visible():
                try:
                    load_more.click(timeout=3000)
                    page.wait_for_timeout(1500)
                except Exception:
                    break
            else:
                break
        html = page.content()
    except Exception as e:
        log.error("Falha ao carregar %s: %s", url, e)
        return []

    soup = BeautifulSoup(html, "html.parser")

    bookmakers = parse_summary_table(soup, country_code)
    if not bookmakers:
        log.warning("Nenhum bookmaker extraído para %s mesmo com JS renderizado.", country_code)
        return []

    ratings = parse_rating_breakdowns(soup)
    matched = 0
    for name_key, bk in bookmakers.items():
        entry = ratings.get(name_key)
        if entry:
            matched += 1
            for field_name, value in entry.items():
                setattr(bk, field_name, value)

    log.info("  -> %d bookmakers, %d com rating breakdown", len(bookmakers), matched)
    return list(bookmakers.values())


def main():
    parser = argparse.ArgumentParser(description="Scraper (Playwright) do List Of All Bookmakers")
    parser.add_argument("--countries", nargs="+", default=["pt"])
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--output", type=str, default="data/loab_bookmakers.csv")
    parser.add_argument("--ignore-robots", action="store_true")
    args = parser.parse_args()

    paths_to_check = [f"/{c}/" for c in args.countries]
    allowed = check_robots_allowed(paths_to_check)
    if not allowed and not args.ignore_robots:
        log.error("robots.txt bloqueia um ou mais caminhos. A parar.")
        sys.exit(1)
    elif not allowed:
        log.warning("robots.txt bloqueia, mas --ignore-robots foi passado.")

    all_results: List[Bookmaker] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        for country in args.countries:
            results = scrape_country(page, country)
            all_results.extend(results)
            time.sleep(args.delay)

        browser.close()

    if not all_results:
        log.error("Nenhum resultado extraído em nenhum país, mesmo com JS renderizado. A abortar.")
        sys.exit(1)

    import os
    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    fieldnames = [f.name for f in fields(Bookmaker)]
    try:
        import pandas as pd
        df = pd.DataFrame([vars(r) for r in all_results])[fieldnames]
        df.to_csv(args.output, index=False)
    except ImportError:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in all_results:
                writer.writerow(vars(r))

    log.info("Concluído. %d bookmakers extraídos (%d países).", len(all_results), len(args.countries))
    log.info("Ficheiro: %s", args.output)

    if len(args.countries) > 1:
        consolidated = {}
        for bk in all_results:
            key = bk.bookmaker_name.strip().lower()
            if key not in consolidated:
                consolidated[key] = {
                    "bookmaker_name": bk.bookmaker_name,
                    "paises": [bk.pais],
                    "rating_confianca": bk.rating_confianca,
                    "rating_jogos_software": bk.rating_jogos_software,
                    "rating_bonus": bk.rating_bonus,
                    "rating_suporte": bk.rating_suporte,
                    "url_review": bk.url_review,
                    "url_visit": bk.url_visit,
                }
            else:
                if bk.pais not in consolidated[key]["paises"]:
                    consolidated[key]["paises"].append(bk.pais)
                for field_name in ("rating_confianca", "rating_jogos_software", "rating_bonus", "rating_suporte"):
                    if consolidated[key][field_name] is None:
                        consolidated[key][field_name] = getattr(bk, field_name)

        consolidated_rows = []
        for entry in consolidated.values():
            entry = dict(entry)
            entry["num_paises"] = len(entry["paises"])
            entry["paises"] = ",".join(sorted(entry["paises"]))
            consolidated_rows.append(entry)

        consolidated_rows.sort(key=lambda r: r["num_paises"], reverse=True)

        consolidated_path = args.output.replace(".csv", "_transversal.csv")
        cons_fieldnames = [
            "bookmaker_name", "num_paises", "paises",
            "rating_confianca", "rating_jogos_software", "rating_bonus", "rating_suporte",
            "url_review", "url_visit",
        ]
        try:
            import pandas as pd
            pd.DataFrame(consolidated_rows)[cons_fieldnames].to_csv(consolidated_path, index=False)
        except ImportError:
            with open(consolidated_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=cons_fieldnames)
                writer.writeheader()
                for row in consolidated_rows:
                    writer.writerow(row)

        log.info("Ficheiro consolidado: %s (%d bookmakers únicos)", consolidated_path, len(consolidated_rows))


if __name__ == "__main__":
    main()
