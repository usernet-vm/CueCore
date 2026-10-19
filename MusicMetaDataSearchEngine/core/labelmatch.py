"""Label-Name -> GVL-Labelcode-Matching. Single Source of Truth.

Geteilt von process_folder.py (Pipeline) und dem LC-Resolver (App-Backend).
Logik 1:1 aus der bewährten Pipeline gehoben: alias -> exakter Voll-Match ->
fuzzy (suffix-bereinigt) -> Universal-Fallback -> genau-ein-Major-Token-Fallback.
Gibt den Labelcode-String (ohne 'LC') zurück oder None (uneindeutig / kein Treffer).

Niemals labelcode als Integer behandeln (führende Nullen).
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# --- verifizierte Sonderfaelle (normalisierter Tag -> Labelcode) ---
# Greift VOR der CSV/DB-Suche. Deckt abweichende Schreibweisen & Distributor-/Album-LCs ab.
ALIASES: dict[str, str] = {
    "4ad": "05807",
    "awal": "30596", "awal recordings": "30596", "awal recordings ltd": "30596",
    "harbour artists music": "30596", "world in red": "30596", "mxmtoon": "30596",
    "young": "34645", "young turks": "34645", "young turks recordings": "34645",
    "nettwerk": "11269", "nettwerk music group": "11269",
    "nettwerk music group inc": "11269", "nettwerk records inc": "11269",
    "warner records": "00392",
    "universal island records ltd": "00407", "island": "00407",
    "rca victor": "00316",
    # „BMG“ allein bleibt bewusst offen: bis 2008 BMG Entertainment (heute Sony-Katalog, LC 01413), seit 2008
    # BMG Rights Management (LC 19813) — der Tag sagt nicht, welches gemeint ist.
    "bmg rights management": "19813",
    "cosmos music": "25145", "cosmos": "25145",
    "fueled by ramen": "00121",
    "artist house": "14337", "artisthouse": "14337",
    "amf records": "08419",
    "doddleoddle": "23398",
    "sony bmg catalog": "10879",
    "secret road records": "73208", "secret road music services": "73208",
    "harvest records": "01305",
    "darkroom": "06406", "darkroom interscope": "06406", "opium interscope": "06406",
    "domino recording co": "10192", "domino recording co ltd": "10192",
    "mom and pop": "101515", "mom and pop music": "101515",
    "columbia": "00162", "atlantic": "00121",
    "rhino warner": "02982", "chop shop atlantic": "00121",
    "deutsche grammophon": "00173", "deutsche grammophon dg": "00173",
    "syco music": "29644", "syco music uk": "29644",
    "gamma": "103326", "rounder": "03719",
    # Production-Music-Libraries: Sub-Label-Namen aus den Library-Listen (Extreme Music v2,
    # KPM 2022-05), deren Code im GVL-Master unter ANDEREM Namen steht (El Dorado = A-Tone,
    # Vortrax = Spintrest, Extreme Unique Library = The Extreme Music Library, KPM Main = KPM Music).
    "el dorado": "52577", "vortrax": "51024", "extreme unique library": "01732", "hype music": "51049",
    "kpm main series": "02061", "kpm international": "02061",
}

# Letzter Fallback: enthaelt der Tag-String GENAU EIN bekanntes Major-Imprint-Token?
# (faengt Compound-/Lizenz-Strings wie '... under exclusive license to Interscope Records')
MAJOR_TOKENS: dict[str, str] = {
    "interscope": "06406", "island": "00407", "atlantic": "00121", "columbia": "00162",
    "republic": "07552", "capitol": "00148", "polydor": "00309", "parlophone": "00299",
    "geffen": "07266", "epic": "00199", "virgin": "03098", "mercury": "00268",
    "decca": "00171", "rhino": "02982", "rca": "00316",
}

# GVL-`hersteller`-Marker der drei Majors (+ Universal-Klassik DG), normalisiert.
# Trennt das gleichnamige Indie vom Major-Imprint (Republic-Fall):
# '07552 REPUBLIC' (Universal Music Entertainment GmbH) vs '57251 Republic Records'
# (Z Records Ltd). Der exakte Voll-Match liefe sonst aufs Indie.
MAJOR_HERSTELLER: tuple[str, ...] = (
    "universal music", "warner music", "sony music", "deutsche grammophon", "bmg rights management",
)

GENERIC = re.compile(
    r"\b(records?|recordings?|recording|music|musik|ltd|limited|gmbh|inc|llc|label|the|co"
    r"|uk|us|usa|gb|germany|deutschland|france|australia|norway|sweden|europe|european"
    r"|international|intl|pty)\b")


_PAREN = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
# Mehrere Labels in einem Tag: 'BMG / Mute', 'Kobalt, BMG', '… under exclusive license to Interscope'.
_COMPOUND = re.compile(r"\s*(?:/|;|,|\bunder (?:exclusive )?licen[cs]e to\b|\bin association with\b)\s*", re.IGNORECASE)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold()
    s = s.replace("&", " and ").replace("+", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_generic(n: str) -> str:
    return re.sub(r"\s+", " ", GENERIC.sub(" ", n)).strip()


@dataclass
class LabelIndex:
    """Normalisierte Such-Indizes über den GVL-Master (aus CSV oder labels.db)."""
    code2name: dict[str, str] = field(default_factory=dict)
    code2hersteller: dict[str, str] = field(default_factory=dict)  # Code -> Tonträgerhersteller
    full: dict[str, set[str]] = field(default_factory=dict)       # norm(label) -> {codes}
    stripped: dict[str, set[str]] = field(default_factory=dict)   # strip_generic(norm) -> {codes}
    # GVL-`kurzname` (Katalog-Kürzel wie CEZ/KPM/UPM) -> Code. NUR eindeutige Kürzel — 27 sind
    # im Master mehrfach vergeben (ARI, GRAM, ELITE …), die werden verworfen, nicht geraten.
    short2code: dict[str, str] = field(default_factory=dict)
    # Sub-Label-Kürzel aus den Library-Listen (data/library_prefixes.csv): normalisiertes
    # Kürzel -> {labelcode, library, prefix}. Hat VORRANG vor short2code (feiner: XCD statt nur EXTREME).
    prefix2code: dict[str, dict] = field(default_factory=dict)
    hersteller: dict[str, set[str]] = field(default_factory=dict)  # norm(hersteller) -> {codes}
    hersteller_name: dict[str, str] = field(default_factory=dict)  # norm(hersteller) -> Original
    # Labelname ohne Leerzeichen -> Code (nur eindeutig): 'The Nerve' -> THENERVE, weil Libraries
    # den Labelnamen selbst als Katalog-Kürzel benutzen (APL: THENERVE333).
    squash2code: dict[str, str] = field(default_factory=dict)
    # MusicBrainz-Label-Namen und -Aliasse (data/label_aliases_mb.csv, build_label_aliases_mb.py):
    # norm(name) bzw. strip_generic davon -> Code. Nur GVL-geprüfte Codes, nur eindeutige Schlüssel.
    # Dritte Quelle NACH GVL-Master und ALIASES — greift erst, wenn der Master nichts hergibt.
    mb_alias: dict[str, str] = field(default_factory=dict)
    # Labelname ohne Klammerzusatz -> {codes}: 'Rise Records (BMG)' ist im Tag nur 'Rise Records'.
    # Greift erst, wenn der volle Name nichts trifft; mehrdeutig -> nicht raten.
    paren_free: dict[str, set[str]] = field(default_factory=dict)


def build_index(rows: Iterable[tuple]) -> LabelIndex:
    """rows = (labelcode, label[, hersteller[, kurzname]]). labelcode bleibt String.
    hersteller optional (2-Tupel weiterhin gültig) — Major/Indie-Disambig + TPUB-Abgleich.
    kurzname optional — Katalog-Kürzel-Index (nur eindeutige Kürzel überleben)."""
    idx = LabelIndex()
    short_seen: dict[str, set[str]] = {}
    for row in rows:
        code, label = row[0], row[1]
        idx.code2name[code] = label
        if len(row) > 2 and row[2]:
            hn = norm(row[2])
            if hn:
                idx.hersteller.setdefault(hn, set()).add(code)
                idx.hersteller_name.setdefault(hn, row[2])
        if len(row) > 3 and row[3]:
            short_seen.setdefault(str(row[3]).strip().upper(), set()).add(code)
        if len(row) > 2 and row[2]:
            idx.code2hersteller[code] = row[2]
        n = norm(label)
        idx.full.setdefault(n, set()).add(code)
        pf = norm(_PAREN.sub(" ", label))
        if pf and pf != n:
            idx.paren_free.setdefault(pf, set()).add(code)
        sn = strip_generic(n)
        if sn:
            idx.stripped.setdefault(sn, set()).add(code)
    for short, codes in short_seen.items():
        if len(codes) == 1:
            idx.short2code[short] = next(iter(codes))
    squash_seen: dict[str, set[str]] = {}
    for n, codes in idx.full.items():
        squash_seen.setdefault(n.replace(" ", "").upper(), set()).update(codes)
    idx.squash2code = {k: next(iter(v)) for k, v in squash_seen.items() if len(v) == 1 and len(k) >= 4}
    return idx


def build_index_from_db(con: sqlite3.Connection) -> LabelIndex:
    """Index aus der App-Label-DB (Tabelle labels). Einmal beim Backend-Start laden. Manuelle Labels zuerst,
    GVL-Zeilen danach: code2name nimmt die zuletzt gelesene Zeile, und ein zweites, von Hand angelegtes Label
    unter einem GVL-Code darf den GVL-Namen nicht verdrängen."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(labels)")}
    order = " ORDER BY (source<>'manuell'), rowid" if "source" in cols else ""
    return build_index(
        (str(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or ""))
        for r in con.execute("SELECT labelcode, label, hersteller, kurzname FROM labels" + order)
    )


def attach_library_prefixes(idx: LabelIndex, path) -> int:
    """data/library_prefixes.csv in den Index hängen — nur Kürzel, deren Code der Index kennt
    (Master + Supplemental). Rückgabe: Anzahl übernommener Kürzel."""
    from .catalog import load_library_prefixes
    idx.prefix2code = {k: v for k, v in load_library_prefixes(path).items() if v["labelcode"] in idx.code2name}
    return len(idx.prefix2code)


def attach_mb_aliases(idx: LabelIndex, path) -> int:
    """data/label_aliases_mb.csv in den Index hängen — nur Codes, die der Index kennt. Schlüssel sind
    norm(name) und strip_generic davon; zeigt ein Schlüssel auf mehrere Codes, fliegt er (nicht raten).
    Fehlt die Datei -> leer. Rückgabe: Anzahl Schlüssel."""
    import csv
    idx.mb_alias = {}
    p = Path(path)
    if not p.exists():
        return 0
    keys: dict[str, set[str]] = {}
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = (row.get("labelcode") or "").strip()
            if code not in idx.code2name:
                continue
            n = norm(row.get("name") or "")
            for k in {n, strip_generic(n)}:
                if k:
                    keys.setdefault(k, set()).add(code)
    idx.mb_alias = {k: next(iter(v)) for k, v in keys.items() if len(v) == 1}
    return len(idx.mb_alias)


def catalog_lookup(prefix: str, idx: LabelIndex) -> tuple[str, str]:
    """Katalog-Kürzel -> (Labelcode, Quellen-Text) oder ('', ''). Erst die Library-Listen
    (Sub-Label, z.B. 'Library-Kürzel XCD (Extreme Music)'), dann der GVL-`kurzname`
    ('GVL-Kürzel CEZ'). Mehrdeutige Kürzel stehen in keiner der beiden Tabellen."""
    from .catalog import normalize_prefix
    key = normalize_prefix(prefix)
    if not key:
        return "", ""
    hit = idx.prefix2code.get(key)
    if hit:
        return hit["labelcode"], f"Library-Kürzel {hit['prefix']} ({hit['library']})"
    code = idx.short2code.get(key, "") or idx.short2code.get((prefix or "").strip().upper(), "")
    if code:
        return code, f"GVL-Kürzel {(prefix or '').strip().upper()}"
    code = idx.squash2code.get(key, "")
    return (code, f"Kürzel {(prefix or '').strip().upper()} = Labelname") if code else ("", "")


def hersteller_scope(word: str, idx: LabelIndex) -> set[str]:
    """Codes aller Labels eines Herstellers, dessen Name mit `word` beginnt ('APL' -> APL
    Publishing GmbH). Leer, wenn kein oder mehr als ein Hersteller passt."""
    w = norm(word)
    if len(w) < 3:
        return set()
    hits = [hn for hn in idx.hersteller if hn == w or hn.startswith(w + " ")]
    return set(idx.hersteller[hits[0]]) if len(hits) == 1 else set()


def match_label_in_scope(name: str, idx: LabelIndex, scope: set[str]) -> str:
    """Labelname, der als PRÄFIX im Tag-Text steckt ('The Nerve Media Group Ltd' trägt 'The Nerve'),
    beschränkt auf die Codes eines Herstellers (scope). Eindeutig oder ''."""
    n = strip_generic(norm(name))
    if not n or not scope:
        return ""
    hits = set()
    for code in scope:
        ln = strip_generic(norm(idx.code2name.get(code, "")))
        if len(ln) >= 4 and (n == ln or n.startswith(ln + " ")):
            hits.add(code)
    return next(iter(hits)) if len(hits) == 1 else ""


def catalog_code(prefix: str, idx: LabelIndex) -> str:
    """Nur der Code aus catalog_lookup (Library-Listen vor GVL-kurzname), '' wenn unbekannt."""
    return catalog_lookup(prefix, idx)[0]


def match_hersteller(name: str, idx: LabelIndex) -> str:
    """Label-/TPUB-Tag gegen die GVL-Hersteller: exakter normalisierter Treffer, sonst
    Präfix-Enthaltensein ab 8 Zeichen, wenn genau EIN Hersteller passt ('Frederic Leibovitz
    Editeur' ⊂ 'Frederic Leibovitz Editeur Sarl.'). Rückgabe: Original-Schreibweise oder ''."""
    n = norm(name)
    if not n:
        return ""
    if n in idx.hersteller_name:
        return idx.hersteller_name[n]
    if len(n) < 8:
        return ""
    # Präfix nur an Wortgrenzen und auf BEIDEN Seiten mindestens 8 Zeichen — sonst wird
    # 'The Nerve Media Group Ltd' zum GVL-Hersteller 'THE' (den gibt es wirklich).
    hits = [hn for hn in idx.hersteller_name
            if len(hn) >= 8 and (hn.startswith(n + " ") or n.startswith(hn + " "))]
    return idx.hersteller_name[hits[0]] if len(hits) == 1 else ""


def _is_major_hersteller(h: str) -> bool:
    if not h:
        return False
    hn = norm(h)
    return any(m in hn for m in MAJOR_HERSTELLER)


def _major_imprint_override(sn: str, exact_code: str, idx: LabelIndex) -> str | None:
    """Gleichnamiges Indie vs Major-Imprint (Republic-Fall): wenn der gestrippte Kern
    GENAU ein Major-Imprint-Token ist, der exakte Treffer aber KEIN Major-Hersteller,
    und der Major-Code dieses Tokens von einem Major-Hersteller stammt -> Major.
    Sonst None (exakter Treffer bleibt). Kein Raten: greift nur bei echter Namensdublette.
    """
    major = MAJOR_TOKENS.get(sn)
    if not major or major == exact_code:
        return None
    if _is_major_hersteller(idx.code2hersteller.get(exact_code, "")):
        return None  # exakter Treffer ist selbst Major -> unangetastet
    if _is_major_hersteller(idx.code2hersteller.get(major, "")):
        return major
    return None


def match_label(label: str, idx: LabelIndex) -> str | None:
    """Offizieller Labelcode (String, ohne 'LC') oder None (uneindeutig / kein Treffer)."""
    n = norm(label)
    sn = strip_generic(n)
    if n in ALIASES:
        return ALIASES[n]
    if sn in ALIASES:
        return ALIASES[sn]
    # exakter Voll-Match hat Vorrang (hohe Praezision)
    if n in idx.full and len(idx.full[n]) == 1:
        code = next(iter(idx.full[n]))
        return _major_imprint_override(sn, code, idx) or code
    # Name ohne Klammerzusatz im Master ('Rise Records (BMG)'), nur wenn der volle Name selbst unbekannt ist
    if n not in idx.full and len(idx.paren_free.get(n, ())) == 1:
        return next(iter(idx.paren_free[n]))
    # sonst fuzzy (Suffix-bereinigt)
    cands: set[str] = set()
    if sn in idx.full:
        cands |= idx.full[sn]
    if n in idx.stripped:
        cands |= idx.stripped[n]
    if sn in idx.stripped:
        cands |= idx.stripped[sn]
    if len(cands) == 1:
        return next(iter(cands))
    if len(cands) > 1:
        return None  # echtes 1:n -> nicht raten
    # Dritte lokale Quelle: MusicBrainz-Namen/-Aliasse (nur GVL-geprüfte Codes, s. attach_mb_aliases) —
    # erst jetzt, wenn der GVL-Master selbst nichts hergibt; ein GVL-Treffer wird nie überstimmt.
    code = idx.mb_alias.get(n) or idx.mb_alias.get(sn)
    if code:
        return code
    # Zusammengesetzter Tag: jeden Teil einzeln — nur wenn genau EIN Code herauskommt
    parts = [p for p in _COMPOUND.split(label or "") if p and p.strip()]
    if len(parts) > 1:
        codes = {c for c in (match_label(p, idx) for p in parts) if c}
        if len(codes) == 1:
            return next(iter(codes))
        if len(codes) > 1:
            return None
    # Fallback 1: uneindeutiges Universal (auch UME) -> Universal Sammellabel
    if re.search(r"\b(universal|ume)\b", n):
        return "97777"
    # Fallback 2: genau ein bekanntes Major-Token im String
    hits = {code for tok, code in MAJOR_TOKENS.items() if re.search(rf"\b{re.escape(tok)}\b", n)}
    if len(hits) == 1:
        return next(iter(hits))
    return None
