"""Conservative reference identity shared by rule and creature retrieval.

Punctuation and case may vary across extracted headings; words, numbers and qualifiers
must remain intact. This deliberately does not singularize names or use substring identity.
"""

import re
import unicodedata


def normalize_reference(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().replace("’", "'")
    return re.sub(r"\W+", " ", text).strip()


def reference_matches(value: str, reference: str) -> bool:
    normalized = normalize_reference(reference)
    return bool(normalized) and normalize_reference(value) == normalized
