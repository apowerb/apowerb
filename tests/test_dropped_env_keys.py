"""A variable written in .env but not returned by the parser must be named.

Production carried `GOOGLE_WEBHOOK_AUDIENCE="tbd"NOTIFICATION_EMAIL="..."` on
one line -- a missing newline. python-dotenv drops the whole statement: the
first key took the placeholder as its value and the second did not exist at
all. A grep on the file reported both as present. The only signal was dotenv's
own `could not parse statement starting at line N`, which counts statements
rather than file lines and so points at the wrong place.

These tests pin the check that would have named the key in one line of log.
"""

from __future__ import annotations

import logging

import pytest

from apowerb.configs.settings import _warn_env_keys_dropped_by_the_parser

LOGGER = "apowerb.configs.settings"


def _env(tmp_path, body: str):
    path = tmp_path / ".env"
    path.write_text(body)
    return str(path)


def test_the_real_production_line_is_caught(tmp_path, caplog):
    """Two assignments crammed together: the second key vanishes."""
    path = _env(
        tmp_path,
        'API_KEY=fine\n'
        'GOOGLE_WEBHOOK_AUDIENCE="tbd"NOTIFICATION_EMAIL="ops@example.com"\n'
        'OTHER=fine\n',
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _warn_env_keys_dropped_by_the_parser(path)

    assert "GOOGLE_WEBHOOK_AUDIENCE" in caplog.text
    assert "line 2" in caplog.text


def test_a_clean_file_says_nothing(tmp_path, caplog):
    """Noise on every boot would train operators to ignore this."""
    path = _env(tmp_path, "# comment\nAPI_KEY=fine\n\nexport OTHER='value'\n")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _warn_env_keys_dropped_by_the_parser(path)

    assert caplog.text == ""


def test_the_message_says_the_setting_is_absent(tmp_path, caplog):
    """"Present in the file" is exactly the wrong conclusion to leave available."""
    path = _env(tmp_path, 'A="x"B="y"\n')
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _warn_env_keys_dropped_by_the_parser(path)

    assert "ABSENT" in caplog.text


def test_a_missing_file_is_not_an_error(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _warn_env_keys_dropped_by_the_parser(str(tmp_path / "nope.env"))
    assert caplog.text == ""


@pytest.mark.parametrize(
    "body",
    [
        "COMMENTED=1\n# NOT_A_KEY=2\n",
        "SPACED = 1\n",
        "export EXPORTED=1\n",
    ],
)
def test_ordinary_shapes_do_not_trip_it(tmp_path, caplog, body):
    """False positives here cost more than the check saves."""
    path = _env(tmp_path, body)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _warn_env_keys_dropped_by_the_parser(path)
    assert caplog.text == ""


def test_a_quoted_key_is_reported_because_dotenv_really_drops_it(tmp_path, caplog):
    """Not a false positive: python-dotenv does not accept a quoted key name.

    The duplicate check next door normalises `"FOO"=bar` on purpose, because
    other .env parsers accept that form. Ours does not, so the setting really
    is absent at runtime and saying so is the whole point.
    """
    path = _env(tmp_path, '"QUOTED_KEY"=1\n')
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _warn_env_keys_dropped_by_the_parser(path)

    assert "QUOTED_KEY" in caplog.text


def test_a_multi_line_quoted_value_is_not_read_as_declarations(tmp_path, caplog):
    """A PEM key spans lines and its body contains `=`.

    Reading those continuation lines as declarations invented one warning per
    line -- precisely the noise `test_a_clean_file_says_nothing` exists to
    prevent. Reported by review before this check ever shipped.
    """
    path = _env(
        tmp_path,
        'API_KEY=fine\n'
        'PRIVATE_KEY="-----BEGIN KEY-----\n'
        'abc=def\n'
        'ghi==\n'
        '-----END KEY-----"\n'
        'OTHER=fine\n',
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _warn_env_keys_dropped_by_the_parser(path)

    assert caplog.text == "", caplog.text


def test_base64_padding_inside_a_quoted_value_stays_quiet(tmp_path, caplog):
    path = _env(tmp_path, 'BLOB="aGVsbG8=\nd29ybGQ=="\nAFTER=fine\n')
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _warn_env_keys_dropped_by_the_parser(path)

    assert caplog.text == ""


def test_a_real_problem_after_a_multi_line_value_is_still_caught(tmp_path, caplog):
    """Skipping continuation lines must not skip what follows them."""
    path = _env(
        tmp_path,
        'PRIVATE_KEY="-----BEGIN KEY-----\nabc=def\n-----END KEY-----"\n'
        'GOOD="x"BAD="y"\n',
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _warn_env_keys_dropped_by_the_parser(path)

    assert "GOOD" in caplog.text
    assert "line 4" in caplog.text


def test_the_second_key_of_a_crammed_line_is_named(tmp_path, caplog):
    """It is the more damaging of the two: absent entirely, not merely wrong."""
    path = _env(tmp_path, 'GOOGLE_WEBHOOK_AUDIENCE="tbd"NOTIFICATION_EMAIL="ops@x.fr"\n')
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _warn_env_keys_dropped_by_the_parser(path)

    assert "GOOGLE_WEBHOOK_AUDIENCE" in caplog.text
    assert "NOTIFICATION_EMAIL" in caplog.text
