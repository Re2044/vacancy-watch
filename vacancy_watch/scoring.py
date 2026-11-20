"""Scoring pipeline for one job: eligibility gate -> quick fit score -> ATS score.

The ATS score is the built-in rubric score or hiring-agent (see hiring_agent.py). The final score is the
lower of the quick and ATS scores: ATS only checks that the resume covers the posting's requirements, so
it must not override a "wrong level / wrong role" quick verdict.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from functools import partial
from pathlib import Path

from .config import Config
from .gate import eligible
from .llm import INT, LLM, STR, arr, obj
from .profile import Profile

# ---------- quick fit score: reasoning fields come before the score, so the score follows from them

FIT = obj(level_match=STR, role_match=STR, gaps=STR, score=INT)


def quick_score(llm: LLM, p: Profile, title: str, company: str, desc: str) -> dict:
    return llm.ask(
        "You decide whether a job posting is a good NEXT career step for the candidate. Fill the fields in "
        f"order. level_match: the posting's level vs the candidate's ({p.years} years of professional "
        f"experience, target levels: {', '.join(sorted(p.levels - {'unknown'}))}). role_match: the "
        "posting's job family and main skills vs what the candidate does and wants. gaps: mandatory "
        "requirements the candidate lacks. score: 0-100 on this scale:\n"
        "- 85-100: same job family, skills mostly match, level within target, no hard gaps;\n"
        "- 65-84: good fit with minor gaps (a secondary skill or technology, a different domain);\n"
        "- 40-64: related job with noticeable gaps or partly different skills;\n"
        "- at most 40: different job family (e.g. QA, support or management for a software developer; "
        "sales for a marketer), needs 3+ more years than the candidate has, or a major skills mismatch;\n"
        "- at most 30: BELOW the candidate's target level (internship, trainee or graduate program, "
        "training course). Being overqualified is a bad fit, not a good one.\n"
        "Examples for a backend developer with 2 years of experience and target levels junior, middle:\n"
        "- 'Automated Testing in .NET Trainee' -> 10 (below level, QA family)\n"
        "- 'Senior Backend .NET Engineer, 5+ years' -> 35 (needs 3 more years)\n"
        "- 'Backend Engineer (.NET, AWS), 2+ years' -> 90\n"
        f"\nCANDIDATE PREFERENCES:\n{p.prefs}\n\nRESUME:\n{p.resume}\n\nJOB: {title} at {company}\n{desc}",
        FIT)


# ---------- rubric score: a rubric built from the posting alone, then the resume scored against it

RUBRIC = obj(position_title=STR, categories=arr(obj(name=STR, weight=INT, high=STR, low=STR)))


def make_rubric(llm: LLM, title: str, company: str, desc: str) -> list[dict]:
    spec = llm.ask(
        "You design resume scoring rubrics. From the job posting below produce 4-6 categories covering the "
        "posting's actual requirements (core skills, required experience level, domain, practices). weight: "
        "points, summing to 100 across categories; high/low describe what earns most/few points. The "
        "experience-level category gives few points both when the candidate is clearly below and when "
        "clearly above the posting's level (e.g. an experienced professional for a trainee role). NEVER "
        "create categories for education, degrees, grades, institutions or spoken languages."
        f"\n\nJOB: {title} at {company}\n{desc}", RUBRIC)
    cats = [c for c in spec["categories"] if c["weight"] > 0]
    if not cats:
        raise ValueError("empty rubric")
    for i, c in enumerate(cats):  # unique snake_case keys, used as schema properties
        c["key"] = f"{re.sub(r'[^a-z0-9]+', '_', c['name'].lower()).strip('_')[:40] or 'category'}_{i}"
    return cats


def rubric_score(llm: LLM, p: Profile, title: str, company: str, desc: str) -> tuple[int, str]:
    cats = make_rubric(llm, title, company, desc)
    criteria = "\n".join(f"- {c['key']} ({c['name']}): 10 = {c['high']}; 0 = {c['low']}" for c in cats)
    scale = {"type": "integer", "minimum": 0, "maximum": 10}
    schema = obj(**{c["key"]: obj(evidence=STR, score=scale) for c in cats},
                 strengths=arr(STR), gaps=arr(STR))
    r = llm.ask(
        "You are an expert recruiter scoring a resume against a job's requirements. For each category give "
        "evidence (what the resume demonstrates for it, or 'none') and then a score from 0 to 10. Base "
        "scores only on demonstrated skills and experience; never on name, gender, age, nationality, "
        "location, institution or grades. Then list up to 3 strengths and up to 2 gaps for this job."
        f"\n\nCATEGORIES:\n{criteria}\n\nJOB: {title} at {company}\n{desc}\n\nRESUME:\n{p.resume}", schema)
    total = sum(c["weight"] * min(max(r[c["key"]]["score"], 0), 10) / 10 for c in cats)
    notes = "✅ " + "; ".join(r["strengths"][:3]) + "\n⚠️ " + "; ".join(r["gaps"][:2])
    return round(total * 100 / sum(c["weight"] for c in cats)), notes


# ----------

AtsScorer = Callable[[str, str, str], tuple[int, str]]  # (title, company, description) -> (score, notes)


def make_ats(cfg: Config, llm: LLM, p: Profile, resume: str | Path) -> AtsScorer | None:
    if cfg.ats == "off":
        return None
    if cfg.ats == "hiring-agent":
        from .hiring_agent import HiringAgent
        return HiringAgent(cfg, llm, Path(resume).resolve(), p.queries[0]).score
    return partial(rubric_score, llm, p)


def evaluate(llm: LLM, p: Profile, cfg: Config, title: str, company: str, desc: str,
             ats: AtsScorer | None = None) -> dict:
    why, gate = eligible(llm, p, cfg, title, company, desc)
    r = {"rejected": why, "gate": gate, "quick": None, "ats": None, "score": None}
    if why:
        return r
    r["quick"] = quick_score(llm, p, title, company, desc)
    r["score"] = r["quick"]["score"]
    if ats and r["score"] >= cfg.pre_score:
        score, notes = ats(title, company, desc)
        r["ats"], r["score"] = {"by": cfg.ats, "score": score, "notes": notes}, min(r["score"], score)
    return r
