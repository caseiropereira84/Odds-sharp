"""
askgamblers_scraper_colab.py
Versão para colar diretamente numa célula do Google Colab.

Como usar:
1. Ajusta as variáveis em CONFIG (em baixo) se quiseres.
2. Corre a célula.
3. No final, o ficheiro fica em /content/askgamblers_sportsbooks.csv
   -> descarrega pelo ícone de pasta (📁) na barra lateral esquerda do Colab,
      clicando com o botão direito (ou toque longo) no ficheiro -> Download.
"""

import csv
import logging
import re
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, fields
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from tqdm.notebook import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

# ------------------------------------------------------------------
# CONFIG — ajusta aqui, sem precisar de mexer no resto do código
# ------------------------------------------------------------------
MAX_PAGES = 2          # None para percorrer tudo; começa pequeno para testar
DELAY_SECONDS = 1.5    # segundos entre pedidos HTTP
OUTPUT_FILE = "/content/askgamblers_sportsbooks.csv"
IGNORE_ROBOTS = False  # não mudar para True sem confirmar os ToS do site
# ------------------------------------------------------------------

BASE_URL = "https://www.askgamblers.com"
LISTING_URL = "https://www.askgamblers.com/sports-betting/sportsbook-reviews"
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
log = logging.getLogger("askgamblers_scraper")


@dataclass
class Bookmaker:
    bookmaker_name: str = ""
    rating_global: Optional[float] = None
    num_reviews: Optional[int] = None
    rating_seguranca: Optional[float] = None
    rating_suporte: Optional[float] = None
    rating_bonus: Optional[float] = None
    rating_pagamentos: Optional[float] = None
    url_review: str = ""


def check_robots_allowed(base_url: str, paths: List[str]) -> bool:
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        rp.set_url(robots_url)
        rp.read()
    except Exception as e:
        log.warning("Não foi possível ler robots.txt (%s): %s — a assumir bloqueio.", robots_url, e)
        return False
    ok = True
    for path in paths:
        allowed = rp.can_fetch(USER_AGENT, urljoin(base_url, path))
        log.info("robots.txt: %s -> %s", path, "permitido" if allowed else "BLOQUEADO")
        ok = ok and allowed
    return ok


def fetch_page(session, url, retries=2, timeout=15):
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


def _extract_float(text):
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    return float(m.group(1).replace(",", ".")) if m else None


def _extract_int(text):
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    return int(cleaned) if cleaned else None


def _first_text_matching_label(soup, labels):
    label_pattern = re.compile("|".join(re.escape(l) for l in labels), re.IGNORECASE)
    for text_node in soup.find_all(string=label_pattern):
        container = text_node.parent
        for _ in range(3):
            if container is None:
                break
            rating_el = container.find(class_=re.compile(r"(rating|score|stars|value)", re.IGNORECASE))
            if rating_el and rating_el.get_text(strip=True) and re.search(r"\d", rating_el.get_text(strip=True)):
                return rating_el.get_text(strip=True)
            container = container.parent
    return None


def parse_sportsbook_list(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    urls = set()
    for a in soup.find_all("a", href=True):
        full_url = urljoin(base_url, a["href"])
        parsed = urlparse(full_url)
        if "/sports-betting/sportsbook-reviews/" not in parsed.path:
            continue
        slug = parsed.path.rstrip("/").split("/")[-1]
        if slug in ("sportsbook-reviews", "") or slug.startswith("page-"):
            continue
        urls.add(full_url.split("?")[0])
    return sorted(urls)


def find_next_page_url(html, current_url):
    soup = BeautifulSoup(html, "html.parser")
    next_link = soup.find("a", rel=lambda v: v and "next" in v)
    if next_link and next_link.get("href"):
        return urljoin(current_url, next_link["href"])
    candidate = soup.find("a", attrs={"aria-label": re.compile("next", re.IGNORECASE)})
    if candidate and candidate.get("href"):
        return urljoin(current_url, candidate["href"])
    return None


def build_paginated_url(page_num):
    sep = "&" if "?" in LISTING_URL else "?"
    return f"{LISTING_URL}{sep}page={page_num}"


def discover_review_urls(session, max_pages, delay):
    all_urls = []
    seen_pages = set()
    page_num = 1
    current_url = LISTING_URL

    while True:
        if max_pages is not None and page_num > max_pages:
            log.info("Limite MAX_PAGES=%d atingido.", max_pages)
            break
        if current_url in seen_pages:
            break
        seen_pages.add(current_url)

        log.info("A ler página de listagem %d: %s", page_num, current_url)
        html = fetch_page(session, current_url)
        if html is None:
            break

        page_urls = parse_sportsbook_list(html, current_url)
        if not page_urls:
            log.info("Nenhum bookmaker encontrado nesta página — fim da listagem.")
            break

        new_urls = [u for u in page_urls if u not in all_urls]
        if not new_urls:
            log.info("Sem URLs novas — fim da listagem.")
            break

        all_urls.extend(new_urls)
        log.info("  -> %d novos (total: %d)", len(new_urls), len(all_urls))

        next_url = find_next_page_url(html, current_url)
        page_num += 1
        current_url = next_url or build_paginated_url(page_num)
        time.sleep(delay)

    return all_urls


def parse_sportsbook_review(html, url):
    soup = BeautifulSoup(html, "html.parser")
    bk = Bookmaker(url_review=url)

    title_el = soup.find(["h1", "h2"])
    if title_el:
        bk.bookmaker_name = title_el.get_text(strip=True)

    global_rating_el = soup.find(class_=re.compile(r"(overall.?rating|rating.?value|score)", re.IGNORECASE))
    if global_rating_el:
        bk.rating_global = _extract_float(global_rating_el.get_text(strip=True))
    if bk.rating_global is None:
        m = re.search(r"(\d(?:\.\d)?)\s*(?:/|out of)\s*5", soup.get_text())
        if m:
            bk.rating_global = float(m.group(1))

    reviews_el = soup.find(string=re.compile(r"reviews?", re.IGNORECASE))
    if reviews_el:
        bk.num_reviews = _extract_int(str(reviews_el))

    seguranca_txt = _first_text_matching_label(soup, ["Safety", "Security"])
    bk.rating_seguranca = _extract_float(seguranca_txt) if seguranca_txt else None

    suporte_txt = _first_text_matching_label(soup, ["Customer Support", "Support"])
    bk.rating_suporte = _extract_float(suporte_txt) if suporte_txt else None

    bonus_txt = _first_text_matching_label(soup, ["Bonus", "Welcome Bonus", "Promotions"])
    bk.rating_bonus = _extract_float(bonus_txt) if bonus_txt else None

    pagamentos_txt = _first_text_matching_label(soup, ["Payments", "Banking", "Payout"])
    bk.rating_pagamentos = _extract_float(pagamentos_txt) if pagamentos_txt else None

    return bk


# ------------------------------------------------------------------
# EXECUÇÃO — corre automaticamente quando a célula é executada
# ------------------------------------------------------------------

listing_path = urlparse(LISTING_URL).path
allowed = check_robots_allowed(BASE_URL, [listing_path])
if not allowed and not IGNORE_ROBOTS:
    log.error("robots.txt não permite scraping de %s. A parar.", listing_path)
    raise SystemExit("robots.txt bloqueia — ver mensagem acima")
elif not allowed:
    log.warning("robots.txt bloqueia, mas IGNORE_ROBOTS=True. A prosseguir sob tua responsabilidade.")

session = requests.Session()

log.info("A descobrir bookmakers na listagem...")
review_urls = discover_review_urls(session, MAX_PAGES, DELAY_SECONDS)
log.info("Total de URLs de review encontrados: %d", len(review_urls))

results = []
for url in tqdm(review_urls, desc="A extrair reviews"):
    html = fetch_page(session, url)
    if html is None:
        log.warning("A saltar %s (falha de download).", url)
        continue
    try:
        results.append(parse_sportsbook_review(html, url))
    except Exception as e:
        log.warning("Falha ao parsear %s: %s", url, e)
    time.sleep(DELAY_SECONDS)

if not results:
    log.error("Nenhum resultado extraído. A estrutura da página pode ter mudado.")
else:
    fieldnames = [f.name for f in fields(Bookmaker)]
    try:
        import pandas as pd
        df = pd.DataFrame([vars(r) for r in results])[fieldnames]
        df.to_csv(OUTPUT_FILE, index=False)
    except ImportError:
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in results:
                writer.writerow(vars(r))

    log.info("Concluído. %d bookmakers extraídos.", len(results))
    log.info("Ficheiro gerado: %s", OUTPUT_FILE)

    # Mostra uma pré-visualização direto no notebook
    try:
        import pandas as pd
        display(pd.read_csv(OUTPUT_FILE).head(10))
    except Exception:
        pass
