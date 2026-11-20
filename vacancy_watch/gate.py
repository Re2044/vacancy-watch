"""Strict eligibility gate: the LLM extracts facts with verbatim quotes, Python makes the decision.

A fact only counts if its quote really occurs in the posting, so a hallucinated requirement cannot
reject a job.
"""
from __future__ import annotations

import re

from .config import Config
from .llm import INT, ISO_LANG, LLM, STR, arr, obj
from .profile import LEVELS, Profile

QUOTE = "exact sentence copied verbatim from the posting, or empty string if the posting does not say it"
GATE = obj(
    seniority={"type": "string", "enum": LEVELS}, seniority_quote=STR,
    required_languages=arr(obj(language=ISO_LANG, quote=STR)),
    min_years=INT, min_years_quote=STR,
    country={"type": "string", "pattern": "^([A-Z]{2})?$"}, country_quote=STR,
    remote_any_country_quote=STR, relocation_or_visa_quote=STR, citizenship_quote=STR, permit_quote=STR)
NUM = "zero one two three four five six seven eight nine ten".split()
NEGATION = re.compile(r"\b(not|no|unable|cannot|can't|without|unfortunately)\b|\bне\b|\bнет\b")


def extract_facts(llm: LLM, title: str, company: str, desc: str) -> dict:
    return llm.ask(
        "Extract hiring requirements from this job posting. Each *quote field must be an " + QUOTE +
        " (the title counts as part of the posting). seniority: level of the position as stated in the "
        "title or text - intern, trainee (also graduate/apprentice/training programs), junior, middle "
        "(also mid, regular, intermediate), senior, lead (team/tech lead), principal (also staff, "
        "architect), or unknown if not stated; seniority_quote: the words stating it. "
        "required_languages: spoken human languages (ISO 639-1 code, e.g. 'de') that are "
        "MANDATORY - not programming languages, not 'nice to have'. min_years: minimum years of "
        "professional experience required (0 if not stated). remote_any_country_quote: states the job "
        "is fully remote for candidates in ANY country (not 'remote within EU/US/<country>'). "
        "relocation_or_visa_quote: states relocation support or visa sponsorship is offered. "
        "country: ISO 3166-1 alpha-2 code of the single country where the job is based or to which "
        "remote work is restricted ('' if worldwide, multi-country or unknown), country_quote: the text "
        "naming that place (city or country). citizenship_quote: requires a specific citizenship or "
        "security clearance. permit_quote: requires an existing work permit/right to work or residence "
        f"in a specific country/region.\n\nJOB: {title} at {company}\n{desc}", GATE)


def _norm(t: str) -> str:
    return re.sub(r"\W+", " ", t).lower().strip()


def decide(p: Profile, cfg: Config, title: str, desc: str, g: dict) -> str:
    """Why the job is out of reach, or '' if it is not."""
    src = _norm(f"{title} {desc}")

    def said(q, n=6):  # the quote really is in the posting (short minimum for city names like Баку)
        return len(_norm(q)) >= n and _norm(q)[:60] in src

    def offered(q):  # "we do not sponsor visas" is not an offer
        return said(q) and not NEGATION.search(q.lower())

    def stated(n, q):  # the number really appears in the quote
        return str(n) in q or (0 <= n < len(NUM) and re.search(rf"\b{NUM[n]}\b", q.lower()))

    why = [f"needs {lang['language']}" for lang in g["required_languages"]
           if said(lang["quote"]) and lang["language"] not in p.languages
           and not re.search(r"#|\+\+|\.net", lang["quote"], re.I)]  # C# -> "cs" is not Czech
    if g["seniority"] not in p.levels and said(g["seniority_quote"], 3):
        why.append(f"level {g['seniority']}")
    if said(g["min_years_quote"]) and stated(g["min_years"], g["min_years_quote"]) \
            and g["min_years"] > p.years + cfg.years_slack:
        why.append(f"needs {g['min_years']}+ years")
    if said(g["citizenship_quote"]):
        why.append("needs citizenship/clearance")
    if cfg.work_countries:  # location checks only when the user said where they can work
        can_work = said(g["country_quote"], 3) and g["country"] in cfg.work_countries
        if said(g["permit_quote"]) and not can_work:
            why.append("needs local work permit")
        if not (can_work or offered(g["remote_any_country_quote"]) or offered(g["relocation_or_visa_quote"])):
            why.append(f"location {g['country'] or '?'}: not in WORK_COUNTRIES, remote-anywhere or relocation/visa")
    return ", ".join(why)


def eligible(llm: LLM, p: Profile, cfg: Config, title: str, company: str, desc: str) -> tuple[str, dict]:
    facts = extract_facts(llm, title, company, desc)
    return decide(p, cfg, title, desc, facts), facts
