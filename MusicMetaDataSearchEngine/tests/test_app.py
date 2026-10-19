from pathlib import Path

from fastapi.testclient import TestClient

import app as appmod
from app import app
from core import tags
from core.labeldb import build_label_db
from core.models import LabelRecord
from core.musicbrainz import MBLookup
from core.normalize import normalize_labelcode

client = TestClient(app)

SAMPLE = [
    LabelRecord("00121", "Atlantic", "", "", "Warner Music", "R6;R9"),
    LabelRecord("0309", "Foo Bar", "", "ja", "Some GmbH", "R6"),
    LabelRecord("10192", "DOMINO British", "", "", "Domino Recording Co", ""),
]


def _setup(tmp_path):
    db = tmp_path / "labels.db"
    build_label_db(SAMPLE, db, "2026-06-23")
    appmod.configure(db)
    return db


def test_health_shape():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["product"] == "CueCore"


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "CUE" in r.text and "POSTBRO" in r.text


def test_web_assets_serve_logo_pngs_only():
    for name in ("cuecore-logo-dark.png", "cuecore-logo-light.png", "cuecore-mark-dark.png", "cuecore-mark-light.png"):
        r = client.get(f"/web/{name}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png", name
    assert client.get("/web/index.html").status_code == 404          # nur Bilder
    assert client.get("/web/..%2Fapp.py").status_code == 404
    assert client.get("/web/nope.png").status_code == 404


def test_settings_get_lists_keys():
    r = client.get("/api/settings")
    assert r.status_code == 200
    keys = {k["key"] for k in r.json()["keys"]}
    assert {"audd_token", "discogs_consumer_key", "discogs_consumer_secret"} <= keys


def test_load_heals_missing_supplemental_codes(tmp_path, monkeypatch):
    """Selbstheilung beim Start: eine labels.db ohne die Zusatz-Codes (PDF-Update einer älteren Version,
    alte Installation) bekommt sie beim Laden nachgetragen. GVL-Master und manuelle Zeilen
    gewinnen, ein zweiter Start trägt nichts doppelt ein, die Suche findet den Code sofort."""
    import json as _json
    import sqlite3
    supp = tmp_path / "labelcodes_supplemental.csv"
    supp.write_text("labelcode,label,source\n99001,Zusatz Library,KPM-PDF\n00121,Atlantic Doppelt,x\n"
                    "99002,Zusatz statt manuell,x\n", encoding="utf-8")
    orig = appmod._data_file
    monkeypatch.setattr(appmod, "_data_file", lambda n: supp if n == "labelcodes_supplemental.csv" else orig(n))
    (tmp_path / "labels_manual.json").write_text(_json.dumps([{"labelcode": "99002", "label": "Handangelegt"}]),
                                                 encoding="utf-8")
    db = _setup(tmp_path)
    assert appmod.DB_META["rows"] == len(SAMPLE) + 2
    con = sqlite3.connect(db)
    rows = con.execute("SELECT labelcode,label,hersteller,source FROM labels WHERE labelcode IN ('99001','00121','99002') "
                       "ORDER BY labelcode").fetchall()
    con.close()
    assert rows == [("00121", "Atlantic", "Warner Music", "gvl"),
                    ("99001", "Zusatz Library", "Zusatzquelle: KPM-PDF", "gvl"),
                    ("99002", "Handangelegt", "", "manuell")]
    assert client.get("/api/search", params={"q": "99001"}).json()["results"][0]["label"] == "Zusatz Library"
    appmod.configure(db)                                            # zweiter Start
    assert appmod.DB_META["rows"] == len(SAMPLE) + 2
    assert "99001" in appmod.INDEX.code2name                        # Resolver sieht den Code


def test_search_fts(tmp_path):
    _setup(tmp_path)
    j = client.get("/api/search", params={"q": "domino"}).json()
    assert j["mode"] == "fts"
    assert any(x["labelcode"] == "10192" for x in j["results"])


def test_search_code(tmp_path):
    _setup(tmp_path)
    j = client.get("/api/search", params={"q": "00121"}).json()
    assert j["mode"] == "code"
    assert j["results"][0]["label"] == "Atlantic"


def _mock_net(monkeypatch, label_names=("Atlantic",)):
    monkeypatch.setattr(appmod.musicbrainz, "lookup",
                        lambda a, t, **kw: MBLookup(matched=True, score=100,
                                                    label_names=list(label_names), discogs_release_ids=[]))
    monkeypatch.setattr(appmod.discogs, "lookup_label_codes", lambda rid, **kw: [])
    monkeypatch.setattr(appmod.deezer, "lookup", lambda a, t, **kw: None)
    monkeypatch.setattr(appmod.genius, "lookup", lambda a, t, **kw: None)   # kein Netz im Test
    monkeypatch.setattr(appmod.wikidata, "lookup", lambda a, t, **kw: None)  # kein Netz im Test
    monkeypatch.setattr(appmod.musicbrainz, "search_work_writers", lambda t, **kw: ("", "", ""))
    monkeypatch.setattr(appmod.itunes, "lookup", lambda a, t, **kw: None)


def test_resolve_endpoint(tmp_path, monkeypatch):
    _setup(tmp_path)
    _mock_net(monkeypatch)
    r = client.post("/api/resolve", json={"artist": "Zzz Unbekannt", "title": "Nicht In DB"})
    j = r.json()
    assert r.status_code == 200 and j["matched"]
    assert j["candidates"][0]["labelcode"] == "00121"


def test_resolve_retries_on_transient_source_failure(tmp_path, monkeypatch):
    """1. Lauf: eine Quelle flaket (fetcher.failures steigt) + kein Label -> nachfassen.
    2. Lauf: sauber, liefert das Label. Der Endpoint gibt den besseren Lauf zurück."""
    _setup(tmp_path)

    class FakeFetcher:
        def __init__(self):
            self.calls = self.cache_hits = self.failures = 0

    fake = FakeFetcher()
    monkeypatch.setattr(appmod.netfetch, "build_fetcher", lambda db: fake)
    state = {"pass": 0}

    def fake_mb(a, t, *, fetch=None, sleep=None):
        state["pass"] += 1
        if state["pass"] == 1:
            fetch.failures += 1                                    # transienter Fehler in Runde 1
            return MBLookup(matched=True, score=100, label_names=[], discogs_release_ids=[])
        return MBLookup(matched=True, score=100, label_names=["Atlantic"], discogs_release_ids=[])

    monkeypatch.setattr(appmod.musicbrainz, "lookup", fake_mb)
    monkeypatch.setattr(appmod.musicbrainz, "search_work_writers", lambda t, **kw: ("", "", ""))
    monkeypatch.setattr(appmod.discogs, "lookup_label_codes", lambda rid, **kw: [])
    monkeypatch.setattr(appmod.deezer, "lookup", lambda a, t, **kw: None)
    monkeypatch.setattr(appmod.genius, "lookup", lambda a, t, **kw: None)
    monkeypatch.setattr(appmod.wikidata, "lookup", lambda a, t, **kw: None)
    monkeypatch.setattr(appmod.itunes, "lookup", lambda a, t, **kw: None)

    j = client.post("/api/resolve", json={"artist": "Zzz Retry", "title": "Flaky"}).json()
    assert state["pass"] == 2                                       # genau einmal nachgefasst
    assert j["candidates"][0]["labelcode"] == "00121"              # 2. Lauf lieferte das Label
    assert j["incomplete"] is False                                # nach erfolgreichem Retry sauber


def _dead_net(monkeypatch):
    """Netz komplett weg: jeder Abruf ist ein Timeout und kostet 15 s simulierte Zeit. Fetcher ohne
    Cache und Limiter, mit simulierter Uhr; iTunes darf ebenfalls nicht ins echte Netz."""
    clock = {"now": 0.0, "calls": 0, "built": 0}

    def http(url, ua, headers=None):
        clock["now"] += 15.0
        clock["calls"] += 1
        return None, False

    def build(db):
        clock["built"] += 1
        return appmod.netfetch.Fetcher(http=http, clock=lambda: clock["now"])
    monkeypatch.setattr(appmod.netfetch, "build_fetcher", build)
    monkeypatch.setattr(appmod.itunes.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("kein Netz im Test")))
    return clock


def test_resolve_gives_up_on_dead_network_and_reports_failures(tmp_path, monkeypatch):
    """MusicBrainz 503/Timeout in Serie: nach spätestens
    etwa 60 s ist Schluss, stats.failures > 0, damit die Oberfläche „Netz nicht erreichbar“ statt „kein Match“ zeigt."""
    _setup(tmp_path)
    clock = _dead_net(monkeypatch)
    j = client.post("/api/resolve", json={"artist": "Zzq Offline", "title": "Nix Da"}).json()
    assert j["matched"] is False and j["best"] is None
    assert j["stats"]["failures"] > 0
    assert clock["now"] <= 60.0, f"{clock['now']} s simuliert, {clock['calls']} Abrufe"


def test_resolve_cues_share_one_budget_and_report_stats(tmp_path, monkeypatch):
    """Im Cue-Sheet („Metadaten vervollständigen“): ein Fetcher je Anfrage, das Budget gilt für den Batch statt
    je Cue neu (10 Cues × 45 s wären 7,5 min). stats je Eintrag und gesamt."""
    _setup(tmp_path)
    clock = _dead_net(monkeypatch)
    cues = [{"clip_name": f"c{i}.wav", "artist": f"Zzq Offline {i}", "title": "Nix Da"} for i in range(5)]
    j = client.post("/api/edl/resolve-cues", json={"cues": cues}).json()
    assert j["resolved"] == 0 and j["stats"]["failures"] > 0
    assert all("stats" in e for e in j["entries"]) and j["entries"][0]["stats"]["failures"] > 0
    assert sum(e["stats"]["failures"] for e in j["entries"]) == j["stats"]["failures"]
    assert clock["built"] == 1 and clock["now"] <= 60.0, clock
    f = client.post("/api/edl/fill-authors", json={"cues": [{"artist": c["artist"], "title": "Nix Da"} for c in cues]}).json()
    assert f["found"] == 0 and f["stats"]["failures"] > 0 and f["entries"][0]["stats"]["failures"] > 0
    assert clock["built"] == 2 and clock["now"] <= 120.0, clock


def test_edl_import_online_shares_one_budget(tmp_path, monkeypatch):
    """Beim Einlesen mit „Fehlende online suchen“: alle Clips einer EDL teilen sich ein Budget."""
    _setup(tmp_path)
    clock = _dead_net(monkeypatch)
    edl = "TITLE: TEST\n" + "".join(
        f"00{i}  AX  AA  C  00:00:0{i}:00 00:00:0{i}:05 00:00:0{i}:00 00:00:0{i}:05\n"
        f"* FROM CLIP NAME: Zzq Offline {i} - Nix Da.mp3\n" for i in range(1, 5))
    j = client.post("/api/edl/import", json={"content": edl, "online": True}).json()
    assert j["cues"] == 4 and j["stats"]["failures"] > 0
    assert clock["built"] == 1 and clock["now"] <= 60.0, clock
    assert "stats" not in client.post("/api/edl/import", json={"content": edl, "online": False}).json()


def test_resolve_title_only_returns_artist_candidates(tmp_path, monkeypatch):
    """Leerer Interpret -> Titel-only-Pfad: Interpret-Kandidaten aus Wikidata, GVL-Code
    zuerst. Kein blindes Raten."""
    _setup(tmp_path)
    from core.wikidata import WikidataItem
    items = [WikidataItem(performers=["Bon Iver"], labels=["Zzz Indie"], item_url="https://www.wikidata.org/wiki/Q1"),
             WikidataItem(performers=["Birdy"], labels=["Atlantic"], item_url="https://www.wikidata.org/wiki/Q2")]
    monkeypatch.setattr(appmod.wikidata, "lookup_items", lambda t, **kw: items)
    # „Skinny Love“ selbst steht verifiziert in der songs.db (Seed) und käme lokal — hier soll der Netzpfad laufen
    j = client.post("/api/resolve", json={"artist": "", "title": "Qzx Skinny Love"}).json()
    assert j["mode"] == "title_only" and j["title"] == "Qzx Skinny Love" and j["verified"] == [] and j["index"] == []
    cands = j["title_candidates"]
    assert cands[0]["artist"] == "Birdy" and cands[0]["labelcode"] == "00121"   # GVL-Code zuerst
    assert cands[1]["artist"] == "Bon Iver" and cands[1]["labelcode"] == ""


def _key_only(monkeypatch, key="KEY"):
    monkeypatch.setattr(appmod.settings, "get",
                        lambda k, *a, **kw: key if k == "editingtools_api_key" else "")


def test_edl_library_scan_requires_key(tmp_path, monkeypatch):
    _setup(tmp_path)
    _key_only(monkeypatch, key="")
    r = client.post("/api/edl/library-scan", json={"cues": [{"fileName": "a.wav"}]})
    assert r.status_code == 400


def test_edl_library_scan_validates_labelcode_against_gvl(tmp_path, monkeypatch):
    _setup(tmp_path)
    _key_only(monkeypatch)

    def fake_scan(files, **kw):
        return {"ok": True, "scan_complete": True, "polls": 1, "entries": [
            {"fileName": "a.wav", "matched": True, "labelcode_raw": "00121", "labelname": "Irgendwas", "titel": "T", "komponist": "C"},
            {"fileName": "b.wav", "matched": True, "labelcode_raw": "4369", "labelname": "Atlantic", "titel": "U"},   # interne ID -> via Name
            {"fileName": "c.wav", "matched": True, "labelcode_raw": "99999", "labelname": "Nixgibts", "titel": "V"},  # kein GVL
        ]}
    monkeypatch.setattr(appmod.editingtools, "scan_files", fake_scan)
    j = client.post("/api/edl/library-scan", json={"cues": [{"fileName": "a.wav"}]}).json()
    assert j["ok"] and j["matched"] == 3 and j["count"] == 3
    e = j["entries"]
    assert e[0]["labelcode"] == "00121" and e[0]["labelname"] == "Atlantic"          # roher LC im Master
    assert e[1]["labelcode"] == "00121" and "Labelname" in e[1]["lc_note"]           # via labelName gemappt
    assert e[2]["labelcode"] == "" and "nicht im GVL" in e[2]["lc_note"]             # nicht mappbar
    assert [x["rechterueckrufe"] for x in e] == ["R6;R9", "R6;R9", ""]                # R-Code zum LC


def test_edl_library_scan_surfaces_scan_error(tmp_path, monkeypatch):
    _setup(tmp_path)
    _key_only(monkeypatch)
    monkeypatch.setattr(appmod.editingtools, "scan_files",
                        lambda files, **kw: {"ok": False, "error": "quota", "status": 402, "entries": []})
    j = client.post("/api/edl/library-scan", json={"cues": [{"fileName": "a.wav"}]}).json()
    assert j["ok"] is False and j["error"] == "quota" and j["status"] == 402


def test_resolve_offers_local_suggestions_for_near_title(tmp_path, monkeypatch):
    from core import songsdb
    _setup(tmp_path)
    _mock_net(monkeypatch, label_names=())   # kein Netz-Treffer -> nur Vorschläge zählen
    songsdb.upsert(appmod.SONGS_CON, interpret="AURORA", titel="A Dangerous Thing",
                   labelname="Decca", labelcode="LC00171")
    appmod.SONGS_CON.commit()
    # Titel unvollständig -> exakter Treffer scheitert, Fuzzy-Vorschlag muss greifen
    j = client.post("/api/resolve", json={"artist": "AURORA", "title": "dangerous thing"}).json()
    titles = [s["titel"] for s in j.get("local_suggestions", [])]
    assert "A Dangerous Thing" in titles


def test_resolve_exact_local_hit_has_no_suggestions(tmp_path):
    from core import songsdb
    _setup(tmp_path)
    songsdb.upsert(appmod.SONGS_CON, interpret="AURORA", titel="Runaway",
                   labelname="Decca", labelcode="LC00171")
    appmod.SONGS_CON.commit()
    j = client.post("/api/resolve", json={"artist": "AURORA", "title": "Runaway"}).json()
    assert j["local"] is True and "local_suggestions" not in j


def test_resolve_local_hit_surfaces_album_and_isrc(tmp_path):
    from core import songsdb
    _setup(tmp_path)
    songsdb.upsert(appmod.SONGS_CON, interpret="Zzz Local", titel="Verified Song",
                   labelname="Decca", labelcode="LC00171",
                   album="Debut Album", isrc="GBUM71501189")
    appmod.SONGS_CON.commit()
    j = client.post("/api/resolve", json={"artist": "Zzz Local", "title": "Verified Song"}).json()
    assert j["local"] is True and j["album"] == "Debut Album" and j["isrcs"] == ["GBUM71501189"]


# --- Resolver-Merge (DB vs. Netz, Feld-Konflikte) ---

def test_resolve_merge_flags_conflict(tmp_path, monkeypatch):
    from core import songsdb
    _setup(tmp_path)
    _mock_net(monkeypatch, label_names=("Atlantic",))   # Netz sagt Atlantic (LC00121)
    # frischer Track (nicht im Seed) mit abweichendem Label -> echter Konflikt
    songsdb.upsert(appmod.SONGS_CON, interpret="Zzz Konflikt", titel="Zwei Labels",
                   labelname="Decca", labelcode="LC00171")   # DB sagt Decca (LC00171)
    appmod.SONGS_CON.commit()
    j = client.post("/api/resolve/merge", json={"artist": "Zzz Konflikt", "title": "Zwei Labels"}).json()
    assert j["db_id"] is not None
    f = {x["key"]: x for x in j["fields"]}
    assert f["labelcode"]["status"] == "conflict"
    assert f["labelcode"]["db"] == "LC00171" and f["labelcode"]["net"] == "00121"
    assert f["labelname"]["status"] == "conflict"


def test_resolve_merge_net_adds_when_db_field_empty(tmp_path, monkeypatch):
    from core import songsdb
    _setup(tmp_path)
    _mock_net(monkeypatch, label_names=("Atlantic",))
    # frischer Track (nicht im Seed) OHNE Label -> Netz ergänzt das Label
    songsdb.upsert(appmod.SONGS_CON, interpret="Zzz Frischling", titel="Ohne Label")
    appmod.SONGS_CON.commit()
    j = client.post("/api/resolve/merge", json={"artist": "Zzz Frischling", "title": "Ohne Label"}).json()
    assert j["db_id"] is not None
    f = {x["key"]: x for x in j["fields"]}
    assert f["labelname"]["status"] == "net_adds" and f["labelname"]["net"] == "Atlantic"


def test_db_lookup_returns_row_without_net(tmp_path):
    from core import songsdb
    _setup(tmp_path)
    songsdb.upsert(appmod.SONGS_CON, interpret="Zzz Lokal", titel="Nur Editieren",
                   labelname="Decca", labelcode="LC00171")
    appmod.SONGS_CON.commit()
    j = client.post("/api/db/lookup", json={"artist": "Zzz Lokal", "title": "Nur Editieren"}).json()
    assert j["db_id"] is not None and j["db"]["labelname"] == "Decca"
    assert j["fields"] == [] and j["net"] == {}     # kein Netz-Abgleich
    # unbekannter Track -> db None (Neuanlage-Formular)
    j2 = client.post("/api/db/lookup", json={"artist": "Zzz Neu", "title": "Gibt Es Nicht"}).json()
    assert j2["db"] is None and j2["db_id"] is None


def test_resolve_merge_new_track_has_no_db(tmp_path, monkeypatch):
    _setup(tmp_path)
    _mock_net(monkeypatch, label_names=("Atlantic",))
    j = client.post("/api/resolve/merge", json={"artist": "Zzz New", "title": "Ghost"}).json()
    assert j["db"] is None and j["db_id"] is None
    f = {x["key"]: x for x in j["fields"]}
    assert f["labelcode"]["status"] == "net_adds"


# --- EDL-Import ---

_EDL = ("TITLE: TEST\n"
        "001  AX  AA  C  00:00:00:00 00:00:05:00 00:00:00:00 00:00:05:00\n"
        "* FROM CLIP NAME: Alanis Morissette - Thank You.mp3\n")


def test_edl_import_found_in_db(tmp_path):
    from core import songsdb
    _setup(tmp_path)
    songsdb.upsert(appmod.SONGS_CON, interpret="Alanis Morissette", titel="Thank You",
                   labelname="MAVERICK", labelcode="LC01485")
    appmod.SONGS_CON.commit()

    r = client.post("/api/edl/import", json={"content": _EDL, "online": False})
    j = r.json()
    assert r.status_code == 200
    assert j["events"] == 1
    assert j["cards"][0]["status"] == "found_in_db"
    assert j["cards"][0]["labelcode"] == "LC01485"
    assert j["cards"][0]["duration_sec"] == 5 and j["total_duration_sec"] == 5
    assert j["summary"]["found_in_db"] == 1


def test_edl_import_offline_leaves_missing_unresolved(tmp_path):
    _setup(tmp_path)
    # Fantasie-Clip-Name, garantiert nicht in der geseedeten songs.db:
    edl = ("001  AX  AA  C  00:00:00:00 00:00:05:00 00:00:00:00 00:00:05:00\n"
           "* FROM CLIP NAME: Zzq Nichtexistent - Kein Echter Song 9x.mp3\n")
    r = client.post("/api/edl/import", json={"content": edl, "online": False})
    # online=False -> kein Netz, unbekannter Song bleibt 'missing_no_resolver'
    assert r.json()["cards"][0]["status"] == "missing_no_resolver"


def test_edl_import_skips_video_and_camera_audio_and_reports_them(tmp_path):
    _setup(tmp_path)
    edl = ("001  A001C003 V     C        14:22:10:00 14:22:20:00 10:00:00:00 10:00:10:00\n"
           "* FROM CLIP NAME: A001C003_220801.MXF\n"
           "002  A001C003 A     C        14:22:10:00 14:22:20:00 10:00:00:00 10:00:10:00\n"
           "* FROM CLIP NAME: A001C003_220801.MXF\n"
           "003  AX       A3    C        00:00:00:00 00:00:30:00 10:00:05:00 10:00:35:00\n"
           "* FROM CLIP NAME: Zzq Nichtexistent - Kein Echter Song 9x.wav\n")
    j = client.post("/api/edl/import", json={"content": edl, "online": False}).json()
    assert [c["clip_name"] for c in j["cards"]] == ["Zzq Nichtexistent - Kein Echter Song 9x.wav"]
    assert j["events"] == 3
    assert [(x["event_num"], x["reason"]) for x in j["ignored"]] == [("001", "Videospur"),
                                                                     ("002", "Ton aus Videodatei (O-Ton)")]
    # abschaltbar: dann wie bisher jedes Event mit Clip-Namen als Cue
    j = client.post("/api/edl/import", json={"content": edl, "online": False, "music_only": False}).json()
    assert len(j["cards"]) == 2 and j["ignored"] == []


def test_edl_import_rejects_non_edl(tmp_path):
    _setup(tmp_path)
    r = client.post("/api/edl/import", json={"content": "kein gültiger EDL-Inhalt\n"})
    assert r.status_code == 422


def test_edl_export_returns_xlsx(tmp_path):
    _setup(tmp_path)
    cards = [{"artist": "AURORA", "title": "Runaway", "labelname": "Decca",
              "labelcode": "LC00171", "duration_sec": 4}]
    r = client.post("/api/edl/export", json={"cards": cards, "filename": "cue"})
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert r.content[:2] == b"PK"   # xlsx = ZIP-Container


def test_edl_export_csv_and_pdf(tmp_path):
    _setup(tmp_path)
    cards = [{"artist": "AURORA", "title": "Runaway", "labelcode": "LC00171", "duration_sec": 4}]
    csv_r = client.post("/api/edl/export", json={"cards": cards, "filename": "cue", "fmt": "csv"})
    assert csv_r.status_code == 200 and "text/csv" in csv_r.headers["content-type"]
    pdf_r = client.post("/api/edl/export", json={"cards": cards, "filename": "cue", "fmt": "pdf"})
    assert pdf_r.status_code == 200 and pdf_r.content[:5] == b"%PDF-"
    assert client.post("/api/edl/export", json={"cards": cards, "fmt": "json"}).status_code == 422


def test_edl_import_merge_identical_sums_duration(tmp_path):
    from core import songsdb
    _setup(tmp_path)
    songsdb.upsert(appmod.SONGS_CON, interpret="AURORA", titel="Runaway",
                   labelname="Decca", labelcode="LC00171")
    appmod.SONGS_CON.commit()
    # derselbe Track zweimal an verschiedenen Stellen im Schnitt
    edl = ("001  AX  AA  C  00:00:00:00 00:00:05:00 00:00:00:00 00:00:05:00\n"
           "* FROM CLIP NAME: AURORA - Runaway.mp3\n"
           "002  AX  AA  C  00:00:10:00 00:00:13:00 00:00:20:00 00:00:23:00\n"
           "* FROM CLIP NAME: AURORA - Runaway.mp3\n")
    raw = client.post("/api/edl/import", json={"content": edl, "online": False}).json()
    assert raw["cues"] == 2   # ohne Merge: zwei Cues
    merged = client.post("/api/edl/import",
                         json={"content": edl, "online": False, "merge_identical": True}).json()
    assert merged["cues"] == 1
    assert merged["cards"][0]["duration_sec"] == 8 and merged["cards"][0]["merged_count"] == 2
    assert merged["total_duration_sec"] == 8


# --- DB-Editor (Datenbank-Tab) ---

def test_db_song_create_and_list(tmp_path):
    _setup(tmp_path)
    r = client.post("/api/db/song", json={"fields": {
        "interpret": "Zzq Neuling", "titel": "Erster Song", "labelcode": "LC00121",
        "album": "Debut", "track_number": "1/10"}})
    assert r.status_code == 200 and isinstance(r.json()["id"], int)
    j = client.get("/api/db/songs", params={"q": "Zzq Neuling"}).json()
    assert j["total"] == 1
    row = j["rows"][0]
    assert row["album"] == "Debut" and row["track_number"] == "1/10" and row["labelcode"] == "LC00121"


def test_db_songs_search_matches_words_across_fields(tmp_path):
    _setup(tmp_path)
    client.post("/api/db/song", json={"fields": {"interpret": "Zzq Wordy", "titel": "Split Fields", "labelcode": "LC00121"}})
    assert client.get("/api/db/songs", params={"q": "wordy split"}).json()["total"] == 1     # Interpret + Titel
    assert client.get("/api/db/songs", params={"q": "split 00121"}).json()["total"] == 1     # Titel + Code
    assert client.get("/api/db/songs", params={"q": "wordy nothere"}).json()["total"] == 0


def test_db_song_create_requires_artist_title(tmp_path):
    _setup(tmp_path)
    r = client.post("/api/db/song", json={"fields": {"interpret": "", "titel": "X"}})
    assert r.status_code == 409


def test_db_song_create_rejects_duplicate(tmp_path):
    _setup(tmp_path)
    body = {"fields": {"interpret": "Zzq Doppelt", "titel": "Nur Einmal"}}
    assert client.post("/api/db/song", json=body).status_code == 200
    assert client.post("/api/db/song", json=body).status_code == 409


def test_db_song_update_edits_album(tmp_path):
    _setup(tmp_path)
    sid = client.post("/api/db/song", json={"fields": {
        "interpret": "Zzq Editier", "titel": "Ändermich"}}).json()["id"]
    r = client.post(f"/api/db/song/{sid}", json={"fields": {"album": "Neues Album", "track_number": "7"}})
    assert r.status_code == 200
    row = client.get("/api/db/songs", params={"q": "Zzq Editier"}).json()["rows"][0]
    assert row["album"] == "Neues Album" and row["track_number"] == "7"


def test_db_song_delete(tmp_path):
    """DELETE /api/songs/{id} -> 200 {deleted, id}; unbekannte id -> 404 mit detail."""
    _setup(tmp_path)
    sid = client.post("/api/db/song", json={"fields": {"interpret": "Zzq Loesch", "titel": "Falsch Angelegt",
                                                       "labelcode": "LC00121"}}).json()["id"]
    before = client.get("/api/health").json()["songs"]
    r = client.delete(f"/api/songs/{sid}")
    assert r.status_code == 200 and r.json() == {"deleted": True, "id": sid}
    assert client.get("/api/db/songs", params={"q": "Zzq Loesch"}).json()["total"] == 0
    assert appmod.songsdb.find(appmod.SONGS_CON, "Zzq Loesch", "Falsch Angelegt") is None
    assert client.get("/api/health").json()["songs"] == before - 1
    r = client.delete(f"/api/songs/{sid}")
    assert r.status_code == 404 and r.json()["detail"]


# --- Musik-DB-Import: Upload preview + commit ---

def _b64_csv(text: str) -> str:
    import base64
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_ingest_upload_preview_then_commit(tmp_path):
    _setup(tmp_path)
    csv_text = "Artist,Title,Album,Track-Nr.,LC\nZzq Neuartist,Neusong,Neualbum,3,LC00121\n"
    b64 = _b64_csv(csv_text)

    prev = client.post("/api/ingest/upload",
                       json={"filename": "lib.csv", "content_b64": b64, "commit": False})
    assert prev.status_code == 200
    pj = prev.json()
    assert pj["required_ok"] and pj["rows"] == 1 and pj["mapping"]["album"] == "Album"
    # Trockenlauf schreibt nichts
    assert client.get("/api/db/songs", params={"q": "Zzq Neuartist"}).json()["total"] == 0

    com = client.post("/api/ingest/upload",
                      json={"filename": "lib.csv", "content_b64": b64, "commit": True})
    assert com.status_code == 200 and com.json()["inserted"] == 1
    row = client.get("/api/db/songs", params={"q": "Zzq Neuartist"}).json()["rows"][0]
    assert row["album"] == "Neualbum" and row["track_number"] == "3" and row["labelcode"] == "LC00121"


def test_ingest_upload_rejects_unknown_extension(tmp_path):
    _setup(tmp_path)
    r = client.post("/api/ingest/upload",
                    json={"filename": "notes.txt", "content_b64": _b64_csv("x"), "commit": False})
    assert r.status_code == 422


def test_ingest_upload_missing_required_columns(tmp_path):
    _setup(tmp_path)
    body = {"filename": "bad.csv", "content_b64": _b64_csv("ColA,ColB\n1,2\n")}
    # Preview meldet den Zustand (200, required_ok=False) — die UI kann warnen ...
    prev = client.post("/api/ingest/upload", json={**body, "commit": False})
    assert prev.status_code == 200 and prev.json()["required_ok"] is False
    # ... ein Commit ohne Pflichtspalten wird abgelehnt.
    assert client.post("/api/ingest/upload", json={**body, "commit": True}).status_code == 422


# --- DB-Export: csv / xlsx / pdf ---

def test_db_export_csv(tmp_path):
    _setup(tmp_path)
    r = client.get("/api/db/export", params={"fmt": "csv"})
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    first = r.text.splitlines()[0].lstrip("﻿")   # utf-8-sig BOM abstreifen
    assert first.startswith("Interpret,")


def test_db_export_xlsx(tmp_path):
    _setup(tmp_path)
    r = client.get("/api/db/export", params={"fmt": "xlsx"})
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_db_export_pdf(tmp_path):
    _setup(tmp_path)
    r = client.get("/api/db/export", params={"fmt": "pdf"})
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


def test_exports_leave_no_temp_files(tmp_path, monkeypatch):
    """Jeder Export legt cuecore_edl_*/cuecore_db_* im Temp-Ordner an (ein voller DB-PDF-Export ist groß;
    Windows räumt %TEMP% nicht selbst auf). Nach dem Senden sind sie weg, auch wenn das Schreiben scheitert."""
    import tempfile
    _setup(tmp_path)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    cards = [{"artist": "AURORA", "title": "Runaway", "labelcode": "LC00171", "duration_sec": 4}]
    for fmt in ("xlsx", "csv", "pdf"):
        r = client.post("/api/edl/export", json={"cards": cards, "filename": "cue", "fmt": fmt})
        assert r.status_code == 200 and len(r.content) > 0
        r = client.get("/api/db/export", params={"fmt": fmt, "q": "Runaway"})
        assert r.status_code == 200 and len(r.content) > 0
    assert list(tmpdir.iterdir()) == []

    def boom(*a, **kw):
        raise RuntimeError("Platte voll")
    monkeypatch.setattr(appmod.edl_import, "export_csv", boom)
    monkeypatch.setattr(appmod.dbexport, "export_csv", boom)
    failing = TestClient(app, raise_server_exceptions=False)
    assert failing.post("/api/edl/export", json={"cards": cards, "fmt": "csv"}).status_code == 500
    assert failing.get("/api/db/export", params={"fmt": "csv"}).status_code == 500
    assert list(tmpdir.iterdir()) == []


def test_db_export_rejects_bad_format(tmp_path):
    _setup(tmp_path)
    assert client.get("/api/db/export", params={"fmt": "json"}).status_code == 422


# --- GVL-PDF-Reconcile: preview + merge (Master wird auf tmp umgelenkt!) ---

_GVLX = {"labelcode": 17, "label": 114, "kurzname": 211, "plm": 308, "hersteller": 362, "rr": 460}


def _gvl_project(tmp_path, monkeypatch):
    """Legt eine tmp data/docs-Struktur an und lenkt app.PROJECT_DIR dorthin um,
    damit der GVL-Master nicht angefasst wird. Gibt (pdf_b64, project_dir)."""
    import base64
    import json as _json

    import pymupdf
    proj = tmp_path / "proj"
    (proj / "data").mkdir(parents=True); (proj / "docs").mkdir(parents=True)
    (proj / "data" / "GVL_Labelcodes_synced.csv").write_text(
        "labelcode,label,kurzname,plm,hersteller,rechterueckrufe\n00002,Altes Label,,,Alt GmbH,\n",
        encoding="utf-8")
    (proj / "data" / "GVL_Labelcodes_synced.meta.json").write_text(
        _json.dumps({"source_version": "2026-06-23", "row_count": 1}), encoding="utf-8")

    pdf = tmp_path / "GVL-Labelcodes.pdf"
    doc = pymupdf.open()
    doc.set_metadata({"creationDate": "D:20260630080632+02'00'"})
    pg = doc.new_page(width=612, height=792)
    for col, head in [("labelcode", "Labelcode"), ("label", "Label"), ("kurzname", "Kurzname"),
                      ("plm", "PLM"), ("hersteller", "Hersteller"), ("rr", "Rechterückrufe")]:
        pg.insert_text((_GVLX[col], 85), head, fontsize=9)
    pg.insert_text((_GVLX["labelcode"], 110), "00001", fontsize=9)
    pg.insert_text((_GVLX["label"], 110), "Frisches Label", fontsize=9)
    pg.insert_text((_GVLX["hersteller"], 110), "Firma GmbH", fontsize=9)
    doc.save(str(pdf)); doc.close()

    monkeypatch.setattr(appmod, "PROJECT_DIR", proj)
    return base64.b64encode(pdf.read_bytes()).decode("ascii"), proj


def test_gvl_preview_then_merge(tmp_path, monkeypatch):
    _setup(tmp_path)   # setzt DB_PATH auf tmp/labels.db (merge baut genau die neu)
    b64, proj = _gvl_project(tmp_path, monkeypatch)

    prev = client.post("/api/gvl/preview", json={"filename": "GVL.pdf", "content_b64": b64})
    assert prev.status_code == 200
    pj = prev.json()
    assert pj["source_version"] == "2026-06-30" and pj["counts"]["neu"] == 1 and pj["counts"]["entfernt"] == 1
    # Vorschau schreibt nichts
    assert "Frisches Label" not in (proj / "data" / "GVL_Labelcodes_synced.csv").read_text(encoding="utf-8")

    merged = client.post("/api/gvl/merge", json={"token": pj["token"]})
    assert merged.status_code == 200
    mj = merged.json()
    assert mj["rows"] == 1 and mj["source_version"] == "2026-06-30"
    assert "Frisches Label" in (proj / "data" / "GVL_Labelcodes_synced.csv").read_text(encoding="utf-8")
    # Index wurde neu geladen und findet den frischen Code
    assert client.get("/api/search", params={"q": "00001"}).json()["results"][0]["label"] == "Frisches Label"


def test_gvl_merge_older_pdf_needs_allow_older(tmp_path, monkeypatch):
    """Vorschau trägt older_than_master/pdf_version/master_version; Merge verweigert ein älteres PDF
    mit 409 und lesbarem detail, außer allow_older=true im JSON-Body wie token."""
    import json as _json
    _setup(tmp_path)
    b64, proj = _gvl_project(tmp_path, monkeypatch)
    (proj / "data" / "GVL_Labelcodes_synced.meta.json").write_text(
        _json.dumps({"source_version": "2026-09-15", "row_count": 1}), encoding="utf-8")
    pj = client.post("/api/gvl/preview", json={"filename": "GVL.pdf", "content_b64": b64}).json()
    assert (pj["pdf_version"], pj["master_version"], pj["older_than_master"]) == ("2026-06-30", "2026-09-15", True)
    r = client.post("/api/gvl/merge", json={"token": pj["token"]})
    assert r.status_code == 409 and "2026-06-30" in r.json()["detail"] and "2026-09-15" in r.json()["detail"]
    assert "Frisches Label" not in (proj / "data" / "GVL_Labelcodes_synced.csv").read_text(encoding="utf-8")
    r = client.post("/api/gvl/merge", json={"token": pj["token"], "allow_older": True})
    assert r.status_code == 200 and r.json()["source_version"] == "2026-06-30"


def test_gvl_merge_rows_match_loaded_rows_in_bundle_layout(tmp_path, monkeypatch):
    """Gebündelte App (gvl/ im Datenordner, Zusatz-Codes im Bundle): merge zählt mit denselben Zusatz-Codes wie
    das Laden, die Erfolgsmeldung nennt dieselbe Zeilenzahl wie die Rail danach."""
    import base64
    import sys
    db = _setup(tmp_path)
    b64, proj = _gvl_project(tmp_path, monkeypatch)
    bundle = tmp_path / "bundle"
    (bundle / "data").mkdir(parents=True)
    for name in ("GVL_Labelcodes_synced.csv", "GVL_Labelcodes_synced.meta.json"):
        (bundle / "data" / name).write_bytes((proj / "data" / name).read_bytes())
    (bundle / "data" / "labelcodes_supplemental.csv").write_text(
        "labelcode,label,kurzname,plm,hersteller,rechterueckrufe,quelle\n"
        "99001,Zusatz Library,,,Some GmbH,,KPM-PDF\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(appmod, "_bundle_base", lambda: bundle)
    pj = client.post("/api/gvl/preview", json={"filename": "GVL.pdf", "content_b64": b64}).json()
    assert (db.parent / "gvl" / "GVL_Labelcodes_synced.csv").exists()          # Bundle-Layout greift
    mj = client.post("/api/gvl/merge", json={"token": pj["token"]}).json()
    assert mj["rows"] == mj["rows_loaded"] == 2


def test_gvl_preview_rejects_non_pdf(tmp_path):
    _setup(tmp_path)
    import base64
    b64 = base64.b64encode(b"nope").decode("ascii")
    assert client.post("/api/gvl/preview", json={"filename": "x.txt", "content_b64": b64}).status_code == 422


def test_gvl_merge_stale_token(tmp_path, monkeypatch):
    _setup(tmp_path)
    _gvl_project(tmp_path, monkeypatch)
    assert client.post("/api/gvl/merge", json={"token": "abc"}).status_code == 409


# --- Tagger-Intake ---

def test_intake_scan(tmp_path):
    (tmp_path / "The Weeknd - Blinding Lights.mp3").write_bytes(b"")
    (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")
    r = client.post("/api/intake/scan", json={"folder": str(tmp_path)})
    assert r.status_code == 200
    j = r.json()
    assert j["count"] == 1 and j["files"][0]["artist"] == "The Weeknd"


def test_intake_scan_bad_folder():
    r = client.post("/api/intake/scan", json={"folder": "/gibt/es/nicht/xyz"})
    assert r.status_code == 400


def test_intake_scan_surfaces_filename_lc(tmp_path):
    _setup(tmp_path)   # Master kennt 00121 -> Atlantic
    (tmp_path / "Alanis - Thank You - [Atlantic_LC00121].mp3").write_bytes(b"")
    j = client.post("/api/intake/scan", json={"folder": str(tmp_path)}).json()
    f = j["files"][0]
    assert f["filename_lc"] == "00121" and f["filename_label"] == "Atlantic"
    assert f["filename_rr"] == "R6;R9"


# --- Tagger-Plan (manuelle LC-Eingabe / 'Ändern'-Edits, nicht-destruktiv) ---

def test_intake_resolve_without_keys_names_free_acoustid_first(tmp_path, monkeypatch):
    """Mit AcoustID reicht einer der beiden Schlüssel: der Hinweis nennt den kostenlosen AcoustID-Schlüssel
    zuerst, nicht nur den kostenpflichtigen AudD-Token. Die englische Oberfläche übersetzt das Detail wortgleich (I18N_EN)."""
    _setup(tmp_path)
    monkeypatch.setattr(appmod.settings, "get", lambda k, *a, **kw: "")
    monkeypatch.setattr(appmod.settings, "BUILTIN_PATH", tmp_path / "fehlt.bin")
    r = client.post("/api/intake/resolve", json={"path": str(tmp_path / "x.mp3"), "artist": "", "title": ""})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "AcoustID" in detail and "AudD" in detail and detail.index("AcoustID") < detail.index("AudD")
    html = (Path(appmod.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    assert '"' + detail + '":' in html, "I18N_EN-Eintrag für das Detail fehlt"



# --- Eingebauter AcoustID-Key: wirkt wie ein eigener, steht nie in /api/settings ---

def _builtin_acoustid(tmp_path, monkeypatch, value="zzqBuiltinApp9"):
    f = tmp_path / "builtin_keys.bin"
    f.write_bytes(appmod.settings.encode_builtin({"acoustid_api_key": value}))
    monkeypatch.setattr(appmod.settings, "BUILTIN_PATH", f)
    monkeypatch.setattr(appmod.settings, "SETTINGS_PATH", tmp_path / "settings.json")
    return value


def test_builtin_acoustid_key_counts_as_configured_but_never_leaves_the_app(tmp_path, monkeypatch):
    value = _builtin_acoustid(tmp_path, monkeypatch)
    r = client.get("/api/settings")
    assert r.json()["configured"]["acoustid"] is True
    k = next(k for k in r.json()["keys"] if k["key"] == "acoustid_api_key")
    assert k["builtin"] is True and k["set"] is False
    assert value not in r.text
    assert value not in client.post("/api/settings", json={"updates": {}}).text


def test_builtin_acoustid_key_drives_fingerprint_availability_and_conncheck(tmp_path, monkeypatch):
    value = _builtin_acoustid(tmp_path, monkeypatch)
    _setup(tmp_path)
    seen = []
    monkeypatch.setattr(appmod.acoustid, "recognize", lambda path, key, **kw: seen.append(key))
    monkeypatch.setattr(appmod, "_audd_recognize", lambda *a: None)
    appmod._fingerprint_recognize("x.mp3", "", "")
    monkeypatch.setattr(appmod.intake, "resolve_card", lambda *a, **kw: {})
    r = client.post("/api/intake/resolve", json={"path": str(tmp_path / "x.mp3"), "artist": "", "title": ""})
    assert r.status_code == 200                       # kein „Artist und Titel nötig": der eingebaute Key reicht
    monkeypatch.setattr(appmod.conncheck, "check_acoustid", lambda key: seen.append(key) or {"ok": True})
    r = client.post("/api/settings/test/acoustid")
    assert seen == [value, value] and value not in r.text


def test_intake_plan_valid_lc(tmp_path):
    _setup(tmp_path)
    src = tmp_path / "x.mp3"
    src.write_bytes(b"")
    r = client.post("/api/intake/plan",
                    json={"path": str(src), "artist": "Alanis", "title": "Thank You", "labelcode": "00121"})
    assert r.status_code == 200
    j = r.json()
    assert j["label"] == "Atlantic" and j["lc"] == "LC00121" and j["in_master"] is True
    assert j["plan"]["can_apply"] is True
    assert j["plan"]["proposed_name"] == "Alanis - Thank You - [Atlantic_LC00121].mp3"
    assert tags.read_tags(src) == {}        # nicht-destruktiv


def test_intake_plan_lc_not_in_master(tmp_path):
    _setup(tmp_path)
    src = tmp_path / "x.mp3"
    src.write_bytes(b"")
    j = client.post("/api/intake/plan",
                    json={"path": str(src), "artist": "A", "title": "B", "labelcode": "99999"}).json()
    assert j["label"] == "" and j["in_master"] is False
    assert j["plan"]["can_apply"] is False
    assert any("nicht im GVL-Master" in w for w in j["plan"]["warnings"])


def test_intake_plan_empty_lc_blocks(tmp_path):
    _setup(tmp_path)
    src = tmp_path / "x.mp3"
    src.write_bytes(b"")
    j = client.post("/api/intake/plan",
                    json={"path": str(src), "artist": "A", "title": "B", "labelcode": ""}).json()
    assert j["in_master"] is None and j["plan"]["can_apply"] is False


def test_intake_resolve_endpoint(tmp_path, monkeypatch):
    _setup(tmp_path)
    _mock_net(monkeypatch)
    src = tmp_path / "x.mp3"
    src.write_bytes(b"")
    r = client.post("/api/intake/resolve",
                    json={"path": str(src), "artist": "Zzz Unbekannt", "title": "Nicht In DB"})
    assert r.status_code == 200
    j = r.json()
    assert j["best"]["labelcode"] == "00121" and j["best"]["lc"] == "LC00121"
    assert j["plan"]["proposed_name"].endswith("[Atlantic_LC00121].mp3")
    assert tags.read_tags(src) == {}            # Dry-Run: Datei unangetastet


def test_intake_apply_requires_confirm(tmp_path):
    _setup(tmp_path)
    src = tmp_path / "s.mp3"
    src.write_bytes(b"")
    r = client.post("/api/intake/apply",
                    json={"path": str(src), "artist": "A", "title": "B", "labelcode": "00121"})
    assert r.status_code == 400 and src.exists()


def test_intake_apply_blocks_without_lc_dir(tmp_path, monkeypatch):
    _setup(tmp_path)
    monkeypatch.setattr(appmod.settings, "SETTINGS_PATH", tmp_path / "settings.json")  # leer -> kein lc_dir
    src = tmp_path / "s.mp3"
    src.write_bytes(b"")
    r = client.post("/api/intake/apply",
                    json={"path": str(src), "artist": "A", "title": "B", "labelcode": "00121", "confirm": True})
    assert r.status_code == 409 and src.exists()


def test_intake_apply_and_undo(tmp_path, monkeypatch):
    _setup(tmp_path)
    monkeypatch.setattr(appmod.settings, "SETTINGS_PATH", tmp_path / "settings.json")
    lc_dir = tmp_path / "LC verified"
    appmod.settings.set_paths({"lc_verified_dir": str(lc_dir)})
    src = tmp_path / "src.mp3"
    src.write_bytes(b"")
    tags.write_tags(src, {"artist": "Old", "title": "Old"})

    r = client.post("/api/intake/apply",
                    json={"path": str(src), "artist": "Alanis", "title": "Thank You",
                          "labelcode": "00121", "confirm": True})
    assert r.status_code == 200
    j = r.json()
    assert j["name"] == "Alanis - Thank You - [Atlantic_LC00121].mp3"
    assert j["rr_pending"] == "R6;R9"
    moved = lc_dir / j["name"]
    assert moved.exists() and not src.exists()
    assert tags.read_tags(moved)["lc"] == "LC00121"

    u = client.post("/api/intake/undo", json={"op_ids": j["op_ids"]})
    assert u.status_code == 200 and u.json()["undone"] == 3      # Tag, Rename, songs.db-Eintrag
    assert src.exists() and not moved.exists()              # Rename zurück
    assert "lc" not in tags.read_tags(src)                  # Tags zurück


def _apply_undo_setup(tmp_path, monkeypatch):
    _setup(tmp_path)
    monkeypatch.setattr(appmod.settings, "SETTINGS_PATH", tmp_path / "settings.json")
    lc_dir = tmp_path / "LC verified"
    appmod.settings.set_paths({"lc_verified_dir": str(lc_dir)})
    src = tmp_path / "src.mp3"
    src.write_bytes(b"")
    tags.write_tags(src, {"artist": "Old", "title": "Old"})
    return src


def _apply(src, code, **extra):
    r = client.post("/api/intake/apply", json={"path": str(src), "artist": "Zzq Undo", "title": "Probe Stellig",
                                              "labelcode": code, "confirm": True, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_intake_undo_removes_flywheel_row(tmp_path, monkeypatch):
    """Undo nimmt auch den songs.db-Eintrag des Apply zurück — sonst käme der verworfene LC als
    lokaler, verifizierter AUTO-Treffer (conf 1.0, kein Netz) zurück, auch ins Cue-Sheet."""
    src = _apply_undo_setup(tmp_path, monkeypatch)
    before = client.get("/api/health").json()["songs"]
    j = _apply(src, "00121", composer="Zzq Komponist")
    assert appmod.songsdb.find(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig")["labelcode"] == "LC00121"   # songs.db speichert LC+Ziffern
    u = client.post("/api/intake/undo", json={"op_ids": j["op_ids"]})
    assert u.status_code == 200 and u.json()["undone"] == len(j["op_ids"])
    assert src.exists()
    assert appmod.songsdb.find(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig") is None
    assert client.post("/api/db/lookup", json={"artist": "Zzq Undo", "title": "Probe Stellig"}).json()["db"] is None
    assert client.get("/api/health").json()["songs"] == before
    # zweites Undo derselben ids: nichts mehr zu tun, nichts kaputt
    assert client.post("/api/intake/undo", json={"op_ids": j["op_ids"]}).json()["undone"] == 0


def test_intake_undo_empties_fields_the_apply_filled(tmp_path, monkeypatch):
    """Vorhandene Zeile ohne LC: Apply füllt labelcode/labelname (PROTECT füllt nur Leeres), Undo
    leert genau diese Felder wieder — sonst schützt PROTECT den verworfenen Code dauerhaft."""
    src = _apply_undo_setup(tmp_path, monkeypatch)
    appmod.songsdb.create_song(appmod.SONGS_CON, {"interpret": "Zzq Undo", "titel": "Probe Stellig",
                                                  "komponist": "Schon Da"})
    j = _apply(src, "00121", composer="Anderer Komponist", album="Neues Album")
    row = appmod.songsdb.find(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig")
    assert (row["labelcode"], row["album"], row["komponist"]) == ("LC00121", "Neues Album", "Schon Da")
    client.post("/api/intake/undo", json={"op_ids": j["op_ids"]})
    row = appmod.songsdb.find(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig")
    assert row is not None                                    # die Zeile gab es vorher, sie bleibt
    assert not row["labelcode"] and not row["labelname"] and not row["album"]
    assert row["komponist"] == "Schon Da"


def test_intake_undo_then_correct_lc_lands_in_songsdb(tmp_path, monkeypatch):
    """Nach Undo und neuem Apply mit dem richtigen LC steht der richtige Code in der songs.db
    (der verworfene bleibt nicht stehen, PROTECT lässt den neuen durch)."""
    src = _apply_undo_setup(tmp_path, monkeypatch)
    j = _apply(src, "00121")
    client.post("/api/intake/undo", json={"op_ids": j["op_ids"]})
    _apply(src, "10192")
    row = appmod.songsdb.find(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig")
    assert (row["labelcode"], row["labelname"]) == ("LC10192", "DOMINO British")


def _quelle(artist, title):
    return appmod.SONGS_CON.execute("SELECT quelle FROM songs WHERE interpret=? AND titel=?", (artist, title)).fetchone()[0]


def _second_file(tmp_path, name):
    src = tmp_path / name
    src.write_bytes(b"")
    tags.write_tags(src, {"artist": "Old", "title": "Old"})
    return src


def test_intake_apply_ueberschreibt_den_lc_der_eigenen_tagger_zeile(tmp_path, monkeypatch):
    """Ein zweites bestätigtes Apply desselben Songs mit einem ANDEREN LC (zweite Datei, oder der
    erste Code war falsch und wurde ohne Undo neu getaggt) bleibt in der songs.db nicht am alten Code hängen, obwohl
    PROTECT nur Leeres füllt: Datei sagt LC10192, die DB nicht weiter LC00121. Eine Zeile, die der Tagger selbst
    angelegt hat (quelle 'tagger'), folgt dem neuen Code samt GVL-Namen; Undo stellt den alten wieder her."""
    src = _apply_undo_setup(tmp_path, monkeypatch)
    _apply(src, "00121", composer="Zzq Komponist")
    row = appmod.songsdb.find(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig")
    assert (_quelle("Zzq Undo", "Probe Stellig"), row["labelcode"]) == ("tagger", "LC00121")
    j = _apply(_second_file(tmp_path, "zweit.mp3"), "10192", composer="Anderer Komponist")
    assert tags.read_tags(Path(j["new_path"]))["lc"] == "LC10192"
    row = appmod.songsdb.find(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig")
    assert (row["labelcode"], row["labelname"]) == ("LC10192", "DOMINO British")
    assert row["komponist"] == "Zzq Komponist"                        # übrige Felder bleiben PROTECT
    client.post("/api/intake/undo", json={"op_ids": j["op_ids"]})
    row = appmod.songsdb.find(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig")
    assert (row["labelcode"], row["labelname"]) == ("LC00121", "Atlantic")


def test_intake_apply_laesst_fremde_zeilen_geschuetzt(tmp_path, monkeypatch):
    """Gegenprobe: eine Zeile, die nicht der Tagger angelegt hat (Hand-Eintrag, Library, Import), behält ihren
    LC — PROTECT gilt dort weiter."""
    src = _apply_undo_setup(tmp_path, monkeypatch)
    appmod.songsdb.create_song(appmod.SONGS_CON, {"interpret": "Zzq Undo", "titel": "Probe Stellig",
                                                  "labelcode": "LC00121", "labelname": "Atlantic"})
    _apply(src, "10192")
    row = appmod.songsdb.find(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig")
    assert (_quelle("Zzq Undo", "Probe Stellig"), row["labelcode"], row["labelname"]) == ("manuell", "LC00121", "Atlantic")


def test_von_hand_korrigierte_tagger_zeile_bleibt_beim_naechsten_apply(tmp_path, monkeypatch):
    """Eine Tagger-Zeile, die im Editor der Track-Datenbank von Hand korrigiert wurde,
    ist keine unveränderte eigene Zeile mehr. Speichern kennzeichnet sie (quelle 'tagger:manuell'), das nächste
    Apply mit einem anderen LC lässt Code und Namen stehen. Unveränderte Tagger-Zeilen folgen weiter (Test oben)."""
    src = _apply_undo_setup(tmp_path, monkeypatch)
    _apply(src, "00121")
    sid = appmod.songsdb.find_row(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig")["id"]
    r = client.post(f"/api/db/song/{sid}", json={"fields": {"labelcode": "10192", "labelname": "DOMINO British"}})
    assert r.status_code == 200, r.text
    assert _quelle("Zzq Undo", "Probe Stellig") == "tagger:manuell"
    j = _apply(_second_file(tmp_path, "zweit.mp3"), "00309")
    assert tags.read_tags(Path(j["new_path"]))["lc"] == "LC00309"                   # die Datei bekommt den neuen Code
    row = appmod.songsdb.find(appmod.SONGS_CON, "Zzq Undo", "Probe Stellig")
    assert (row["labelcode"], row["labelname"]) == ("LC10192", "DOMINO British")


def test_editor_kennzeichnet_nur_tagger_zeilen(tmp_path):
    """Gegenprobe: andere Herkunft bleibt beim Speichern im Editor, wie sie ist — Library-Zeilen erkennt die
    Selbstheilung an quelle 'library:*', Hand-Einträge sind schon 'manuell'."""
    _setup(tmp_path)
    con = appmod.SONGS_CON
    for quelle, titel in (("library:upm", "Eins"), ("manuell", "Zwei"), ("index", "Drei")):
        appmod.songsdb.upsert(con, interpret="Zzq Herkunft", titel=titel, quelle=quelle)
        sid = appmod.songsdb.find_row(con, "Zzq Herkunft", titel)["id"]
        assert client.post(f"/api/db/song/{sid}", json={"fields": {"komponist": "Erika Testerin"}}).status_code == 200
        assert _quelle("Zzq Herkunft", titel) == quelle


def test_intake_apply_collision_409(tmp_path, monkeypatch):
    _setup(tmp_path)
    monkeypatch.setattr(appmod.settings, "SETTINGS_PATH", tmp_path / "settings.json")
    lc_dir = tmp_path / "LC verified"
    lc_dir.mkdir()
    appmod.settings.set_paths({"lc_verified_dir": str(lc_dir)})
    # Zielname schon belegt
    (lc_dir / "Alanis - Thank You - [Atlantic_LC00121].mp3").write_bytes(b"da")
    src = tmp_path / "src.mp3"
    src.write_bytes(b"")
    r = client.post("/api/intake/apply",
                    json={"path": str(src), "artist": "Alanis", "title": "Thank You",
                          "labelcode": "00121", "confirm": True})
    assert r.status_code == 409 and src.exists()


def test_intake_apply_writes_metadata_and_upserts_songsdb(tmp_path, monkeypatch):
    _setup(tmp_path)
    monkeypatch.setattr(appmod.settings, "SETTINGS_PATH", tmp_path / "settings.json")
    lc_dir = tmp_path / "LC verified"
    appmod.settings.set_paths({"lc_verified_dir": str(lc_dir)})
    src = tmp_path / "src.mp3"
    src.write_bytes(b"")

    r = client.post("/api/intake/apply",
                    json={"path": str(src), "artist": "Alanis", "title": "Thank You",
                          "labelcode": "00121", "isrc": "USMA10100001",
                          "album": "Jagged Little Pill", "composer": "Alanis Morissette",
                          "lyricist": "Alanis Morissette", "confirm": True})
    assert r.status_code == 200
    j = r.json()
    moved = Path(j["new_path"])
    tagged = tags.read_tags(moved)
    assert tagged["album"] == "Jagged Little Pill"
    assert tagged["composer"] == "Alanis Morissette"
    assert tagged["lyricist"] == "Alanis Morissette"

    # Der Track muss sofort als verifizierte lokale Quelle auffindbar sein
    hit = appmod.songsdb.find(appmod.SONGS_CON, "Alanis", "Thank You")
    assert hit is not None
    assert hit["labelcode"] == "LC00121" and hit["labelname"] == "Atlantic"        # LC + Ziffern
    assert hit["komponist"] == "Alanis Morissette" and hit["texter"] == "Alanis Morissette"


def test_intake_apply_persists_gema_fields_to_songsdb(tmp_path, monkeypatch):
    _setup(tmp_path)
    monkeypatch.setattr(appmod.settings, "SETTINGS_PATH", tmp_path / "settings.json")
    lc_dir = tmp_path / "LC verified"
    appmod.settings.set_paths({"lc_verified_dir": str(lc_dir)})
    src = tmp_path / "src.mp3"
    src.write_bytes(b"")
    r = client.post("/api/intake/apply",
                    json={"path": str(src), "artist": "Zzq Gema", "title": "Werktest",
                          "labelcode": "00121", "album": "Alben", "track_number": "3/12",
                          "iswc": "T-034.524.680-1", "gema_werknummer": "12345678",
                          "confirm": True})
    assert r.status_code == 200
    hit = appmod.songsdb.find(appmod.SONGS_CON, "Zzq Gema", "Werktest")
    assert hit["album"] == "Alben" and hit["track_number"] == "3/12"
    assert hit["iswc"] == "T-034.524.680-1" and hit["gema_werknummer"] == "12345678"


def test_settings_includes_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod.settings, "SETTINGS_PATH", tmp_path / "settings.json")
    r = client.get("/api/settings")
    assert r.status_code == 200
    keys = {p["key"] for p in r.json()["paths"]}
    assert "lc_verified_dir" in keys


# --- Nativer Ordner-Dialog + Beenden ---

def test_pick_folder_returns_chosen_path(monkeypatch):
    monkeypatch.setattr(appmod.folderpick, "choose_folder", lambda: "/Users/zzq/Eingang")
    r = client.post("/api/pick-folder")
    assert r.status_code == 200 and r.json()["path"] == "/Users/zzq/Eingang"


def test_pick_folder_cancel_returns_empty(monkeypatch):
    monkeypatch.setattr(appmod.folderpick, "choose_folder", lambda: "")
    assert client.post("/api/pick-folder").json()["path"] == ""


def test_quit_schedules_shutdown_without_killing(monkeypatch):
    called = {"n": 0}
    monkeypatch.setitem(appmod._QUIT, "fn", lambda: called.__setitem__("n", called["n"] + 1))
    r = client.post("/api/quit")
    assert r.status_code == 200 and r.json()["quitting"] is True


# --- Album-basierte LC-Auflösung ---

def test_album_resolve_via_musicbrainz(tmp_path, monkeypatch):
    from core.musicbrainz import MBReleaseHit
    _setup(tmp_path)   # Master kennt 00121 Atlantic
    monkeypatch.setattr(appmod.musicbrainz, "search_releases",
                        lambda alb, **kw: [MBReleaseHit(title="Soundtrack", status="Official",
                                                        labels=["Atlantic"], score=100, release_id="rel-9")])
    monkeypatch.setattr(appmod.discogs, "search_release_ids", lambda alb, **kw: [])
    monkeypatch.setattr(appmod.musiksammler, "confirms", lambda code, label: False)
    r = client.post("/api/album/resolve", json={"album": "Some Soundtrack", "artist": "Various"})
    assert r.status_code == 200
    j = r.json()
    assert j["best"]["labelcode"] == "00121" and j["best"]["label"] == "Atlantic"
    assert j["best"]["source_urls"][0]["url"].endswith("/rel-9")


def test_album_resolve_dual_source_with_musiksammler(tmp_path, monkeypatch):
    from core.musicbrainz import MBReleaseHit
    _setup(tmp_path)
    monkeypatch.setattr(appmod.musicbrainz, "search_releases",
                        lambda alb, **kw: [MBReleaseHit(status="Official", labels=["Atlantic"],
                                                        score=100, release_id="rel-1")])
    monkeypatch.setattr(appmod.discogs, "search_release_ids", lambda alb, **kw: [])
    monkeypatch.setattr(appmod.musiksammler, "confirms", lambda code, label: code == "00121")
    j = client.post("/api/album/resolve", json={"album": "X"}).json()
    assert j["best"]["dual_source"] is True and set(j["best"]["sources"]) == {"musicbrainz", "musik-sammler"}


def test_album_resolve_requires_album(tmp_path):
    _setup(tmp_path)
    assert client.post("/api/album/resolve", json={"album": "   "}).status_code == 400


def test_open_url_rejects_non_http():
    assert client.post("/api/open-url", json={"url": "file:///etc/passwd"}).status_code == 422


def test_open_url_opens_browser(monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: True)
    r = client.post("/api/open-url", json={"url": "https://musicbrainz.org/release/x"})
    assert r.status_code == 200 and r.json()["opened"] is True


def test_resolve_endpoint_uses_isrc_stage0(tmp_path, monkeypatch):
    """Mit ISRC im Body läuft die exakte Stufe 0 — die Fuzzy-Textsuche wird
    übersprungen (hier hart geprüft: sie würde eine Exception werfen)."""
    _setup(tmp_path)
    _mock_net(monkeypatch)
    seen = {}

    def never(a, t, **kw):
        raise AssertionError("Textsuche darf bei ISRC-Treffer nicht laufen")

    def by_isrc(code, **kw):
        seen["isrc"] = code
        return MBLookup(matched=True, score=100, via_isrc=True, label_names=["Atlantic"])

    monkeypatch.setattr(appmod.musicbrainz, "lookup", never)
    monkeypatch.setattr(appmod.musicbrainz, "lookup_by_isrc", by_isrc)
    j = client.post("/api/resolve", json={"artist": "Zzz Unbekannt", "title": "Nicht In DB",
                                          "isrc": "US-MA1-01-00001"}).json()
    assert seen["isrc"] == "US-MA1-01-00001"          # Normalisierung passiert im Lookup
    assert j["candidates"][0]["labelcode"] == "00121"


def test_intake_resolve_endpoint_accepts_isrc(tmp_path, monkeypatch):
    _setup(tmp_path)
    _mock_net(monkeypatch)
    seen = {}

    def by_isrc(code, **kw):
        seen["isrc"] = code
        return MBLookup(matched=True, score=100, via_isrc=True, label_names=["Atlantic"])

    monkeypatch.setattr(appmod.musicbrainz, "lookup_by_isrc", by_isrc)
    src = tmp_path / "x.mp3"
    src.write_bytes(b"")
    j = client.post("/api/intake/resolve",
                    json={"path": str(src), "artist": "Zzz Unbekannt", "title": "Nicht In DB",
                          "isrc": "USMA10100001"}).json()
    assert seen["isrc"] == "USMA10100001"
    assert j["via_isrc"] is True and j["best"]["labelcode"] == "00121"


# --- Track-Metadata-API (EDL-Tab, zweite Library-Quelle neben editingtools) ---

def _trackmeta_cfg(monkeypatch, host="api.example.com", cid="test-client", secret="S", clear=True, partners=None):
    """Partnerliste in den Settings faken (Datei bleibt unangetastet)."""
    plist = partners if partners is not None else [
        {"id": "p1", "name": "Partner A", "host": host, "client_id": cid, "client_secret": secret}]
    plist = [p for p in plist if p["host"] and p["client_id"]]
    monkeypatch.setattr(appmod.settings, "partners", lambda *a, **kw: plist)
    monkeypatch.setattr(appmod.settings, "partners_status", lambda *a, **kw: [
        {"id": p["id"], "name": p["name"], "host": p["host"], "client_id": p["client_id"],
         "secret_set": bool(p["client_secret"]), "complete": bool(p["client_secret"])} for p in plist])
    if clear:
        appmod._TRACKMETA_CLIENTS.clear()


def _lookup_ok(entries):
    return {"ok": True, "error": "", "status": 200, "requests": 1, "entries": entries,
            "matched": sum(1 for e in entries if e.get("matched"))}


def test_settings_view_lists_partners_without_secrets(monkeypatch):
    _trackmeta_cfg(monkeypatch)
    r = client.get("/api/settings").json()
    assert r["partners"] == [{"id": "p1", "name": "Partner A", "host": "api.example.com", "client_id": "test-client",
                              "secret_set": True, "complete": True}]
    assert set(r["partner_fields"]) == {"name", "host", "client_id", "client_secret"}
    assert not any(k["key"].startswith("trackmeta") for k in r["keys"])
    assert r["configured"]["trackmeta"] is True
    assert all("value" not in k for k in r["keys"])


def test_settings_post_partners_roundtrip(tmp_path, monkeypatch):
    """POST /api/settings mit `partners` ersetzt die Liste; ohne `partners` bleibt sie stehen."""
    monkeypatch.setattr(appmod.settings, "SETTINGS_PATH", tmp_path / "settings.json")
    j = client.post("/api/settings", json={"updates": {}, "partners": [
        {"name": "Partner A", "host": "a.example", "client_id": "test-client", "client_secret": "S"},
        {"name": "Zwei", "host": "b.example", "client_id": "11", "client_secret": "T"}]}).json()
    assert [p["name"] for p in j["partners"]] == ["Partner A", "Zwei"] and j["configured"]["trackmeta"] is True
    j = client.post("/api/settings", json={"updates": {"discogs_token": "x"}}).json()
    assert [p["name"] for p in j["partners"]] == ["Partner A", "Zwei"]
    j = client.post("/api/settings", json={"updates": {}, "partners": []}).json()
    assert j["partners"] == [] and j["configured"]["trackmeta"] is False
    appmod._TRACKMETA_CLIENTS.clear()


def test_edl_trackmeta_scan_requires_full_config(tmp_path, monkeypatch):
    _setup(tmp_path)
    _trackmeta_cfg(monkeypatch, secret="")
    r = client.post("/api/edl/trackmeta-scan", json={"cues": [{"fileName": "FT_BAM-AL87_15-Newborn_Day.mp3"}]})
    assert r.status_code == 400
    _trackmeta_cfg(monkeypatch)
    r = client.post("/api/edl/trackmeta-scan", json={"cues": [{"fileName": "  "}]})
    assert r.status_code == 422


def test_edl_trackmeta_scan_validates_labelcode_against_gvl(tmp_path, monkeypatch):
    _setup(tmp_path)
    _trackmeta_cfg(monkeypatch)
    seen = {}

    def fake_lookup(names, *, client):
        seen["names"], seen["client"] = names, client
        return _lookup_ok([
            {"fileName": "a.mp3", "query_filename": "a.mp3", "matched": True, "labelcode_raw": "00121", "labelname": "Irgendwas", "titel": "T", "komponist": "C", "source": "trackmeta"},
            {"fileName": "b.mp3", "query_filename": "b.mp3", "matched": True, "labelcode_raw": "", "labelname": "Atlantic", "titel": "U", "source": "trackmeta"},   # nur Name -> gemappt
            {"fileName": "c.mp3", "query_filename": "c.mp3", "matched": True, "labelcode_raw": "99999", "labelname": "Nixgibts", "titel": "V", "source": "trackmeta"},  # kein GVL
            {"fileName": "d.mp3", "query_filename": "d.mp3", "matched": False, "labelcode_raw": "", "labelname": "", "titel": "", "source": "trackmeta"},         # kein Treffer
        ])
    monkeypatch.setattr(appmod.trackmeta, "lookup", fake_lookup)
    j = client.post("/api/edl/trackmeta-scan", json={"cues": [
        {"fileName": "a.mp3"}, {"clip_name": "b.mp3"}, {"fileName": "c.mp3"}, {"fileName": "d.mp3"}, {"fileName": " "}]}).json()
    assert seen["names"] == ["a.mp3", "b.mp3", "c.mp3", "d.mp3"]
    assert seen["client"].configured() and seen["client"].base == "https://api.example.com/zzq/v1"
    assert j["ok"] and j["matched"] == 3 and j["count"] == 4 and j["requests"] == 1
    e = j["entries"]
    assert e[0]["labelcode"] == "00121" and e[0]["labelname"] == "Atlantic"          # roher LC im Master
    assert e[1]["labelcode"] == "00121" and "Labelname" in e[1]["lc_note"]           # via Name gemappt
    assert e[2]["labelcode"] == "" and "Partner A-LC nicht im GVL" in e[2]["lc_note"]  # nicht mappbar, Quelle = Partnername
    assert e[3]["labelcode"] == "" and e[3]["lc_note"] == ""                         # null: keine Note
    assert [x.get("rechterueckrufe") for x in e] == ["R6;R9", "R6;R9", "", ""]         # R-Code zum LC


def test_edl_trackmeta_scan_reuses_client_until_settings_change(tmp_path, monkeypatch):
    _setup(tmp_path)
    _trackmeta_cfg(monkeypatch)
    clients = []
    monkeypatch.setattr(appmod.trackmeta, "lookup", lambda names, *, client: (clients.append(client), _lookup_ok([]))[1])
    body = {"cues": [{"fileName": "a.mp3"}]}
    client.post("/api/edl/trackmeta-scan", json=body)
    client.post("/api/edl/trackmeta-scan", json=body)
    assert clients[0] is clients[1]                                                  # Token-Cache bleibt
    _trackmeta_cfg(monkeypatch, secret="S2", clear=False)                            # rotiertes Secret
    client.post("/api/edl/trackmeta-scan", json=body)
    assert clients[2] is not clients[0] and clients[2].client_secret == "S2"


def test_edl_trackmeta_scan_asks_partners_in_order_only_for_open_names(tmp_path, monkeypatch):
    """Mehrere Partner: der zweite bekommt nur die Namen, die der erste nicht
    aufgelöst hat; jeder Treffer trägt den Partnernamen; ein Partner-Fehler kippt nicht den
    ganzen Scan, solange ein anderer geantwortet hat."""
    _setup(tmp_path)
    _trackmeta_cfg(monkeypatch, partners=[
        {"id": "p1", "name": "Partner A", "host": "a.example", "client_id": "test-client", "client_secret": "S"},
        {"id": "p2", "name": "Zwei", "host": "b.example", "client_id": "11", "client_secret": "T"},
        {"id": "p3", "name": "Drei", "host": "c.example", "client_id": "12", "client_secret": "U"}])
    asked = []

    def fake_lookup(names, *, client):
        asked.append((client.client_id, list(names)))
        if client.client_id == "test-client":
            return _lookup_ok([{"fileName": "a.mp3", "query_filename": "a.mp3", "matched": True, "labelcode_raw": "00121",
                                "labelname": "", "titel": "A", "source": "trackmeta"},
                               {"fileName": "b.mp3", "query_filename": "b.mp3", "matched": False, "labelcode_raw": "", "labelname": "", "titel": "", "source": "trackmeta"},
                               {"fileName": "c.mp3", "query_filename": "c.mp3", "matched": False, "labelcode_raw": "", "labelname": "", "titel": "", "source": "trackmeta"}])
        if client.client_id == "11":
            return {"ok": False, "error": "rate_limited", "status": 429, "entries": [], "matched": 0, "requests": 1}
        return _lookup_ok([{"fileName": "b.mp3", "query_filename": "b.mp3", "matched": True, "labelcode_raw": "",
                            "labelname": "Atlantic", "titel": "B", "source": "trackmeta"},
                           {"fileName": "c.mp3", "query_filename": "c.mp3", "matched": False, "labelcode_raw": "", "labelname": "", "titel": "", "source": "trackmeta"}])
    monkeypatch.setattr(appmod.trackmeta, "lookup", fake_lookup)
    j = client.post("/api/edl/trackmeta-scan", json={"cues": [{"fileName": "a.mp3"}, {"fileName": "b.mp3"}, {"fileName": "c.mp3"}]}).json()
    assert asked == [("test-client", ["a.mp3", "b.mp3", "c.mp3"]), ("11", ["b.mp3", "c.mp3"]), ("12", ["b.mp3", "c.mp3"])]
    assert j["ok"] is True and j["matched"] == 2 and j["count"] == 3 and j["requests"] == 3
    e = {x["fileName"]: x for x in j["entries"]}
    assert e["a.mp3"]["partner"] == "Partner A" and e["a.mp3"]["labelcode"] == "00121"
    assert e["b.mp3"]["partner"] == "Drei" and e["b.mp3"]["labelcode"] == "00121" and e["b.mp3"]["labelname"] == "Atlantic"
    assert e["c.mp3"]["matched"] is False and "partner" not in e["c.mp3"]
    assert [(r["partner"], r["ok"], r["asked"], r["matched"]) for r in j["partners"]] == [
        ("Partner A", True, 3, 1), ("Zwei", False, 2, 0), ("Drei", True, 2, 1)]
    # Verbindungstest je Partner: id wählt, unbekannte id -> not_configured
    monkeypatch.setattr(appmod.conncheck, "check_trackmeta",
                        lambda c: {"ok": c is not None, "status": 200 if c else 0, "error": "" if c else "not_configured", "detail": c.client_id if c else ""})
    assert client.post("/api/settings/test/trackmeta", params={"partner": "p3"}).json()["detail"] == "12"
    assert client.post("/api/settings/test/trackmeta").json()["detail"] == "test-client"
    assert client.post("/api/settings/test/trackmeta", params={"partner": "nix"}).json()["error"] == "not_configured"


def test_edl_trackmeta_scan_surfaces_lookup_error(tmp_path, monkeypatch):
    _setup(tmp_path)
    _trackmeta_cfg(monkeypatch)
    monkeypatch.setattr(appmod.trackmeta, "lookup", lambda names, *, client: {
        "ok": False, "error": "rate_limited", "status": 429, "entries": [], "matched": 0, "requests": 1})
    j = client.post("/api/edl/trackmeta-scan", json={"cues": [{"fileName": "a.mp3"}]}).json()
    assert j["ok"] is False and j["error"] == "rate_limited" and j["status"] == 429


def test_db_song_verlag_is_editable_and_listed(tmp_path):
    _setup(tmp_path)
    r = client.post("/api/db/song", json={"fields": {
        "interpret": "Zzq Verlagstest", "titel": "Song", "labelcode": "LC00121", "verlag": "Edition Meisel"}})
    assert r.status_code == 200
    sid = r.json()["id"]
    row = client.get("/api/db/songs", params={"q": "Zzq Verlagstest"}).json()["rows"][0]
    assert row["verlag"] == "Edition Meisel"
    client.post(f"/api/db/song/{sid}", json={"fields": {"verlag": "Budde Music"}})       # Korrektur überschreibt
    row = client.get("/api/db/songs", params={"q": "Zzq Verlagstest"}).json()["rows"][0]
    assert row["verlag"] == "Budde Music"
    assert appmod._suggestion(row)["verlag"] == "Budde Music"                            # Resolver-Merge sieht ihn


def test_index_from_db_carries_kurzname(tmp_path):
    from core.labelmatch import build_index_from_db, catalog_code
    from core.labeldb import connect
    db = tmp_path / "labels.db"
    build_label_db([LabelRecord("10347", "CMG", "CEZ", "Ja", "Frederic Leibovitz Editeur Sarl.", "")], db, "2026-06-30")
    idx = build_index_from_db(connect(db))
    assert catalog_code("CEZ", idx) == "10347"
    assert idx.code2hersteller["10347"] == "Frederic Leibovitz Editeur Sarl."


# --- EDL „Vervollständigen": Batch-Resolve + konfigurierte Quellen ---

def test_settings_view_reports_configured_sources(monkeypatch):
    vals = {"editingtools_api_key": "K", "trackmeta_host": "h", "trackmeta_client_id": "1",
            "trackmeta_client_secret": "", "discogs_token": "T"}
    monkeypatch.setattr(appmod.settings, "get", lambda k, *a, **kw: vals.get(k, ""))
    c = client.get("/api/settings").json()["configured"]
    assert c == {"editingtools": True, "trackmeta": False, "discogs": True, "genius": False,
                 "acoustid": False, "audd": False, "acrcloud": False}


def test_edl_resolve_cues_batches_write_flywheel_and_skip_unidentified(tmp_path, monkeypatch):
    from core import songsdb
    _setup(tmp_path)
    monkeypatch.setattr(appmod, "_edl_resolve_fn", lambda a, t: (
        {"labelname": "Atlantic", "labelcode": "00121", "komponist": "C", "texter": "L", "verlag": "V", "isrc": "X1"}
        if a == "Birdy" else None))
    j = client.post("/api/edl/resolve-cues", json={"cues": [
        {"clip_name": "birdy.wav", "artist": "Birdy", "title": "Skinny Love", "duration_sec": 20},
        {"clip_name": "nix.wav", "artist": "Nobody", "title": "Nothing"},
        {"clip_name": "atmo.wav", "artist": "", "title": "ATMO"}]}).json()
    assert j["resolved"] == 1 and j["count"] == 3
    e = j["entries"]
    assert e[0]["status"] == "resolved_and_written" and e[0]["labelcode"] == "00121" and e[0]["verlag"] == "V"
    assert e[1]["status"] == "unresolved" and e[2]["status"] == "missing_no_resolver"
    hit = songsdb.find(appmod.SONGS_CON, "Birdy", "Skinny Love")
    assert hit["labelcode"] == "LC00121" and not hit["verlag"] and hit["source_type"] == "edl_import"   # LC-Präfix; Netz-Verlag nur Vorschlag


def test_edl_resolve_cues_does_not_store_cue_duration(tmp_path, monkeypatch):
    """„Vervollständigen" schreibt den Song in die songs.db, aber nicht die Dauer dieses Cues — sie käme
    im nächsten Cue-Sheet sonst als Dauer eines ganz anderen Einsatzes wieder."""
    from core import songsdb
    _setup(tmp_path)
    monkeypatch.setattr(appmod, "_edl_resolve_fn", lambda a, t: {"labelname": "Atlantic", "labelcode": "00121",
                                                                  "komponist": "Fremd Werk", "texter": "Fremd"})
    j = client.post("/api/edl/resolve-cues", json={"cues": [
        {"clip_name": "zzq.wav", "artist": "Zzq Neu", "title": "Probe Titel Dauer", "duration_sec": 7}]}).json()
    assert j["entries"][0]["status"] == "resolved_and_written"
    hit = songsdb.find(appmod.SONGS_CON, "Zzq Neu", "Probe Titel Dauer")
    assert hit["labelcode"] == "LC00121" and hit["duration_sec"] is None
    # Netz-Autoren nur als Vorschlag am Eintrag, nicht in der songs.db
    assert not hit["komponist"] and j["entries"][0]["komponist"] == "Fremd Werk"


def test_edl_import_cards_carry_rechterueckrufe_also_after_merge(tmp_path):
    """Jede Karte aus /api/edl/import trägt `rechterueckrufe` zum LC. Füllt das Zusammenfassen
    den LC erst aus einer späteren Karte, kommt der R-Code mit."""
    _setup(tmp_path)
    edl = ("001  AX  AA  C  00:00:00:00 00:00:05:00 00:00:00:00 00:00:05:00\n"
           "* FROM CLIP NAME: Zzq Fremd - Song Mit Code.wav\n"
           "002  AX  AA  C  00:00:10:00 00:00:13:00 00:00:10:00 00:00:13:00\n"
           "* FROM CLIP NAME: Zzq Fremd - Song Mit Code - [Atlantic_LC00121].wav\n"
           "003  AX  AA  C  00:00:20:00 00:00:23:00 00:00:20:00 00:00:23:00\n"
           "* FROM CLIP NAME: Zzq Nichtexistent - Kein Echter Song 9x.wav\n")
    raw = client.post("/api/edl/import", json={"content": edl, "online": False}).json()["cards"]
    assert [c["rechterueckrufe"] for c in raw] == ["", "R6;R9", ""]
    merged = client.post("/api/edl/import", json={"content": edl, "online": False, "merge_identical": True}).json()["cards"]
    assert merged[0]["labelcode"] == "00121" and merged[0]["rechterueckrufe"] == "R6;R9"
    assert merged[1]["rechterueckrufe"] == ""


def test_edl_export_rechterueckruf_column_follows_the_lc(tmp_path):
    """Export: die Spalte 'Rechterückruf' steht nach 'LC' und kommt aus dem LC der Karte, wie sie exportiert wird
    (im Editor geänderter LC, Karte ohne Feld) — nicht aus einem veralteten Kartenwert."""
    import csv, io
    _setup(tmp_path)
    cards = [{"artist": "Zzq", "title": "A", "labelcode": "LC00121", "rechterueckrufe": "", "duration_sec": 4},
             {"artist": "Zzq", "title": "B", "labelcode": "10192", "rechterueckrufe": "R6", "duration_sec": 3}]
    r = client.post("/api/edl/export", json={"cards": cards, "filename": "cue", "fmt": "csv"})
    data = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    i = data[0].index("Rechterückruf")
    assert data[0][i - 1] == "LC" and [row[i] for row in data[1:3]] == ["R6;R9", ""]


def test_edl_resolve_cues_entries_carry_rechterueckrufe(tmp_path, monkeypatch):
    _setup(tmp_path)
    monkeypatch.setattr(appmod, "_edl_resolve_fn", lambda a, t: {"labelname": "Atlantic", "labelcode": "00121"})
    j = client.post("/api/edl/resolve-cues", json={"cues": [
        {"clip_name": "zzq.wav", "artist": "Zzq Neu", "title": "Probe Titel Rechte"}]}).json()
    assert j["entries"][0]["status"] == "resolved_and_written" and j["entries"][0]["rechterueckrufe"] == "R6;R9"


def test_resolve_local_hit_carries_rechterueckrufe(tmp_path):
    """Die Track-Suche zeigt für einen DB-Treffer den R-Code, /api/resolve liefert ihn."""
    from core import songsdb
    _setup(tmp_path)
    songsdb.upsert(appmod.SONGS_CON, interpret="Zzz Local", titel="Rights Song", labelname="Atlantic", labelcode="LC00121")
    appmod.SONGS_CON.commit()
    j = client.post("/api/resolve", json={"artist": "Zzz Local", "title": "Rights Song"}).json()
    assert j["local"] is True and j["best"]["rechterueckrufe"] == "R6;R9"
    assert j["candidates"][0]["rechterueckrufe"] == "R6;R9"


def test_edl_resolve_cues_limits_batch_size(tmp_path):
    _setup(tmp_path)
    assert client.post("/api/edl/resolve-cues", json={"cues": []}).status_code == 422
    too_many = [{"artist": "A", "title": f"T{i}"} for i in range(appmod.EDL_RESOLVE_BATCH + 1)]
    assert client.post("/api/edl/resolve-cues", json={"cues": too_many}).status_code == 422


def test_health_and_settings_expose_sources_and_groups(monkeypatch):
    monkeypatch.setattr(appmod.settings, "get", lambda k, *a, **kw: "K" if k == "editingtools_api_key" else "")
    h = client.get("/api/health").json()
    assert h["sources"]["editingtools"] is True and h["sources"]["trackmeta"] is False
    s = client.get("/api/settings").json()
    assert [g["key"] for g in s["groups"]][0] == "ordner" and all("group" in k for k in s["keys"])


def test_settings_test_endpoint_routes_to_conncheck(monkeypatch):
    monkeypatch.setattr(appmod.settings, "get", lambda k, *a, **kw: {"editingtools_api_key": "K", "discogs_token": "T"}.get(k, ""))
    monkeypatch.setattr(appmod.conncheck, "check_editingtools", lambda key, **kw: {"ok": True, "status": 404, "error": "", "detail": f"key={key}"})
    monkeypatch.setattr(appmod.conncheck, "check_discogs", lambda t, k, s, **kw: {"ok": False, "status": 401, "error": "auth", "detail": ""})
    j = client.post("/api/settings/test/editingtools").json()
    assert j["ok"] and j["service"] == "editingtools" and j["detail"] == "key=K"
    j = client.post("/api/settings/test/discogs").json()
    assert j["ok"] is False and j["error"] == "auth"
    assert client.post("/api/settings/test/trackmeta").json()["error"] == "not_configured"   # Host/ID/Secret fehlen
    assert client.post("/api/settings/test/audd").status_code == 404


def test_db_songs_missing_filter_endpoint(tmp_path):
    from core import songsdb
    _setup(tmp_path)
    songsdb.upsert(appmod.SONGS_CON, interpret="Zzq Gap", titel="No Verlag", labelcode="LC00121")
    songsdb.upsert(appmod.SONGS_CON, interpret="Zzq Full", titel="With Verlag", labelcode="LC00121", verlag="Edition X")
    appmod.SONGS_CON.commit()
    j = client.get("/api/db/songs", params={"q": "Zzq", "missing": "verlag"}).json()
    assert j["total"] == 1 and j["rows"][0]["titel"] == "No Verlag" and j["missing_counts"]["verlag"] == 1
    r = client.get("/api/db/export", params={"fmt": "csv", "q": "Zzq", "missing": "verlag"})
    assert r.status_code == 200 and "No Verlag" in r.text and "With Verlag" not in r.text


def test_load_populates_kurzname_and_library_prefixes(tmp_path):
    from core.labelmatch import catalog_lookup
    db = tmp_path / "labels.db"
    build_label_db(SAMPLE + [LabelRecord("10347", "CMG", "CEZ", "Ja", "Frederic Leibovitz Editeur Sarl.", ""),
                             LabelRecord("99001", "Zzq Test Label", "ZZQ", "Ja", "Zzq GmbH", ""),
                             LabelRecord("01732", "THE EXTREME MUSIC LIBRARY", "", "Ja", "Extreme Musik GmbH", "")], db, "2026-06-30")
    appmod.configure(db)
    assert catalog_lookup("ZZQ", appmod.INDEX) == ("99001", "GVL-Kürzel ZZQ")          # kurzname kommt im App-Index an
    assert appmod.INDEX.hersteller_name                                                  # Hersteller-Index ebenfalls
    if appmod._data_file("library_prefixes.csv").exists():                              # Dev-Layout: Repo-Tabelle
        assert catalog_lookup("XCD", appmod.INDEX) == ("01732", "Library-Kürzel XCD (Extreme Music)")
        assert catalog_lookup("CEZ", appmod.INDEX) == ("10347", "Library-Kürzel CEZ (Cezame)")   # Liste vor kurzname
    else:
        assert catalog_lookup("CEZ", appmod.INDEX) == ("10347", "GVL-Kürzel CEZ")
    _setup(tmp_path)                                                                     # Standard-DB für Folgetests


def test_edl_resolve_cues_flags_tie_and_writes_nothing(tmp_path, monkeypatch):
    """Margin-Gate über den EDL-Endpoint: MB nennt zwei Label, Discogs bestätigt das eine,
    Deezer das andere -> 0.5/0.5. Kein Schreiben in die songs.db, Status 'ambiguous' mit beiden Codes."""
    from core import songsdb
    from core.deezer import DeezerResult
    _setup(tmp_path)
    monkeypatch.setattr(appmod.musicbrainz, "lookup",
                        lambda a, t, **kw: MBLookup(matched=True, score=100, label_names=["Atlantic", "REPUBLIC"],
                                                    discogs_release_ids=["111"]))
    monkeypatch.setattr(appmod.discogs, "lookup_label_codes", lambda rid, **kw: ["00121"])
    monkeypatch.setattr(appmod.deezer, "lookup", lambda a, t, **kw: DeezerResult(label="REPUBLIC"))
    monkeypatch.setattr(appmod.wikidata, "lookup", lambda a, t, **kw: None)
    monkeypatch.setattr(appmod.musicbrainz, "search_work_writers", lambda t, **kw: ("", "", ""))
    monkeypatch.setattr(appmod.itunes, "lookup", lambda a, t, **kw: None)
    j = client.post("/api/edl/resolve-cues", json={"cues": [{"clip_name": "x.wav", "artist": "Zzz Tie", "title": "Two Labels"}]}).json()
    e = j["entries"][0]
    assert e["status"] == "ambiguous" and e["resolved"] is False and j["resolved"] == 0
    assert [c["labelcode"] for c in e["tied"]] == ["00121", "07552"]
    assert songsdb.find(appmod.SONGS_CON, "Zzz Tie", "Two Labels") is None
    # /api/resolve/merge trägt das Gate ebenfalls, damit der DB-Editor keinen Zufallssieger vorschlägt
    m = client.post("/api/resolve/merge", json={"artist": "Zzz Tie", "title": "Two Labels"}).json()
    assert m["ambiguous"] is True and [c["labelcode"] for c in m["tied"]] == ["00121", "07552"]
    assert m["band"] == "review" and m["net"]["labelcode"] == ""


def test_manual_label_create_search_persist_delete(tmp_path):
    """Label-Katalog: händisch angelegte Labels landen in labels.db + labels_manual.json,
    sind sofort suchbar, überleben ein Neu-Laden (PDF-Update baut labels.db neu) und lassen
    sich wieder entfernen. GVL-Zeilen bleiben unantastbar."""
    import json
    db = _setup(tmp_path)
    r = client.post("/api/labels/manual", json={"labelcode": "lc 99001", "label": "Partner A Music",
                                                "hersteller": "Partner A GmbH", "note": "Testnotiz"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["created"]["labelcode"] == "99001" and j["created"]["source"] == "manuell"
    assert client.get("/api/search", params={"q": "99001"}).json()["results"][0]["source"] == "manuell"
    assert any(x["labelcode"] == "99001" for x in client.get("/api/search", params={"q": "partner"}).json()["results"])
    assert appmod.INDEX.code2name["99001"] == "Partner A Music"           # Resolver/Tagger sehen es
    manual = json.loads((tmp_path / "labels_manual.json").read_text(encoding="utf-8"))
    assert manual[0]["label"] == "Partner A Music" and manual[0]["note"] == "Testnotiz"
    # Doppelt anlegen -> 409, Pflichtfelder -> 422
    assert client.post("/api/labels/manual", json={"labelcode": "99001", "label": "Partner A Music"}).status_code == 409
    assert client.post("/api/labels/manual", json={"labelcode": "12", "label": "x"}).status_code == 422
    assert client.post("/api/labels/manual", json={"labelcode": "99003", "label": "  "}).status_code == 422
    # Bestehender GVL-Code mit anderem Namen: erlaubt (1:n), Antwort nennt die bestehenden Zeilen
    j2 = client.post("/api/labels/manual", json={"labelcode": "00121", "label": "Atlantic Sub"}).json()
    assert [x["label"] for x in j2["existing"]] == ["Atlantic"]
    # Neu bauen wie nach einem PDF-Update: labels.db weg, configure() -> manuelle bleiben
    build_label_db(SAMPLE, db, "2026-06-30")
    appmod.configure(db)
    assert client.get("/api/search", params={"q": "99001"}).json()["results"][0]["label"] == "Partner A Music"
    assert [m["labelcode"] for m in client.get("/api/labels/manual").json()["labels"]] == ["00121", "99001"]
    # Entfernen
    assert client.request("DELETE", "/api/labels/manual", json={"labelcode": "99001", "label": "Partner A Music"}).status_code == 200
    assert client.get("/api/search", params={"q": "99001"}).json()["results"] == []
    assert "99001" not in appmod.INDEX.code2name
    assert client.request("DELETE", "/api/labels/manual", json={"labelcode": "00121", "label": "Atlantic"}).status_code == 404


def test_manual_label_check_reports_conflicts(tmp_path):
    """Vor dem Anlegen: Code schon vergeben (fremder / gleicher Hersteller), Name unter anderem
    Code bekannt, exakte Dublette. Der Mensch sieht das, bevor er speichert."""
    _setup(tmp_path)
    # Code vergeben, anderer Name, fremder Hersteller -> warn
    j = client.post("/api/labels/check", json={"labelcode": "00121", "label": "Benztown Branding", "hersteller": "Benztown USA"}).json()
    kinds = {w["kind"]: w for w in j["warnings"]}
    assert kinds["code_taken"]["severity"] == "warn" and kinds["code_taken"]["rows"][0]["label"] == "Atlantic"
    # Code vergeben, anderer Name, GLEICHER Hersteller -> info (Umbenennung / Sub-Label)
    j = client.post("/api/labels/check", json={"labelcode": "00121", "label": "Atlantic Sub", "hersteller": "Warner Music"}).json()
    assert {w["kind"]: w["severity"] for w in j["warnings"]} == {"code_taken": "info"}
    # Name existiert unter anderem Code -> warn, nennt den Code
    j = client.post("/api/labels/check", json={"labelcode": "99001", "label": "Domino British"}).json()
    kinds = {w["kind"]: w for w in j["warnings"]}
    assert kinds["name_known"]["severity"] == "warn" and kinds["name_known"]["rows"][0]["labelcode"] == "10192"
    # exakte Dublette -> error
    j = client.post("/api/labels/check", json={"labelcode": "00121", "label": "atlantic"}).json()
    assert any(w["kind"] == "duplicate" and w["severity"] == "error" for w in j["warnings"])
    # sauber -> keine Warnung
    assert client.post("/api/labels/check", json={"labelcode": "99002", "label": "Ganz Neu Records"}).json()["warnings"] == []
    # create liefert dieselben Warnungen mit, Anlegen bleibt möglich (1:n)
    r = client.post("/api/labels/manual", json={"labelcode": "00121", "label": "Atlantic Sub", "hersteller": "Warner Music"})
    assert r.status_code == 200 and [w["kind"] for w in r.json()["warnings"]] == ["code_taken"]
    client.request("DELETE", "/api/labels/manual", json={"labelcode": "00121", "label": "Atlantic Sub"})


def test_manual_label_delete_without_labels_db_is_503(tmp_path):
    """Ohne labels.db darf DELETE weder eine leere DB anlegen noch die JSON anfassen."""
    import json
    appmod.configure(tmp_path / "missing" / "labels.db")     # existiert nicht -> ready False
    (tmp_path / "missing").mkdir(exist_ok=True)
    (tmp_path / "missing" / "labels_manual.json").write_text(json.dumps([{"labelcode": "99001", "label": "X", "hersteller": "", "note": ""}]), encoding="utf-8")
    r = client.request("DELETE", "/api/labels/manual", json={"labelcode": "99001", "label": "X"})
    assert r.status_code == 503
    assert not (tmp_path / "missing" / "labels.db").exists()
    assert json.loads((tmp_path / "missing" / "labels_manual.json").read_text(encoding="utf-8"))[0]["label"] == "X"
    _setup(tmp_path)


def test_manual_label_check_finds_exact_name_regardless_of_fts_rank(tmp_path):
    """Der Namensvergleich läuft per SQL, nicht über die FTS-Top-10."""
    from core.labeldb import build_label_db
    db = tmp_path / "labels.db"
    many = [LabelRecord(f"{50000+i:05d}", f"Atlantic {i} Records", "", "", "X", "") for i in range(30)]
    build_label_db(SAMPLE + many, db, "2026-06-23")
    appmod.configure(db)
    j = client.post("/api/labels/check", json={"labelcode": "99001", "label": "Atlantic"}).json()
    kinds = {w["kind"]: w for w in j["warnings"]}
    assert "name_known" in kinds and kinds["name_known"]["rows"][0]["labelcode"] == "00121"
    _setup(tmp_path)


def test_manual_labels_json_tolerates_hand_edits(tmp_path):
    """labels_manual.json ist hand-editierbar: Nicht-Dict-Einträge und
    Codes als Zahl dürfen weder den App-Start noch das nächste Anlegen zerlegen."""
    import json
    db = tmp_path / "labels.db"
    build_label_db(SAMPLE, db, "2026-06-23")
    (tmp_path / "labels_manual.json").write_text(json.dumps(
        [{"labelcode": 99001, "label": "IntCode"}, "foo", 7, {"labelcode": "x", "label": "ohne Code"},
         {"labelcode": "99002", "label": "  "}]), encoding="utf-8")
    appmod.configure(db)
    assert appmod.DB_META["ready"] is True
    assert [m["labelcode"] for m in client.get("/api/labels/manual").json()["labels"]] == ["99001"]
    assert appmod.INDEX.code2name["99001"] == "IntCode"
    r = client.post("/api/labels/manual", json={"labelcode": "99003", "label": "Danach"})
    assert r.status_code == 200, r.text
    saved = json.loads((tmp_path / "labels_manual.json").read_text(encoding="utf-8"))
    assert [(m["labelcode"], m["label"]) for m in saved] == [("99001", "IntCode"), ("99003", "Danach")]
    _setup(tmp_path)


def test_reload_keeps_songs_connection_and_index_never_empty(tmp_path, monkeypatch):
    """Reload nach „Label anlegen": laufende Requests halten SONGS_CON —
    die Verbindung bleibt offen, und der Index ist während des Neuaufbaus nie leer."""
    from core import songsdb
    _setup(tmp_path)
    con_before = appmod.SONGS_CON
    seen = []
    orig = appmod.build_index_from_db
    def spy(con):
        seen.append(("during", len(appmod.INDEX.code2name), appmod.DB_META["ready"]))
        return orig(con)
    monkeypatch.setattr(appmod, "build_index_from_db", spy)
    client.post("/api/labels/manual", json={"labelcode": "99005", "label": "Reload Test"})
    assert appmod.SONGS_CON is con_before
    assert songsdb.count(con_before) >= 0                       # alte Referenz weiter benutzbar
    # nicht geleert (>= SAMPLE: die Selbstheilung trägt beim Laden die Zusatz-Codes nach), nicht ready=False
    assert seen and seen[0][1] >= len(SAMPLE) and seen[0][2] is True
    assert appmod.INDEX.code2name["99005"] == "Reload Test"
    client.request("DELETE", "/api/labels/manual", json={"labelcode": "99005", "label": "Reload Test"})


def test_manual_label_delete_matches_unicode_and_code_spelling(tmp_path):
    """DELETE: Umlaut/Groß-Klein per casefold, Code wie beim Anlegen
    vorbehandelt (LC 100 148 -> 100148); JSON und labels.db laufen nicht auseinander."""
    import json
    _setup(tmp_path)
    assert client.post("/api/labels/manual", json={"labelcode": "LC 100 148", "label": "Ärzte Records"}).status_code == 200
    assert client.get("/api/search", params={"q": "100148"}).json()["results"][0]["label"] == "Ärzte Records"
    r = client.request("DELETE", "/api/labels/manual", json={"labelcode": "LC 100 148", "label": "ärzte records"})
    assert r.status_code == 200, r.text
    assert client.get("/api/search", params={"q": "100148"}).json()["results"] == []
    assert json.loads((tmp_path / "labels_manual.json").read_text(encoding="utf-8")) == []
    assert client.request("DELETE", "/api/labels/manual", json={"labelcode": "100148", "label": "Ärzte Records"}).status_code == 404


def test_edl_import_list_endpoint_csv_and_errors(tmp_path):
    """Cue-Liste (CSV/XLSX) statt EDL: Base64 rein, Karten wie aus der EDL
    raus, Spaltenerkennung sichtbar, LC aus der Liste gegen den Master."""
    import base64
    from core import songsdb
    _setup(tmp_path)
    songsdb.upsert(appmod.SONGS_CON, interpret="AURORA", titel="Runaway", labelname="Decca", labelcode="00171")
    appmod.SONGS_CON.commit()
    csv_text = "Interpret;Titel;Dauer;LC\nAURORA;Runaway;1:05;\nNobody;List Song;30;LC 121\nNobody;List Song;30;LC 121\n"
    b64 = base64.b64encode(csv_text.encode("utf-8")).decode()
    j = client.post("/api/edl/import-list", json={"filename": "cues.csv", "content_b64": b64, "fps": 25}).json()
    assert j["rows"] == 3 and j["cues"] == 3 and j["events"] == 3 and j["total_duration_sec"] == 125
    assert j["mapping"] == {"interpret": "Interpret", "titel": "Titel", "labelcode": "LC", "duration": "Dauer"}
    c = j["cards"]
    assert c[0]["status"] == "found_in_db" and c[0]["labelcode"] == "LC00171"     # songs.db trägt LC+Ziffern
    assert c[1]["status"] == "list_lc" and c[1]["labelcode"] == "00121" and c[1]["labelname"] == "Atlantic"
    assert j["summary"] == {"found_in_db": 1, "list_lc": 2}
    # Zusammenfassen wie bei der EDL
    j2 = client.post("/api/edl/import-list", json={"filename": "cues.csv", "content_b64": b64, "merge_identical": True}).json()
    assert j2["cues"] == 2 and j2["total_duration_sec"] == 125
    # Fehlerpfade: Endung, Base64, leer, keine brauchbare Spalte
    assert client.post("/api/edl/import-list", json={"filename": "cues.txt", "content_b64": b64}).status_code == 422
    assert client.post("/api/edl/import-list", json={"filename": "cues.csv", "content_b64": "%%%"}).status_code == 422
    assert client.post("/api/edl/import-list", json={"filename": "cues.csv", "content_b64": ""}).status_code == 422
    b = base64.b64encode(b"Foo;Bar\n1;2\n").decode()
    r = client.post("/api/edl/import-list", json={"filename": "cues.csv", "content_b64": b})
    assert r.status_code == 422 and "Keine Spalte" in r.json()["detail"]
    assert client.post("/api/edl/import-list", json={"filename": "cues.csv"}).status_code == 422
    # Export der Karten funktioniert wie bei der EDL (ausgefuellte Liste)
    r = client.post("/api/edl/export", json={"cards": c, "filename": "cues", "fmt": "csv"})
    assert r.status_code == 200 and "Atlantic" in r.text and "00121" in r.text


def test_manual_label_duplicate_409_names_gvl_only_for_gvl_rows(tmp_path):
    """Ein eigenes Label zum zweiten Mal anlegen -> 409 ohne „(GVL-Master)“, auch wenn der Code im
    Master unter einem anderen Namen steht. Nur ein Duplikat einer Master-Zeile trägt den Zusatz."""
    _setup(tmp_path)
    body = {"labelcode": "10192", "label": "Zzq Eigenes Label"}       # GVL: 10192 = DOMINO British
    assert client.post("/api/labels/manual", json=body).status_code == 200
    r = client.post("/api/labels/manual", json=body)
    assert r.status_code == 409 and "gibt es schon" in r.json()["detail"]
    assert "GVL-Master" not in r.json()["detail"]
    r = client.post("/api/labels/manual", json={"labelcode": "00121", "label": "Atlantic"})
    assert r.status_code == 409 and r.json()["detail"].endswith("(GVL-Master)")


def test_library_list_upload_preview_commit_delete(tmp_path):
    """Listen-Upload in der Label-Datenbank: Vorschau mit Konfliktbericht,
    nichts geschrieben; Übernehmen legt die Datei ab, baut die Nutzer-Tabelle, hängt die Kürzel
    in den Index (gelieferte Tabelle gewinnt); Entfernen räumt wieder auf."""
    import base64
    import json
    from core import librarylists
    _setup(tmp_path)
    csv_text = "Kürzel;Label;LC\nZZA;Zza Records;LC 121\nZZB;Zzb;10192\nZZC;Nix;99999\nZZA;Anders;10192\n"
    b64 = base64.b64encode(csv_text.encode("utf-8")).decode()
    body = {"filename": "Zzq_Liste.csv", "content_b64": b64, "library": "Partner A"}
    j = client.post("/api/labels/lists/preview", json=body).json()
    assert j["rows"] == 4 and j["mapping"]["prefix"] == "Kürzel"
    assert [(k["prefix"], k["labelcode"], k["label_gvl"], k["library"]) for k in j["kept"]] == [("ZZB", "10192", "DOMINO British", "Partner A")]
    assert {e["prefix"]: e["why"] for e in j["excluded"]}["ZZC"] == "Code weder im Master noch im Supplemental"
    assert all("mehrdeutig" in e["why"] for e in j["excluded"] if e["prefix"] == "ZZA")
    assert any(i["prefix"] == "ZZB" and "GVL „DOMINO British“" in i["why"] for i in j["infos"])
    assert not librarylists.user_lists_dir(tmp_path).exists()                     # Vorschau schreibt nichts
    assert "ZZB" not in appmod.INDEX.prefix2code
    # Übernehmen
    j = client.post("/api/labels/lists/commit", json=body).json()
    assert j["stored"].endswith("_Zzq_Liste.csv") and j["user_prefixes"] == 1 and j["attached"] == 1
    assert appmod.INDEX.prefix2code["ZZB"] == {"labelcode": "10192", "library": "Partner A", "prefix": "ZZB", "user": True}
    assert (tmp_path / librarylists.USER_PREFIXES).exists()
    meta = json.loads((tmp_path / librarylists.USER_META).read_text(encoding="utf-8"))
    assert meta["rows"] == 1 and any(e["prefix"] == "ZZC" for e in meta["excluded"])
    lst = client.get("/api/labels/lists").json()
    assert [f["name"] for f in lst["files"]] == [j["stored"]] and lst["attached"] == 1
    # zweite Liste mit demselben Kürzel und anderem Code -> „schon vergeben“ (erste Liste gewinnt)
    b2 = base64.b64encode("Kürzel;Label;LC\nZZB;Other;00121\n".encode("utf-8")).decode()
    j2 = client.post("/api/labels/lists/preview", json={"filename": "zwei.csv", "content_b64": b2}).json()
    assert j2["kept"] == [] and j2["excluded"][0]["why"].startswith("Kürzel schon vergeben: LC10192 DOMINO British (Partner A)")
    # Entfernen
    r = client.request("DELETE", "/api/labels/lists", json={"name": j["stored"]})
    assert r.status_code == 200 and r.json()["attached"] == 0 and "ZZB" not in appmod.INDEX.prefix2code
    assert client.request("DELETE", "/api/labels/lists", json={"name": "../labels.db"}).status_code == 404
    assert (tmp_path / "labels.db").exists()
    # Fehlerpfade
    assert client.post("/api/labels/lists/preview", json={"filename": "x.txt", "content_b64": b64}).status_code == 422
    assert client.post("/api/labels/lists/preview", json={"filename": "x.csv", "content_b64": "%%"}).status_code == 422
    bad = base64.b64encode(b"Foo;Bar\n1;2\n").decode()
    assert "Kürzel und LC" in client.post("/api/labels/lists/preview", json={"filename": "x.csv", "content_b64": bad}).json()["detail"]


def test_library_list_preview_names_library_like_commit(tmp_path):
    """Ohne Library-Namen leiten Vorschau und Übernehmen den Namen aus dem hochgeladenen Dateinamen ab
    (nicht aus dem Temp-Dateinamen)."""
    import base64
    _setup(tmp_path)
    b64 = base64.b64encode("Kürzel;Label;LC\nZZB;Zzb;10192\n".encode("utf-8")).decode()
    body = {"filename": "liblist.csv", "content_b64": b64}
    pj = client.post("/api/labels/lists/preview", json=body).json()
    assert [(k["library"], k["source"]) for k in pj["kept"]] == [("liblist", "liblist.csv")]
    cj = client.post("/api/labels/lists/commit", json=body).json()
    assert [k["library"] for k in cj["kept"]] == ["liblist"]
    assert appmod.INDEX.prefix2code["ZZB"]["library"] == "liblist"


# --- Lokaler Track-Index ------------------------------------------------

def _with_index(tmp_path, rows):
    """labels.db + track_index.db im selben Ordner, dann configure() -> App öffnet den Index."""
    from core import trackindex
    con = trackindex.open_index(trackindex.path_for(tmp_path), create=True)
    for src in trackindex.SOURCES:                    # je Quelle ein Staging-Lauf (finalize nimmt eine Quelle)
        mine = [r for r in rows if r[-2] == src]
        if not mine:
            continue
        trackindex.begin_staging(con)
        trackindex.stage_rows(con, mine)
        trackindex.finalize(con, src, {"dump": f"{src}_test"})
    con.close()
    return _setup(tmp_path)


def test_resolve_uses_track_index_before_net(tmp_path, monkeypatch):
    from core import trackindex
    _with_index(tmp_path, [trackindex.make_row(interpret="Zzq Indexed", titel="Only In Index", labelcode="00121",
                                               label="Atlantic Recs", komponist="A. Writer", texter="B. Lyric",
                                               album="Album X", jahr="2001", source="discogs", source_id="7")])
    monkeypatch.setattr(appmod, "_resolve_net", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Netz gefragt")))
    assert appmod.DB_META["index"]["present"] and appmod.DB_META["index"]["rows"] == 1
    r = client.post("/api/resolve", json={"artist": "zzq indexed", "title": "ONLY IN INDEX"})
    j = r.json()
    assert r.status_code == 200 and j["index"] is True and j["local"] is False
    assert j["band"] == "review" and j["best"]["labelcode"] == "00121" and j["best"]["label"] == "Atlantic"
    assert j["best"]["sources"] == ["index:discogs"] and j["best"]["confidence"] == 0.5
    assert j["composer"] == "A. Writer" and j["lyricist"] == "B. Lyric" and j["album"] == "Album X" and j["date"] == "2001"
    assert any("Track-Index" in n for n in j["notes"])
    assert client.get("/api/health").json()["index"]["rows"] == 1
    st = client.get("/api/index/status").json()
    assert st["present"] and st["by_source"] == {"discogs": 1, "musicbrainz": 0} and st["meta"]["discogs_dump"] == "discogs_test"
    assert st["path"].endswith("track_index.db") and st["size_bytes"] > 0


def test_resolve_offers_index_suggestions_for_partial_title(tmp_path, monkeypatch):
    from core import trackindex
    _with_index(tmp_path, [trackindex.make_row(interpret="Ian Pooley", titel="What's Your Number", labelcode="00121",
                                               label="V2 Recs", source="discogs", source_id="1")])
    from core.resolver import ResolveResult

    class FakeFetcher:
        calls = cache_hits = failures = 0
    monkeypatch.setattr(appmod, "_resolve_net", lambda a, t, i="": (ResolveResult(artist=a, title=t), FakeFetcher()))
    j = client.post("/api/resolve", json={"artist": "Ian Pooley", "title": "Number"}).json()
    assert [s["titel"] for s in j["index_suggestions"]] == ["What's Your Number"]
    assert j["index_suggestions"][0]["labels"] == ["Atlantic"]                # GVL-Schreibweise, nicht Dump


def test_db_songs_unifies_verified_and_index_rows_and_verify_moves_row(tmp_path):
    from core import trackindex
    _with_index(tmp_path, [
        trackindex.make_row(interpret="Zzq Both", titel="Already Verified", labelcode="00121", label="Atl", source="discogs"),
        trackindex.make_row(interpret="Zzq Both", titel="Only Index", labelcode="00121", label="Atl", komponist="K. Omp",
                            source="discogs"),
        trackindex.make_row(interpret="Zzq Both", titel="Only Index", labelcode="00121", label="Atlantic", source="musicbrainz")])
    client.post("/api/db/song", json={"fields": {"interpret": "Zzq Both", "titel": "Already Verified", "labelcode": "LC00121"}})
    j = client.get("/api/db/songs", params={"q": "zzq both"}).json()
    assert [r["titel"] for r in j["rows"]] == ["Already Verified"] and j["rows"][0]["verified"] is True
    assert [r["titel"] for r in j["index_rows"]] == ["Only Index"]            # verifizierte Zeile nicht doppelt
    ix = j["index_rows"][0]
    assert ix["verified"] is False and ix["labelname"] == "Atlantic" and sorted(ix["sources"]) == ["discogs", "musicbrainz"]
    assert ix["labelcode"] == "LC00121"                                       # Schreibweise wie in der songs.db
    assert client.get("/api/db/songs", params={"q": "zzq both", "scope": "verified"}).json()["index_rows"] == []
    v = client.get("/api/db/songs", params={"q": "zzq both", "scope": "index"}).json()
    assert v["rows"] == [] and len(v["index_rows"]) == 2                     # scope index: auch die schon verifizierte
    assert client.get("/api/db/songs", params={"q": ""}).json()["index_rows"] == []   # ohne Text kein Index

    r = client.post("/api/db/verify", json={"interpret": ix["interpret"], "titel": ix["titel"], "labelcode": ix["labelcode"],
                                            "labelname": ix["labelname"], "komponist": ix["komponist"], "texter": ix["texter"],
                                            "album": ix["album"], "verlag": "Zzq Verlag", "isrc": "DEZZQ2600001"})
    assert r.status_code == 200 and r.json()["row"]["verified"] is True and r.json()["row"]["quelle"] == "index"
    j = client.get("/api/db/songs", params={"q": "zzq both"}).json()
    assert sorted(x["titel"] for x in j["rows"]) == ["Already Verified", "Only Index"] and j["index_rows"] == []
    row = next(x for x in j["rows"] if x["titel"] == "Only Index")
    assert row["komponist"] == "K. Omp" and row["labelname"] == "Atlantic" and row["date_verified"]
    assert row["labelcode"] == "LC00121" and row["verlag"] == "Zzq Verlag" and row["isrc"] == "DEZZQ2600001"
    assert client.post("/api/db/verify", json={"interpret": "A", "titel": "B", "labelcode": "99999"}).status_code == 400
    assert client.post("/api/db/verify", json={"interpret": "", "titel": "B"}).status_code == 400


def test_resolve_artist_only_lists_titles_from_db_and_index(tmp_path, monkeypatch):
    from core import trackindex
    _with_index(tmp_path, [trackindex.make_row(interpret="Zzq Solo", titel="Index Song", labelcode="00121", label="A", source="discogs"),
                           trackindex.make_row(interpret="Zzq Solo", titel="Both Song", labelcode="00121", label="A", source="discogs")])
    client.post("/api/db/song", json={"fields": {"interpret": "Zzq Solo", "titel": "Both Song", "labelcode": "LC00121"}})
    monkeypatch.setattr(appmod, "_resolve_net", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Netz gefragt")))
    j = client.post("/api/resolve", json={"artist": "zzq solo", "title": ""}).json()
    assert j["mode"] == "artist_only"
    assert [v["titel"] for v in j["verified"]] == ["Both Song"] and j["verified"][0]["verified"] is True
    assert [i["titel"] for i in j["index"]] == ["Index Song"]                  # Both Song nicht doppelt


def test_resolve_artist_and_album_lists_album_tracks_with_chips(tmp_path, monkeypatch):
    """Album-Suche: Interpret + Album, Titel leer -> Tracks des Albums aus songs.db und Index;
    Album-Chips (albums) für die Interpret-only-Liste. Album ohne Interpret ist keine Suche (400)."""
    from core import trackindex
    _with_index(tmp_path, [
        trackindex.make_row(interpret="Zzq Alb", titel="Track One", labelcode="00121", label="A", album="First LP", source="discogs"),
        trackindex.make_row(interpret="Zzq Alb", titel="Track Two", labelcode="00121", label="A", album="First LP", source="discogs"),
        trackindex.make_row(interpret="Zzq Alb", titel="Other Song", labelcode="00121", label="A", album="Second LP", source="discogs")])
    client.post("/api/db/song", json={"fields": {"interpret": "Zzq Alb", "titel": "Verified Cut", "labelcode": "LC00121",
                                                 "album": "First LP (Deluxe)"}})
    monkeypatch.setattr(appmod, "_resolve_net", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Netz gefragt")))
    j = client.post("/api/resolve", json={"artist": "zzq alb", "title": "", "album": "first lp"}).json()
    assert j["mode"] == "artist_only" and j["album"] == "first lp"
    assert [v["titel"] for v in j["verified"]] == ["Verified Cut"]                 # „First LP (Deluxe)“ zählt zu „first lp“
    assert sorted(i["titel"] for i in j["index"]) == ["Track One", "Track Two"]    # Second LP nicht
    assert j["albums"][0] == {"album": "First LP", "n": 2} and {"album": "Second LP", "n": 1} in j["albums"]
    assert {"album": "First LP (Deluxe)", "n": 1} in j["albums"]                   # verifizierte Alben sind auch Chips
    j = client.post("/api/resolve", json={"artist": "zzq alb", "title": ""}).json()
    assert len(j["verified"]) == 1 and len(j["index"]) == 3 and len(j["albums"]) == 3
    assert client.post("/api/resolve", json={"artist": "", "title": "", "album": "first lp"}).status_code == 400


def test_resolve_title_only_lists_index_before_wikidata(tmp_path, monkeypatch):
    """Titel-only: erst songs.db + Track-Index (Trigram, kein Netz), Wikidata nur wenn beide leer.
    Teilwort trifft, Interpret-Spalte je Zeile, Label in GVL-Schreibweise."""
    from core import trackindex
    _with_index(tmp_path, [
        trackindex.make_row(interpret="Ian Pooley", titel="What's Your Qzxnum", labelcode="00121", label="V2 Recs", source="discogs"),
        trackindex.make_row(interpret="Zzq Verified", titel="Qzxnum Nine", labelcode="00121", label="V2 Recs", source="discogs"),
        trackindex.make_row(interpret="Steven Julien", titel="Qzxnum", labelcode="10192", label="Domino", source="discogs")])
    client.post("/api/db/song", json={"fields": {"interpret": "Zzq Verified", "titel": "Qzxnum Nine", "labelcode": "LC00121"}})
    monkeypatch.setattr(appmod.wikidata, "lookup_items", lambda t, **kw: (_ for _ in ()).throw(AssertionError("Wikidata gefragt")))
    monkeypatch.setattr(appmod, "_resolve_net", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Netz gefragt")))
    j = client.post("/api/resolve", json={"artist": "", "title": "Qzxnum"}).json()
    assert j["mode"] == "title_only" and j["title"] == "Qzxnum"
    assert [v["titel"] for v in j["verified"]] == ["Qzxnum Nine"] and j["verified"][0]["verified"] is True
    assert [(i["interpret"], i["titel"]) for i in j["index"]] == [("Steven Julien", "Qzxnum"), ("Ian Pooley", "What's Your Qzxnum")]
    assert j["index"][0]["labels"] == ["DOMINO British"] and j["index"][0]["labelcodes"] == ["10192"]   # GVL-Schreibweise
    assert j["index_total"] == 3 and j["index_capped"] is False and j["index_present"] is True     # Qzxnum Nine nicht doppelt
    assert j["title_candidates"] == [] and any("Wikidata" in n for n in j["notes"])
    # „mehr zeigen“: offset blättert die Index-Liste, verifizierte Zeilen nur auf Seite 1
    j1 = client.post("/api/resolve", json={"artist": "", "title": "Qzxnum", "limit": 1}).json()
    assert [i["titel"] for i in j1["index"]] == ["Qzxnum"] and j1["index_capped"] is True and j1["index_total"] == 3
    assert j1["index_next"] == 1
    j2 = client.post("/api/resolve", json={"artist": "", "title": "Qzxnum", "limit": 1, "offset": j1["index_next"]}).json()
    assert j2["verified"] == [] and j2["index"] == [] and j2["index_next"] == 2 and j2["index_capped"] is True  # Seite 2 = verifizierte Dublette, bleibt weg
    j3 = client.post("/api/resolve", json={"artist": "", "title": "Qzxnum", "limit": 1, "offset": j2["index_next"]}).json()
    assert [i["titel"] for i in j3["index"]] == ["What's Your Qzxnum"] and j3["index_capped"] is False and j3["index_offset"] == 2
    # „auch bei Wikidata suchen“: net=True fragt Wikidata trotz lokaler Treffer
    from core.wikidata import WikidataItem
    monkeypatch.setattr(appmod.wikidata, "lookup_items",
                        lambda t, **kw: [WikidataItem(performers=["Wiki Artist"], labels=["Atlantic"], item_url="u")])
    j4 = client.post("/api/resolve", json={"artist": "", "title": "Qzxnum", "net": True}).json()
    assert len(j4["index"]) == 2 and j4["title_candidates"][0]["artist"] == "Wiki Artist" and "stats" in j4
    # Nichts lokal -> Wikidata wie bisher
    from core.wikidata import WikidataItem
    monkeypatch.setattr(appmod.wikidata, "lookup_items",
                        lambda t, **kw: [WikidataItem(performers=["Birdy"], labels=["Atlantic"], item_url="u")])
    j = client.post("/api/resolve", json={"artist": "", "title": "Qzx Skinny Love"}).json()
    assert j["verified"] == [] and j["index"] == [] and j["title_candidates"][0]["artist"] == "Birdy"


def test_resolve_without_index_goes_to_net(tmp_path, monkeypatch):
    _setup(tmp_path)
    assert appmod.INDEX_CON is None and appmod.DB_META["index"]["present"] is False
    called = []
    monkeypatch.setattr(appmod, "_resolve_net", lambda a, t, isrc="": (called.append((a, t)) or
                        (appmod.local_result(a, t, {"interpret": a, "titel": t}), None)))
    client.post("/api/resolve", json={"artist": "Nobody", "title": "Nothing"})
    assert called == [("Nobody", "Nothing")]
    assert client.get("/api/index/status").json()["present"] is False


def test_edl_offline_uses_index_but_writes_nothing(tmp_path):
    from core import songsdb, trackindex
    _with_index(tmp_path, [trackindex.make_row(interpret="Zzq Indexed", titel="Only In Index", labelcode="00121",
                                               label="Atlantic", komponist="A. Writer", source="discogs")])
    edl = ("001  AX  AA  C  00:00:00:00 00:00:05:00 00:00:00:00 00:00:05:00\n"
           "* FROM CLIP NAME: Zzq Indexed - Only In Index.mp3\n")
    card = client.post("/api/edl/import", json={"content": edl, "online": False}).json()["cards"][0]
    assert card["status"] == "index_hit" and card["labelcode"] == "00121" and card["labelname"] == "Atlantic"
    assert card["komponist"] == "A. Writer" and card.get("index_hit") is True
    assert songsdb.find(appmod.SONGS_CON, "Zzq Indexed", "Only In Index") is None   # nichts in die songs.db geschrieben
    # Vervollständigen-Schritt: derselbe Vorschlag, ebenfalls ohne Schreiben
    r = client.post("/api/edl/resolve-cues", json={"cues": [{"artist": "Zzq Indexed", "title": "Only In Index"}]}).json()
    assert r["entries"][0]["status"] == "index_hit" and r["entries"][0]["labelcode"] == "00121" and r["resolved"] == 0
    assert songsdb.find(appmod.SONGS_CON, "Zzq Indexed", "Only In Index") is None
    # songs.db gewinnt vor dem Index
    songsdb.upsert(appmod.SONGS_CON, interpret="Zzq Indexed", titel="Only In Index", labelname="Foo Bar", labelcode="0309")
    appmod.SONGS_CON.commit()
    card = client.post("/api/edl/import", json={"content": edl, "online": False}).json()["cards"][0]
    assert card["status"] == "found_in_db" and normalize_labelcode(card["labelcode"]) == "00309"


def test_edl_offline_index_tie_gives_candidates(tmp_path):
    from core import trackindex
    _with_index(tmp_path, [trackindex.make_row(interpret="Tie", titel="Song", labelcode="00121", source="discogs"),
                           trackindex.make_row(interpret="Tie", titel="Song", labelcode="10192", source="discogs")])
    edl = ("001  AX  AA  C  00:00:00:00 00:00:05:00 00:00:00:00 00:00:05:00\n* FROM CLIP NAME: Tie - Song.mp3\n")
    card = client.post("/api/edl/import", json={"content": edl, "online": False}).json()["cards"][0]
    assert card["status"] == "ambiguous" and card["labelcode"] == "" and {t["labelcode"] for t in card["tied"]} == {"00121", "10192"}


def test_resolve_index_tie_goes_to_net_and_fills_writers(tmp_path, monkeypatch):
    """Gleichstand im Index ersetzt das Netz NICHT:
    der volle Resolver läuft, Komponist/Texter aus dem Index werden nur nachgefüllt."""
    from core import trackindex
    _with_index(tmp_path, [trackindex.make_row(interpret="Tie", titel="Song", labelcode="00121", komponist="Index Comp",
                                               source="discogs"),
                           trackindex.make_row(interpret="Tie", titel="Song", labelcode="10192", source="discogs")])
    called = []

    def fake_net(a, t, isrc=""):
        called.append((a, t))
        r = appmod.local_result(a, t, {"interpret": a, "titel": t, "labelcode": "00121", "labelname": "Atlantic"})
        r.notes = []
        return r, None

    monkeypatch.setattr(appmod, "_resolve_net", fake_net)
    j = client.post("/api/resolve", json={"artist": "Tie", "title": "Song"}).json()
    assert called == [("Tie", "Song")] and j["index"] is False
    assert j["composer"] == "Index Comp"
    assert any("Track-Index uneindeutig" in n for n in j["notes"])


def test_intake_card_carries_index_flag(tmp_path, monkeypatch):
    from core import intake, trackindex
    _with_index(tmp_path, [trackindex.make_row(interpret="Zzq Indexed", titel="Only In Index", labelcode="00121", source="discogs")])
    mp3 = tmp_path / "Zzq Indexed - Only In Index.mp3"
    mp3.write_bytes(b"ID3" + b"\x00" * 64)
    card = intake.resolve_card(str(mp3), "Zzq Indexed", "Only In Index",
                               resolve_fn=lambda a, t, i="": appmod._resolve_one(a, t, i)[0],
                               label_lookup=appmod._label_lookup)
    assert card["index"] is True and card["band"] == "review" and card["best"]["labelcode"] == "00121"


def test_locked_track_index_does_not_break_startup(tmp_path, monkeypatch, capsys):
    """build_track_index.py compact/titles hält die Datei exklusiv: die App startet dann ohne Index statt mit
    OperationalError abzubrechen."""
    import sqlite3 as _sq
    from core import trackindex
    _with_index(tmp_path, [trackindex.make_row(interpret="Zzq Lock", titel="T", labelcode="00121", source="discogs")])
    appmod.INDEX_CON.close(); appmod.INDEX_CON = None
    monkeypatch.setattr(trackindex, "open_index",
                        lambda p, **k: (_ for _ in ()).throw(_sq.OperationalError("database is locked")))
    appmod._load_track_index()
    assert appmod.INDEX_CON is None and appmod.DB_META["index"]["present"] is False
    assert "database is locked" in capsys.readouterr().out


def test_library_file_name_in_track_search_and_tagger(tmp_path, monkeypatch):
    """Library-Dateiname als Titel („BIB213_3“) in der Track-Suche und als Datei im Tagger: verifizierte Zeile der
    Lieferung mit den Fakten der Datei (LC des Sub-Katalogs), kein Netz (Lieferung Cezame)."""
    from core import songsdb, tags
    _setup(tmp_path)
    songsdb.import_library_tracks(appmod.SONGS_CON, [
        {"file": "CEV3031_17.mp3", "interpret": "Domaine Public", "titel": "Silent Night", "labelname": "CMG", "labelcode": "LC10347",
         "album": "The Christmas Album", "catno": "CEV3031", "sublibrary": "Cézame", "komponist": "Trad."},
        {"file": "BIB213_3.mp3", "interpret": "Domaine Public", "titel": "Silent Night", "labelname": "Bibliotheque Music",
         "labelcode": "LC50141", "album": "A Big Band Christmas", "catno": "BIB213", "sublibrary": "Bibliotheque Music"}],
        library="cezame", date_verified="2026-09-20")
    monkeypatch.setattr(appmod, "_resolve_net", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Netz gefragt")))
    monkeypatch.setattr(appmod.wikidata, "lookup_items", lambda t, **kw: (_ for _ in ()).throw(AssertionError("Wikidata gefragt")))
    j = client.post("/api/resolve", json={"artist": "", "title": "bib213_3.wav"}).json()
    assert j["matched_by"] == "file" and j["local"] is True and j["artist"] == "Domaine Public" and j["title"] == "Silent Night"
    assert j["best"]["labelcode"] == "50141" and j["best"]["label"] == "Bibliotheque Music" and j["album"] == "A Big Band Christmas"
    assert j["sublibrary"] == "Bibliotheque Music" and j["catno"] == "BIB213" and any("Library-Datei BIB213_3.mp3" in n for n in j["notes"])
    assert client.post("/api/resolve", json={"artist": "", "title": "CEV3031_17"}).json()["best"]["labelcode"] == "10347"
    # Tagger: Datei ohne Tags, Name nur Katalognummer -> Scan identifiziert, Resolve nimmt die Datei-Fakten
    folder = tmp_path / "in"; folder.mkdir()
    import shutil; shutil.copy(Path(__file__).parent / "fixtures" / "silence.flac", folder / "BIB213_3.flac")
    sc = client.post("/api/intake/scan", json={"folder": str(folder)}).json()
    f = sc["files"][0]
    assert f["identified"] is True and f["artist"] == "Domaine Public" and f["title"] == "Silent Night"
    assert f["library_file"]["sublibrary"] == "Bibliotheque Music" and f["library_file"]["labelcode"] == "LC50141"
    c = client.post("/api/intake/resolve", json={"path": f["path"], "artist": f["artist"], "title": f["title"]}).json()
    assert c["best"]["labelcode"] == "50141" and c["matched"] is True and any("Library-Datei" in n for n in c["notes"])


def test_tagger_page_has_target_gate_and_drop_zone():
    """Tagger: Zielordner-Hinweis mit Knopf vor der Drop-Zone, Scan-Leiste erst nach Ordnerwahl."""
    html = client.get("/").text
    for i in ("tprereq", "ttargetpick", "tokline", "tdrop", "tpick", "trecent", "tform", "tother"):
        assert f'id="{i}"' in html, i
    assert "Ordner hierher ziehen" in html and "Zuerst den Zielordner festlegen" in html
    assert 'lc_verified_dir' in html                                   # Gate liest den Pfad aus /api/settings


def test_cue_sheet_page_has_ampel_summary():
    """Cue-Sheet-Ampel: Zähler verifiziert / Vorschläge / offen als Filter, Fortschrittsbalken, Export rechts."""
    html = client.get("/").text
    assert "function edlAmpel(" in html and "asum" in html and "abar" in html
    assert "Alles verifiziert — bereit für die Abgabe" in html


def test_track_db_page_has_filter_bar():
    """Track-Datenbank-Filterleiste: Suchen als Hauptverb, Segmentschalter + Lücken-Gruppe, Verwaltung ghost."""
    html = client.get("/").text
    assert 'id="dbgo"' in html and 'id="dbfbar"' in html
    assert 'class="btn ghost sm" id="dbnew"' in html                    # rot bleibt dem Suchen vorbehalten
    assert "Sub-Katalog oder LC" in html


def test_home_page_is_the_entry_tab():
    """Startseite: eigener Tab „Start“ vor der Track-Suche, drei Kacheln, Datenbestand, Zuletzt."""
    html = client.get("/").text
    assert 'id="tool-home"' in html and 'data-tool="home" aria-current="true"' in html
    assert 'id="hometiles"' in html and 'id="homehealth"' in html and 'id="homerecent"' in html
    assert "function renderHome(" in html


def test_onboarding_is_one_hint_with_button_and_grey_footer():
    """Onboarding: kein roter Pflicht-Kasten, ein Hinweis mit Knopf (schreibt lc_verified_dir),
    Gewähr als graue Fußzeile; Gewähr-Balken der Einstellungen steht am Ende; prefers-reduced-motion."""
    html = client.get("/").text
    assert "ob-pflicht" not in html and "Wichtig — keine Gewähr" not in html
    assert 'id="obpick"' in html and 'id="obpickpath"' in html and 'class="ob-foot"' in html
    assert html.index('class="ob-foot"') > html.index('id="obclose"')
    sec = html[html.index('id="tool-settings"'):html.index('id="oboverlay"')]
    assert sec.index('class="disclaimer"') > sec.index('id="settings-msg"')
    assert "prefers-reduced-motion" in html


def test_tagger_drop_zone_accepts_path_from_app_shell():
    """App-Hülle (run_cuecore.attach_drop): der Pfad eines Drops kommt aus Python über window.cuecoreDropPath;
    im Browser bleibt der Hinweis, in der Hülle schweigt der JS-Handler (window.pywebview)."""
    html = client.get("/").text
    assert "window.cuecoreDropPath = function(p, isDir)" in html
    assert "if(window.pywebview){" in html


def test_settings_default_path_follows_platform_data_dir():
    """settings.json liegt im Datenordner der Plattform (Windows %LOCALAPPDATA%\\CueCore), nicht fest im Mac-Pfad."""
    from core import settings, trackindex
    assert settings.DEFAULT_PATH == trackindex.default_data_dir() / "settings.json"


def test_cue_sheet_form_accepts_file_drop():
    """Schnittliste/Cue-Liste per Drop auf die Lade-Leiste — landet im Datei-Feld und wird eingelesen."""
    html = client.get("/").text
    assert "ef.addEventListener('drop'" in html and "$('#efile').files=dt.files" in html
    assert "Oder die Datei hierher ziehen" in html


def test_cue_sheet_import_does_not_reference_submit_scope():
    """Regression: runEdlImport griff auf `file` aus dem Submit-Handler zu — ReferenceError nach dem
    Rendern, der catch leerte die fertige Tabelle (jeder Import zeigte „Import fehlgeschlagen“)."""
    html = client.get("/").text
    body = html[html.index("async function runEdlImport"):html.index("const _fileB64=")]
    assert "file.name" not in body and "edlState.filename" in body


def test_gvl_paths_in_bundle_live_in_data_dir(tmp_path, monkeypatch):
    """Gebündelte App: PDF-Update arbeitet auf <Datenordner>/gvl/ (aus dem Bundle geseedet), nie im Bundle —
    PROJECT_DIR zeigt dort ins Leere."""
    import sys
    import app as appmod
    bundle = tmp_path / "bundle"; (bundle / "data").mkdir(parents=True)
    (bundle / "data" / "GVL_Labelcodes_synced.csv").write_text("labelcode,label\n00171,Decca\n", encoding="utf-8")
    (bundle / "data" / "GVL_Labelcodes_synced.meta.json").write_text('{"source_version":"x"}', encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(appmod, "_bundle_base", lambda: bundle)
    monkeypatch.setattr(appmod, "DB_PATH", tmp_path / "userdata" / "labels.db")
    csv_path, data_dir, docs_dir = appmod._gvl_paths()
    assert csv_path == tmp_path / "userdata" / "gvl" / "GVL_Labelcodes_synced.csv" and csv_path.exists()
    assert data_dir == csv_path.parent and docs_dir == data_dir / "docs"
    assert (data_dir / "GVL_Labelcodes_synced.meta.json").exists()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert appmod._gvl_paths()[0] == appmod.PROJECT_DIR / "data" / "GVL_Labelcodes_synced.csv"


def test_intake_apply_rolls_back_tags_when_move_fails(tmp_path, monkeypatch):
    """Tags geschrieben, Verschieben scheitert (Volume voll, Rechte) -> Datei nicht halb verändert
    liegen lassen: Tag-Schritt zurückrollen, 500 mit Grund statt stummem Teil-Erfolg."""
    _setup(tmp_path)
    monkeypatch.setattr(appmod.settings, "SETTINGS_PATH", tmp_path / "settings.json")
    appmod.settings.set_paths({"lc_verified_dir": str(tmp_path / "LC verified")})
    src = tmp_path / "src.mp3"
    src.write_bytes(b"")
    tags.write_tags(src, {"artist": "Old", "title": "Old"})
    before = dict(tags.read_tags(src))
    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(appmod.tagging.shutil, "move", boom)
    r = client.post("/api/intake/apply", json={"path": str(src), "artist": "Alanis", "title": "Thank You",
                                              "labelcode": "00121", "confirm": True})
    assert r.status_code == 500 and "Verschieben fehlgeschlagen" in r.json()["detail"], r.text
    assert "zurückgesetzt" in r.json()["detail"]
    assert src.exists() and dict(tags.read_tags(src)) == before             # Tags wieder wie vorher, kein LC


def test_unhandled_error_is_json_with_detail(monkeypatch):
    """Globaler Fehlerhandler: 500 als JSON {"detail": "Typ: Text"} statt text/plain — die UI zeigt das Detail."""
    import app as appmod
    def boom(*a, **k):
        raise RuntimeError("kaputt")
    monkeypatch.setattr(appmod.songsdb, "count", boom)
    from fastapi.testclient import TestClient
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/db/song", json={"fields": {"interpret": "X", "titel": "Y"}})
    assert r.status_code == 500 and r.json()["detail"] == "RuntimeError: kaputt"


def test_exports_go_through_save_download_helper():
    """Cue-Sheet- und Datenbank-Export laufen über saveDownload (Hülle: nativer Sichern-Dialog, Browser: Download)."""
    html = client.get("/").text
    assert "async function saveDownload(blob, name)" in html
    assert html.count("saveDownload(") >= 3 and "api.save_bytes" in html
    assert 'id="dbmsg"' in html


def test_fonts_are_bundled_not_fetched_from_google():
    """Google Fonts im <head> würden offline das Rendern blockieren; die Schriften sind mitgeliefert."""
    html = client.get("/").text
    assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html
    assert '<link href="/web/fonts/fonts.css" rel="stylesheet">' in html
    css = client.get("/web/fonts/fonts.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "Saira Condensed" in css.text and "Inter" in css.text and "https://" not in css.text
    for line in css.text.splitlines():
        if "url(" in line:
            name = line.split("url(/web/fonts/")[1].split(")")[0]
            r = client.get(f"/web/fonts/{name}")
            assert r.status_code == 200 and r.headers["content-type"] == "font/woff2", name
    assert client.get("/web/fonts/..%2Findex.html").status_code == 404
    assert client.get("/web/fonts/nope.woff2").status_code == 404


def test_cue_sheet_has_accept_button_and_net_scope():
    """Haken „Vorschlag übernehmen" je gelber Zeile (gleiche Vorschläge mit), Vervollständigen
    sucht auch Dateinamen-LC- und Gleichstand-Cues im Netz und zeigt Fortschritt/Ergebnis."""
    html = client.get("/").text
    assert "async function acceptCue(c)" in html and "iconBtn('check', ()=>t('Vorschlag übernehmen" in html   # Text als Funktion (tFn)
    assert "function edlNetCandidates()" in html and "'filename_lc','ambiguous'" in html
    assert "Suche {n} Cue(s) mit Interpret + Titel im Netz …" in html and "Suche läuft …" in html
    for st in ("filename_lc", "sibling_hit", "confirmed"):
        assert st + ":" in html


def test_edl_resolve_fn_returns_authors_without_label(monkeypatch):
    """RY X „Hurt": MusicBrainz kennt die Autoren (Cover von Trent Reznor), aber kein Label — die Autoren
    gehören trotzdem auf die Karte."""
    import app as appmod
    from core.resolver import ResolveResult
    res = ResolveResult(artist="RY X", title="Hurt", matched=True)
    res.composer = res.lyricist = "Trent Reznor"
    monkeypatch.setattr(appmod, "_resolve_one", lambda a, t: (res, None))
    out = appmod._edl_resolve_fn("RY X", "Hurt")
    assert out and out["komponist"] == "Trent Reznor" and not out.get("labelcode")
    empty = ResolveResult(artist="X", title="Y", matched=False)
    monkeypatch.setattr(appmod, "_resolve_one", lambda a, t: (empty, None))
    assert appmod._edl_resolve_fn("X", "Y") is None


def test_resolve_cues_reports_authors_only(monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod, "_edl_resolve_fn", lambda a, t: {"komponist": "Trent Reznor", "texter": "Trent Reznor", "verlag": "", "isrc": ""})
    r = client.post("/api/edl/resolve-cues", json={"cues": [{"artist": "RY X", "title": "Hurt"}]})
    e = r.json()["entries"][0]
    assert r.status_code == 200 and e["status"] == "authors_only" and e["komponist"] == "Trent Reznor" and not e["resolved"]


def test_fill_authors_endpoint_suggests_but_does_not_write_db(tmp_path, monkeypatch):
    """Cue mit LC aus der songs.db, aber ohne Komponist: Autoren aus dem Netz kommen als Vorschlag zurück, die
    songs.db-Zeile bleibt unverändert. Eine einzige Quelle kann die Autoren eines gleichnamigen fremden Werks
    liefern; gespeichert wird deshalb erst, wenn ein Mensch die Zeile im Editor speichert.

    Erfundene Namen mit Absicht: der ausgelieferte Seed (data/songs.db) trägt bekannte Interpreten, und ein
    solcher Name hier kollidierte mit der Zeile aus dem Seed (Komponist dort schon gefüllt und damit geschützt)."""
    import app as appmod
    from core import songsdb
    from core.resolver import ResolveResult
    _setup(tmp_path)
    ART, TIT = "Testfall Nordwand", "Lied ohne Autoren"
    songsdb.upsert(appmod.SONGS_CON, interpret=ART, titel=TIT,
                   labelname="NETTWERK PRODUCTIONS", labelcode="LC11269", quelle="tagger")
    appmod.SONGS_CON.commit()
    res = ResolveResult(artist=ART, title=TIT, matched=True)
    res.composer, res.lyricist, res.publisher = "Billy Raffoul; Cameron Neal", "Billy Raffoul", "Sony/ATV"
    res.author_source_url = "https://genius.com/x"
    # fill-authors muss IMMER das Netz fragen (_resolve_net), nie den DB-Kurzschluss (_resolve_one) — sonst
    # kommt genau die Zeile ohne Autoren zurück, derentwegen wir hier sind.
    monkeypatch.setattr(appmod, "_resolve_net", lambda a, t, isrc="": (res, None))
    monkeypatch.setattr(appmod, "_resolve_one", lambda a, t: (_ for _ in ()).throw(AssertionError("Kurzschluss")))
    r = client.post("/api/edl/fill-authors", json={"cues": [{"artist": ART, "title": TIT}, {"artist": "", "title": "x"}]})
    assert r.status_code == 200 and r.json()["found"] == 1
    e = r.json()["entries"][0]
    assert e["source"] == "genius" and e["source_url"] == "https://genius.com/x"
    assert e["komponist"] == "Billy Raffoul; Cameron Neal" and e["suggestion"] is True
    row = songsdb.find(appmod.SONGS_CON, ART, TIT)
    assert not row["komponist"] and not row["texter"] and row["labelcode"] == "LC11269"


def test_cue_editor_uebernimmt_die_vorschlaege_aus_der_zeile():
    """Was in der Übersicht als Vorschlag steht (Komponist aus dem Dateinamen), muss im Anlege-Dialog
    drinstehen — und bei Library-Tracks ohne Interpret tritt der Komponist als Interpret ein
    (GEMA-Regel). Kursiv markiert, damit Vorschlag und geprüfter Wert unterscheidbar bleiben.
    Dazu der Dateiname oben, auswählbar und mit Kopier-Knopf."""
    html = client.get("/").text
    assert "const chint=(card.composer_hint||'').trim();" in html
    assert "interpret:card.artist||(vorschlag.interpret ? card.komponist||chint : '')" in html   # nur Library
    assert "komponist:card.komponist||chint" in html
    assert "i.classList.add('sugg')" in html and ".dbedit .inp.sugg{" in html
    assert "navigator.clipboard.writeText(card.clip_name)" in html
    assert "Kursive Felder sind Vorschläge aus dem Dateinamen" in html


def test_cue_sheet_ui_has_tie_picker_and_author_fill():
    html = client.get("/").text
    assert "function openTiePicker(c)" in html and "async function chooseTie(c, cand)" in html
    assert "/api/edl/fill-authors" in html and "authors_only" in html
    assert "library_name:" in html and "composer_hint" in html


def _setup_split_codes(tmp_path):
    """Master mit dem Code der ersten Zifferngruppe (00024) UND dem richtigen (24584): genau die Falle bei 'LC 24 584'."""
    db = tmp_path / "labels.db"
    build_label_db([LabelRecord("00024", "ROYALBUNKER", "", "", "x", ""),
                    LabelRecord("24584", "MOM & POP", "", "", "Sony Music", "R12"),
                    LabelRecord("00100", "LUMAS RECORDS", "", "", "y", ""),
                    LabelRecord("100148", "Louve", "", "", "z", "")], db, "2026-10-12")
    appmod.configure(db)


def test_intake_plan_lc_with_spaces_is_not_cut_to_first_group(tmp_path):
    """'LC 24 584' im Tagger darf nicht zu [ROYALBUNKER_LC00024] mit can_apply=true werden."""
    _setup_split_codes(tmp_path)
    src = tmp_path / "x.mp3"
    src.write_bytes(b"")
    j = client.post("/api/intake/plan", json={"path": str(src), "artist": "Zzq", "title": "Probe",
                                              "labelcode": "LC 24 584"}).json()
    assert j["label"] == "MOM & POP" and j["lc"] == "LC24584" and j["rechterueckrufe"] == "R12"
    assert j["plan"]["proposed_name"] == "Zzq - Probe - [MOM & POP_LC24584].mp3"


def test_import_list_lc_with_spaces_is_not_cut_to_first_group(tmp_path):
    """Cue-Liste mit 'LC 24 584' / 'LC 100 148' darf nicht grün als 'LC aus Liste' mit dem falschen Label stehen."""
    import base64
    _setup_split_codes(tmp_path)
    csv_text = "Interpret;Titel;Dauer;LC\nZzq Liste;Leerzeichen;0:10;LC 24 584\nZzq Liste;Sechsstellig;0:20;LC 100 148\n"
    b64 = base64.b64encode(csv_text.encode("utf-8")).decode()
    c = client.post("/api/edl/import-list", json={"filename": "cues.csv", "content_b64": b64, "fps": 25}).json()["cards"]
    assert (c[0]["labelcode"], c[0]["labelname"]) == ("24584", "MOM & POP")
    assert (c[1]["labelcode"], c[1]["labelname"]) == ("100148", "Louve")


def test_resolve_non_latin_title_does_not_hit_foreign_verified_song(tmp_path, monkeypatch):
    """Billie Eilish + Katakana-Titel darf nicht als 'lokale DB (verifiziert)', AUTO, conf 1.0 mit dem
    Titel '!!!!!!!' zurückkommen. Ohne Treffer muss der Resolver weitergehen (hier: Netz-Attrappe)."""
    from core import songsdb
    from core.resolver import ResolveResult
    _setup(tmp_path)
    songsdb.upsert(appmod.SONGS_CON, interpret="Billie Eilish", titel="!!!!!!!", labelname="Interscope",
                   labelcode="LC06406")
    appmod.SONGS_CON.commit()
    monkeypatch.setattr(appmod, "_resolve_index", lambda a, t: None)
    monkeypatch.setattr(appmod, "_resolve_net", lambda a, t, isrc="": (ResolveResult(artist=a, title=t), None))
    for title in ("ハッピアー・ザン・エヴァー", "???"):
        res, _ = appmod._resolve_one("Billie Eilish", title)
        assert res.title == title and not res.candidates, title
    res, fetcher = appmod._resolve_one("Billie Eilish", "!!!!!!!")
    assert fetcher is None and res.candidates[0].labelcode == "06406"     # derselbe Wortlaut trifft weiter


def test_intake_resolve_library_file_needs_same_title_not_just_same_empty_key(tmp_path, monkeypatch):
    """Der Library-Treffer zur Datei gilt nicht schon bei gleichem norm()-Schlüssel (bei Titeln ohne a-z/0-9
    wäre das jeder Titel des Interpreten)."""
    from core.resolver import ResolveResult
    _setup(tmp_path)
    src = tmp_path / "LIB_1_1.mp3"
    src.write_bytes(b"")
    fhit = {"interpret": "Zzq Lib", "titel": "!!!", "labelcode": "LC00121", "labelname": "Atlantic",
            "file": "LIB_1_1.mp3", "library": "lib"}
    monkeypatch.setattr(appmod, "_library_file", lambda name: dict(fhit))
    called = []
    monkeypatch.setattr(appmod, "_resolve_one",
                        lambda a, t, i="": (called.append((a, t)) or ResolveResult(artist=a, title=t), None))
    client.post("/api/intake/resolve", json={"path": str(src), "artist": "Zzq Lib", "title": "Счастье"})
    assert called == [("Zzq Lib", "Счастье")]                  # eigener Resolve statt fremder Library-Zeile
    called.clear()
    client.post("/api/intake/resolve", json={"path": str(src), "artist": "Zzq Lib", "title": "!!!"})
    assert called == []                                          # derselbe Titel: Library-Zeile gilt


# --- Labelcode aus DB-Editor, „Neuer Eintrag“, Track-Suche, Cue-Editor ---

def _db_row(q):
    return client.get("/api/db/songs", params={"q": q}).json()["rows"]


def test_db_song_create_normalizes_code_and_takes_gvl_label_name(tmp_path):
    _setup(tmp_path)
    r = client.post("/api/db/song", json={"fields": {"interpret": "Zzq Neu Eins", "titel": "Mit Code 121",
                                                      "labelcode": "121", "labelname": ""}})
    assert r.status_code == 200
    assert r.json()["fields"]["labelcode"] == "LC00121" and r.json()["fields"]["labelname"] == "Atlantic"
    row = _db_row("Zzq Neu Eins")[0]
    assert (row["labelcode"], row["labelname"]) == ("LC00121", "Atlantic")
    client.post("/api/db/song", json={"fields": {"interpret": "Zzq Neu Zwei", "titel": "Gruppen",
                                                 "labelcode": "LC 10 192", "labelname": "domino records"}})
    row = _db_row("Zzq Neu Zwei")[0]
    assert (row["labelcode"], row["labelname"]) == ("LC10192", "DOMINO British")   # GVL-Schreibweise


def test_db_song_create_rejects_code_outside_master(tmp_path):
    _setup(tmp_path)
    r = client.post("/api/db/song", json={"fields": {"interpret": "Zzq Neu Drei", "titel": "Unbekannter Code",
                                                      "labelcode": "99999"}})
    assert r.status_code == 400 and "nicht im GVL-Master" in r.json()["detail"]
    assert _db_row("Zzq Neu Drei") == []                                  # nichts still als verifiziert gespeichert
    assert client.post("/api/db/song", json={"fields": {"interpret": "Zzq Neu Drei", "titel": "Kaputt",
                                                         "labelcode": "LC-abc"}}).status_code == 400


def test_db_song_update_normalizes_and_checks_only_a_changed_code(tmp_path):
    from core import songsdb
    _setup(tmp_path)
    sid = client.post("/api/db/song", json={"fields": {"interpret": "Zzq Upd", "titel": "Song"}}).json()["id"]
    r = client.post(f"/api/db/song/{sid}", json={"fields": {"labelcode": "LC 00121", "labelname": "x"}})
    assert r.status_code == 200 and r.json()["fields"]["labelcode"] == "LC00121"
    assert (_db_row("Zzq Upd")[0]["labelcode"], _db_row("Zzq Upd")[0]["labelname"]) == ("LC00121", "Atlantic")
    assert client.post(f"/api/db/song/{sid}", json={"fields": {"labelcode": "99999"}}).status_code == 400
    assert _db_row("Zzq Upd")[0]["labelcode"] == "LC00121"
    # Alt-Zeile mit Code außerhalb des Masters: Komponist nachtragen bleibt möglich (Code unverändert)
    appmod.SONGS_CON.execute("INSERT INTO songs (interpret, titel, labelcode, labelname, nkey) VALUES "
                             "('Zzq Alt', 'Zeile', '99999', '', ?)", (songsdb._nkey("Zzq Alt", "Zeile"),))
    appmod.SONGS_CON.commit()
    old = _db_row("Zzq Alt")[0]
    r = client.post(f"/api/db/song/{old['id']}", json={"fields": {"labelcode": "99999", "komponist": "K. Omponist"}})
    assert r.status_code == 200 and _db_row("Zzq Alt")[0]["komponist"] == "K. Omponist"


def test_db_verify_accepts_short_code_and_stores_lc_digits(tmp_path):
    _setup(tmp_path)
    r = client.post("/api/db/verify", json={"interpret": "Zzq Verif", "titel": "Kurz", "labelcode": "121"})
    assert r.status_code == 200 and r.json()["row"]["labelcode"] == "LC00121"
    assert r.json()["row"]["labelname"] == "Atlantic"


def test_db_export_writes_every_code_as_lc_digits(tmp_path):
    import csv as _csv
    import io
    _setup(tmp_path)
    for i, code in enumerate(("121", "LC 10 192", "00309")):
        client.post("/api/db/song", json={"fields": {"interpret": "Zzq Export", "titel": f"T{i}", "labelcode": code}})
    r = client.get("/api/db/export", params={"fmt": "csv", "q": "Zzq Export"})
    rows = list(_csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert sorted(x["LC"] for x in rows) == ["LC00121", "LC00309", "LC10192"]


def test_cue_and_db_editor_take_saved_fields_from_the_response():
    """Cue-Editor und DB-Editor übernehmen nach dem Speichern die normalisierten Felder aus der Antwort
    (LC00121, GVL-Name) in Karte bzw. Tabellenzeile, nicht die Rohwerte aus den Eingabefeldern ('121')."""
    html = client.get("/").text
    assert "const sv=Object.assign({}, fields, j.fields||{});" in html
    assert "labelname:sv.labelname, labelcode:sv.labelcode" in html
    assert "else Object.assign(r, j.fields||fields);" in html


def test_intake_plan_outside_master_shows_no_broken_target_name(tmp_path):
    """Unter 'Neuer Name' darf nicht 'A - B - [_LC99999].mp3' stehen."""
    _setup(tmp_path)
    src = tmp_path / "x.mp3"
    src.write_bytes(b"")
    j = client.post("/api/intake/plan", json={"path": str(src), "artist": "A", "title": "B", "labelcode": "99999"}).json()
    assert j["plan"]["proposed_name"] == "" and j["plan"]["can_apply"] is False


def test_unlesbarer_labelcode_wird_bei_eingabe_abgelehnt_statt_gekuerzt(tmp_path):
    """Tippfehler, fremde Trenner und zwei Codes in einem Feld werden nicht still zum ersten Ziffernblock
    gekürzt ('LC 24-584' -> LC00024 ROYALBUNKER) und als verifiziert gespeichert bzw. getaggt, sondern abgelehnt."""
    _setup(tmp_path)
    for raw in ("LC 24-584", "LC2458O", "LC00121 / LC00392"):
        r = client.post("/api/db/song", json={"fields": {"interpret": "Zz Code", "titel": raw, "labelcode": raw}})
        assert r.status_code == 400 and "nicht lesbar" in r.json()["detail"], (raw, r.text)
        r = client.post("/api/db/verify", json={"interpret": "Zz Code V", "titel": raw, "labelcode": raw})
        assert r.status_code == 400, (raw, r.text)
    r = client.post("/api/db/song", json={"fields": {"interpret": "Zz Code", "titel": "Ok", "labelcode": "lc 121"}})
    assert r.status_code == 200 and r.json()["fields"]["labelcode"] == "LC00121", r.text
    plan = client.post("/api/intake/plan", json={"path": str(tmp_path / "x.mp3"), "artist": "A", "title": "B",
                                                   "labelcode": "LC 24-584"}).json()
    assert plan["plan"]["can_apply"] is False and plan["labelcode"] == ""
    assert any("nicht lesbar" in w for w in plan["plan"]["warnings"]), plan
    r = client.post("/api/intake/apply", json={"path": str(tmp_path / "x.mp3"), "artist": "A", "title": "B",
                                                "labelcode": "LC 24-584", "confirm": True})
    assert r.status_code == 422, r.text


def test_manuelles_label_unter_gvl_code_verdraengt_weder_namen_noch_rechterueckruf(tmp_path):
    """Ein zweites, von Hand angelegtes Label unter einem GVL-Code („ein Code, mehrere Label“) verdrängt in
    Tagger, Track-Datenbank und Suche weder den GVL-Namen noch den Rechterückruf des GVL-Labels (Cue-Karte,
    Export, Tagger): code2name nimmt nicht die zuletzt geladene Zeile, lookup_by_code sortiert 'Aaa Alias' nicht
    vor 'Atlantic'."""
    db = _setup(tmp_path)
    r = client.post("/api/labels/manual", json={"labelcode": "00121", "label": "Aaa Alias", "note": "Test"})
    assert r.status_code == 200, r.text
    assert appmod.INDEX.code2name["00121"] == "Atlantic"
    assert appmod._label_lookup("00121") == ("Atlantic", "R6;R9")
    plan = client.post("/api/intake/plan", json={"path": str(tmp_path / "x.mp3"), "artist": "A", "title": "B",
                                                   "labelcode": "LC00121"}).json()
    assert (plan["label"], plan["rechterueckrufe"]) == ("Atlantic", "R6;R9")
    j = client.post("/api/db/song", json={"fields": {"interpret": "Zz Alias", "titel": "Neu", "labelcode": "LC00121"}}).json()
    assert j["fields"]["labelname"] == "Atlantic"
    # das manuelle Label bleibt als eigener Eintrag sichtbar (1:n), nur nicht als GVL-Schreibweise
    assert {x["label"] for x in client.get("/api/search", params={"q": "00121"}).json()["results"]} >= {"Atlantic", "Aaa Alias"}


def test_netz_autoren_erscheinen_als_vorschlag_nicht_als_gepruefter_wert():
    """Autoren aus dem Netz stehen in der Cue-Tabelle als Vorschlag (wie der Komponist aus dem Dateinamen),
    mit Quelle und dem Hinweis, dass sie nicht gespeichert sind; die Zusammenfassung sagt „Vorschläge“."""
    html = client.get("/").text
    assert "if(v && c.author_new && i<3){ d.classList.add('hintv');" in html
    assert "Vorschlag aus dem Netz — nicht gespeichert, im Editor prüfen und speichern" in html
    assert "Komponisten-Vorschläge für {n} Titel (nicht gespeichert)" in html


def test_version_steht_in_health_spec_und_oberflaeche():
    """Eine Version für Rückmeldungen von Testenden — in /api/health, in der Info.plist der Mac-App und
    sichtbar in der Seitenleiste."""
    import re
    from pathlib import Path
    v = client.get("/api/health").json()["version"]
    assert v == appmod.APP_VERSION and re.fullmatch(r"\d+\.\d+\.\d+(-beta)?", v)
    spec = (Path(appmod.__file__).resolve().parent.parent / "cuecore.spec").read_text(encoding="utf-8")
    assert f'"CFBundleShortVersionString": "{v.split("-")[0]}"' in spec
    html = client.get("/").text
    assert 'id="appver"' in html and "j.version" in html


# --- Track-Index aus der App laden + Datenordner öffnen ---

def _wait_index_download(timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = client.get("/api/index/download").json()
        if st["state"] != "running":
            return st
        time.sleep(0.02)
    raise AssertionError("Download-Thread läuft noch")


def test_index_download_installs_and_loads_index_without_restart(tmp_path, monkeypatch):
    from core import trackindex
    _setup(tmp_path)
    songs_before = (tmp_path / "songs.db").read_bytes()

    def fake_install(d, *, progress, cancel, before_replace, **kw):
        progress("download", 5, 10)
        before_replace()
        con = trackindex.open_index(trackindex.path_for(d), create=True)
        con.close()
        return trackindex.path_for(d)
    monkeypatch.setattr(appmod.indexdownload, "install", fake_install)
    r = client.post("/api/index/download").json()
    assert r["state"] in ("running", "done")
    st = _wait_index_download()
    assert st["state"] == "done" and st["error"] == ""
    assert client.get("/api/index/status").json()["present"] is True        # sofort geladen, kein Neustart
    assert (tmp_path / "songs.db").read_bytes() == songs_before              # eigene Tracks unberührt


def test_index_download_reports_error_and_cancel(tmp_path, monkeypatch):
    _setup(tmp_path)

    def broken(d, **kw):
        raise appmod.indexdownload.DownloadError("Zu wenig freier Platz: 1,0 GB frei, rund 8,1 GB nötig.")
    monkeypatch.setattr(appmod.indexdownload, "install", broken)
    client.post("/api/index/download")
    st = _wait_index_download()
    assert st["state"] == "error" and "Platz" in st["error"]

    def waits_for_cancel(d, *, cancel, **kw):
        assert cancel.wait(5)
        raise appmod.indexdownload.Cancelled()
    monkeypatch.setattr(appmod.indexdownload, "install", waits_for_cancel)
    client.post("/api/index/download")
    client.post("/api/index/download/cancel")
    assert _wait_index_download()["state"] == "cancelled"


def test_index_status_names_download_size_and_data_dir(tmp_path):
    _setup(tmp_path)
    st = client.get("/api/index/status").json()
    assert st["download"]["size"] == appmod.indexdownload.SIZE and st["data_dir"] == str(tmp_path)


def test_open_data_folder_uses_platform_opener(tmp_path, monkeypatch):
    _setup(tmp_path)
    opened = []
    monkeypatch.setattr(appmod, "_open_folder", opened.append)
    r = client.post("/api/data-folder/open").json()
    assert r["path"] == str(tmp_path) and opened == [tmp_path]


def test_resolve_offers_musicbrainz_title_suggestions_when_title_is_partial(tmp_path, monkeypatch):
    """„rolling stones“ + „sympathy“ endet nicht in „kein Match“: Ohne MusicBrainz-Treffer bietet die
    Antwort volle Titel desselben Interpreten zum Anklicken an; mit Treffer nicht."""
    _setup(tmp_path)
    _mock_net(monkeypatch)
    monkeypatch.setattr(appmod.musicbrainz, "lookup", lambda a, t, **kw: MBLookup())
    asked = []
    monkeypatch.setattr(appmod.musicbrainz, "title_suggestions",
                        lambda a, t, **kw: asked.append((a, t)) or [{"interpret": "The Rolling Stones", "titel": "Sympathy for the Devil"}])
    j = client.post("/api/resolve", json={"artist": "rolling stones", "title": "sympathy"}).json()
    assert j["net_suggestions"] == [{"interpret": "The Rolling Stones", "titel": "Sympathy for the Devil"}]
    assert asked == [("rolling stones", "sympathy")]
    _mock_net(monkeypatch)
    j = client.post("/api/resolve", json={"artist": "Zzz Unbekannt", "title": "Nicht In DB"}).json()
    assert j["matched"] and "net_suggestions" not in j



def test_index_download_keeps_macos_from_napping(tmp_path, monkeypatch):
    """Im Hintergrund drosselt macOS die App (App Nap), das Entpacken wäre deutlich langsamer.
    Während des Downloads ist eine Aktivität angemeldet und wird danach wieder beendet, auch bei Fehler."""
    _setup(tmp_path)
    calls = []
    monkeypatch.setattr(appmod, "_no_app_nap", lambda reason: calls.append("begin") or (lambda: calls.append("end")))

    def broken(d, **kw):
        assert calls == ["begin"]
        raise appmod.indexdownload.DownloadError("kaputt")
    monkeypatch.setattr(appmod.indexdownload, "install", broken)
    client.post("/api/index/download")
    assert _wait_index_download()["state"] == "error" and calls == ["begin", "end"]


def test_start_removes_leftovers_of_an_interrupted_index_download(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    (tmp_path / "track_index.db.part").write_bytes(b"halb")
    monkeypatch.setitem(appmod._INDEX_DL, "state", "running")
    appmod.configure(db)                                   # läuft gerade ein Download: nichts anfassen
    assert (tmp_path / "track_index.db.part").exists()
    monkeypatch.setitem(appmod._INDEX_DL, "state", "idle")
    appmod.configure(db)
    assert not (tmp_path / "track_index.db.part").exists()
