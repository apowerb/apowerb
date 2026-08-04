"""No default setting may carry someone's identity.

This is an open-source package: a default that names a real address or a
company domain makes every installation mail that person, or trust that domain.
Three settings did exactly that until 0.1.8, and the loop it caused
(`owner user not found in DB`) was only noticed by running the published
Compose stack.

The model id `thaink2/default` is not an identity — it is the public name of
the shared-model provider, and renaming it would break every agent using it.
"""

import re

from apowerb.configs.settings import Settings

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# The shared-model provider id, and only that.
ALLOWED = {"thaink2/default"}


def _string_defaults():
    for name, field in Settings.model_fields.items():
        default = field.default
        if isinstance(default, str) and default:
            yield name, default


def test_no_email_address_in_a_default():
    offenders = {
        name: value for name, value in _string_defaults() if EMAIL.search(value)
    }
    assert not offenders, f"email address hardcoded as a default: {offenders}"


def test_no_company_domain_in_a_default():
    offenders = {
        name: value
        for name, value in _string_defaults()
        if "thaink2" in value and value not in ALLOWED
    }
    assert not offenders, f"company domain hardcoded as a default: {offenders}"
