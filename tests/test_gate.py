from vacancy_watch.gate import decide

DESC = ("Location: Berlin. We are hiring a junior backend developer. You need 2+ years of Python. "
        "Fluent German is required. We do not offer visa sponsorship. Fully remote from anywhere in the world.")


def facts(**kw):
    g = {"seniority": "unknown", "seniority_quote": "", "required_languages": [], "min_years": 0,
         "min_years_quote": "", "country": "", "country_quote": "", "remote_any_country_quote": "",
         "relocation_or_visa_quote": "", "citizenship_quote": "", "permit_quote": ""}
    return {**g, **kw}


def test_nothing_stated(profile, cfg):
    assert decide(profile, cfg, "Backend Developer", DESC, facts()) == ""


def test_language(profile, cfg):
    g = facts(required_languages=[{"language": "de", "quote": "Fluent German is required"}])
    assert decide(profile, cfg, "t", DESC, g) == "needs de"


def test_hallucinated_quote_is_ignored(profile, cfg):
    g = facts(required_languages=[{"language": "fr", "quote": "French is mandatory"}], seniority="senior",
              seniority_quote="senior engineer wanted")
    assert decide(profile, cfg, "t", DESC, g) == ""


def test_csharp_is_not_czech(profile, cfg):
    desc = "Strong C# and .NET skills are a must."
    g = facts(required_languages=[{"language": "cs", "quote": "Strong C# and .NET skills are a must"}])
    assert decide(profile, cfg, "t", desc, g) == ""


def test_level_and_years(profile, cfg):
    desc = "Senior engineer. At least five years of experience."
    g = facts(seniority="senior", seniority_quote="Senior engineer", min_years=5,
              min_years_quote="At least five years of experience")
    assert decide(profile, cfg, "t", desc, g) == "level senior, needs 5+ years"


def test_years_within_slack(profile, cfg):
    g = facts(min_years=3, min_years_quote="3+ years of experience")
    assert decide(profile, cfg, "t", "3+ years of experience", g) == ""  # 2 years + slack 1


def test_location_unchecked_without_work_countries(profile, cfg):
    g = facts(country="DE", country_quote="Berlin", permit_quote="You need a German work permit")
    assert decide(profile, cfg, "t", DESC + " You need a German work permit", g) == ""


def test_location(profile, cfg):
    cfg.work_countries = {"NL"}
    g = facts(country="DE", country_quote="Berlin")
    assert decide(profile, cfg, "t", DESC, g).startswith("location DE")
    cfg.work_countries = {"DE"}
    assert decide(profile, cfg, "t", DESC, g) == ""


def test_negated_offer_does_not_count(profile, cfg):
    cfg.work_countries = {"NL"}
    g = facts(country="DE", country_quote="Berlin", relocation_or_visa_quote="We do not offer visa sponsorship")
    assert "location" in decide(profile, cfg, "t", DESC, g)
    g["remote_any_country_quote"] = "Fully remote from anywhere in the world"
    assert decide(profile, cfg, "t", DESC, g) == ""
