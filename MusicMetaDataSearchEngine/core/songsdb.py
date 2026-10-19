"""Verifizierte Song-DB + Skalierung.

Statt alle Zeilen in den Speicher zu laden (kippt bei Millionen Einträgen, wenn
Library-Datenbanken eingespeist werden), wird ein normalisierter Schlüssel
`nkey` = key(interpret)+US+key(titel) als INDEX gehalten und direkt abgefragt
(O(log n)). open() migriert bestehende DBs idempotent. upsert() lässt die DB
wachsen, ohne schon verifizierte Felder zu überschreiben.

Die aktive DB liegt außerhalb von Cloud-synchronisierten Ordnern (Korruptionsrisiko); seed() kopiert den
Repo-Stand einmalig dorthin.

Schema-Erweiterung: Spalten für
Fingerprint-/Confidence-Tracking, Datei-Referenz, GEMA-Abgleich, Audit-Trail.
Zwei Update-Semantiken beim upsert():
  - PROTECT (nur leere Felder auffüllen, nie überschreiben — wie die
    bestehenden komponist/texter/labelname/labelcode): isrc, iswc,
    gema_werknummer. Das sind stabile Fakten, keine sich ändernden Zustände.
  - REFRESH (immer auf den zuletzt übergebenen Wert setzen, wenn übergeben):
    source_type, confidence_score, review_status, fingerprint_source,
    duration_sec, file_path, spotify_url, apple_music_url, possible_cover,
    gema_lookup_pending, date_verified. Das sind Zustände, die sich mit jedem
    neuen Pipeline-Lauf legitim ändern dürfen (z.B. ein Re-Resolve mit mehr
    Quellen darf einen höheren confidence_score eintragen).
date_added wird nur beim ersten INSERT gesetzt, nie überschrieben.
"""
from __future__ import annotations
import re
import shutil
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .labelmatch import norm
from .normalize import normalize_labelcode, single_labelcode

_US = "\x1f"

# Spalte -> SQL-Typ. PROTECT/REFRESH-Zuordnung s. Moduldoc oben.
# album + track_number (GEMA-schlank): stabile Release-Fakten -> PROTECT.
# track_number bleibt TEXT (ID3-TRCK ist oft "3/12" — als Ganzzahl ginge das "/total" verloren).
_NEW_COLUMNS = {
    "isrc": "TEXT", "source_type": "TEXT", "confidence_score": "REAL",
    "review_status": "TEXT", "fingerprint_source": "TEXT", "duration_sec": "REAL",
    "file_path": "TEXT", "spotify_url": "TEXT", "apple_music_url": "TEXT",
    "possible_cover": "INTEGER", "iswc": "TEXT", "gema_werknummer": "TEXT",
    "gema_lookup_pending": "INTEGER", "date_added": "TEXT", "date_verified": "TEXT",
    "album": "TEXT", "track_number": "TEXT",
    # verlag: Musikverlag für die GEMA-Meldung — bewusst getrennt vom Label
    # (Tonträgerhersteller, GVL). Stabiler Fakt -> PROTECT.
    "verlag": "TEXT",
    # label_geliefert: was eine Library als Label + Code geliefert hat, wenn der GVL-Master das
    # Label unter einem anderen Code führt und deshalb der GVL-Code in labelcode steht ('Nordwand Music ·
    # LC99001'). Nie still verwerfen — die Notiz bleibt an der Zeile. Gesetzt nur zusammen mit dem labelcode.
    "label_geliefert": "TEXT",
}
_PROTECT_FIELDS = ("komponist", "texter", "labelname", "labelcode", "isrc", "iswc",
                   "gema_werknummer", "album", "track_number", "verlag")
_REFRESH_FIELDS = ("source_type", "confidence_score", "review_status", "fingerprint_source",
                   "duration_sec", "file_path", "spotify_url", "apple_music_url",
                   "possible_cover", "gema_lookup_pending", "date_verified")


def _nkey(artist: str, title: str) -> str:
    return key(artist) + _US + key(title)


def key(s: str) -> str:
    """Schlüsselteil für nkey in songs.db und Track-Index. Lässt norm() etwas
    übrig, ist es genau dieser Wert — für jeden bisher nicht leeren Schlüssel unverändert, ein vorhandener
    Track-Index passt weiter. Text ohne a-z/0-9 fiel dort auf '' zusammen (alle
    japanischen Titel eines Interpreten unter einem Schlüssel); er bekommt jetzt die Buchstaben und
    Ziffern seiner Schrift („Кино“ -> „кино“), reiner Satzzeichen-Text seinen Wortlaut („!!!“)."""
    return norm(s or "") or _skey(s)


def _legacy_nkey(artist: str, title: str) -> str:
    """nkey nach der früheren Formel (norm, leere Teile möglich) — so liegen die Zeilen in älteren
    Track-Indizes, bis sie neu gebaut sind."""
    return norm(artist or "") + _US + norm(title or "")


def _ukey(s: str) -> str:
    """Wie norm(), aber Buchstaben und Ziffern jeder Schrift bleiben stehen (Akzente auf lateinischen
    Buchstaben fallen weg wie dort). Für Text, den norm() verschluckt: Japanisch, Kyrillisch, „ø“, „ı“."""
    out = []
    for ch in unicodedata.normalize("NFC", s or "").casefold():
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        out.append(base if base.isascii() else ch)
    t = "".join(out).replace("&", " and ").replace("+", " and ")
    return re.sub(r"[\W_]+", " ", t).strip()


def _skey(s: str) -> str:
    """Vergleichsschlüssel für same_song: _ukey, bei reinem Satzzeichen-Text („!!!!!!!“) der Wortlaut."""
    return _ukey(s) or " ".join((s or "").casefold().split())


def same_song(artist: str, title: str, other_artist: str, other_title: str) -> bool:
    """Gleicher nkey heißt nur dann derselbe Song, wenn norm() nichts verschluckt hat. Titel ohne a-z/0-9
    („!!!!!!!“, „ハッピアー“, „???“) ergeben einen leeren Titelteil und kollidierten je Interpret — ein fremder
    Song kam als verifizierter Treffer mit conf 1.0 zurück. Für lateinischen Text ist _skey
    gleich norm(), dort ändert sich nichts."""
    return (_nkey(artist, title) == _nkey(other_artist, other_title)
            and _skey(artist) == _skey(other_artist) and _skey(title) == _skey(other_title))


def _match(con: sqlite3.Connection, artist: str, title: str, cols: str = "id", *,
           exclude_id: int | None = None) -> sqlite3.Row | None:
    """Erste Zeile zu Interpret+Titel: nkey über den Index, dann same_song gegen den Wortlaut."""
    for r in con.execute(f"SELECT {cols}, id AS _id, interpret AS _interpret, titel AS _titel FROM songs "
                         "WHERE nkey=?", (_nkey(artist, title),)):
        if r["_id"] != exclude_id and same_song(artist, title, r["_interpret"], r["_titel"]):
            return r
    return None


def _lc_store(value) -> str:
    """Speicherform eines Labelcodes in songs.db: 'LC' + Ziffern, führende Nullen, kein
    Leerzeichen ('121' -> 'LC00121', 'LC 24 584' -> 'LC24584'). Was sich nicht als genau EIN Code lesen
    lässt (kein Ziffernblock, mehrere Codes), bleibt wie es ist — nie still verwerfen."""
    v = (value or "").strip()
    code = single_labelcode(v)
    return "LC" + code if code else v


def normalize_labelcodes(con: sqlite3.Connection) -> dict:
    """Bestehende Zeilen (songs + library_files) auf die Speicherform bringen — für die Bereinigung von
    Seed und Live-DB (eigener Schritt mit Sicherung; läuft NICHT beim Öffnen).
    Idempotent. Rückgabe: geänderte Zeilen je Tabelle. songs_fts folgt über den Update-Trigger."""
    out = {}
    for table in ("songs", "library_files"):
        rows = con.execute(f"SELECT rowid, labelcode FROM {table} WHERE COALESCE(labelcode,'') <> ''").fetchall()
        changed = [(new, r[0]) for r in rows if (new := _lc_store(r[1])) != r[1]]
        con.executemany(f"UPDATE {table} SET labelcode=? WHERE rowid=?", changed)
        out[table] = len(changed)
    con.commit()
    return out


def _strip_match(r: sqlite3.Row | None) -> dict | None:
    return {k: r[k] for k in r.keys() if k not in ("_id", "_interpret", "_titel")} if r else None


def seed(active_path: str | Path, source_path: str | Path) -> None:
    """Aktive DB einmalig aus dem Repo-Stand befüllen (falls noch nicht da)."""
    active, source = Path(active_path), Path(source_path)
    if not active.exists() and source.exists():
        active.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, active)


# Stand der Bestandsheilungen beim Start (Schlüssel, Code-Korrektur, Code über Labelnamen) als PRAGMA user_version. Eine
# vorhandene songs.db mit kleinerem Stand wird vor dem ersten Umschreiben einmal gesichert (backup_before_heal).
HEAL_VERSION = 20261012


def backup_before_heal(path: str | Path, stamp: str) -> Path | None:
    """Datierte Sicherung 'songs.sicherung-<stamp>.db' daneben, wenn die Datei einen älteren Heilungsstand trägt
    (user_version < HEAL_VERSION). Über die Backup-API einer nur-lesenden Verbindung, nie überschreibend.
    Rückgabe: die Sicherung oder None (nichts zu tun)."""
    p = Path(path)
    if not p.is_file():
        return None
    src = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if src.execute("PRAGMA user_version").fetchone()[0] >= HEAL_VERSION:
            return None
        dst = p.with_name(f"{p.stem}.sicherung-{stamp}{p.suffix}")
        if dst.exists():
            return None
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
        return dst
    finally:
        src.close()


def mark_healed(con: sqlite3.Connection) -> None:
    """Nach den Bestandsheilungen: Stand vermerken, der nächste Start sichert nicht noch einmal."""
    con.execute(f"PRAGMA user_version={HEAL_VERSION}")
    con.commit()


def open(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS songs (
        id INTEGER PRIMARY KEY, interpret TEXT NOT NULL, titel TEXT NOT NULL,
        komponist TEXT, texter TEXT, labelname TEXT, labelcode TEXT,
        nkey TEXT, quelle TEXT, UNIQUE(interpret, titel))""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(songs)")]
    if "nkey" not in cols:
        con.execute("ALTER TABLE songs ADD COLUMN nkey TEXT")
    if "quelle" not in cols:
        con.execute("ALTER TABLE songs ADD COLUMN quelle TEXT")
    for col, sqltype in _NEW_COLUMNS.items():
        if col not in cols:
            con.execute(f"ALTER TABLE songs ADD COLUMN {col} {sqltype}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_songs_nkey ON songs(nkey)")
    # Sortierung der Track-Datenbank ohne Groß/klein (_ORDER) — ohne Index sortierte jede Seite alle Zeilen neu
    con.execute("CREATE INDEX IF NOT EXISTS idx_songs_sort ON songs(interpret COLLATE NOCASE, titel COLLATE NOCASE)")
    # Fehlender nkey, oder einer mit leerem Teil (frühere Formel): neu rechnen. Idempotent — eine Zeile,
    # deren Teil auch neu leer bleibt (leerer Titel), wird nur gelesen, nicht geschrieben.
    for r in con.execute("SELECT id, interpret, titel, nkey FROM songs WHERE nkey IS NULL OR nkey='' "
                         "OR substr(nkey, 1, 1)=? OR substr(nkey, -1)=?", (_US, _US)).fetchall():
        nk = _nkey(r["interpret"], r["titel"])
        if nk != r["nkey"]:
            con.execute("UPDATE songs SET nkey=? WHERE id=?", (nk, r["id"]))
    # Library-Dateinamen: Library-Clips heißen in EDLs nur nach Katalognummer („ZZQ4030_1“);
    # jeder gelieferte Dateiname zeigt auf seine songs-Zeile — auch wenn derselbe Interpret+Titel auf
    # mehreren Alben liegt und deshalb nur EINE songs-Zeile trägt (UNIQUE interpret+titel).
    con.execute("""CREATE TABLE IF NOT EXISTS library_files (
        file TEXT PRIMARY KEY, file_name TEXT NOT NULL, song_id INTEGER NOT NULL, library TEXT NOT NULL,
        catno TEXT, track_number TEXT, version TEXT, isrc TEXT, duration_sec REAL, sublibrary TEXT)""")
    lf_cols = {r[1] for r in con.execute("PRAGMA table_info(library_files)")}
    # Je Datei: Sub-Katalog, Label/Code und Album der Lieferung — derselbe Interpret+Titel liegt auf mehreren
    # Alben (auch verschiedener Sub-Libraries mit eigenem LC), trägt aber nur
    # EINE songs-Zeile; die Cue-Karte muss den LC der getroffenen Datei zeigen, nicht den der ersten.
    for col in ("sublibrary", "labelcode", "labelname", "album", "edit_id", "label_geliefert"):
        if col not in lf_cols:
            con.execute(f"ALTER TABLE library_files ADD COLUMN {col} TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_library_files_song ON library_files(song_id)")
    # Edit-/Track-ID der Lieferung: manche Lieferungen nennen sie am Ende jedes Dateinamens, und die EDL-Clips
    # tragen dieselbe Nummer bei abweichender Katalog-Schreibweise (Lieferung 'ZZQ_40_11_…_1000001',
    # EDL 'UPM_ZZQ40_11_…_1000001'). Damit trifft die Zuordnung auch dann.
    con.execute("CREATE INDEX IF NOT EXISTS idx_library_files_edit ON library_files(edit_id)")
    # Sub-Kataloge der Lieferung je songs-Zeile („Nordwand Music; Zzq Library“), damit „nordwand“ im Datenbank-Tab
    # trifft — gepflegt von import_library_tracks, kein Editor-Feld.
    if "sublibraries" not in {r[1] for r in con.execute("PRAGMA table_info(songs)")}:
        con.execute("ALTER TABLE songs ADD COLUMN sublibraries TEXT")
    # Welche mitgelieferte Lieferungs-CSV schon drin ist — je DATEI, nicht je Library; „einmal je Library"
    # hätte jede zweite Lieferung derselben Library stumm verschluckt.
    con.execute("""CREATE TABLE IF NOT EXISTS library_deliveries (
        file TEXT PRIMARY KEY, library TEXT NOT NULL, imported TEXT NOT NULL, rows INTEGER)""")
    ensure_fts(con)
    con.commit()


# quelle/sublibraries: „zzq“, „nordwand“. gema_werknummer, damit die Werknummer
# auffindbar ist (Feld immer führen, füllen was vorhanden ist, mitsuchen — eine GEMA-API gibt es nicht).
_FTS_COLS = ("interpret", "titel", "labelname", "labelcode", "quelle", "sublibraries", "gema_werknummer")


def ensure_fts(con: sqlite3.Connection) -> None:
    """FTS5-Trigram über Interpret, Titel, Label, Code für die Suche im Datenbank-Tab (external content,
    Trigger halten es aktuell). Vorher LIKE über vier Spalten: 0,5 s bei 61k Zeilen,
    kein Index möglich. remove_diacritics: „athane“ trifft „Athané“. Fehlt die Tabelle (alte DB),
    wird sie angelegt und einmal aus songs gefüllt."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='songs_fts'").fetchone():
        have = tuple(r[1] for r in con.execute("PRAGMA table_info(songs_fts)"))
        if have == _FTS_COLS:
            return
        # Spaltensatz hat sich geändert (z.B. sublibraries dazu): Trigger + Tabelle neu, einmal neu füllen
        for trg in ("songs_fts_ai", "songs_fts_ad", "songs_fts_au"):
            con.execute(f"DROP TRIGGER IF EXISTS {trg}")
        con.execute("DROP TABLE songs_fts")
    cols = ", ".join(_FTS_COLS)
    con.execute(f"CREATE VIRTUAL TABLE songs_fts USING fts5({cols}, content='songs', content_rowid='id', "
                "tokenize='trigram remove_diacritics 1')")
    new_vals = ", ".join(f"new.{c}" for c in _FTS_COLS)
    old_vals = ", ".join(f"old.{c}" for c in _FTS_COLS)
    con.execute(f"CREATE TRIGGER IF NOT EXISTS songs_fts_ai AFTER INSERT ON songs BEGIN "
                f"INSERT INTO songs_fts(rowid, {cols}) VALUES (new.id, {new_vals}); END")
    con.execute(f"CREATE TRIGGER IF NOT EXISTS songs_fts_ad AFTER DELETE ON songs BEGIN "
                f"INSERT INTO songs_fts(songs_fts, rowid, {cols}) VALUES ('delete', old.id, {old_vals}); END")
    con.execute(f"CREATE TRIGGER IF NOT EXISTS songs_fts_au AFTER UPDATE ON songs BEGIN "
                f"INSERT INTO songs_fts(songs_fts, rowid, {cols}) VALUES ('delete', old.id, {old_vals}); "
                f"INSERT INTO songs_fts(rowid, {cols}) VALUES (new.id, {new_vals}); END")
    con.execute("INSERT INTO songs_fts(songs_fts) VALUES ('rebuild')")


def has_fts(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='songs_fts'").fetchone() is not None


_ALL_FIELDS = ("interpret", "titel", "komponist", "texter", "labelname", "labelcode",
              *_NEW_COLUMNS.keys())


def find(con: sqlite3.Connection, artist: str, title: str) -> dict | None:
    return _strip_match(_match(con, artist, title, ",".join(_ALL_FIELDS)))


def count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM songs").fetchone()[0]


def _file_key(name: str) -> str:
    """Dateiname -> Schlüssel: nur der Stamm, klein (EDL-Clips kommen mit oder ohne Endung, in beliebiger
    Schreibung); Pfadanteile sind vorher per clip_basename entfernt."""
    stem = (name or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in stem and len(stem.rsplit(".", 1)[1]) <= 5:
        stem = stem.rsplit(".", 1)[0]
    return stem.casefold()


_TRAIL_ID = re.compile(r"(?<![0-9])(\d{6,9})\s*$")


def _edit_id(name: str) -> str:
    """Die Zahl am Ende eines Library-Dateinamens (6–9 Stellen) = Edit-/Track-ID der Lieferung. '' wenn keine."""
    stem = _file_key(name)
    m = _TRAIL_ID.search(stem)
    return m.group(1) if m else ""


def _shares_word(words: set[str], *texts: str) -> bool:
    """Teilt der Clipname (words) ein Wort ab drei Zeichen, das keine reine Zahl ist, mit einem der Texte?"""
    return any(t in words for x in texts for t in norm(x or "").split() if len(t) >= 3 and not t.isdigit())


def find_by_file(con: sqlite3.Connection, name: str) -> dict | None:
    """songs-Zeile (mit id) zu einem Library-Dateinamen aus library_files, plus die Datei-Fakten der
    Lieferung (file, file_catno, file_track_number, file_version, file_isrc, file_duration_sec, library).
    None ohne Treffer — der Aufrufer geht dann den normalen Weg (Parse, Katalog-Hint)."""
    key = _file_key(name)
    if not key:
        return None
    r = con.execute(
        f"SELECT s.id,{','.join('s.' + f for f in _ALL_FIELDS)}, f.file_name AS file, f.library, f.catno AS file_catno, "
        "f.track_number AS file_track_number, f.version AS file_version, f.isrc AS file_isrc, "
        "f.duration_sec AS file_duration_sec, f.sublibrary, f.labelcode AS file_labelcode, f.labelname AS file_labelname, "
        "f.album AS file_album, f.label_geliefert AS file_label_geliefert "
        "FROM library_files f JOIN songs s ON s.id = f.song_id WHERE f.file=?",
        (key,)).fetchone()
    if not r:
        eid = _edit_id(name)
        if eid:
            r = con.execute(
                f"SELECT s.id,{','.join('s.' + f for f in _ALL_FIELDS)}, f.file_name AS file, f.library, f.catno AS file_catno, "
                "f.track_number AS file_track_number, f.version AS file_version, f.isrc AS file_isrc, "
                "f.duration_sec AS file_duration_sec, f.sublibrary, f.labelcode AS file_labelcode, "
                "f.labelname AS file_labelname, f.album AS file_album, f.label_geliefert AS file_label_geliefert "
                "FROM library_files f "
                "JOIN songs s ON s.id = f.song_id WHERE f.edit_id=?", (eid,)).fetchall()
            # Die Endzahl allein ist keine Zuordnung — „Interview Zzq Take 3 1000001“ traf sonst einen
            # Library-Track als verifiziert. Ein Wort aus Titel oder Dateiname der Lieferung muss auch
            # im Clipnamen stehen (einzelne Lieferdateien tragen den Titel nicht im Namen).
            words = set(norm(key).split())
            r = [x for x in r if _shares_word(words, x["titel"], _file_key(x["file"]))]
            r = r[0] if len(r) == 1 else None      # mehrdeutig -> lieber nichts
    if not r:
        return None
    d = dict(r)
    d["catno"] = d["file_catno"]
    for k in ("labelcode", "labelname", "album"):          # Datei-Fakten schlagen die songs-Zeile
        if d.get(f"file_{k}"):
            d[k] = d[f"file_{k}"]
    d["label_geliefert"] = d.get("file_label_geliefert") or ""   # gehört zum LC dieser Datei, nicht zum Song
    return d


def library_files_count(con: sqlite3.Connection, library: str) -> int:
    return con.execute("SELECT COUNT(*) FROM library_files WHERE library=?", (library,)).fetchone()[0]


def imported_deliveries(con: sqlite3.Connection) -> set[str]:
    """Dateinamen der schon eingepflegten mitgelieferten Lieferungen."""
    return {r[0] for r in con.execute("SELECT file FROM library_deliveries")}


def mark_delivery(con: sqlite3.Connection, file: str, library: str, *, rows: int | None, date: str) -> None:
    con.execute("INSERT OR REPLACE INTO library_deliveries (file, library, imported, rows) VALUES (?,?,?,?)",
                (file, library, date, rows))
    con.commit()


def _num(v) -> float | None:
    """Sekunden aus der Lieferung: Zahl oder None (manche Lieferungen führen bei fehlender Länge auch „''“)."""
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def import_library_tracks(con: sqlite3.Connection, rows: list[dict], *, library: str, date_verified: str) -> dict:
    """Library-Lieferung einpflegen (Metadaten aus Libraries gelten als verifiziert).
    Je Zeile: upsert in songs (quelle library:<name>, review_status verified, source_type library,
    date_verified; PROTECT-Felder einer schon vorhandenen Zeile bleiben) und eine Zeile in library_files
    (Dateiname -> songs.id). rows: dicts mit file, interpret, titel und optional komponist, texter, verlag,
    labelname, labelcode, album, track_number, isrc, iswc, gema_werknummer, duration_sec, catno, version, sublibrary.
    Idempotent; ein Commit am Ende. Rückgabe {inserted, updated, files}."""
    rep = {"inserted": 0, "updated": 0, "files": 0}
    quelle = f"library:{library}"
    for r in rows:
        interpret, titel = (r.get("interpret") or "").strip(), (r.get("titel") or "").strip()
        if not interpret or not titel:
            continue
        existed = _match(con, interpret, titel, "id, file_path")
        dur = _num(r.get("duration_sec"))
        res = upsert(con, interpret=interpret, titel=titel, komponist=r.get("komponist", ""), texter=r.get("texter", ""),
                     verlag=r.get("verlag", ""), labelname=r.get("labelname", ""), labelcode=r.get("labelcode", ""),
                     album=r.get("album", ""), track_number=r.get("track_number", ""), isrc=r.get("isrc", ""),
                     iswc=r.get("iswc", ""), gema_werknummer=r.get("gema_werknummer", ""), quelle=quelle,
                     source_type="library", review_status="verified", date_verified=date_verified,
                     duration_sec=dur,
                     # file_path ist REFRESH — die erste Datei der Lieferung bleibt die Referenz
                     file_path="" if (existed and existed["file_path"]) else (r.get("file") or ""))
        rep["inserted" if res == "inserted" else "updated"] += 1
        song_id = existed["id"] if existed else _match(con, interpret, titel)["id"]
        names = [(r.get("file") or "").strip()] + [x.strip() for x in (r.get("file_alt") or "").split("|")]
        for fname in dict.fromkeys(n for n in names if n):     # Harvest-Export: mehrere Schreibweisen je Track
            con.execute("INSERT OR REPLACE INTO library_files (file, file_name, song_id, library, catno, track_number, "
                        "version, isrc, duration_sec, sublibrary, labelcode, labelname, album, edit_id, label_geliefert) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (_file_key(fname), fname, song_id, library, r.get("catno", ""), r.get("track_number", ""),
                         r.get("version", ""), r.get("isrc", ""), dur, r.get("sublibrary", ""), _lc_store(r.get("labelcode")),
                         r.get("labelname", ""), r.get("album", ""),
                         (r.get("edit_id") or "").strip() or _edit_id(fname), r.get("label_geliefert", "")))
            rep["files"] += 1
        if r.get("label_geliefert"):
            # Notiz nur, wenn die songs-Zeile auch diesen Code trägt (labelcode ist PROTECT: eine schon belegte
            # Zeile behält ihren Code, dann gehört die Notiz nicht zu ihr)
            con.execute("UPDATE songs SET label_geliefert=? WHERE id=? AND labelcode=? AND COALESCE(label_geliefert,'')=''",
                        (r["label_geliefert"], song_id, r.get("labelcode", "")))
    # Sub-Kataloge je songs-Zeile nachziehen (Suche im Datenbank-Tab); Update-Trigger pflegt songs_fts
    con.execute("""UPDATE songs SET sublibraries = (SELECT group_concat(DISTINCT sublibrary) FROM library_files f
                   WHERE f.song_id = songs.id AND COALESCE(f.sublibrary,'') <> '')
                   WHERE id IN (SELECT song_id FROM library_files WHERE library=?)""", (library,))
    con.commit()
    return rep


def clear_cue_durations(con: sqlite3.Connection) -> int:
    """Bereinigung: der EDL-Import hat früher die Dauer des Cues als duration_sec des Songs
    gespeichert, das nächste Cue-Sheet zeigte sie als Dauer eines ganz anderen Einsatzes. Leert duration_sec der
    edl_import-Zeilen; Library-Zeilen (quelle library:…) behalten ihre gelieferte Tracklänge, auch wenn ein
    späterer Lauf source_type umgeschrieben hat. Idempotent, committet. Rückgabe: Zahl der geleerten Zeilen."""
    n = con.execute("UPDATE songs SET duration_sec=NULL WHERE source_type='edl_import' AND duration_sec IS NOT NULL "
                    "AND COALESCE(quelle,'') NOT LIKE 'library:%'").rowcount
    con.commit()
    return n


def find_by_isrc(con: sqlite3.Connection, isrc: str) -> dict | None:
    """songs-Zeile zu einem ISRC (Bindestriche/Schreibung egal). Nur bei genau EINER Zeile — mehrere Zeilen
    mit demselben ISRC sind ein Datenfehler, dann lieber nichts."""
    key = re.sub(r"[^A-Z0-9]", "", (isrc or "").upper())
    if len(key) != 12:
        return None
    rows = con.execute(f"SELECT {','.join(_ALL_FIELDS)} FROM songs "
                       "WHERE REPLACE(UPPER(COALESCE(isrc,'')),'-','')=? LIMIT 2", (key,)).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def find_row(con: sqlite3.Connection, artist: str, title: str) -> dict | None:
    """Wie find(), aber inklusive id — für den Resolver-Merge (Bearbeiten/Abgleich),
    der die Zeile danach gezielt per id aktualisieren können muss."""
    return _strip_match(_match(con, artist, title, "id," + ",".join(_ALL_FIELDS)))


def find_similar(con: sqlite3.Connection, artist: str, title: str, *,
                 limit: int = 5, min_ratio: float = 0.62) -> list[dict]:
    """Ähnliche Songs vorschlagen, wenn kein exakter nkey-Treffer da ist
    („Meintest du…?"). Fängt leicht abweichende Titel (fehlendes Wort, Variante,
    Tippfehler) ab. Kandidaten primär beim gleichen normalisierten Interpret; als
    Fallback (Interpret evtl. anders geschrieben) über einen Titel-Token vorgefiltert.
    Ranking: difflib-Titel-Ratio + Substring-Bonus, gewichtet mit Interpret-Ähnlichkeit.
    Gibt Zeilen (mit id + allen Feldern) zurück, beste zuerst."""
    import difflib
    na, nt = key(artist), key(title)
    if not nt:
        return []
    cols = "id," + ",".join(_ALL_FIELDS)
    seen: set[int] = set()
    scored: list[tuple[float, dict]] = []

    def consider(rows, artist_exact: bool) -> None:
        for r in rows:
            if r["id"] in seen:
                continue
            rt, ra = key(r["titel"]), key(r["interpret"])
            tscore = difflib.SequenceMatcher(None, nt, rt).ratio()
            if rt and (nt in rt or rt in nt):        # ein Titel steckt im anderen -> starker Treffer
                tscore = max(tscore, 0.9)
            ascore = 1.0 if artist_exact else difflib.SequenceMatcher(None, na, ra).ratio()
            if na and ascore < 0.6:      # anderer Interpret als angegeben: kein Vorschlag
                continue                 # (sonst käme zu „rolling stones“ + „sympathy“ der Song eines Fremden)
            score = (0.7 * tscore + 0.3 * ascore) if na else tscore
            if score >= min_ratio:
                seen.add(r["id"])
                scored.append((score, dict(r)))

    if na:
        consider(con.execute(f"SELECT {cols} FROM songs WHERE nkey LIKE ?",
                             (na + _US + "%",)).fetchall(), True)
    if not scored:   # Interpret evtl. leicht anders -> über den längsten Titel-Token vorfiltern
        tokens = [t for t in nt.split() if len(t) >= 4]
        if tokens:
            tok = max(tokens, key=len)
            consider(con.execute(f"SELECT {cols} FROM songs WHERE nkey LIKE ?",
                                 ("%" + tok + "%",)).fetchall(), False)
    scored.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in scored[:limit]]


# Felder, die der DB-Editor anzeigen und bearbeiten darf (interpret/titel sind Pflicht).
EDITABLE_FIELDS = ("interpret", "titel", "komponist", "texter", "verlag", "labelname", "labelcode",
                   "album", "track_number", "isrc", "iswc", "gema_werknummer")


# Felder, nach deren LEERE der DB-Editor filtern kann („ohne Labelcode", „ohne Verlag" …).
MISSING_FIELDS = ("labelcode", "komponist", "texter", "verlag", "isrc", "gema_werknummer")


_LC_WORD = re.compile(r"^lc(\d{1,6})$")


def _where(query: str, missing: str) -> tuple[str, tuple]:
    """WHERE-Klausel + Argumente für Suche (Interpret/Titel/Labelname/Labelcode) und
    optionalen Lücken-Filter. Unbekanntes `missing` wird ignoriert (kein Raten, kein SQL aus
    Nutzereingabe — nur Whitelist-Spalten landen im Statement)."""
    clauses, args = [], []
    q = re.sub(r"\blc[\s-]+(?=\d)", "lc", (query or "").strip().lower())    # „LC 00171“ ist EIN Suchwort
    # Wort für Wort: jedes Wort muss in einem der vier Felder stehen („ian pooley balmes“ trifft
    # Interpret + Titel zusammen; ein LIKE über den ganzen Text träfe nur ein einzelnes Feld).
    # Wörter ab drei Zeichen über songs_fts (Trigram, Teilwort, ohne Akzente), kürzere per LIKE.
    terms = []
    for word in q.split():
        m = _LC_WORD.match(word)
        if m:
            # Labelcode: Alt-Zeilen tragen ihn ohne „LC“ — beide Schreibweisen, der
            # ganze Code auch kurz geschrieben („LC171“), beim Tippen weiter per Präfix
            digits = m.group(1)
            code = normalize_labelcode(digits)
            clauses.append("(labelcode IN (?, ?) OR labelcode LIKE ? OR labelcode LIKE ?)")
            args += [code, "LC" + code, "LC" + digits + "%", digits + "%"]
            continue
        if len(word) >= 3:
            terms.append('"' + word.replace('"', '""') + '"')
            continue
        like = f"%{word}%"
        clauses.append("(lower(interpret) LIKE ? OR lower(titel) LIKE ? "
                       "OR lower(COALESCE(labelname,'')) LIKE ? OR lower(COALESCE(labelcode,'')) LIKE ?)")
        args += [like, like, like, like]
    if terms:
        clauses.insert(0, "id IN (SELECT rowid FROM songs_fts WHERE songs_fts MATCH ?)")
        args.insert(0, " AND ".join(terms))
    if missing in MISSING_FIELDS:
        # Lücken-Filter = Recherche-Warteschlange: Library-Lieferungen sind komplett, wie sie kommen
        # (eine Instrumental-Library ohne Texter zählte sonst Zehntausende Zeilen „ohne Texter“).
        clauses.append(f"COALESCE({missing},'')='' AND COALESCE(source_type,'')<>'library'")
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", tuple(args)


def missing_counts(con: sqlite3.Connection, *, query: str = "") -> dict:
    """Je MISSING_FIELDS: wie viele (der gesuchten) Zeilen haben das Feld leer — für die
    Filter-Chips im DB-Editor. Ein Scan, keine Pagination."""
    where, args = _where(query, "")
    sums = ", ".join(f"SUM(COALESCE({f},'')='' AND COALESCE(source_type,'')<>'library')" for f in MISSING_FIELDS)
    row = con.execute(f"SELECT {sums} FROM songs {where}", args).fetchone()
    return {f: int(row[i] or 0) for i, f in enumerate(MISSING_FIELDS)}


# Alphabetisch ohne Groß/klein („ZZQ BAND“ stand binär vor „Aaron Beispiel“).
_ORDER = "interpret COLLATE NOCASE, titel COLLATE NOCASE"


def list_songs(con: sqlite3.Connection, *, query: str = "", limit: int = 100, offset: int = 0,
               missing: str = "") -> dict:
    """Zeilen für den DB-Editor (paginiert, mit id). Optionale Suche über
    Interpret/Titel/Labelname/Labelcode und Lücken-Filter (`missing` in MISSING_FIELDS).
    Gibt {rows, total, limit, offset, total_all, total_verified, missing_counts}."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    cols = "id," + ",".join(_ALL_FIELDS)
    where, args = _where(query, missing)
    total = con.execute(f"SELECT COUNT(*) FROM songs {where}", args).fetchone()[0]
    rows = con.execute(
        f"SELECT {cols} FROM songs {where} ORDER BY {_ORDER} LIMIT ? OFFSET ?",
        (*args, limit, offset)).fetchall()
    where_q, args_q = _where(query, "")
    total_all, total_verified = con.execute(
        f"SELECT COUNT(*), SUM(COALESCE(review_status,'')<>'review') FROM songs {where_q}", args_q).fetchone()
    return {"rows": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset,
            "total_all": total_all,                       # Treffer der Suche OHNE Lücken-Filter (Chip „Alle")
            "total_verified": int(total_verified or 0),   # davon ohne „Prüfen“-Zeilen (Umfang „Verifiziert N“)
            "missing_counts": missing_counts(con, query=query)}


def all_songs(con: sqlite3.Connection, *, query: str = "", missing: str = "") -> list[dict]:
    """Alle (optional gefilterten) Zeilen für den Export — keine Pagination.
    Gleiche Filterlogik wie list_songs (Suche + Lücken-Filter)."""
    cols = ",".join(_ALL_FIELDS)
    where, args = _where(query, missing)
    rows = con.execute(f"SELECT {cols} FROM songs {where} ORDER BY {_ORDER}", args).fetchall()
    return [dict(r) for r in rows]


# quelle einer Tagger-Zeile, die im Editor der Track-Datenbank gespeichert wurde (update_song)
TAGGER_EDITED = "tagger:manuell"


def update_song(con: sqlite3.Connection, song_id: int, fields: dict) -> dict:
    """Eine Zeile gezielt per id bearbeiten (DB-Editor). Anders als upsert() werden
    hier auch sonst geschützte Felder bewusst überschrieben — der Nutzer trägt
    korrigierte Werte ein (z.B. einen manuell recherchierten Komponisten). nkey wird
    bei Interpret/Titel-Änderung mitgezogen. Gibt {ok:True} oder {error:...}."""
    cur = con.execute("SELECT interpret, titel FROM songs WHERE id=?", (song_id,)).fetchone()
    if cur is None:
        return {"error": "Zeile nicht gefunden"}
    updates = {k: (v or "").strip() for k, v in fields.items() if k in EDITABLE_FIELDS}
    if "labelcode" in updates:
        updates["labelcode"] = _lc_store(updates["labelcode"])
    new_int = updates.get("interpret", cur["interpret"])
    new_tit = updates.get("titel", cur["titel"])
    if not new_int or not new_tit:
        return {"error": "Interpret und Titel dürfen nicht leer sein"}
    set_clauses = [f"{k}=?" for k in updates]
    params = list(updates.values())
    if "interpret" in updates or "titel" in updates:
        new_nkey = _nkey(new_int, new_tit)
        # Kollision explizit prüfen (migrierte Alt-DBs tragen kein UNIQUE-Constraint).
        if _match(con, new_int, new_tit, exclude_id=song_id):
            return {"error": f"Ein anderer Eintrag mit '{new_int} – {new_tit}' existiert bereits"}
        set_clauses.append("nkey=?")
        params.append(new_nkey)
    if not set_clauses:
        return {"ok": True, "unchanged": True}
    # Gespeichert im Editor = vom Menschen geprüft: eine zur Prüfung markierte Zeile gilt danach als verifiziert
    set_clauses.append("review_status=CASE WHEN review_status='review' THEN 'verified' ELSE review_status END")
    # Eine Tagger-Zeile ist danach von Hand bearbeitet: das nächste Apply überschreibt ihren LC nicht mehr
    # (upsert own_quelle gilt nur für unveränderte Tagger-Zeilen). Andere Herkunft bleibt, wie sie ist.
    set_clauses.append(f"quelle=CASE WHEN quelle='tagger' THEN '{TAGGER_EDITED}' ELSE quelle END")
    params.append(song_id)
    con.execute(f"UPDATE songs SET {', '.join(set_clauses)} WHERE id=?", params)
    con.commit()
    return {"ok": True}


def create_song(con: sqlite3.Connection, fields: dict) -> dict:
    """Neuen Song komplett selbst anlegen (DB-Editor „Neuer Eintrag"). Interpret +
    Titel sind Pflicht, alle anderen EDITABLE_FIELDS optional. Kollision wird über
    nkey geprüft (migrierte Alt-DBs tragen kein UNIQUE-Constraint). quelle='manuell',
    date_added gesetzt. Gibt {ok:True, id:...} oder {error:...}."""
    vals = {k: (v or "").strip() for k, v in fields.items() if k in EDITABLE_FIELDS}
    if "labelcode" in vals:
        vals["labelcode"] = _lc_store(vals["labelcode"])
    interpret, titel = vals.get("interpret", ""), vals.get("titel", "")
    if not interpret or not titel:
        return {"error": "Interpret und Titel dürfen nicht leer sein"}
    if _match(con, interpret, titel):
        return {"error": f"Ein Eintrag mit '{interpret} – {titel}' existiert bereits"}
    vals["nkey"] = _nkey(interpret, titel)
    vals["quelle"] = "manuell"
    vals["date_added"] = datetime.now(timezone.utc).isoformat()
    cols = ",".join(vals)
    cur = con.execute(f"INSERT INTO songs ({cols}) VALUES ({','.join('?' * len(vals))})",
                      list(vals.values()))
    con.commit()
    return {"ok": True, "id": cur.lastrowid}


def delete_song(con: sqlite3.Connection, song_id: int) -> bool:
    """Eine Zeile per id löschen (DB-Editor „Löschen“ mit Rückfrage).
    Die Dateinamen der Lieferung (library_files) gehen mit — sonst zeigten sie auf eine verwaiste id.
    songs_fts zieht der Delete-Trigger nach. False, wenn es die id nicht gibt."""
    if con.execute("SELECT 1 FROM songs WHERE id=?", (song_id,)).fetchone() is None:
        return False
    con.execute("DELETE FROM library_files WHERE song_id=?", (song_id,))
    con.execute("DELETE FROM songs WHERE id=?", (song_id,))
    con.commit()
    return True


def upsert_undo(before: dict | None, after: dict) -> dict:
    """Gegenoperation zu einem upsert() fürs Undo-Log des Taggers (sonst bliebe der Eintrag nach dem Undo
    stehen und der verworfene LC käme als verifizierter AUTO-Treffer zurück). before/after = find_row() vor und
    nach dem Schreiben. Neu angelegt -> inserted; sonst die geänderten Felder als {feld: [vorher, nachher]}
    (NULL -> '' zählt nicht als Änderung, upsert schreibt '' in leere PROTECT-Felder)."""
    blank = lambda v: "" if v is None else v
    changed = {} if before is None else {f: [before.get(f), after.get(f)] for f in _ALL_FIELDS
                                         if blank(before.get(f)) != blank(after.get(f))}
    return {"song_id": after["id"], "nkey": _nkey(after["interpret"], after["titel"]),
            "inserted": before is None, "changed": changed}


def revert_upsert(con: sqlite3.Connection, info: dict) -> None:
    """upsert_undo() anwenden. Neu angelegte Zeile -> gelöscht, aber nur, wenn id und nkey noch
    zusammenpassen (eine inzwischen neu vergebene id bleibt unberührt). Sonst jedes geänderte Feld zurück
    auf den alten Wert — nur, solange es noch den Wert des Apply trägt (eine spätere Handkorrektur bleibt)."""
    sid = info["song_id"]
    if info.get("inserted"):
        if con.execute("SELECT 1 FROM songs WHERE id=? AND nkey=?", (sid, info["nkey"])).fetchone():
            delete_song(con, sid)
        return
    for f, (old, new) in (info.get("changed") or {}).items():
        if f in _ALL_FIELDS:                       # Spaltenname nur aus der Whitelist ins Statement
            con.execute(f"UPDATE songs SET {f}=? WHERE id=? AND {f} IS ?", (old, sid, new))
    con.commit()


def get_song(con: sqlite3.Connection, song_id: int) -> dict | None:
    r = con.execute(f"SELECT id,{','.join(_ALL_FIELDS)} FROM songs WHERE id=?", (song_id,)).fetchone()
    return dict(r) if r else None


def upsert(con: sqlite3.Connection, *, interpret: str, titel: str, komponist: str = "",
           texter: str = "", labelname: str = "", labelcode: str = "", quelle: str = "",
           isrc: str = "", source_type: str = "", confidence_score: float | None = None,
           review_status: str = "", fingerprint_source: str = "", duration_sec: float | None = None,
           file_path: str = "", spotify_url: str = "", apple_music_url: str = "",
           possible_cover: bool | None = None, iswc: str = "", gema_werknummer: str = "",
           gema_lookup_pending: bool | None = None, date_verified: str = "",
           album: str = "", track_number: str = "", verlag: str = "", own_quelle: str = "") -> str:
    """Insert oder Merge nach nkey. Zwei Update-Semantiken (s. Moduldoc):
    PROTECT-Felder werden nie überschrieben, nur leere Felder aufgefüllt;
    REFRESH-Felder werden bei jedem Aufruf mit übergebenem Wert aktualisiert.
    date_added wird nur beim ersten INSERT gesetzt. Caller committed am Batch-Ende.
    own_quelle: trägt die vorhandene Zeile genau diese quelle (der Aufrufer hat sie selbst angelegt), folgen
    labelcode und labelname dem übergebenen Code — der Tagger schreibt den neu bestätigten LC in die Datei, und
    die DB darf ihm nicht widersprechen. Fremde und Hand-Zeilen bleiben PROTECT, auch eine im
    Editor bearbeitete Tagger-Zeile (quelle TAGGER_EDITED, s. update_song).
    """
    nk = _nkey(interpret, titel)
    labelcode = _lc_store(labelcode)                    # 'LC' + Ziffern, jeder Schreibweg
    protect_vals = {"komponist": komponist, "texter": texter, "labelname": labelname, "labelcode": labelcode,
                    "isrc": isrc, "iswc": iswc, "gema_werknummer": gema_werknummer,
                    "album": album, "track_number": track_number, "verlag": verlag}
    refresh_vals = {"source_type": source_type, "confidence_score": confidence_score,
                    "review_status": review_status, "fingerprint_source": fingerprint_source,
                    "duration_sec": duration_sec, "file_path": file_path, "spotify_url": spotify_url,
                    "apple_music_url": apple_music_url,
                    "possible_cover": None if possible_cover is None else int(possible_cover),
                    "gema_lookup_pending": None if gema_lookup_pending is None else int(gema_lookup_pending),
                    "date_verified": date_verified}

    row = _match(con, interpret, titel, "id, quelle")
    if row:
        own = bool(own_quelle) and row["quelle"] == own_quelle and bool(labelcode)
        set_clauses = [f"{f}=?" if own and f in ("labelcode", "labelname") else
                       f"{f}=CASE WHEN COALESCE({f},'')='' THEN ? ELSE {f} END" for f in protect_vals]
        params = list(protect_vals.values())
        for f, v in refresh_vals.items():
            if v not in (None, ""):
                set_clauses.append(f"{f}=?")
                params.append(v)
        params.append(row["id"])
        con.execute(f"UPDATE songs SET {', '.join(set_clauses)} WHERE id=?", params)
        return "updated"

    fields = ["interpret", "titel", "nkey", "quelle", "date_added", *protect_vals.keys(), *refresh_vals.keys()]
    values = [interpret, titel, nk, quelle, datetime.now(timezone.utc).isoformat(),
             *protect_vals.values(), *refresh_vals.values()]
    placeholders = ",".join("?" * len(fields))
    con.execute(f"INSERT INTO songs ({','.join(fields)}) VALUES ({placeholders})", values)
    return "inserted"
