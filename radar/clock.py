"""The run's clock.

Every date the pipeline uses comes from one Clock, so the same code can run live (now) or replay simulated
days offline (see radar.offline).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


def parse_ts(ts: str | None) -> dt.datetime | None:
    """ISO-8601 timestamp as the YouTube API writes it ('2026-11-07T14:03:11Z') -> aware datetime, or None."""
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def age_days(ts: str | None, now: dt.datetime) -> float | None:
    """Days from a timestamp to `now`; None when the timestamp is missing or malformed."""
    t = parse_ts(ts)
    return None if t is None else (now - t).total_seconds() / 86400


@dataclass
class Clock:
    now: dt.datetime  # timezone-aware, UTC

    @classmethod
    def system(cls) -> Clock:
        return cls(dt.datetime.now(dt.timezone.utc))

    @property
    def today(self) -> str:
        return self.now.date().isoformat()

    @property
    def daynum(self) -> int:
        return self.now.date().toordinal()

    def days_ago(self, n: int) -> str:
        return (self.now.date() - dt.timedelta(days=n)).isoformat()

    def age_days(self, ts: str | None) -> float | None:
        return age_days(ts, self.now)

    def time(self) -> float:
        """Seconds since the epoch, for cache TTL checks."""
        return self.now.timestamp()
