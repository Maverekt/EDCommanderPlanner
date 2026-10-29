from app.core.normalize import display_text, mask_fid, normalize_symbol


def test_strips_localisation_wrapper():
    assert normalize_symbol("$int_hyperdrive_size5_class5_name;") == "int_hyperdrive_size5_class5"


def test_trims_and_lowercases():
    # FDevIDs lists `Untypical Shield Scans` with a trailing space (DESIGN §6).
    assert normalize_symbol("Untypical Shield Scans ") == "untypical shield scans"
    assert normalize_symbol("  Anaconda") == "anaconda"


def test_wrapper_without_name_suffix():
    assert normalize_symbol("$MULTIPLAYER_SCENARIO42_TITLE;") == "multiplayer_scenario42_title"


def test_none_passthrough():
    assert normalize_symbol(None) is None


def test_unnamed_ship_is_blank():
    assert display_text("") is None
    assert display_text(" ") is None
    assert display_text(" Mav ") == "Mav"


def test_mask_fid():
    assert mask_fid("F0000001") == "F00***01"
    assert mask_fid("") == ""
