"""Place labels require public place identity rather than the first words of an event."""

import pytest

from edgar_core.public_places import public_place_label


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Scene",
        "The sheep arrives",
        "Clattering hooves, surprised yelps, and frantic bleating cut",
    ],
)
def test_unnamed_opening_prose_has_a_stable_generic_place_label(name):
    opening = (
        "Clattering hooves, surprised yelps, and frantic bleating cut through the afternoon. "
        "A sheep runs toward you with a scroll in its mouth."
    )
    assert public_place_label(opening, name) == "Unidentified place"


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("You stand outside the Red Lion Inn. Its door is open.", "Red Lion Inn"),
        ("The Red Lion Inn is busy tonight.", "Red Lion Inn"),
        ("You arrive in Lindenford as the lamps are lit.", "Lindenford"),
        ("The map identifies the settlement as a place called North Weir.", "North Weir"),
        ("Town square is crowded with festival stalls.", "Town square"),
    ],
)
def test_a_short_explicitly_earned_place_name_is_preserved(text, name):
    assert public_place_label(text, name) == name


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("A sheep runs toward you.", "Secret Wizard Tower"),
        ("A Sheep Arrives and offers you a scroll.", "A Sheep Arrives"),
        ("A Winterhall resident offers a greeting.", "Hall"),
    ],
)
def test_private_names_prose_prefixes_and_event_titles_do_not_become_places(text, name):
    assert public_place_label(text, name) == "Unidentified place"


@pytest.mark.parametrize(("text", "name", "expected"), [
    ("Festival boats bump against the quay. Orin waves you over.", "Festival boats bump against the quay", "Quay"),
    ("The towpath is slick with fresh silt.", "The towpath is slick with fresh silt", "Towpath"),
])
def test_long_arrival_prefix_is_replaced_by_its_public_physical_noun(text, name, expected):
    assert public_place_label(text, name) == expected
