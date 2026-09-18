"""La connexion GitHub conserve le jeton de rafraichissement (roadmap#71).

Une GitHub App peut faire expirer ses jetons utilisateur au bout de 8 h. Le
jeton de rafraichissement que GitHub renvoie alors etait jete
(``refresh_token=None``) : les outils seraient tombes en panne le lendemain de
chaque connexion.
"""

from apowerb.integrations import github as integ


class _FakeResult:
    def scalar_one(self):
        return object()


class _FakeDb:
    async def execute(self, _stmt):
        return _FakeResult()


async def test_la_sauvegarde_transmet_le_jeton_de_rafraichissement(monkeypatch):
    saved = {}
    monkeypatch.setattr(integ, "save_integration_tokens", lambda **kw: saved.update(kw))

    await integ.GitHubIntegrationService.save_integration(
        db=_FakeDb(), user_id=9, github_data={"login": "octocat", "id": 1},
        access_token="at", refresh_token="rt", scopes="",
    )
    assert saved["refresh_token"] == "rt"
    assert saved["provider"] == "github"
