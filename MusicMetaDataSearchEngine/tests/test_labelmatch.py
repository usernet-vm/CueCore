from core.labelmatch import LabelIndex, build_index, match_label, norm

EMPTY = LabelIndex()


def test_alias_exact():
    assert match_label("Atlantic", EMPTY) == "00121"
    assert match_label("4AD", EMPTY) == "05807"
    assert match_label("Warner Records", EMPTY) == "00392"


def test_alias_after_strip_generic():
    # 'mom and pop' ist als normalisierter Alias hinterlegt (Sony-Pressung)
    assert match_label("Mom & Pop", EMPTY) == "101515"


def test_universal_fallback():
    assert match_label("Universal Music Group", EMPTY) == "97777"
    assert match_label("UME", EMPTY) == "97777"


def test_major_token_in_compound_license_string():
    assert match_label("under exclusive license to Interscope Records", EMPTY) == "06406"


def test_exact_db_match():
    idx = build_index([("12345", "Foo Bar Tonträger")])
    assert match_label("Foo Bar Tonträger", idx) == "12345"


def test_fuzzy_strip_generic_match():
    idx = build_index([("12345", "Foo Bar Records")])
    assert match_label("Foo Bar", idx) == "12345"


def test_ambiguous_returns_none():
    # gleicher bereinigter Name -> zwei Codes -> nicht raten (1:n Labelname)
    idx = build_index([("11111", "Glassnote Records"), ("22222", "Glassnote Music")])
    assert match_label("Glassnote", idx) is None


def test_no_match_returns_none():
    assert match_label("Völlig Unbekanntes Label XYZ", LabelIndex()) is None


# --- Major/Indie-Namensdublette via GVL-hersteller (Republic-Fall) ---
# GVL-Lage: '07552 REPUBLIC' (Universal) vs '57251 Republic Records' (Z Records Ltd).
# Der Major-Imprint heißt im Master nur 'REPUBLIC', das Indie trägt exakt 'Republic Records'.
REPUBLIC_IDX = build_index([
    ("07552", "REPUBLIC", "Universal Music Entertainment GmbH"),
    ("57251", "Republic Records", "Z Records Ltd"),
])


def test_republic_records_resolves_to_major_not_indie():
    # exakter Voll-Match liefe aufs Indie 57251 -> hersteller-Disambig zieht den Major
    assert match_label("Republic Records", REPUBLIC_IDX) == "07552"


def test_republic_exact_major_name_unaffected():
    # 'REPUBLIC' matcht den Major direkt — Override darf nicht stören
    assert match_label("REPUBLIC", REPUBLIC_IDX) == "07552"


def test_no_override_when_major_code_absent_from_index():
    # 'Mercury Records' strip -> 'mercury' (Major-Token), aber der Major-Code 00268 ist
    # in diesem Index nicht vorhanden -> nichts erfinden, exakter Treffer bleibt
    idx = build_index([("55555", "Mercury Records", "Tiny Indie Ltd")])
    assert match_label("Mercury Records", idx) == "55555"


def test_no_override_when_exact_match_is_itself_major():
    # exakter Treffer ist selbst ein Major -> nicht auf einen anderen Code umbiegen
    idx = build_index([("06406", "Interscope", "Universal Music Entertainment GmbH")])
    assert match_label("Interscope", idx) == "06406"


def test_norm_shared():
    assert norm("AC/DC & Co.") == "ac dc and co"
    assert norm("LC 24584") == "lc 24584"  # norm ist generisch, kein LC-Parser


def test_alias_production_library_sublabels():
    # Library-Listen (Extreme Music v2, KPM 2022-05): Sub-Label-Namen, deren Code im GVL-Master
    # unter anderem Namen steht — ohne Alias kein Treffer (El Dorado = A-Tone, Vortrax = Spintrest).
    assert match_label("El Dorado", EMPTY) == "52577"
    assert match_label("Vortrax", EMPTY) == "51024"
    assert match_label("Extreme Unique Library", EMPTY) == "01732"
    assert match_label("Hype Music", EMPTY) == "51049"
    assert match_label("KPM Main Series", EMPTY) == "02061"
    assert match_label("KPM International", EMPTY) == "02061"


# --- MusicBrainz-Aliasse: dritte Quelle NACH GVL-Master und ALIASES ---------------------------

def _mb_index(tmp_path, rows):
    from core.labelmatch import attach_mb_aliases
    idx = build_index([("00121", "Atlantic", "Warner Music"), ("10192", "DOMINO British", "Domino"),
                       ("11111", "Blue Note", "X"), ("22222", "Other Label", "Y"),
                       ("30001", "Alpha Records", "A"), ("30002", "Beta Music", "B")])
    p = tmp_path / "label_aliases_mb.csv"
    p.write_text("name,labelcode,kind,mb_label\n" + "".join(f"{n},{c},alias,1\n" for n, c in rows), encoding="utf-8")
    return idx, attach_mb_aliases(idx, p)


def test_mb_alias_used_only_when_gvl_has_nothing(tmp_path):
    idx, n = _mb_index(tmp_path, [("Atlantik Schallplatten", "00121"), ("Domino Recording Company", "10192"),
                                  ("Unbekannt", "55555")])
    assert n == 3                                                # 'atlantik schallplatten' + 'domino recording company' + gestrippt 'domino company'; 55555 ignoriert
    assert match_label("Atlantik Schallplatten", idx) == "00121"
    assert match_label("Domino Recording Company Ltd", idx) == "10192"   # gestrippter Schlüssel
    assert match_label("Unbekannt", idx) is None                 # Code nicht im Index -> nie geraten
    assert match_label("Atlantic", idx) == "00121"               # GVL/ALIASES unverändert


def test_mb_alias_never_overrides_gvl_hit(tmp_path):
    idx, _ = _mb_index(tmp_path, [("Blue Note", "22222")])       # MB sagt 22222, GVL kennt 'Blue Note' als 11111
    assert match_label("Blue Note", idx) == "11111"


def test_mb_alias_conflicting_stripped_key_is_dropped(tmp_path):
    idx, _ = _mb_index(tmp_path, [("Delta Records", "30001"), ("Delta Music", "30002")])
    assert match_label("Delta Records", idx) == "30001"
    assert match_label("Delta Music", idx) == "30002"
    assert match_label("Delta Ltd", idx) is None                 # 'delta' zeigt auf zwei Codes


def test_mb_alias_missing_file_is_empty(tmp_path):
    from core.labelmatch import attach_mb_aliases
    idx = build_index([("00121", "Atlantic", "Warner Music")])
    assert attach_mb_aliases(idx, tmp_path / "nope.csv") == 0 and idx.mb_alias == {}


# --- BMG ---------------------------------------------------------------------------------------------------

_BMG = build_index([
    ("01413", "BMG Entertainment", "Sony Music Entertainment Germany GmbH", ""),
    ("19813", "BMG RIGHTS MANAGEMENT", "BMG Rights Management GmbH", ""),
    ("51803", "Rise Records (BMG)", "BMG Rights Management GmbH", ""),
    ("28867", "Rise Music", "Rise International Music Ltd", ""),
    ("05834", "Mute Records", "BMG Rights Management GmbH", ""),
])


def test_plain_bmg_is_not_mapped_to_old_sony_catalog():
    """'BMG' allein traf LC 01413 (BMG Entertainment, heute Sony, Rückruf R12) — für heutiges BMG falsch."""
    assert match_label("BMG", EMPTY) is None
    assert match_label("BMG", _BMG) is None


def test_bmg_rights_management_variants():
    assert match_label("BMG Rights Management", EMPTY) == "19813"
    assert match_label("BMG Rights Management (UK) Ltd", _BMG) == "19813"


def test_label_with_parenthetical_suffix_in_master():
    assert match_label("Rise Records", _BMG) == "51803"
    assert match_label("Rise Records (BMG)", _BMG) == "51803"


def test_compound_label_tag_resolves_when_unique():
    assert match_label("BMG / Mute", _BMG) == "05834"
    assert match_label("Mute Records, BMG Rights Management", _BMG) is None   # zwei Codes -> nicht raten
