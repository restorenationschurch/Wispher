"""Use the Telugu Bible (IRV) when the speaker names or quotes scripture.

Works on the English transcript (before translation):
  * a spoken reference such as "John 3:16", "Genesis chapter 1" or "Psalm 23" becomes the Telugu book name
    plus the chapter/verse in Telugu words;
  * a segment that is (almost) a verse the speaker quotes in English (KJV or WEB wording) is replaced by the
    exact Telugu IRV verse, instead of a machine translation.
Everything else keeps the normal machine translation. Needs the "bible" folder (run fetch_bible.py once).
Telugu text: Indian Revised Version 2019, (c) 2017, 2019 Bridge Connectivity Solutions, CC BY-SA 4.0.
"""
import os, re, json
from collections import Counter, defaultdict
from difflib import SequenceMatcher

BIBLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bible")
CREDIT = ("Telugu scripture: Indian Revised Version (IRV) 2019, (c) 2017, 2019 Bridge Connectivity Solutions, "
          "CC BY-SA 4.0 (ebible.org/tel2017). Verses are used unchanged; other text is machine-translated.")

# code (SIL/USFM), English name, extra spoken names
BOOKS = [
    ("GEN", "Genesis", []), ("EXO", "Exodus", []), ("LEV", "Leviticus", []), ("NUM", "Numbers", []),
    ("DEU", "Deuteronomy", []), ("JOS", "Joshua", []), ("JDG", "Judges", []), ("RUT", "Ruth", []),
    ("1SA", "1 Samuel", []), ("2SA", "2 Samuel", []), ("1KI", "1 Kings", []), ("2KI", "2 Kings", []),
    ("1CH", "1 Chronicles", []), ("2CH", "2 Chronicles", []), ("EZR", "Ezra", []), ("NEH", "Nehemiah", []),
    ("EST", "Esther", []), ("JOB", "Job", []), ("PSA", "Psalms", ["Psalm"]), ("PRO", "Proverbs", []),
    ("ECC", "Ecclesiastes", []), ("SNG", "Song of Solomon", ["Song of Songs"]), ("ISA", "Isaiah", []),
    ("JER", "Jeremiah", []), ("LAM", "Lamentations", []), ("EZK", "Ezekiel", []), ("DAN", "Daniel", []),
    ("HOS", "Hosea", []), ("JOL", "Joel", []), ("AMO", "Amos", []), ("OBA", "Obadiah", []), ("JON", "Jonah", []),
    ("MIC", "Micah", []), ("NAM", "Nahum", []), ("HAB", "Habakkuk", []), ("ZEP", "Zephaniah", []),
    ("HAG", "Haggai", []), ("ZEC", "Zechariah", []), ("MAL", "Malachi", []), ("MAT", "Matthew", []),
    ("MRK", "Mark", []), ("LUK", "Luke", []), ("JHN", "John", []), ("ACT", "Acts", ["Acts of the Apostles"]),
    ("ROM", "Romans", []), ("1CO", "1 Corinthians", []), ("2CO", "2 Corinthians", []), ("GAL", "Galatians", []),
    ("EPH", "Ephesians", []), ("PHP", "Philippians", []), ("COL", "Colossians", []),
    ("1TH", "1 Thessalonians", []), ("2TH", "2 Thessalonians", []), ("1TI", "1 Timothy", []),
    ("2TI", "2 Timothy", []), ("TIT", "Titus", []), ("PHM", "Philemon", []), ("HEB", "Hebrews", []),
    ("JAS", "James", []), ("1PE", "1 Peter", []), ("2PE", "2 Peter", []), ("1JN", "1 John", []),
    ("2JN", "2 John", []), ("3JN", "3 John", []), ("JUD", "Jude", []), ("REV", "Revelation", ["Revelations"]),
]
_PREFIX = {"1": ["1", "I", "First", "1st"], "2": ["2", "II", "Second", "2nd"], "3": ["3", "III", "Third", "3rd"]}


def _variants():
    out = {}  # spoken form (lower-case, single spaces) -> code
    for code, name, extra in BOOKS:
        for nm in [name] + extra:
            m = re.match(r"^([123]) (.+)$", nm)
            if m:
                for p in _PREFIX[m.group(1)]:
                    out[f"{p} {m.group(2)}".lower()] = code
            else:
                out[nm.lower()] = code
    return out


_VARIANTS = _variants()
_NAMES_RE = "|".join(re.escape(v).replace(r"\ ", r"\s+") for v in sorted(_VARIANTS, key=len, reverse=True))
_REF_RE = re.compile(
    rf"\b(?P<book>{_NAMES_RE})\b\.?\s*(?P<chw>(?:chapter|ch\.?)\s*)?(?P<ch>\d{{1,3}})"
    rf"(?:\s*(?:[:.,]|\s+verses?\s+|\s+vs?\.?\s*)\s*(?P<v1>\d{{1,3}})(?:\s*(?:-|–|—|to|through)\s*(?P<v2>\d{{1,3}}))?)?",
    re.I)
_BOOKOF_RE = re.compile(rf"\bbook\s+of\s+(?P<book>{_NAMES_RE})\b", re.I)

_state = {}


def available():
    need = ("tel2017_vpl.txt", "eng-kjv_vpl.txt", "eng-web_vpl.txt", "books_te.json")
    return all(os.path.exists(os.path.join(BIBLE_DIR, f)) for f in need)


def _words(t):
    return re.findall(r"[a-z0-9']+", (t or "").lower().replace("’", "'"))


# the eBible text files use older book codes than the USFM ones in BOOKS
_VPL_CODES = {"SOL": "SNG", "EZE": "EZK", "JOE": "JOL", "NAH": "NAM", "MAR": "MRK", "JOH": "JHN", "PHI": "PHP",
              "JAM": "JAS", "1JO": "1JN", "2JO": "2JN", "3JO": "3JN"}
_CANON = {c for c, _, _ in BOOKS}


def _read_vpl(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            m = re.match(r"^(\w{3}) (\d+):(\d+) (.*)$", line.rstrip("\n"))
            if m:
                code = _VPL_CODES.get(m.group(1), m.group(1))
                if code in _CANON:  # skips the apocrypha books that the KJV file also contains
                    rows.append((code, int(m.group(2)), int(m.group(3)), m.group(4).strip()))
    return rows


def _load():
    if _state:
        return _state
    with open(os.path.join(BIBLE_DIR, "books_te.json"), encoding="utf-8") as f:
        _state["names"] = json.load(f)
    _state["tel"] = {(c, ch, v): t for c, ch, v, t in _read_vpl(os.path.join(BIBLE_DIR, "tel2017_vpl.txt"))}
    bibles = []
    for fn in ("eng-kjv_vpl.txt", "eng-web_vpl.txt"):
        verses = [(c, ch, v, _words(t)) for c, ch, v, t in _read_vpl(os.path.join(BIBLE_DIR, fn))]
        index = defaultdict(list)
        for i, (_, _, _, w) in enumerate(verses):
            for g in set(zip(w, w[1:], w[2:])):
                index[g].append(i)
        bibles.append((verses, index))
    _state["bibles"] = bibles
    return _state


def te_ref(code, ch, v1=None, v2=None):
    name = _load()["names"].get(code, code)
    s = f"{name} {ch}వ అధ్యాయం"
    if v1 and v2 and v2 > v1:
        s += f" {v1} నుండి {v2} వరకు వచనాలు"
    elif v1:
        s += f" {v1}వ వచనం"
    return s


def en_label(code, ch, v1=None, v2=None):
    name = next(n for c, n, _ in BOOKS if c == code)
    return f"{name} {ch}" + (f":{v1}" + (f"-{v2}" if v2 and v2 > v1 else "") if v1 else "")


def find_refs(text):
    """[(start, end, code, ch, v1, v2)] for spoken references; a bare 'Name 5' needs the word chapter or a verse
    (Psalms may be given as just a chapter)."""
    found = []
    for m in _REF_RE.finditer(text):
        code = _VARIANTS[re.sub(r"\s+", " ", m.group("book").lower())]
        if not (m.group("v1") or m.group("chw") or code == "PSA"):
            continue
        found.append((m.start(), m.end(), code, int(m.group("ch")),
                      int(m.group("v1")) if m.group("v1") else None,
                      int(m.group("v2")) if m.group("v2") else None))
    for m in _BOOKOF_RE.finditer(text):  # "the book of Genesis"
        if not any(a <= m.start() < b for a, b, *_ in found):
            found.append((m.start(), m.end(), _VARIANTS[re.sub(r"\s+", " ", m.group("book").lower())], None, None, None))
    return sorted(found)


def match_quote(words, min_words=7, min_score=0.78):
    """Best (score, code, chapter, v_first, v_last) if the words are (almost) 1-3 verses in KJV/WEB, else None."""
    if len(words) < min_words:
        return None
    st = _load()
    best = None
    grams = set(zip(words, words[1:], words[2:]))
    for verses, index in st["bibles"]:
        cand = Counter(vi for g in grams for vi in index.get(g, ()))
        for vi, _ in cand.most_common(6):
            code, ch, v0, _w = verses[vi]
            bw = []
            for span in (1, 2, 3):
                if vi + span - 1 >= len(verses):
                    break
                c2, ch2, v2, w2 = verses[vi + span - 1]
                if (c2, ch2) != (code, ch):
                    break
                bw = bw + w2
                sm = SequenceMatcher(None, words, bw, autojunk=False)
                m = sum(b.size for b in sm.get_matching_blocks())
                score = min(m / len(bw), m / len(words))
                if score >= min_score and (best is None or score > best[0]):
                    best = (score, code, ch, v0, v2)
    return best


def score_range(words, code, ch, v1, v2, min_words=5):
    """Similarity of the words to one named verse range (used when the speaker says the reference)."""
    if len(words) < min_words:
        return 0.0
    best = 0.0
    for verses, _ in _load()["bibles"]:
        bw = [w for c, c2, v, ws in verses if c == code and c2 == ch and v1 <= v <= v2 for w in ws]
        if bw:
            m = sum(b.size for b in SequenceMatcher(None, words, bw, autojunk=False).get_matching_blocks())
            best = max(best, min(m / len(bw), m / len(words)))
    return best


def te_verses(code, ch, v1, v2):
    tel = _load()["tel"]
    parts = [tel.get((code, ch, v)) for v in range(v1, v2 + 1)]
    return " ".join(parts) if all(parts) else None


def prepare(segs):
    """Returns (segments, info): info[i] is None (translate normally), ("verse", telugu_text, label) or
    ("refs", pieces, labels) where pieces mixes English text to translate and ready Telugu references.
    Two neighbouring segments are merged when together they are one quoted verse."""
    _load()
    out, info = [], []
    i = 0
    while i < len(segs):
        s = segs[i]
        refs = find_refs(s["text"])
        # text of the segment without the spoken reference, for matching the quote itself
        stripped = s["text"]
        for a, b, *_ in reversed(refs):
            stripped = stripped[:a] + " " + stripped[b:]
        hit, merged = match_quote(_words(stripped)), None
        if not hit:  # the speaker named the verse: accept a looser match against exactly that verse
            for a, b, code, ch, v1, v2 in refs:
                if ch and v1 and score_range(_words(stripped), code, ch, v1, v2 or v1) >= 0.55:
                    hit = (1.0, code, ch, v1, v2 or v1)
                    break
        if not hit and i + 1 < len(segs) and segs[i + 1]["start"] - s["end"] < 1.0:
            nxt = segs[i + 1]
            both = match_quote(_words(stripped + " " + nxt["text"]))
            if both and both[0] >= 0.85:
                hit, merged = both, nxt
        if hit:
            _, code, ch, v1, v2 = hit
            te = te_verses(code, ch, v1, v2)
            if te:
                if refs and refs[0][3] is None:
                    refs = refs[1:]
                prefix = f"{te_ref(*refs[0][2:])}: " if refs and refs[0][3] else ""
                seg = dict(s)
                if merged:
                    seg.update(end=merged["end"], text=s["text"] + " " + merged["text"], words=[])
                    i += 1
                out.append(seg)
                info.append(("verse", prefix + te, en_label(code, ch, v1, v2) + " (quote)"))
                i += 1
                continue
        if refs:
            pieces, pos, labels = [], 0, []
            for a, b, code, ch, v1, v2 in refs:
                pieces.append(("en", s["text"][pos:a]))
                if ch is None:
                    pieces.append(("te", _load()["names"].get(code, code) + " గ్రంథం"))
                    labels.append(next(n for c, n, _ in BOOKS if c == code))
                else:
                    pieces.append(("te", te_ref(code, ch, v1, v2)))
                    labels.append(en_label(code, ch, v1, v2))
                pos = b
            pieces.append(("en", s["text"][pos:]))
            out.append(s)
            info.append(("refs", pieces, labels))
        else:
            out.append(s)
            info.append(None)
        i += 1
    return out, info


def translate_segments(segs, info, translate_fn, src, tgt):
    """Translated text per segment (list) and the labels of the scripture used (list)."""
    jobs = []  # English strings to machine-translate
    for s, inf in zip(segs, info):
        if inf is None:
            jobs.append(s["text"])
        elif inf[0] == "refs":
            jobs += [t for kind, t in inf[1] if kind == "en" and re.search(r"\w", t)]
    done = iter(translate_fn(jobs, src, tgt) if jobs else [])
    texts, used = [], []
    for s, inf in zip(segs, info):
        if inf is None:
            texts.append(next(done))
        elif inf[0] == "verse":
            texts.append(inf[1])
            used.append(inf[2])
        else:
            parts = [next(done) if kind == "en" and re.search(r"\w", t) else (t if kind == "te" else "")
                     for kind, t in inf[1]]
            texts.append(re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip())
            used += inf[2]
    return texts, used
