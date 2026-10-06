"""`/api/config` tells the interface whether a second factor can be enrolled.

The admin panel offers "Require MFA" only when it is true: without the MFA
brick the demand is refused (409), so the button would only lead to an error.
"""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("ENCRYPT_KEY", "test-only-key-not-used-anywhere-else")

from apowerb.core.extensions.registry import registry as extension_registry  # noqa: E402
from apowerb.routers import config as config_module  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize("loaded", [False, True])
async def test_public_config_reports_whether_mfa_is_available(loaded):
    factor = (lambda user: None) if loaded else None
    with patch.object(extension_registry, "second_factor", return_value=factor):
        cfg = await config_module.get_public_config()

    assert cfg["mfa_available"] is loaded
