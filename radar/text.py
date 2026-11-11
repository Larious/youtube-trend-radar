"""Title text: normalisation, language checks, tokens and title skeletons.

A title skeleton keeps the words that are common across the day's titles (the format's grammar), turns rare
words (the subject) into X and numbers into N. "Married to Her Uncle at 15" and "Married to Her Cousin at 14"
both become `married to her X at N`, so they land in the same format cluster.
"""

from __future__ import annotations

import collections
import re
import unicodedata
from collections.abc import Iterable

# Title-language evidence: common function words (accent-stripped) and the accented letters of each language.
LATIN_LANGS = {
    "en": ("the of and to in how why what was were is are this that with from for they you your who when inside "
           "every after before their into never", ""),
    "es": ("el la los las de del que y en por para con una un como mas lo su se al porque nunca fue era asi ya hasta "
           "sus esta este", "ñ¿¡áéíóú"),
    "pt": ("o a os as de do da dos das que e em no na nos nas por para com uma um como mais nao voce foi era isso "
           "esse essa ate sua seu", "ãõçâêô"),
    "de": ("der die das und ist nicht wie warum mit ein eine im den dem zu von auf fur was wurde war sich auch nach",
           "äöüß"),
    "fr": ("le la les des du de est pourquoi comment et une un dans pour avec qui que sur ce cette etait au aux",
           "éèêàçùâîô"),
    "it": ("il lo la gli le di del della che e perche come una un con per nel nella era sono questo", "àèìòù"),
}
# Languages identified by their script instead (Unicode ranges).
SCRIPT_LANGS = {"hi": [(0x0900, 0x097F)], "mr": [(0x0900, 0x097F)], "bn": [(0x0980, 0x09FF)],
                "ta": [(0x0B80, 0x0BFF)], "te": [(0x0C00, 0x0C7F)], "ru": [(0x0400, 0x04FF)],
                "uk": [(0x0400, 0x04FF)], "ar": [(0x0600, 0x06FF)], "th": [(0x0E00, 0x0E7F)],
                "ko": [(0xAC00, 0xD7AF), (0x1100, 0x11FF)], "ja": [(0x3040, 0x30FF), (0x4E00, 0x9FFF)],
                "zh": [(0x4E00, 0x9FFF)]}
STOP = set((LATIN_LANGS["en"][0] + " a an on at by it its as be has had have not no but or so if than then "
            "there these those our we he she his her them us my me all any can could would should will just about "
            "over under vs only one two three most more why what how when where who").split())
HINDI_STOP = {"ki", "ka", "ke", "ko", "se", "hai", "aur", "mein", "का", "की", "के", "को", "से", "है", "और", "में", "क्या"}

# Titles the radar ignores: games, reactions, music, live shows, re-uploads, kids' content and similar
# formats that are not the long-form videos it tracks.
JUNK_RX = re.compile(
    r"\b(gameplay|walkthrough|let'?s play|playthrough|reacts?|reaction|livestream|live stream|trailer|"
    r"teaser|full movie|lyrics?|karaoke|asmr|lo-?fi|unboxing|minecraft|roblox|fortnite|gta ?v?)\b"
    r"|\b(highlights?|full broadcast|full show|breaking news today|news today|live news|world news tonight|"
    r"gmod|garry'?s mod|beamng|sprunki|official (music )?video|official audio|lyric|song|songs|dubbed|"
    r"full action movie|full hd movie|episode \d+ full|podcast|sermon|pravachan|katha|mukbang|vlog|"
    r"periscope|tiktok compilation|nursery|kids learning|supercut|every point|bollywood|south indian|"
    r"cartoon for kids|paw patrol|tv serial)\b",
    re.I)

# Words, keeping Devanagari and Bengali vowel signs inside the word (\w alone splits on them).
_TOK = re.compile(r"[\wऀ-ॿঀ-৿]+")


def norm(s: str | None) -> str:
    """Lowercase; strip accents from Latin letters only (Devanagari and other scripts keep their marks)."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", (s or "").lower()):
        if unicodedata.combining(ch) and out and out[-1] < "ɐ":
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def is_junk(title: str) -> bool:
    return bool(JUNK_RX.search(norm(title)))


def title_lang_verdict(title: str, want: str) -> bool | None:
    """True / False when the title carries evidence of its language, None when it doesn't."""
    raw = (title or "").lower()
    letters = [c for c in raw if c.isalpha()]
    if not letters:
        return None
    if want in SCRIPT_LANGS:
        inside = sum(any(a <= ord(c) <= b for a, b in SCRIPT_LANGS[want]) for c in letters)
        return True if inside / len(letters) >= 0.3 else None   # Latin-script Hinglish etc. falls through
    if sum(ord(c) >= 0x250 for c in letters) / len(letters) > 0.3:
        return False                                            # mostly another script
    if want not in LATIN_LANGS:
        return None
    toks = set(re.findall(r"[a-z]+", norm(raw)))
    scores = {lg: len(toks & set(words.split())) + 2 * sum(1 for c in set(raw) if c in accents)
              for lg, (words, accents) in LATIN_LANGS.items()}
    mine, best = scores[want], max(scores.values())
    if best == 0:
        return None
    if mine >= best:
        return True
    return False if best - mine >= 2 else None


def lang_ok(title: str, api_lang: str | None, want: str | None) -> bool:
    """Is this video in the language we searched for? The title decides; the API's audio language breaks ties."""
    if not want or want == "any":
        return True
    verdict = title_lang_verdict(title, want)
    if verdict is not None:
        return verdict
    if api_lang and api_lang not in ("zxx", "und", "mul"):
        return api_lang.split("-")[0] == want
    return True


def tokens(title: str) -> list[str]:
    """Lowercased words; apostrophes folded (here's -> here, don't -> dont); Indic vowel signs kept in the word."""
    t = norm(title).replace("’", "'").replace("`", "'")
    t = re.sub(r"'s\b", "", t).replace("'", "")
    return [w for w in _TOK.findall(t) if w and w != "_"]


def grammar_words(titles: Iterable[str], share: float = 0.004, min_df: int = 3) -> set[str]:
    """Words frequent enough across a day's titles to count as format grammar, plus English stopwords.

    A word is grammar when it appears in at least max(min_df, share x number of titles) titles. Numbers never are.
    """
    df: collections.Counter[str] = collections.Counter()
    n = 0
    for t in titles:
        df.update(set(tokens(t)))
        n += 1
    floor = max(min_df, int(share * n))
    return {w for w, c in df.items() if c >= floor and not w.isdigit()} | STOP


def skeleton(title: str, grammar: set[str]) -> list[str]:
    """Grammar words kept, other words -> X (runs of X collapse to one), numbers -> N."""
    out: list[str] = []
    for t in tokens(title):
        s = "N" if t.isdigit() else (t if t in grammar else "X")
        if s == "X" and out and out[-1] == "X":
            continue
        out.append(s)
    return out


def stopwords(lang: str) -> set[str]:
    return STOP | set(LATIN_LANGS.get(lang, ("", ""))[0].split()) | HINDI_STOP


def good_shingles(sk: list[str], lang: str) -> set[str]:
    """Skeleton 3-grams that can identify a format: at most one placeholder and at least one content word."""
    stop, keep = stopwords(lang), set()
    for k in range(len(sk) - 2):
        g = sk[k:k + 3]
        if sum(t in ("X", "N") for t in g) > 1 or not any(t not in ("X", "N") and t not in stop for t in g):
            continue
        keep.add(" ".join(g))
    return keep


def grammar_query(shingle: str) -> str:
    """The search query that probes a format: its real words, if there are 2+ and at least one isn't a stopword."""
    words = [w for w in shingle.split() if w not in ("X", "N")]
    return " ".join(words) if len([w for w in words if w not in STOP]) >= 1 and len(words) >= 2 else ""
