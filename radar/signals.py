"""Radar measures as pure functions: breakouts, format clusters, wave stages, niche status, ranks, fill rates.

Nothing here touches the network or the database, so every rule can be unit-tested on its own.
"""

from __future__ import annotations

import collections
import datetime as dt
import math
import statistics
import zlib
from collections.abc import Callable, Iterable

from . import text
from .clock import age_days

SMALL_SUBS = 50_000   # a "small" channel has fewer subscribers than this
NEW_DAYS = 183        # a "new" channel was created in the last six months
MIN_MINUTES = 6       # long-form only
MAX_AGE_DAYS = 60     # the scan ignores videos older than this
RECENT_DAYS = 30      # breakouts and the niche board look at videos up to 30 days old
# share of the daily copy-probe budget per language
PROBE_SHARE = {"en": 0.5, "es": 0.125, "pt": 0.1, "hi": 0.125, "fr": 0.075, "de": 0.075}
PENDING = "PENDING (fresh sample from day 3)"
STAGES = ("EARLY", "MID", "ESTABLISHED", "CROWDED", "SATURATED", "UNPROBED")
STATUSES = ("BREAKING OUT", "UNSATURATED", "CROWDED-BUT-LIVE", "COMPETITIVE", "SATURATED", PENDING, "COLD")


def rotate_in(seed: str, k: int, daynum: int) -> bool:
    """Run a seed on 1 day in k (stable per seed), so expensive sorts cover every seed over k days."""
    return k <= 1 or zlib.crc32(seed.encode()) % k == daynum % k


def classify_video(*, views: int, age: float, subs: int | None, channel_age: float | None, bar: dict) -> dict:
    """Size, velocity and the breakout flag for one video.

    A breakout is at most 30 days old, comes from a small (<50k subs) or new (<6 months) channel, clears the
    language's views and views/day bar, and has an outlier score (views / max(subs, 1,000)) of at least 1.0.
    """
    new = channel_age is not None and channel_age <= NEW_DAYS
    small = subs is not None and subs < SMALL_SUBS
    vpd = round(views / age)
    outlier = round(views / max(subs or 0, 1000), 1)
    breakout = ((small or new) and age <= RECENT_DAYS and views >= bar["min_views"] and vpd >= bar["min_vpd"]
                and outlier >= 1.0)
    return {"small": small, "new": new, "vpd": vpd, "outlier": outlier, "breakout": breakout}


def cluster_language(videos: list[dict], lang: str) -> tuple[list[dict], list[dict]]:
    """Title-format clusters for one language, plus "lonely" breakouts that match no format yet.

    A cluster is a skeleton 3-gram shared by breakouts from 2+ different channels. Each video needs `title`, `id`,
    `cid`, `published`, `vpd`, `breakout`, `small` and `new`; this function adds `skeleton` to it.
    """
    grammar = text.grammar_words(v["title"] for v in videos)
    by_shingle: dict[str, list[dict]] = collections.defaultdict(list)
    for v in videos:
        v["skeleton"] = " ".join(text.skeleton(v["title"], grammar))
        for g in text.good_shingles(v["skeleton"].split(), lang):
            by_shingle[g].append(v)
    candidates = []
    for g, supply in by_shingle.items():
        bos = [v for v in supply if v["breakout"]]
        channels = {v["cid"] for v in bos}
        if len(channels) < 2:
            continue
        small_supply = [v for v in supply if v["small"] or v["new"]]
        candidates.append(dict(
            key=f"{lang}|{g}", lang=lang, shingle=g, bos=bos, supply=supply, channels=len(channels),
            new_channels=len({v["cid"] for v in bos if v["new"]}),
            supply_channels=len({v["cid"] for v in supply}),
            hit_rate=round(len(bos) / max(len(small_supply), 1), 2),
            med_vpd=int(statistics.median(v["vpd"] for v in bos)),
            first_pub=min(v["published"] for v in supply)[:10],
            last_bo=max(v["published"] for v in bos)[:10]))
    # One wave shows up as several overlapping 3-grams. Keep the strongest and drop any cluster whose
    # breakouts are mostly (60%+) inside one already kept. Ties go to the most specific shingle (most content
    # words, then fewest placeholders), then alphabetical order, so runs are repeatable.
    stop = text.stopwords(lang)

    def specificity(shingle: str) -> tuple[int, int]:
        words = shingle.split()
        return (-sum(w not in ("X", "N") and w not in stop for w in words), sum(w in ("X", "N") for w in words))

    candidates.sort(key=lambda c: (-c["channels"], -c["med_vpd"], *specificity(c["shingle"]), c["shingle"]))
    clusters: list[dict] = []
    taken: list[set[str]] = []
    for cl in candidates:
        ids = {v["id"] for v in cl["bos"]}
        if any(len(ids & t) >= 0.6 * len(ids) for t in taken):
            continue
        taken.append(ids)
        clusters.append(cl)
    shared = {v["id"] for cl in clusters for v in cl["bos"]}
    lonely = [v for v in videos if v["breakout"] and v["new"] and v["small"] and v["id"] not in shared]
    return clusters, lonely


def cluster_score(cl: dict) -> float:
    """Priority for the copy probes: more channels, more new channels, more views/day, and newly seen."""
    return (cl["channels"] * 2 + cl["new_channels"] * 3 + math.log10(max(cl["med_vpd"], 1)) * 2
            + (3 if cl["is_new"] else 0))


def wave_stage(*, probed: bool, copies14: int | None, copy_med: int | None, old_hits: int | None,
               supply_channels: int, hit_rate: float, format_age: int, channels: int, min_views: int) -> str:
    """Where a format sits in its life: EARLY, MID, ESTABLISHED, CROWDED or SATURATED (UNPROBED if not measured).

    copies14  in-format uploads in the last 14 days found by the probe searches
    copy_med  median views of those copies aged 3+ days
    old_hits  in-format videos older than 60 days with 10x the language's views bar
    """
    if not probed:
        return "UNPROBED"
    if (copies14 or 0) >= 12 and (copy_med or 0) < min_views / 5:
        return "SATURATED"   # many recent copies and they get little
    if (old_hits or 0) >= 3 and supply_channels >= 15 and hit_rate < 0.15:
        return "SATURATED"   # an old format, many channels, few small-channel hits
    if (old_hits or 0) >= 3 or format_age > 90:
        return "ESTABLISHED" if hit_rate >= 0.15 else "CROWDED"
    if channels >= 10 or (copies14 or 0) >= 10:
        return "MID"
    return "EARLY"


def rank_pct(values: Iterable[float | None]) -> Callable[[float | None], int | None]:
    """value -> percentile rank 0..100 among `values` (ties share the lower rank; None stays None)."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return lambda v: None
    return lambda v: None if v is None else round(100 * sum(1 for x in xs if x < v) / max(len(xs) - 1, 1))


def saturation_index(fill_rank: int | None, fresh_rank: int | None) -> int:
    """0-100: high means more fresh supply and less for each fresh upload than the other niches. Missing -> 50."""
    f = fill_rank if fill_rank is not None else 50
    n = fresh_rank if fresh_rank is not None else 50
    return round((f + (100 - n)) / 2)


def niche_status(*, top10_vpd: int, min_vpd: int, newest_med: int | None, sat_index: int,
                 breakout_rank: int | None, demand_rank: int | None, new_breakout_channels: int) -> str:
    """A niche's status relative to the other niches in its language on the same day."""
    if top10_vpd < min_vpd:
        return "COLD"
    if newest_med is None:   # the fresh sample has no outcome yet (first two days): don't guess saturation
        return "BREAKING OUT" if (breakout_rank or 0) >= 80 else PENDING
    if (breakout_rank or 0) >= 80 and sat_index < 65:
        return "BREAKING OUT"
    if sat_index >= 65:
        return "SATURATED"
    if sat_index <= 35 and (demand_rank or 0) >= 40:
        return "UNSATURATED"
    return "CROWDED-BUT-LIVE" if new_breakout_channels >= 2 else "COMPETITIVE"


def fill_rate(videos: list[dict], now: dt.datetime) -> tuple[float | None, int | None]:
    """Uploads/day implied by a newest-first results page (n / span of their ages), and the median views of its
    uploads aged 2+ days. Needs 5+ dated videos."""
    ages = sorted(a for a in (age_days(v["published"], now) for v in videos) if a is not None)
    if len(ages) < 5:
        return None, None
    span = max(ages[-1] - ages[0], 0.25)
    rate = round(len(ages) / span, 1)
    old = [v["views"] for v in videos if (age_days(v["published"], now) or 0) >= 2]
    return rate, (int(statistics.median(old)) if len(old) >= 3 else None)


def niche_board(videos: Iterable[dict], fills: dict, fresh: dict, bars: dict) -> list[dict]:
    """One row per (niche, language): demand, small-channel hit rate, supply and a status.

    videos  the scan's kept videos (with niche, rlang, age, vpd, small, new, breakout, cid)
    fills   (niche, lang) -> [(uploads per day, median views)] from the 'uploaded today' probes
    fresh   (niche, lang) -> view counts of the fresh sample taken 2-7 days ago, measured today
    bars    language -> {"min_vpd": .., "min_views": ..}
    """
    groups: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for v in videos:
        if v["age"] <= RECENT_DAYS:
            groups[(v["niche"], v["rlang"])].append(v)
    rows = []
    for (niche, lang), vs in groups.items():
        if len(vs) < 8:
            continue
        bar = bars.get(lang, bars["en"])
        top = sorted((v["vpd"] for v in vs), reverse=True)[:10]
        small = [v for v in vs if v["small"] or v["new"]]
        rates = [f[0] for f in fills.get((niche, lang), []) if f[0] is not None]
        sample = fresh.get((niche, lang), [])
        rows.append(dict(
            niche=niche, lang=lang, videos=len(vs), channels=len({v["cid"] for v in vs}),
            top10_vpd=int(statistics.median(top)), med_vpd=int(statistics.median(v["vpd"] for v in vs)),
            small_n=len(small), small_med_vpd=int(statistics.median(v["vpd"] for v in small)) if small else 0,
            small_hit_rate=round(sum(v["breakout"] for v in small) / max(len(small), 1), 2),
            breakout_channels=len({v["cid"] for v in vs if v["breakout"]}),
            new_breakout_channels=len({v["cid"] for v in vs if v["breakout"] and v["new"]}),
            fill_per_day=round(statistics.median(rates), 1) if rates else None,
            newest_med=int(statistics.median(sample)) if len(sample) >= 5 else None,
            fresh_win=round(sum(x >= 1000 for x in sample) / len(sample), 2) if len(sample) >= 5 else None,
            min_vpd=bar["min_vpd"]))
    for lang in {r["lang"] for r in rows}:
        rs = [r for r in rows if r["lang"] == lang]
        fill_rank = rank_pct(r["fill_per_day"] for r in rs)
        fresh_rank = rank_pct(r["newest_med"] for r in rs)
        demand_rank = rank_pct(r["top10_vpd"] for r in rs)
        breakout_rank = rank_pct(r["new_breakout_channels"] for r in rs)
        for r in rs:
            r["sat_index"] = saturation_index(fill_rank(r["fill_per_day"]), fresh_rank(r["newest_med"]))
            r["status"] = niche_status(top10_vpd=r["top10_vpd"], min_vpd=r["min_vpd"], newest_med=r["newest_med"],
                                       sat_index=r["sat_index"],
                                       breakout_rank=breakout_rank(r["new_breakout_channels"]),
                                       demand_rank=demand_rank(r["top10_vpd"]),
                                       new_breakout_channels=r["new_breakout_channels"])
    return rows
