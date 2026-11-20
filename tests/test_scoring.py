from functools import partial

from tests.conftest import FakeLLM
from vacancy_watch.scoring import evaluate, make_ats, rubric_score

GATE_OK = {"seniority": "unknown", "seniority_quote": "", "required_languages": [], "min_years": 0,
           "min_years_quote": "", "country": "", "country_quote": "", "remote_any_country_quote": "",
           "relocation_or_visa_quote": "", "citizenship_quote": "", "permit_quote": ""}
RUBRIC = {"position_title": "Backend Engineer", "categories": [
    {"name": "Python", "weight": 60, "high": "h", "low": "l"},
    {"name": "Cloud / AWS", "weight": 40, "high": "h", "low": "l"}]}


def quick(score):
    return {"level_match": "fits", "role_match": "same family", "gaps": "none", "score": score}


def scores(schema):
    keys = [k for k in schema["properties"] if k not in ("strengths", "gaps")]
    assert keys == ["python_0", "cloud_aws_1"]
    return {"python_0": {"evidence": "e", "score": 10}, "cloud_aws_1": {"evidence": "none", "score": 5},
            "strengths": ["Python"], "gaps": ["AWS"]}


def test_rubric_lowers_score(profile, cfg):
    llm = FakeLLM(GATE_OK, quick(90), RUBRIC, scores)
    r = evaluate(llm, profile, cfg, "Backend Engineer", "Acme", "desc", make_ats(cfg, llm, profile, "cv.pdf"))
    assert r["quick"]["score"] == 90 and r["ats"]["score"] == 80 and r["score"] == 80
    assert r["ats"]["by"] == "rubric" and "Python" in r["ats"]["notes"]


def test_low_quick_score_skips_rubric(profile, cfg):
    llm = FakeLLM(GATE_OK, quick(30))
    r = evaluate(llm, profile, cfg, "t", "c", "d", partial(rubric_score, llm, profile))
    assert r["score"] == 30 and r["ats"] is None


def test_ats_off(profile, cfg):
    cfg.ats = "off"
    llm = FakeLLM(GATE_OK, quick(95))
    r = evaluate(llm, profile, cfg, "t", "c", "d", make_ats(cfg, llm, profile, "cv.pdf"))
    assert r["score"] == 95 and r["ats"] is None


def test_hiring_agent_needs_checkout(profile, cfg):
    import pytest
    cfg.ats = "hiring-agent"
    with pytest.raises(ValueError, match="HIRING_AGENT"):
        make_ats(cfg, FakeLLM(), profile, "cv.pdf")


def test_rejected_job_is_not_scored(profile, cfg):
    gate = {**GATE_OK, "citizenship_quote": "US citizenship required"}
    r = evaluate(FakeLLM(gate), profile, cfg, "t", "c", "US citizenship required")
    assert r["rejected"] == "needs citizenship/clearance" and r["score"] is None
