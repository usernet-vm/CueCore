"""CMX3600-Parser + GEMA-Dauerberechnung.

Testdaten decken typische Muster von Avid-, Resolve-, Premiere- und Nucoda-Exporten
ab: FROM CLIP NAME / SOURCE FILE / FROM FILE, Kommentare mit
und ohne Leerzeichen nach dem Stern, Locator-Zeilen, Marker-Block-Abschluss,
Drop-Frame-Notation und derselbe Clip auf mehreren Spuren.
"""
import math

import pytest

from core.edl import (EDLEvent, ceil_duration, dedupe_multitrack, parse_assemble_list,
                      parse_cmx3600, parse_cmx3600_file, parse_edl, timecode_to_seconds)

SAMPLE_EDL = """\
TITLE: MY_PROJECT
FCM: NON-DROP FRAME
001  AX       AA    C        00:00:00:00 00:00:05:12 00:00:00:00 00:00:05:12
* FROM CLIP NAME: Alanis Morissette - Thank You.mp3
002  AX       AA    C        00:00:10:00 00:00:14:00 00:00:05:12 00:00:09:12
* SOURCE FILE: /audio/AURORA - Runaway.wav
003  AX       AA    C        00:01:00:00 00:01:03:15 00:00:09:12 00:00:12:15
"""

# Avid „Assemble List" (Tab-getrennt). Dekozeilen, Spaltenkopf, eine Lückenzeile (Clip leer)
# und zwei Einsätze desselben Clips an verschiedenen Positionen.
SAMPLE_ASSEMBLE = (
    "============================================================\n"
    "Assemble List\n"
    "------------------------------------------------------------\n"
    "Event\tFtgStart\tFtgEnd\tMasDur\tClip\t\n"
    "1\t00:00:00:00\t00:04:17:00\t00:04:17:01\t\t\n"                     # Lücke (kein Clip)
    "2\t00:04:17:01\t00:04:30:08\t00:00:13:08\tNordwand_LC02604.wav\t\n"
    "3\t00:04:30:09\t00:05:00:05\t00:00:29:22\tNordwand_LC02604.wav\t\n"
)


# --- Timecode-Konvertierung ---

def test_timecode_to_seconds_basic():
    assert timecode_to_seconds("00:00:05:00", fps=25.0) == 5.0
    assert timecode_to_seconds("00:01:00:00", fps=25.0) == 60.0
    assert timecode_to_seconds("01:00:00:00", fps=25.0) == 3600.0


def test_timecode_to_seconds_with_frames():
    assert timecode_to_seconds("00:00:00:12", fps=25.0) == pytest.approx(0.48)
    assert timecode_to_seconds("00:00:05:12", fps=25.0) == pytest.approx(5.48)


def test_timecode_to_seconds_invalid_raises():
    with pytest.raises(ValueError):
        timecode_to_seconds("not-a-timecode")


def test_timecode_semicolon_notation_non_drop_stays_linear():
    # Ohne drop_frame-Flag wird ';' nur als Trenner gelesen (lineare Rechnung).
    assert timecode_to_seconds("00:00:05;12", fps=25.0) == pytest.approx(5.48)


def test_timecode_drop_frame_skips_frame_numbers():
    # Drop-Frame (29.97): TC 00:00:00;00 ist der Nullpunkt.
    assert timecode_to_seconds("00:00:00;00", fps=30.0, drop_frame=True) == pytest.approx(0.0)
    # Bei einer vollen Minute fallen 2 Frame-Nummern weg -> 1798 Frames / 29.97 ≈ 59.99 s
    # (die 00:01:00;00-Notation existiert bei Drop-Frame gar nicht als eigener Sprung).
    one_min = timecode_to_seconds("00:00:59;29", fps=30.0, drop_frame=True)
    assert one_min == pytest.approx(1799 / (30000/1001), abs=0.01)
    # 10 Minuten: keine Auslassung in der zehnten Minute -> nahe 600 s
    ten = timecode_to_seconds("00:10:00;00", fps=30.0, drop_frame=True)
    assert ten == pytest.approx(600.0, abs=0.05)


# --- GEMA-konforme Dauerberechnung (einmalig pro Cue, nicht kumulativ) ---

def test_ceil_duration_rounds_up_to_next_full_second():
    assert ceil_duration(3.12) == 4
    assert ceil_duration(4.0) == 4       # exakt volle Sekunde bleibt unveraendert
    assert ceil_duration(0.01) == 1      # jede Nutzung mind. 1 volle Sekunde


def test_event_gema_duration_matches_math_ceil():
    ev = EDLEvent("001", "AX", "AA", "C", "00:00:00:00", "00:00:05:12", "00:00:09:12", "00:00:12:15")
    raw = ev.duration_sec(fps=25.0)
    assert ev.gema_duration_sec(fps=25.0) == math.ceil(raw)


# --- CMX3600-Parser ---

def test_parse_cmx3600_extracts_all_events():
    events = parse_cmx3600(SAMPLE_EDL)
    assert [e.event_num for e in events] == ["001", "002", "003"]
    assert [e.track_type for e in events] == ["AA", "AA", "AA"]
    assert [e.edit_type for e in events] == ["C", "C", "C"]


def test_parse_cmx3600_reads_timecodes():
    ev = parse_cmx3600(SAMPLE_EDL)[0]
    assert ev.source_in == "00:00:00:00" and ev.source_out == "00:00:05:12"
    assert ev.record_in == "00:00:00:00" and ev.record_out == "00:00:05:12"


def test_parse_cmx3600_associates_clip_name_from_comment():
    events = parse_cmx3600(SAMPLE_EDL)
    assert events[0].clip_name == "Alanis Morissette - Thank You.mp3"   # FROM CLIP NAME-Konvention
    assert events[1].clip_name == "/audio/AURORA - Runaway.wav"          # SOURCE FILE-Konvention


def test_parse_cmx3600_event_without_comment_has_empty_clip_name():
    events = parse_cmx3600(SAMPLE_EDL)
    assert events[2].clip_name == ""


def test_parse_cmx3600_computes_gema_durations():
    events = parse_cmx3600(SAMPLE_EDL)
    # Event 1: rec 00:00:00:00 -> 00:00:05:12 = 5.48s -> ceil 6
    assert events[0].gema_duration_sec() == 6
    # Event 2: rec 00:00:05:12 -> 00:00:09:12 = exakt 4.0s -> ceil 4
    assert events[1].gema_duration_sec() == 4
    # Event 3: rec 00:00:09:12 -> 00:00:12:15 = 3.12s -> ceil 4
    assert events[2].gema_duration_sec() == 4


def test_parse_cmx3600_ignores_blank_lines_and_unrelated_comments():
    edl = SAMPLE_EDL + "\n* RANDOM COMMENT NOT A CLIP NAME\n\n"
    events = parse_cmx3600(edl)
    assert len(events) == 3  # keine Geister-Events aus dem Rauschen


def test_parse_cmx3600_file(tmp_path):
    p = tmp_path / "sample.edl"
    p.write_text(SAMPLE_EDL, encoding="utf-8")
    events = parse_cmx3600_file(p)
    assert len(events) == 3


# --- Quellnamen-Konventionen (mehrere NLEs) ---

def test_source_file_preferred_over_from_clip_name():
    # Beide Kommentare vorhanden: der Original-Dateiname (sauberer) gewinnt für clip_name.
    edl = ("001  R1 V     C        00:00:00:00 00:00:02:00 00:00:00:00 00:00:02:00\n"
           "*FROM CLIP NAME:  Band X - Song (LAY3)\n"
           "*SOURCE FILE: Band X - Song.wav\n")
    ev = parse_cmx3600(edl)[0]
    assert ev.from_clip_name == "Band X - Song (LAY3)"
    assert ev.source_file == "Band X - Song.wav"
    assert ev.clip_name == "Band X - Song.wav"      # Property bevorzugt SOURCE FILE


def test_comment_without_space_after_star():
    # Avid schreibt '*FROM CLIP NAME:' ohne Leerzeichen nach dem Stern.
    edl = ("001  R1 V     C        00:00:00:00 00:00:02:00 00:00:00:00 00:00:02:00\n"
           "*FROM CLIP NAME:  Artist - Title.mp3\n")
    assert parse_cmx3600(edl)[0].clip_name == "Artist - Title.mp3"


def test_from_file_media_reference_fallback():
    # Nucoda-Stil 'FROM FILE:' — nur Medienreferenz, kein Clip-Name/Source-File.
    edl = ("001  R1 V     C        00:00:00:00 00:00:02:00 00:00:00:00 00:00:02:00\n"
           "* FROM FILE: /vol/media/Artist - Title.wav\n")
    ev = parse_cmx3600(edl)[0]
    assert ev.media_ref == "/vol/media/Artist - Title.wav"
    assert ev.clip_name == "Artist - Title.wav"     # Basisname aus der Medienreferenz


def test_locator_and_effect_comments_are_not_clip_names():
    edl = ("001  R1 V     C        00:00:00:00 00:00:02:00 00:00:00:00 00:00:02:00\n"
           "*LOC: 00:00:01:00 RED  Cue Marker\n"
           "*ASC_SOP (1.0 1.0 1.0)(0 0 0)(1 1 1)\n"
           "* FROM CLIP NAME:  Real - Clip.wav\n")
    ev = parse_cmx3600(edl)[0]
    assert ev.clip_name == "Real - Clip.wav"        # LOC/ASC_SOP nicht als Name missdeutet


def test_marker_block_ends_event_parsing():
    # Avid hängt nach den Events einen Marker-Block an ('* ===='); der darf keine
    # Geister-Events oder falschen Clip-Namen erzeugen.
    edl = ("001  R1 V     C        00:00:00:00 00:00:02:00 00:00:00:00 00:00:02:00\n"
           "* FROM CLIP NAME:  Artist - Title.wav\n"
           "*\n"
           "* ============================================================\n"
           "* Marker Metadata\n"
           "* Sequence name: whatever\n")
    events = parse_cmx3600(edl)
    assert len(events) == 1 and events[0].clip_name == "Artist - Title.wav"


# --- Drop-Frame per FCM-Header ---

def test_fcm_drop_frame_header_sets_event_drop_flag():
    edl = ("FCM: DROP FRAME\n"
           "001  R1 V     C        00:00:00;00 00:00:02;00 00:00:00;00 00:00:02;00\n")
    assert parse_cmx3600(edl)[0].drop_frame is True


def test_fcm_non_drop_frame_header_keeps_linear():
    edl = ("FCM: NON-DROP FRAME\n"
           "001  R1 V     C        00:00:00:00 00:00:02:00 00:00:00:00 00:00:02:00\n")
    assert parse_cmx3600(edl)[0].drop_frame is False


# --- Multi-Track-Deduplizierung ---

def test_dedupe_multitrack_collapses_same_clip_same_position():
    evs = [
        EDLEvent("001", "R1", "V", "C", "0", "0", "00:00:00:00", "00:00:05:00", from_clip_name="A - B.wav"),
        EDLEvent("002", "R1", "A", "C", "0", "0", "00:00:00:00", "00:00:05:00", from_clip_name="A - B.wav"),
        EDLEvent("003", "R1", "A2", "C", "0", "0", "00:00:00:00", "00:00:05:00", from_clip_name="A - B.wav"),
    ]
    out = dedupe_multitrack(evs)
    assert len(out) == 1 and out[0].event_num == "001"


def test_dedupe_keeps_distinct_positions_and_unnamed():
    evs = [
        EDLEvent("001", "R1", "V", "C", "0", "0", "00:00:00:00", "00:00:05:00", from_clip_name="A - B.wav"),
        EDLEvent("002", "R1", "V", "C", "0", "0", "00:00:05:00", "00:00:10:00", from_clip_name="A - B.wav"),
        EDLEvent("003", "R1", "V", "C", "0", "0", "00:00:10:00", "00:00:12:00"),  # kein Name -> bleibt
    ]
    out = dedupe_multitrack(evs)
    assert len(out) == 3   # andere Record-Position + namenloses Event bleiben erhalten


# --- Avid „Assemble List" (Tab-Format) ---

def test_parse_assemble_list_extracts_named_cues_skips_gap():
    events = parse_assemble_list(SAMPLE_ASSEMBLE)
    # Lückenzeile (Event 1, leerer Clip) sowie Deko/Spaltenkopf entfallen.
    assert [e.event_num for e in events] == ["2", "3"]
    assert all(e.clip_name == "Nordwand_LC02604.wav" for e in events)


def test_parse_assemble_list_uses_ftg_as_record_tc():
    ev = parse_assemble_list(SAMPLE_ASSEMBLE)[0]
    assert ev.record_in == "00:04:17:01" and ev.record_out == "00:04:30:08"
    # Record-Dauer = FtgEnd - FtgStart -> ceil
    assert ev.gema_duration_sec(fps=25.0) == ceil_duration(ev.duration_sec(25.0))


def test_parse_assemble_list_handles_cr_line_endings():
    # Avid exportiert die Assemble List mit reinen \\r-Zeilenenden.
    text = SAMPLE_ASSEMBLE.replace("\n", "\r")
    events = parse_assemble_list(text)
    assert [e.event_num for e in events] == ["2", "3"]


def test_parse_edl_dispatches_assemble_list():
    events = parse_edl(SAMPLE_ASSEMBLE)
    assert [e.event_num for e in events] == ["2", "3"]   # Tab-Format erkannt


def test_parse_edl_dispatches_cmx3600():
    events = parse_edl(SAMPLE_EDL)
    assert [e.event_num for e in events] == ["001", "002", "003"]   # CMX3600-Default


def test_parse_edl_falls_back_to_cmx_when_assemble_markers_absent():
    # Keine FtgStart/MasDur-Spalten -> CMX3600-Pfad.
    assert len(parse_edl(SAMPLE_EDL)) == 3


# --- Überblendungen, Musik-Filter, Split-Edits ---------------------------------------------------------------

_DISSOLVE_EDL = (
    "FCM: NON-DROP FRAME\n"
    "009  AX       A3    C        00:00:00:00 00:00:00:00 10:01:50:00 10:01:50:00\n"
    "009  AX       A3    D    025 00:00:05:00 00:00:25:00 10:01:50:00 10:02:10:00\n"
    "* FROM CLIP NAME: Alt - Ausblenden.wav\n"
    "* TO CLIP NAME: Neu - Einblenden.wav\n")


def test_dissolve_line_with_duration_is_parsed():
    """'D    025' (Blendendauer) brach die Regex — die einblendende Zeile fiel samt Musik aus dem Cue-Sheet."""
    events = parse_cmx3600(_DISSOLVE_EDL)
    assert [e.edit_type for e in events] == ["C", "D"]
    assert events[1].record_in == "10:01:50:00" and events[1].record_out == "10:02:10:00"


def test_dissolve_from_and_to_clip_names_go_to_their_lines():
    out_ev, in_ev = parse_cmx3600(_DISSOLVE_EDL)
    assert out_ev.clip_name == "Alt - Ausblenden.wav"
    assert in_ev.clip_name == "Neu - Einblenden.wav"
    assert in_ev.gema_duration_sec() == 20


def test_split_music_events_drops_video_zero_length_and_camera_audio():
    from core.edl import split_music_events
    evs = [EDLEvent("001", "R", "V", "C", "00:00:00:00", "00:00:02:00", "10:00:00:00", "10:00:02:00",
                    from_clip_name="A001C003.MXF"),
           EDLEvent("002", "R", "A", "C", "00:00:00:00", "00:00:02:00", "10:00:00:00", "10:00:02:00",
                    from_clip_name="A001C003_220801.MXF"),
           EDLEvent("003", "AX", "A3", "C", "00:00:00:00", "00:00:00:00", "10:00:05:00", "10:00:05:00",
                    from_clip_name="Song - Leer.wav"),
           EDLEvent("004", "AX", "A3", "C", "00:00:00:00", "00:00:10:00", "10:00:05:00", "10:00:15:00",
                    from_clip_name="Artist - Song.wav"),
           EDLEvent("005", "AX", "B", "C", "00:00:00:00", "00:00:10:00", "10:00:20:00", "10:00:30:00",
                    from_clip_name="Artist - Clip mit Bild.wav")]
    keep, dropped = split_music_events(evs)
    assert [e.event_num for e in keep] == ["004", "005"]
    assert [r for _, r in dropped] == ["Videospur", "Ton aus Videodatei (O-Ton)", "Länge 0"]


def test_merge_contiguous_joins_split_edit_and_rounds_once():
    from core.edl import merge_contiguous
    a = EDLEvent("006", "AX", "A3", "C", "00:00:10:00", "00:00:20:10", "10:01:20:00", "10:01:30:10",
                 from_clip_name="Night Drive.wav")
    b = EDLEvent("007", "AX", "A3", "C", "00:00:40:00", "00:00:54:15", "10:01:30:10", "10:01:45:00",
                 from_clip_name="Night Drive.wav")
    out = merge_contiguous([a, b])
    assert len(out) == 1 and out[0].record_out == "10:01:45:00"
    assert out[0].gema_duration_sec() == 25          # vorher 11 + 15 = 26
    assert a.record_out == "10:01:30:10"             # Eingabe bleibt unverändert


def test_merge_contiguous_keeps_separate_uses_and_other_clips():
    from core.edl import merge_contiguous
    evs = [EDLEvent("001", "AX", "A3", "C", "00:00:00:00", "00:00:10:00", "10:00:00:00", "10:00:10:00",
                    from_clip_name="Theme.wav"),
           EDLEvent("002", "AX", "A3", "C", "00:00:00:00", "00:00:05:00", "10:00:10:00", "10:00:15:00",
                    from_clip_name="Other.wav"),
           EDLEvent("003", "AX", "A3", "C", "00:00:20:00", "00:00:30:00", "10:00:20:00", "10:00:30:00",
                    from_clip_name="Theme.wav")]
    assert [e.event_num for e in merge_contiguous(evs)] == ["001", "002", "003"]
