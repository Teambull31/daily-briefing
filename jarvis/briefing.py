"""Briefing quotidien : météo (Open-Meteo, gratuit sans clé) + titres RSS, résumés par le LLM local."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .llm import Ollama

# Codes météo WMO -> libellé court
WEATHER_CODES = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant", 51: "bruine légère", 53: "bruine", 55: "bruine forte",
    61: "pluie légère", 63: "pluie", 65: "forte pluie", 71: "neige légère", 73: "neige", 75: "forte neige",
    80: "averses", 81: "averses", 82: "fortes averses", 95: "orages", 96: "orages avec grêle", 99: "orages violents",
}


def parse_feed(xml_text: str, limit: int = 5) -> list[str]:
    """Extrait les titres d'un flux RSS 2.0 ou Atom."""
    root = ET.fromstring(xml_text)
    atom = "{http://www.w3.org/2005/Atom}"
    titles = [item.findtext("title") for item in root.iter("item")]
    if not titles:
        titles = [entry.findtext(f"{atom}title") for entry in root.iter(f"{atom}entry")]
    return [t.strip() for t in titles if t and t.strip()][:limit]


async def fetch_weather(client: httpx.AsyncClient, cfg: Config) -> str:
    r = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": cfg.latitude,
            "longitude": cfg.longitude,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": cfg.timezone,
            "forecast_days": 1,
        },
    )
    r.raise_for_status()
    d = r.json()["daily"]
    desc = WEATHER_CODES.get(d["weather_code"][0], "météo variable")
    return (
        f"{cfg.city} : {desc}, {d['temperature_2m_min'][0]:.0f}° → {d['temperature_2m_max'][0]:.0f}°, "
        f"pluie {d['precipitation_probability_max'][0]}%"
    )


async def fetch_headlines(client: httpx.AsyncClient, feeds: tuple[str, ...]) -> list[str]:
    async def one(url: str) -> list[str]:
        try:
            r = await client.get(url, follow_redirects=True)
            r.raise_for_status()
            return parse_feed(r.text)
        except (httpx.HTTPError, ET.ParseError):
            return []

    results = await asyncio.gather(*(one(u) for u in feeds))
    return [t for titles in results for t in titles]


async def build_briefing(cfg: Config, llm: Ollama | None, agenda: str = "") -> str:
    now = datetime.now(ZoneInfo(cfg.timezone))
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "jarvis-briefing"}) as client:
        try:
            weather = await fetch_weather(client, cfg)
        except (httpx.HTTPError, KeyError, IndexError):
            weather = "Météo indisponible."
        headlines = await fetch_headlines(client, cfg.feeds)

    header = f"☀️ Briefing du {now:%d/%m/%Y}\n\n🌦 {weather}\n"
    if agenda:
        header += f"\n{agenda}\n"
    if not headlines:
        return header + "\n📰 Aucune actualité récupérée."
    raw = "\n".join(f"- {h}" for h in headlines)
    if llm:
        try:
            summary = await llm.ask(
                "Voici les titres de l'actualité du jour. Fais un résumé en 5 puces max, "
                "regroupe les sujets similaires, sans inventer de détails :\n" + raw
            )
            return header + "\n📰 Actualités\n" + summary
        except httpx.HTTPError:
            pass  # Ollama éteint : on renvoie les titres bruts
    return header + "\n📰 Actualités\n" + raw
