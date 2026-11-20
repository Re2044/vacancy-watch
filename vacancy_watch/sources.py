"""Job sources. Each source turns the search queries into named searches; a search fetches a list of Jobs.

Add a source by decorating a generator with @source("name"): it receives the Config and the queries and
yields (label, search) pairs, where search(fetcher) returns a list of Job. Enable it with SOURCES=...,name.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from itertools import zip_longest

from .config import Config
from .web import Cooling, Fetcher

log = logging.getLogger("vacancy_watch")


@dataclass
class Job:
    url: str
    title: str
    company: str
    location: str
    description: str | Callable[[], str] = ""  # a callable fetches the full text only for jobs worth scoring

    @property
    def id(self) -> str:
        return hashlib.sha1(self.url.encode()).hexdigest()[:12]

    @property
    def key(self) -> str:
        """Identifies the same job cross-posted on several boards."""
        return re.sub(r"\W+", "", f"{self.title}|{self.company}".lower())

    def text(self) -> str:
        return self.description() if callable(self.description) else self.description


Search = Callable[[Fetcher], list[Job]]
Planner = Callable[[Config, list[str]], Iterator[tuple[str, Search]]]
REGISTRY: dict[str, tuple[Planner, int]] = {}


def source(name: str, every: int = 0):
    """Register a source; every = minimum seconds between fetches (some APIs ask for few requests a day)."""
    def register(fn: Planner) -> Planner:
        REGISTRY[name] = (fn, every)
        return fn
    return register


# ---------- public JSON APIs: one feed each, not query-specific (the title filter does the matching)

@source("remotive", every=6 * 3600)  # Remotive asks API users to fetch only a few times a day
def remotive(cfg, queries):
    def search(f):
        return [Job(j["url"], j["title"], j["company_name"], j["candidate_required_location"], j["description"])
                for j in f.get("https://remotive.com/api/remote-jobs").json()["jobs"]]
    yield "remotive", search


@source("arbeitnow")
def arbeitnow(cfg, queries):
    def search(f):
        return [Job(j["url"], j["title"], j["company_name"], j["location"], j["description"])
                for j in f.get("https://www.arbeitnow.com/api/job-board-api").json()["data"]]
    yield "arbeitnow", search


@source("remoteok")
def remoteok(cfg, queries):
    def search(f):
        return [Job(j["url"], j["position"], j["company"], j["location"], j["description"])
                for j in f.get("https://remoteok.com/api").json()[1:]]  # item 0 is the legal notice
    yield "remoteok", search


# ---------- HTML search pages: one search per query (and per location for LinkedIn)

@source("linkedin")
def linkedin(cfg, queries):
    for q in queries:
        for loc in cfg.search_locations:
            yield f"linkedin:{q}@{loc}", partial(linkedin_search, q=q, loc=loc)


def linkedin_search(f: Fetcher, q: str, loc: str) -> list[Job]:
    jobs = []
    for c in f.soup("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                    params={"keywords": q, "location": loc, "f_TPR": "r604800"}).select("div.base-card"):
        jid = c["data-entity-urn"].split(":")[-1]
        text = partial(f.text_of, f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}",
                       ".show-more-less-html__markup")
        jobs.append(Job(f"https://www.linkedin.com/jobs/view/{jid}",
                        c.select_one(".base-search-card__title").get_text(strip=True),
                        c.select_one(".base-search-card__subtitle").get_text(strip=True),
                        c.select_one(".job-search-card__location").get_text(strip=True), text))
    return jobs


@source("hh")  # HeadHunter (CIS); set HH_HOST to a regional site (hh.kz, hh.uz, ...) if hh.ru blocks your region
def hh(cfg, queries):
    for q in queries:
        yield f"{cfg.hh_host}:{q}", partial(hh_search, host=cfg.hh_host, q=q)


def hh_search(f: Fetcher, host: str, q: str) -> list[Job]:
    jobs = []
    for a in f.soup(f"https://{host}/search/vacancy", params={"text": q, "order_by": "publication_time"}
                    ).select('a[data-qa="serp-item__title"]'):
        url = a["href"].split("?")[0]

        def near(qa, a=a):  # nearest matching element within the same result card
            return next((x.get_text(" ", strip=True) for p in a.parents
                         if (x := p.select_one(f'[data-qa="vacancy-serp__vacancy-{qa}"]'))), "?")
        jobs.append(Job(url, a.get_text(strip=True), near("employer-text"), near("address"),
                        partial(f.text_of, url, '[data-qa="vacancy-description"]')))
    return jobs


# ----------

class JobFeed:
    """Runs the enabled sources, respecting each source's minimum refresh interval."""

    def __init__(self, cfg: Config, fetcher: Fetcher, last_fetch: dict[str, float]):
        if unknown := [s for s in cfg.sources if s not in REGISTRY]:
            raise ValueError(f"unknown SOURCES {unknown}; available: {', '.join(REGISTRY)}")
        self.cfg, self.fetcher, self.last_fetch = cfg, fetcher, last_fetch  # last_fetch is persisted by the caller

    def fetch(self, queries: list[str]) -> list[Job]:
        lists, cooling = [], []
        for name in self.cfg.sources:
            planner, every = REGISTRY[name]
            if time.time() - self.last_fetch.get(name, 0) < every:
                continue
            for label, search in planner(self.cfg, queries):
                try:
                    lists.append(search(self.fetcher))
                    self.last_fetch[name] = time.time()
                except Cooling:
                    cooling.append(label)
                except Exception as e:
                    log.info(f"[{label}] failed: {type(e).__name__}: {e}")
        if cooling:
            log.info(f"skipped {len(cooling)} searches on rate-limited hosts: {', '.join(cooling)}")
        return [j for row in zip_longest(*lists) for j in row if j]  # interleave so no source starves
