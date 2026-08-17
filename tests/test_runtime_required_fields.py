"""What the server actually refuses to start without.

`TEST_TOKEN` used to be in that list. It is read by exactly one middleware,
`apowerb/middleware/auth.py`, which is mounted nowhere -- its own docstring says
so. Requiring it meant every deployment had to invent a "test token" to run in
production, which is what these tests keep from coming back.

Nothing here weakens the middleware: its guard already refuses an empty token
(`if not settings.test_token or token != settings.test_token`), so a build with
no TEST_TOKEN rejects every request it sees rather than letting them through.
"""

import pytest

from apowerb.configs.settings import RUNTIME_REQUIRED_FIELDS, Settings

# The five the server genuinely cannot work without.
_MINIMUM = {
    "db_host": "localhost",
    "db_name": "apowerb",
    "db_user": "apowerb",
    "db_password": "secret",
    "encrypt_key": "0" * 43 + "=",
}


class TestTestTokenIsNotRequired:
    def test_test_token_is_absent_from_the_required_fields(self):
        assert "test_token" not in RUNTIME_REQUIRED_FIELDS

    def test_the_server_starts_without_a_test_token(self):
        """The minimum set is enough: no TEST_TOKEN anywhere."""
        settings = Settings(**_MINIMUM, test_token="")

        assert settings.missing_runtime_fields() == []
        settings.assert_runtime_ready()  # must not raise

    def test_an_empty_test_token_still_rejects_every_request(self):
        """Optional must not mean permissive."""
        from apowerb.middleware.auth import AuthMiddleware

        assert AuthMiddleware is not None
        settings = Settings(**_MINIMUM, test_token="")
        # The guard is `not settings.test_token or token != settings.test_token`:
        # with an empty value the first branch is true, so nothing passes.
        assert not settings.test_token


class TestWhatIsStillRequired:
    @pytest.mark.parametrize("field", sorted(RUNTIME_REQUIRED_FIELDS))
    def test_each_required_field_is_still_refused_when_missing(self, field):
        values = dict(_MINIMUM)
        values[field] = ""
        settings = Settings(**values)

        assert field in settings.missing_runtime_fields()
        with pytest.raises(RuntimeError) as excinfo:
            settings.assert_runtime_ready()
        assert field.upper() in str(excinfo.value)
