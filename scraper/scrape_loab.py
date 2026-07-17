"""
scrape_loab.py

Scraper para o "List Of All Bookmakers" (listofallbookmakers.com),
focado nas páginas por país (ex.: /pt/ para Portugal), que são
páginas únicas e estáticas — sem paginação nem "Load More".

robots.txt confirmado como permitindo estes caminhos (verificado via
check_robots.py em [data da verificação]). Ainda assim, este script
volta a verificar antes de correr, por segurança caso o robots.txt
mude.

Dados extraídos (melhor esforço):
- Da tabela de resumo (fiável, é uma <table> HTML real):
    bookmaker_name, promo, turnover, min_odds, url_review, url_visit
- Da secção "Rating Breakdown" (melhor esforço — pode faltar se for
  carregado via JS/accordion em vez de estar no HTML estático):
    rating_confianca (Trust & Fairness)
    rating_jogos_software (Games & Software)
    rating_bonus (Bonuses & Offers)
    rating_suporte (Customer Support)

Uso:
    python scrape_loab.py                  # só Portugal (default)
    python scrape_loab.py --countries pt fr ee   # múltiplos países
    python scrape_loab.py --output data/loab.csv
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

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://listofallbookmakers.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scrape_loab")

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


def fetch_page(session: requests.Session, url: str, retries: int = 2, timeout: int = 15) -> Optional[str]:
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 200:
                return resp.text
            log.warning("HTTP %s em %s (tentativa %d/%d)", resp.status_code, url, attempt + 1, retries + 1)
        except requests.RequestException as e:
            log.warning("Erro em %s (tentativa %d/%d): %s", url, attempt + 1, retries + 1, e)
        if attempt < retries:
            time.sleep(2 ** attempt)
    log.error("Falha definitiva a obter %s", url)
    return None


def _extract_float(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"(\d(?:\.\d)?)", text)
    return float(m.group(1)) if m else None


def parse_summary_table(soup: BeautifulSoup, country_code: str) -> dict:
    """
    Parseia a tabela de resumo (Company | Languages | Promo | Turnover |
    Min. Odds | Bonus Expiry | Visit). Devolve dict {nome_normalizado: Bookmaker}.
    """
    results = {}
    table = soup.find("table")
    if table is None:
        log.warning("Nenhuma <table> encontrada na página — estrutura pode ter mudado.")
        return results

    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        # Nome + link de review: normalmente na primeira célula
        name_cell = cells[0]
        name_link = name_cell.find("a", href=re.compile(rf"/{country_code}/(?!visit/)"))
        if not name_link:
            # fallback: qualquer link dentro da célula que não seja "visit"
            links = name_cell.find_all("a", href=True)
            name_link = next((a for a in links if "/visit/" not in a["href"]), None)
        if not name_link:
            continue

        name = name_link.get_text(strip=True)
        if not name:
            continue

        bk = Bookmaker(pais=country_code, bookmaker_name=name)
        bk.url_review = urljoin(BASE_URL, name_link["href"])

        # link "Visit" / site do bookmaker
        visit_link = name_cell.find("a", href=re.compile(r"/visit/"))
        if not visit_link:
            all_links = row.find_all("a", href=re.compile(r"/visit/"))
            visit_link = all_links[0] if all_links else None
        if visit_link:
            bk.url_visit = urljoin(BASE_URL, visit_link["href"])

        # Restantes colunas: heurística por posição (Promo, Turnover, Min Odds)
        text_cells = [c.get_text(" ", strip=True) for c in cells]
        for cell_text in text_cells:
            low = cell_text.lower()
            if "x" in low and re.match(r"^\d", low.strip()) and not bk.turnover:
                bk.turnover = cell_text
            elif re.match(r"^\d+(\.\d+)?$", cell_text.strip()) and not bk.min_odds:
                bk.min_odds = cell_text
        # Promo é tipicamente a célula mais longa (texto de bónus)
        promo_candidates = sorted(text_cells, key=len, reverse=True)
        if promo_candidates:
            bk.promo = promo_candidates[0][:300]  # corta para não ficar gigante

        results[name.lower()] = bk

    return results


def parse_rating_breakdowns(soup: BeautifulSoup) -> dict:
    """
    Procura blocos "<Nome> Rating Breakdown" e extrai os 4 critérios.
    Melhor esforço: se o conteúdo for carregado via JS/accordion, pode
    não estar presente no HTML estático — nesse caso devolve vazio
    para esse bookmaker, sem falhar o resto do scraping.
    """
    results = {}

    heading_pattern = re.compile(r"(.+?)\s+Rating Breakdown", re.IGNORECASE)
    headings = soup.find_all(string=heading_pattern)

    for heading_text in headings:
        m = heading_pattern.search(str(heading_text))
        if not m:
            continue
        bookmaker_name = m.group(1).strip()

        container = heading_text.parent
        # sobe até encontrar um contentor que tenha as labels de rating por perto
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


def scrape_country(session: requests.Session, country_code: str) -> List[Bookmaker]:
    url = f"{BASE_URL}/{country_code}/"
    log.info("A processar país: %s (%s)", country_code, url)

    html = fetch_page(session, url)
    if html is None:
        return []

    soup = BeautifulSoup(html, "html.parser")

    bookmakers = parse_summary_table(soup, country_code)
    if not bookmakers:
        log.warning("Nenhum bookmaker extraído da tabela para %s. Estrutura pode ter mudado.", country_code)
        return []

    ratings = parse_rating_breakdowns(soup)
    matched_ratings = 0
    for name_key, bk in bookmakers.items():
        rating_entry = ratings.get(name_key)
        if rating_entry:
            matched_ratings += 1
            for field_name, value in rating_entry.items():
                setattr(bk, field_name, value)

    log.info(
        "  -> %d bookmakers na tabela, %d com rating breakdown completo/parcial",
        len(bookmakers),
        matched_ratings,
    )
    return list(bookmakers.values())


def main():
    parser = argparse.ArgumentParser(description="Scraper do List Of All Bookmakers, por país")
    parser.add_argument(
        "--countries",
        nargs="+",
        default=["pt"],
        help="Códigos de país a processar (ex.: pt fr ee). Default: pt",
    )
    parser.add_argument("--delay", type=float, default=2.0, help="Segundos entre pedidos (default: 2.0)")
    parser.add_argument("--output", type=str, default="data/loab_bookmakers.csv", help="CSV de saída")
    parser.add_argument("--ignore-robots", action="store_true", help="NÃO recomendado")
    args = parser.parse_args()

    paths_to_check = [f"/{c}/" for c in args.countries]
    allowed = check_robots_allowed(paths_to_check)
    if not allowed and not args.ignore_robots:
        log.error("robots.txt bloqueia um ou mais caminhos. A parar.")
        sys.exit(1)
    elif not allowed:
        log.warning("robots.txt bloqueia, mas --ignore-robots foi passado. A prosseguir sob responsabilidade do utilizador.")

    session = requests.Session()

    all_results: List[Bookmaker] = []
    for country in args.countries:
        results = scrape_country(session, country)
        all_results.extend(results)
        time.sleep(args.delay)

    if not all_results:
        log.error("Nenhum resultado extraído em nenhum país. A abortar.")
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

    log.info("Concluído. %d bookmakers extraídos no total (%d países).", len(all_results), len(args.countries))
    log.info("Ficheiro gerado (bruto, por país): %s", args.output)

    # --- versão consolidada: 1 linha por bookmaker único, com lista de países ---
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
                # preenche ratings em falta com dados de outro país, se disponível
                for field_name in ("rating_confianca", "rating_jogos_software", "rating_bonus", "rating_suporte"):
                    if consolidated[key][field_name] is None:
                        consolidated[key][field_name] = getattr(bk, field_name)

        consolidated_rows = []
        for entry in consolidated.values():
            entry = dict(entry)
            entry["num_paises"] = len(entry["paises"])
            entry["paises"] = ",".join(sorted(entry["paises"]))
            consolidated_rows.append(entry)

        # ordena por presença em mais países primeiro (sinal de bookmaker mais internacional)
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

        log.info("Ficheiro consolidado (1 linha/bookmaker, transversal): %s", consolidated_path)
        log.info("Bookmakers únicos: %d", len(consolidated_rows))


if __name__ == "__main__":
    main()
