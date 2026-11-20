"""Optional ATS scorer backed by hiring-agent (https://github.com/interviewstreet/hiring-agent).

hiring-agent parses the PDF resume into JSON Resume form, enriches it with the candidate's GitHub
profile, and scores it against a role. Here the role is generated on the fly from each job posting.

Setup: clone hiring-agent, configure its model (its own .env / providers.json; DEFAULT_MODEL must be a
model listed there), install its dependencies (`pip install -e ".[hiring-agent]"`), then set
ATS=hiring-agent and HIRING_AGENT=/path/to/hiring-agent. hiring-agent loads its prompt templates relative
to the working directory, so this module changes into that directory; all vacancy-watch paths are
absolute by then.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

from .config import Config
from .llm import INT, LLM, STR, arr, obj

log = logging.getLogger("vacancy_watch")
ROLE = obj(position_title=STR, categories=arr(obj(key=STR, label=STR, max=INT, high=STR, low=STR)))


class HiringAgent:
    def __init__(self, cfg: Config, llm: LLM, resume: Path, position: str):
        """Load hiring-agent and parse the resume once (cached in DATA_DIR)."""
        home = cfg.hiring_agent
        if not home or not (home / "evaluator.py").is_file():
            raise ValueError(f"ATS=hiring-agent needs HIRING_AGENT set to a hiring-agent checkout (got {home})")
        if resume.suffix.lower() != ".pdf":
            raise ValueError("ATS=hiring-agent needs a PDF resume")
        self.llm = llm
        before = set(logging.root.manager.loggerDict)
        sys.path.insert(0, str(home))
        os.chdir(home)
        try:
            from models import JSONResume
            from transform import convert_github_data_to_text, convert_json_resume_to_text
        except ImportError as e:
            raise ValueError(f"cannot import hiring-agent ({e}); install its dependencies: "
                             'pip install -e ".[hiring-agent]"') from None
        cache = cfg.data_dir / f"{resume.stem}.resume.json"
        gh_cache = cfg.data_dir / f"{resume.stem}.github.json"
        if cache.exists():
            data = JSONResume(**json.loads(cache.read_text(encoding="utf-8")))
        else:
            log.info("hiring-agent: parsing the resume (once; cached in DATA_DIR)")
            from pdf import PDFHandler
            data = PDFHandler().extract_json_from_pdf(str(resume))
            if data is None:
                raise ValueError("hiring-agent could not parse the resume; check its model settings")
            cache.write_text(json.dumps(data.model_dump(), ensure_ascii=False), encoding="utf-8")
        gh = next((p.url for p in (data.basics and data.basics.profiles or [])
                   if (p.network or "").lower() == "github"), None)
        if gh and not gh_cache.exists():
            from github import fetch_and_display_github_info
            gh_cache.write_text(json.dumps(fetch_and_display_github_info(gh, position_title=position) or {}))
        gh_data = json.loads(gh_cache.read_text()) if gh_cache.exists() else {}
        self.text = convert_json_resume_to_text(data) + (convert_github_data_to_text(gh_data) if gh_data else "")
        for name in set(logging.root.manager.loggerDict) - before:  # it logs every LLM reply at ERROR level
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)

    def make_role(self, title: str, company: str, desc: str):
        from roles import Category, Role
        spec = self.llm.ask(
            "You design ATS scoring rubrics. From the job posting below produce 4-6 categories covering "
            "the posting's actual requirements (core skills, required experience level, domain, "
            "practices). key is snake_case, max values sum to 100, high/low describe what earns top/few "
            "points. The experience-level category gives few points both when the candidate is clearly "
            "below and when clearly above the posting's level (e.g. an experienced professional for a "
            "trainee role). NEVER create categories for education, degrees, grades, institutions or languages."
            f"\n\nJOB: {title} at {company}\n{desc}", ROLE)
        cats = [Category(re.sub(r"\W", "_", c["key"]) + f"_{i}", c["label"], max(1, c["max"]))
                for i, c in enumerate(spec["categories"])]
        if not cats:
            raise ValueError("empty rubric")
        sections = "\n\n".join(f"### {c['label']} (0-{k.max} points)\nHIGH: {c['high']}\nLOW: {c['low']}"
                               for c, k in zip(spec["categories"], cats, strict=True))
        criteria = (f"You are evaluating a resume for: {spec['position_title']} at {company}.\n\n"
                    "## JOB DESCRIPTION\n{% raw %}" + desc + "{% endraw %}\n\n"
                    f"**MANDATORY: fill ALL categories: {', '.join(c.key for c in cats)}** with evidence.\n\n"
                    f"## SCORING CRITERIA\n\n{sections}\n\n"
                    "Bonus points total must be 0. Deductions total must be 0 (hard requirements are "
                    "checked separately).\n\nResume to evaluate:\n\n{{ text_content }}")
        system = (f"You are an expert technical recruiter scoring resumes for {spec['position_title']}. "
                  "You are SCORING, not summarizing. Never let scores depend on name, gender, age, "
                  "nationality, location, institution or grades. Base scores only on demonstrated "
                  f"skills and experience. Category maxima: {', '.join(f'{c.key} {c.max}' for c in cats)}. "
                  "Respond with valid JSON matching the requested schema exactly.")
        return Role(name="auto", position_title=spec["position_title"], categories=cats, bonus_max=0,
                    min_final_score=0, max_final_score=sum(c.max for c in cats),
                    criteria_source=criteria, system_message_source=system)

    def score(self, title: str, company: str, desc: str) -> tuple[int, str]:
        from evaluator import ResumeEvaluator
        from models import build_evaluation_model
        role = self.make_role(title, company, desc)
        err = None
        for _ in range(3):  # malformed JSON from the model
            try:
                ev = ResumeEvaluator(role, build_evaluation_model(role), model_params={"temperature": 0.0,
                                     "top_p": 0.9}).evaluate_resume(self.text)
                break
            except (ValueError, KeyError, TypeError) as e:
                err = e
        else:
            raise err
        got = sum(min(s["score"], s["max"]) for s in ev.scores.model_dump().values())
        notes = "✅ " + "; ".join(ev.key_strengths[:3]) + "\n⚠️ " + "; ".join(ev.areas_for_improvement[:2])
        return round(got * 100 / role.max_final_score), notes  # deductions ignored: the gate covers them
