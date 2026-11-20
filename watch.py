"""Monitor job boards for vacancies matching a resume; send good matches to Telegram.

Pipeline per job: title filter (role word + keyword, no excluded or below-level words) -> strict
eligibility gate (languages, years, seniority level, remote-anywhere / relocation / visa / visa-free
country) -> quick LLM fit score -> hiring-agent ATS score against a role generated on the fly from the
job description; final score = min(quick, ATS). Every evaluated job is saved to jobs.jsonl, 👍/👎 taps
in Telegram go to labels.jsonl, and --replay re-scores the labelled jobs to measure prompt changes.

usage: python watch.py RESUME.(pdf|docx|txt|md)    watch boards (ATS scoring needs a PDF)
       python watch.py --replay RESUME             re-score labelled jobs, print precision/recall
       python watch.py --label JOB_ID good|bad     label a job by the #id shown in watch.log
env (or .env file): TG_TOKEN, TG_CHAT, [AI_URL, PRE_SCORE, MIN_SCORE, INTERVAL, LOCATIONS, VISA_OK,
                    HIRING_AGENT, LANGUAGES="en,de", YEARS=5, YEARS_SLACK, TARGET_LEVELS="junior,middle",
                    EXCLUDE_TITLE=regex, KEYWORDS, ROLE_WORDS, EXCLUDE_KEYWORDS (comma lists),
                    PREFS=prefs.md, DESC_CHARS, HH_HOST=hh.kz, LINKEDIN_DELAY=5, REQUEST_DELAY=1, COOLDOWN=900]
"""
import functools, hashlib, html, json, logging, os, random, re, sys, time
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlsplit
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))
AI_URL = os.getenv("AI_URL", "http://localhost:8000/v1")
PRE_SCORE = int(os.getenv("PRE_SCORE", 50))   # quick LLM score needed to run ATS scoring
MIN_SCORE = int(os.getenv("MIN_SCORE", 70))   # final score needed to notify
INTERVAL = int(os.getenv("INTERVAL", 3600))
LOCATIONS = os.getenv("LOCATIONS", "Europe,Kazakhstan,Uzbekistan,Kyrgyzstan,Armenia,Tbilisi,Turkey").split(",")
VISA_OK = set(os.getenv("VISA_OK", "KZ,UZ,RU,KG,TJ,BY,AM,GE,TR").split(","))  # can work there without sponsorship
HA = Path(os.getenv("HIRING_AGENT", Path.home() / "Projects/hiring-agent"))
HERE = Path(__file__).parent
SEEN, LOG = HERE / "seen.json", HERE / "watch.log"
JOBS, LABELS = HERE / "jobs.jsonl", HERE / "labels.jsonl"
PREFS = Path(os.getenv("PREFS", HERE / "prefs.md"))
YEARS_SLACK = int(os.getenv("YEARS_SLACK", 1))  # accept jobs asking up to this many years more
DESC_CHARS = int(os.getenv("DESC_CHARS", 10000))  # requirements often sit at the end of a posting
EXCLUDE_TITLE = os.getenv("EXCLUDE_TITLE", "")  # extra title regex to skip, e.g. "senior|lead|principal"
LEVELS = ["intern", "trainee", "junior", "middle", "senior", "lead", "principal", "unknown"]
BELOW = re.compile(r"(?<!\w)(intern(ship)?|trainee|apprentice(ship)?|graduate|werkstudent|working student|"
                   r"praktik\w*|practicante|pr[aá]cticas|stagiaire|стаж[её]р\w*|практикант\w*)(?!\w)", re.I)
NUM = "zero one two three four five six seven eight nine ten".split()
HH_HOST = os.getenv("HH_HOST", "hh.kz")  # hh.ru answers 451 to this region; regional sites list the same vacancies
DELAY = {"www.linkedin.com": float(os.getenv("LINKEDIN_DELAY", 5))}  # min seconds between requests per host
DEFAULT_DELAY = float(os.getenv("REQUEST_DELAY", 1))
COOLDOWN = int(os.getenv("COOLDOWN", 900))  # first pause for a host after a 429; doubles up to MAX_COOLDOWN
MAX_COOLDOWN = 4 * 3600
UA = {"User-Agent": "Mozilla/5.0"}
STR, INT, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}


def arr(t): return {"type": "array", "items": t}
def obj(**p): return {"type": "object", "properties": p, "required": list(p)}  # fields generated in this order
ISO = {"type": "string", "pattern": "^[a-z]{2}$"}  # ISO 639-1 language code


def log(*a):
    line = " ".join(map(str, a))
    print(line)
    with LOG.open("a") as f:
        f.write(time.strftime("%F %T ") + line + "\n")


def retry(fn, n=3):
    for _ in range(n):
        try:
            return fn()
        except (ValueError, KeyError, TypeError) as e:  # bad/invalid JSON from the model
            err = e
    raise err


def read_resume(p):
    p = Path(p)
    if p.suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(pg.extract_text() or "" for pg in PdfReader(p).pages)
    if p.suffix == ".docx":
        from docx import Document
        return "\n".join(par.text for par in Document(p).paragraphs)
    return p.read_text()


@functools.cache
def model():
    return requests.get(f"{AI_URL}/models").json()["data"][0]["id"]


def ask(prompt, schema):
    body = {"model": model(), "temperature": 0, "messages": [{"role": "user", "content": prompt}],
            "chat_template_kwargs": {"enable_thinking": False},  # ignored under json_schema anyway
            "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": schema}}}
    return retry(lambda: json.loads(requests.post(f"{AI_URL}/chat/completions", json=body, timeout=300)
                                    .json()["choices"][0]["message"]["content"]))


def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(s or ""))))[:DESC_CHARS]


class Cooling(Exception):
    """The host rate-limited us recently; skip it until the cooldown ends."""


_last, _cool = {}, {}  # host -> time of last request / (cooldown end, current backoff)


def get(url, **kw):
    host = urlsplit(url).hostname
    until, backoff = _cool.get(host, (0, 0))
    if time.time() < until:
        raise Cooling(f"{host} cooling down for {until - time.time():.0f}s more")
    if (pause := _last.get(host, 0) + DELAY.get(host, DEFAULT_DELAY) * random.uniform(1, 1.5) - time.time()) > 0:
        time.sleep(pause)  # jittered spacing so requests don't arrive in a fixed rhythm
    _last[host] = time.time()
    r = requests.get(url, headers=UA, timeout=30, **kw)
    if r.status_code == 429:
        backoff = min(max(backoff * 2, COOLDOWN), MAX_COOLDOWN)
        ra = r.headers.get("Retry-After", "")
        _cool[host] = (time.time() + (int(ra) if ra.isdigit() else backoff), backoff)
        log(f"[{host}] 429 Too Many Requests: pausing this host for {_cool[host][0] - time.time():.0f}s")
        raise Cooling(f"{host} rate-limited")
    if r.ok:
        _cool.pop(host, None)  # success after a cooldown resets the backoff
    r.raise_for_status()
    return r


def soup(url, **kw):
    return BeautifulSoup(get(url, **kw).text, "html.parser")


def text_of(url, css):
    return (soup(url).select_one(css) or BeautifulSoup("", "html.parser")).get_text(" ", strip=True)


def job_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


# ---------- sources: each yields (url, title, company, location, description | callable -> description)

def linkedin(q, loc):
    for c in soup("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                  params={"keywords": q, "location": loc, "f_TPR": "r604800"}).select("div.base-card"):
        jid = c["data-entity-urn"].split(":")[-1]
        yield (f"https://www.linkedin.com/jobs/view/{jid}",
               c.select_one(".base-search-card__title").get_text(strip=True),
               c.select_one(".base-search-card__subtitle").get_text(strip=True),
               c.select_one(".job-search-card__location").get_text(strip=True),
               lambda jid=jid: text_of(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}",
                                       ".show-more-less-html__markup"))


def hh(q):
    for a in soup(f"https://{HH_HOST}/search/vacancy", params={"text": q, "order_by": "publication_time"}
                  ).select('a[data-qa="serp-item__title"]'):
        url = a["href"].split("?")[0]
        near = lambda qa: next((x.get_text(" ", strip=True) for p in a.parents  # nearest in same card
                                if (x := p.select_one(f'[data-qa="vacancy-serp__vacancy-{qa}"]'))), "?")
        yield (url, a.get_text(strip=True), near("employer-text"), near("address"),
               lambda u=url: text_of(u, '[data-qa="vacancy-description"]'))


def fetch_jobs(queries):
    sources = {
        "remotive": lambda: [(j["url"], j["title"], j["company_name"], j["candidate_required_location"], j["description"])
                             for j in get("https://remotive.com/api/remote-jobs").json()["jobs"]],
        "arbeitnow": lambda: [(j["url"], j["title"], j["company_name"], j["location"], j["description"])
                              for j in get("https://www.arbeitnow.com/api/job-board-api").json()["data"]],
        "remoteok": lambda: [(j["url"], j["position"], j["company"], j["location"], j["description"])
                             for j in get("https://remoteok.com/api").json()[1:]],
        **{f"linkedin:{q}@{l}": lambda q=q, l=l: list(linkedin(q, l)) for q in queries for l in LOCATIONS},
        **{f"{HH_HOST}:{q}": lambda q=q: list(hh(q)) for q in queries},
    }
    lists, cooling = [], []
    for name, fn in sources.items():
        try:
            lists.append(fn())
        except Cooling:
            cooling.append(name)
        except Exception as e:
            log(f"[{name}] failed: {e}")
    if cooling:
        log(f"skipped {len(cooling)} searches on rate-limited hosts: {', '.join(cooling)}")
    return [j for row in zip_longest(*lists) for j in row if j]  # interleave so no source starves


# ---------- candidate profile: resume + preferences -> search plan, title filter, target levels

PLAN = obj(queries=arr(STR), keywords=arr(STR), role_words=arr(STR), exclude_keywords=arr(STR),
           languages=arr(ISO), years=INT)


def default_levels(years):
    return ("intern,trainee,junior" if years < 1 else "junior,middle" if years < 4
            else "middle,senior" if years < 8 else "senior,lead,principal")


def setup(resume_path):
    resume = read_resume(resume_path)[:6000]
    prefs = PREFS.read_text().strip() if PREFS.exists() else "(none given)"
    plan = ask(
        "From this resume and the candidate's preferences extract: queries - 3 short job-search queries "
        "(job titles); keywords - 10-20 short lowercase words likely to appear in titles of suitable "
        "postings: specialisations and key technologies (e.g. 'backend', 'software', 'cloud', '.net'), not "
        "generic job nouns; role_words - 3-10 lowercase generic job nouns of the candidate's job family "
        "as they appear in titles, including Russian and German forms (e.g. 'engineer', 'developer', "
        "'разработчик', 'entwickler'); exclude_keywords - 5-15 lowercase title words for job families or "
        "levels the candidate should NOT get, following the preferences (e.g. 'qa', 'test', 'support', "
        "'sales', 'manager'); languages - ISO 639-1 codes of spoken languages the candidate knows at B2 "
        "level or higher; years - total years of professional software experience (integer)."
        f"\n\nPREFERENCES:\n{prefs}\n\nRESUME:\n{resume}", PLAN)
    lst = lambda env, default: [k.strip().lower() for k in (os.getenv(env) or ",".join(default)).split(",") if k.strip()]
    years = int(os.getenv("YEARS") or plan["years"])
    ctx = dict(resume=resume, prefs=prefs, queries=plan["queries"], years=years,
               keywords=lst("KEYWORDS", plan["keywords"] + plan["queries"]),
               role_words=lst("ROLE_WORDS", plan["role_words"]),
               languages=set(lst("LANGUAGES", plan["languages"])),
               levels=set(lst("TARGET_LEVELS", default_levels(years).split(","))) | {"unknown"})
    generic = set(ctx["role_words"]) | {"engineer", "developer", "programmer"}  # would match any title
    ctx["keywords"] = [k for k in dict.fromkeys(ctx["keywords"]) if k not in generic]
    keep = set(ctx["keywords"]) | set(ctx["role_words"])  # never exclude what we search for
    ctx["exclude"] = [k for k in lst("EXCLUDE_KEYWORDS", plan["exclude_keywords"]) if k not in keep]
    log("queries:", ctx["queries"], "| keywords:", ctx["keywords"], "| role words:", ctx["role_words"],
        "| exclude:", ctx["exclude"], "| languages:", ctx["languages"], "| years:", years,
        "| levels:", sorted(ctx["levels"]))
    if bad := ctx["levels"] - set(LEVELS):
        log(f"warning: unknown TARGET_LEVELS {bad}; valid: {LEVELS}")
    ctx["ats_text"] = load_hiring_agent(resume_path, plan["queries"][0]) if str(resume_path).endswith(".pdf") else None
    return ctx


def title_ok(ctx, title):
    t = title.lower()
    starts = lambda k: re.search(rf"(?<!\w){re.escape(k)}", t)  # 'test' hits 'testing', not 'fastest'
    below = not ctx["levels"] & {"intern", "trainee"} and BELOW.search(t)
    return (any(k in t for k in ctx["keywords"]) and any(w in t for w in ctx["role_words"])
            and not any(starts(k) for k in ctx["exclude"]) and not below
            and not (EXCLUDE_TITLE and re.search(EXCLUDE_TITLE, t, re.I)))


# ---------- strict eligibility gate (facts extracted by LLM, decision made in Python)

QUOTE = "exact sentence copied verbatim from the posting, or empty string if the posting does not say it"
GATE = obj(
    seniority={"type": "string", "enum": LEVELS}, seniority_quote=STR,
    required_languages=arr(obj(language=ISO, quote=STR)),
    min_years=INT, min_years_quote=STR,
    country={"type": "string", "pattern": "^([A-Z]{2})?$"}, country_quote=STR,
    remote_any_country_quote=STR, relocation_or_visa_quote=STR, citizenship_quote=STR, permit_quote=STR)


def eligible(ctx, title, company, desc):
    """Return (why the job is out of reach or '', extracted facts). Facts count only if quoted verbatim."""
    g = ask("Extract hiring requirements from this job posting. Each *quote field must be an " + QUOTE +
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
    norm = lambda t: re.sub(r"\W+", " ", t).lower().strip()
    src = norm(f"{title} {desc}")
    said = lambda q, n=6: len(norm(q)) >= n and norm(q)[:60] in src  # reject hallucinated facts
    offered = lambda q: said(q) and not re.search(  # "does not sponsor" is not an offer
        r"\b(not|no|unable|cannot|can't|without|unfortunately)\b|\bне\b|\bнет\b", q.lower())
    stated = lambda n, q: str(n) in q or (n < len(NUM) and re.search(rf"\b{NUM[n]}\b", q.lower()))
    why = [f"needs {l['language']}" for l in g["required_languages"]
           if said(l["quote"]) and l["language"] not in ctx["languages"]
           and not re.search(r"#|\+\+|\.net", l["quote"], re.I)]  # C# -> "cs" is not Czech
    if g["seniority"] not in ctx["levels"] and said(g["seniority_quote"], 3):
        why.append(f"level {g['seniority']}")
    if said(g["min_years_quote"]) and stated(g["min_years"], g["min_years_quote"]) \
            and g["min_years"] > ctx["years"] + YEARS_SLACK:
        why.append(f"needs {g['min_years']}+ years")
    visa_free = said(g["country_quote"], 3) and g["country"] in VISA_OK  # short city names: Минск, Баку
    if said(g["citizenship_quote"]):
        why.append("needs citizenship/clearance")
    if said(g["permit_quote"]) and not visa_free:
        why.append("needs local work permit")
    if not (visa_free or offered(g["remote_any_country_quote"]) or offered(g["relocation_or_visa_quote"])):
        why.append(f"location {g['country'] or '?'}: not visa-free, remote-anywhere or relocation/visa")
    return ", ".join(why), g


# ---------- quick fit score: reasoning fields come before the score, so the score follows from them

FIT = obj(level_match=STR, role_match=STR, gaps=STR, score=INT)


def quick_score(ctx, title, company, desc):
    return ask(
        "You decide whether a job posting is a good NEXT career step for the candidate. Fill the fields in "
        f"order. level_match: the posting's level vs the candidate's ({ctx['years']} years of professional "
        f"experience, target levels: {', '.join(sorted(ctx['levels'] - {'unknown'}))}). role_match: the "
        "posting's job family and main stack vs what the candidate does and wants. gaps: mandatory "
        "requirements the candidate lacks. score: 0-100 on this scale:\n"
        "- 85-100: same job family, stack mostly matches, level within target, no hard gaps;\n"
        "- 65-84: good fit with minor gaps (a secondary technology, a different domain);\n"
        "- 40-64: related job with noticeable gaps or a partly different stack;\n"
        "- at most 40: different job family (QA/testing, support, data, ops or management for a developer), "
        "needs 3+ more years than the candidate has, or a major stack mismatch;\n"
        "- at most 30: BELOW the candidate's target level (internship, trainee or graduate program, "
        "training course). Being overqualified is a bad fit, not a good one.\n"
        "Examples for a backend developer with 2 years of experience and target levels junior, middle:\n"
        "- 'Automated Testing in .NET Trainee' -> 10 (below level, QA family)\n"
        "- 'Senior Backend .NET Engineer, 5+ years' -> 35 (needs 3 more years)\n"
        "- 'Backend Engineer (.NET, AWS), 2+ years' -> 90\n"
        f"\nCANDIDATE PREFERENCES:\n{ctx['prefs']}\n\nRESUME:\n{ctx['resume']}\n\nJOB: {title} at {company}\n{desc}",
        FIT)


# ---------- hiring-agent ATS score against a role generated from the job description

def load_hiring_agent(pdf, position):
    """Parse the resume (and its GitHub profile) once with hiring-agent; return evaluation text."""
    pdf = Path(pdf).resolve()
    sys.path.insert(0, str(HA))
    os.chdir(HA)  # hiring-agent loads its prompt templates relative to cwd
    logging.disable(logging.CRITICAL)  # hiring-agent logs every LLM response at ERROR level
    from models import JSONResume
    from transform import convert_json_resume_to_text, convert_github_data_to_text
    cache, gh_cache = HERE / f"{pdf.stem}.resume.json", HERE / f"{pdf.stem}.github.json"
    if cache.exists():
        data = JSONResume(**json.loads(cache.read_text()))
    else:
        from pdf import PDFHandler
        data = PDFHandler().extract_json_from_pdf(str(pdf))
        cache.write_text(json.dumps(data.model_dump(), ensure_ascii=False))
    gh = next((p.url for p in (data.basics and data.basics.profiles or [])
               if (p.network or "").lower() == "github"), None)
    if gh and not gh_cache.exists():
        from github import fetch_and_display_github_info
        gh_cache.write_text(json.dumps(fetch_and_display_github_info(gh, position_title=position) or {}))
    gh_data = json.loads(gh_cache.read_text()) if gh_cache.exists() else {}
    return convert_json_resume_to_text(data) + (convert_github_data_to_text(gh_data) if gh_data else "")


ROLE = obj(position_title=STR, categories=arr(obj(key=STR, label=STR, max=INT, high=STR, low=STR)))


def make_role(title, company, desc):
    from roles import Role, Category
    spec = ask(
        "You design ATS scoring rubrics. From the job posting below produce 4-6 categories covering "
        "the posting's actual requirements (technical skills, required experience level, domain, "
        "practices). key is snake_case, max values sum to 100, high/low describe what earns top/few "
        "points. The experience-level category gives few points both when the candidate is clearly below "
        "and when clearly above the posting's level (e.g. an experienced engineer for a trainee role). "
        "NEVER create categories for education, degrees, grades, institutions or languages."
        f"\n\nJOB: {title} at {company}\n{desc}", ROLE)
    cats = [Category(re.sub(r"\W", "_", c["key"]), c["label"], max(1, c["max"])) for c in spec["categories"]]
    sections = "\n\n".join(f"### {c['label']} (0-{k.max} points)\nHIGH: {c['high']}\nLOW: {c['low']}"
                           for c, k in zip(spec["categories"], cats))
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


def ats_score(resume_text, title, company, desc):
    from models import build_evaluation_model
    from evaluator import ResumeEvaluator
    role = make_role(title, company, desc)
    params = {"temperature": 0.0, "top_p": 0.9}  # repeatable scores
    ev = retry(lambda: ResumeEvaluator(role, build_evaluation_model(role), model_params=params)
               .evaluate_resume(resume_text))
    got = sum(min(s["score"], s["max"]) for s in ev.scores.model_dump().values())
    total = got * 100 / role.max_final_score  # deductions ignored: the gate handles hard requirements
    return round(total), "✅ " + "; ".join(ev.key_strengths[:3]) + "\n⚠️ " + "; ".join(ev.areas_for_improvement[:2])


def evaluate(ctx, title, company, desc):
    """Gate -> quick score -> ATS. Final score is the lower of quick and ATS: ATS only checks that the
    resume covers the requirements, so it must not override a 'wrong level / wrong role' quick verdict."""
    why, gate = eligible(ctx, title, company, desc)
    r = {"rejected": why, "gate": gate, "quick": None, "ats": None, "score": None}
    if why:
        return r
    r["quick"] = quick_score(ctx, title, company, desc)
    r["score"] = r["quick"]["score"]
    if ctx["ats_text"] and r["score"] >= PRE_SCORE:
        ats, notes = ats_score(ctx["ats_text"], title, company, desc)
        r["ats"], r["score"] = {"score": ats, "notes": notes}, min(r["score"], ats)
    return r


# ---------- Telegram: notifications with 👍/👎 buttons, labels collected via getUpdates

def tg(method, **kw):
    return requests.post(f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/{method}", json=kw,
                         timeout=kw.get("timeout", 0) + 30).json()


def notify(text, jid):
    tg("sendMessage", chat_id=os.environ["TG_CHAT"], text=text[:4000],
       reply_markup={"inline_keyboard": [[{"text": "👍 good match", "callback_data": f"{jid}:1"},
                                          {"text": "👎 bad match", "callback_data": f"{jid}:0"}]]})


def add_label(jid, good):
    with LABELS.open("a") as f:  # last label per id wins
        f.write(json.dumps({"id": jid, "good": good, "ts": time.strftime("%F %T")}) + "\n")


_offset = 0


def poll_labels(timeout=0):
    """Save 👍/👎 taps. Returns False if Telegram is unreachable."""
    global _offset
    try:
        ups = tg("getUpdates", offset=_offset, timeout=timeout, allowed_updates=["callback_query"])
        for u in ups.get("result", []):
            _offset = u["update_id"] + 1
            q = u.get("callback_query") or {}
            chat = str(q.get("message", {}).get("chat", {}).get("id"))
            if chat == os.environ["TG_CHAT"] and re.fullmatch(r"[0-9a-f]{12}:[01]", q.get("data", "")):
                jid, good = q["data"].split(":")
                add_label(jid, good == "1")
                tg("answerCallbackQuery", callback_query_id=q["id"], text="saved 👍" if good == "1" else "saved 👎")
        return "result" in ups
    except (requests.RequestException, ValueError):
        return False


def wait(seconds):
    end = time.time() + seconds
    while (left := end - time.time()) > 0:
        if not poll_labels(int(min(50, left))):
            time.sleep(min(60, left))


# ----------

def main(resume_path):
    ctx = setup(resume_path)
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    while True:
        for url, title, company, loc, desc in fetch_jobs(ctx["queries"]):
            key = re.sub(r"\W+", "", f"{title}|{company}".lower())  # same job cross-posted elsewhere
            if url in seen or key in seen or not title_ok(ctx, title):
                continue
            seen |= {url, key}
            jid = job_id(url)
            try:
                desc = f"Location: {loc}. " + clean(desc() if callable(desc) else desc)
                r = evaluate(ctx, title, company, desc)
            except Cooling:  # description host rate-limited: retry this job next cycle
                seen -= {url, key}; continue
            except requests.ConnectionError as e:  # AI server / site down: pause, retry next cycle
                log(f"connection error ({e.request.url if e.request else '?'}), pausing 60s")
                seen -= {url, key}; time.sleep(60); continue
            except Exception as e:
                log(f"error on {url}: {type(e).__name__}: {e}"); seen -= {url, key}; continue
            with JOBS.open("a") as f:
                f.write(json.dumps({"id": jid, "ts": time.strftime("%F %T"), "url": url, "title": title,
                                    "company": company, "loc": loc, "desc": desc, **r}, ensure_ascii=False) + "\n")
            SEEN.write_text(json.dumps(sorted(seen)))
            if r["rejected"]:
                log(f"  ✗ {title} @ {company} ({loc}): {r['rejected']}  {url} #{jid}")
                continue
            q, ats = r["quick"], r["ats"]
            kind = f"quick {q['score']}" + (f", ATS {ats['score']}" if ats else "")
            log(f"{r['score']:>3} [{kind}] {title} @ {company} ({loc})  {url} #{jid}")
            if r["score"] >= MIN_SCORE:
                notify(f"🎯 {r['score']}/100 ({kind}) — {title} @ {company} ({loc})\n"
                       f"{q['level_match']}\n{q['role_match']}\n" + (ats["notes"] + "\n" if ats else "") + url, jid)
            poll_labels()
        wait(INTERVAL)


def replay(resume_path):
    """Re-score every labelled job with the current prompts and compare with the labels."""
    rows = lambda p: [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
    labels = {l["id"]: l["good"] for l in rows(LABELS)}
    jobs = {r["id"]: r for r in rows(JOBS) if r["id"] in labels}
    if not jobs:
        sys.exit("no labelled jobs yet: tap 👍/👎 in Telegram or run --label JOB_ID good|bad")
    ctx = setup(resume_path)
    n = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for jid, j in jobs.items():
        try:
            r = evaluate(ctx, j["title"], j["company"], j["desc"])
        except Exception as e:
            print(f"error #{jid}: {type(e).__name__}: {e}"); continue
        hit, good = r["score"] is not None and r["score"] >= MIN_SCORE, labels[jid]
        n[("tp" if good else "fp") if hit else ("fn" if good else "tn")] += 1
        now = r["score"] if r["score"] is not None else f"✗ ({r['rejected']})"
        print(f"{'ok ' if hit == good else 'BAD'} {'👍' if good else '👎'} was {j['score']} now {now}  "
              f"{j['title']} @ {j['company']} #{jid}")
    print(f"\nprecision {n['tp']}/{n['tp'] + n['fp']}, recall {n['tp']}/{n['tp'] + n['fn']} at MIN_SCORE={MIN_SCORE}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--replay"]:
        replay(args[1])
    elif args[:1] == ["--label"]:
        add_label(args[1].lstrip("#"), args[2].lower() in ("good", "1", "y", "yes", "👍"))
    else:
        main(args[0])
