"""Intake link tokens are masked in uvicorn's access log (#74)."""

import logging
import subprocess
import sys

import pytest

from app.access_log import MaskIntakeTokens, mask_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/i/abc", "/i/***"),
        ("//i/abc", "/i/***"),
        ("/i/SECRET123?lang=zh", "/i/***?lang=zh"),
        ("/i/SECRET%2F123", "/i/***"),
        ("/api/intake/abc", "/api/intake/***"),
        ("//api/intake/abc/files", "/api/intake/***/files"),
        ("/api/intake/abc/files", "/api/intake/***/files"),
        ("/api/intake/abc/submit", "/api/intake/***/submit"),
        ("/api/intake/abc?x=1", "/api/intake/***?x=1"),
        ("/", "/"),
        ("/i/", "/i/"),
        ("/i/?x=1", "/i/?x=1"),
        ("/api/intake", "/api/intake"),
        ("/api/intake/", "/api/intake/"),
        ("/api/cases/x", "/api/cases/x"),
        ("/images/logo.png", "/images/logo.png"),
    ],
)
def test_mask_path_hides_intake_tokens_and_keeps_other_paths(raw: str, expected: str) -> None:
    assert mask_path(raw) == expected


def test_uvicorn_access_record_rewrites_token_before_formatting(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main  # noqa: F401

    access_logger = logging.getLogger("uvicorn.access")
    # Alembic's fileConfig disables loggers imported before test DB setup.
    monkeypatch.setattr(access_logger, "disabled", False)
    monkeypatch.setattr(access_logger, "propagate", True)
    caplog.set_level(logging.INFO, logger="uvicorn.access")

    access_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5000",
        "POST",
        "/api/intake/SECRET123/files",
        "1.1",
        404,
    )

    assert "SECRET123" not in caplog.text
    assert "/api/intake/***/files" in caplog.text
    assert any(
        "/api/intake/***/files" in record.getMessage() and "SECRET123" not in record.getMessage()
        for record in caplog.records
    )


def test_alternative_record_layouts_and_plain_messages_are_masked(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main  # noqa: F401

    access_logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access_logger, "disabled", False)
    monkeypatch.setattr(access_logger, "propagate", True)
    caplog.set_level(logging.INFO, logger="uvicorn.access")

    access_logger.info("%s %d", "/i/SECRET123?x=1", 200)
    access_logger.info(
        '%s "%s" %d %s',
        "127.0.0.1:5000",
        "/api/intake/SECRET123/submit",
        200,
        "OK",
    )
    access_logger.info("/i/SECRET123")

    no_args_record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, "//api/intake/SECRET123/files", None, None
    )
    assert MaskIntakeTokens().filter(no_args_record) is True
    assert no_args_record.msg == "/api/intake/***/files"

    assert "SECRET123" not in caplog.text
    assert [r.getMessage() for r in caplog.records] == [
        "/i/***?x=1 200",
        '127.0.0.1:5000 "/api/intake/***/submit" 200 OK',
        "/i/***",
    ]


def test_reimporting_main_attaches_only_one_intake_filter() -> None:
    import app.main  # noqa: F401

    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, MaskIntakeTokens) for f in filters) == 1
    # Reload in a subprocess so we do not replace app.main.app/api in the live pytest process.
    code = (
        "import importlib, logging, app.main; "
        "importlib.reload(app.main); "
        "from app.access_log import MaskIntakeTokens; "
        "assert sum(isinstance(f, MaskIntakeTokens) "
        "for f in logging.getLogger('uvicorn.access').filters) == 1"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_non_access_records_pass_through_unchanged(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main  # noqa: F401

    access_logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access_logger, "disabled", False)
    monkeypatch.setattr(access_logger, "propagate", True)
    caplog.set_level(logging.INFO, logger="uvicorn.access")

    access_logger.info("Server started")
    access_logger.info("worker %s pid %d", "a", 1)

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, "%s %s %s %s %s", (1, 2, 3, 4, 5), None
    )
    assert MaskIntakeTokens().filter(record) is True
    assert record.args == (1, 2, 3, 4, 5)
    assert [r.getMessage() for r in caplog.records] == ["Server started", "worker a pid 1"]
