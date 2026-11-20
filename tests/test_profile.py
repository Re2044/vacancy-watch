import pytest

from tests.conftest import FakeLLM
from vacancy_watch.config import Config
from vacancy_watch.profile import build_profile, default_levels, title_ok

PLAN = {"queries": ["backend developer"], "keywords": ["backend", "Python", "developer"],
        "role_words": ["developer", "engineer"], "exclude_keywords": ["qa", "backend"],
        "languages": ["en"], "years": 3}


@pytest.mark.parametrize("title, ok", [
    ("Backend Engineer (Python)", True),
    ("Python-Entwickler Backend", True),
    ("Senior .NET Developer", True),           # level is the gate's job, not the title filter's
    ("Frontend Engineer", False),              # no keyword
    ("Backend QA Engineer", False),            # excluded word
    ("Python Test Automation Engineer", False),  # 'test' excludes 'testing'-family titles
    ("Fastest Backend Engineer", True),        # ...but not words that merely contain it
    ("Backend Developer Intern", False),       # below target level
    ("Python Data Analyst", False),            # no role word
])
def test_title_ok(profile, title, ok):
    assert title_ok(profile, title) is ok


def test_title_ok_intern_target(profile):
    profile.levels |= {"intern"}
    assert title_ok(profile, "Backend Developer Intern")


def test_exclude_title_regex(profile):
    assert not title_ok(profile, "Senior Backend Engineer", exclude_title="senior|lead")


def test_default_levels():
    assert default_levels(0) == ["intern", "trainee", "junior"]
    assert default_levels(2) == ["junior", "middle"]
    assert default_levels(10)[-1] == "principal"


def test_build_profile(tmp_path, cfg):
    resume = tmp_path / "cv.txt"
    resume.write_text("Python backend developer. " * 20)
    p = build_profile(cfg, FakeLLM(PLAN), resume)
    assert p.keywords == ["backend", "python", "backend developer"]  # generic and role words dropped
    assert p.exclude == ["qa"]  # never exclude what we search for
    assert p.levels == {"junior", "middle", "unknown"} and p.years == 3


def test_build_profile_overrides(tmp_path):
    resume = tmp_path / "cv.md"
    resume.write_text("Python backend developer. " * 20)
    cfg = Config(prefs=tmp_path / "none.md", years=9, target_levels=["senior"], languages=["en", "fr"])
    p = build_profile(cfg, FakeLLM(PLAN), resume)
    assert p.years == 9 and p.levels == {"senior", "unknown"} and p.languages == {"en", "fr"}


def test_bad_levels(tmp_path):
    resume = tmp_path / "cv.txt"
    resume.write_text("x " * 200)
    with pytest.raises(ValueError, match="TARGET_LEVELS"):
        build_profile(Config(target_levels=["mid"]), FakeLLM(PLAN), resume)


def test_empty_resume(tmp_path, cfg):
    resume = tmp_path / "cv.txt"
    resume.write_text("")
    with pytest.raises(ValueError, match="no text"):
        build_profile(cfg, FakeLLM(PLAN), resume)
