"""Recherche web gratuite et privée via SearXNG (auto-hébergé), résumée par le LLM local avec sources."""

from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser

import httpx

from .llm import Ollama

WEB_PROMPT = """Réponds à la question en t'appuyant UNIQUEMENT sur les sources ci-dessous.
Cite les sources utilisées avec leur numéro entre crochets, par exemple [1].
Si les sources ne suffisent pas, dis-le franchement. Réponds en français, de façon concise.

Question : {question}

Sources :
{sources}"""


# Pages de protection anti-robots : leur texte n'apporte rien (et pourrait induire le LLM en erreur).
BLOCKED_MARKERS = (
    "not a bot", "access denied", "captcha", "enable javascript", "activez javascript",
    "robot policy", "cloudflare", "checking your browser", "vérification",
)


def looks_blocked(text: str) -> bool:
    head = text[:500].lower()
    return len(text) < 200 or any(marker in head for marker in BLOCKED_MARKERS)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "svg", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str, max_chars: int = 3000) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # HTML très cassé : on garde ce qui a été lu
        pass
    return re.sub(r"\s+", " ", " ".join(parser.parts))[:max_chars]


async def search(client: httpx.AsyncClient, searxng_url: str, query: str, limit: int = 5) -> list[dict]:
    r = await client.get(
        f"{searxng_url.rstrip('/')}/search",
        params={"q": query, "format": "json", "language": "fr"},
    )
    r.raise_for_status()
    results = []
    for item in r.json().get("results", []):
        if item.get("url") and item.get("title"):
            results.append({"title": item["title"], "url": item["url"], "content": item.get("content", "")})
        if len(results) >= limit:
            break
    return results


async def _page_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, follow_redirects=True)
    except httpx.HTTPError:
        return ""
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return ""
    text = html_to_text(r.text)
    return "" if looks_blocked(text) else text


async def web_answer(llm: Ollama, searxng_url: str, question: str, read_pages: int = 3) -> str:
    """Cherche, lit les meilleures pages, et fait répondre le LLM en citant ses sources."""
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) jarvis-assistant"}
    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        try:
            results = await search(client, searxng_url, question)
        except (httpx.HTTPError, ValueError) as exc:
            return f"🔎 Recherche impossible ({exc.__class__.__name__}). SearXNG est-il lancé ?"
        if not results:
            return "🔎 Aucun résultat trouvé."
        pages = await asyncio.gather(*(_page_text(client, r["url"]) for r in results[:read_pages]))

    blocks = []
    for i, res in enumerate(results, 1):
        page = pages[i - 1] if i <= len(pages) else ""
        body = "\n".join(part for part in (res["content"], page) if part)  # extrait du moteur + page lue
        blocks.append(f"[{i}] {res['title']} ({res['url']})\n{body or '(pas de texte)'}")
    try:
        answer = await llm.chat(
            [{"role": "user", "content": WEB_PROMPT.format(question=question, sources="\n\n".join(blocks))}]
        )
    except httpx.HTTPError:
        answer = "(IA locale indisponible, voici les résultats bruts)"
    links = "\n".join(f"[{i}] {r['url']}" for i, r in enumerate(results, 1))
    return f"{answer}\n\n🔗 Sources :\n{links}"
