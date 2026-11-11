import collections
import io
import urllib.error

import pytest

from radar.youtube import QuotaExceeded, Searcher, TTLCache, YouTubeAPI, iso_dur


def test_cache_entries_expire_by_age():
    now = [1_000.0]
    cache = TTLCache(":memory:", clock=lambda: now[0])
    cache.put("k", {"v": 1})
    assert cache.get("k", hours=1) == {"v": 1}
    now[0] += 3601
    assert cache.get("k", hours=1) is None
    assert cache.get("k", hours=2) == {"v": 1}     # the reader decides how old is too old


def _api_items(ids):
    return {"items": [{"id": i, "snippet": {"title": i, "channelId": "UC1", "publishedAt": "2026-11-01T00:00:00Z"},
                       "statistics": {"viewCount": "5"}, "contentDetails": {"duration": "PT10M"}, "status": {}}
                      for i in ids if i != "gone"]}


def test_lookups_are_batched_counted_per_endpoint_and_cached():
    calls = []

    def transport(endpoint, params):
        ids = params["id"].split(",")
        calls.append(len(ids))
        return _api_items(ids)

    api = YouTubeAPI("key", TTLCache(":memory:"), transport=transport)
    ids = [f"id{n:03d}" for n in range(119)] + ["gone"]
    out = api.videos(ids)
    assert calls == [50, 50, 20]                    # 120 IDs = 3 calls = 3 quota units
    assert api.units == {"videos": 3}
    assert out["id000"]["dur"] == 600 and out["gone"] is None
    api.videos(ids)                                 # again: everything comes from the cache, misses included
    assert api.units == {"videos": 3}


def test_quota_exhaustion_stops_the_run_instead_of_retrying():
    def transport(endpoint, params):
        body = io.BytesIO(b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}')
        raise urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", None, body)

    api = YouTubeAPI("key", TTLCache(":memory:"), transport=transport, sleep=lambda s: None)
    with pytest.raises(QuotaExceeded):
        api.channels(["UC1"])
    assert api.units == {"channels": 1}


def test_failed_searches_are_retried_once():
    attempts = collections.Counter()

    def fetch(query, sort, depth):
        attempts[query] += 1
        if query == "flaky" and attempts[query] == 1:
            return None, "HTTP Error 403: Forbidden"
        return [{"id": "abcdefghijk", "title": query, "views": 1, "channel": "c"}], ""

    s = Searcher(TTLCache(":memory:"), workers=2, fetch=fetch, progress=False)
    res = s.many_with_retry([("ok", "today_new", 20), ("flaky", "today_new", 20)], sleep=lambda s: None)
    assert attempts == {"ok": 1, "flaky": 2}
    assert all(res.values())
    assert not [entry for entry in s.log if entry["error"]]    # a failure that recovered leaves no error behind


def test_iso_duration():
    assert iso_dur("PT1H2M3S") == 3723
    assert iso_dur("P1DT1M") == 86_460
    assert iso_dur(None) == 0
