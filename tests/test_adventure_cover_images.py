import pytest

from apps.api.schemas.adventure import AdventureUpdate
from apps.api.services.cover_images import normalize_cover_image


@pytest.mark.parametrize(
    ("stored", "canonical"),
    [
        ("assets/covers/bridge.svg", "assets/covers/bridge.svg"),
        ("ui/assets/covers/bridge.svg", "/ui/assets/covers/bridge.svg"),
        ("/ui/assets/covers/bridge.svg", "/ui/assets/covers/bridge.svg"),
        ("api/sessions/42/assets/map.svg", "/api/sessions/42/assets/map.svg"),
        ("/api/sessions/42/assets/map.svg", "/api/sessions/42/assets/map.svg"),
    ],
)
def test_normalizes_supported_cover_metadata(stored: str, canonical: str):
    assert normalize_cover_image(stored) == canonical
    assert AdventureUpdate(cover_image=stored).cover_image == canonical


@pytest.mark.parametrize(
    "unsafe",
    [
        "https://example.test/cover.jpg",
        "//example.test/cover.jpg",
        "javascript:alert(1)",
        "data:image/svg+xml,<svg/>",
        "assets/../private/cover.svg",
        "assets/%2e%2e/private.svg",
        "assets//cover.svg",
        "assets/./cover.svg",
        "assets/cover.svg?secret=1",
        "assets/cover.svg#fragment",
        "assets\\cover.svg",
        "private/cover.svg",
    ],
)
def test_rejects_unsafe_or_unsupported_cover_metadata(unsafe: str):
    with pytest.raises(ValueError):
        normalize_cover_image(unsafe)
    with pytest.raises(ValueError):
        AdventureUpdate(cover_image=unsafe)


def test_absent_cover_is_supported():
    assert normalize_cover_image(None) is None
    assert AdventureUpdate(cover_image=None).cover_image is None
