"""EDL-Import-Orchestrierung: Cross-Check gegen songs.db,
Schreibback nach songs.db bei Erfolg über die Identification Pipeline, Excel-Export.

cross_check() ist netzfrei testbar — resolve_fn wird injiziert (dieselbe DI wie
core/resolver.py/app.py). Schreibt nur nach songs.db, NIE ID3/Rename — das ist
ausschließlich Aufgabe von core/tagging.py auf echten Dateien, EDL-Events
referenzieren nur Clip-Namen, keine garantiert vorhandenen Dateien.
"""
from __future__ import annotations
import functools
import re
from pathlib import Path

from openpyxl import Workbook

from . import songsdb, watch
from .edl import EDLEvent, dedupe_multitrack, merge_contiguous
from .normalize import export_lc, normalize_labelcode


def clip_basename(name: str) -> str:
    """Dateiname ohne Ordner. SOURCE FILE trägt bei Premiere/Resolve oft den ganzen Pfad
    (auch Windows-Backslashes); der Ordner darf weder als Interpret noch als Dateiname
    an Library-APIs gehen."""
    return re.split(r"[\\/]", (name or "").strip())[-1].strip()

# Rechterückruf direkt nach dem LC: R-Codes müssen im Tool sichtbar sein, auch im Export.
EXPORT_COLUMNS = ("Interpret", "Titel", "Komponist", "Texter", "Verlag", "Label", "LC", "Rechterückruf", "ISRC",
                  "Länge Sek.")
_SUM_PAD = [""] * (len(EXPORT_COLUMNS) - 2)      # Gesamtdauer-Zeile: Leerzellen bis 'Gesamtdauer:' + Wert

# Bekannte Stem-Bezeichner. Nur wenn das ABSCHLIESSENDE Segment eines Titels genau so
# ein Wort ist, wird es als Stem-Marker entfernt (konservativ — echte Titel wie
# „(Live)"/„- Reprise" bleiben unangetastet).
_STEM_TOKENS = frozenset({
    "drums", "drum", "bass", "vocal", "vocals", "vox", "gtr", "guitar", "guitars",
    "keys", "key", "synth", "synths", "perc", "percussion", "fx", "sfx", "inst",
    "instrumental", "instr", "stem", "stems", "dry", "wet", "sub", "music", "mix",
    "click", "strings", "brass", "pad", "pads", "lead", "backing", "choir", "piano",
    "horns", "organ", "bells", "loop", "beat", "beats", "acapella", "accapella",
})
_STEM_PAREN = re.compile(r"^(.*?)[\s_]*[\(\[]\s*(?:stem\s+)?([A-Za-zÄÖÜäöüß]+)\s*\d*\s*[\)\]]\s*$")
_STEM_SUFFIX = re.compile(r"^(.*?\S)\s*[-–_]\s*(?:stem\s+)?([A-Za-zÄÖÜäöüß]+)\s*\d*\s*$")


def strip_stem(title: str) -> str:
    """Abschließende Stem-Marker konservativ entfernen — nur wenn das letzte Segment
    ein bekanntes Stem-Wort ist: „Sunrise (Drums)" / „Sunrise - Bass 2" -> „Sunrise".
    „Sunrise (Live)" / „Track - Reprise" bleiben unverändert (kein Stem-Wort)."""
    t = (title or "").strip()
    while True:
        new = t
        for rx in (_STEM_PAREN, _STEM_SUFFIX):
            m = rx.match(new)
            if m and m.group(2).lower() in _STEM_TOKENS and m.group(1).strip():
                new = m.group(1).strip(" _-–")
                break
        if new == t:
            break
        t = new
    return t or (title or "").strip()


def merge_cards(cards: list[dict], *, merge_identical: bool = False,
                unify_stems: bool = False) -> list[dict]:
    """Cue-Karten für die Rechteabrechnung zusammenfassen (Reihenfolge bleibt erhalten):
      merge_identical — Cues mit gleichem Interpret+Titel werden EINE Zeile, Dauer summiert.
                        Für Cues OHNE lesbaren Interpret/Titel (z. B. Production-Music-Dateinamen
                        wie „ZZQ_MUSIKLOOP_MUSIK_02") wird stattdessen nach dem Clip-Namen
                        gruppiert — gleiche Dateinamen werden EINE Zeile, Dauer summiert.
      unify_stems     — Stem-Varianten desselben Tracks (Drums/Bass/…) werden auf den
                        Basistitel vereinheitlicht und dann zusammengefasst.
    `merged_count` zählt die zusammengefassten Nutzungen. Cues ganz ohne Clip-Namen (Lücken)
    bleiben einzeln."""
    if not (merge_identical or unify_stems):
        return cards
    groups: dict[tuple, dict] = {}
    result: list[dict] = []
    for c in cards:
        artist = (c.get("artist") or "").strip()
        title = (c.get("title") or "").strip()
        clip = (c.get("clip_name") or "").strip()
        if artist and title:
            base = strip_stem(title) if unify_stems else title
            key: tuple = ("at", artist.lower(), base.lower())
            new_title = base if unify_stems else title
        elif merge_identical and clip:
            # Unlesbarer/kein Interpret+Titel: nach Dateiname gruppieren (Production Music,
            # Loops, Atmos …). Dauer summiert, Anzeige über den Clip-Namen.
            key = ("clip", clip.lower())
            new_title = None
        else:
            result.append(c)                # kein Clip-Name (Lücke) -> einzeln
            continue
        g = groups.get(key)
        if g is None:
            merged = dict(c)
            if new_title is not None:
                merged["title"] = new_title
            merged["duration_sec"] = c.get("duration_sec", 0) or 0
            merged["merged_count"] = 1
            groups[key] = merged
            result.append(merged)           # per Referenz — spätere Merges mutieren in place
        else:
            g["duration_sec"] = (g.get("duration_sec", 0) or 0) + (c.get("duration_sec", 0) or 0)
            g["merged_count"] += 1
            for k in ("labelname", "labelcode", "komponist", "texter", "verlag", "isrc"):
                if not g.get(k) and c.get(k):
                    g[k] = c[k]
    return result


def _export_rows(cards: list[dict]) -> tuple[list[list], int]:
    """(Zeilen in EXPORT_COLUMNS-Reihenfolge, Gesamtdauer). Geteilt von xlsx/csv/pdf. Der LC steht immer als
    'LC' + Ziffern, auch wenn die Karte ihn ohne Präfix trägt (Dateiname, Seed, Katalog)."""
    rows, total = [], 0
    for c in cards:
        d = c.get("duration_sec", 0) or 0
        total += d
        rows.append([c.get("artist", ""), c.get("title", ""), c.get("komponist", ""),
                     c.get("texter", ""), c.get("verlag", ""), c.get("labelname", ""),
                     export_lc(c.get("labelcode")), c.get("rechterueckrufe") or "", c.get("isrc", ""), d])
    return rows, total


def attach_rights(cards: list[dict], label_lookup=None) -> None:
    """Jede Cue-Karte trägt `rechterueckrufe` ('R6;R12' oder '') aus der Label-Datenbank zum LC.
    Immer aus dem aktuellen LC neu bestimmt, denn der ändert sich nach dem Import (Geschwister, Liste,
    Zusammenfassen, Editor). Ohne label_lookup bleibt ein vorhandener Wert stehen."""
    seen: dict[str, str] = {}
    for c in cards:
        if label_lookup is None:
            c.setdefault("rechterueckrufe", "")
            continue
        code = normalize_labelcode(c.get("labelcode") or "")
        if code and code not in seen:
            seen[code] = label_lookup(code)[1] or ""
        c["rechterueckrufe"] = seen.get(code, "")


def _take(card: dict, found: dict) -> None:
    """Felder einer Fundstelle (songs.db-Zeile, Library-Datei, Resolver) auf die Karte, außer der Dauer: die
    GEMA-Dauer gehört dem Cue und kommt nur aus der Schnittliste. songs.duration_sec ist die Tracklänge einer
    Lieferung oder (Altbestand) die Cue-Dauer eines früheren Projekts und taugt deshalb nicht als Cue-Dauer."""
    card.update({k: v for k, v in found.items() if v not in (None, "") and k != "duration_sec"})


def _apply_catalog(card: dict, cand) -> None:
    """Katalog-Kürzel-Kandidat (GVL-Kurzname) in die Karte: LC + offizielles Label als
    Vorschlag, Status `catalog_hint` (review — der Mensch bestätigt)."""
    card["labelcode"], card["labelname"] = cand.labelcode, cand.label
    card["catalog_hint"] = cand.sources[0] if cand.sources else "Katalog-Kürzel"
    card["status"] = "catalog_hint"


# Versions-Zusätze, die im Titel stehen, aber keine eigene Aufnahme mit eigenem LC sind:
# '(Instrumental)', '(Bonus Track)', 'Instrumental Guitar', '(Radio Edit)' … -> Basistitel.
_VERSION_MARK = re.compile(
    r"\s*[\(\[][^\)\]]*\b(instrumental|instr|inst|bonus|radio edit|edit|remaster(ed)?|live|acoustic|demo|version|"
    r"mix|remix|score|karaoke|extended|single|album|mono|stereo|clean|explicit)\b[^\)\]]*[\)\]]\s*$", re.IGNORECASE)
_TRAIL_VERSION = re.compile(r"[\s_\-]+(instrumental(\s+(guitar|piano|version|mix))?|instr|inst|acoustic|remix|radio edit|live|karaoke)\s*$",
                            re.IGNORECASE)


def base_title(title: str) -> str:
    """Titel ohne Versions-Zusatz: 'NORDWAND (INSTRUMENTAL)' -> 'NORDWAND',
    'NORDLICHT (BONUS TRACK) (INSTRUMENTAL)' -> 'NORDLICHT', 'ZZQ SONG INSTRUMENTAL GUITAR' -> 'ZZQ SONG'."""
    t, prev = (title or "").strip(), None
    while prev != t:
        prev = t
        t = _VERSION_MARK.sub("", t).strip()
        t = _TRAIL_VERSION.sub("", t).strip()
    return t


_DONOR_STATUS = frozenset({"found_in_db", "index_hit", "resolved_and_written", "library_matched", "list_lc", "sibling_hit"})
_NEEDY_STATUS = frozenset({"unresolved", "filename_lc", "missing_no_resolver", "unparsable_clip_name"})


def inherit_from_siblings(cards: list[dict], con=None, local_fn=None) -> int:
    """Gleicher Interpret + gleicher Basistitel in derselben Schnittliste: wird eine Fassung gefunden, erben
    die anderen ('NORDWAND' im Index, 'NORDWAND (INSTRUMENTAL)' nicht). Ohne Geschwister-Treffer wird der Basistitel selbst
    in songs.db und Index gesucht. Das Ergebnis bleibt ein VORSCHLAG (Status sibling_hit, Ampel gelb) —
    ein LC aus dem Dateinamen, der abweicht, bleibt als Notiz sichtbar. Rückgabe: Zahl der geerbten Cues."""
    from .labelmatch import norm
    groups: dict[tuple[str, str], list[dict]] = {}
    for c in cards:
        if c.get("artist") and c.get("title"):
            groups.setdefault((norm(c["artist"]), norm(base_title(c["title"]))), []).append(c)
    n = 0
    for grp in groups.values():
        donors = [c for c in grp if c.get("labelcode") and c.get("status") in _DONOR_STATUS]
        needy = [c for c in grp if c.get("status") in _NEEDY_STATUS]
        if not needy:
            continue
        donor = donors[0] if donors else None
        if donor is None:
            c0 = needy[0]
            bt = base_title(c0["title"])
            if bt and bt.lower() != c0["title"].lower():
                hit = songsdb.find(con, c0["artist"], bt) if con is not None else None
                src = hit
                if not src and local_fn is not None:
                    r = local_fn(c0["artist"], bt)
                    src = r if (r and r.get("labelcode")) else None
                if src:
                    donor = {**src, "event_num": None, "title": bt}
        if donor is None:
            continue
        for c in needy:
            own_lc = c.get("labelcode") or ""
            for k in ("labelname", "labelcode", "komponist", "texter", "verlag", "isrc"):
                if donor.get(k) and (k in ("labelname", "labelcode") or not c.get(k)):
                    c[k] = donor[k]
            c["status"] = "sibling_hit"
            c["sibling_of"] = donor.get("event_num") or ""
            c["sibling_title"] = donor.get("title") or ""
            if own_lc and own_lc != c["labelcode"]:
                c["note"] = (c.get("note") + " · " if c.get("note") else "") + f"Dateiname nennt LC{own_lc}"
            n += 1
    return n


def _pick_candidate(cands, con, local_fn):
    """Erstes (Interpret, Titel)-Paar, das lokal trifft: songs.db vor Track-Index. Rückgabe
    (artist, title, db_hit, index_hit) — ohne Treffer das erste Paar, beide Hits None."""
    for a, t in cands:
        if a and t:
            h = songsdb.find(con, a, t)
            if h:
                return a, t, h, None
    if local_fn is not None:
        for a, t in cands:
            if a and t:
                r = local_fn(a, t)
                if r and (r.get("labelcode") or r.get("ambiguous")):
                    if r.get("swapped"):             # der Index fand das Paar nur vertauscht -> Karte richtig herum
                        a, t = t, a
                    return a, t, None, r
    a, t = cands[0]
    return a, t, None, None


def cross_check(events: list[EDLEvent], con, *, resolve_fn=None, label_lookup=None,
                fps: float = 25.0, dedupe: bool = True, catalog_fn=None, local_fn=None,
                merge_split_edits: bool = True) -> list[dict]:
    """Pro EDL-Event: Clip-Name -> Interpret/Titel/LC/Label (`watch.parse_cue_from_stem`,
    kein ID3-Zugriff möglich, s. core/edl.py), gegen songs.db prüfen.

    - `label_lookup(code) -> (label, rr)`: ein im Dateinamen gefundener LC wird gegen den
      GVL-Master geprüft und das OFFIZIELLE Label (+ Rechterückruf) direkt vorbefüllt —
      auch ohne Netz (Domain-Regel: Dateiname = 1. Quelle, Master = 2.).
    - `resolve_fn(artist, title)`: bei Interpret+Titel ohne DB-Treffer die Identification
      Pipeline; Erfolg wird als songs.db-Eintrag (source_type='edl_import') zurückgeschrieben.
      Cues ohne Interpret (nur Titel, z.B. Sync-Cues) werden NICHT geraten — Titelsuche/
      manuelle Recherche pro Karte.

    - `catalog_fn(text) -> Candidate|None`: Library-Clips heißen oft nur nach Katalognummer
      (`CEZ4030_1`). Bleibt der Cue sonst unlesbar/ungelöst, liefert das GVL-Kurzname-Mapping
      LC + Label als Vorschlag (Status `catalog_hint`, kein Auto-Confirm).

    dedupe=True (Default): ein Clip auf Video + mehreren Audiospuren zählt als EIN Cue.
    merge_split_edits=True (Default): lückenlos aneinanderliegende Stücke desselben Clips (Split-Edit) sind
    EINE Nutzung, aufgerundet wird die Gesamtdauer (s. edl.merge_contiguous).

    Online gefundene Treffer gehen mit review_status='review' nach songs.db: beim nächsten Import sind sie
    „In DB, ungeprüft“ (gelb), bis der Mensch die Zeile im Editor speichert (update_song -> verified).
    """
    if dedupe:
        events = dedupe_multitrack(events)
    if dedupe and merge_split_edits:                 # dedupe=False heißt: jedes Event bleibt eine Zeile
        events = merge_contiguous(events, fps)
    results: list[dict] = []
    for ev in events:
        duration = ev.gema_duration_sec(fps)
        card: dict = {"event_num": ev.event_num, "clip_name": ev.clip_name,
                      "artist": "", "title": "", "labelname": "", "labelcode": "",
                      "duration_sec": duration, "status": "", "record_in": ev.record_in}
        if not ev.clip_name:
            card["status"] = "no_clip_name"
            results.append(card)
            continue

        stem = clip_basename(ev.clip_name)
        # Library-Lieferung zuerst: der Dateiname („CEZ4030_1“) ist bei Library-Clips der Schlüssel, nicht
        # Interpret+Titel — Treffer sind verifiziert, kein Parse, kein Netz.
        fhit = songsdb.find_by_file(con, stem)
        if fhit:
            _take(card, fhit)
            card["artist"], card["title"] = fhit["interpret"], fhit["titel"]
            card["status"], card["matched_by"] = "found_in_db", "file"
            if not card["labelcode"] and catalog_fn is not None:
                # Lieferung ohne Code (z. B. Libraries ohne LC in der Lieferung): der Katalog-Vorschlag gilt wie bei einer unbekannten
                # Datei — erst das Kürzel im Clip-Namen, sonst der Sub-Library-Name der Lieferung als
                # Label-Signal. Der LC bleibt Vorschlag, Titel/Autoren verifiziert.
                name = fhit.get("sublibrary") or fhit.get("labelname") or ""
                cat = catalog_fn(stem) or (catalog_fn(stem, tpub=name) if name else None)
                if cat:
                    _apply_catalog(card, cat)
            results.append(card)
            continue
        # ISRC im Clip-Namen: eindeutiger Schlüssel — vor dem Zerlegen nach Interpret/Titel prüfen und aus dem
        # Namen nehmen, sonst landet er als Titel oder Interpret auf der Karte.
        isrc, stem = watch.extract_isrc_from_stem(stem)
        if isrc:
            card["isrc"] = isrc
            ihit = songsdb.find_by_isrc(con, isrc)
            if ihit:
                _take(card, ihit)
                card["artist"], card["title"] = ihit["interpret"], ihit["titel"]
                card["status"] = "db_review" if ihit.get("review_status") == "review" else "found_in_db"
                card["matched_by"] = "isrc"
                results.append(card)
                continue
        parsed = watch.parse_cue_from_stem(stem)
        lc = parsed["labelcode"]
        if parsed.get("library_name"):
            # Titel aus dem Library-Dateinamen — die Katalognummer bleibt als Quelle sichtbar, die Komponisten-
            # Nachnamen als Hinweis (nicht als Feldwert: nur Nachnamen, unverifiziert).
            card["library_name"] = parsed["library_name"]
            if parsed.get("composer_hint"):
                card["composer_hint"] = parsed["composer_hint"]
        # Underscore-Namen sind mehrdeutig (Interpret_Titel / Titel_Album_Interpret / Show_Interpret_Titel):
        # alle plausiblen Paare lokal prüfen, das erste mit Treffer gewinnt (kein Netz).
        artist, title, hit, local_hit = _pick_candidate(watch.parse_cue_candidates(stem), con, local_fn)
        card["artist"], card["title"] = artist, title
        # LC aus dem Dateinamen gegen den GVL-Master prüfen -> offizielles Label + rr vorbefüllen.
        if lc and label_lookup:
            master_label, rr = label_lookup(lc)
            if master_label:
                card["labelcode"], card["labelname"] = lc, master_label
                if rr:
                    card["rechterueckrufe"] = rr

        # Ohne Titel, oder ohne verwertbares Signal (weder Interpret noch LC) -> unlesbar
        # (Loop/SFX/reiner Dateiname). LC- oder Interpret-tragende Cues sind verwertbar.
        cat = catalog_fn(stem) if (catalog_fn is not None and not card["labelcode"]) else None
        if not title or (not artist and not card["labelcode"]):
            card["status"] = "unparsable_clip_name"
            if cat:
                _apply_catalog(card, cat)
            elif card.get("library_name"):
                card["status"] = "library_name"          # Titel lesbar, Kürzel unbekannt: offen, aber mit Titel
            results.append(card)
            continue

        if hit:
            _take(card, hit)
            # Zeile aus einer fremden Liste, deren Code nicht zum GVL-Master passt: Vorschlag, nicht verifiziert
            card["status"] = "db_review" if hit.get("review_status") == "review" else "found_in_db"
            results.append(card)
            continue

        # Ohne Interpret nicht raten (Netzsuche bräuchte Interpret) — manuell/Titelsuche.
        if (resolve_fn is None and local_hit is None) or not (artist and title):
            card["status"] = "filename_lc" if card["labelcode"] else "missing_no_resolver"
            if cat:
                _apply_catalog(card, cat)
            results.append(card)
            continue

        # Lokaler Index-Treffer eines Kandidaten-Paars: bei offline ist das schon das Ergebnis; online
        # darf das Netz noch bestätigen — findet es nichts, bleibt der Index-Vorschlag.
        if local_hit is not None and (resolve_fn is None or resolve_fn is local_fn):
            resolved = local_hit
        else:
            resolved = resolve_fn(artist, title) or local_hit
            if resolved and resolved.get("swapped") and resolved is not local_hit:
                artist, title = title, artist
                card["artist"], card["title"] = artist, title
        if resolved and resolved.get("ambiguous"):
            # Margin-Gate: mehrere Label gleichauf -> nichts nach songs.db, Kandidaten auf die Karte.
            _take(card, resolved)
            card["labelcode"], card["labelname"] = "", ""
            card["status"] = "ambiguous"
            if cat:
                # Katalog-Kürzel als drittes Signal in die Kandidatenliste — LC bleibt leer, Mensch wählt.
                card["catalog_hint"] = cat.sources[0] if cat.sources else "Katalog-Kürzel"
                card["tied"] = list(card.get("tied") or []) + [
                    {"labelcode": cat.labelcode, "label": cat.label, "confidence": cat.confidence,
                     "sources": list(cat.sources), "source_urls": list(cat.source_urls)}]
            results.append(card)
            continue
        if not resolved or not resolved.get("labelcode"):
            # LC aus dem Dateinamen ist ein Vorschlag (gelb), nicht „nicht gefunden" — der Mensch bestätigt ihn.
            card["status"] = "filename_lc" if card["labelcode"] else "unresolved"
            if cat:
                _apply_catalog(card, cat)
            results.append(card)
            continue
        if resolved.get("index_hit"):
            # Lokaler Track-Index (Dump-Daten): Vorschlag auf die Karte, KEIN Schreiben nach songs.db — die songs.db
            # wächst nur durch bestätigte Cues; bestätigen heißt im Editor speichern.
            _take(card, resolved)
            card["status"] = "index_hit"
            results.append(card)
            continue

        # ohne Cue-Dauer und ohne Autoren: Netz-Autoren sind nur Vorschlag auf der Karte
        songsdb.upsert(con, interpret=artist, titel=title,
                       labelname=resolved.get("labelname", ""), labelcode=resolved.get("labelcode", ""),
                       isrc=resolved.get("isrc", ""), source_type="edl_import", review_status="review")
        _take(card, resolved)
        if any(resolved.get(k) for k in ("komponist", "texter", "verlag")):
            card["author_new"] = True
        card["status"] = "resolved_and_written"
        results.append(card)
    inherit_from_siblings(results, con, local_fn)
    attach_rights(results, label_lookup)
    return results


def export_excel(cards: list[dict], path: str | Path) -> None:
    """Excel mit Interpret/Titel/Komponist/Texter/Label/LC/ISRC +
    Länge Sek. + Gesamtdauer-Zeile am Ende."""
    rows, total = _export_rows(cards)
    wb = Workbook()
    ws = wb.active
    ws.append(EXPORT_COLUMNS)
    for r in rows:
        ws.append(r)
    ws.append([*_SUM_PAD, "Gesamtdauer:", total])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))


def export_csv(cards: list[dict], path: str | Path) -> None:
    """Cue-Sheet als CSV (UTF-8-mit-BOM, Excel-freundlich) + Gesamtdauer-Zeile."""
    import csv
    rows, total = _export_rows(cards)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(EXPORT_COLUMNS)
        for r in rows:
            w.writerow(r)
        w.writerow([*_SUM_PAD, "Gesamtdauer:", total])


# Spaltenbreiten in Prozent, Summe 100. Ohne feste Breiten bekam ein langer Dateiname als Titel die
# halbe Seite und drückte die letzte Spalte („Länge Sek.") aus dem Blatt — bei 113 Zeichen ohne
# Leerzeichen fehlte sie ganz.
# Die Story-Engine wertet Prozent nicht aus, nur Breiten in pt: export_pdf rechnet um.
# Jede Kopfzeile passt fett in ihre Spalte, LC104994, R6;R9;R12;R13 und eine ISRC ohne Umbruch.
#              Interpret Titel Komponist Texter Verlag Label  LC  RR  ISRC Länge
_PDF_WIDTHS = (12, 18, 13, 8, 9, 9, 7, 9, 10, 5)
assert len(_PDF_WIDTHS) == len(EXPORT_COLUMNS) and sum(_PDF_WIDTHS) == 100
_PDF_FONT, _PDF_PAD = 8.0, 10.5             # Schriftgröße der Tabelle; Innenabstand links+rechts + Rahmen in pt

# Trennstellen in langen Wörtern ohne Leerzeichen: Dateinamen wie
# „ZZQ_TESTPROJEKT_EINS+ZZQ+NORDWAND+SERIE_1920X1080" haben keine, und die Story-Engine kann sie
# deshalb nicht umbrechen. Ein unsichtbares Zero-Width-Space nach Trennzeichen gibt ihr welche
# (Komma und Semikolon für Namenslisten ohne Leerzeichen: „Johnson,Smith,Williams").
_BREAK_AFTER = re.compile(r"([_+\-./,;])(?=[^\s]{6,})")


def _soft_break(text: str, min_token: int = 18) -> str:
    out = []
    for word in text.split(" "):
        if len(word) >= min_token:
            word = _BREAK_AFTER.sub("\\1\u200b", word)
        out.append(word)
    return " ".join(out)


@functools.lru_cache(maxsize=None)
def _char_width(ch: str, size: float) -> float:
    """Breite eines Zeichens in pt, wie die Story setzt: ASCII in Helvetica-Metrik (nachgemessen: identisch),
    alles andere kommt aus einer breiteren Ersatzschrift (Umlaute rund 0,6 em), vorsichtig 0,75 em."""
    import fitz
    return fitz.get_text_length(ch, fontname="helv", fontsize=size) if ord(ch) < 128 else 0.75 * size


def _text_width(text: str, size: float = _PDF_FONT) -> float:
    return sum(_char_width(ch, size) for ch in text)


def _fit_break(text: str, width: float, size: float = _PDF_FONT) -> str:
    """Wie _soft_break, dazu eine Umbruchstelle, wo ein Stück trotzdem breiter ist als seine Spalte (113 x,
    CamelCase, „Johnson,Smith,…"). Sonst setzt die Story die Spalte so breit wie das Stück und schiebt die
    hinteren Spalten aus dem Blatt, feste Breiten hin oder her. Auch für den Datenbank-Export."""
    widest = 1.02 * size                     # breitestes Zeichen ('@', 'W'): kurze Stücke ohne Messen durch
    words = []
    for word in _soft_break(text).split(" "):
        pieces = []
        for piece in word.split("\u200b"):
            if len(piece) * widest <= width or _text_width(piece, size) <= width:
                pieces.append(piece)
                continue
            cur, w = "", 0.0
            for ch in piece:
                cw = _char_width(ch, size)
                if cur and w + cw > width:
                    pieces.append(cur)
                    cur, w = "", 0.0
                cur, w = cur + ch, w + cw
            pieces.append(cur)
        words.append("\u200b".join(pieces))
    return " ".join(words)


def export_pdf(cards: list[dict], path: str | Path) -> None:
    """Cue-Sheet als PDF (A4 quer, PyMuPDF-Story, auto-paginiert) + Gesamtdauer."""
    import html

    import fitz
    rows, total = _export_rows(cards)
    media = fitz.paper_rect("a4-l")
    where = media + (36, 36, -36, -36)
    # Inhaltsbreite je Spalte in pt (Prozent der Satzbreite minus Innenabstand und Rahmen); body ohne Rand,
    # damit die Tabelle genau die Satzbreite hat.
    inner = [pct * where.width / 100 - _PDF_PAD for pct in _PDF_WIDTHS]
    ths = "".join(f'<th style="width:{w:.2f}pt">{html.escape(c)}</th>' for c, w in zip(EXPORT_COLUMNS, inner))
    trs = []
    for r in rows:
        # Erst umbrechen, dann escapen: andersherum koennte eine Umbruchhilfe in einer HTML-Entitaet
        # landen (&amp; und Co. tragen keine Trennzeichen, aber darauf soll sich nichts verlassen).
        tds = "".join(f"<td>{html.escape(_fit_break(str(v), w))}</td>" for v, w in zip(r, inner))
        trs.append(f"<tr>{tds}</tr>")
    trs.append(f"<tr class='sum'><td colspan='{len(EXPORT_COLUMNS) - 1}'>Gesamtdauer</td><td>{total}</td></tr>")
    style = ("<style>body{font-family:sans-serif;margin:0}h2{font-size:14pt;margin:0 0 10pt}"
             f"table{{border-collapse:collapse;width:100%;font-size:{_PDF_FONT:g}pt;table-layout:fixed}}"
             "th,td{border:0.5pt solid #999;padding:3pt 5pt;text-align:left}"
             "th{background:#eee}tr.sum td{font-weight:bold;background:#f5f5f5}</style>")
    doc_html = f"{style}<h2>CueCore — Cue-Sheet</h2><table><thead><tr>{ths}</tr></thead><tbody>{''.join(trs)}</tbody></table>"
    story = fitz.Story(html=doc_html)
    writer = fitz.DocumentWriter(str(path))
    more = 1
    while more:
        dev = writer.begin_page(media)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
