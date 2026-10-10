"""The daemon must not log request URLs from HTTP clients: Telegram's Bot API
puts the bot token in the URL path (leak found in benbois logs, 2026-10-10)."""
import logging

from bitcoiners_dca.cli import _quiet_http_client_logs


def test_http_client_loggers_are_quieted_to_warning():
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)

    _quiet_http_client_logs()

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
