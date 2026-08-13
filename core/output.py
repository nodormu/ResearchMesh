"""Shared output trimming for the local tools.

Every local tool clips what it returns so a single call can't flood the
context window. The per-tool budgets differ (bash/editor output is worth more
than a page of scraped HTML), so the limit is the caller's choice.
"""


def clip(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n…[truncated, {len(text) - limit} more chars]"
    return text
