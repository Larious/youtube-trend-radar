"""A small synthetic YouTube for offline runs, tests and the bundled sample database.

It replaces only the two network edges:
  search(query, sort, depth)  what a yt-dlp results page would return (upload window, length filter, order)
  api(endpoint, params)       what videos.list / channels.list would return, in the API's JSON shape
Everything between them is production code: the TTL cache, batching and quota counter, filters, clustering,
staging, the SQLite loads and the brief.

The world is generated from a seed, so a replay is repeatable. Every title, channel and number in it is made up.
It contains a handful of planted storylines so each radar signal has something to find:
  * an EARLY wave that fills up to MID   "Why Chess Was Banned in 1920"      (en, history)
  * an ESTABLISHED format with old hits  "The Untold Story of the Emu War"   (en, history)
  * a SATURATED format: many weak copies "7 Money Mistakes That Keep You Poor" (en, money)
  * a pre-wave breakout cloned on the last day   "What If the Earth Had Two Moons" (en, space)
  * an EARLY Spanish wave                "¿Por Qué el Chicle Está Prohibido en Singapur?" (es, history)
  * one-off breakouts on new channels, plus filler uploads, shorts, a live stream and a kids' video
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass
from pathlib import Path

from . import text
from .clock import Clock
from .youtube import SEARCH_FILTERS, Searcher, TTLCache, YouTubeAPI

UTC = dt.timezone.utc
DEFAULT_START = dt.datetime(2026, 11, 1, 6, 30, tzinfo=UTC)   # the first replayed scan
DEFAULT_DAYS = 5

LANGS = {"en": {"min_vpd": 3000, "min_views": 20000}, "es": {"min_vpd": 1500, "min_views": 10000}}
NICHES = {
    "history": {"en": ["history documentary", "forgotten history"], "es": ["historia", "documental de historia"]},
    "money": {"en": ["money", "personal finance"], "es": ["dinero", "economía"]},
    "space": {"en": ["space documentary", "universe"]},
}
# Daily uploads from big and from small channels, the median views of an ordinary small-channel upload, and the
# view range of a big channel's upload. As on YouTube, small channels upload far more than a 20-result page can
# show: the 'uploaded today' page is mostly small channels, while a views-sorted page is mostly big channels
# plus the few small uploads that took off. Only what a search returns is stored, so the database stays small.
PROFILES = {
    ("history", "en"): dict(big_rate=4, small_rate=45, small_med=2000, big=(80_000, 600_000)),
    ("money", "en"): dict(big_rate=4, small_rate=60, small_med=700, big=(60_000, 400_000)),
    ("space", "en"): dict(big_rate=4, small_rate=30, small_med=3000, big=(100_000, 900_000)),
    ("history", "es"): dict(big_rate=3, small_rate=35, small_med=900, big=(30_000, 200_000)),
    ("money", "es"): dict(big_rate=1, small_rate=20, small_med=400, big=(1_500, 5_000)),
}
FILLER = {
    ("history", "en"): (["How {X} Really Worked", "The Rise and Fall of {X}", "{X}: A Complete History",
                         "What Life Was Like in {X}", "The Lost World of {X}", "Inside {X}", "Why {X} Collapsed",
                         "The Strange History of {X}", "A Day in {X}", "{X} and the Birth of an Empire"],
                        ["Carthage", "Byzantium", "the Hanseatic League", "Timbuktu", "the Hittites", "Angkor",
                         "Venice", "Great Zimbabwe", "Sparta", "the Mughal Court", "Pompeii", "the Silk Road",
                         "the Medici Bank", "Old Kyoto", "the Inca Roads", "Babylon", "Aksum", "the Phoenicians"]),
    ("money", "en"): (["The Truth About {X}", "How {X} Makes Money", "Is {X} Worth It in {N}", "{X} Explained Simply",
                       "The Economics of {X}", "Where Does {X} Money Go", "Why {X} Is So Expensive",
                       "{X}: Boom or Bust"],
                      ["Index Funds", "Credit Scores", "Student Loans", "Gold", "Rent Control", "Payday Loans",
                       "Car Leasing", "Private Equity", "Tipping", "Airline Miles", "Real Estate", "Insurance",
                       "Mortgages", "Pensions", "Grocery Prices", "Gym Memberships"]),
    ("space", "en"): (["What Is Inside {X}", "Journey to {X}", "{X}: Space Documentary", "The Mystery of {X}",
                       "How Big Is {X}", "Could We Live on {X}", "{X} vs Earth: Size Comparison",
                       "Could Earth Survive {X}"],
                      ["Europa", "Titan", "Betelgeuse", "the Kuiper Belt", "Proxima b", "Enceladus", "the Oort Cloud",
                       "Io", "Ganymede", "a Magnetar", "the Andromeda Galaxy", "Venus", "Pluto", "a Neutron Star"]),
    ("history", "es"): (["La Historia de {X}", "Así Era la Vida en {X}", "El Ascenso y la Caída de {X}",
                         "{X}: Documental Completo", "Los Secretos de {X}", "Cómo Funcionaba {X}"],
                        ["Cartago", "Bizancio", "Tenochtitlan", "los Incas", "Al-Ándalus", "Roma", "los Mayas",
                         "la Ruta de la Seda", "Esparta", "Babilonia", "Toledo", "Cuzco"]),
    ("money", "es"): (["Cómo Funciona {X}", "La Verdad Sobre {X}", "¿Vale la Pena {X} en {N}?", "{X} Explicado Fácil",
                       "Por Qué {X} Es Tan Caro"],
                      ["la Inflación", "los Bancos", "las Hipotecas", "el Oro", "las Criptomonedas", "el Alquiler",
                       "los Seguros", "las Pensiones", "el Dólar", "la Bolsa"]),
}
# One-off titles for the rare ordinary upload that takes off. Each is used once, so these never form a format.
HOOKS = {
    "en": ["{X}: Nobody Saw This Coming", "How {X} Fooled an Entire Country", "The Week {X} Went Silent",
           "{X} Was Hiding a Secret", "I Visited {X} So You Don't Have To", "{X} Is Weirder Than You Think",
           "The Day {X} Broke Every Record", "{X}, Ranked by a Stranger", "A Letter Found Inside {X}",
           "{X} in One Chaotic Afternoon", "The Accidental Genius of {X}", "{X} Should Not Have Worked",
           "Everything Wrong With {X}", "{X} Changed My Mind", "The Forgotten Rival of {X}",
           "{X} at Three in the Morning", "Why I Can't Stop Thinking About {X}", "The Quiet Collapse of {X}",
           "{X}: An Honest Review", "My Grandfather Remembered {X}", "{X} Without the Myths",
           "The Smartest Mistake in {X}", "Who Paid for {X}?", "{X} Explained With Kitchen Objects",
           "A Stranger's Guide to {X}", "{X} Has a Problem", "Walking Through {X} Alone", "{X}, Frame by Frame",
           "The Last Photo of {X}", "{X} on a Budget of Zero", "Inside the Paperwork of {X}",
           "{X}: The Version Schools Skip", "Sleeping Next to {X}", "{X} Through a Child's Eyes",
           "The Map That Explains {X}", "Buying {X} for One Dollar", "{X} Told by Its Neighbours"],
    "es": ["{X}: Nadie Lo Vio Venir", "Así Engañó {X} a Todo un País", "La Semana en Que {X} Se Calló",
           "{X} Escondía un Secreto", "Lo Que Nadie Dice de {X}", "{X} Es Más Raro de Lo Que Crees",
           "Una Carta Encontrada en {X}", "{X} No Debería Haber Funcionado", "Todo Lo Malo de {X}",
           "{X} Me Hizo Cambiar de Opinión", "El Rival Olvidado de {X}", "{X} Sin Mitos", "¿Quién Pagó {X}?",
           "{X} Explicado con Objetos de Cocina", "Caminando Solo por {X}", "{X}, Cuadro por Cuadro"],
}


@dataclass
class Channel:
    id: str
    title: str
    created: dt.datetime
    subs: int


@dataclass
class Video:
    id: str
    title: str
    cid: str
    niche: str
    lang: str
    published: dt.datetime
    dur: int          # seconds
    total: int        # views it ends up with
    tau: float        # days to reach ~63% of them
    mfk: bool = False
    live: bool = False

    def views(self, now: dt.datetime) -> int:
        age = (now - self.published).total_seconds() / 86400
        return 0 if age <= 0 else int(self.total * (1 - math.exp(-age / self.tau)))


def _ts(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    def __init__(self, seed: int = 7, start: dt.datetime = DEFAULT_START, days: int = DEFAULT_DAYS):
        self.rng = random.Random(seed)
        self.start, self.days, self.now = start, days, start
        self.channels: dict[str, Channel] = {}
        self.videos: list[Video] = []
        self._build()
        self.by_id = {v.id: v for v in self.videos}
        self.tokens = {v.id: set(text.tokens(v.title)) for v in self.videos}
        self.seeds = {v.id: set(NICHES[v.niche].get(v.lang, [])) for v in self.videos}

    # ------------------------------------------------------------ the two network edges
    def config(self) -> dict:
        return {"langs": LANGS, "niches": NICHES}

    def time(self) -> float:
        return self.now.timestamp()

    def search(self, query: str, sort: str, depth: int):
        """A results page: seed queries match their niche's videos, other queries need all their words in the title."""
        _, window, length, order = SEARCH_FILTERS[sort]
        words = set(text.tokens(query))
        hits = []
        for v in self.videos:
            age = (self.now - v.published).total_seconds() / 86400
            if age < 0 or (window is not None and age > window):
                continue
            if (length == "long" and v.dur < 1200) or (length == "mid" and not 240 <= v.dur < 1200):
                continue
            if query in self.seeds[v.id] or (words and words <= self.tokens[v.id]):
                hits.append(v)
        hits.sort(key=(lambda v: v.published) if order == "date" else (lambda v: v.views(self.now)), reverse=True)
        return [{"id": v.id, "title": v.title, "views": v.views(self.now), "channel": self.channels[v.cid].title}
                for v in hits[:depth]], ""

    def api(self, endpoint: str, params: dict) -> dict:
        """videos.list / channels.list in the API's JSON shape; IDs that don't exist yet are simply absent."""
        ids = params["id"].split(",")
        if endpoint == "videos":
            return {"items": [self._video_item(self.by_id[i]) for i in ids
                              if i in self.by_id and self.by_id[i].published <= self.now]}
        if endpoint == "channels":
            return {"items": [self._channel_item(self.channels[i]) for i in ids
                              if i in self.channels and self.channels[i].created <= self.now]}
        return {"items": []}

    def _video_item(self, v: Video) -> dict:
        snippet = {"title": v.title, "channelId": v.cid, "channelTitle": self.channels[v.cid].title,
                   "publishedAt": _ts(v.published), "defaultAudioLanguage": v.lang,
                   "liveBroadcastContent": "live" if v.live else "none"}
        return {"id": v.id, "snippet": snippet, "statistics": {"viewCount": str(v.views(self.now))},
                "contentDetails": {"duration": f"PT{v.dur // 60}M{v.dur % 60}S"},
                "status": {"madeForKids": v.mfk}}

    def _channel_item(self, c: Channel) -> dict:
        return {"id": c.id, "snippet": {"title": c.title, "publishedAt": _ts(c.created)},
                "statistics": {"subscriberCount": str(c.subs), "videoCount": "120", "hiddenSubscriberCount": False}}

    # ------------------------------------------------------------ generation
    def at(self, day: float, hours: float = 0.0) -> dt.datetime:
        """A moment relative to the first scan: at(0) is the first scan, at(2, -6) six hours before the third."""
        return self.start + dt.timedelta(days=day, hours=hours)

    def channel(self, kind: str) -> Channel:
        r = self.rng
        subs, age = {"big": (r.randint(250_000, 2_500_000), r.randint(1200, 3600)),
                     "small": (r.randint(800, 45_000), r.randint(400, 2200)),
                     "new": (r.randint(150, 9_000), r.randint(20, 160))}[kind]
        n = len(self.channels) + 1
        c = Channel(f"UCdemo{n:018d}", f"Sample channel {n:03d}", self.start - dt.timedelta(days=age), subs)
        self.channels[c.id] = c
        return c

    def upload(self, c: Channel, niche: str, lang: str, title: str, published: dt.datetime, total: float,
               tau: float = 3.0, minutes: float | None = None, **flags) -> Video:
        r = self.rng
        if minutes is None:
            minutes = r.uniform(8, 19.5) if r.random() < 0.55 else r.uniform(21, 48)
        v = Video(f"demo_{len(self.videos) + 1:06d}", title, c.id, niche, lang, published, int(minutes * 60),
                  int(total), tau, **flags)
        self.videos.append(v)
        return v

    def between_scans(self, day: int) -> dt.datetime:
        """A random moment in the 24 hours before scan `day`."""
        return self.at(day, -self.rng.uniform(0.5, 23.5))

    def _build(self) -> None:
        r, last = self.rng, self.days - 1
        self.hooks = {lang: r.sample(hooks, len(hooks)) for lang, hooks in HOOKS.items()}
        for (niche, lang), p in PROFILES.items():
            self._filler(niche, lang, p)
        self._oddities()

        # EARLY -> MID: a new format spreading across small and new channels
        subjects = ["Chess", "Tomatoes", "Saffron", "Jazz", "Coffee", "Kites", "Pinball", "Margarine", "Comic Books",
                    "Bicycles", "Novels", "Ice Cream", "Tea Houses", "Yo-yos"]
        r.shuffle(subjects)
        title = lambda: f"Why {subjects.pop()} Was Banned in {r.randint(1890, 1985)}"   # noqa: E731
        for d, kind, total in ((-5, "new", 260_000), (-3, "small", 180_000), (-2, "new", 320_000)):
            self.upload(self.channel(kind), "history", "en", title(), self.at(d, -6), total, tau=2.5, minutes=24)
        for d in range(self.days):
            self.upload(self.channel("new" if d % 2 else "small"), "history", "en", title(), self.between_scans(d),
                        r.uniform(150_000, 350_000), tau=2.5)
            self.upload(self.channel("small"), "history", "en", title(), self.between_scans(d), r.uniform(2_000, 5_000))

        # ESTABLISHED: an old format with big old hits that still pays small channels
        subjects = ["the Dancing Plague", "the Great Molasses Flood", "the Tunguska Blast", "the Radium Girls",
                    "the Emu War", "the Year Without a Summer", "the Boston Beer Flood", "the Kentucky Meat Shower",
                    "the Defenestration of Prague", "the Great Stink"]
        title = lambda: f"The Untold Story of {subjects.pop()}"   # noqa: E731
        for _ in range(4):
            self.upload(self.channel("big"), "history", "en", title(), self.at(-r.randint(120, 900)),
                        r.uniform(400_000, 2_500_000), tau=20, minutes=r.uniform(25, 50))
        for d in (-6, -3, 0, 2, 4):
            if d <= last:
                self.upload(self.channel("small"), "history", "en", title(), self.at(d, -10),
                            r.uniform(120_000, 220_000), minutes=r.uniform(21, 35))

        # SATURATED: a listicle everyone copies; two early winners, a stream of copies that get little
        copycats = [self.channel("small") for _ in range(14)]
        title = lambda: f"{r.choice([5, 7, 9, 10, 12, 15, 21])} Money Mistakes That Keep You Poor"   # noqa: E731
        for d in (-5, -2, 1, 3):
            if d <= last:
                self.upload(self.channel("small"), "money", "en", title(), self.at(d, -8), r.uniform(70_000, 110_000))
        for d in range(-13, self.days):
            for _ in range(2):
                self.upload(r.choice(copycats), "money", "en", title(), self.between_scans(d), r.uniform(600, 3_500),
                            minutes=r.uniform(8, 15))

        # PRE-WAVE: one breakout on a new channel, cloned by another new channel on the last day
        self.upload(self.channel("new"), "space", "en", "What If the Earth Had Rings Like Saturn", self.at(-1, -12),
                    260_000, tau=4, minutes=24)
        self.upload(self.channel("new"), "space", "en", "What If the Earth Had Two Moons", self.at(last, -9),
                    400_000, tau=1.2, minutes=22)

        # an EARLY Spanish wave
        subjects = ["el Chicle", "el Kétchup", "Halloween", "los Videojuegos", "el Color Rojo", "las Bicicletas"]
        places = ["Singapur", "Corea del Norte", "Francia", "Japón", "Turkmenistán", "Bután"]
        for d, kind in ((-4, "small"), (-2, "new"), (1, "small"), (3, "new")):
            if d <= last:
                self.upload(self.channel(kind), "history", "es",
                            f"¿Por Qué {subjects.pop()} Está Prohibido en {places.pop()}?", self.at(d, -7),
                            r.uniform(40_000, 90_000))

        # one-off breakouts on new channels (pre-wave candidates that never get a clone here)
        for niche, lang, t, d in (("history", "en", "The Detective Who Solved a Case With a Library Card", -2),
                                  ("space", "en", "A Quiet Observatory Kept One Secret for Forty Years", 1),
                                  ("history", "en", "The Shepherd Who Mapped a Kingdom From Memory", 2),
                                  ("money", "es", "Ahorré Todo Mi Sueldo Durante Un Año Entero", 0)):
            if d <= last:
                total = 120_000 if lang == "en" else 45_000
                self.upload(self.channel("new"), niche, lang, t, self.at(d, -14), total, minutes=r.uniform(12, 30))

    def _filler(self, niche: str, lang: str, p: dict) -> None:
        """Ordinary uploads: a few big channels and many small ones. About 1.5% of small uploads take off; each
        of those gets a one-off title, so a lucky hit can't pose as a format. The rest stay under the bar."""
        r = self.rng
        templates, subjects = FILLER[(niche, lang)]
        big = [self.channel("big") for _ in range(4)]
        small = [self.channel("small") for _ in range(8)] + [self.channel("new") for _ in range(2)]
        bar = LANGS[lang]["min_views"]

        def title(pattern: str | None = None) -> str:
            t = (pattern or r.choice(templates)).replace("{X}", r.choice(subjects))
            t = t.replace("{N}", str(r.randint(2024, 2026)))
            return t[0].upper() + t[1:]

        for d in range(-9, self.days):
            for _ in range(max(1, p["big_rate"] + r.randint(-1, 1))):
                self.upload(r.choice(big), niche, lang, title(), self.between_scans(d), r.uniform(*p["big"]), tau=4)
            for _ in range(max(1, p["small_rate"] + r.randint(-3, 3))):
                if r.random() < 0.015 and self.hooks[lang]:
                    self.upload(r.choice(small), niche, lang, title(self.hooks[lang].pop()), self.between_scans(d),
                                r.uniform(1.5, 6) * bar, tau=2.5)
                    continue
                views = min(r.lognormvariate(math.log(p["small_med"]), 1.0), 0.8 * bar)
                self.upload(r.choice(small), niche, lang, title(), self.between_scans(d), views,
                            minutes=r.uniform(2, 5) if r.random() < 0.06 else None)   # a few shorts

    def _oddities(self) -> None:
        """Videos the filters must drop: a live stream, a kids' video, a gameplay video, a title in the wrong
        language."""
        c = self.channel("small")
        self.upload(c, "history", "en", "LIVE: Ask a Historian Anything", self.at(-1, -3), 9_000, live=True)
        self.upload(c, "money", "en", "Counting Coins for Kids", self.at(-1, -5), 9_000, mfk=True)
        self.upload(c, "space", "en", "Space Engine Gameplay: Flying to Titan", self.at(-1, -4), 9_000)
        self.upload(c, "history", "es", "The History of Spain in Ten Minutes", self.at(-1, -6), 9_000, minutes=10)


def replay(db_path: str | Path, out_dir: str | Path, days: int = DEFAULT_DAYS, seed: int = 7,
           start: dt.datetime = DEFAULT_START, workers: int = 4, quiet: bool = False) -> list[dict]:
    """Run the real scan once per simulated day against the synthetic world. Returns each day's summary."""
    from . import store
    from .scan import Run, ScanOptions, scan

    world = World(seed=seed, start=start, days=days)
    db = store.connect(db_path)
    cache = TTLCache(":memory:", clock=world.time)
    summaries = []
    try:
        for d in range(days):
            world.now = world.at(d)
            run = Run(clock=Clock(world.now),
                      searcher=Searcher(cache, workers=workers, fetch=world.search, progress=False),
                      api=YouTubeAPI(key=None, cache=cache, transport=world.api),
                      db=db, out_dir=Path(out_dir))
            summaries.append(scan(world.config(), run, ScanOptions(retry_wait=0, quiet=quiet)))
    finally:
        db.close()
    return summaries
