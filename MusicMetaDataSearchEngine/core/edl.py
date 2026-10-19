"""CMX3600-EDL-Parser + GEMA-konforme Dauerberechnung.

Grundidee: Clip-Name als Titel, Record-Dauer je Cue, gleiche Namen summieren.

Zwei Eingangsformate werden erkannt (Dispatcher `parse_edl`):
  - CMX3600 (Standard, s. u.) — von Avid/Resolve/Premiere/Nucoda exportierbar.
  - Avid „Assemble List" (Tab-getrennt: Event/FtgStart/FtgEnd/MasDur/Clip) — ein reiner
    Text-Export mit Clip-Namen pro Zeile und Record-Timecodes; siehe `parse_assemble_list`.

CMX3600 ist der stabil standardisierte Broadcast/Post-Production-EDL-Standard
(seit den 1970ern, von praktisch jedem NLE exportierbar). Der Event-Zeilen-Kern
(Event-Nummer, Reel, Kanal, Schnittart, vier Timecodes) ist Norm und hier sicher
implementiert und mit Exporten aus Avid, DaVinci Resolve, Premiere und Nucoda
kompatibel.

Quelldateinamen tragen NLEs uneinheitlich in Kommentarzeilen. Die gängigen
Konventionen werden alle erkannt (mit/ohne Leerzeichen nach dem Stern, ein oder
zwei Leerzeichen nach dem Doppelpunkt):

  FROM CLIP NAME:  <Clip-Name im Schnitt>   — oft mit Versions-/Layer-Zusatz
  SOURCE FILE:     <Original-Dateiname>      — meist der sauberste Name
  FROM FILE: / FROM CLIP: <Medienpfad>       — Medienreferenz (Fallback)

Für die Artist/Titel-Extraktion wird der Original-Dateiname bevorzugt, weil er
seltener Zusätze wie '(LAY3)' oder '.NEW.01' trägt.

Kommentarzeilen wie LOC (Marker), M2 (Speed), ASC_SOP/SAT (Farbe) oder
Effekt-Zeilen werden bewusst NICHT als Clip-Namen gelesen.

Timecode: 25 fps non-drop ist der EU/PAL-Standard und Default. Drop-Frame
(NTSC-Umfeld, Frame-Trenner ';', angekündigt per 'FCM: DROP FRAME') wird erkannt
und für die Dauerberechnung korrekt behandelt — pro Minute fallen zwei
Frame-Nummern weg, außer in jeder zehnten Minute.
"""
from __future__ import annotations
import math
import re
from dataclasses import dataclass
from pathlib import Path

_EVENT_LINE = re.compile(
    r"^(?P<event>\d{3,})\s+"
    r"(?P<reel>\S+)\s+"
    r"(?P<track>[A-Z0-9/]+)\s+"                      # V, A, A2, AA, B, A2/V …
    r"(?P<edit>[A-Z]+\d*)\s+"                        # C, D, W###, KB …
    r"(?:(?P<trans_dur>\d{1,4})\s+)?"                # Blendendauer in Frames bei D/W/K ('D    025')
    r"(?P<src_in>\d{2}:\d{2}:\d{2}[:;]\d{2})\s+"
    r"(?P<src_out>\d{2}:\d{2}:\d{2}[:;]\d{2})\s+"
    r"(?P<rec_in>\d{2}:\d{2}:\d{2}[:;]\d{2})\s+"
    r"(?P<rec_out>\d{2}:\d{2}:\d{2}[:;]\d{2})\s*$"
)
_TC = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[:;](\d{2})$")

# Kommentar-Konventionen für Quellnamen. Ein Stern, optionaler Space, die Kennung,
# optionaler Doppelpunkt, dann der Wert. 'FROM CLIP NAME' MUSS vor 'FROM CLIP'
# geprüft werden (sonst schluckt das kürzere Präfix den Namen).
def _comment_re(cid: str) -> re.Pattern:
    return re.compile(r"^\*?\s*" + cid + r"\s*:?\s*(.+)$", re.IGNORECASE)

_C_FROM_CLIP_NAME = _comment_re("FROM CLIP NAME")
_C_TO_CLIP_NAME = _comment_re("TO CLIP NAME")       # Überblendung: Name der einblendenden Zeile
_C_SOURCE_FILE = _comment_re("SOURCE FILE")
_C_MEDIA_REF = _comment_re(r"FROM (?:FILE|CLIP)")   # Nucoda 'FROM FILE', Avid 'FROM CLIP'
# Avid hängt nach den Events einen Marker-Metadaten-Block an, eingeleitet mit '* ===='.
_SECTION_END = re.compile(r"^\*\s*=")


def _is_drop_frame(tc: str) -> bool:
    return ";" in tc


def timecode_to_seconds(tc: str, fps: float = 25.0, drop_frame: bool = False) -> float:
    """'HH:MM:SS:FF' (oder ';FF' bei Drop-Frame) -> Sekunden.

    Non-drop: geradlinig h*3600 + m*60 + s + f/fps.
    Drop-Frame (NTSC-Umfeld): die Timecode-Nummern überspringen pro Minute zwei
    Frames (außer jede zehnte Minute); zurückgerechnet auf echte Sekunden über die
    tatsächliche 30000/1001-Rate. Rundungsfrei — die Ganzzahl-Aufrundung passiert
    separat via ceil_duration() pro Cue.
    """
    m = _TC.match(tc)
    if not m:
        raise ValueError(f"Ungültiger Timecode: {tc!r}")
    h, mi, s, f = (int(g) for g in m.groups())
    if drop_frame:
        nominal = round(fps) or 30                  # Drop-Frame ist ein 30/29.97-Konzept
        total_min = 60 * h + mi
        dropped = 2 * (total_min - total_min // 10)
        total_frames = (h * 3600 + mi * 60 + s) * nominal + f - dropped
        return total_frames / (nominal * 1000.0 / 1001.0)
    return h * 3600 + mi * 60 + s + f / fps


def ceil_duration(seconds: float) -> int:
    """GEMA-konforme Dauerberechnung: jede einzelne Nutzung wird
    EINMALIG auf die nächste volle Sekunde aufgerundet — nicht kumulativ über
    mehrere Cues hinweg. Aufruf pro Event, nicht auf eine Gesamtsumme."""
    return math.ceil(seconds)


@dataclass
class EDLEvent:
    event_num: str
    reel: str
    track_type: str
    edit_type: str
    source_in: str
    source_out: str
    record_in: str
    record_out: str
    from_clip_name: str = ""   # Clip-Name im Schnitt (kann Versions-/Layer-Zusatz tragen)
    source_file: str = ""      # Original-Dateiname (meist der sauberste)
    media_ref: str = ""        # Medienpfad (FROM FILE / FROM CLIP), Fallback
    drop_frame: bool = False
    # XML-Formate (xmeml: Frames, FCPXML: rationale Sekunden) liefern die Dauer exakt — dann
    # zählt sie und nicht die aus den synthetischen Timecodes zurückgerechnete.
    duration_override: float | None = None

    @property
    def clip_name(self) -> str:
        """Bester Name für die Artist/Titel-Extraktion: Original-Dateiname zuerst
        (seltener mit Zusätzen), dann Clip-Name, dann der Basisname der Medienreferenz."""
        if self.source_file:
            return self.source_file
        if self.from_clip_name:
            return self.from_clip_name
        return Path(self.media_ref).name if self.media_ref else ""

    def duration_sec(self, fps: float = 25.0) -> float:
        """Record-Dauer (wie lange der Cue im fertigen Programm läuft) —
        nicht die Source-Dauer (die bei Zeitraffer/-lupe abweichen könnte)."""
        if self.duration_override is not None:
            return self.duration_override
        return (timecode_to_seconds(self.record_out, fps, self.drop_frame)
                - timecode_to_seconds(self.record_in, fps, self.drop_frame))

    def gema_duration_sec(self, fps: float = 25.0) -> int:
        return ceil_duration(self.duration_sec(fps))


def parse_cmx3600(text: str) -> list[EDLEvent]:
    """CMX3600-Text zu EDLEvents. Kommentarzeilen direkt nach einem Event füllen
    dessen Quellnamen (FROM CLIP NAME / SOURCE FILE / FROM FILE|CLIP). Der Drop-Frame-
    Modus folgt dem letzten 'FCM:'-Header und dem Frame-Trenner im Timecode selbst.
    Ein Avid-Marker-Block ('* ====') beendet die Event-Auswertung.

    Überblendung (D/W/K): zwei Zeilen mit derselben Event-Nummer — die erste (meist 0 Frames) ist der
    ausblendende Clip, die zweite der einblendende. FROM CLIP NAME gehört zur ersten, TO CLIP NAME zur
    zweiten; SOURCE FILE/FROM FILE füllen der Reihe nach die erste Zeile, der der Wert noch fehlt."""
    events: list[EDLEvent] = []
    group: list[EDLEvent] = []          # Zeilen des aktuellen Events (2 bei Überblendung)
    fcm_drop = False
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("FCM"):
            fcm_drop = "DROP" in upper and "NON" not in upper
            continue
        if _SECTION_END.match(line):
            break
        m = _EVENT_LINE.match(line)
        if m:
            drop = fcm_drop or _is_drop_frame(m["rec_in"])
            current = EDLEvent(
                event_num=m["event"], reel=m["reel"], track_type=m["track"], edit_type=m["edit"],
                source_in=m["src_in"], source_out=m["src_out"],
                record_in=m["rec_in"], record_out=m["rec_out"], drop_frame=drop,
            )
            if not group or group[0].event_num != current.event_num:
                group = []
            group.append(current)
            events.append(current)
            continue
        if not group:
            continue
        cm = _C_FROM_CLIP_NAME.match(line)
        if cm:
            if not group[0].from_clip_name:
                group[0].from_clip_name = cm.group(1).strip()
            continue
        cm = _C_TO_CLIP_NAME.match(line)
        if cm:
            if len(group) > 1 and not group[-1].from_clip_name:
                group[-1].from_clip_name = cm.group(1).strip()
            continue
        cm = _C_SOURCE_FILE.match(line)
        if cm:
            target = next((e for e in group if not e.source_file), None)
            if target is not None:
                target.source_file = cm.group(1).strip()
            continue
        cm = _C_MEDIA_REF.match(line)
        if cm:
            target = next((e for e in group if not e.media_ref), None)
            if target is not None:
                target.media_ref = cm.group(1).strip()
    return events


def parse_assemble_list(text: str) -> list[EDLEvent]:
    """Avid „Assemble List" (Tab-getrennter Text-Export) zu EDLEvents.

    Spalten: ``Event  FtgStart  FtgEnd  MasDur  Clip`` (Tab-getrennt). FtgStart/FtgEnd sind
    Record-Timecodes im Programm, ``Clip`` der Dateiname (leer = Lücke ohne Musik). Mehrere
    „Assemble List"-Blöcke (je Audiospur) werden alle gelesen; identische Clips an gleicher
    Position fasst `dedupe_multitrack` später zusammen, verschiedene Einsätze summiert
    `edl_import.merge_cards`. Dekozeilen (====, ----), Block- und Spaltenüberschrift werden
    übersprungen. Zeilen ohne Clip-Namen (Lücken) sind keine Musik-Cues und entfallen."""
    events: list[EDLEvent] = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) < 5 or not cells[0].isdigit():
            continue
        ftg_start, ftg_end, clip = cells[1], cells[2], cells[4]
        if not (_TC.match(ftg_start) and _TC.match(ftg_end)) or not clip:
            continue
        events.append(EDLEvent(
            event_num=cells[0], reel="", track_type="", edit_type="",
            source_in=ftg_start, source_out=ftg_end,          # Assemble List hat keine Source-TCs
            record_in=ftg_start, record_out=ftg_end,
            from_clip_name=clip, drop_frame=_is_drop_frame(ftg_start),
        ))
    return events


# --- XML-Schnittlisten: Final Cut Pro 7 XML (xmeml) und FCPXML -------------------------------
# Grundlage (nur offizielle Dokumentation):
#  - xmeml: Apple „Final Cut Pro XML Interchange Format" (Archiv, Elements): <xmeml version>, <sequence>
#    mit <rate><timebase>/<ntsc>, <media><audio><track><clipitem> mit <name>, <start>, <end> (Frames
#    in der Sequenz-Timebase; -1 = an eine Transition angrenzend), <in>, <out>, <file id> mit <name>/
#    <pathurl>. Premiere Pro schreibt dieses Format über File > Export > Final Cut Pro XML (Adobe-Hilfe
#    „Export a project as a Final Cut Pro XML file", 07.01.2026); DaVinci Resolve über
#    resolve.EXPORT_FCP_7_XML (Scripting-API).
#  - FCPXML: Apple „FCPXML Reference" (developer.apple.com): <fcpxml version>, <resources> mit <asset
#    id name …> (+ <media-rep src> ab 1.9), <sequence><spine> mit <asset-clip name ref offset duration
#    start>, <clip> mit <audio>/<video>-Kindern, Zeiten als rationale Sekunden („1001/24000s", „5s"),
#    Lane-Index für angeschlossene Clips. DaVinci Resolve schreibt EXPORT_FCPXML_1_8/1_9/1_10.
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse


def _basename_from_url(url: str) -> str:
    """file:///Volumes/Media/My%20Song.wav -> 'My Song.wav' (auch relative Pfade, auch Windows)."""
    u = (url or "").strip()
    if not u:
        return ""
    path = urlparse(u).path if "://" in u else u
    return unquote(path).replace("\\", "/").rsplit("/", 1)[-1]


def rational_seconds(value: str) -> float | None:
    """FCPXML-Zeitwert -> Sekunden: '1001/24000s', '5s', '0s'. Unlesbar -> None."""
    v = (value or "").strip()
    if not v.endswith("s"):
        return None
    v = v[:-1]
    try:
        if "/" in v:
            n, d = v.split("/", 1)
            return int(n) / int(d)
        return float(v)
    except (ValueError, ZeroDivisionError):
        return None


def _frames_to_tc(frames: int, fps: float) -> str:
    nominal = max(int(round(fps)), 1)
    frames = max(int(frames), 0)
    total_s, ff = divmod(frames, nominal)
    h, rem = divmod(total_s, 3600)
    mi, se = divmod(rem, 60)
    return f"{h:02d}:{mi:02d}:{se:02d}:{ff:02d}"


def _seconds_to_tc(seconds: float, fps: float) -> str:
    return _frames_to_tc(int(round(max(seconds, 0.0) * fps)), fps)


def _strip_ns(root) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]


def parse_xmeml(text: str) -> list[EDLEvent]:
    """Final Cut Pro 7 XML (xmeml; Premiere „Final Cut Pro XML", Resolve FCP 7 XML): alle
    <clipitem> unter <media><audio><track>. Dauer = (end - start) Frames in der Sequenz-Timebase
    (ntsc TRUE -> ×1000/1001); grenzt der Clip an eine Transition (start/end = -1), zählt out - in.
    Clip-Name = <name>, Original-Dateiname = <file><name> bzw. Basisname von <pathurl>; ein <file>
    ohne Kinder verweist per id auf die erste vollständige Definition."""
    try:
        root = ET.fromstring(text.lstrip("\ufeff").strip())
    except ET.ParseError:
        return []
    _strip_ns(root)
    if root.tag != "xmeml":
        return []
    events: list[EDLEvent] = []
    files: dict[str, tuple[str, str]] = {}
    n = 0
    for seq in root.iter("sequence"):
        rate = seq.find("rate")
        timebase = float((rate.findtext("timebase") if rate is not None else None) or 25)
        ntsc = ((rate.findtext("ntsc") if rate is not None else "") or "").strip().upper() == "TRUE"
        fps = timebase * 1000 / 1001 if ntsc else timebase
        media = seq.find("media")
        audio = media.find("audio") if media is not None else None
        if audio is None:
            continue
        for ti, track in enumerate(audio.findall("track"), 1):
            for ci in track.findall("clipitem"):
                name = (ci.findtext("name") or "").strip()
                f = ci.find("file")
                fname, furl = "", ""
                if f is not None:
                    fid = f.get("id", "")
                    fname = (f.findtext("name") or "").strip()
                    furl = (f.findtext("pathurl") or "").strip()
                    if fname or furl:
                        files.setdefault(fid, (fname, furl))
                    elif fid in files:
                        fname, furl = files[fid]
                try:
                    start, end = int(ci.findtext("start") or -1), int(ci.findtext("end") or -1)
                    cin, cout = int(ci.findtext("in") or -1), int(ci.findtext("out") or -1)
                except ValueError:
                    continue
                if start >= 0 and end >= 0 and end >= start:
                    frames, rec_in = end - start, start
                elif cin >= 0 and cout >= 0 and cout >= cin:      # Transition angrenzend: -1
                    frames, rec_in = cout - cin, max(start, 0)
                else:
                    continue
                n += 1
                events.append(EDLEvent(
                    f"{n:03d}", "AX", f"A{ti}", "C",
                    _frames_to_tc(max(cin, 0), fps), _frames_to_tc(max(cout, 0), fps),
                    _frames_to_tc(rec_in, fps), _frames_to_tc(rec_in + frames, fps),
                    from_clip_name=name, source_file=fname or _basename_from_url(furl), media_ref=furl,
                    duration_override=frames / fps if fps else 0.0))
    return events


def parse_fcpxml(text: str) -> list[EDLEvent]:
    """FCPXML 1.8–1.10 (Final Cut Pro X, DaVinci Resolve): Story-Elemente mit Audio in jeder
    <sequence> — <asset-clip> (ganzes Asset), <clip> mit <audio>-Kind, <audio> direkt. Asset ohne
    Audio (hasAudio="0") zählt nicht. Dauer = rationale Sekunden aus `duration`; Position = `offset`
    (Elternzeit, Lane-Clips relativ zum Anker). Dateiname = Basisname von <media-rep src> bzw.
    <asset src>, sonst Asset-`name`."""
    try:
        root = ET.fromstring(text.lstrip("\ufeff").strip())
    except ET.ParseError:
        return []
    _strip_ns(root)
    if root.tag != "fcpxml":
        return []
    assets: dict[str, dict] = {}
    for a in root.iter("asset"):
        rep = a.find("media-rep")
        src = (rep.get("src") if rep is not None else "") or a.get("src", "")
        assets[a.get("id", "")] = {"name": a.get("name", ""), "src": src,
                                   "has_audio": (a.get("hasAudio") or "1").strip() != "0"}
    fps = 25.0
    fmt = root.find("resources/format")
    fd = rational_seconds(fmt.get("frameDuration", "")) if fmt is not None else None
    if fd:
        fps = 1 / fd
    events: list[EDLEvent] = []
    n = 0
    # Ein <audio> als Kind eines Clips ist dessen Tonkomponente (über den Clip gezählt), nur ein
    # frei stehendes <audio> (Spine, Lane) ist ein eigener Cue.
    parent = {c: a for a in root.iter() for c in a}
    component_hosts = {"clip", "sync-clip", "ref-clip", "mc-clip", "asset-clip", "audition"}
    for seq in root.iter("sequence"):
        for el in seq.iter():
            if el.tag == "asset-clip":
                ref = el.get("ref", "")
            elif el.tag == "clip":
                au = el.find("audio")
                if au is None:
                    continue
                ref = au.get("ref", "")
            elif el.tag == "audio":
                par = parent.get(el)
                if par is not None and par.tag in component_hosts:
                    continue
                ref = el.get("ref", "")
            else:
                continue
            asset = assets.get(ref, {"name": "", "src": "", "has_audio": True})
            if not asset["has_audio"]:
                continue
            dur = rational_seconds(el.get("duration", ""))
            if dur is None or dur <= 0:
                continue
            off = rational_seconds(el.get("offset", "")) or 0.0
            start = rational_seconds(el.get("start", "")) or 0.0
            n += 1
            fname = _basename_from_url(asset["src"]) or asset["name"]
            events.append(EDLEvent(
                f"{n:03d}", "AX", "AA", "C",
                _seconds_to_tc(start, fps), _seconds_to_tc(start + dur, fps),
                _seconds_to_tc(off, fps), _seconds_to_tc(off + dur, fps),
                from_clip_name=el.get("name", "") or asset["name"], source_file=fname,
                media_ref=asset["src"], duration_override=dur))
    return events


# --- Pro Tools „Session Info as Text" --------------------------------------------------------
# Grundlage: Avid Pro Tools Reference Guide, Abschnitt zum Export von Session-Informationen als Text
# (File > Export > Session Info as Text). Die Datei beginnt mit SESSION NAME / SAMPLE RATE /
# BIT DEPTH / TIMECODE FORMAT / # OF AUDIO TRACKS/CLIPS/FILES, dann optional File List und Clip List,
# zuletzt die Track-EDLs: je Track eine tab-getrennte Liste mit Event-Nummer, Clip-Name, Start, Ende
# und Dauer. Das Zeitformat ist im Dialog wählbar. Die exakten Spaltenüberschriften sind nicht
# festgelegt — der Parser liest sie deshalb aus der Kopfzeile der Datei (Spalte mit „CLIP NAME",
# dazu START/END/DURATION).
_PT_HEADER_KEYS = ("SESSION NAME", "SAMPLE RATE", "BIT DEPTH", "TIMECODE FORMAT")


def _pt_time_to_seconds(value: str, fps: float, sample_rate: float) -> float | None:
    """Pro-Tools-Zeitfeld -> Sekunden: Timecode hh:mm:ss:ff[.ss] (Subframes), Min:Secs m:ss.mmm,
    Samples (Ganzzahl bei Sample-Rate). Bars|Beats und Feet+Frames sind nicht umrechenbar -> None."""
    v = (value or "").strip()
    if not v:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})[:;](\d{1,3})(?:\.(\d+))?$", v)
    if m:
        h, mi, s, f = (int(m.group(i)) for i in range(1, 5))
        sub = int(m.group(5) or 0) / 100.0
        return h * 3600 + mi * 60 + s + (f + sub) / (fps or 25.0)
    m = re.match(r"^(\d{1,3}):(\d{2})(?:[.,](\d{1,3}))?$", v)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2)) + (int(m.group(3).ljust(3, "0")) / 1000.0 if m.group(3) else 0.0)
    if re.fullmatch(r"\d{4,}", v) and sample_rate:
        return int(v) / sample_rate
    return None


def parse_protools_text(text: str) -> list[EDLEvent]:
    """Pro Tools „Session Info as Text": je Track-EDL (Zeile „TRACK NAME:") die tab-getrennten
    Event-Zeilen unter der Kopfzeile mit „CLIP NAME"; Dauer aus der Duration-Spalte, sonst aus
    Start/End. Clip-Name = Spalte CLIP NAME (Pro-Tools-Clipnamen tragen meist den Dateinamen ohne
    Endung, plus Suffixe wie „-01" für Teilclips)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    head = "\n".join(lines[:40]).upper()
    if "SESSION NAME" not in head:
        return []
    fps, sample_rate = 25.0, 0.0
    for ln in lines[:40]:
        up = ln.upper()
        if up.startswith("TIMECODE FORMAT:"):
            m = re.search(r"(\d+(?:[.,]\d+)?)", ln.split(":", 1)[1])
            if m:
                fps = float(m.group(1).replace(",", "."))
                if fps in (30.0, 24.0, 60.0) and "DROP" in up:
                    fps = fps * 1000 / 1001
        elif up.startswith("SAMPLE RATE:"):
            m = re.search(r"(\d+(?:\.\d+)?)", ln.split(":", 1)[1])
            if m:
                sample_rate = float(m.group(1))
    events: list[EDLEvent] = []
    track, cols, n = "", None, 0
    for ln in lines:
        if ln.upper().startswith("TRACK NAME:"):
            track = ln.split(":", 1)[1].strip()
            cols = None
            continue
        if "\t" not in ln:
            continue
        cells = [c.strip() for c in ln.split("\t")]
        up = [c.upper() for c in cells]
        if "CLIP NAME" in up:
            cols = {name: i for i, name in enumerate(up)}
            continue
        if cols is None or "CLIP NAME" not in cols or cols["CLIP NAME"] >= len(cells):
            continue
        name = cells[cols["CLIP NAME"]]
        if not name:
            continue
        def cell(*keys):
            for k in keys:
                for cname, i in cols.items():
                    if cname.startswith(k) and i < len(cells):
                        return cells[i]
            return ""
        start = _pt_time_to_seconds(cell("START"), fps, sample_rate)
        end = _pt_time_to_seconds(cell("END"), fps, sample_rate)
        dur = _pt_time_to_seconds(cell("DURATION"), fps, sample_rate)
        if dur is None and start is not None and end is not None and end >= start:
            dur = end - start
        if dur is None:
            continue
        n += 1
        rec_in = start or 0.0
        events.append(EDLEvent(
            f"{n:03d}", "AX", track or "AA", "C",
            "00:00:00:00", _seconds_to_tc(dur, fps),
            _seconds_to_tc(rec_in, fps), _seconds_to_tc(rec_in + dur, fps),
            from_clip_name=name, duration_override=dur))
    return events


def parse_edl(text: str) -> list[EDLEvent]:
    """Format-Dispatcher: XML (FCPXML / Final Cut Pro 7 xmeml — Premiere, Resolve, FCP) an der
    Wurzel erkannt; Avid „Assemble List" (Tab, Spalten FtgStart/MasDur — Avid-typisch, nie in
    CMX3600) vs. CMX3600 (Default). Alle liefern EDLEvent[] für dieselbe Pipeline.
    Fällt auf CMX3600 zurück, falls die Assemble-List-Erkennung nichts Verwertbares findet."""
    text = text.lstrip("\ufeff")
    head = text.lstrip()[:400].lower()
    if head.startswith("<"):
        if "<fcpxml" in head:
            return parse_fcpxml(text)
        if "<xmeml" in head:
            return parse_xmeml(text)
    if "SESSION NAME" in text[:2000].upper() and "TRACK NAME" in text.upper():
        events = parse_protools_text(text)
        if events:
            return events
    if "FtgStart" in text and "MasDur" in text:
        events = parse_assemble_list(text)
        if events:
            return events
    return parse_cmx3600(text)


def dedupe_multitrack(events: list[EDLEvent]) -> list[EDLEvent]:
    """Ein Clip, der gleichzeitig auf mehreren Spuren liegt (Video + mehrere
    Audiospuren), ist EIN musikalischer Cue, nicht mehrere. Events mit gleichem
    Quellnamen und gleicher Record-Position werden zu einem zusammengefasst (der
    erste gewinnt). Events ohne Quellnamen bleiben unangetastet (nicht gruppierbar)."""
    seen: set[tuple[str, str, str]] = set()
    out: list[EDLEvent] = []
    for ev in events:
        name = ev.clip_name
        if not name:
            out.append(ev)
            continue
        key = (name, ev.record_in, ev.record_out)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


# Kamera-/Video-Container: Ton daraus ist O-Ton, keine Musik (A001C003_220801.MXF, CLIP0001.MOV …).
_VIDEO_EXT = re.compile(r"\.(mxf|mov|mp4|m4v|avi|mts|m2ts|r3d|braw|ari|dnxhd|dpx|mkv|wmv)$", re.IGNORECASE)


def non_music_reason(ev: EDLEvent) -> str:
    """Grund, warum ein Event kein Musik-Cue ist ('' = Musik-Kandidat):
    reine Videospur, Länge 0 (ausblendende Zeile einer Überblendung) oder Ton aus einem Video-Container."""
    track = (ev.track_type or "").upper()
    if track and "A" not in track and track != "B":
        return "Videospur"
    if ev.duration_override is None and ev.record_in == ev.record_out:
        return "Länge 0"
    if _VIDEO_EXT.search((ev.clip_name or "").strip()):
        return "Ton aus Videodatei (O-Ton)"
    return ""


def split_music_events(events: list[EDLEvent]) -> tuple[list[EDLEvent], list[tuple[EDLEvent, str]]]:
    """(Musik-Kandidaten, [(ausgefiltertes Event, Grund)]). Atmo/SFX werden NICHT nach Namen gefiltert —
    das bleibt der Karte überlassen (unparsable_clip_name), damit kein echter Titel verschwindet."""
    keep: list[EDLEvent] = []
    dropped: list[tuple[EDLEvent, str]] = []
    for ev in events:
        reason = non_music_reason(ev)
        if reason:
            dropped.append((ev, reason))
        else:
            keep.append(ev)
    return keep, dropped


def merge_contiguous(events: list[EDLEvent], fps: float = 25.0) -> list[EDLEvent]:
    """Split-Edits zu einem Cue: gleicher Quellname, Record-Bereiche überlappend oder lückenlos
    aneinander (auch über Spuren hinweg) = EINE Nutzung. Dauer = Anfang bis Ende im Programm, aufgerundet
    wird erst danach (sonst wird jedes Teilstück einzeln aufgerundet: 10 + 15 statt 25 s).
    Getrennte Verwendungen (mit Lücke) bleiben eigene Cues. Events mit exakter Dauer aus XML
    (duration_override) und Events ohne Quellnamen bleiben unangetastet."""
    out: list[EDLEvent] = []
    open_by_name: dict[str, tuple[EDLEvent, float, float]] = {}

    def secs(ev: EDLEvent) -> tuple[float, float]:
        return (timecode_to_seconds(ev.record_in, fps, ev.drop_frame),
                timecode_to_seconds(ev.record_out, fps, ev.drop_frame))

    order = sorted(range(len(events)), key=lambda i: secs(events[i])[0] if events[i].duration_override is None else -1)
    merged: dict[int, EDLEvent] = {}
    for i in order:
        ev = events[i]
        name = ev.clip_name
        if not name or ev.duration_override is not None:
            merged[i] = ev
            continue
        start, end = secs(ev)
        prev = open_by_name.get(name)
        if prev is not None and start <= prev[2] + 1e-6:
            base, p_start, p_end = prev
            if end > p_end:
                base.record_out = ev.record_out
                open_by_name[name] = (base, p_start, end)
            continue
        base = EDLEvent(**{**ev.__dict__})
        open_by_name[name] = (base, start, end)
        merged[i] = base
    for i in range(len(events)):
        if i in merged:
            out.append(merged[i])
    return out


def parse_cmx3600_file(path: str | Path) -> list[EDLEvent]:
    return parse_cmx3600(Path(path).read_text(encoding="utf-8", errors="replace"))
