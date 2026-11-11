"""The two network edges: YouTube results pages via yt-dlp, and the YouTube Data API v3.

Search goes through yt-dlp's results-page extractor, so it uses no API quota. Statistics come from the Data API
in batches of 50 IDs, one quota unit per call. Both sit behind one SQLite TTL cache, and every API call is counted
per endpoint so each run reports the quota it used.

Both classes take their transport as a parameter (`fetch`, `transport`). Live runs use yt-dlp and HTTPS; tests
and the offline demo plug in a synthetic YouTube (radar.offline) and exercise the same caching, batching and
quota code.
"""

from __future__ import annotations

import collections
import concurrent.futures as cf
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

UA = "youtube-trend-radar/1.0 (market research script; python-urllib)"
SEARCH_TTL_HOURS, VIDEO_TTL_HOURS, CHANNEL_TTL_HOURS = 12, 6, 24
BATCH = 50   # IDs per videos.list / channels.list call

# Results-page filters: name -> (YouTube's `sp=` value, double-encoded as in its own URLs;
# upload window in days or None; length filter; order). Tested against youtube.com in Oct-Nov 2026.
SEARCH_FILTERS: dict[str, tuple[str, int | None, str | None, str]] = {
    "today_new":   ("CAISBAgCEAE%253D",           1,    None,   "date"),   # the only truly fresh sample
    "week_new":    ("CAISBAgDEAE%253D",           7,    None,   "date"),
    "month_new":   ("CAISBAgEEAE%253D",           30,   None,   "date"),
    "new":         ("CAI%253D",                   None, None,   "date"),
    "week_views":  ("CAMSBAgDEAE%253D",           7,    None,   "views"),
    "month_views": ("CAMSBAgEEAE%253D",           30,   None,   "views"),
    "year_views":  ("CAMSBAgFEAE%253D",           365,  None,   "views"),
    "all_views":   ("CAM%253D",                   None, None,   "views"),
    "week_mid":    ("CAMSBggDEAEYAw%253D%253D",   7,    "mid",  "views"),  # 4-20 min
    "week_long":   ("CAMSBggDEAEYAg%253D%253D",   7,    "long", "views"),  # 20+ min
    "month_mid":   ("CAMSBggEEAEYAw%253D%253D",   30,   "mid",  "views"),
    "month_long":  ("CAMSBggEEAEYAg%253D%253D",   30,   "long", "views"),
    "year_mid":    ("CAMSBggFEAEYAw%253D%253D",   365,  "mid",  "views"),
    "year_long":   ("CAMSBggFEAEYAg%253D%253D",   365,  "long", "views"),
}

Rows = list[dict]
SearchFetch = Callable[[str, str, int], "tuple[Rows | None, str]"]


def iso_dur(s: str | None) -> int:
    """ISO-8601 duration ('PT1H2M3S') -> seconds."""
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def load_api_key(env_file: str | Path = ".env") -> str:
    """YOUTUBE_API_KEY from the environment, else from a .env file in the working directory."""
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if key:
        return key
    try:
        for line in open(env_file, encoding="utf-8"):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith("YOUTUBE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    raise SystemExit("YOUTUBE_API_KEY is not set: export it or put it in .env (see .env.example)")


# ---------------------------------------------------------------- cache
class TTLCache:
    """Key -> JSON value in SQLite with its write time. Each read says how old a value it accepts.

    One table serves every source: searches (s|...), video stats (v|id) and channel stats (c|id). `clock` is
    injectable so the offline replay can move time forward a day per run.
    """

    def __init__(self, path: str | Path, read: bool = True, clock: Callable[[], float] = time.time):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.read, self.clock = read, clock
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, ts REAL, v TEXT)")
        self.lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: str, hours: float):
        with self.lock:
            row = self.db.execute("SELECT ts, v FROM kv WHERE k=?", (key,)).fetchone() if self.read else None
            if row and self.clock() - row[0] <= hours * 3600:
                self.hits += 1
                return json.loads(row[1])
            self.misses += 1
            return None

    def put(self, key: str, val) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, self.clock(), json.dumps(val)))
            self.db.commit()


# ---------------------------------------------------------------- search (yt-dlp, no quota)
def ytdlp_path() -> str:
    return shutil.which("yt-dlp") or str(Path(sys.executable).with_name("yt-dlp"))


def ytdlp_search(query: str, sort: str, depth: int) -> tuple[Rows | None, str]:
    """One YouTube results page through yt-dlp. Returns (rows, error); rows is None on failure."""
    url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
           + "&sp=" + SEARCH_FILTERS[sort][0])
    try:
        r = subprocess.run([ytdlp_path(), "--flat-playlist", "--dump-single-json", "--no-warnings",
                            "--playlist-end", str(depth), url], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except FileNotFoundError:
        return None, "yt-dlp not found (pip install yt-dlp)"
    if r.returncode != 0 or not r.stdout.strip():
        return None, ((r.stderr or "").strip().splitlines() or ["yt-dlp failed"])[-1][:160]
    try:
        entries = json.loads(r.stdout).get("entries") or []
    except json.JSONDecodeError:
        return None, "bad json"
    rows = [{"id": e["id"], "title": e.get("title") or "", "views": e.get("view_count"),
             "channel": e.get("channel") or e.get("uploader") or ""}
            for e in entries if e and isinstance(e.get("id"), str) and len(e["id"]) == 11]
    return rows, ""


class Searcher:
    """Runs results-page searches in a thread pool, through the cache, and logs every one."""

    def __init__(self, cache: TTLCache, workers: int = 6, fetch: SearchFetch = ytdlp_search,
                 progress: bool = True):
        self.cache, self.workers, self.fetch, self.progress = cache, workers, fetch, progress
        self.log: list[dict] = []
        self.lock, self.done = threading.Lock(), 0

    def one(self, query: str, sort: str, depth: int) -> Rows:
        key = f"s|{query}|{sort}|{depth}"
        rows, err, cached = self.cache.get(key, SEARCH_TTL_HOURS), "", True
        if rows is None:
            cached = False
            rows, err = self.fetch(query, sort, depth)
            if rows is None and depth > 20:   # deeper pages fail more often: settle for page 1
                rows, err2 = self.fetch(query, sort, 20)
                err = f"{err}; page 1 only" if rows is not None else err2
            if rows is not None:
                self.cache.put(key, rows)
        with self.lock:
            self.done += 1
            self.log.append(dict(query=query, sort=sort, depth=depth, results=len(rows or []), cached=cached,
                                 error=err))
            if self.progress and self.done % 10 == 0:
                print(f"  searches: {self.done} done", file=sys.stderr, flush=True)
        return rows or []

    def many(self, jobs: list[tuple[str, str, int]]) -> dict[tuple[str, str, int], Rows]:
        """Run jobs in parallel; results come back in job order so downstream output is repeatable."""
        jobs = list(dict.fromkeys(jobs))
        with cf.ThreadPoolExecutor(self.workers) as ex:
            futures = {j: ex.submit(self.one, *j) for j in jobs}
            return {j: futures[j].result() for j in jobs}

    def many_with_retry(self, jobs: list[tuple[str, str, int]], wait: float = 30,
                        sleep: Callable[[float], None] = time.sleep) -> dict[tuple[str, str, int], Rows]:
        """`many`, then one slower retry of the searches that errored (YouTube's 403s are mostly transient)."""
        res = self.many(jobs)
        errored = {(lg["query"], lg["sort"], lg["depth"]) for lg in self.log if lg["error"]}
        failed = [j for j in dict.fromkeys(jobs) if j in errored and not res.get(j)]
        if failed:
            sleep(wait)
            workers, self.workers = self.workers, 2
            res.update({k: v for k, v in self.many(failed).items() if v})
            self.workers = workers
            ok = {(lg["query"], lg["sort"], lg["depth"]) for lg in self.log if not lg["error"]}
            self.log = [lg for lg in self.log if not (lg["error"] and (lg["query"], lg["sort"], lg["depth"]) in ok)]
        return res


# ---------------------------------------------------------------- YouTube Data API v3
class QuotaExceeded(Exception):
    pass


class YouTubeAPI:
    """videos.list and channels.list in batches of 50, cached, with a per-endpoint unit counter.

    The default YouTube quota is 10,000 units a day; each call here costs 1 unit whatever its parts, so batching
    and caching are what keep a 600-search scan at a few hundred units. `units` counts every attempt, retries
    included, so the reported figure is an upper bound.
    """

    BASE = "https://www.googleapis.com/youtube/v3/"

    def __init__(self, key: str | None, cache: TTLCache,
                 transport: Callable[[str, dict], dict] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.key, self.cache, self.sleep = key, cache, sleep
        self.transport = transport or self._https
        self.units: collections.Counter[str] = collections.Counter()
        self.lock = threading.Lock()

    def _https(self, endpoint: str, params: dict) -> dict:
        url = self.BASE + endpoint + "?" + urllib.parse.urlencode({**params, "key": self.key})
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)

    def get(self, endpoint: str, **params) -> dict:
        for attempt in range(4):
            with self.lock:
                self.units[endpoint] += 1
            try:
                return self.transport(endpoint, params)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")
                if e.code == 403 and ("quotaExceeded" in body or "dailyLimitExceeded" in body):
                    raise QuotaExceeded("YouTube API daily quota is used up; it resets at midnight Pacific time") from e
                if e.code in (429, 500, 502, 503, 504):
                    self.sleep(2 * (attempt + 1))
                    continue
                if e.code == 404:
                    return {"items": []}
                raise RuntimeError(f"YouTube API {endpoint} HTTP {e.code}: {body[:240]}") from e
            except (urllib.error.URLError, TimeoutError):
                self.sleep(2 * (attempt + 1))
        return {"items": []}

    def videos(self, ids) -> dict[str, dict | None]:
        """id -> {id, title, cid, channel, published, views, dur, lang, mfk, live}; None if gone or private."""
        out: dict[str, dict | None] = {}
        todo = []
        for i in dict.fromkeys(ids):
            c = self.cache.get("v|" + i, VIDEO_TTL_HOURS)
            if c is not None:
                out[i] = c or None
            else:
                todo.append(i)
        for k in range(0, len(todo), BATCH):
            chunk = todo[k:k + BATCH]
            data = self.get("videos", part="snippet,statistics,contentDetails,status,liveStreamingDetails",
                            id=",".join(chunk), maxResults=BATCH)
            got = set()
            for it in data.get("items", []):
                sn, st = it.get("snippet", {}), it.get("statistics", {})
                rec = dict(id=it["id"], title=sn.get("title", ""), cid=sn.get("channelId", ""),
                           channel=sn.get("channelTitle", ""), published=sn.get("publishedAt", ""),
                           views=int(st.get("viewCount", 0) or 0),
                           dur=iso_dur(it.get("contentDetails", {}).get("duration")),
                           lang=(sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower(),
                           mfk=bool(it.get("status", {}).get("madeForKids", False)),
                           live=bool(it.get("liveStreamingDetails", {}).get("actualStartTime"))
                           or sn.get("liveBroadcastContent") in ("live", "upcoming"))
                out[rec["id"]] = rec
                got.add(rec["id"])
                self.cache.put("v|" + rec["id"], rec)
            for i in chunk:
                if i not in got:      # deleted or private: remember that too
                    out[i] = None
                    self.cache.put("v|" + i, {})
        return out

    def channels(self, cids) -> dict[str, dict | None]:
        """channel id -> {id, title, created, subs, videos}; subs is None when the channel hides it."""
        out: dict[str, dict | None] = {}
        todo = []
        for c in dict.fromkeys(x for x in cids if x):
            hit = self.cache.get("c|" + c, CHANNEL_TTL_HOURS)
            if hit is not None:
                out[c] = hit or None
            else:
                todo.append(c)
        for k in range(0, len(todo), BATCH):
            chunk = todo[k:k + BATCH]
            data = self.get("channels", part="snippet,statistics", id=",".join(chunk), maxResults=BATCH)
            got = set()
            for it in data.get("items", []):
                sn, st = it.get("snippet", {}), it.get("statistics", {})
                rec = dict(id=it["id"], title=sn.get("title", ""), created=sn.get("publishedAt", ""),
                           subs=None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0) or 0),
                           videos=int(st.get("videoCount", 0) or 0))
                out[rec["id"]] = rec
                got.add(rec["id"])
                self.cache.put("c|" + rec["id"], rec)
            for c in chunk:
                if c not in got:
                    out[c] = None
                    self.cache.put("c|" + c, {})
        return out
