"""Command line: vacancy-watch {run,check,replay,label}. Settings come from the environment / .env."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from . import __version__
from .config import Config
from .llm import BOOL, LLM, LLMError, obj
from .notify import make_notifier, telegram
from .profile import build_profile, title_ok
from .scoring import evaluate, make_ats
from .sources import REGISTRY, JobFeed
from .store import Store, setup_logging
from .web import Cooling, Fetcher, clean


def run(cfg: Config, resume: str, once: bool = False) -> None:
    store = Store(cfg.data_dir)
    log = setup_logging(store.log_path)
    llm = LLM(cfg)
    profile = build_profile(cfg, llm, resume)
    ats = make_ats(cfg, llm, profile, resume)
    state = store.load_state()
    feed = JobFeed(cfg, Fetcher(cfg), state.setdefault("last_fetch", {}))
    notifier = make_notifier(cfg)
    seen = store.load_seen()
    while True:
        jobs = feed.fetch(profile.queries)
        store.save_state(state)
        for job in jobs:
            if job.url in seen or job.key in seen or not title_ok(profile, job.title, cfg.exclude_title):
                continue
            seen |= {job.url, job.key}
            try:
                desc = f"Location: {job.location}. " + clean(job.text(), cfg.desc_chars)
                r = evaluate(llm, profile, cfg, job.title, job.company, desc, ats)
            except Cooling:  # description host rate-limited: retry this job next cycle
                seen -= {job.url, job.key}
                continue
            except requests.ConnectionError as e:  # LLM server or site down: pause, retry next cycle
                log.info(f"connection error ({e.request.url if e.request else '?'}), pausing 60s")
                seen -= {job.url, job.key}
                time.sleep(60)
                continue
            except Exception as e:
                log.info(f"error on {job.url}: {type(e).__name__}: {e}")
                seen -= {job.url, job.key}
                continue
            store.add_job({"id": job.id, "url": job.url, "title": job.title, "company": job.company,
                           "loc": job.location, "desc": desc, **r})
            store.save_seen(seen)
            where = f"{job.title} @ {job.company} ({job.location})"
            if r["rejected"]:
                log.info(f"  ✗ {where}: {r['rejected']}  {job.url} #{job.id}")
                continue
            q, ats_r = r["quick"], r["ats"]
            kind = f"quick {q['score']}" + (f", ATS {ats_r['score']}" if ats_r else "")
            log.info(f"{r['score']:>3} [{kind}] {where}  {job.url} #{job.id}")
            if r["score"] >= cfg.min_score:
                notifier.send(f"🎯 {r['score']}/100 ({kind}) — {where}\n{q['level_match']}\n{q['role_match']}\n"
                              + (ats_r["notes"] + "\n" if ats_r else "") + job.url, job.id)
            notifier.poll(store.add_label)
        if once:
            return
        notifier.wait(cfg.interval, store.add_label)


def replay(cfg: Config, resume: str) -> None:
    """Re-score every labelled job with the current prompts and settings, and compare with the labels."""
    store = Store(cfg.data_dir)
    labels = store.labels()
    jobs = {j["id"]: j for j in store.jobs() if j["id"] in labels}
    if not jobs:
        sys.exit("no labelled jobs yet: tap 👍/👎 in Telegram or run `vacancy-watch label JOB_ID good|bad`")
    setup_logging()
    llm = LLM(cfg)
    profile = build_profile(cfg, llm, resume)
    ats = make_ats(cfg, llm, profile, resume)
    n = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for jid, j in jobs.items():
        try:
            r = evaluate(llm, profile, cfg, j["title"], j["company"], j["desc"], ats)
        except Exception as e:
            print(f"error #{jid}: {type(e).__name__}: {e}")
            continue
        hit, good = r["score"] is not None and r["score"] >= cfg.min_score, labels[jid]
        n[("tp" if good else "fp") if hit else ("fn" if good else "tn")] += 1
        now = r["score"] if r["score"] is not None else f"✗ ({r['rejected']})"
        print(f"{'ok ' if hit == good else 'BAD'} {'👍' if good else '👎'} was {j['score']} now {now}  "
              f"{j['title']} @ {j['company']} #{jid}")
    print(f"\nprecision {n['tp']}/{n['tp'] + n['fp']}, recall {n['tp']}/{n['tp'] + n['fn']} "
          f"at MIN_SCORE={cfg.min_score}")


def check(cfg: Config, resume: str | None) -> int:
    """Verify the LLM, Telegram and sources settings; with a resume, also show the derived search plan."""
    setup_logging()
    ok = True
    print(f"LLM: {cfg.ai_url}")
    llm = LLM(cfg)
    try:
        print(f"  model: {llm.model}")
        llm.ask('Reply with {"ok": true}.', obj(ok=BOOL))
        print("  structured output: ok")
    except (requests.RequestException, LLMError) as e:
        ok = False
        print(f"  FAILED: {e}\n  Is the server running? Check AI_URL, AI_MODEL and AI_API_KEY.")

    print("Telegram:")
    if not cfg.tg_token:
        print("  not configured: matches are only printed (see README, 'Telegram notifications')")
    else:
        me = telegram(cfg.tg_token, "getMe")
        if not me.get("ok"):
            ok = False
            print(f"  FAILED: {me.get('description')} (check TG_TOKEN)")
        elif not cfg.tg_chat:
            chats = {str(u["message"]["chat"]["id"]): u["message"]["chat"].get("first_name") or
                     u["message"]["chat"].get("title") for u in telegram(cfg.tg_token, "getUpdates").get("result", [])
                     if "message" in u}
            print(f"  bot @{me['result']['username']} ok, TG_CHAT not set.")
            print("\n".join(f"  found chat: TG_CHAT={c}  ({name})" for c, name in chats.items()) or
                  f"  Send any message to @{me['result']['username']} and run check again to see your chat id.")
        else:
            sent = telegram(cfg.tg_token, "sendMessage", chat_id=cfg.tg_chat, text="✅ vacancy-watch is connected")
            ok &= bool(sent.get("ok"))
            print(f"  bot @{me['result']['username']}: " +
                  ("test message sent" if sent.get("ok") else f"FAILED: {sent.get('description')}"))

    print(f"Sources: {', '.join(cfg.sources)}")
    if unknown := [s for s in cfg.sources if s not in REGISTRY]:
        ok = False
        print(f"  FAILED: unknown {unknown}; available: {', '.join(REGISTRY)}")
    print(f"ATS score: {cfg.ats}" + (f" ({cfg.hiring_agent})" if cfg.ats == "hiring-agent" else ""))
    print(f"Work countries: {', '.join(sorted(cfg.work_countries)) or 'not set (location is not checked)'}")
    print(f"Preferences: {cfg.prefs}" + ("" if cfg.prefs.is_file() else " (missing: copy prefs.example.md)"))
    if resume and ok:
        print("Search plan from the resume:")
        build_profile(cfg, llm, resume)
    print("\nall good" if ok else "\nsome checks failed")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vacancy-watch", description=(
        "Watch job boards for vacancies that fit your resume; score them with an LLM and notify you."))
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--env", type=Path, default=Path(".env"), help="settings file (default: ./.env)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="watch job boards and notify about matches")
    p.add_argument("resume", help="resume file: .pdf, .docx, .txt or .md")
    p.add_argument("--once", action="store_true", help="run one cycle and exit (for cron / systemd timers)")
    p = sub.add_parser("check", help="test the LLM, Telegram and source settings")
    p.add_argument("resume", nargs="?", help="also show the search plan derived from this resume")
    p = sub.add_parser("replay", help="re-score labelled jobs and print precision / recall")
    p.add_argument("resume")
    p = sub.add_parser("label", help="label a job by the #id shown in the log")
    p.add_argument("job_id")
    p.add_argument("verdict", choices=["good", "bad"])
    args = ap.parse_args(argv)

    load_dotenv(args.env)
    try:
        cfg = Config.from_env()
        if args.cmd == "run":
            run(cfg, args.resume, args.once)
        elif args.cmd == "check":
            sys.exit(check(cfg, args.resume))
        elif args.cmd == "replay":
            replay(cfg, args.resume)
        else:
            Store(cfg.data_dir).add_label(args.job_id.lstrip("#"), args.verdict == "good")
    except KeyboardInterrupt:
        pass
    except requests.ConnectionError as e:
        sys.exit(f"cannot connect to {e.request.url if e.request else 'server'}: is the LLM server running? "
                 "Try `vacancy-watch check`.")
    except (LLMError, ValueError, FileNotFoundError) as e:
        sys.exit(f"error: {e}")

