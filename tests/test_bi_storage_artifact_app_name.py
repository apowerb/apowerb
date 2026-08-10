"""BI has no agent concept (organization_id/project_id only), unlike RAG.
bi_artifact_app_name derives a stable, collision-free artifact app_name from
organization_id so the CSV mirror (upload_mirror) has somewhere to write:
the "bi-" prefix keeps it out of the "agent<digits>" namespace real agents
use (apowerb.helpers.ownership / routers.rag.validators), and sanitization
matches build_file_key's own segment rules.
"""

from __future__ import annotations

from apowerb.bi.data._bi_storage import bi_artifact_app_name


class TestBiArtifactAppName:
    def test_prefixes_with_bi(self):
        assert bi_artifact_app_name("thaink2.com") == "bi-thaink2.com"

    def test_sanitizes_like_a_path_segment(self):
        assert bi_artifact_app_name("my org/name") == "bi-my-org-name"

    def test_never_collides_with_a_real_agent_folder(self):
        import re

        assert not re.match(r"^agent\d+$", bi_artifact_app_name("123"))
