"""
check_robots.py

Verifica APENAS o robots.txt de vários sites candidatos — não faz
scraping nenhum de conteúdo. Objectivo: descobrir rapidamente quais
os agregadores que permitem crawling automatizado das páginas de
listagem/review, antes de investir tempo a construir o parsing.

Uso:
    python check_robots.py
"""

import urllib.robotparser
from urllib.parse import urljoin

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Candidatos: (nome, base_url, [caminhos a testar])
CANDIDATES = [
    (
        "AskGamblers",
        "https://www.askgamblers.com",
        ["/sports-betting/sportsbook-reviews"],
    ),
    (
        "LCB.org",
        "https://lcb.org",
        ["/", "/sportsbooks", "/casinos"],
    ),
    (
        "List Of All Bookmakers",
        "https://listofallbookmakers.com",
        ["/", "/european-bookmakers"],
    ),
    (
        "Bookmakers Review (BMR)",
        "https://www.bookmakersreview.com",
        ["/", "/sportsbooks/"],
    ),
    (
        "Casino.org",
        "https://www.casino.org",
        ["/"],
    ),
    (
        "Gambling.com",
        "https://www.gambling.com",
        ["/"],
    ),
]


def check_site(name: str, base_url: str, paths: list[str]) -> None:
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urljoin(base_url, "/robots.txt")

    print(f"\n=== {name} ===")
    print(f"robots.txt: {robots_url}")

    try:
        rp.set_url(robots_url)
        rp.read()
    except Exception as e:
        print(f"  [ERRO] Não foi possível ler robots.txt: {e}")
        return

    for path in paths:
        full_url = urljoin(base_url, path)
        try:
            allowed = rp.can_fetch(USER_AGENT, full_url)
        except Exception as e:
            print(f"  [ERRO] a verificar {path}: {e}")
            continue
        status = "PERMITIDO" if allowed else "BLOQUEADO"
        print(f"  {path:35s} -> {status}")

    # Mostra também o crawl-delay se definido, é boa prática respeitá-lo
    try:
        delay = rp.crawl_delay(USER_AGENT)
        if delay:
            print(f"  crawl-delay sugerido: {delay}s")
    except Exception:
        pass


def main():
    print("Verificação de robots.txt — nenhum conteúdo é acedido, só regras.\n")
    for name, base_url, paths in CANDIDATES:
        check_site(name, base_url, paths)
    print("\nConcluído.")


if __name__ == "__main__":
    main()
