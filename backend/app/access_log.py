"""Mask tenant intake link tokens in uvicorn access-log records (#74)."""

import logging
import re

INTAKE_PATH = re.compile(r"^/(i|api/intake)/[^/?#]+")


def mask_path(path: str) -> str:
    """Replace the token segment in /i/<token> and /api/intake/<token> with ***."""
    return INTAKE_PATH.sub(r"/\1/***", path)


class MaskIntakeTokens(logging.Filter):
    """Rewrites /i/<token> and /api/intake/<token> in uvicorn access records (#74)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (args[0], args[1], mask_path(args[2]), args[3], args[4])
        return True
