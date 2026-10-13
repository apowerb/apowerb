"""The French build of every template is unchanged by the translation work.

The fixtures were written from main before the templates became bilingual, so a
difference here is an accidental rewording or a behaviour change.
"""

from __future__ import annotations

import json

import pytest

from tests.helpers.bi_template_cases import CASES, TEMPLATE_IDS, build_case, golden_path


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_french_build_matches_the_golden_spec(template_id: str, case: str) -> None:
    expected = json.loads(golden_path(template_id, case).read_text())
    assert json.loads(json.dumps(build_case(template_id, case))) == expected
