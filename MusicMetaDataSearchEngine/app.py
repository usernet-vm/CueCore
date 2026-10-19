#!/usr/bin/env python3
"""CueCore — FastAPI-Backend (powered by POSTBRO).

Lokale Web-App auf labels.db: Track-Resolver (GVL-LC) + GVL-Label-Suche.
Start:  ./cuecore.command   (oder: .venv/bin/uvicorn app:app)

DB-Pfad: $CUECORE_DB oder ~/Library/Application Support/MusicMeta/labels.db.
Kein API-Key nötig (MusicBrainz + Discogs keyless).
"""
from __future__ import annotations
import contextvars
import json
import os
import re
import sqlite3
import sys
import threading
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

from core import (acoustid, albumresolve, audd, conncheck, cuelist, dbexport, deezer, discogs, edl, edl_import, editingtools, genius, librarytracks,
                  folderpick, gvlsync, ingest, intake, itunes, musicbrainz, musiksammler,
                  netfetch, operations, settings, songsdb, tagging, trackindex, trackmeta, wikidata)
from core import indexdownload, labelcheck, labeldb, librarylists
from core.labeldb import connect, lookup_by_code, search_labels
from core.labelmatch import (LabelIndex, attach_library_prefixes, attach_mb_aliases, build_index_from_db,
                             match_hersteller, match_label)
from core.normalize import normalize_labelcode, strict_labelcode
from core.resolver import (catalog_candidate, index_settles, local_result, merge_index_writers, resolve,
                           resolve_by_title, resolve_from_index, resolve_reliably)
from reconcile.csv_io import read_supplemental

_noop = lambda *a, **k: None

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent                       # dev-Layout: data/ + docs/ eine Ebene höher


def _bundle_base() -> Path:
    """Basis der gebündelten Read-only-Dateien (web/, data/). Im PyInstaller-Build
    liegt alles unter sys._MEIPASS; im Normalbetrieb neben app.py."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", APP_DIR))
    return APP_DIR


def _user_data_dir() -> Path:
    """Beschreibbarer Ordner für die aktiven DBs (außerhalb von Cloud-synchronisierten Ordnern und des Bundles, kein Admin) —
    Regel in core/trackindex.default_data_dir ($CUECORE_DB, Windows %LOCALAPPDATA%\\CueCore,
    macOS/Linux Application Support/MusicMeta)."""
    return trackindex.default_data_dir()


WEB = _bundle_base() / "web"
DEFAULT_DB = _user_data_dir() / "labels.db"
DB_PATH = Path(os.environ.get("CUECORE_DB", DEFAULT_DB))

# Version der Auslieferung (Rückmeldungen von Testenden, Info.plist in cuecore.spec, Seitenleiste)
APP_VERSION = "0.9.7-beta"

app = FastAPI(title="CueCore", version=APP_VERSION)


@app.exception_handler(Exception)
async def _unhandled(request, exc):
    """Unerwartete Fehler als JSON {"detail": "Typ: Text"} — die UI zeigt das Detail statt einem generischen
    „fehlgeschlagen"; der Traceback geht nach stderr (in der App: cuecore.log)."""
    import traceback
    from fastapi.responses import JSONResponse
    print(f"[500] {request.method} {request.url.path}: {exc.__class__.__name__}: {exc}", flush=True)
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": f"{exc.__class__.__name__}: {exc}"})

# GVL-Index einmal beim Start laden (read-only, threadsicher geteilt).
INDEX = LabelIndex()
DB_META = {"ready": False, "rows": 0, "source_version": "—", "songs": 0, "index": {"present": False, "rows": 0}}
SONGS_SEED = _bundle_base() / "data" / "songs.db"   # Repo-/Bundle-Stand zum einmaligen Seeden


def _data_file(name: str) -> Path:
    """Read-only Datendatei: im Build unter _MEIPASS/data, im Dev-Layout unter <Projekt>/data."""
    bundled = _bundle_base() / "data" / name
    return bundled if bundled.exists() else PROJECT_DIR / "data" / name


# Eingebauter AcoustID-Key der Open Beta (core/settings.BUILTIN_ENV): liegt nur im Bundle, beim Start aus dem
# Quellcode gibt es keinen. Alle AcoustID-Stellen lesen settings.effective — ein eigener Key gewinnt.
settings.BUILTIN_PATH = _data_file("builtin_keys.bin")


def _gvl_paths() -> tuple[Path, Path, Path]:
    """Master-CSV, Datenordner (archive/, diff_*.csv) und docs/ (Report) fürs GVL-PDF-Update.
    Dev-Layout: <Projekt>/data und <Projekt>/docs (git-versioniert). Gebündelte App: <Datenordner>/gvl/,
    beim ersten Mal aus dem Bundle geseedet — ins Bundle darf nichts geschrieben werden, und
    PROJECT_DIR zeigt dort ins Leere."""
    if not getattr(sys, "frozen", False):
        return PROJECT_DIR / "data" / "GVL_Labelcodes_synced.csv", PROJECT_DIR / "data", PROJECT_DIR / "docs"
    import shutil
    d = DB_PATH.parent / "gvl"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("GVL_Labelcodes_synced.csv", "GVL_Labelcodes_synced.meta.json"):
        src = _bundle_base() / "data" / name
        if not (d / name).exists() and src.exists():
            shutil.copy(src, d / name)
    return d / "GVL_Labelcodes_synced.csv", d, d / "docs"
SONGS_CON = None                              # aktive, indizierte Verbindung (außerhalb von Cloud-Ordnern)
# GVL-Master + Zusatzliste OHNE manuelle Labels — Prüfmaßstab für gelieferte Codes (core/labelcheck)
GVL_MASTER = labelcheck.GvlMaster([])
INDEX_CON = None                              # lokaler Track-Index (track_index.db neben labels.db), None = keiner
_INDEX_DL = {"state": "idle", "phase": "", "done": 0, "total": 0, "error": ""}   # Index-Download aus der App
_INDEX_DL_LOCK = threading.Lock()
_INDEX_DL_CANCEL: threading.Event | None = None
_INDEX_PATH: Path | None = None


# --- Label-Katalog: händisch angelegte Labels (labels_manual.json neben labels.db) ---

def _manual_path() -> Path:
    return DB_PATH.parent / "labels_manual.json"


def _manual_labels() -> list[dict]:
    p = _manual_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    # Die Datei ist hand-editierbar: nur saubere Einträge durchlassen (Dict, Name als Text,
    # Code als Ziffern-String mit führenden Nullen) — sonst stirbt der App-Start am Import.
    out: list[dict] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        code = normalize_labelcode(re.sub(r"\D", "", str(r.get("labelcode", ""))))
        label = " ".join(str(r.get("label") or "").split())
        if not code or not label:
            continue
        out.append({"labelcode": code, "label": label,
                    "hersteller": " ".join(str(r.get("hersteller") or "").split()),
                    "note": str(r.get("note") or "").strip(), "created": str(r.get("created") or "")})
    return out


def _add_delivery_labels(unknown: dict, *, library: str, source: str) -> list[dict]:
    """Fremde Codes einer Library-Lieferung als manuelle Labels anlegen (labels_manual.json + labels.db), wie
    im CLI-Weg (import_library_tracks.py). Rückgabe: die neu angelegten."""
    if not unknown or not DB_PATH.exists():
        return []
    con = connect(DB_PATH)
    try:
        return labelcheck.add_manual_labels(con, _manual_path(), labelcheck.manual_label_entries(
            unknown, library=library, source=source, stamp=date.today().isoformat()))
    finally:
        con.close()


# Je Lauf höchstens eine Sicherung je labels.db, bevor die Selbstheilung beim Start sie umschreibt
_LABEL_BACKUPS: set[Path] = set()


def _backup_before_heal(con) -> None:
    """before_write der Selbstheilung: beim ersten wirklichen Schreiben dieses Laufs labels.db und
    labels_manual.json datiert daneben sichern (labelcheck.backup_label_files)."""
    if DB_PATH in _LABEL_BACKUPS:
        return
    _LABEL_BACKUPS.add(DB_PATH)
    for p in labelcheck.backup_label_files(con, DB_PATH, _manual_path(), datetime.now().strftime("%Y-%m-%d_%H%M%S")):
        print(f"Selbstheilung: Sicherung vorher angelegt: {p}", flush=True)


def _save_manual_labels(rows: list[dict]) -> None:
    p = _manual_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(rows, key=lambda r: (r["labelcode"], r["label"].lower())),
                            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")



_INDEX_MAPS = ("code2name", "code2hersteller", "full", "stripped", "short2code", "prefix2code",
               "hersteller", "hersteller_name", "squash2code", "mb_alias")
_SONGS_PATH: Path | None = None


def _swap_index(idx: LabelIndex | None) -> None:
    """Den geteilten INDEX in einem kurzen Schritt auf den neuen Stand bringen (kein leerer
    Index, während aus labels.db gelesen wird — Requests laufen im Threadpool weiter)."""
    for name in _INDEX_MAPS:
        d = getattr(INDEX, name)
        d.clear()
        if idx is not None:
            d.update(getattr(idx, name))


def _load_track_index() -> None:
    """Lokalen Track-Index öffnen, wenn track_index.db im Datenordner liegt — sonst läuft
    die App ohne diese Stufe. Wird bei jedem _load() geprüft, damit ein frisch gebauter Index ohne
    Neustart greift (Reload nach „Label anlegen“ o.ä.)."""
    global INDEX_CON, _INDEX_PATH
    if _INDEX_DL["state"] != "running":         # Reste eines abgebrochenen Downloads (Fenster mittendrin zu)
        for name in indexdownload.cleanup(DB_PATH.parent):
            print(f"Track-Index: Rest eines abgebrochenen Downloads entfernt: {name}", flush=True)
    path = trackindex.path_for(DB_PATH.parent)
    if INDEX_CON is not None and (_INDEX_PATH != path or not path.exists()):
        try: INDEX_CON.close()
        except Exception: pass
        INDEX_CON = None
    if INDEX_CON is None and path.exists():
        try:
            INDEX_CON = trackindex.open_index(path)
            _INDEX_PATH = path
        except sqlite3.OperationalError as e:
            # z.B. „database is locked“, während build_track_index.py compact/titles die Datei umbaut:
            # App startet ohne Index (wie ohne Datei) statt abzubrechen; der nächste _load() versucht es erneut.
            print(f"Track-Index nicht geöffnet ({e}) — läuft gerade ein Bau? App startet ohne Index.")
            INDEX_CON = None
    st = trackindex.stats(INDEX_CON, path)
    DB_META["index"] = {"present": st["present"], "rows": st["rows"], "size_bytes": st["size_bytes"],
                        "by_source": st["by_source"]}


def _load() -> None:
    global SONGS_CON, _SONGS_PATH, GVL_MASTER
    songs_path = DB_PATH.parent / "songs.db"
    existed = songs_path.exists()
    songsdb.seed(songs_path, SONGS_SEED)
    if SONGS_CON is None or _SONGS_PATH != songs_path:
        # Nur bei Pfadwechsel neu öffnen — ein Reload nach „Label anlegen“ darf laufenden
        # Requests (EDL-Resolve, Tagger-Apply) nicht die Verbindung unterm Hintern schließen.
        if SONGS_CON is not None:
            try: SONGS_CON.close()
            except Exception: pass
        if existed:
            # Erster Start einer neuen Version auf vorhandenen Daten: songs.db vor dem Umschreiben sichern (open
            # rechnet die Suchschlüssel neu, die Bestandsheilungen unten schreiben Zeilen um) — eine frische Kopie
            # des Seeds braucht das nicht
            dst = songsdb.backup_before_heal(songs_path, datetime.now().strftime("%Y-%m-%d_%H%M%S"))
            if dst:
                print(f"Selbstheilung: Sicherung vorher angelegt: {dst}", flush=True)
        SONGS_CON = songsdb.open(songs_path)
        _SONGS_PATH = songs_path
    DB_META["songs"] = songsdb.count(SONGS_CON)
    _load_track_index()
    if not DB_PATH.exists():
        _swap_index(None)
        DB_META.update(ready=False, rows=0, source_version="—")
        return
    con = connect(DB_PATH)
    try:
        labeldb.add_manual_labels(con, _manual_labels())   # überleben den Neuaufbau aus dem PDF
        # Selbstheilung: Zusatz-Codes, die der DB fehlen (PDF-Update vor dem merge-Fix, alte Installation,
        # neuer Build mit mehr Codes) nachtragen — nur fehlende Codes, kein Neuaufbau (GVL-Stand bleibt).
        # Jede Schreibstelle der Selbstheilung sichert vorher labels.db + labels_manual.json (einmal je Lauf)
        backup = lambda: _backup_before_heal(con)          # noqa: E731
        healed = labeldb.add_supplemental_labels(
            con, read_supplemental(_data_file("labelcodes_supplemental.csv"), set()), before_write=backup)
        if healed:
            print(f"labels.db: {healed} Zusatz-Codes aus labelcodes_supplemental.csv nachgetragen", flush=True)
        GVL_MASTER = labelcheck.GvlMaster.from_db(con)
        # Library-Lieferungen (data/library_tracks/*.csv, verifiziert): einmal je Datei in die songs.db —
        # wie die Zusatz-Codes eine Selbstheilung, damit jede Installation die Daten ohne Handarbeit hat
        # (60.938 Cezame-Zeilen, 3 s). Tests schalten das per CUECORE_SKIP_LIBRARY_IMPORT ab (conftest):
        # jede frische Test-DB bekäme sonst den vollen Import, 100 Tests × 3 s.
        skip = bool(os.environ.get("CUECORE_SKIP_LIBRARY_IMPORT"))
        manual_dst = _manual_path()
        for rep in [] if skip else librarytracks.ensure_imported(SONGS_CON, _data_file("library_tracks"), GVL_MASTER,
                                                                  date_verified=date.today().isoformat()):
            print(f"songs.db: Library {rep['library']} aus {rep['file']} eingepflegt — {rep['inserted']} neu, "
                  f"{rep['updated']} vorhanden, {rep['files']} Dateinamen", flush=True)
            labelcheck.add_manual_labels(con, manual_dst, labelcheck.manual_label_entries(
                rep["unknown_codes"], library=rep["library"], source=rep["file"], stamp=date.today().isoformat()),
                before_write=backup)
        # Bestand heilen: früher eingepflegte Lieferungen können fremde Codes tragen, deren Name im Master unter
        # einem anderen Code steht (BBC Production Music LC21110 -> LC27926), und für die übrigen fremden Codes
        # kann das manuelle Label fehlen. Billig (eine Gruppierung je Tabelle), läuft immer.
        heal = librarytracks.heal_library_codes(SONGS_CON, GVL_MASTER)
        for c in heal["corrected"]:
            print(f"songs.db: LC{c['from']} „{c['label']}“ -> GVL LC{c['to']} ({c['files']} Dateien, {c['songs']} Tracks)",
                  flush=True)
        for code, info in heal["unknown_codes"].items():
            labelcheck.add_manual_labels(con, manual_dst, labelcheck.manual_label_entries(
                {code: info}, library=info.get("library", ""), source="Bestand", stamp=date.today().isoformat()),
                before_write=backup)
        # Bestand: Lieferungszeilen ohne Code, deren Labelname der Master eindeutig führt ('Hooked'
        # u.a.), bekommen den GVL-Code mit Notiz. Neue Lieferungen bekommen ihn schon beim Import (to_song_rows).
        for c in librarytracks.fill_codes_by_name(SONGS_CON, GVL_MASTER)["filled"]:
            print(f"songs.db: „{c['label']}“ ohne Labelcode -> GVL LC{c['code']} über den Labelnamen "
                  f"({c['files']} Dateien, {c['songs']} Tracks)", flush=True)
        # Automatisch angelegte Labels, die der GVL-Code überholt hat, stünden in der Suche vor ihm — aus der JSON
        # und aus labels.db (ältere labels_prebuilt.db-Stände trugen LC21110/LC26766 selbst)
        manual = _manual_labels()
        old = labelcheck.obsolete_delivery_labels(manual + labelcheck.db_only_manual_labels(con, manual), GVL_MASTER)
        if old:
            labelcheck.remove_manual_labels(con, manual_dst, old, before_write=backup)
            print("labels_manual.json: durch GVL-Code überholt, entfernt: "
                  + ", ".join(f"LC{e['labelcode']} {e['label']}" for e in old), flush=True)
        songsdb.mark_healed(SONGS_CON)
        DB_META["songs"] = songsdb.count(SONGS_CON)
        idx = build_index_from_db(con)
        attach_library_prefixes(idx, _data_file("library_prefixes.csv"))   # Sub-Label-Kürzel (fein)
        librarylists.attach_user_prefixes(idx, DB_PATH.parent)               # vom Nutzer hochgeladene Listen (gelieferte Tabelle gewinnt)
        attach_mb_aliases(idx, _data_file("label_aliases_mb.csv"))          # MusicBrainz-Schreibvarianten (GVL-geprüfte Codes)
        rows = con.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
        row = con.execute("SELECT value FROM meta WHERE key='source_version'").fetchone()
    finally:
        con.close()
    _swap_index(idx)
    DB_META.update(rows=rows, source_version=row[0] if row else "—", ready=True)


def configure(db_path: str | Path) -> None:
    """DB-Pfad umstellen + neu laden (für Tests)."""
    global DB_PATH
    DB_PATH = Path(db_path)
    _load()


_load()


def _row(r) -> dict:
    return {
        "labelcode": r["labelcode"], "label": r["label"], "kurzname": r["kurzname"],
        "plm": r["plm"], "hersteller": r["hersteller"], "rechterueckrufe": r["rechterueckrufe"],
        "source": r["source"] if "source" in r.keys() else "gvl",
    }


class ManualLabelIn(BaseModel):
    labelcode: str
    label: str
    hersteller: str = ""
    note: str = ""


@app.get("/api/labels/manual")
def manual_labels_list() -> dict:
    return {"labels": _manual_labels(), "path": str(_manual_path())}


def _label_conflicts(con, code: str, label: str, hersteller: str) -> list[dict]:
    """Konfliktprüfung vor dem Anlegen eines Labels — der Mensch sieht, was GVL-Master und
    eigene Labels zu Code und Name schon sagen:
      duplicate  (error): Code + Name gibt es schon.
      code_taken (warn/info): Code trägt andere Namen — warn, wenn der Hersteller ein anderer ist
                  oder unbekannt (Beispiel APL „Benztown“ LC89388 = GVL „Fake Palm Records“);
                  info, wenn der Hersteller derselbe ist (Umbenennung, Sub-Label, 1:n).
      name_known (warn): der Name steht schon unter einem ANDEREN Code.
    Rückgabe leer = nichts spricht dagegen."""
    from core.labelmatch import norm
    out: list[dict] = []
    same_code = [_row(r) for r in lookup_by_code(con, code)]
    dup = [r for r in same_code if norm(r["label"]) == norm(label)]
    if dup:
        out.append({"kind": "duplicate", "severity": "error", "rows": dup})
    others = [r for r in same_code if norm(r["label"]) != norm(label)]
    if others:
        hn = norm(hersteller)
        same_h = bool(hn) and any(norm(r["hersteller"] or "") == hn for r in others)
        out.append({"kind": "code_taken", "severity": "info" if same_h else "warn", "rows": others})
    ln = norm(label)
    labeldb.ensure_source_column(con)
    by_name = [_row(r) for r in con.execute(
        "SELECT labelcode,label,kurzname,plm,hersteller,rechterueckrufe,source FROM labels "
        "WHERE lower(label)=lower(?) AND labelcode<>? ORDER BY labelcode", (label, code)).fetchall()]
    if not by_name:   # Schreibvarianten (Umlaute, &/+, Satzzeichen) über die normalisierte Form
        by_name = [_row(r) for r in search_labels(con, label, limit=50)
                   if norm(r["label"]) == ln and r["labelcode"] != code]
    if by_name:
        out.append({"kind": "name_known", "severity": "warn", "rows": by_name})
    return out


@app.post("/api/labels/check")
def manual_label_check(body: ManualLabelIn) -> dict:
    """Trockenlauf für „Label anlegen“: nur prüfen, nichts schreiben."""
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    digits = re.sub(r"\D", "", body.labelcode)
    label = " ".join(body.label.split())
    if not re.fullmatch(r"\d{4,6}", digits) or not label:
        return {"warnings": [], "valid": False}
    con = connect(DB_PATH)
    try:
        return {"warnings": _label_conflicts(con, normalize_labelcode(digits), label, body.hersteller), "valid": True}
    finally:
        con.close()


@app.post("/api/labels/manual")
def manual_label_create(body: ManualLabelIn) -> dict:
    """Ein Label händisch anlegen (LC + Name, optional Hersteller/Verlag + Notiz). Landet in
    labels_manual.json UND in labels.db (source='manuell'), Index wird neu geladen. Ein Code, der
    in der GVL schon existiert, darf einen weiteren Namen bekommen (1:n) — die bestehenden Zeilen
    kommen als `existing` zurück, damit der Mensch sieht, was die GVL dazu sagt."""
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    digits = re.sub(r"\D", "", body.labelcode)
    label = " ".join(body.label.split())
    if not re.fullmatch(r"\d{4,6}", digits):
        raise HTTPException(422, "Labelcode: 4 bis 6 Ziffern (z.B. 24584 oder LC 100148)")
    code = normalize_labelcode(digits)
    if not label:
        raise HTTPException(422, "Labelname fehlt")
    con = connect(DB_PATH)
    try:
        existing = [_row(r) for r in lookup_by_code(con, code)]
        warnings = _label_conflicts(con, code, label, body.hersteller)
        dup = next((w["rows"] for w in warnings if w["kind"] == "duplicate"), None)
        if dup:
            # Herkunft der DOPPELTEN Zeile, nicht aller Zeilen des Codes (sonst hieße ein eigenes Label
            # „(GVL-Master)“, sobald der Code im Master einen anderen Namen trägt).
            raise HTTPException(409, f"LC{code} „{label}“ gibt es schon"
                                     + (" (GVL-Master)" if any(r["source"] == "gvl" for r in dup) else ""))
        rows = _manual_labels()
        entry = {"labelcode": code, "label": label, "hersteller": " ".join(body.hersteller.split()),
                 "note": body.note.strip(), "created": date.today().isoformat()}
        rows.append(entry)
        _save_manual_labels(rows)
        labeldb.add_manual_labels(con, [entry])
    finally:
        con.close()
    _load()
    return {"created": {**entry, "source": "manuell"}, "existing": existing, "warnings": warnings, "count": len(rows)}


@app.delete("/api/labels/manual")
def manual_label_delete(body: ManualLabelIn) -> dict:
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    from core.labelmatch import norm
    code = normalize_labelcode(re.sub(r"\D", "", body.labelcode))   # dieselbe Vorbehandlung wie beim Anlegen
    target = norm(body.label)
    rows = _manual_labels()
    keep = [r for r in rows if not (r["labelcode"] == code and norm(r["label"]) == target)]
    con = connect(DB_PATH)
    try:
        removed_db = labeldb.remove_manual_label(con, code, body.label)
    finally:
        con.close()
    if len(keep) == len(rows) and not removed_db:
        raise HTTPException(404, "Kein händisch angelegtes Label mit diesem Code und Namen")
    if len(keep) != len(rows):
        _save_manual_labels(keep)    # erst nach dem DB-Schritt, damit JSON und DB nicht auseinanderlaufen
    _load()
    return {"removed": {"labelcode": code, "label": body.label.strip()}, "count": len(keep)}


# --- Library-Listen des Nutzers (Kürzel -> LC): Vorschau mit Konfliktbericht, dann Übernahme ---

class LibraryListIn(BaseModel):
    filename: str
    content_b64: str
    library: str = ""          # Anzeigename der Library (leer = aus dem Dateinamen)


class LibraryListDeleteIn(BaseModel):
    name: str


def _shipped_prefixes() -> dict[str, dict]:
    """Die gelieferte Kürzel-Tabelle (Repo/Bundle) — gewinnt bei gleichem Kürzel."""
    from core.catalog import load_library_prefixes
    return load_library_prefixes(_data_file("library_prefixes.csv"))


def _library_list_check(body: LibraryListIn) -> tuple[dict, bytes, str]:
    """Datei dekodieren, lesen, Regelwerk anwenden — nichts schreiben. (Bericht, Rohbytes, Name)."""
    import base64
    import tempfile
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    name = librarylists.safe_name(body.filename)
    suffix = Path(name).suffix.lower()
    if suffix not in ingest.SUPPORTED_EXTENSIONS:
        raise HTTPException(422, f"Format nicht unterstützt: {suffix or '(keine Endung)'} "
                                 f"(erlaubt: {', '.join(ingest.SUPPORTED_EXTENSIONS)})")
    try:
        raw = base64.b64decode(body.content_b64, validate=True)
    except Exception:
        raise HTTPException(422, "Datei-Inhalt nicht dekodierbar (Base64 erwartet)")
    if not raw:
        raise HTTPException(422, "Leere Datei")
    # Unter dem hochgeladenen Namen in einen Temp-Ordner: read_user_list leitet Library und Quelle aus dem
    # Dateinamen ab — eine mkstemp-Datei zeigte in der Vorschau einen Zufallsnamen.
    import shutil
    tmpdir = tempfile.mkdtemp(prefix="cuecore_liblist_")
    try:
        tmp = Path(tmpdir) / name
        tmp.write_bytes(raw)
        res = librarylists.read_user_list(tmp, library=body.library)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if "error" in res:
        raise HTTPException(422, res["error"])
    # Bekannt = gelieferte Tabelle + schon hochgeladene Listen (ohne eine gleichnamige, die ersetzt wird)
    known = dict(_shipped_prefixes())
    for key, v in list(INDEX.prefix2code.items()):
        if v.get("user") and key not in known and name not in (v.get("source") or ""):
            known[key] = v
    rep = librarylists.reduce_rows(res["rows"], INDEX.code2name, known=known)
    report = {"filename": name, "library": (body.library or "").strip(), "rows": len(res["rows"]),
              "mapping": res["mapping"], "fieldnames": res["fieldnames"], "warnings": res["warnings"],
              "kept": [{k: r.get(k, "") for k in librarylists.COLUMNS} for r in rep["kept"]],
              "excluded": [{**{k: r.get(k, "") for k in ("prefix", "labelcode", "list_label", "library")}, "why": r["why"]} for r in rep["excluded"]],
              "infos": [{**{k: r.get(k, "") for k in ("prefix", "labelcode", "list_label", "label_gvl", "library")}, "why": r["why"]} for r in rep["infos"]]}
    return report, raw, name


@app.post("/api/labels/lists/preview")
def library_list_preview(body: LibraryListIn) -> dict:
    """Trockenlauf: was würde übernommen, was fliegt raus und warum. Schreibt nichts."""
    report, _, _ = _library_list_check(body)
    return report


@app.post("/api/labels/lists/commit")
def library_list_commit(body: LibraryListIn) -> dict:
    """Liste unter <Datenordner>/library_lists/<Datum>_<Name> ablegen, Nutzer-Tabelle neu bauen,
    Index laden. Antwort = Bericht der Vorschau + Stand aller Nutzer-Listen."""
    report, raw, name = _library_list_check(body)
    d = librarylists.user_lists_dir(DB_PATH.parent)
    d.mkdir(parents=True, exist_ok=True)
    stored = f"{date.today().isoformat()}_{name}"
    (d / stored).write_bytes(raw)
    man = librarylists.read_manifest(DB_PATH.parent)
    man[stored] = {"library": report["library"], "original": body.filename, "uploaded": date.today().isoformat()}
    librarylists.write_manifest(DB_PATH.parent, man)
    rebuilt = librarylists.rebuild_user_prefixes(DB_PATH.parent, INDEX.code2name, _shipped_prefixes())
    _load()
    return {**report, "stored": stored, "user_prefixes": len(rebuilt["kept"]),
            "files": rebuilt["files"], "attached": sum(1 for v in INDEX.prefix2code.values() if v.get("user"))}


@app.get("/api/labels/lists")
def library_lists_list() -> dict:
    base = DB_PATH.parent
    return {"files": librarylists.list_user_files(base), "dir": str(librarylists.user_lists_dir(base)),
            "attached": sum(1 for v in INDEX.prefix2code.values() if v.get("user")),
            "shipped": sum(1 for v in INDEX.prefix2code.values() if not v.get("user"))}


@app.delete("/api/labels/lists")
def library_list_delete(body: LibraryListDeleteIn) -> dict:
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    d = librarylists.user_lists_dir(DB_PATH.parent)
    target = d / librarylists.safe_name(body.name)
    if not target.exists() or target.parent != d:
        raise HTTPException(404, "Liste nicht gefunden")
    target.unlink()
    man = librarylists.read_manifest(DB_PATH.parent)
    if man.pop(target.name, None) is not None:
        librarylists.write_manifest(DB_PATH.parent, man)
    rebuilt = librarylists.rebuild_user_prefixes(DB_PATH.parent, INDEX.code2name, _shipped_prefixes())
    _load()
    return {"removed": target.name, "files": rebuilt["files"], "user_prefixes": len(rebuilt["kept"]),
            "attached": sum(1 for v in INDEX.prefix2code.values() if v.get("user"))}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.get("/web/{name}")
def web_asset(name: str) -> FileResponse:
    """Bilddateien aus web/ (Logo-PNGs). Nur Dateinamen ohne Pfad, nur Bildendungen."""
    if name != Path(name).name or Path(name).suffix.lower() not in {".png", ".svg", ".ico"}:
        raise HTTPException(404)
    f = WEB / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


@app.get("/web/fonts/{name}")
def web_font(name: str) -> FileResponse:
    """Gebündelte Schriften (web/fonts: fonts.css + woff2). Google Fonts im <head> ließen WebKit auf
    fonts.gstatic.com warten — hinter einer Netzsperre oder offline blieb das Fenster schwarz."""
    if name != Path(name).name or Path(name).suffix.lower() not in {".woff2", ".css"}:
        raise HTTPException(404)
    f = WEB / "fonts" / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="text/css" if f.suffix == ".css" else "font/woff2")


@app.get("/api/health")
def health() -> dict:
    return {
        "product": "CueCore", "version": APP_VERSION, "ready": DB_META["ready"], "rows": DB_META["rows"],
        "source_version": DB_META["source_version"], "songs": DB_META["songs"], "db_path": str(DB_PATH),
        "sources": _configured_sources(),     # Rail: welche externen Quellen sind eingerichtet
        "index": DB_META["index"],            # lokaler Track-Index: vorhanden, Zeilen, Größe
    }


@app.get("/api/index/status")
def index_status() -> dict:
    """Lokaler Track-Index (track_index.db neben labels.db): Zeilen je Quelle, Größe, Dump-Stände,
    Pfad — für die Einstellungen. Bau per `build_track_index.py` (Script, kein Netz in der App)."""
    path = trackindex.path_for(DB_PATH.parent)
    st = trackindex.stats(INDEX_CON, path)
    st["path"] = str(path)
    st["data_dir"] = str(DB_PATH.parent)
    st["download"] = {"size": indexdownload.SIZE, "unpacked": indexdownload.UNPACKED, "stand": indexdownload.STAND}
    return st


# Track-Index aus der App laden (die App soll ohne Dateischieben starten). Läuft im
# Hintergrund-Thread; die Oberfläche fragt den Stand ab. Schreibt nur track_index.db, nie songs.db. Der Stand
# (_INDEX_DL) steht oben bei INDEX_CON, weil _load_track_index ihn schon beim Import braucht.


def _close_track_index() -> None:
    """Vor dem Tausch: offene Verbindung schließen (Windows sperrt die Datei sonst)."""
    global INDEX_CON
    if INDEX_CON is not None:
        try: INDEX_CON.close()
        except Exception: pass
        INDEX_CON = None


def _no_app_nap(reason: str):
    """macOS: App Nap aus, solange eine lange Arbeit läuft (mit dem Fenster im Hintergrund entpackt die App den
    Track-Index deutlich langsamer). Gibt die Funktion zum Beenden
    zurück; außerhalb von macOS oder ohne pyobjc gibt es nichts zu tun."""
    if sys.platform != "darwin":
        return lambda: None
    try:
        from Foundation import NSActivityUserInitiated, NSProcessInfo
        info = NSProcessInfo.processInfo()
        token = info.beginActivityWithOptions_reason_(NSActivityUserInitiated, reason)
        return lambda: info.endActivity_(token)
    except Exception as e:
        print(f"[app-nap] nicht abschaltbar: {e.__class__.__name__}: {e}", flush=True)
        return lambda: None


def _run_index_download(cancel: threading.Event) -> None:
    def progress(phase: str, done: int, total: int) -> None:
        _INDEX_DL.update(phase=phase, done=done, total=total)
    end_activity = _no_app_nap("CueCore lädt den Track-Index")
    try:
        indexdownload.install(DB_PATH.parent, progress=progress, cancel=cancel, before_replace=_close_track_index)
        _load_track_index()
        _INDEX_DL.update(state="done", phase="done")
    except indexdownload.Cancelled:
        _INDEX_DL.update(state="cancelled")
    except indexdownload.DownloadError as e:
        _INDEX_DL.update(state="error", error=str(e))
    except Exception as e:                      # Netz weg, Datenträger voll, …: Grund sichtbar, nie still
        print(f"[index-download] {e.__class__.__name__}: {e}", flush=True)
        _INDEX_DL.update(state="error", error=f"{e.__class__.__name__}: {e}")
        _load_track_index()                     # ein vorhandener Index bleibt nutzbar
    finally:
        end_activity()


@app.get("/api/index/download")
def index_download_status() -> dict:
    return dict(_INDEX_DL)


@app.post("/api/index/download")
def index_download_start() -> dict:
    global _INDEX_DL_CANCEL
    with _INDEX_DL_LOCK:
        if _INDEX_DL["state"] == "running":
            return dict(_INDEX_DL)
        cancel = threading.Event()
        _INDEX_DL_CANCEL = cancel
        _INDEX_DL.update(state="running", phase="download", done=0, total=indexdownload.SIZE, error="")
    threading.Thread(target=_run_index_download, args=(cancel,), daemon=True).start()
    return dict(_INDEX_DL)


@app.post("/api/index/download/cancel")
def index_download_cancel() -> dict:
    if _INDEX_DL_CANCEL is not None:
        _INDEX_DL_CANCEL.set()
    return dict(_INDEX_DL)


def _open_folder(path: Path) -> None:
    """Ordner im Finder/Explorer öffnen."""
    import subprocess
    if sys.platform.startswith("win"):
        os.startfile(str(path))          # noqa: S606 — eigener Datenordner
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


@app.post("/api/data-folder/open")
def open_data_folder() -> dict:
    """Datenordner zeigen: dort liegen Datenbanken, Einstellungen und Protokoll; die App selbst wird nie beschrieben."""
    d = DB_PATH.parent
    _open_folder(d)
    return {"path": str(d)}


@app.get("/api/search")
def search(q: str = "", limit: int = 30) -> dict:
    q = q.strip()
    if not q or not DB_META["ready"]:
        return {"query": q, "mode": "none", "results": []}
    con = connect(DB_PATH)
    try:
        digits = "".join(ch for ch in q if ch.isdigit())
        if digits and len(digits) >= 4 and len(q) <= 8:          # sieht nach LC-Nummer aus
            rows = lookup_by_code(con, q)
            return {"query": q, "mode": "code", "results": [_row(r) for r in rows]}
        rows = search_labels(con, q, limit=limit)
        return {"query": q, "mode": "fts", "results": [_row(r) for r in rows]}
    finally:
        con.close()


class ResolveIn(BaseModel):
    artist: str
    title: str
    isrc: str = ""        # optional: schaltet die exakte ISRC-Stufe-0 frei (EDL/Tags)
    album: str = ""       # optional: Interpret + Album (Titel leer) listet die Tracks des Albums
    limit: int = 20       # Titel-only: Index-Zeilen je Seite
    offset: int = 0       # Titel-only: „mehr zeigen“ (Recall vor Präzision — Runaway hat 1.879 Schlüssel)
    net: bool = False     # Titel-only: Wikidata auch fragen, wenn Datenbank/Index schon Treffer haben


# Ein Fetcher für eine ganze Anfrage mit mehreren Auflösungen (Cue-Sheet-Batch, Online-Import, Autoren): das
# Dauerfehler-Budget (netfetch.FAIL_BUDGET) gilt dann für den Batch, nicht je Cue neu — sonst liefen
# 10 Cues bei totem Netz 10 × 40 s. Gesetzt nur innerhalb der Route (_batch_fetcher), sonst None.
_BATCH_FETCHER: contextvars.ContextVar = contextvars.ContextVar("cuecore_batch_fetcher", default=None)


def _fetch_stats(f, before: tuple = (0, 0, 0)) -> dict:
    """calls/cache_hits/failures eines Fetchers, optional als Differenz zu einem früheren Stand."""
    return {"calls": f.calls - before[0], "cache_hits": f.cache_hits - before[1], "failures": f.failures - before[2]}


def _resolve_net(artist: str, title: str, isrc: str = ""):
    """(ResolveResult, fetcher) — immer der volle Netz-Resolver (MB+Discogs+Deezer+
    Wikidata) + Enrich, ohne den lokalen Kurzschluss. Für /resolve/merge, das DB UND
    Netz braucht. Bei Rate-Limit-Lücke (`incomplete`) wird automatisch nachgefasst
    (resolve_reliably) — derselbe Fetcher, also warmer Cache beim 2. Lauf.

    `isrc` (aus den Datei-Tags/der EDL) schaltet die exakte Stufe 0 des Resolvers frei."""
    fetcher = _BATCH_FETCHER.get() or netfetch.build_fetcher(DB_PATH)   # Cache + zentraler Rate-Limit-Scheduler

    def run():
        before = fetcher.failures
        res = resolve(
            artist, title, INDEX,
            isrc=isrc,
            isrc_lookup=lambda i: musicbrainz.lookup_by_isrc(i, fetch=fetcher, sleep=_noop),
            mb_lookup=lambda a, t: musicbrainz.lookup(a, t, fetch=fetcher, sleep=_noop),
            discogs_label_codes=lambda rid: discogs.lookup_label_codes(rid, fetch=fetcher, sleep=_noop),
            deezer_lookup=lambda a, t: deezer.lookup(a, t, fetch=fetcher, sleep=_noop),
            wikidata_lookup=lambda a, t: wikidata.lookup(a, t, fetch=fetcher, sleep=_noop),
            work_lookup=lambda t: musicbrainz.search_work_writers(t, fetch=fetcher, sleep=_noop),
            genius_lookup=(lambda a, t: genius.lookup(a, t, fetch=fetcher)) if settings.get("genius_token") else None,
        )
        _enrich(res, artist, title, fetcher)
        # 'Nie leer aufhören': nur wenn diese Runde GAR KEINEN Kandidaten fand UND dabei
        # ein Quelle-Fetch-Fehler auftrat (Deezer/Wikidata/Discogs 429/Timeout), war das
        # Leer-Ergebnis wahrscheinlich transient -> nachfassen (Cache ist beim 2. Lauf warm).
        # Steht schon ein Kandidat, verzichten wir bewusst auf den teuren Voll-Retry, nur um
        # eine Zusatz-Korroboration nachzuholen (WDQS flaket -> sonst Timeout-Gefahr).
        if fetcher.failures > before and not res.candidates:
            res.incomplete = True
        return res

    return resolve_reliably(run), fetcher


def _resolve_one(artist: str, title: str, isrc: str = ""):
    """(ResolveResult, fetcher|None). Stufe 1 verifizierte lokale DB (kein Netz), Stufe 1b
    lokaler Track-Index (kein Netz, Band review) — nur wenn EINDEUTIG (index_settles); bei Gleichstand im
    Index läuft der volle Resolver und bekommt nur Komponist/Texter aus dem Index nachgefüllt.
    Geteilt von /resolve + Intake."""
    hit = songsdb.find(SONGS_CON, artist, title)
    if hit:
        return local_result(artist, title, hit), None
    ix = _resolve_index(artist, title)
    if index_settles(ix):
        return ix, None
    res, fetcher = _resolve_net(artist, title, isrc)
    return merge_index_writers(res, ix), fetcher


def _resolve_index(artist: str, title: str):
    """Stufe 1b: lokaler Track-Index (Discogs-/MusicBrainz-Dump, kein Netz). None ohne Index oder
    ohne Treffer mit GVL-Code -> der Aufrufer geht ins Netz. Nie Band `auto` (nutzergepflegte Daten)."""
    if INDEX_CON is None:
        return None
    return resolve_from_index(artist, title, INDEX,
                              index_lookup=lambda a, t: trackindex.lookup(INDEX_CON, a, t))


def _resolve_title(title: str):
    """(TitleResolveResult, fetcher) — Titel-only-Pfad: Wikidata liefert Interpret-
    Kandidaten (Performer) mit Label-Preview. Kein stilles Raten — die verbindliche,
    doppelt verifizierte LC entsteht erst, wenn der Nutzer einen Interpret wählt und
    darüber der volle Resolver läuft."""
    fetcher = netfetch.build_fetcher(DB_PATH)
    tr = resolve_by_title(
        title, INDEX,
        wikidata_items_lookup=lambda t: wikidata.lookup_items(t, fetch=fetcher, sleep=_noop),
    )
    return tr, fetcher


def _audd_recognize(path: str, expected_artist: str, expected_title: str):
    """AudD-Fingerprinting-Callback (Stufe 4 der Erkennung) — None ohne Token gesetzt.
    Kostenpflichtig, daher nur aus dem Fallback-Pfad aufgerufen (Tags+Name versagt)."""
    token = settings.get("audd_token")
    if not token:
        return None
    return audd.recognize(path, token, expected_artist=expected_artist, expected_title=expected_title)


def _fingerprint_recognize(path: str, expected_artist: str, expected_title: str):
    """Fingerabdruck-Kette: erst AcoustID (frei, lokaler Chromaprint), dann AudD (kostenpflichtig).

    Reihenfolge nach Kosten, nicht nach Qualität — wer AcoustID eingerichtet hat, zahlt für die
    meisten Dateien gar nichts mehr. Der Fingerabdruck erkennt auch
    Dateien, deren Name und Tags nichts taugen.
    """
    key = settings.effective("acoustid_api_key")
    if key:
        hit = acoustid.recognize(path, key, expected_artist=expected_artist, expected_title=expected_title)
        if hit:
            hit["fingerprint_source"] = "acoustid"
            return hit
    hit = _audd_recognize(path, expected_artist, expected_title)
    if hit:
        hit["fingerprint_source"] = "audd"
    return hit


def _label_lookup(code: str) -> tuple[str, str]:
    """(offizielle GVL-Schreibweise, rechterueckrufe) zum Code — aus labels.db."""
    con = connect(DB_PATH)
    try:
        rows = lookup_by_code(con, code)
        return (rows[0]["label"], rows[0]["rechterueckrufe"]) if rows else ("", "")
    finally:
        con.close()


def _suggestion(row: dict) -> dict:
    """Kompakter Song-Auszug — für „Meintest du…?"-Vorschläge und den Resolver-Merge
    (deshalb inkl. der editierbaren Zusatzfelder track_number/iswc/gema_werknummer)."""
    return {"id": row.get("id"), "interpret": row.get("interpret", ""), "titel": row.get("titel", ""),
            "labelname": row.get("labelname", ""), "labelcode": row.get("labelcode", ""),
            "komponist": row.get("komponist", ""), "texter": row.get("texter", ""),
            "verlag": row.get("verlag", ""),
            "album": row.get("album", ""), "track_number": row.get("track_number", ""),
            "isrc": row.get("isrc", ""), "iswc": row.get("iswc", ""),
            "gema_werknummer": row.get("gema_werknummer", "")}


def _artist_tracks(artist: str, album: str = "", limit: int = 60) -> dict:
    """Alle bekannten Titel eines Interpreten: verifizierte aus der songs.db (wortweise Suche im Interpret-Feld)
    plus Index-Zeilen (unscharf über die artists-Tabelle). Verifizierte zuerst, keine Dubletten. Mit `album`
    nur die Tracks dieses Albums (Album-Text enthält den normierten Namen, beide DBs haben das Feld);
    `albums` = Album-Chips über alle Alben des Interpreten (songs.db + Index), unabhängig vom Filter."""
    nalb = songsdb.norm(album or "")
    verified: list[dict] = []
    albums: dict[str, dict] = {}
    if SONGS_CON is not None:
        rows = songsdb.list_songs(SONGS_CON, query=artist, limit=limit)["rows"]
        na = songsdb.norm(artist)
        for r in rows:
            if not (na in songsdb.norm(r["interpret"]) or songsdb.norm(r["interpret"]) in na):
                continue
            if r.get("album"):
                albums.setdefault(songsdb.norm(r["album"]), {"album": r["album"], "n": 0})["n"] += 1
            if nalb and nalb not in songsdb.norm(r.get("album") or ""):
                continue
            verified.append({**_suggestion(r), "verified": True, "sources": []})
    seen = {songsdb._nkey(v["interpret"], v["titel"]) for v in verified}
    index: list[dict] = []
    capped = False
    if INDEX_CON is not None:
        # search() liefert eine Zeile je Interpret+Titel+CODE — die Liste hier zeigt je Titel eine; deshalb mehr
        # Rohzeilen holen und erst nach dem Zusammenfassen kappen (sonst hieß ein 14-Track-Album „gekappt“).
        found = trackindex.search(INDEX_CON, artist, limit=limit * 4, album=album)
        for r in found["rows"]:
            k = trackindex.nkey(r["interpret"], r["titel"])
            if k in seen:
                continue
            seen.add(k)
            index.append({**_index_row(r), "verified": False})
        capped = found["capped"] or len(index) > limit
        index = index[:limit]
        for a in trackindex.albums(INDEX_CON, artist):
            albums.setdefault(songsdb.norm(a["album"]), {"album": a["album"], "n": 0})["n"] += a["n"]
    chips = sorted(albums.values(), key=lambda e: (-e["n"], e["album"]))
    return {"verified": verified, "index": index, "index_capped": capped, "index_present": INDEX_CON is not None,
            "album": album, "albums": chips}


def _title_tracks(title: str, limit: int = 20, offset: int = 0) -> dict:
    """Titel-only ohne Netz (Suche nur nach Titel): verifizierte Zeilen aus der songs.db
    (Titel enthält den Suchtext) plus Interpreten zum Titel aus dem Track-Index (titles + FTS5-Trigram, Teilwort
    trifft), verifizierte zuerst, keine Dubletten. Recall vor Präzision: bis zu `limit` Index-Zeilen mit
    Interpret-Spalte, `index_total` sagt, wie viele Schlüssel den Titel tragen."""
    nt = songsdb.norm(title)
    verified: list[dict] = []
    if SONGS_CON is not None:                                   # immer holen (Dubletten-Schlüssel), nur Seite 1 zeigt sie
        for r in songsdb.list_songs(SONGS_CON, query=title, limit=limit)["rows"]:
            if nt in songsdb.norm(r["titel"]):
                verified.append({**_suggestion(r), "verified": True, "sources": []})
    seen = {songsdb._nkey(v["interpret"], v["titel"]) for v in verified}
    index: list[dict] = []
    total, capped, nxt = 0, False, offset
    if INDEX_CON is not None:
        found = trackindex.search_title(INDEX_CON, title, limit=limit, offset=offset)
        total, capped, nxt = found["total"], found["capped"], found["next_offset"]
        for s in found["rows"]:
            if trackindex.nkey(s["interpret"], s["titel"]) in seen:
                continue
            index.append({"interpret": s["interpret"], "titel": s["titel"], "labelcodes": s["labelcodes"],
                          "labels": [INDEX.code2name.get(c) or l for c, l in zip(s["labelcodes"], s["labels"])],
                          "sources": s["sources"], "komponist": s["komponist"], "texter": s["texter"],
                          "album": s["album"], "n_releases": s["n_releases"], "verified": False})
    return {"verified": verified if offset == 0 else [], "index": index, "index_total": total, "index_capped": capped,
            "index_offset": offset, "index_next": nxt, "index_present": INDEX_CON is not None}


@app.post("/api/resolve")
def resolve_endpoint(body: ResolveIn) -> dict:
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen — zuerst build_label_db.py laufen lassen")
    artist, title, album = body.artist.strip(), body.title.strip(), body.album.strip()
    # Titel-only (Interpret fehlt, z.B. Sync-/Score-Cue): Interpret-Kandidaten anbieten,
    # statt blind zu raten. Der Nutzer wählt -> dann läuft der volle, verifizierte Resolve.
    # Interpret-only (Titel fehlt): Trackliste des Interpreten aus songs.db + Track-Index,
    # kein Netz, optional auf ein Album eingegrenzt. Der Nutzer wählt einen Titel -> dann läuft der normale Resolve.
    if artist and not title:
        return {"mode": "artist_only", "artist": artist, **_artist_tracks(artist, album)}
    if album and not artist and not title:
        raise HTTPException(400, "Album-Suche braucht den Interpret — Interpret + Album eingeben, Titel leer lassen")
    if not artist and title:
        # Library-Dateiname im Titel-Feld („CEZ4030_1“, „BIB213_3.mp3“): verifizierte Zeile der Lieferung mit den
        # Fakten der Datei (LC je Sub-Katalog), kein Netz — wie im Cue-Sheet (songsdb.find_by_file).
        fhit = songsdb.find_by_file(SONGS_CON, title) if SONGS_CON is not None else None
        if fhit:
            res = local_result(fhit["interpret"], fhit["titel"], fhit)
            res.notes.append(f"Library-Datei {fhit['file']} · {fhit.get('sublibrary') or fhit.get('library') or ''}"
                             f"{' ' + fhit['catno'] if fhit.get('catno') else ''} (Lieferung, verifiziert)")
            out = _resolve_dict(res, None)
            out.update({"matched_by": "file", "library": fhit.get("library", ""), "sublibrary": fhit.get("sublibrary") or "",
                        "catno": fhit.get("catno") or "", "file": fhit["file"]})
            return out
        # Erst songs.db + Track-Index (kein Netz); Wikidata nur, wenn beide nichts zum Titel wissen —
        # dieselbe Staffel wie beim vollen Resolve (lokal vor Netz).
        local = _title_tracks(title, limit=max(1, min(int(body.limit), 100)), offset=body.offset)
        if body.offset > 0 or ((local["verified"] or local["index"]) and not body.net):   # Blättern nie ins Netz
            return {"mode": "title_only", "title": title, **local, "title_candidates": [],
                    "notes": ["Wikidata nicht gefragt: Treffer aus Datenbank und Track-Index (kein Netz). "
                              "Interpret oben ergänzen, wenn der richtige fehlt."]}
        tr, fetcher = _resolve_title(title)
        return {"mode": "title_only", "title": tr.title, **local,
                "title_candidates": [asdict(c) for c in tr.candidates], "notes": tr.notes,
                "stats": {"calls": fetcher.calls, "cache_hits": fetcher.cache_hits, "failures": fetcher.failures}}
    res, fetcher = _resolve_one(artist, title, body.isrc.strip())
    out = _resolve_dict(res, fetcher)
    # Kein exakter lokaler Treffer (fetcher gesetzt) -> ähnliche DB-Einträge als
    # „Meintest du…?" anbieten (fängt „dangerous thing" -> „A Dangerous Thing" ab).
    if fetcher is not None and SONGS_CON is not None:
        out["local_suggestions"] = [_suggestion(s) for s in
                                    songsdb.find_similar(SONGS_CON, artist, title)]
    # Kein MusicBrainz-Treffer, weil der Titel nur ein Teil ist („sympathy“ statt „Sympathy for the Devil“):
    # volle Titel desselben Interpreten anbieten. Dieselbe lockere Suche lief schon, kommt aus dem Cache.
    if fetcher is not None and not res.matched and artist and title:
        try:
            out["net_suggestions"] = musicbrainz.title_suggestions(artist, title, fetch=fetcher, sleep=_noop)
        except Exception as e:                    # Vorschläge sind Beiwerk: Grund ins Log, Antwort bleibt
            print(f"[resolve] MusicBrainz-Vorschläge nicht geladen: {e.__class__.__name__}: {e}", flush=True)
    # Dasselbe aus dem Track-Index (Teil-Titel wie „Number“ -> „What's Your Number“); unverifiziert,
    # Klick löst den exakten Schlüssel neu auf (dann Stufe 1b mit Band review).
    if fetcher is not None and INDEX_CON is not None:
        out["index_suggestions"] = [
            {"interpret": s["interpret"], "titel": s["titel"], "labelcodes": s["labelcodes"],
             "labels": [INDEX.code2name.get(c) or l for c, l in zip(s["labelcodes"], s["labels"])],
             "sources": s["sources"], "komponist": s["komponist"], "texter": s["texter"], "album": s["album"]}
            for s in trackindex.find_similar(INDEX_CON, artist, title)]
    return out


# Felder, die zwischen DB-Stand und Netzfund abgeglichen werden (Resolver-Merge).
_MERGE_FIELDS = [("komponist", "Komponist"), ("texter", "Texter"), ("verlag", "Verlag"),
                 ("labelname", "Label"), ("labelcode", "Labelcode"), ("album", "Album"), ("isrc", "ISRC")]


def _merge_fields(db_row: dict | None, net: dict) -> list[dict]:
    """Feld-für-Feld-Vergleich DB vs. Netz. status je Feld:
    same | conflict | net_adds (DB leer, Netz hat was) | db_only | empty."""
    out = []
    for key, label in _MERGE_FIELDS:
        dv = ((db_row.get(key) if db_row else "") or "").strip()
        nv = (net.get(key, "") or "").strip()
        if key == "labelcode":
            dcmp, ncmp = normalize_labelcode(dv), normalize_labelcode(nv)
        else:
            dcmp, ncmp = dv.lower(), nv.lower()
        if not dv and not nv:
            status = "empty"
        elif dv and not nv:
            status = "db_only"
        elif not dv and nv:
            status = "net_adds"
        elif dcmp == ncmp:
            status = "same"
        else:
            status = "conflict"
        out.append({"key": key, "label": label, "db": dv, "net": nv, "status": status})
    return out


@app.post("/api/db/lookup")
def db_lookup(body: ResolveIn) -> dict:
    """Rein lokaler Nachschlag für „DB-Eintrag bearbeiten" — KEIN Netz. Liefert die
    songs.db-Zeile (mit id) zum direkten Bearbeiten, oder db=None (dann Neuanlage).
    Ohne net/fields, damit die UI ein reines Editier-Formular ohne Abgleich zeigt."""
    if SONGS_CON is None:
        raise HTTPException(503, "songs.db nicht geladen")
    artist, title = body.artist.strip(), body.title.strip()
    row = songsdb.find_row(SONGS_CON, artist, title)
    return {"db": _suggestion(row) if row else None,
            "db_id": row.get("id") if row else None,
            "net": {}, "fields": [], "artist": artist, "title": title}


@app.post("/api/resolve/merge")
def resolve_merge(body: ResolveIn) -> dict:
    """Bearbeiten & Abgleichen: liefert den DB-Stand (falls vorhanden, mit id) UND den
    Netzfund nebeneinander, plus einen Feld-für-Feld-Status (Konflikt / Netz ergänzt /
    gleich). Der eigentliche Speichervorgang läuft danach über /api/db/song[/{id}]."""
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    artist, title = body.artist.strip(), body.title.strip()
    db_row = songsdb.find_row(SONGS_CON, artist, title) if SONGS_CON is not None else None
    res, fetcher = _resolve_net(artist, title, body.isrc.strip())
    best = res.best
    tie = bool(getattr(res, "ambiguous", False))     # Margin-Gate: Label/LC nicht vorbelegen
    net = {"komponist": res.composer or "", "texter": res.lyricist or "",
           "verlag": res.publisher or "",
           "labelname": (best.label if best and not tie else "") or "",
           "labelcode": (best.labelcode if best and not tie else "") or "",
           "album": res.album or "", "isrc": res.isrcs[0] if res.isrcs else ""}
    return {
        "db": _suggestion(db_row) if db_row else None,
        "db_id": db_row.get("id") if db_row else None,
        "net": net, "matched": res.matched,
        "fields": _merge_fields(db_row, net),
        "source_urls": (best.source_urls if best else []) or [],
        "band": res.band, "ambiguous": tie, "tied": _tied(res) if tie else [],
        "confidence": best.confidence if best else 0.0,
        "candidates": [asdict(c) for c in res.candidates], "notes": list(res.notes),
        "artist": res.artist, "title": res.title, "author_source_url": getattr(res, "author_source_url", "") or "",
        "stats": {"calls": fetcher.calls, "cache_hits": fetcher.cache_hits, "failures": fetcher.failures},
    }


def _enrich(res, artist: str, title: str, fetcher) -> None:
    """Album/Jahr via iTunes (frei) nachfüllen — nur wo leer. Komponist/Texter kommen
    aus MusicBrainz Work-Relations (im Resolver), Genius war dafür redundant und ist raus.
    Über den Fetcher der Auflösung: Cache, Rate-Limit (~20/min) und Dauerfehler-Budget gelten auch hier."""
    if not res.album:
        it = itunes.lookup(artist, title, fetch=fetcher, sleep=_noop)
        if it:
            res.album = res.album or it.album
            res.date = res.date or it.date


def _resolve_dict(res, fetcher) -> dict:
    stats = ({"calls": fetcher.calls, "cache_hits": fetcher.cache_hits, "failures": fetcher.failures}
             if fetcher else {"calls": 0, "cache_hits": 0, "failures": 0})
    best = asdict(res.best) if res.best else None
    candidates = [asdict(c) for c in res.candidates]
    # Rechterückruf je Kandidat aus der Label-Datenbank
    for c in ([best] if best else []) + candidates:
        c["rechterueckrufe"] = _label_lookup(c["labelcode"])[1] if c.get("labelcode") else ""
    return {
        "artist": res.artist, "title": res.title, "matched": res.matched,
        "mb_score": res.mb_score, "isrcs": res.isrcs, "band": res.band,
        "ambiguous": getattr(res, "ambiguous", False), "tied": list(getattr(res, "tied", [])),
        "composer": res.composer, "lyricist": res.lyricist, "publisher": res.publisher,
        "album": res.album, "date": res.date,
        # incomplete = Ergebnis des letzten (besten) Laufs; fetcher.failures ist über
        # Retry-Läufe kumulativ und würde nach erfolgreichem Nachfassen falsch flaggen.
        "incomplete": res.incomplete,
        "local": fetcher is None and not res.from_index,   # verifizierte songs.db
        "index": res.from_index,                           # lokaler Track-Index (Dump-Daten, kein Netz)
        "best": best,
        "label_hints": res.label_hints,
        "candidates": candidates, "notes": res.notes,
        "stats": stats,
    }


# --- Tagger-Intake: Ordner -> Karten -> Resolve -> Apply (geloggt) -> Undo ---

class IntakeScanIn(BaseModel):
    folder: str


class IntakeResolveIn(BaseModel):
    path: str
    artist: str
    title: str
    isrc: str = ""        # aus den Datei-Tags; leer -> intake liest ihn selbst nach


class IntakePlanIn(BaseModel):
    path: str
    artist: str
    title: str
    labelcode: str
    isrc: str = ""
    album: str = ""
    composer: str = ""
    lyricist: str = ""
    publisher: str = ""          # Verlag: nur songs.db, kein ID3-Frame (TPUB bleibt das Label)


class IntakeApplyIn(BaseModel):
    path: str
    artist: str
    title: str
    labelcode: str
    isrc: str = ""
    album: str = ""
    composer: str = ""
    lyricist: str = ""
    publisher: str = ""          # Verlag: nur songs.db, kein ID3-Frame (TPUB bleibt das Label)
    track_number: str = ""       # nur songs.db (kein Standard-ID3-Tag)
    iswc: str = ""               # nur songs.db (GEMA-Werkabgleich)
    gema_werknummer: str = ""    # nur songs.db (GEMA-Werkabgleich)
    confirm: bool = False


class IntakeUndoIn(BaseModel):
    op_ids: list[int]


def _catalog_fn(text: str, tpub: str = ""):
    """Katalog-Kürzel (CEZ4030_1) -> GVL-Kurzname-Kandidat (review, nie auto). s. core/catalog."""
    return catalog_candidate(text, INDEX, tpub=tpub)


def _verlag_hint(name: str) -> str:
    """TPUB-Tag, der KEIN GVL-Label, aber ein GVL-HERSTELLER ist -> Verlag-Vorschlag
    (Library-Dateien tragen im Label-Tag den Verlag, z.B. 'Frederic Leibovitz Editeur')."""
    if not name or match_label(name, INDEX):
        return ""
    return match_hersteller(name, INDEX)


def _library_file(name: str) -> dict | None:
    """songsdb.find_by_file über die aktive DB — None ohne DB/Treffer (Tagger + Track-Suche)."""
    return songsdb.find_by_file(SONGS_CON, name) if SONGS_CON is not None else None


@app.post("/api/intake/scan")
def intake_scan(body: IntakeScanIn) -> dict:
    """MP3s eines Ordners + Identify. Nicht-destruktiv, kein Netz."""
    folder = body.folder.strip()
    if not folder or not Path(folder).is_dir():
        raise HTTPException(400, f"Kein Ordner: {folder or '—'}")
    # label_lookup nur, wenn der Master geladen ist — sonst kein Dateiname-LC-Vorschlag.
    lookup = _label_lookup if DB_META["ready"] else None
    files = intake.scan_folder(folder, label_lookup=lookup, library_lookup=_library_file)
    return {"folder": folder, "count": len(files), "files": files}


@app.post("/api/intake/resolve")
def intake_resolve(body: IntakeResolveIn) -> dict:
    """Eine Datei auflösen -> Vorschlags-Karte. Schreibt nichts (Dry-Run).

    Fehlen Artist/Titel (Tags + Name identifizieren die Datei nicht), greift der
    Fingerprinting-Fallback (erst AcoustID, frei, dann AudD,
    kostenpflichtig) — nur wenn einer der beiden Schlüssel in den Settings steht. Ohne
    Schlüssel bleibt es beim Hinweis, dass die Datei manuell benannt/getaggt werden muss."""
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    artist, title = body.artist.strip(), body.title.strip()
    if not (artist and title) and not (settings.effective("acoustid_api_key") or settings.get("audd_token")):
        raise HTTPException(400, "Artist und Titel nötig — Datei nicht identifiziert. Für automatische Erkennung "
                                 "einen AcoustID-Key (frei) oder einen AudD-Token (kostenpflichtig) in den "
                                 "Einstellungen hinterlegen.")
    holder: dict = {}

    fhit = _library_file(Path(body.path).name)      # Library-Datei: Fakten der Lieferung je Datei, kein Netz
    if fhit and not (artist and title):
        artist, title = fhit["interpret"], fhit["titel"]

    def rf(a: str, t: str, i: str = ""):
        if fhit and songsdb.same_song(a, t, fhit["interpret"], fhit["titel"]):
            res = local_result(a, t, fhit)
            res.notes.append(f"Library-Datei {fhit['file']} · {fhit.get('sublibrary') or fhit.get('library') or ''}"
                             f"{' ' + fhit['catno'] if fhit.get('catno') else ''} (Lieferung, verifiziert)")
            return res
        res, fetcher = _resolve_one(a, t, i)
        holder["fetcher"] = fetcher
        return res

    card = intake.resolve_card(body.path, artist, title, resolve_fn=rf,
                               label_lookup=_label_lookup, recognize=_fingerprint_recognize,
                               isrc=body.isrc.strip(), catalog_fn=_catalog_fn,
                               verlag_hint_fn=_verlag_hint)
    f = holder.get("fetcher")
    card["stats"] = ({"calls": f.calls, "cache_hits": f.cache_hits, "failures": f.failures}
                     if f else {"calls": 0, "cache_hits": 0, "failures": 0})
    return card


@app.post("/api/intake/plan")
def intake_plan(body: IntakePlanIn) -> dict:
    """Nicht-destruktiver Plan aus benutzergesetzten Werten (manuelle LC-Eingabe
    ODER 'Ändern'-Edits auf der Karte). KEIN Netz, KEIN Schreiben.

    Validiert die LC gegen den GVL-Master (offizielle Schreibweise). can_apply ist
    nur True, wenn der Code IM Master existiert — die Hartstopp-Regel gilt auch hier.
    """
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    artist, title = body.artist.strip(), body.title.strip()
    code = strict_labelcode(body.labelcode)
    unreadable = code is None                    # nie still auf den ersten Ziffernblock kürzen
    code = code or ""
    label, rr = _label_lookup(code) if code else ("", "")
    pl = tagging.plan(body.path, artist, title, code, label,
                      isrc=body.isrc.strip(), rechterueckrufe=rr,
                      album=body.album.strip(), composer=body.composer.strip(),
                      lyricist=body.lyricist.strip())
    warnings = list(pl.warnings)
    if unreadable:
        warnings.append(f"Labelcode „{body.labelcode.strip()}“ nicht lesbar — erwartet z.B. LC00121")
    if code and not label:
        warnings.append(f"LC{code} nicht im GVL-Master — kein Tagging möglich.")
    return {
        "label": label, "labelcode": code, "lc": pl.lc, "rechterueckrufe": rr,
        "in_master": bool(label) if code else None,
        "plan": {"current_tags": pl.current_tags, "proposed_tags": pl.proposed_tags,
                 "current_name": pl.current_name, "proposed_name": pl.proposed_name,
                 "warnings": warnings, "can_apply": bool(label)},
    }


@app.post("/api/intake/apply")
def intake_apply(body: IntakeApplyIn) -> dict:
    """Tags schreiben + nach Schema in den LC-verified-Ordner verschieben. Geloggt, undo-fähig.

    Hartstopp: ohne confirm=True, ohne bestätigten LC, ohne konfigurierten Zielordner
    passiert NICHTS. Labelname kommt IMMER aus dem GVL-Master (offizielle Schreibweise).
    """
    if not body.confirm:
        raise HTTPException(400, "confirm fehlt — kein Schreiben ohne Bestätigung")
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    code = strict_labelcode(body.labelcode)
    if not code:                                 # leer oder nicht lesbar
        raise HTTPException(422, "Kein bestätigter Labelcode — kein Tagging (Default-Stopp)")
    lc_dir = settings.get("lc_verified_dir")
    if not lc_dir:
        raise HTTPException(409, "Kein LC-verified-Ordner in den Settings konfiguriert")
    label, rr = _label_lookup(code)
    if not label:
        raise HTTPException(422, f"LC{code} nicht im GVL-Master — kein Tagging")
    src = Path(body.path)
    if not src.is_file():
        raise HTTPException(404, f"Datei nicht gefunden: {src.name}")

    pl = tagging.plan(src, body.artist.strip(), body.title.strip(), code, label,
                      isrc=body.isrc.strip(), rechterueckrufe=rr,
                      album=body.album.strip(), composer=body.composer.strip(),
                      lyricist=body.lyricist.strip())
    op = operations.open_oplog(DB_PATH.parent / "operations.db")
    try:
        out = tagging.apply(pl, op_con=op, confirm=True, move_to=lc_dir)
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except OSError as e:
        # Tags sind evtl. schon geschrieben, das Verschieben scheiterte (Volume voll, Rechte, Ziel weg):
        # die Datei nicht halb verändert liegen lassen — den Tag-Schritt zurückrollen, Grund nennen.
        rolled = tagging.rollback_last_tag(op, str(src))
        raise HTTPException(500, f"Verschieben fehlgeschlagen: {e}" + (" — Tags zurückgesetzt." if rolled else ""))
    finally:
        op.close()

    # Jeder erfolgreich getaggte Track wird sofort verifizierte Quelle (Stufe 1 der Auflösung).
    # album/isrc/track_number/iswc/gema kommen (ggf. per 'Ändern' korrigiert) von der Karte mit.
    artist, title = body.artist.strip(), body.title.strip()
    before = songsdb.find_row(SONGS_CON, artist, title)
    songsdb.upsert(SONGS_CON, interpret=artist, titel=title,
                   komponist=body.composer.strip(), texter=body.lyricist.strip(),
                   verlag=body.publisher.strip(),
                   labelname=label, labelcode=code, quelle="tagger",
                   isrc=body.isrc.strip(), album=body.album.strip(),
                   track_number=body.track_number.strip(), iswc=body.iswc.strip(),
                   gema_werknummer=body.gema_werknummer.strip(),
                   own_quelle="tagger")    # eigene Tagger-Zeile folgt dem neuen LC; fremde und im Editor
                                           # bearbeitete (quelle 'tagger:manuell') bleiben PROTECT
    SONGS_CON.commit()
    DB_META["songs"] = songsdb.count(SONGS_CON)
    # Der songs.db-Write gehört ins selbe Undo-Log wie Tags und Umbenennung: sonst kam ein
    # zurückgenommener LC als verifizierter AUTO-Treffer zurück, und PROTECT ließ den richtigen nie mehr durch.
    undo_info = songsdb.upsert_undo(before, songsdb.find_row(SONGS_CON, artist, title))
    op = operations.open_oplog(DB_PATH.parent / "operations.db")
    try:
        op_ids = out["op_ids"] + [operations.log(op, "songs", details=json.dumps(undo_info))]
    finally:
        op.close()

    return {"new_path": out["new_path"], "name": Path(out["new_path"]).name,
            "op_ids": op_ids, "label": label, "lc": pl.lc, "rr_pending": rr}


class AlbumResolveIn(BaseModel):
    album: str
    artist: str = ""


@app.post("/api/album/resolve")
def album_resolve(body: AlbumResolveIn) -> dict:
    """Album -> GVL-LC über die ALBUM-Suche (MB-Release + Discogs + musik-sammler),
    wenn die Track-Suche scheitert (Soundtrack/Score-Tracks). Nicht-destruktiv.

    Der Kandidat gilt nur, wenn sein LC IM GVL-Master steht; ≥2 unabhängige Quellen =
    doppelt verifiziert. Übernommen wird der LC danach ganz normal über /intake/apply
    (mit Bestätigung, Labelname aus dem Master)."""
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    album = body.album.strip()
    if not album:
        raise HTTPException(400, "Kein Albumname übergeben")
    fetcher = netfetch.build_fetcher(DB_PATH)
    res = albumresolve.resolve_album(
        album, body.artist.strip(), INDEX,
        mb_search=lambda alb: musicbrainz.search_releases(alb, fetch=fetcher, sleep=_noop),
        discogs_search=lambda alb: discogs.search_release_ids(alb, fetch=fetcher, sleep=_noop),
        discogs_codes=lambda rid: discogs.lookup_label_codes(rid, fetch=fetcher, sleep=_noop),
        ms_confirm=musiksammler.confirms,
    )
    return {
        "album": res.album, "artist": res.artist, "notes": res.notes,
        "candidates": [asdict(c) for c in res.candidates],
        "best": asdict(res.best) if res.best else None,
        "stats": {"calls": fetcher.calls, "cache_hits": fetcher.cache_hits, "failures": fetcher.failures},
    }


@app.post("/api/intake/undo")
def intake_undo(body: IntakeUndoIn) -> dict:
    """Tag-/Rename-Operationen und den songs.db-Eintrag des Apply rückgängig (umgekehrte Reihenfolge)."""
    op = operations.open_oplog(DB_PATH.parent / "operations.db")
    try:
        n = operations.undo(op, body.op_ids, songs_con=SONGS_CON)
    finally:
        op.close()
    if SONGS_CON is not None:
        DB_META["songs"] = songsdb.count(SONGS_CON)
    return {"undone": n}


def _edl_resolve_fn(artist: str, title: str) -> dict | None:
    """Adapter: der volle Resolver (MB+Discogs+Deezer) -> das dict, das
    edl_import.cross_check erwartet (labelname/labelcode/komponist/texter/verlag/isrc)."""
    res, _ = _resolve_one(artist, title)
    best = res.best
    out = {"komponist": res.composer, "texter": res.lyricist, "verlag": res.publisher,
           "isrc": res.isrcs[0] if res.isrcs else ""}
    if getattr(res, "author_source_url", ""):
        out["author_source_url"] = res.author_source_url
    if not best:
        # Kein Label, aber MusicBrainz kennt die Autoren (z.B. Johnny Cash „Hurt“ -> Trent Reznor):
        # die Autoren gehören auf die Karte, der LC bleibt offen.
        return out if (res.composer or res.lyricist or res.publisher) else None
    if getattr(res, "ambiguous", False):
        # Margin-Gate: kein Zufallssieger in die songs.db — Kandidaten mitgeben, Mensch wählt.
        out.update({"labelname": "", "labelcode": "", "ambiguous": True, "tied": _tied(res)})
        return out
    label, _rr = _label_lookup(best.labelcode)
    out.update({"labelname": label or best.label, "labelcode": best.labelcode})
    if res.from_index:
        # Track-Index: Vorschlag auf die Karte, aber KEIN Eintrag in die songs.db — die wächst nur durch
        # bestätigte Cues. Bestätigen = Karte im Editor speichern.
        out["index_hit"] = True
    return out


def _edl_index_fn(artist: str, title: str) -> dict | None:
    """Offline-Variante für den Cue-Sheet-Tab (online=False): nur der lokale Track-Index, kein Netz."""
    res = _resolve_index(artist, title)
    if res is None or not res.best:
        return None
    label, _rr = _label_lookup(res.best.labelcode)
    out = {"komponist": res.composer, "texter": res.lyricist, "verlag": res.publisher, "isrc": "",
           "labelname": label or res.best.label, "labelcode": res.best.labelcode, "index_hit": True}
    if getattr(res, "swapped", False):
        out["swapped"] = True                        # Interpret/Titel kamen vertauscht an — cross_check dreht die Karte
    if res.ambiguous:
        out.update({"labelname": "", "labelcode": "", "ambiguous": True, "tied": _tied(res)})
    return out


def _tied(res) -> list[dict]:
    """Die gleichauf liegenden Kandidaten (Margin-Gate) als kompakte Liste für UI/EDL."""
    codes = set(getattr(res, "tied", []) or [])
    return [{"labelcode": c.labelcode, "label": c.label, "confidence": c.confidence,
             "sources": list(c.sources), "source_urls": list(c.source_urls)}
            for c in res.candidates if c.labelcode in codes]


class EdlImportIn(BaseModel):
    content: str
    fps: float = 25.0
    online: bool = False           # False = nur songs.db (schnell, kostenlos); True = Netz-Lookup
    merge_identical: bool = False  # gleiche Cues zu einer Zeile zusammenfassen (Dauer summiert)
    unify_stems: bool = False      # Stem-Varianten auf den Basistitel vereinheitlichen
    music_only: bool = True        # Videospuren, Länge-0-Zeilen und O-Ton aus Videodateien nicht als Cue werten


@app.post("/api/edl/import")
def edl_import_endpoint(body: EdlImportIn) -> dict:
    """CMX3600-EDL-Text parsen + gegen songs.db cross-checken.

    online=False (Default): verifizierte songs.db + lokaler Track-Index (kein Netz, keine Kosten).
    online=True: fehlende Cues zusätzlich über die Identification Pipeline auflösen und
    als Eintrag (source_type='edl_import') zurückschreiben.
    merge_identical/unify_stems: Cue-Karten für die Abrechnung zusammenfassen (Dauer summiert)."""
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    events = edl.parse_edl(body.content)   # CMX3600, Avid „Assemble List", Final Cut Pro 7 XML, FCPXML, Pro Tools Session-Text (auto)
    if not events:
        raise HTTPException(422, "Keine Cues erkannt — CMX3600-EDL, Avid Assemble List, Final Cut Pro XML (xmeml), FCPXML oder Pro Tools „Session Info as Text“?")
    ignored: list[tuple] = []
    if body.music_only:
        events, ignored = edl.split_music_events(events)
    rf = _edl_resolve_fn if body.online else (_edl_index_fn if INDEX_CON is not None else None)
    ll = _label_lookup if DB_META["ready"] else None   # LC aus Dateiname -> offizielles GVL-Label
    fetcher = netfetch.build_fetcher(DB_PATH) if body.online else None
    token = _BATCH_FETCHER.set(fetcher)              # online: ein Dauerfehler-Budget für alle Clips
    try:
        cards = edl_import.cross_check(events, SONGS_CON, resolve_fn=rf, label_lookup=ll, fps=body.fps,
                                       catalog_fn=_catalog_fn, local_fn=_edl_index_fn if INDEX_CON is not None else None)
    finally:
        _BATCH_FETCHER.reset(token)
    SONGS_CON.commit()
    DB_META["songs"] = songsdb.count(SONGS_CON)
    cards = edl_import.merge_cards(cards, merge_identical=body.merge_identical,
                                   unify_stems=body.unify_stems)
    edl_import.attach_rights(cards, ll)       # Zusammenfassen kann den LC aus einer späteren Karte füllen
    from collections import Counter
    total = sum(c.get("duration_sec", 0) or 0 for c in cards)
    out = {"events": len(events) + len(ignored), "cues": len(cards), "cards": cards,
           "summary": dict(Counter(c["status"] for c in cards)), "total_duration_sec": total,
           "ignored": [{"event_num": ev.event_num, "track": ev.track_type, "clip_name": ev.clip_name,
                        "record_in": ev.record_in, "reason": reason} for ev, reason in ignored]}
    if fetcher is not None:
        out["stats"] = _fetch_stats(fetcher)
    return out


class EdlListImportIn(BaseModel):
    filename: str
    content_b64: str               # CSV/XLSX/XLS, Base64 (xlsx ist binär — kein Text-Body)
    fps: float = 25.0
    online: bool = False
    merge_identical: bool = False
    unify_stems: bool = False


@app.post("/api/edl/import-list")
def edl_import_list_endpoint(body: EdlListImportIn) -> dict:
    """Cue-Liste (Excel/CSV: Interpret, Titel, optional Clip/Dateiname, Dauer, weitere Spalten)
    statt EDL einlesen — jede Zeile wird ein Cue, dann derselbe Weg wie /api/edl/import
    (songs.db, optional Netz, Karten). Spalten der Liste füllen leere Kartenfelder vor; ein LC
    aus der Liste nur, wenn der GVL-Master ihn kennt. Antwort wie /api/edl/import plus
    mapping/fieldnames/rows/list_warnings."""
    import base64
    import tempfile
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    suffix = Path(body.filename).suffix.lower()
    if suffix not in ingest.SUPPORTED_EXTENSIONS:
        raise HTTPException(422, f"Format nicht unterstützt: {suffix or '(keine Endung)'} "
                                 f"(erlaubt: {', '.join(ingest.SUPPORTED_EXTENSIONS)})")
    try:
        raw = base64.b64decode(body.content_b64, validate=True)
    except Exception:
        raise HTTPException(422, "Datei-Inhalt nicht dekodierbar (Base64 erwartet)")
    if not raw:
        raise HTTPException(422, "Leere Datei")
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="cuecore_cuelist_")
    os.close(fd)
    fetcher = netfetch.build_fetcher(DB_PATH) if body.online else None
    token = _BATCH_FETCHER.set(fetcher)              # online: ein Dauerfehler-Budget für alle Zeilen
    try:
        Path(tmp).write_bytes(raw)
        rf = _edl_resolve_fn if body.online else (_edl_index_fn if INDEX_CON is not None else None)
        res = cuelist.import_list(tmp, SONGS_CON, fps=body.fps, resolve_fn=rf, label_lookup=_label_lookup,
                                  catalog_fn=_catalog_fn)
    finally:
        _BATCH_FETCHER.reset(token)
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if "error" in res:
        raise HTTPException(422, res["error"])
    SONGS_CON.commit()
    DB_META["songs"] = songsdb.count(SONGS_CON)
    cards = edl_import.merge_cards(res["cards"], merge_identical=body.merge_identical,
                                   unify_stems=body.unify_stems)
    edl_import.attach_rights(cards, _label_lookup)    # nach Liste und Zusammenfassen
    from collections import Counter
    total = sum(c.get("duration_sec", 0) or 0 for c in cards)
    out = {"events": res["events"], "cues": len(cards), "cards": cards,
           "summary": dict(Counter(c["status"] for c in cards)), "total_duration_sec": total,
           "mapping": res["mapping"], "fieldnames": res["fieldnames"], "rows": res["rows"],
           "list_warnings": res["warnings"]}
    if fetcher is not None:
        out["stats"] = _fetch_stats(fetcher)
    return out


class EdlResolveCuesIn(BaseModel):
    cues: list[dict]     # [{clip_name, artist, title, isrc?, duration_sec?}] — max. 10 je Anfrage


EDL_RESOLVE_BATCH = 10   # MusicBrainz ~1 Anfrage/s: kleine Batches, damit das Frontend Fortschritt zeigt


@app.post("/api/edl/resolve-cues")
def edl_resolve_cues(body: EdlResolveCuesIn) -> dict:
    """Schritt „Vervollständigen" im EDL-Tab: offene Cues (Interpret+Titel, kein LC) über die
    Identification Pipeline auflösen — batchweise, das Frontend zeigt „Netz: 10 von 37".
    Erfolg wird wie beim Online-Import als Eintrag (source_type='edl_import') in die
    songs.db geschrieben; Cues ohne Interpret+Titel werden NICHT geraten."""
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    if not body.cues:
        raise HTTPException(422, "Keine Cues.")
    if len(body.cues) > EDL_RESOLVE_BATCH:
        raise HTTPException(422, f"Maximal {EDL_RESOLVE_BATCH} Cues je Anfrage.")
    fetcher = netfetch.build_fetcher(DB_PATH)
    token = _BATCH_FETCHER.set(fetcher)              # ein Dauerfehler-Budget für den ganzen Batch
    try:
        out = _edl_resolve_cues(body.cues)
    finally:
        _BATCH_FETCHER.reset(token)
    SONGS_CON.commit()
    DB_META["songs"] = songsdb.count(SONGS_CON)
    # stats wie bei /api/resolve: failures > 0 ohne Treffer heißt „Netz nicht erreichbar“, nicht „kein Treffer“
    return {"entries": out, "resolved": sum(1 for e in out if e["resolved"]), "count": len(out),
            "stats": _fetch_stats(fetcher)}


def _edl_resolve_cues(cues: list[dict]) -> list[dict]:
    """Die Schleife von /api/edl/resolve-cues — läuft mit dem Batch-Fetcher aus _BATCH_FETCHER."""
    fetcher = _BATCH_FETCHER.get()
    out: list[dict] = []
    for c in cues:
        artist, title = (c.get("artist") or "").strip(), (c.get("title") or "").strip()
        entry = {"clip_name": c.get("clip_name", "") or "", "artist": artist, "title": title, "resolved": False,
                 "stats": {"calls": 0, "cache_hits": 0, "failures": 0}}
        if not (artist and title):
            entry["status"] = "missing_no_resolver"
            out.append(entry)
            continue
        before = (fetcher.calls, fetcher.cache_hits, fetcher.failures)
        resolved = _edl_resolve_fn(artist, title)
        entry["stats"] = _fetch_stats(fetcher, before)
        if resolved and not resolved.get("labelcode") and not resolved.get("ambiguous"):
            # Autoren ohne Label: Komponist/Texter/Verlag auf die Karte, Status bleibt offen.
            entry.update({k: v for k, v in resolved.items() if v not in (None, "")})
            entry["status"] = "authors_only"
            out.append(entry)
            continue
        if resolved and resolved.get("ambiguous"):
            entry.update({k: v for k, v in resolved.items() if v not in (None, "")})
            entry["status"] = "ambiguous"          # Gleichstand: nichts geschrieben, Kandidaten dabei
            out.append(entry)
            continue
        if resolved and resolved.get("index_hit") and resolved.get("labelcode"):
            entry.update({k: v for k, v in resolved.items() if v not in (None, "")})
            entry["status"] = "index_hit"          # Track-Index: Vorschlag, nichts geschrieben
            out.append(entry)
            continue
        if not resolved or not resolved.get("labelcode"):
            entry["status"] = "unresolved"
            out.append(entry)
            continue
        # Ohne duration_sec: die Dauer dieses Cues ist projektbezogen und keine Eigenschaft des Songs. Ohne
        # Autoren: die sind aus dem Netz nur Vorschlag am Eintrag, gespeichert per Editor.
        songsdb.upsert(SONGS_CON, interpret=artist, titel=title,
                       labelname=resolved.get("labelname", ""), labelcode=resolved.get("labelcode", ""),
                       isrc=resolved.get("isrc", ""), source_type="edl_import")
        entry.update({k: v for k, v in resolved.items() if v not in (None, "")})
        entry["status"], entry["resolved"] = "resolved_and_written", True
        out.append(entry)
    edl_import.attach_rights(out, _label_lookup)      # R-Code zum gefundenen LC
    return out


class EdlAuthorsIn(BaseModel):
    cues: list[dict]           # {artist, title} je Cue — Cues mit LC, aber ohne Komponist


@app.post("/api/edl/fill-authors")
def edl_fill_authors(body: EdlAuthorsIn) -> dict:
    """Komponist/Texter/Verlag für Cues nachfüllen, die schon einen Labelcode haben (Datenbank-, Index- oder
    Dateinamen-Treffer), aber keine Autoren. Netz (MusicBrainz-Werk-Relationen, Discogs-Credits), batchweise;
    ein Treffer wird in die songs.db-Zeile nachgetragen, wenn die Felder dort leer sind (PROTECT bleibt)."""
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    if len(body.cues) > EDL_RESOLVE_BATCH:
        raise HTTPException(422, f"Maximal {EDL_RESOLVE_BATCH} Cues je Anfrage.")
    fetcher = netfetch.build_fetcher(DB_PATH)
    token = _BATCH_FETCHER.set(fetcher)              # ein Dauerfehler-Budget für den ganzen Batch
    try:
        out = _edl_fill_authors(body.cues)
    finally:
        _BATCH_FETCHER.reset(token)
    if SONGS_CON is not None:
        SONGS_CON.commit()
    return {"entries": out, "found": sum(1 for e in out if e["found"]), "count": len(out),
            "stats": _fetch_stats(fetcher)}


def _edl_fill_authors(cues: list[dict]) -> list[dict]:
    """Die Schleife von /api/edl/fill-authors — läuft mit dem Batch-Fetcher aus _BATCH_FETCHER."""
    fetcher = _BATCH_FETCHER.get()
    out: list[dict] = []
    for c in cues:
        artist, title = (c.get("artist") or "").strip(), (c.get("title") or "").strip()
        entry = {"artist": artist, "title": title, "komponist": "", "texter": "", "verlag": "", "found": False, "source": "",
                 "stats": {"calls": 0, "cache_hits": 0, "failures": 0}}
        if artist and title:
            # Immer das Netz: _resolve_one würde bei einem songs.db-Treffer kurzschließen und genau die Zeile ohne
            # Autoren zurückgeben, derentwegen wir hier sind.
            before = (fetcher.calls, fetcher.cache_hits, fetcher.failures)
            res, _ = _resolve_net(artist, title)
            entry["stats"] = _fetch_stats(fetcher, before)
            entry.update({"komponist": res.composer or "", "texter": res.lyricist or "", "verlag": res.publisher or ""})
            entry["found"] = bool(res.composer or res.lyricist or res.publisher)
            if entry["found"]:
                url = getattr(res, "author_source_url", "") or ""
                entry["source"] = "genius" if url else "musicbrainz"
                entry["source_url"] = url
                entry["note"] = next((n for n in res.notes if "Autoren" in n), "")
                # Nur Vorschlag, NICHT in die songs.db — eine einzige Quelle kann ein gleichnamiges fremdes Werk
                # treffen, und die Zeile gälte danach als verifiziert.
                # Gespeichert wird, wenn ein Mensch die Zeile im Editor speichert.
                entry["suggestion"] = True
        out.append(entry)
    return out


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _temp_download(write, tmp: str, filename: str, media_type: str) -> FileResponse:
    """Export in die Temp-Datei schreiben und senden; danach wird sie gelöscht, auch wenn das Schreiben scheitert
    (sonst bleiben cuecore_*-Dateien liegen, Windows räumt %TEMP% nicht selbst auf)."""
    try:
        write(tmp)
    except Exception:
        _unlink_quiet(tmp)
        raise
    return FileResponse(tmp, filename=filename, media_type=media_type, background=BackgroundTask(_unlink_quiet, tmp))


class EdlExportIn(BaseModel):
    cards: list[dict]
    filename: str = "cue_sheet"
    fmt: str = "xlsx"   # xlsx | csv | pdf


@app.post("/api/edl/export")
def edl_export_endpoint(body: EdlExportIn):
    """Die (ggf. schon zusammengefassten) Cue-Karten als Excel/CSV/PDF zum Download."""
    import tempfile
    fmt = body.fmt.lower().strip()
    writers = {"xlsx": edl_import.export_excel, "csv": edl_import.export_csv,
               "pdf": edl_import.export_pdf}
    media = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "csv": "text/csv", "pdf": "application/pdf"}
    if fmt not in writers:
        raise HTTPException(422, "fmt muss xlsx, csv oder pdf sein")
    # Spalte Rechterückruf aus dem LC, wie er jetzt auf der Karte steht (Editor, Vervollständigen).
    edl_import.attach_rights(body.cards, _label_lookup if DB_META["ready"] else None)
    fd, tmp = tempfile.mkstemp(suffix="." + fmt, prefix="cuecore_edl_")
    os.close(fd)
    stem = body.filename[:-len("." + fmt)] if body.filename.lower().endswith("." + fmt) else body.filename
    return _temp_download(lambda p: writers[fmt](body.cards, p), tmp, f"{stem}.{fmt}", media[fmt])


class EdlLibraryScanIn(BaseModel):
    cues: list[dict]                 # [{fileName|clip_name, duration_sec}] — meist die Cues OHNE LC
    scan_libraries: str = "all"
    framerate: str = "25"


def _validate_library_lc(entry: dict, source: str) -> tuple[str, str, str]:
    """Library-`labelcode_raw` (editingtools, Track-Metadata-API) gegen den GVL-Master prüfen
    (Domain-Regel: Anbieter-Codes sind nicht GVL-verifiziert, editingtools liefert teils
    interne Label-IDs). Reihenfolge: (1) roher Code IM Master -> nehmen; (2) sonst Labelname
    -> GVL-Code mappen; (3) sonst kein LC. Rückgabe (labelcode, gvl_label, note)."""
    raw = normalize_labelcode(entry.get("labelcode_raw", "") or "")
    if raw and raw in INDEX.code2name:
        return raw, INDEX.code2name[raw], ""
    code = match_label(entry.get("labelname", ""), INDEX) if entry.get("labelname") else None
    if code:
        return code, INDEX.code2name.get(code, entry.get("labelname", "")), "LC über Labelname gemappt"
    return "", entry.get("labelname", "") or "", (f"{source}-LC nicht im GVL-Master" if raw else "")


@app.post("/api/edl/library-scan")
def edl_library_scan(body: EdlLibraryScanIn) -> dict:
    """Production-Library-Metadaten für Cues holen, die unsere kommerziellen Quellen NICHT
    lösen, via editingtools.io `/cuesheets`-Scan. Nicht-destruktiv: liefert
    Metadaten je Dateiname, das Mergen in die Karten macht das Frontend. Jeder LC wird gegen
    den GVL-Master gegengeprüft (editingtools-`labelCode` ist teils nur eine interne ID)."""
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    key = settings.get("editingtools_api_key")
    if not key:
        raise HTTPException(400, "Kein editingtools-API-Key hinterlegt — in den Einstellungen eintragen.")
    files = [{"fileName": edl_import.clip_basename(c.get("fileName") or c.get("clip_name") or ""),
              "duration_sec": c.get("duration_sec") or 0} for c in body.cues]
    files = [f for f in files if f["fileName"]]
    if not files:
        raise HTTPException(422, "Keine Cue-Dateinamen für den Library-Scan.")
    res = editingtools.scan_files(files, http=editingtools.build_http(key),
                                  scan_libraries=body.scan_libraries, framerate=body.framerate)
    if not res["ok"]:
        return {"ok": False, "error": res.get("error", "scan_failed"), "status": res.get("status"),
                "scan_complete": res.get("scan_complete", False), "entries": [], "matched": 0}
    matched = 0
    for e in res["entries"]:
        e["labelcode"], e["labelname"], e["lc_note"] = _validate_library_lc(e, "editingtools")
        # 'matched' = editingtools hat tatsächliche Library-Metadaten geliefert (aus normalize_entry;
        # NICHT der auf den Dateinamen defaultende trackTitle).
        if e.get("matched"):
            matched += 1
    edl_import.attach_rights(res["entries"], _label_lookup)      # R-Code zum geprüften LC
    return {"ok": True, "scan_complete": res.get("scan_complete", False),
            "polls": res.get("polls", 0), "entries": res["entries"], "matched": matched,
            "count": len(res["entries"])}


_TRACKMETA_CLIENTS: dict[tuple, trackmeta.Client] = {}


def _trackmeta_clients() -> list[tuple[dict, trackmeta.Client]]:
    """Ein Client je konfiguriertem Partner (Reihenfolge = Settings-Liste). Pro Tripel (Host,
    Client-ID, Secret) EIN Client, damit das Token über Anfragen hinweg wiederverwendet wird
    und ein rotiertes Secret sofort einen frischen Client bekommt. Leer = keiner
    konfiguriert."""
    out: list[tuple[dict, trackmeta.Client]] = []
    live: set[tuple] = set()
    for p in settings.partners():
        key = (p["host"], p["client_id"], p["client_secret"])
        if not all(key):
            continue
        if key not in _TRACKMETA_CLIENTS:
            _TRACKMETA_CLIENTS[key] = trackmeta.Client(*key)
        live.add(key)
        out.append((p, _TRACKMETA_CLIENTS[key]))
    for key in [k for k in _TRACKMETA_CLIENTS if k not in live]:   # entfernte/geänderte Partner
        _TRACKMETA_CLIENTS.pop(key, None)
    return out


def _trackmeta_client(partner_id: str = "") -> trackmeta.Client | None:
    """Ein einzelner Client (Verbindungstest): der Partner mit dieser id, sonst der erste."""
    clients = _trackmeta_clients()
    for p, c in clients:
        if p["id"] == partner_id:
            return c
    return clients[0][1] if clients and not partner_id else None


@app.post("/api/edl/trackmeta-scan")
def edl_trackmeta_scan(body: EdlLibraryScanIn) -> dict:
    """Cues über die Track-Metadata-API des Library-Partners auflösen (Dateiname -> Titel,
    Interpret, Komponist, Texter, Label, LC, Album, ISRC). Gleiche Antwortform wie
    /api/edl/library-scan, damit das Frontend beide Quellen identisch mergt. Jeder LC wird
    gegen den GVL-Master gegengeprüft (Anbieter-LC ist nicht GVL-verifiziert). `null` vom
    Anbieter = nicht auflösbar, kein Fehler (matched=False)."""
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen")
    clients = _trackmeta_clients()
    if not clients:
        raise HTTPException(400, "Track-Metadata-API nicht konfiguriert — Partner (Host, Client-ID, Secret) in den Einstellungen eintragen.")
    names = [edl_import.clip_basename(c.get("fileName") or c.get("clip_name") or "") for c in body.cues]
    names = [n for n in names if n]
    if not names:
        raise HTTPException(422, "Keine Cue-Dateinamen für die Track-Metadata-Abfrage.")
    # Partner der Reihe nach: jeder bekommt nur die Namen, die noch keiner aufgelöst hat.
    resolved: dict[str, dict] = {}
    reports: list[dict] = []
    requests_total, any_ok, first_error, first_status = 0, False, "", None
    for partner, client in clients:
        todo = [n for n in names if n.lower() not in resolved]
        if not todo:
            break
        res = trackmeta.lookup(todo, client=client)
        requests_total += res.get("requests", 0)
        hit = 0
        if res["ok"]:
            any_ok = True
            for e in res["entries"]:
                if not e.get("matched"):
                    continue
                e["labelcode"], e["labelname"], e["lc_note"] = _validate_library_lc(e, partner["name"])
                e["partner"] = partner["name"]
                resolved[(e.get("query_filename") or e.get("fileName") or "").lower()] = e
                hit += 1
        elif not first_error:
            first_error, first_status = res.get("error", "") or "unknown", res.get("status")
        reports.append({"partner": partner["name"], "ok": res["ok"], "error": res.get("error", ""),
                        "status": res.get("status"), "asked": len(todo), "matched": hit})
    entries = [resolved.get(n.lower()) or {"fileName": n, "query_filename": n, "matched": False,
                                            "labelcode": "", "labelname": "", "lc_note": "", "source": "trackmeta"}
               for n in names]
    edl_import.attach_rights(entries, _label_lookup)             # R-Code zum geprüften LC
    return {"ok": any_ok, "error": "" if any_ok else first_error, "status": None if any_ok else first_status,
            "entries": entries, "matched": len(resolved), "count": len(entries),
            "requests": requests_total, "partners": reports}


def _index_row(r: dict) -> dict:
    """Index-Zeile für den Datenbank-Tab: Label in GVL-Schreibweise, Status unverifiziert."""
    return {"id": None, "interpret": r["interpret"], "titel": r["titel"], "labelcode": "LC" + r["labelcode"],
            "labelname": INDEX.code2name.get(r["labelcode"]) or r["label"], "komponist": r["komponist"],
            "texter": r["texter"], "album": r["album"], "jahr": r["jahr"], "sources": r["sources"],
            "verified": False}


@app.get("/api/db/songs")
def db_songs(q: str = "", limit: int = 100, offset: int = 0, missing: str = "", scope: str = "all") -> dict:
    """Eine Track-Datenbank für den Nutzer: verifizierte Zeilen aus der
    songs.db plus unverifizierte aus dem Track-Index, jede Zeile trägt `verified`. `scope` =
    all|verified|index. Index-Zeilen kommen nur mit Suchtext (17 Mio. Zeilen blättert niemand) und
    brauchen einen Interpreten im Text (Suche läuft über die Interpreten-Tabelle, nicht über alle Titel).
    `missing` = Lücken-Filter (labelcode|komponist|texter|verlag|isrc), wirkt auf verifizierte Zeilen."""
    if SONGS_CON is None:
        raise HTTPException(503, "songs.db nicht geladen")
    if scope == "index":
        out = {"rows": [], "total": 0, "limit": limit, "offset": 0, "total_all": 0, "missing_counts": {}}
    else:
        out = songsdb.list_songs(SONGS_CON, query=q, limit=limit, offset=offset, missing=missing)
        for r in out["rows"]:
            r["verified"] = True
    out.update({"scope": scope, "index_rows": [], "index_total": 0, "index_capped": False,
                "index_needs_artist": False, "index_present": INDEX_CON is not None})
    if scope != "verified" and INDEX_CON is not None and q.strip():
        found = trackindex.search(INDEX_CON, q, limit=limit)
        verified_keys = {songsdb._nkey(r["interpret"], r["titel"]) for r in out["rows"]}
        out["index_rows"] = [_index_row(r) for r in found["rows"]
                             if trackindex.nkey(r["interpret"], r["titel"]) not in verified_keys]
        out["index_total"] = found["total"]
        out["index_capped"] = found["capped"]
        out["index_needs_artist"] = found["needs_artist"]
    return out


class VerifyIn(BaseModel):
    interpret: str
    titel: str
    labelcode: str = ""
    labelname: str = ""
    komponist: str = ""
    texter: str = ""
    album: str = ""
    verlag: str = ""
    track_number: str = ""
    isrc: str = ""
    iswc: str = ""
    gema_werknummer: str = ""


@app.post("/api/db/verify")
def db_verify(body: VerifyIn) -> dict:
    """Haken „Verifiziert" an einer Index-Zeile: der Track wandert in die songs.db (quelle=index,
    date_verified=jetzt). Existiert er schon, werden nur leere Felder gefüllt und das Datum gesetzt."""
    if SONGS_CON is None:
        raise HTTPException(503, "songs.db nicht geladen")
    if not body.interpret.strip() or not body.titel.strip():
        raise HTTPException(400, "Interpret und Titel dürfen nicht leer sein")
    raw = body.labelcode.strip()
    digits = (strict_labelcode(raw) or "") if raw else ""      # '121' == '00121'; 'LC 24-584' nicht lesbar
    if raw and digits not in INDEX.code2name:
        raise HTTPException(400, f"Labelcode {raw} steht nicht im GVL-Master")
    songsdb.upsert(SONGS_CON, interpret=body.interpret.strip(), titel=body.titel.strip(),
                   komponist=body.komponist, texter=body.texter, labelname=INDEX.code2name.get(digits) or body.labelname,
                   labelcode=("LC" + digits) if digits else "", album=body.album, verlag=body.verlag,
                   track_number=body.track_number, isrc=body.isrc, iswc=body.iswc, gema_werknummer=body.gema_werknummer,
                   quelle="index", review_status="verified",
                   date_verified=datetime.now(timezone.utc).isoformat())
    SONGS_CON.commit()
    row = songsdb.find_row(SONGS_CON, body.interpret, body.titel)
    quelle = SONGS_CON.execute("SELECT quelle FROM songs WHERE id=?", (row["id"],)).fetchone()[0]
    DB_META["songs"] = songsdb.count(SONGS_CON)
    return {"ok": True, "row": {**row, "quelle": quelle, "verified": True}}


class IngestIn(BaseModel):
    filename: str
    content_b64: str
    commit: bool = False
    # Vom Nutzer korrigiertes Spalten-Mapping {unser Feld: Spaltenname}. Leer = automatische Erkennung.
    mapping: dict[str, str] | None = None


@app.post("/api/ingest/upload")
def ingest_upload(body: IngestIn) -> dict:
    """Library-Dump (CSV/XLSX/XLS) einspeisen. commit=False (Default) = Trockenlauf
    mit Mapping-Vorschau (schreibt nichts); commit=True = per upsert in songs.db mergen
    (verifizierte Felder bleiben geschützt). Datei kommt Base64-kodiert (kein Multipart)."""
    import base64
    import tempfile
    if SONGS_CON is None:
        raise HTTPException(503, "songs.db nicht geladen")
    suffix = Path(body.filename).suffix.lower() or ".csv"
    if suffix not in ingest.SUPPORTED_EXTENSIONS:
        raise HTTPException(422, f"Format nicht unterstützt: {suffix} "
                                 f"(erlaubt: {', '.join(ingest.SUPPORTED_EXTENSIONS)})")
    try:
        raw = base64.b64decode(body.content_b64, validate=True)
    except Exception:
        raise HTTPException(422, "Datei-Inhalt nicht dekodierbar (Base64 erwartet)")
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="cuecore_ingest_")
    os.close(fd)
    try:
        Path(tmp).write_bytes(raw)
        # Track-Lieferung einer Library (Metadaten-Export): eigener Weg — verifiziert, mit Dateinamen-/Edit-ID-Tabelle
        # für die Cue-Zuordnung, statt Spalten-Mapping (Metadaten, die eine Library selbst liefert, gelten als verifiziert).
        if ingest.library_delivery_kind(tmp):
            res = (ingest.import_library_delivery(SONGS_CON, tmp, GVL_MASTER, date_verified=date.today().isoformat())
                   if body.commit else ingest.preview_library_delivery(tmp, GVL_MASTER))
            if "error" in res:
                raise HTTPException(422, res["error"])
            if body.commit:
                DB_META["songs"] = songsdb.count(SONGS_CON)
                res["manual_labels"] = _add_delivery_labels(res["unknown_codes"], library=res["library"],
                                                            source=Path(body.filename).name)
                if res["manual_labels"]:
                    _load()                    # Index neu: Suche, Tagger und Cue-Sheet kennen die Codes sofort
            return {"delivery": True, **res}
        mp = {f: c for f, c in (body.mapping or {}).items() if f in ingest.FIELDS and c} or None
        # Fremde Listen gegen den GVL-Master: unpassende Codes werden zur Prüfung markiert, nicht verifiziert
        res = (ingest.import_file(SONGS_CON, tmp, quelle=Path(body.filename).stem, mapping=mp, master=GVL_MASTER)
               if body.commit else ingest.preview_file(tmp, master=GVL_MASTER))
        if "error" in res:
            raise HTTPException(422, res["error"])
        if body.commit:
            DB_META["songs"] = songsdb.count(SONGS_CON)
        return res
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


class DeliveryScanIn(BaseModel):
    folder: str
    commit: bool = False


@app.post("/api/ingest/scan-folder")
def ingest_scan_folder(body: DeliveryScanIn) -> dict:
    """Ordner nach Library-Lieferungen durchsuchen (manche Libraries legen bei jedem Download eine music_metadata.csv dazu)
    und sie als verifizierte Tracks einpflegen. commit=False: nur Vorschau."""
    if SONGS_CON is None:
        raise HTTPException(503, "songs.db nicht geladen")
    folder = body.folder.strip()
    if not folder or not Path(folder).is_dir():
        raise HTTPException(400, f"Kein Ordner: {folder or '—'}")
    if body.commit:
        res = ingest.import_delivery_folder(SONGS_CON, folder, GVL_MASTER, date_verified=date.today().isoformat())
        DB_META["songs"] = songsdb.count(SONGS_CON)
        res["manual_labels"] = []
        for it in res["items"]:
            if it.get("unknown_codes"):
                res["manual_labels"] += _add_delivery_labels(it["unknown_codes"], library=it["library"],
                                                             source=str(Path(it["file"]).relative_to(folder)))
        if res["manual_labels"]:
            _load()
        return res
    return ingest.preview_delivery_folder(folder, GVL_MASTER)


class GvlPreviewIn(BaseModel):
    filename: str
    content_b64: str


@app.post("/api/gvl/preview")
def gvl_preview(body: GvlPreviewIn) -> dict:
    """Ein neues GVL-PDF gegen den aktuellen Master diffen (Trockenlauf, schreibt
    nichts). Zeigt NEU/GEÄNDERT/ENTFERNT/DUPLIKATE + Stichproben. PDF kommt Base64."""
    import base64
    import tempfile
    if Path(body.filename).suffix.lower() != ".pdf":
        raise HTTPException(422, "Bitte ein PDF hochladen (.pdf)")
    try:
        raw = base64.b64decode(body.content_b64, validate=True)
    except Exception:
        raise HTTPException(422, "Datei-Inhalt nicht dekodierbar (Base64 erwartet)")
    fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="cuecore_gvl_")
    os.close(fd)
    try:
        Path(tmp).write_bytes(raw)
        try:
            return gvlsync.preview(tmp, _gvl_paths()[0])
        except Exception as e:
            raise HTTPException(422, f"PDF nicht lesbar: {e}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


class GvlMergeIn(BaseModel):
    token: str
    allow_older: bool = False      # PDF älter als der Master: nur nach ausdrücklicher Rückfrage


@app.post("/api/gvl/merge")
def gvl_merge(body: GvlMergeIn) -> dict:
    """Den zuvor gezeigten Diff festschreiben: Master/Report/Diff/Meta schreiben
    (Vorgänger wird archiviert) + labels.db neu bauen + In-Memory-Index neu laden.
    Ein älteres PDF als der Master -> 409, außer allow_older. Zusatz-Codes kommen von
    derselben Stelle wie beim Laden, sonst nennt die Antwort weniger Zeilen als danach die Rail."""
    csv_path, data_dir, docs_dir = _gvl_paths()
    res = gvlsync.merge(body.token, csv_path=csv_path, data_dir=data_dir, docs_dir=docs_dir,
                        labels_db=DB_PATH, allow_older=body.allow_older,
                        supplemental_path=_data_file("labelcodes_supplemental.csv"))
    if "error" in res:
        raise HTTPException(409, res["error"])
    _load()   # frisch gebaute labels.db in den Matching-Index laden
    res["rows_loaded"] = DB_META["rows"]
    res["source_version_loaded"] = DB_META["source_version"]
    return res


@app.get("/api/db/export")
def db_export(fmt: str = "csv", q: str = "", missing: str = ""):
    """Die (optional gefilterte) Song-DB als Excel/CSV/PDF herunterladen."""
    import tempfile
    if SONGS_CON is None:
        raise HTTPException(503, "songs.db nicht geladen")
    fmt = fmt.lower().strip()
    writers = {"csv": dbexport.export_csv, "xlsx": dbexport.export_xlsx, "pdf": dbexport.export_pdf}
    media = {"csv": "text/csv",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "pdf": "application/pdf"}
    if fmt not in writers:
        raise HTTPException(422, "fmt muss csv, xlsx oder pdf sein")
    rows = songsdb.all_songs(SONGS_CON, query=q, missing=missing)
    fd, tmp = tempfile.mkstemp(suffix="." + fmt, prefix="cuecore_db_")
    os.close(fd)
    return _temp_download(lambda p: writers[fmt](rows, p), tmp, f"cuecore_songs.{fmt}", media[fmt])


class DbSongIn(BaseModel):
    fields: dict


def _db_label_fields(fields: dict, current: dict | None = None) -> dict:
    """Labelcode aus DB-Editor, „Neuer Eintrag“, Track-Suche und Cue-Editor (alle über /api/db/song) wie
    /api/db/verify behandeln: gespeichert als 'LC' + Ziffern, der Code muss im
    GVL-Master (oder als eigenes Label) stehen, der Labelname kommt in GVL-Schreibweise — ein Name der
    Eingabe bleibt nur, wenn er zu den Namen dieses Codes gehört (1:n). Ein fremder oder unlesbarer Code
    wird abgelehnt (400) statt still als verifiziert gespeichert. Beim Bearbeiten zählt nur ein GEÄNDERTER
    Code: Alt-Zeilen bleiben editierbar, ihre Bereinigung ist ein eigener Schritt."""
    from core.labelmatch import norm
    if "labelcode" not in fields:
        return dict(fields)
    out = dict(fields)
    raw = str(fields.get("labelcode") or "").strip()
    if not raw:
        out["labelcode"] = ""
        return out
    code = strict_labelcode(raw) or ""           # None (nicht lesbar) -> 400 unten
    if current is not None and code and code == normalize_labelcode(current.get("labelcode") or "") \
            and code not in INDEX.code2name:
        out.pop("labelcode")                         # unveränderter Alt-Code: so lassen, wie er steht
        return out
    if not DB_META["ready"]:
        raise HTTPException(503, "labels.db nicht geladen — Labelcode kann nicht geprüft werden")
    if not code:
        raise HTTPException(400, f"Labelcode „{raw}“ nicht lesbar — erwartet z.B. LC00121")
    if code not in INDEX.code2name:
        raise HTTPException(400, f"LC{code} steht nicht im GVL-Master — nicht gespeichert. "
                                 "Ein eigenes Label zuerst über „+ Label anlegen“ eintragen.")
    con = connect(DB_PATH)
    try:
        names = [r["label"] for r in lookup_by_code(con, code)]
    finally:
        con.close()
    given = norm(str(fields.get("labelname") or ""))
    out["labelname"] = next((n for n in names if norm(n) == given), None) or (names[0] if names else INDEX.code2name[code])
    out["labelcode"] = "LC" + code
    return out


@app.post("/api/db/song")
def db_song_create(body: DbSongIn) -> dict:
    """Neuen Song komplett selbst anlegen (DB-Editor „Neuer Eintrag"). Interpret +
    Titel Pflicht, Rest optional. quelle='manuell'. Antwort trägt die gespeicherten
    `fields` (Labelcode normalisiert, Labelname aus dem Master)."""
    if SONGS_CON is None:
        raise HTTPException(503, "songs.db nicht geladen")
    fields = _db_label_fields(body.fields)
    res = songsdb.create_song(SONGS_CON, fields)
    if "error" in res:
        raise HTTPException(409, res["error"])
    DB_META["songs"] = songsdb.count(SONGS_CON)
    return {**res, "fields": fields}


class DbSongUpdateIn(BaseModel):
    fields: dict


@app.post("/api/db/song/{song_id}")
def db_song_update(song_id: int, body: DbSongUpdateIn) -> dict:
    """Eine Song-Zeile bearbeiten (z.B. manuell recherchierten Komponisten nachtragen).
    Überschreibt bewusst auch sonst geschützte Felder — der Nutzer korrigiert absichtlich."""
    if SONGS_CON is None:
        raise HTTPException(503, "songs.db nicht geladen")
    fields = _db_label_fields(body.fields, songsdb.get_song(SONGS_CON, song_id) or {})
    res = songsdb.update_song(SONGS_CON, song_id, fields)
    if "error" in res:
        raise HTTPException(409, res["error"])
    DB_META["songs"] = songsdb.count(SONGS_CON)
    return {**res, "fields": fields}


@app.delete("/api/songs/{song_id}")
def db_song_delete(song_id: int) -> dict:
    """Eine Song-Zeile löschen (Track-Datenbank, Knopf „Löschen“ mit Rückfrage).
    Falsch angelegte Einträge ließen sich sonst nur überschreiben und blieben „verifiziert“.
    Die library_files-Zeilen des Songs gehen mit."""
    if SONGS_CON is None:
        raise HTTPException(503, "songs.db nicht geladen")
    if not songsdb.delete_song(SONGS_CON, song_id):
        raise HTTPException(404, f"Song {song_id} nicht gefunden")
    DB_META["songs"] = songsdb.count(SONGS_CON)
    return {"deleted": True, "id": song_id}


class SettingsIn(BaseModel):
    updates: dict
    partners: list[dict] | None = None    # Track-Metadata-Partnerliste (None = unverändert lassen)


def _configured_sources() -> dict:
    """Welche externen Quellen sind eingerichtet? Für den Rail-Status und den Schritt
    „Metadaten vervollständigen" im EDL-Tab (nur konfigurierte Quellen werden gefragt)."""
    g = settings.get
    return {
        "editingtools": bool(g("editingtools_api_key")),
        "trackmeta": any(p["complete"] for p in settings.partners_status()),
        "discogs": bool(g("discogs_token") or (g("discogs_consumer_key") and g("discogs_consumer_secret"))),
        "genius": bool(g("genius_token")),
        "acoustid": bool(settings.effective("acoustid_api_key")),
        "audd": bool(g("audd_token")),
        "acrcloud": bool(g("acrcloud_access_key") and g("acrcloud_access_secret")),
    }


def _settings_view() -> dict:
    # fpcalc-Pfad mit ausliefern: ohne das Binary tut AcoustID nichts, und der Nutzer soll den Grund
    # sehen statt zu raten, warum nichts gefunden wird (im Mac-Bundle liegt es dabei, unter Windows
    # noch nicht).
    return {"keys": settings.status(), "paths": settings.paths_status(), "texts": settings.texts_status(),
            "partners": settings.partners_status(), "partner_fields": settings.PARTNER_FIELDS,
            "groups": settings.GROUPS, "configured": _configured_sources(),
            "fpcalc": acoustid.fpcalc_path() or ""}


@app.post("/api/settings/test/{service}")
def settings_test(service: str, partner: str = "") -> dict:
    """„Verbindung prüfen" auf der Einstellungs-Seite — ein billiger Aufruf je Dienst
    (core/conncheck.py). Nur für Dienste mit dokumentiertem Prüfweg. `partner` = id aus der
    Track-Metadata-Partnerliste (ohne: der erste)."""
    g = settings.get
    if service == "trackmeta":
        _TRACKMETA_CLIENTS.clear()                      # frischer Client: geänderte Settings sofort wirksam
        res = conncheck.check_trackmeta(_trackmeta_client(partner))
    elif service == "editingtools":
        res = conncheck.check_editingtools(g("editingtools_api_key"))
    elif service == "discogs":
        res = conncheck.check_discogs(g("discogs_token"), g("discogs_consumer_key"), g("discogs_consumer_secret"))
    elif service == "genius":
        res = conncheck.check_genius(g("genius_token"))
    elif service == "acoustid":
        res = conncheck.check_acoustid(settings.effective("acoustid_api_key"))
    else:
        raise HTTPException(404, f"Kein Verbindungstest für '{service}'.")
    return {"service": service, **res}


@app.get("/api/settings")
def get_settings() -> dict:
    return _settings_view()


@app.post("/api/settings")
def post_settings(body: SettingsIn) -> dict:
    settings.set_keys({k: v for k, v in body.updates.items() if k in settings.KNOWN_KEYS})
    settings.set_paths({k: v for k, v in body.updates.items() if k in settings.KNOWN_PATHS})
    settings.set_texts({k: v for k, v in body.updates.items() if k in settings.KNOWN_TEXTS})
    if body.partners is not None:
        settings.set_partners(body.partners)            # komplette Liste; leeres Secret behält das gespeicherte
    return _settings_view()


class OpenUrlIn(BaseModel):
    url: str


@app.post("/api/open-url")
def open_url(body: OpenUrlIn) -> dict:
    """Eine Quell-URL (MusicBrainz/Discogs/musik-sammler) im Standardbrowser öffnen.
    Robuster als ein reines target=_blank — funktioniert auch im fensterlosen Build."""
    url = body.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(422, "Nur http/https-URLs erlaubt")
    import webbrowser
    return {"opened": bool(webbrowser.open(url)), "url": url}


@app.post("/api/pick-folder")
def pick_folder() -> dict:
    """Nativen OS-Ordnerdialog öffnen und den gewählten Pfad zurückgeben. Eine
    Browser-App bekommt aus dem Datei-Dialog keinen echten Pfad (Sandbox) — daher
    übers Backend. Abbruch -> path=''."""
    return {"path": folderpick.choose_folder()}


# Injizierbar, damit Tests /api/quit prüfen können, ohne den Prozess zu killen.
_QUIT = {"fn": lambda: os._exit(0)}


@app.post("/api/quit")
def quit_app() -> dict:
    """Server sauber beenden. Im fensterlosen (windowed) Build gibt es sonst keinen
    Weg zu beenden. Die Antwort geht noch raus, dann killt ein Timer den Prozess."""
    import threading
    threading.Timer(0.3, _QUIT["fn"]).start()
    return {"quitting": True}
