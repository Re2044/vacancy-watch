import time

from vacancy_watch.config import Config
from vacancy_watch.llm import parse_json
from vacancy_watch.sources import REGISTRY, Job, JobFeed, linkedin_search, source
from vacancy_watch.web import Cooling, clean

CARD = """<div class="base-card" data-entity-urn="urn:li:jobPosting:42">
  <h3 class="base-search-card__title"> Backend Engineer </h3>
  <h4 class="base-search-card__subtitle">Acme</h4>
  <span class="job-search-card__location">Berlin, Germany</span></div>"""


class FakeFetcher:
    def soup(self, url, **kw):
        from bs4 import BeautifulSoup
        return BeautifulSoup(CARD, "html.parser")

    def text_of(self, url, css):
        return f"description from {url}"


def test_linkedin_parse():
    [job] = linkedin_search(FakeFetcher(), "backend", "Germany")
    assert (job.url, job.title, job.company, job.location) == (
        "https://www.linkedin.com/jobs/view/42", "Backend Engineer", "Acme", "Berlin, Germany")
    assert job.text().endswith("/jobPosting/42")  # description fetched lazily


def test_job_identity():
    a = Job("https://a/1", "Backend Engineer", "Acme", "")
    b = Job("https://b/9", "Backend  engineer", "ACME", "")
    assert a.key == b.key and a.id != b.id and len(a.id) == 12


def test_feed_interleaves_throttles_and_survives_failures():
    calls = []

    @source("_t_a", every=3600)
    def a(cfg, queries):
        calls.append("a")
        yield "a", lambda f: [Job("a1", "", "", ""), Job("a2", "", "", "")]

    @source("_t_b")
    def b(cfg, queries):
        yield "b-ok", lambda f: [Job("b1", "", "", "")]
        yield "b-cool", lambda f: (_ for _ in ()).throw(Cooling("x"))
        yield "b-err", lambda f: 1 / 0

    try:
        last = {}
        feed = JobFeed(Config(sources=["_t_a", "_t_b"]), None, last)
        assert [j.url for j in feed.fetch(["q"])] == ["a1", "b1", "a2"]
        assert last["_t_a"] <= time.time()
        assert [j.url for j in feed.fetch(["q"])] == ["b1"] and calls == ["a"]  # a waits for its interval
    finally:
        REGISTRY.pop("_t_a"), REGISTRY.pop("_t_b")


def test_unknown_source():
    import pytest
    with pytest.raises(ValueError, match="unknown SOURCES"):
        JobFeed(Config(sources=["nope"]), None, {})


def test_clean():
    assert clean("&lt;p&gt;Hello&amp;amp;  <b>world</b>&lt;/p&gt;", 100) == "Hello& world"
    assert clean("x" * 50, 10) == "x" * 10


def test_parse_json_tolerates_fences_and_thinking():
    assert parse_json('<think>hmm</think>\n```json\n{"a": 1}\n```') == {"a": 1}
