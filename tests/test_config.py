import pytest

from vacancy_watch.config import Config


def test_defaults():
    c = Config.from_env({})
    assert c.ai_url.endswith("/v1") and c.min_score == 70 and c.ats == "rubric"
    assert c.years is None and c.languages is None and c.work_countries == set()
    assert "linkedin" in c.sources


def test_parsing():
    c = Config.from_env({"SOURCES": "remotive, hh", "WORK_COUNTRIES": "de,nl", "YEARS": "3", "ATS": "off",
                         "LANGUAGES": "EN,De", "SEARCH_LOCATIONS": "Berlin, Germany", "MIN_SCORE": "80",
                         "AI_EXTRA_BODY": '{"top_p": 0.9}', "LINKEDIN_DELAY": "2.5"})
    assert c.sources == ["remotive", "hh"] and c.work_countries == {"DE", "NL"}
    assert c.years == 3 and c.ats == "off" and c.languages == ["en", "de"] and c.min_score == 80
    assert c.search_locations == ["Berlin", "Germany"]  # place names keep their case
    assert c.ai_extra_body == {"top_p": 0.9} and c.linkedin_delay == 2.5


def test_bad_ats():
    with pytest.raises(ValueError, match="ATS"):
        Config.from_env({"ATS": "magic"})


def test_bad_number():
    with pytest.raises(ValueError, match="MIN_SCORE"):
        Config.from_env({"MIN_SCORE": "high"})
