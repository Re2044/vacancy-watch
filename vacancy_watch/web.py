"""Polite HTTP: per-host spacing with jitter, and a growing per-host pause after HTTP 429."""
from __future__ import annotations

import html
import logging
import random
import re
import time
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from .config import Config

log = logging.getLogger("vacancy_watch")
MAX_COOLDOWN = 4 * 3600


class Cooling(Exception):
    """The host rate-limited us recently; skip it until the cooldown ends."""


class Fetcher:
    def __init__(self, cfg: Config):
        self.delays = {"www.linkedin.com": cfg.linkedin_delay}
        self.default_delay = cfg.request_delay
        self.cooldown = cfg.cooldown
        self.last: dict[str, float] = {}  # host -> time of last request
        self.cool: dict[str, tuple[float, float]] = {}  # host -> (cooldown end, current backoff)
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "Mozilla/5.0"

    def get(self, url: str, **kw) -> requests.Response:
        host = urlsplit(url).hostname
        until, backoff = self.cool.get(host, (0, 0))
        if time.time() < until:
            raise Cooling(f"{host} cooling down for {until - time.time():.0f}s more")
        delay = self.delays.get(host, self.default_delay) * random.uniform(1, 1.5)  # no fixed rhythm
        if (pause := self.last.get(host, 0) + delay - time.time()) > 0:
            time.sleep(pause)
        self.last[host] = time.time()
        r = self.http.get(url, timeout=30, **kw)
        if r.status_code == 429:
            backoff = min(max(backoff * 2, self.cooldown), MAX_COOLDOWN)
            ra = r.headers.get("Retry-After", "")
            self.cool[host] = (time.time() + (int(ra) if ra.isdigit() else backoff), backoff)
            log.info(f"[{host}] 429 Too Many Requests: pausing this host for {self.cool[host][0] - time.time():.0f}s")
            raise Cooling(f"{host} rate-limited")
        if r.ok:
            self.cool.pop(host, None)  # success after a cooldown resets the backoff
        r.raise_for_status()
        return r

    def soup(self, url: str, **kw) -> BeautifulSoup:
        return BeautifulSoup(self.get(url, **kw).text, "html.parser")

    def text_of(self, url: str, css: str) -> str:
        """Text of the first element matching css, or '' if none."""
        el = self.soup(url).select_one(css)
        return el.get_text(" ", strip=True) if el else ""


def clean(text: str, limit: int) -> str:
    """HTML (sometimes double-escaped) -> plain text, whitespace collapsed, cut to limit characters."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(text or "")))).strip()[:limit]
