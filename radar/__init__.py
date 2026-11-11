"""YouTube trend radar.

Samples new uploads across many niches and languages every day, keeps the history in SQLite and turns it into
radar signals: which title formats are breaking out on small channels, and which niches look saturated.
The output is a set of signals to investigate, not verdicts.
"""

__version__ = "1.0.0"
