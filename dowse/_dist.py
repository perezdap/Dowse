"""PyPI distribution name (import package remains ``dowse``)."""
from __future__ import annotations

_FALLBACK = "dowse-context"


def distribution_name() -> str:
    """Wheel/sdist name on PyPI (e.g. dowse-context), not the import path."""
    return _FALLBACK


def pip_extra_hint(extra: str) -> str:
    return f'pip install "{distribution_name()}[{extra}]"'