"""Conservative input-token estimates shared by prompt and memory assembly.

No tokenizer is used: `tiktoken` is only a transitive dependency that downloads its vocabulary
at first use, and the encoding of the configured models is not known offline. One token per
three UTF-8 bytes over-counts English prose (about four characters per token) and stays close
to one token per character for CJK text, so a fitted prompt stays inside its provider budget.
"""

from collections.abc import Iterable

BYTES_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    return -(-len(text.encode()) // BYTES_PER_TOKEN)


def fit_count(sizes: Iterable[int], budget: int) -> int:
    """How many leading items fit `budget` together; later items are never skipped to fit."""
    total = 0
    count = 0
    for size in sizes:
        total += size
        if total > budget:
            break
        count += 1
    return count
