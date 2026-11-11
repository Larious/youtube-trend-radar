"""The daily scan.

  1. plan       seed queries x sorts for every niche and language, plus 'uploaded today' probes
  2. search     yt-dlp results pages (no API quota), cached 12 h
  3. look up    batched videos.list / channels.list calls (50 IDs each), cached 6 h / 24 h
  4. filter     long-form, not live, not for kids, not junk, in the searched language, <= 60 days old
  5. load       videos and channels (dimensions), today's view snapshot for each (fact)
  6. cluster    title skeletons -> format clusters shared by 2+ small/new breakout channels
  7. probe      search each top cluster's grammar words: copies in 14 days, what they get, old hits
  8. stage      EARLY / MID / ESTABLISHED / CROWDED / SATURATED -> cluster_days
  9. niche      fill rate from today's uploads; the fresh sample from 2-7 days ago re-measured -> niche_days
 10. publish    brief.md, signals.json and CSVs for the day

Everything it outputs is a radar signal for a person to check, not a verdict.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from . import brief, signals, store, text
from .clock import Clock

PROBE_SORTS = ("month_new", "week_views", "all_views")


@dataclass
class Run:
    """What one scan needs from the outside world. Live runs get yt-dlp and HTTPS; the offline replay gets a
    synthetic YouTube behind the same Searcher and YouTubeAPI classes."""
    clock: Clock
    searcher: object   # radar.youtube.Searcher
    api: object        # radar.youtube.YouTubeAPI
    db: sqlite3.Connection
    out_dir: Path


@dataclass
class ScanOptions:
    sorts: str = "week_long:1,week_mid:1,month_long:3"   # sort:k = run that sort on 1/k of the seeds each day
    only_langs: set[str] | None = None
    probes: int = 40                                     # copy probes per day, shared across languages
    extra: list[dict] | None = None                      # extra video IDs to merge in, e.g. from a trending feed
    retry_wait: float = 30
    quiet: bool = False


def load_extra(path: str | Path | None) -> list[dict]:
    """A JSON list of video IDs, or of {id, niche?, lang?, source?} objects."""
    if not path or not Path(path).exists():
        return []
    return [{"id": e} if isinstance(e, str) else e for e in json.loads(Path(path).read_text(encoding="utf-8"))]


def scan(config: dict, run: Run, opts: ScanOptions | None = None) -> dict:
    opts = opts or ScanOptions()
    clock, db = run.clock, run.db
    langs = config["langs"]

    def say(msg: str) -> None:
        if not opts.quiet:
            print(msg, file=sys.stderr, flush=True)

    # 1-2. plan and search
    jobs, meta, probes = plan_searches(config, opts, clock.daynum)
    say(f"[radar] {clock.today}: {len(jobs)} searches across {len(config['niches'])} niches")
    results = run.searcher.many_with_retry(jobs, wait=opts.retry_wait)
    extra = opts.extra or []
    found = attribute(results, meta, extra)

    # 3-4. look up and filter
    tracked = store.recent_breakouts(db, since=clock.days_ago(21))
    V = run.api.videos(list(found) + tracked)
    kept = keep_videos(V, found, clock)
    C = run.api.channels(sorted({v["cid"] for v in kept.values()}))
    enrich(kept, C, langs, db, clock)

    # 5. load the dimensions and today's snapshots
    load(db, kept, C, V, tracked, clock.today)

    # 6-8. format clusters, copy probes, stages
    clusters, lonely = detect_waves(kept, db, clock)
    probe_copies(clusters, run, langs, opts)
    stage_clusters(clusters, db, langs, clock.today)
    joined_today = track_prewave(db, clusters, lonely, clock.today)

    # 9. niche board on the fresh sample
    nrows = niche_board(kept, probes, results, V, run, langs)

    # 10. publish
    summary = brief.write_outputs(run.out_dir, clock, run.searcher, run.api, db, kept, clusters, lonely,
                                  joined_today, nrows, extra)
    say(f"[radar] {summary['breakouts']} breakouts · {summary['clusters']} clusters ({summary['early']} EARLY) · "
        f"{summary['pre_wave']} pre-wave · {summary['niche_rows']} niche rows · API units {summary['api_units']} · "
        f"searches {summary['searches']} ({summary['search_errors']} errors) → {summary['brief']}")
    return summary


# ---------------------------------------------------------------- 1. plan
def plan_searches(config: dict, opts: ScanOptions, daynum: int):
    """(query, sort, depth) jobs, which niche/language each job came from, and the fill-rate probes."""
    langs, niches = config["langs"], config["niches"]
    sorts = [(s.split(":")[0], int(s.split(":")[1])) for s in opts.sorts.split(",")]
    jobs: list[tuple[str, str, int]] = []
    meta: dict[tuple[str, str], tuple[str, str]] = {}
    probes: list[tuple[str, str, str]] = []
    for niche, by_lang in niches.items():
        for lang, seeds in by_lang.items():
            if lang.startswith("_") or lang not in langs or (opts.only_langs and lang not in opts.only_langs):
                continue
            for q in seeds:
                for s, k in sorts:
                    if signals.rotate_in(q, k, daynum):
                        jobs.append((q, s, 20))
                        meta.setdefault((q, s), (niche, lang))
            for q in seeds[:3]:   # the niche's first three seeds also sample today's uploads
                probes.append((niche, lang, q))
                jobs.append((q, "today_new", 20))
                meta.setdefault((q, "today_new"), (niche, lang))
    return jobs, meta, probes


def attribute(results: dict, meta: dict, extra: list[dict]) -> dict[str, collections.Counter]:
    """video id -> how many searches of each (niche, language) returned it."""
    found: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for (q, s, _depth), rows in results.items():
        for r in rows:
            found[r["id"]][meta[(q, s)]] += 1
    for e in extra:
        found[e["id"]][(e.get("niche") or "extra", e.get("lang") or "en")] += 1
    return found


# ---------------------------------------------------------------- 3-5. filter, enrich, load
def keep_videos(V: dict, found: dict, clock: Clock) -> dict[str, dict]:
    """Long-form, not live, not made for kids, not junk, in the searched language and at most 60 days old.
    Each video is assigned to the niche whose searches returned it most (subject niches beat 'formats')."""
    kept = {}
    for vid, votes in found.items():
        v = V.get(vid)
        if not v or v["live"] or v["mfk"] or v["dur"] < signals.MIN_MINUTES * 60 or text.is_junk(v["title"]):
            continue
        subject = [k for k in votes if k[0] != "formats"] or list(votes)
        niche, lang = max(subject, key=lambda k: votes[k])
        if lang != "any" and not text.lang_ok(v["title"], v["lang"], lang):
            continue
        age = clock.age_days(v["published"])
        if age is None or age > signals.MAX_AGE_DAYS:
            continue
        kept[vid] = dict(v, niche=niche, rlang=lang, age=max(age, 0.5), niches=sorted({k[0] for k in votes}))
    return kept


def enrich(kept: dict, C: dict, langs: dict, db: sqlite3.Connection, clock: Clock) -> None:
    """Channel size and age, views/day since upload, views/day since the last snapshot, breakout flag."""
    today = dt.date.fromisoformat(clock.today)
    for v in kept.values():
        ch = C.get(v["cid"]) or {}
        v["subs"], v["born"] = ch.get("subs"), (ch.get("created") or "")[:10]
        bar = langs.get(v["rlang"], langs["en"])
        v.update(signals.classify_video(views=v["views"], age=v["age"], subs=v["subs"],
                                        channel_age=clock.age_days(ch.get("created") or ""), bar=bar))
        p = store.prev_snapshot(db, v["id"], clock.today)
        v["vpd_now"] = (round((v["views"] - p[1]) / max((today - dt.date.fromisoformat(p[0])).days, 1))
                        if p else None)


def load(db: sqlite3.Connection, kept: dict, C: dict, V: dict, tracked: list[str], today: str) -> None:
    for vid in tracked:   # keep recent breakouts measured even when today's searches didn't return them
        v = V.get(vid)
        if v and vid not in kept:
            store.save_snapshot(db, vid, today, v["views"])
    for v in kept.values():
        store.save_snapshot(db, v["id"], today, v["views"])
        store.save_video(db, v, today)
        if v["breakout"]:
            store.mark_breakout(db, v["id"], today)
    for cid, ch in C.items():
        if ch:
            store.save_channel(db, cid, ch, today)
    db.commit()


# ---------------------------------------------------------------- 6-8. waves
def detect_waves(kept: dict, db: sqlite3.Connection, clock: Clock) -> tuple[list[dict], list[dict]]:
    by_lang: dict[str, list[dict]] = collections.defaultdict(list)
    for v in kept.values():
        by_lang[v["rlang"]].append(v)
    clusters: list[dict] = []
    lonely: list[dict] = []
    for lang, vs in by_lang.items():
        cs, ls = signals.cluster_language(vs, lang)
        clusters += cs
        lonely += ls
    week_ago = clock.days_ago(7)
    for cl in clusters:
        first = store.cluster_first_seen(db, cl["key"])
        cl["first_seen"], cl["is_new"] = first or clock.today, first is None
        cl["ch_week_ago"] = store.cluster_channels_on_or_before(db, cl["key"], week_ago)
        cl["format_age"] = round(clock.age_days(cl["first_pub"] + "T00:00:00Z") or 0)
    clusters.sort(key=lambda cl: (-signals.cluster_score(cl), cl["key"]))
    return clusters, lonely


def probe_copies(clusters: list[dict], run: Run, langs: dict, opts: ScanOptions) -> None:
    """For the strongest clusters, search the format's grammar words and measure what in-format uploads get:
    copies in the last 14 days (newest this month + top this week), their median views, and old hits (all time)."""
    clock = run.clock
    quota = collections.Counter({lg: max(2, round(opts.probes * share)) for lg, share in signals.PROBE_SHARE.items()})
    probe_jobs: dict[str, str] = {}
    for cl in clusters:
        q = text.grammar_query(cl["shingle"])
        if q and quota[cl["lang"]] > 0:
            probe_jobs[cl["key"]] = q
            quota[cl["lang"]] -= 1
    for cl in clusters:
        cl.update(copies14=None, copy_med=None, fill=None, old_hits=None, probed=cl["key"] in probe_jobs)
    if not probe_jobs:
        return
    queries = sorted(set(probe_jobs.values()))
    pres = run.searcher.many_with_retry([(q, s, 20) for q in queries for s in PROBE_SORTS], wait=opts.retry_wait)
    PV = run.api.videos(sorted({r["id"] for rows in pres.values() for r in rows}))
    min_secs = signals.MIN_MINUTES * 60
    for cl in clusters:
        q = probe_jobs.get(cl["key"])
        if not q:
            continue
        gram = {w for w in cl["shingle"].split() if w not in ("X", "N")}
        copies = {}
        for r in pres.get((q, "month_new", 20), []) + pres.get((q, "week_views", 20), []):
            v = PV.get(r["id"])
            if not v or v["dur"] < min_secs:
                continue
            age = clock.age_days(v["published"]) or 99
            if age <= 14 and gram <= set(text.tokens(v["title"])):
                copies[v["id"]] = (v["views"], age)
        cl["copies14"] = len(copies)
        aged = [views for views, age in copies.values() if age >= 3]
        cl["copy_med"] = int(statistics.median(aged)) if len(aged) >= 3 else None
        newest = [PV[r["id"]] for r in pres.get((q, "month_new", 20), []) if PV.get(r["id"])]
        cl["fill"], _ = signals.fill_rate(newest, clock.now)
        old = []
        for r in pres.get((q, "all_views", 20), []):
            v = PV.get(r["id"])
            if (v and v["dur"] >= min_secs and gram <= set(text.tokens(v["title"]))
                    and (clock.age_days(v["published"]) or 0) > 60):
                old.append(v)
        bar = langs.get(cl["lang"], langs["en"])
        cl["old_hits"] = sum(1 for v in old if v["views"] >= 10 * bar["min_views"])
        if old:
            cl["format_first"] = min(v["published"] for v in old)[:10]
            cl["format_age"] = round(clock.age_days(cl["format_first"] + "T00:00:00Z") or 0)


def stage_clusters(clusters: list[dict], db: sqlite3.Connection, langs: dict, today: str) -> None:
    for cl in clusters:
        bar = langs.get(cl["lang"], langs["en"])
        cl["growth_wk"] = cl["channels"] - cl["ch_week_ago"] if cl["ch_week_ago"] is not None else None
        cl["stage"] = signals.wave_stage(probed=cl["probed"], copies14=cl["copies14"], copy_med=cl["copy_med"],
                                         old_hits=cl["old_hits"], supply_channels=cl["supply_channels"],
                                         hit_rate=cl["hit_rate"], format_age=cl["format_age"],
                                         channels=cl["channels"], min_views=bar["min_views"])
        store.save_cluster_day(db, cl, today)
    db.commit()


def track_prewave(db: sqlite3.Connection, clusters: list[dict], lonely: list[dict], today: str) -> list[tuple]:
    """Lonely breakouts are remembered; the day one turns up inside a cluster, it got its first clone."""
    in_cluster = {v["id"]: cl for cl in clusters for v in cl["bos"]}
    joined = []
    for vid in store.open_lonely(db):
        if vid in in_cluster:
            store.join_lonely(db, vid, today)
            joined.append((vid, in_cluster[vid]))
    for v in lonely:
        store.save_lonely(db, v, today)
    db.commit()
    return joined


# ---------------------------------------------------------------- 9. niche board
def niche_board(kept: dict, probes: list, results: dict, V: dict, run: Run, langs: dict) -> list[dict]:
    """Store today's 'uploaded today' sample, re-measure the one taken 2-7 days ago, and rank the niches.

    Views-sorted searches only show the winners, so the fresh sample is the unbiased measure of what a new
    upload in a niche gets; it drives the saturation index."""
    clock, db = run.clock, run.db
    fills: dict[tuple[str, str], list] = collections.defaultdict(list)
    for niche, lang, q in probes:
        rows = results.get((q, "today_new", 20), [])
        fills[(niche, lang)].append(signals.fill_rate([V[r["id"]] for r in rows if V.get(r["id"])], clock.now))
        for r in rows:
            v = V.get(r["id"])
            if v and v["dur"] >= signals.MIN_MINUTES * 60 and not v["live"]:
                store.save_fresh(db, niche, lang, r["id"], clock.today)
    sampled = store.fresh_between(db, clock.days_ago(7), clock.days_ago(2))
    FV = run.api.videos([i for _, _, i in sampled]) if sampled else {}
    fresh: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
    for niche, lang, i in sampled:
        if FV.get(i):
            fresh[(niche, lang)].append(FV[i]["views"])
            store.save_snapshot(db, i, clock.today, FV[i]["views"])
    rows = signals.niche_board(kept.values(), fills, fresh, langs)
    for r in rows:
        r["prev_status"], r["prev_nbc"] = store.prev_niche(db, r["niche"], r["lang"], clock.today) or (None, None)
        store.save_niche_day(db, r, clock.today)
    db.commit()
    return rows
