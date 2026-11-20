# vacancy-watch

Watches job boards for vacancies that fit **your** resume, scores each one with an LLM, and sends the
good ones to Telegram (or just prints them). Your 👍/👎 taps become labels you can use to measure and
tune the scoring.

It works with any OpenAI-compatible model API. Run it fully locally with Ollama or LM Studio, or point
it at OpenAI, OpenRouter or another hosted provider. Nothing is tied to one profession, country or
seniority: the search plan comes from your resume and a short preferences file you write in plain language.

## How it works

```
resume + prefs.md ──LLM──▶ search plan: queries, title keywords, excluded job families, languages, years, target levels
                                   │
job boards ──▶ title filter ──▶ eligibility gate ──▶ quick fit score ──▶ ATS score ──▶ notify if ≥ MIN_SCORE
               (no LLM, cheap)  (hard requirements)   (level, role, gaps)  (requirement by requirement)
```

1. **Title filter**: the title must contain one of your keywords and a role word (e.g. *backend* + *engineer*),
   no excluded words, and nothing below your target level (e.g. *intern*).
2. **Eligibility gate**: the LLM extracts hard requirements (seniority, mandatory spoken languages,
   minimum years, location / work permit / citizenship), **each with a verbatim quote**. Python then
   makes the decision. A requirement only counts if its quote really appears in the posting, so a
   hallucinated requirement can't reject a job.
3. **Quick fit score** (0–100): is this a good *next* step? It covers level, job family, skills and gaps.
4. **ATS score** (0–100): a rubric is built from the posting alone, then your resume is scored against
   each requirement. This step uses the built-in scorer or, optionally,
   [hiring-agent](#ats-scoring-with-hiring-agent). The final score is the lower of the two scores, so a
   perfect skills match can't hide a wrong level or role.

Every evaluated job is saved to `data/jobs.jsonl`, and every decision is logged with its reason in
`data/watch.log`.

## Quick start

**1. Install** (Python 3.10+):

```bash
git clone https://github.com/Re2044/vacancy-watch && cd vacancy-watch
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

**2. Get an LLM.** Any server with an OpenAI-compatible `/v1/chat/completions` endpoint and JSON-schema
structured output works. The easiest local option is [Ollama](https://ollama.com):

```bash
ollama pull qwen3:8b        # a 7–14B instruct model is a reasonable minimum
```

Hosted APIs work too. Set `AI_URL`, `AI_MODEL` and `AI_API_KEY` (see `.env.example`). Each job
that passes the title filter costs 2–4 LLM calls.

**3. Configure:**

```bash
cp .env.example .env               # defaults work for Ollama; add Telegram later if you like
cp prefs.example.md prefs.md       # describe what you want, in your own words
```

**4. Check and run:**

```bash
vacancy-watch check resume.pdf     # tests the LLM / Telegram, shows the search plan derived from your resume
vacancy-watch run resume.pdf       # runs until Ctrl+C, one cycle per INTERVAL (1 hour)
```

The resume can be `.pdf` (text-based, not scanned), `.docx`, `.txt` or `.md`.

## Configuration

All settings are environment variables, read from `.env` in the working directory (or `--env FILE`).
`.env.example` lists every option with comments. The ones you are most likely to change:

| Setting | Default | What it does |
|---|---|---|
| `AI_URL` / `AI_MODEL` / `AI_API_KEY` | Ollama, first listed model | LLM endpoint |
| `SOURCES` | `remotive,arbeitnow,remoteok,linkedin` | job boards to watch (also: `hh`) |
| `SEARCH_LOCATIONS` | `Worldwide` | LinkedIn search locations, e.g. `Germany,Netherlands` |
| `WORK_COUNTRIES` | *(empty: location not checked)* | ISO codes where you can work without sponsorship; elsewhere a job must be remote-from-anywhere or offer relocation/visa |
| `MIN_SCORE` | `70` | final score needed for a notification |
| `TARGET_LEVELS` | from your years of experience | e.g. `junior,middle` |
| `LANGUAGES`, `YEARS`, `KEYWORDS`, `ROLE_WORDS`, `EXCLUDE_KEYWORDS` | derived from your resume | override the LLM's reading of your resume |
| `ATS` | `rubric` | `rubric` (built in), `hiring-agent` (see below), or `off`: faster on slow hardware, slightly less precise |
| `INTERVAL` | `3600` | seconds between cycles |

**Preferences (`prefs.md`)** steer both the search plan and the scoring: target roles, level, job
families to avoid, domains, work format. Run `vacancy-watch check resume.pdf` after editing it to see
the resulting queries and keywords.

### Examples

- *Junior developer in Germany who needs visa sponsorship elsewhere:*
  `SEARCH_LOCATIONS=Germany,European Union`, `WORK_COUNTRIES=DE`, `LANGUAGES=en,de`
- *Senior data scientist who only wants remote roles:* write that in `prefs.md`; keep `WORK_COUNTRIES`
  empty, or set it to your country so local on-site jobs elsewhere are rejected.
- *Job seeker in Central Asia:* `SOURCES=remotive,arbeitnow,remoteok,linkedin,hh`, `HH_HOST=hh.kz`,
  `WORK_COUNTRIES=KZ,UZ,KG,AM,GE`

## Telegram notifications

1. Create a bot with [@BotFather](https://t.me/BotFather) and put its token in `TG_TOKEN`.
2. Send any message to your bot, then run `vacancy-watch check`. It prints your chat id.
3. Put the id in `TG_CHAT` and run `check` again. You should get a test message.

Matches arrive with **👍 good match / 👎 bad match** buttons. Taps are saved to `data/labels.jsonl` while
`run` is active.

## ATS scoring with hiring-agent

[hiring-agent](https://github.com/interviewstreet/hiring-agent) is an open-source resume scorer.
vacancy-watch can use it for the ATS step instead of the built-in rubric. It parses your PDF resume into
structured form and adds signals from the GitHub profile linked in it. For each job, vacancy-watch then
generates a hiring-agent role from the posting and scores your resume against it. This is closer to how
an automated screener reads your resume, at the cost of a heavier setup.

```bash
git clone https://github.com/interviewstreet/hiring-agent ~/hiring-agent
pip install -e ".[hiring-agent]"     # its dependencies, in vacancy-watch's environment
```

Configure hiring-agent's own model (its `.env` / `providers.json`; `DEFAULT_MODEL` must be listed there),
then in vacancy-watch's `.env`:

```bash
ATS=hiring-agent
HIRING_AGENT=~/hiring-agent
```

Requirements and behaviour:

- The resume must be a PDF.
- The parsed resume and GitHub data are cached in `DATA_DIR` as `<resume>.resume.json` and
  `<resume>.github.json`. Delete them after you update your resume.
- Rubric generation uses vacancy-watch's model; the scoring itself uses hiring-agent's model.
- `vacancy-watch check` shows which ATS scorer is active.

## Tuning with your feedback

```bash
vacancy-watch label 3f2a9c1b7d4e bad    # label a job by the #id shown in the log
vacancy-watch replay resume.pdf         # re-score all labelled jobs with the current settings
```

`replay` prints each labelled job's old and new score and the precision / recall at `MIN_SCORE`. Use it
after you change `prefs.md`, `MIN_SCORE`, the model or the prompts to check whether the change helped.

## Running continuously

- Simplest: run `vacancy-watch run resume.pdf` in `tmux` / `screen`.
- Cron or systemd timer: `vacancy-watch run resume.pdf --once` runs one cycle and exits. Already-seen jobs and
  per-source fetch times are stored in `DATA_DIR`, so repeated runs pick up where they left off. Telegram
  👍/👎 taps are collected only while a run is active.

## Job sources

| Name | Coverage | Notes |
|---|---|---|
| `remotive` | remote jobs, worldwide | public API, fetched at most every 6 hours as its terms ask |
| `arbeitnow` | Europe, mostly Germany | public API |
| `remoteok` | remote jobs, worldwide | public API |
| `linkedin` | everywhere | public guest pages; one search per query × location; rate-limits aggressively |
| `hh` | Russia / CIS | HeadHunter search pages; use `HH_HOST` for regional sites |

Requests to each host are spaced out, and a host that answers *429 Too Many Requests* is paused with
growing backoff. Scraping may still conflict with a site's terms of service. Check them, and keep the
request rate low.

**Adding a source** takes one function in `vacancy_watch/sources.py`:

```python
@source("myboard", every=3600)  # optional minimum seconds between fetches
def myboard(cfg, queries):
    def search(f):  # f.get / f.soup / f.text_of are rate-limited HTTP helpers
        return [Job(j["url"], j["title"], j["company"], j["location"], j["description"])
                for j in f.get("https://example.com/api/jobs").json()]
    yield "myboard", search
```

Then add it to `SOURCES`. If the listing has no description, pass a callable (e.g.
`partial(f.text_of, url, ".description")`). It is fetched only for jobs that pass the title filter.

## Privacy

Your resume, preferences and job data stay on your machine, in `DATA_DIR`. They are sent only to the
LLM endpoint you configure. With a local model, nothing leaves your computer except requests to the
job boards and Telegram.

## Development

```bash
pip install -e ".[dev]"
ruff check . && pytest
```

The tests use a fake LLM and don't need network access.

## License

MIT
