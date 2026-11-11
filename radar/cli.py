"""Command line: python -m radar <command>

  scan      the live daily scan (needs yt-dlp and a YouTube Data API key)
  report    the day's radar rebuilt from the database              (offline)
  history   day-by-day metrics of a format cluster                 (offline)
  stats     row counts and the date range                          (offline)
  bias      the sampling-bias check: views-sorted vs fresh sample  (offline)
  demo      replay the whole pipeline on a synthetic YouTube       (offline)

Offline commands default to data/radar.sqlite, or to the bundled sample database when that doesn't exist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "niches.json"
DEFAULT_DB = Path("data/radar.sqlite")
SAMPLE_DB = ROOT / "sample" / "radar_sample.sqlite"
DEMO_DB, DEMO_OUT = Path("data/demo/radar.sqlite"), Path("data/demo/runs")


def load_config(path: str | Path) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return {"langs": d["langs"], "niches": {k: v for k, v in d["niches"].items() if not k.startswith("_")}}


def resolve_db(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    if DEFAULT_DB.exists():
        return DEFAULT_DB
    print(f"(no {DEFAULT_DB} yet: using the bundled sample database)", file=sys.stderr)
    return SAMPLE_DB


def cmd_scan(a: argparse.Namespace) -> None:
    from . import store
    from .clock import Clock
    from .scan import Run, ScanOptions, load_extra, scan
    from .youtube import QuotaExceeded, Searcher, TTLCache, YouTubeAPI, load_api_key

    key = load_api_key()
    cache = TTLCache(a.cache, read=not a.no_cache)
    db = store.connect(a.db)
    run = Run(clock=Clock.system(), searcher=Searcher(cache, a.workers), api=YouTubeAPI(key, cache), db=db,
              out_dir=Path(a.out))
    opts = ScanOptions(sorts=a.sorts, only_langs=set(a.only_langs.split(",")) if a.only_langs else None,
                       probes=a.probes, extra=load_extra(a.extra))
    try:
        scan(load_config(a.config), run, opts)
    except QuotaExceeded as e:
        raise SystemExit(f"stopped: {e}") from e
    finally:
        db.close()


def cmd_report(a: argparse.Namespace) -> None:
    from . import report, store
    print(report.daily(store.connect_readonly(resolve_db(a.db)), a.day), end="")


def cmd_history(a: argparse.Namespace) -> None:
    from . import report, store
    print(report.history(store.connect_readonly(resolve_db(a.db)), " ".join(a.words)), end="")


def cmd_stats(a: argparse.Namespace) -> None:
    from . import report, store
    db = resolve_db(a.db)
    print(f"# {os.path.relpath(db)}\n\n" + report.stats(store.connect_readonly(db)), end="")


def cmd_bias(a: argparse.Namespace) -> None:
    from . import report, store
    bars = load_config(a.config)["langs"]
    result = report.sampling_bias(store.connect_readonly(resolve_db(a.db)), bars, a.min_age, a.max_age, a.min_group)
    print(json.dumps(result, indent=1) if a.json else report.format_bias(result), end="" if not a.json else "\n")


def cmd_demo(a: argparse.Namespace) -> None:
    from .offline import replay
    db, out = Path(a.db), Path(a.out)
    if db.exists():
        if not (a.overwrite or db.resolve() == DEMO_DB.resolve()):
            raise SystemExit(f"{db} already exists: pass --overwrite to replace it, or choose another --db")
        db.unlink()
    summaries = replay(db, out, days=a.days, seed=a.seed, quiet=a.quiet)
    print("| Day | Kept videos | Breakouts | Clusters (EARLY) | Pre-wave | Niche rows | API units | Searches |")
    print("|---|---|---|---|---|---|---|---|")
    for s in summaries:
        print(f"| {s['day']} | {s['kept']} | {s['breakouts']} | {s['clusters']} ({s['early']}) | {s['pre_wave']} | "
              f"{s['niche_rows']} | {s['api_units']} | {s['searches']} |")
    print(f"\nDatabase: {db}\nLatest brief: {out / 'LATEST.md'}\nNext: python -m radar report --db {db}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m radar", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="run today's live scan")
    s.add_argument("--config", default=str(DEFAULT_CONFIG), help="niches, seeds and breakout bars (JSON)")
    s.add_argument("--db", default=str(DEFAULT_DB))
    s.add_argument("--out", default="data/runs", help="where each day's brief and CSVs go")
    s.add_argument("--cache", default="data/cache.sqlite")
    s.add_argument("--no-cache", action="store_true", help="ignore cached searches and stats (still writes them)")
    s.add_argument("--extra", help="JSON list of extra video IDs (or {id, niche, lang} objects) to merge in")
    s.add_argument("--sorts", default="week_long:1,week_mid:1,month_long:3",
                   help="sort:k pairs; each sort runs on 1/k of the seeds per day")
    s.add_argument("--only-langs", help="comma-separated languages, e.g. en,es")
    s.add_argument("--probes", type=int, default=40, help="copy probes per day across languages")
    s.add_argument("--workers", type=int, default=6, help="parallel searches")
    s.set_defaults(fn=cmd_scan)

    r = sub.add_parser("report", help="the day's radar rebuilt from the database")
    r.add_argument("--db")
    r.add_argument("--day", help="YYYY-MM-DD (default: the latest day)")
    r.set_defaults(fn=cmd_report)

    h = sub.add_parser("history", help="a format cluster's day-by-day metrics")
    h.add_argument("--db")
    h.add_argument("words", nargs="+", help="words of the cluster skeleton, e.g. was banned")
    h.set_defaults(fn=cmd_history)

    t = sub.add_parser("stats", help="row counts and date range")
    t.add_argument("--db")
    t.set_defaults(fn=cmd_stats)

    b = sub.add_parser("bias", help="views-sorted vs fresh-sample hit rates for small channels")
    b.add_argument("--db")
    b.add_argument("--config", default=str(DEFAULT_CONFIG))
    b.add_argument("--min-age", type=int, default=2)
    b.add_argument("--max-age", type=int, default=7)
    b.add_argument("--min-group", type=int, default=20, help="smallest niche x language group to rate")
    b.add_argument("--json", action="store_true")
    b.set_defaults(fn=cmd_bias)

    d = sub.add_parser("demo", help="replay the full pipeline on a synthetic YouTube (no network, no API key)")
    d.add_argument("--db", default=str(DEMO_DB))
    d.add_argument("--out", default=str(DEMO_OUT))
    d.add_argument("--days", type=int, default=5)
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--overwrite", action="store_true", help="replace an existing --db")
    d.add_argument("--quiet", action="store_true")
    d.set_defaults(fn=cmd_demo)

    a = ap.parse_args(argv)
    a.fn(a)
