"""Mask tenant intake link tokens in uvicorn access-log records (#74)."""

import logging
import re

INTAKE_PATH = re.compile(r"^/+(i|api/intake)/[^/?#]+")


def mask_path(path: str) -> str:
    """Replace the token segment in /i/<token> and /api/intake/<token> with ***."""
    return INTAKE_PATH.sub(r"/\1/***", path)


class MaskIntakeTokens(logging.Filter):
    """Rewrites /i/<token> and /api/intake/<token> in uvicorn access records (#74)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if args and isinstance(args, tuple):
            record.args = tuple(mask_path(a) if isinstance(a, str) else a for a in args)
        elif not args and isinstance(record.msg, str):
            record.msg = mask_path(record.msg)
        return True
