"""Settings, read from environment variables. The CLI loads a .env file from the working directory first."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ATS_SCORERS = ("rubric", "hiring-agent", "off")


def items(value: str) -> list[str]:
    """Split a comma-separated setting, dropping blanks."""
    return [x.strip() for x in value.split(",") if x.strip()]


@dataclass
class Config:
    # LLM: any OpenAI-compatible chat completions API (Ollama, LM Studio, vLLM, llama.cpp, OpenAI, ...)
    ai_url: str = "http://localhost:11434/v1"
    ai_model: str = ""  # empty: the first model the server lists
    ai_api_key: str = ""
    ai_extra_body: dict = field(default_factory=dict)  # extra request fields, e.g. provider-specific options
    ai_timeout: int = 300

    # Scoring
    pre_score: int = 50  # quick fit score needed to run the ATS score
    min_score: int = 70  # final score needed to notify
    ats: str = "rubric"  # second, requirement-by-requirement score: rubric (built in), hiring-agent or off
    hiring_agent: Path | None = None  # hiring-agent checkout, for ats="hiring-agent"

    # Where to look
    sources: list[str] = field(default_factory=lambda: ["remotive", "arbeitnow", "remoteok", "linkedin"])
    search_locations: list[str] = field(default_factory=lambda: ["Worldwide"])  # LinkedIn search locations
    work_countries: set[str] = field(default_factory=set)  # ISO codes you may work in without sponsorship
    hh_host: str = "hh.ru"
    interval: int = 3600  # seconds between cycles

    # Candidate profile overrides; None means "derive from the resume with the LLM"
    years: int | None = None
    languages: list[str] | None = None
    target_levels: list[str] | None = None
    keywords: list[str] | None = None
    role_words: list[str] | None = None
    exclude_keywords: list[str] | None = None
    exclude_title: str = ""  # extra regex; titles matching it are skipped
    years_slack: int = 1  # accept jobs asking up to this many years more than you have
    desc_chars: int = 10000  # description characters sent to the LLM

    # Politeness towards job sites
    request_delay: float = 1.0
    linkedin_delay: float = 5.0
    cooldown: int = 900  # first pause for a host after HTTP 429, doubles up to 4 hours

    # Files and notifications
    data_dir: Path = Path("data")
    prefs: Path = Path("prefs.md")
    tg_token: str = ""
    tg_chat: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        d = cls()

        def s(key, default):
            return (env.get(key) or "").strip() or default

        def lst(key, default=None, case=str.lower):
            return [case(x) for x in items(env[key])] if (env.get(key) or "").strip() else default

        def num(key, default, kind=int):
            try:
                return kind(s(key, default))
            except ValueError:
                raise ValueError(f"{key} must be a number, got {env[key]!r}") from None

        try:
            extra = json.loads(s("AI_EXTRA_BODY", "{}"))
        except ValueError:
            raise ValueError("AI_EXTRA_BODY must be a JSON object") from None
        years = s("YEARS", "")
        ats = s("ATS", d.ats).lower()
        if ats not in ATS_SCORERS:
            raise ValueError(f"ATS must be one of {', '.join(ATS_SCORERS)}, got {ats!r}")
        return cls(
            ai_url=s("AI_URL", d.ai_url), ai_model=s("AI_MODEL", ""), ai_api_key=s("AI_API_KEY", ""),
            ai_extra_body=extra, ai_timeout=num("AI_TIMEOUT", d.ai_timeout),
            pre_score=num("PRE_SCORE", d.pre_score), min_score=num("MIN_SCORE", d.min_score),
            ats=ats, hiring_agent=Path(s("HIRING_AGENT", "")).expanduser().resolve() if s("HIRING_AGENT", "") else None,
            sources=lst("SOURCES", d.sources), search_locations=lst("SEARCH_LOCATIONS", d.search_locations, str),
            work_countries=set(lst("WORK_COUNTRIES", [], str.upper)), hh_host=s("HH_HOST", d.hh_host),
            interval=num("INTERVAL", d.interval),
            years=num("YEARS", 0) if years else None, languages=lst("LANGUAGES"),
            target_levels=lst("TARGET_LEVELS"), keywords=lst("KEYWORDS"), role_words=lst("ROLE_WORDS"),
            exclude_keywords=lst("EXCLUDE_KEYWORDS"), exclude_title=s("EXCLUDE_TITLE", ""),
            years_slack=num("YEARS_SLACK", d.years_slack), desc_chars=num("DESC_CHARS", d.desc_chars),
            request_delay=num("REQUEST_DELAY", d.request_delay, float),
            linkedin_delay=num("LINKEDIN_DELAY", d.linkedin_delay, float), cooldown=num("COOLDOWN", d.cooldown),
            # absolute, because ATS=hiring-agent changes the working directory
            data_dir=Path(s("DATA_DIR", d.data_dir)).resolve(), prefs=Path(s("PREFS", d.prefs)).resolve(),
            tg_token=s("TG_TOKEN", ""), tg_chat=s("TG_CHAT", ""),
        )
