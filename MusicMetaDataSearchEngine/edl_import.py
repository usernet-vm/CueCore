#!/usr/bin/env python3
"""EDL importieren: CMX3600 einlesen, gegen songs.db cross-checken (Schreibback
über die Identification Pipeline), Excel exportieren.

  python edl_import.py projekt.edl [--out projekt.xlsx] [--fps 25]

Live-Lookup (Netz) für fehlende Songs. Ohne API-Key (MusicBrainz+Discogs+Deezer
keyless). Schreibt NUR nach songs.db — kein ID3/Rename (EDL-Clips referenzieren
Dateinamen, keine garantiert vorhandenen Dateien, s. core/edl_import.py).
"""
from __future__ import annotations
import argparse
import sys
from collections import Counter
from pathlib import Path

from core import deezer, discogs, musicbrainz, netfetch, songsdb
from core.edl import parse_cmx3600_file, split_music_events
from core.edl_import import cross_check, export_excel
from core.labeldb import connect, lookup_by_code
from core.labelmatch import build_index_from_db
from core.resolver import resolve

_noop = lambda *a, **k: None
SONGS_SEED = Path(__file__).resolve().parent / "data" / "songs.db"
ACTIVE_SONGS = Path.home() / "Library" / "Application Support" / "MusicMeta" / "songs.db"
DEFAULT_LABELS_DB = Path.home() / "Library" / "Application Support" / "MusicMeta" / "labels.db"


def _make_resolve_fn(labels_db_path: Path):
    con = connect(labels_db_path)
    idx = build_index_from_db(con)

    def resolve_fn(artist: str, title: str) -> dict | None:
        fetcher = netfetch.build_fetcher(labels_db_path)
        res = resolve(
            artist, title, idx,
            mb_lookup=lambda a, t: musicbrainz.lookup(a, t, fetch=fetcher, sleep=_noop),
            discogs_label_codes=lambda rid: discogs.lookup_label_codes(rid, fetch=fetcher, sleep=_noop),
            deezer_lookup=lambda a, t: deezer.lookup(a, t, fetch=fetcher, sleep=_noop),
        )
        best = res.best
        if not best:
            return None
        rows = lookup_by_code(con, best.labelcode)
        label = rows[0]["label"] if rows else best.label
        return {"labelname": label, "labelcode": best.labelcode,
                "komponist": res.composer, "texter": res.lyricist,
                "isrc": res.isrcs[0] if res.isrcs else ""}

    return resolve_fn


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(description="CMX3600-EDL-Import")
    ap.add_argument("edl_path", help="Pfad zur .edl-Datei (CMX3600)")
    ap.add_argument("--out", default=None, help="Excel-Ausgabepfad (Default: neben der EDL)")
    ap.add_argument("--labels-db", default=str(DEFAULT_LABELS_DB))
    ap.add_argument("--fps", type=float, default=25.0, help="Standard: 25 (PAL/EU)")
    args = ap.parse_args(argv)

    edl_path = Path(args.edl_path)
    if not edl_path.exists():
        print(f"EDL nicht gefunden: {edl_path}", file=sys.stderr)
        return 2
    labels_db = Path(args.labels_db)
    if not labels_db.exists():
        print(f"labels.db nicht gefunden: {labels_db}\n(zuerst build_label_db.py laufen lassen)", file=sys.stderr)
        return 2

    events, ignored = split_music_events(parse_cmx3600_file(edl_path))
    print(f"[1/3] {len(events) + len(ignored)} EDL-Events geparst, {len(ignored)} nicht als Musik gewertet.")
    for ev, reason in ignored:
        print(f"      ignoriert: {ev.event_num} {ev.track_type} {ev.clip_name or '-'} ({reason})")

    songsdb.seed(ACTIVE_SONGS, SONGS_SEED)
    con = songsdb.open(ACTIVE_SONGS)
    resolve_fn = _make_resolve_fn(labels_db)

    print("[2/3] Cross-Check gegen songs.db + Identification Pipeline …")
    cards = cross_check(events, con, resolve_fn=resolve_fn, fps=args.fps)
    con.commit()
    con.close()

    out = Path(args.out) if args.out else edl_path.with_suffix(".xlsx")
    export_excel(cards, out)
    print(f"[3/3] Export: {out}")

    counts = Counter(c["status"] for c in cards)
    for status, n in sorted(counts.items()):
        print(f"      {status}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
