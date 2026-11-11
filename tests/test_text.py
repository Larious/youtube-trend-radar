import re

from radar import text


def test_tokens_fold_case_apostrophes_and_possessives():
    assert text.tokens("Here's Why the Romans DIDN'T Build It") == ["here", "why", "the", "romans", "didnt", "build",
                                                                     "it"]
    assert text.tokens("Rome’s Last Emperor") == ["rome", "last", "emperor"]   # curly apostrophe


def test_tokens_keep_hindi_vowel_signs_inside_words():
    assert re.findall(r"\w+", "इतिहास") == ["इत", "ह", "स"]          # plain \w breaks the word at its vowel signs
    assert text.tokens("भारत का इतिहास") == ["भारत", "का", "इतिहास"]


def test_tokens_strip_latin_accents():
    assert text.tokens("Documentário: A História") == ["documentario", "a", "historia"]


def test_skeleton_turns_the_subject_into_x_and_numbers_into_n():
    grammar = {"married", "to", "her", "at"}
    uncle = text.skeleton("Married to Her Uncle at 15", grammar)
    assert uncle == ["married", "to", "her", "X", "at", "N"]
    assert text.skeleton("Married to Her Cousin at 14", grammar) == uncle


def test_skeleton_collapses_runs_of_rare_words():
    assert text.skeleton("The Lost City of Atlantis", {"the", "of"}) == ["the", "X", "of", "X"]


def test_grammar_words_are_words_shared_by_three_titles():
    titles = ["Why Chess Was Banned in 1920", "Why Jazz Was Banned in 1931", "Why Kites Was Banned in 1950",
              "Why Tea Was Taxed"]
    grammar = text.grammar_words(titles)
    assert {"why", "was", "banned", "in"} <= grammar
    assert "chess" not in grammar and "1920" not in grammar and "taxed" not in grammar


def test_shingles_need_a_content_word_and_at_most_one_placeholder():
    sk = ["why", "X", "was", "banned", "in", "N"]
    assert text.good_shingles(sk, "en") == {"X was banned", "was banned in", "banned in N"}


def test_grammar_query_keeps_the_real_words():
    assert text.grammar_query("X was banned") == "was banned"
    assert text.grammar_query("why X was") == ""            # only stopwords left: too vague to search


def test_language_check_uses_the_title_then_the_api():
    assert text.lang_ok("La historia de los incas", "en", "es") is True
    assert text.lang_ok("The history of the Incas", "es", "es") is False
    assert text.lang_ok("Tenochtitlan", "es", "es") is True     # no evidence in the title: trust the API


def test_junk_filter():
    assert text.is_junk("Minecraft Gameplay Part 3")
    assert not text.is_junk("Why Chess Was Banned in 1920")
