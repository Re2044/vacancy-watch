"""Candidate profile: resume + preferences -> search queries, title filter and target levels."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .llm import INT, ISO_LANG, LLM, STR, arr, obj

log = logging.getLogger("vacancy_watch")

LEVELS = ["intern", "trainee", "junior", "middle", "senior", "lead", "principal", "unknown"]
# Below-junior titles in several languages; skipped unless intern/trainee are among the target levels
BELOW = re.compile(r"(?<!\w)(intern(ship)?|trainee|apprentice(ship)?|graduate|werkstudent|working student|"
                   r"praktik\w*|practicante|pr[aá]cticas|stagiaire|стаж[её]р\w*|практикант\w*)(?!\w)", re.I)
GENERIC = {"engineer", "developer", "programmer"}  # too broad to count as keywords
RESUME_CHARS = 6000


@dataclass
class Profile:
    resume: str
    prefs: str
    queries: list[str]
    keywords: list[str]
    role_words: list[str]
    exclude: list[str]
    languages: set[str]
    years: int
    levels: set[str]


def read_resume(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"resume not found: {p}")
    if p.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        text = "\n".join(pg.extract_text() or "" for pg in PdfReader(p).pages)
    elif p.suffix.lower() == ".docx":
        from docx import Document
        text = "\n".join(par.text for par in Document(p).paragraphs)
    else:
        text = p.read_text(encoding="utf-8")
    if len(text.strip()) < 200:
        raise ValueError(f"{p}: almost no text found (scanned PDF?). Save the resume as .txt, .md or .docx")
    return text


def default_levels(years: int) -> list[str]:
    return (["intern", "trainee", "junior"] if years < 1 else ["junior", "middle"] if years < 4
            else ["middle", "senior"] if years < 8 else ["senior", "lead", "principal"])


PLAN = obj(queries=arr(STR), keywords=arr(STR), role_words=arr(STR), exclude_keywords=arr(STR),
           languages=arr(ISO_LANG), years=INT)


def build_profile(cfg: Config, llm: LLM, resume_path: str | Path) -> Profile:
    resume = read_resume(resume_path)[:RESUME_CHARS]
    if cfg.prefs.is_file():
        prefs = cfg.prefs.read_text(encoding="utf-8").strip()
    else:
        prefs = "(none given)"
        log.info(f"no preferences file at {cfg.prefs}: copy prefs.example.md there to steer the search")
    plan = llm.ask(
        "From this resume and the candidate's preferences extract: queries - 3 short job-search queries "
        "(job titles); keywords - 10-20 short lowercase words likely to appear in titles of suitable "
        "postings: specialisations and key skills or technologies (e.g. 'backend', 'cloud', '.net', "
        "'payroll'), not generic job nouns; role_words - 3-10 lowercase generic job nouns of the candidate's "
        "job family as they appear in titles, also in the other languages the candidate speaks (e.g. "
        "'engineer', 'developer', 'entwickler' for a developer who speaks German); exclude_keywords - 5-15 "
        "lowercase title words for job families or levels the candidate should NOT get, following the "
        "preferences (e.g. 'qa', 'support', 'sales'); languages - ISO 639-1 codes of spoken languages the "
        "candidate knows at B2 level or higher; years - total years of professional experience in the "
        f"candidate's field (integer).\n\nPREFERENCES:\n{prefs}\n\nRESUME:\n{resume}", PLAN)

    def low(xs):
        return [x.strip().lower() for x in xs if x.strip()]

    years = cfg.years if cfg.years is not None else int(plan["years"])
    role_words = cfg.role_words or low(plan["role_words"])
    generic = set(role_words) | GENERIC  # would match almost any title
    keywords = [k for k in dict.fromkeys(cfg.keywords or low(plan["keywords"] + plan["queries"]))
                if k not in generic]
    keep = set(keywords) | set(role_words)  # never exclude what we search for
    levels = set(cfg.target_levels or default_levels(years)) | {"unknown"}
    if bad := levels - set(LEVELS):
        raise ValueError(f"unknown TARGET_LEVELS {sorted(bad)}; valid: {', '.join(LEVELS[:-1])}")
    p = Profile(resume=resume, prefs=prefs, queries=plan["queries"], keywords=keywords, role_words=role_words,
                exclude=[k for k in cfg.exclude_keywords or low(plan["exclude_keywords"]) if k not in keep],
                languages=set(cfg.languages or low(plan["languages"])), years=years, levels=levels)
    log.info(f"queries: {p.queries} | keywords: {p.keywords} | role words: {p.role_words} | "
             f"exclude: {p.exclude} | languages: {sorted(p.languages)} | years: {p.years} | "
             f"levels: {sorted(p.levels - {'unknown'})}")
    return p


def title_ok(p: Profile, title: str, exclude_title: str = "") -> bool:
    """Cheap pre-filter: a keyword and a role word, no excluded word, not below the target level."""
    t = title.lower()

    def starts(k):  # 'test' hits 'testing', not 'fastest'
        return re.search(rf"(?<!\w){re.escape(k)}", t)
    below = not p.levels & {"intern", "trainee"} and BELOW.search(t)
    return bool(any(k in t for k in p.keywords) and any(w in t for w in p.role_words)
                and not any(starts(k) for k in p.exclude) and not below
                and not (exclude_title and re.search(exclude_title, t, re.I)))
