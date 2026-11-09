"""The shared delimiter strip keeps obfuscated tags out of wrapped blocks."""

from coach.scrub import strip_delimited


def test_full_open_and_close_tags_are_removed():
    text = "a <coach-note> b </coach-note> c"
    assert strip_delimited(text, "coach-note") == "a  b  c"


def test_close_tag_with_attributes_is_removed():
    assert strip_delimited("x</coach-note data-x=1>y", "coach-note") == "xy"


def test_zero_width_split_is_removed():
    assert strip_delimited("x</coach-note\u200b>y", "coach-note") == "xy"


def test_split_close_tag_collapses_to_a_fixpoint():
    text = "</coach-guidanc</coach-guidance>e>"
    assert strip_delimited(text, "coach-guidance") == ""


def test_bare_opener_is_removed():
    assert strip_delimited("x<coach-note", "coach-note") == "x"


def test_other_tags_survive():
    assert strip_delimited("<coach-guidance>", "coach-note") == "<coach-guidance>"
