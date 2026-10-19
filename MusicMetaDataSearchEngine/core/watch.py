"""Watchfolder-Verarbeitungskern — testbar, ohne Observer/FS-Events.

identify(): Artist/Titel aus ID3-Tags oder Dateiname ('Artist - Title').
is_stable(): Dateigröße stabil? (Kopiervorgang fertig, gegen Race Condition.)
process_one(): eine Datei -> Resolve -> Plan -> Aktion.

Default ist preview: es wird NICHTS geschrieben. apply=True UND erreichte Band-
Schwelle (min_band) sind nötig, damit getaggt/umbenannt wird — sonst Review.
"""
from __future__ import annotations
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import tags, tagging
from .catalog import is_catalog_only, parse_library_name
from .normalize import normalize_labelcode

AUDIO_EXT = {".mp3", ".m4a", ".flac", ".wav", ".aac"}
_BAND_RANK = {"no_match": 0, "review_low": 1, "review": 2, "auto": 3}
_LABEL_BLOCK = re.compile(r"\s*[-–]?\s*\[[^\]]*\]\s*$")
# LC im Dateinamen: '..._LC24584]' oder 'Title_LC24584' — auch mit Leerzeichen ('LC 24584').
# Auch das verstümmelte 'L 01846' am Ende (manche EDL-Exporte kappen Zeichen: '…_192_L 01846.MP3'):
# ein einzelnes L nach Trenner, direkt gefolgt von genau fünf Ziffern.
_LC_IN_NAME = re.compile(r"(?:LC|(?<=[_\s])L(?=\s?\d{5}(?!\d)))\s?(\d{4,6})", re.IGNORECASE)
# Standard-MP3-Bitraten als End-Marker des Ripper-Schemas '{Titel}_{Album}_{Interpret}_{Bitrate}'.
# Nur exakte Standardwerte, damit Jahre/Track-Nummern nicht fälschlich als Bitrate gelten.
_MP3_BITRATES = frozenset({"32", "40", "48", "56", "64", "80", "96", "112", "128", "160",
                           "192", "224", "256", "320"})


def _parse_underscore_scheme(stem: str) -> tuple[str, str]:
    """'{Titel}_{Album}_{Interpret}_{Bitrate}' (Underscore-Ripper-Schema, Interpret an
    vorletzter Stelle vor der Bitrate) -> (interpret, titel).

    Greift NUR mit Bitrate-End-Marker (sonst ist die Feldreihenfolge zu mehrdeutig,
    z.B. gegen das 'Interpret - Titel'-Schema). Das Album in der Mitte wird für die
    Identifikation ignoriert (Interpret + Titel reichen für den Resolve)."""
    parts = [p.strip() for p in stem.split("_")]
    parts = [p for p in parts if p]                 # führende/doppelte '_' -> leere Felder weg
    if len(parts) < 3 or parts[-1] not in _MP3_BITRATES:
        return "", ""
    fields = parts[:-1]                             # Bitrate abschneiden
    return fields[-1], fields[0]                    # Interpret = letztes Feld, Titel = erstes


# ISRC im Dateinamen ('GBAYE7700001_Titel', 'DE-A12-21-00042 Titel'): Länderkürzel, 3 Zeichen Registrant,
# 2 Ziffern Jahr, 5 Ziffern Nummer. Nicht an Buchstaben/Ziffern angrenzend, sonst greift es mitten in Katalognummern.
_ISRC_IN_NAME = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}-?[A-Z0-9]{3}-?\d{2}-?\d{5})(?![A-Za-z0-9])")


def extract_isrc_from_stem(stem: str) -> tuple[str, str]:
    """(ISRC ohne Bindestriche, Name ohne den ISRC) — ('', stem) wenn keiner drin ist. Der Rest wird
    wie jeder Clip-Name zerlegt; sonst würde der ISRC als Titel oder Interpret gelesen."""
    m = _ISRC_IN_NAME.search(stem or "")
    if not m:
        return "", stem
    rest = (stem[:m.start()] + " " + stem[m.end():]).strip(" _–-")
    return m.group(1).replace("-", ""), re.sub(r"\s{2,}", " ", rest)


def extract_lc_from_stem(stem: str) -> str:
    """Labelcode aus dem Dateinamen ziehen -> normalisierte Ziffern ('' = keiner).

    MUSS vor parse_artist_title_from_stem laufen: das verwirft den Klammerblock
    (und damit den LC) per _LABEL_BLOCK.sub(''). Der Fund ist nur ein VORSCHLAG —
    der Aufrufer prüft ihn gegen den GVL-Master (Dateiname = 1. Quelle, Master = 2.).
    normalize_labelcode füllt <=5 Stellen mit führenden Nullen, 6-stellige bleiben.
    """
    m = _LC_IN_NAME.search(stem)
    return normalize_labelcode(m.group(1)) if m else ""


def parse_artist_title_from_stem(stem: str) -> tuple[str, str]:
    """'Artist - Title - [Label_LCXXXXX]' (Endung bereits entfernt) -> (artist, title).
    Geteilt mit core/edl.py (Clip-Namen aus EDL-Kommentarzeilen sind Dateinamen
    ohne zugehörige Datei — kein ID3-Zugriff möglich, nur der Name selbst)."""
    stem = _LABEL_BLOCK.sub("", stem).strip()
    if " - " in stem:
        a, _, ti = stem.partition(" - ")
        return a.strip(), ti.strip()
    return _parse_underscore_scheme(stem)   # Ripper-Schema '{Titel}_{Album}_{Interpret}_{Bitrate}'


# --- Reicher Cue-Parser für EDL-Clip-Namen aus Schnittsystemen (Avid/DL) ---
# Schnitt-Exporte hängen Endungen/Versions-/Kopier-Suffixe an und mischen Schemata.
# Diese Helfer säubern erst den Namen, ziehen dann LC + Label + Interpret + Titel.
_VERSION_SUFFIX = re.compile(r"\.(new|copy|sub|sync)(\.\d+)*$", re.IGNORECASE)
_FILE_EXT = re.compile(r"\.(mp3|m4a|wav|aiff?|flac|aac|ogg|mov)(?=$|\.)", re.IGNORECASE)
_TRAIL_NUM = re.compile(r"\.\d+$")
_TRACKNUM_PREFIX = re.compile(r"^\d{1,3}\s*-\s*")
# LC-Token samt Trenner UND evtl. angeklebter Endung ('LC 00192WAV', 'TWICE_LC02604').
# KEIN \b vor LC — sonst greift es nicht bei '_LC' (Unterstrich = Wortzeichen, keine Grenze).
_LC_TOKEN = re.compile(r"[_\s–-]*(?:LC|(?<![A-Za-z])L(?=\s?\d{5}(?!\d)))\s?\d{4,6}[A-Za-z]*", re.IGNORECASE)
_BRACKET = re.compile(r"\s*[-–]?\s*\[([^\]]*)\]")
_TRAIL_INSTR = re.compile(r"[_\s]+(instrumental|instr|inst)$", re.IGNORECASE)
# Sync-/Source-Cue: '{Titel} - songs in cinema_From _{Film}' — das Feld nach ' - ' ist Quell-
# Junk, NICHT der Titel. Dann ist das erste Feld der Titel und der Interpret fehlt (Netzsuche).
_SOURCE_JUNK = re.compile(r"songs in cinema|\(source\)|from\s+_", re.IGNORECASE)
# Tokens, die kein Interpret sind (Atmo/SFX) — dann Interpret leer lassen.
_NON_ARTIST = frozenset({"atmo", "atmos", "sfx", "amb", "sound", "sounds"})


def _strip_ext_version(s: str) -> str:
    """Endungen (.mp3/.wav/.m4a…) und Versions-/Kopier-Suffixe (.new.01/.Copy.01/.Sub.01)
    iterativ entfernen — sie treten verschachtelt auf (…mp3.Copy.01.new.01)."""
    prev = None
    while prev != s:
        prev = s
        s = _VERSION_SUFFIX.sub("", s)
        s = _FILE_EXT.sub("", s)
        s = _TRAIL_NUM.sub("", s)
        s = s.strip()
    return s


def parse_cue_from_stem(stem: str) -> dict:
    """Aus einem EDL-Clip-Namen so viel wie möglich extrahieren:
    ``{"interpret", "titel", "labelcode", "label"}`` (leere Strings wo unklar).

    Deckt die gängigen Schnitt-Export-Muster ab: Klammer ``[Label_LCxxxxx]``,
    ``Interpret - Titel`` (mit/ohne inline-LC), Track-Nummer-Prefix ``05 - Titel_Interpret``,
    Underscore-Ripper ``Titel_…_Interpret_Bitrate`` (mit angehängtem ``_LC xxxxx``),
    sowie ``Titel_Interpret_LCxxxxx``. Der LC ist nur ein VORSCHLAG (Aufrufer prüft gegen
    GVL-Master). Interpret bleibt leer, wenn der Name keinen hergibt (dann Netz-/Titelsuche).
    Reine SFX/Atmo/Jingles ohne Schema bleiben als Titel = Restname (nicht auflösbar)."""
    s = _strip_ext_version(stem)
    lc = extract_lc_from_stem(s)
    label = ""
    mb = _BRACKET.search(s)
    if mb:                                          # [Label_LCxxxxx] / [Label_LC]
        inner = mb.group(1)
        s = (s[:mb.start()] + s[mb.end():]).strip()
        lm = re.search(r"^(.*?)[_\s]*LC\s?\d*$", inner, re.IGNORECASE)
        label = (lm.group(1) if lm else inner).strip().strip("_ ").strip()
    s = _LC_TOKEN.sub("", s).strip(" _–-")          # LC-Token (inkl. glued Endung) raus
    s = _TRACKNUM_PREFIX.sub("", s).strip()          # führende Track-Nummer '05 - '
    if is_catalog_only(s):
        # Reine Katalognummer ('CEZ4030_1'): kein Interpret/Titel-Schema — nicht splitten (sonst
        # Interpret '1', Titel 'CEZ4030' und ein Netz-Resolve auf Müll). Der Katalog-Hint löst den Cue.
        return {"interpret": "", "titel": s, "labelcode": lc, "label": label}
    lib = parse_library_name(s)
    if lib:
        # Library-Datei mit Titel im Namen (Library-Schema mit Katalognummer): Titel lesbar, Interpret leer (Komponisten sind
        # kein Interpret), Komponisten-Hinweis und Katalogtext für die Karte; der Katalog-Hint liefert den LC.
        return {"interpret": "", "titel": lib["title"] + (f" ({lib['version']})" if lib["version"] else ""),
                "labelcode": lc, "label": label, "composer_hint": lib["composer_hint"], "library_name": s}

    parts = [p.strip() for p in s.split("_") if p.strip()]
    interpret = titel = ""
    if len(parts) >= 3 and parts[-1] in _MP3_BITRATES:
        # Underscore-Ripper: Titel zuerst, Interpret vor der Bitrate (Album/Quelle dazwischen ignoriert)
        titel, interpret = parts[0], parts[-2]
    elif " - " in s:
        a, _, ti = s.partition(" - ")
        if _SOURCE_JUNK.search(ti):                  # '{Titel} - songs in cinema…' -> LHS ist der Titel
            titel, interpret = a.strip(), ""
        else:                                        # 'Interpret - Titel [- LABEL]'
            interpret = a.strip()
            titel = ti.split(" - ")[0]               # Label-Rest nach zweitem ' - ' abschneiden
        # Titel-Underscore-Junk ('Zzq Beispiel_instrumental', 'Nordwand Lied_instrumental_testtag')
        if "_" in titel:
            titel = titel.split("_")[0]
        titel = _TRAIL_INSTR.sub("", titel).strip(" _–-")
    elif len(parts) == 2:
        titel, interpret = parts[0], parts[1]        # 'Titel_Interpret'
    else:
        titel = s.strip(" _–-")                      # kein Schema erkennbar -> Restname als Titel
    if interpret.lower() in _NON_ARTIST or "sfx" in interpret.lower():
        interpret = ""                               # Atmo/SFX ist kein Interpret
    return {"interpret": interpret, "titel": titel, "labelcode": lc, "label": label}


# Album-/Show-Felder in Underscore-Namen ('ZZQ TESTPROJEKT (SOURCE)', '… (SCORE)'): kein Interpret, kein Titel.
_ALBUMISH = re.compile(r"\((source|score|soundtrack|ost|main title|theme)\)|\bseason\s*\d|\bsoundtrack\b|\bstaffel\s*\d", re.IGNORECASE)


def parse_cue_candidates(stem: str) -> list[tuple[str, str]]:
    """Alle plausiblen (Interpret, Titel)-Paare eines Clip-Namens, das Ergebnis von parse_cue_from_stem
    zuerst. Underscore-Namen sind mehrdeutig — 'Interpret_Titel_192', 'Titel_Album_Interpret_192',
    'Show (SOURCE)_Interpret_Titel_192' kommen alle vor. Der Aufrufer prüft die
    Paare der Reihe nach gegen songs.db und Track-Index (lokal, kein Netz) und nimmt das erste, das trifft;
    trifft keins, bleibt das erste (bisheriges Verhalten)."""
    primary = parse_cue_from_stem(stem)
    cands: list[tuple[str, str]] = [(primary["interpret"], primary["titel"])]
    s = _strip_ext_version(stem)
    s = _LC_TOKEN.sub("", s).strip(" _–-")
    s = _TRACKNUM_PREFIX.sub("", s).strip()
    if is_catalog_only(s):
        return cands
    parts = [p.strip() for p in s.split("_") if p.strip() and p.strip() not in _MP3_BITRATES]
    if len(parts) < 2:
        return cands
    text = [p for p in parts if not _ALBUMISH.search(p)] or parts
    pairs: list[tuple[str, str]] = []
    if len(text) >= 2:
        pairs += [(text[0], text[1]), (text[1], text[0])]
    if len(text) >= 3:
        pairs += [(text[1], text[2]), (text[-1], text[-2]), (text[-1], text[0]), (text[0], text[-1])]
    for a, t in pairs:
        a, t = a.strip(" –-"), t.strip(" –-")
        if a and t and (a, t) not in cands and a.lower() not in _NON_ARTIST and "sfx" not in a.lower():
            cands.append((a, t))
    return cands


def parse_album_from_stem(stem: str) -> str:
    """Album aus dem Underscore-Ripper-Schema '{Titel}_{Album}_{Interpret}_{Bitrate}'
    (mittleres Feld). '' wenn der Name kein Album trägt (z.B. 'Interpret - Titel' oder
    'Titel_Interpret_Bitrate' ohne Album). Bei mehreren Mittelfeldern zusammengefügt."""
    stem = _LABEL_BLOCK.sub("", stem).strip()
    if " - " in stem:
        return ""
    parts = [p.strip() for p in stem.split("_") if p.strip()]
    if len(parts) < 4 or parts[-1] not in _MP3_BITRATES:
        return ""                        # Titel_Album_Interpret_Bitrate braucht ≥4 Felder
    return " ".join(parts[1:-2]).strip()  # zwischen Titel (erstes) und Interpret (vorletztes)


def identify_album(path: str | Path) -> str:
    """Album aus ID3 (TALB), sonst aus dem Dateinamen-Schema. '' wenn keins."""
    path = Path(path)
    t = tags.read_tags(path)
    if t.get("album"):
        return t["album"]
    return parse_album_from_stem(path.stem)


def identify_isrc(path: str | Path) -> str:
    """ISRC aus den Tags (alle vier Formate tragen ihn, s. core/tags.py). '' wenn keiner.
    Speist die ISRC-Stufe-0 des Resolvers — bisher war der ISRC reiner Output."""
    return (tags.read_tags(Path(path)) or {}).get("isrc", "")


def identify(path: str | Path) -> tuple[str, str]:
    """(artist, title) aus ID3 (TPE1/TIT2), sonst Dateiname 'Artist - Title'."""
    path = Path(path)
    t = tags.read_tags(path)
    if t.get("artist") and t.get("title"):
        return t["artist"], t["title"]
    a, ti = parse_artist_title_from_stem(path.stem)
    if a and ti:
        return a, ti
    return t.get("artist", ""), t.get("title", "")


def is_stable(path: str | Path, *, checks: int = 2, interval: float = 0.4, sleep=time.sleep) -> bool:
    """Größe über mehrere Polls unverändert -> Datei fertig kopiert."""
    path = Path(path)
    try:
        last = path.stat().st_size
    except OSError:
        return False
    for _ in range(checks):
        sleep(interval)
        try:
            cur = path.stat().st_size
        except OSError:
            return False
        if cur != last:
            return False
    return True


@dataclass
class WatchResult:
    path: str
    artist: str = ""
    title: str = ""
    identified: bool = False
    band: str = "no_match"
    labelcode: str = ""
    label: str = ""
    action: str = "skip"        # skip | review | applied
    new_path: str = ""
    op_ids: list = field(default_factory=list)
    note: str = ""
    possible_cover: bool = False
    swapped: bool = False       # Interpret/Titel kamen vertauscht an (s. resolver.SWAP_NOTE)


def process_one(path, *, resolve_fn, label_lookup, op_con,
                apply: bool = False, min_band: str = "auto", move_to=None, recognize=None) -> WatchResult:
    """resolve_fn(a,t,isrc)->ResolveResult; label_lookup(code)->(label, rr);
    recognize(path, expected_artist, expected_title)->{artist,title,possible_cover,...}|None.
    Der ISRC kommt aus den Datei-Tags und speist die Stufe 0 des Resolvers."""
    path = Path(path)
    artist, title = identify(path)
    possible_cover = False
    if not (artist and title) and recognize:        # Fallback: Audio-Fingerprinting (AudD)
        info = recognize(path, artist, title)
        if info and info.get("artist") and info.get("title"):
            possible_cover = bool(info.get("possible_cover"))
            artist, title = info["artist"], info["title"]
    res = WatchResult(path=str(path), artist=artist, title=title, possible_cover=possible_cover)
    if not (artist and title):
        res.note = "nicht identifizierbar (keine Tags/Name) — Fingerprinting nötig (AudD-Token in Settings)"
        return res
    res.identified = True

    r = resolve_fn(artist, title, identify_isrc(path))
    if getattr(r, "swapped", False) and r.artist and r.title:
        # Der Resolver hat Interpret/Titel vertauscht aufgelöst (DL-/Sync-Export).
        # Ohne diese Übernahme trüge die Datei den richtigen LC, aber weiter den
        # falschherum gebauten Namen 'Titel - Interpret - [Label_LCxxxxx]'.
        artist, title = r.artist, r.title
        res.artist, res.title, res.swapped = artist, title, True
    res.band = r.band
    best = r.best
    if not best:
        res.action = "review"
        res.note = "kein GVL-Label gefunden"
        return res
    res.labelcode, res.label = best.labelcode, best.label

    if apply and _BAND_RANK.get(r.band, 0) >= _BAND_RANK.get(min_band, 3):
        label, rr = label_lookup(best.labelcode)
        isrc = r.isrcs[0] if r.isrcs else ""
        pl = tagging.plan(path, artist, title, best.labelcode, label or best.label,
                          isrc=isrc, rechterueckrufe=rr)
        out = tagging.apply(pl, op_con=op_con, confirm=True, move_to=move_to)
        res.action, res.new_path, res.op_ids = "applied", out["new_path"], out["op_ids"]
        res.note = f"rechterueckruf_pending: {rr}" if rr else ""
    else:
        res.action = "review"
        res.note = (f"Band {r.band} unter Schwelle {min_band} — Review (kein Auto-Write)"
                    if apply else "Preview-Modus (kein --apply)")
    return res
