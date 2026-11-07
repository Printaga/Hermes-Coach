# tests/test_trustflags.py
from coach.trustflags import read_flags, set_flags


def test_set_flags_writes_both_flags():
    config = {}
    set_flags(config, True)
    assert read_flags(config) == (True, True)


def test_read_flags_survives_a_non_mapping_intermediate_node():
    # A hand-edited config can put a list where a mapping belongs; a read must
    # answer False/False instead of raising AttributeError.
    assert read_flags({"plugins": {"entries": []}}) == (False, False)
    assert read_flags({"plugins": {"entries": {"coach": "nope"}}}) == (False, False)


def test_set_flags_names_the_malformed_config_node():
    config = {"plugins": {"entries": {"coach": []}}}
    try:
        set_flags(config, True)
    except TypeError as exc:
        assert "entries.coach" in str(exc)
    else:  # pragma: no cover - the assertion is the test
        raise AssertionError("expected TypeError")
