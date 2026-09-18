"""Outils GitHub en lecture (apowerb/roadmap#71, lot 1).

Aucun appel reseau : ``httpx`` et la lecture du jeton sont remplaces. Ce qui est
verifie, c'est le contrat vu par le modele — forme des resultats, statuts
d'integration exploitables — et la resolution du jeton pour l'utilisateur qui
invoque l'agent.
"""

import base64

import pytest

from apowerb.tools_store.portfolio import github as gh


class _Resp:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        return self._payload


@pytest.fixture()
def tokens(monkeypatch):
    """Integration connectee ; memorise les providers demandes et les persistances."""
    state = {"asked": [], "persisted": [], "stored": {"access_token": "at-1", "refresh_token": "rt-1"}}

    def _fetch(provider, user=None):
        state["asked"].append(provider)
        return dict(state["stored"])

    def _persist(provider, *, access_token=None, refresh_token=None, scope=None, user=None):
        state["persisted"].append((provider, access_token, refresh_token))
        return True

    monkeypatch.setattr("apowerb.integrations.helpers.fetch_integration_configs", _fetch)
    monkeypatch.setattr("apowerb.integrations.helpers.persist_refreshed_tokens", _persist)
    return state


def _route(monkeypatch, handler):
    calls = []

    def _get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "auth": (headers or {}).get("Authorization"), "params": params or {}})
        return handler(url, params or {}, headers or {})

    monkeypatch.setattr("httpx.get", _get)
    return calls


# ---------------------------------------------------------------- jeton

def test_sans_integration_le_modele_recoit_un_statut_exploitable(monkeypatch):
    def _absent(provider, user=None):
        raise RuntimeError(f"No {provider} integration found for user_id=9.")

    monkeypatch.setattr("apowerb.integrations.helpers.fetch_integration_configs", _absent)
    out = gh.tool_list_repositories()
    assert out["status"] == "integration_status"
    assert out["code"] == "INTEGRATION_MISSING"
    assert out["provider"] == "github"


def test_le_jeton_est_celui_de_l_integration_github_de_l_invocateur(tokens, monkeypatch):
    calls = _route(monkeypatch, lambda url, p, h: _Resp(200, []))
    gh.tool_list_repositories()
    assert tokens["asked"] == ["github"]
    assert calls[0]["auth"] == "Bearer at-1"


def test_un_401_rafraichit_le_jeton_une_fois_et_persiste_la_paire_tournee(tokens, monkeypatch):
    def _handler(url, params, headers):
        if headers.get("Authorization") == "Bearer at-1":
            return _Resp(401, {"message": "Bad credentials"})
        return _Resp(200, [{"full_name": "acme/data", "private": True}])

    calls = _route(monkeypatch, _handler)
    posted = []

    def _post(url, data=None, headers=None, timeout=None):
        posted.append(data)
        return _Resp(200, {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 28800})

    monkeypatch.setattr("httpx.post", _post)
    out = gh.tool_list_repositories()

    assert out["status"] == "success"
    assert [c["auth"] for c in calls] == ["Bearer at-1", "Bearer at-2"]
    assert posted[0]["grant_type"] == "refresh_token"
    assert posted[0]["refresh_token"] == "rt-1"
    assert tokens["persisted"] == [("github", "at-2", "rt-2")]


def test_un_401_sans_jeton_de_rafraichissement_demande_de_reconnecter(tokens, monkeypatch):
    tokens["stored"] = {"access_token": "at-1", "refresh_token": None}
    _route(monkeypatch, lambda url, p, h: _Resp(401, {"message": "Bad credentials"}))
    out = gh.tool_list_repositories()
    assert out["code"] == "INTEGRATION_EXPIRED"
    assert out["remediable_by_reconnect"] is True


def test_un_jeton_de_rafraichissement_refuse_demande_de_reconnecter(tokens, monkeypatch):
    _route(monkeypatch, lambda url, p, h: _Resp(401, {"message": "Bad credentials"}))
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(200, {"error": "bad_refresh_token"}))
    out = gh.tool_list_repositories()
    assert out["code"] == "INTEGRATION_EXPIRED"


# ---------------------------------------------------------------- depots

def test_liste_les_depots_et_filtre_sur_la_requete(tokens, monkeypatch):
    repos = [
        {"full_name": "acme/data-models", "private": True, "description": "Modeles dbt",
         "default_branch": "main", "updated_at": "2026-09-01T00:00:00Z", "html_url": "https://github.com/acme/data-models"},
        {"full_name": "acme/site", "private": False, "description": "Site vitrine",
         "default_branch": "main", "updated_at": "2026-08-01T00:00:00Z", "html_url": "https://github.com/acme/site"},
    ]
    _route(monkeypatch, lambda url, p, h: _Resp(200, repos))
    out = gh.tool_list_repositories(query="dbt")
    assert out["status"] == "success"
    assert [r["full_name"] for r in out["repositories"]] == ["acme/data-models"]
    assert out["repositories"][0]["private"] is True


# ---------------------------------------------------------------- fichiers

def test_lit_un_fichier_texte(tokens, monkeypatch):
    body = "SELECT region, SUM(ca) FROM ventes GROUP BY region;\n"
    payload = {"type": "file", "path": "sql/ca.sql", "size": len(body), "encoding": "base64",
               "content": base64.b64encode(body.encode()).decode(), "html_url": "https://github.com/acme/data/blob/main/sql/ca.sql"}
    calls = _route(monkeypatch, lambda url, p, h: _Resp(200, payload))
    out = gh.tool_read_file("acme/data", "sql/ca.sql", ref="main")
    assert out["status"] == "success"
    assert out["content"] == body
    assert out["truncated"] is False
    assert calls[0]["url"].endswith("/repos/acme/data/contents/sql/ca.sql")
    assert calls[0]["params"] == {"ref": "main"}


def test_liste_un_dossier(tokens, monkeypatch):
    payload = [{"name": "ca.sql", "path": "sql/ca.sql", "type": "file", "size": 52},
               {"name": "marts", "path": "sql/marts", "type": "dir", "size": 0}]
    _route(monkeypatch, lambda url, p, h: _Resp(200, payload))
    out = gh.tool_read_file("acme/data", "sql")
    assert out["status"] == "success"
    assert out["type"] == "directory"
    assert [e["path"] for e in out["entries"]] == ["sql/ca.sql", "sql/marts"]


def test_un_fichier_binaire_n_est_pas_recrache_en_octets(tokens, monkeypatch):
    raw = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0xFF, 0xFE])
    payload = {"type": "file", "path": "logo.png", "size": len(raw), "encoding": "base64",
               "content": base64.b64encode(raw).decode()}
    _route(monkeypatch, lambda url, p, h: _Resp(200, payload))
    out = gh.tool_read_file("acme/data", "logo.png")
    assert out["status"] == "success"
    assert out["binary"] is True
    assert "content" not in out


def test_un_gros_fichier_est_tronque_et_le_dit(tokens, monkeypatch):
    body = "x" * (gh._MAX_FILE_CHARS + 500)
    payload = {"type": "file", "path": "big.txt", "size": len(body), "encoding": "base64",
               "content": base64.b64encode(body.encode()).decode()}
    _route(monkeypatch, lambda url, p, h: _Resp(200, payload))
    out = gh.tool_read_file("acme/data", "big.txt")
    assert out["truncated"] is True
    assert len(out["content"]) == gh._MAX_FILE_CHARS


def test_un_nom_de_depot_invalide_est_refuse_sans_appel(tokens, monkeypatch):
    calls = _route(monkeypatch, lambda url, p, h: _Resp(200, {}))
    out = gh.tool_read_file("../../etc", "passwd")
    assert out["status"] == "error"
    assert calls == []


def test_un_404_dit_que_l_acces_peut_manquer(tokens, monkeypatch):
    _route(monkeypatch, lambda url, p, h: _Resp(404, {"message": "Not Found"}))
    out = gh.tool_read_file("acme/secret", "README.md")
    assert out["status"] == "error"
    assert out["retry"] is False
    assert "access" in out["message"].lower()


# ---------------------------------------------------------------- code

def test_la_recherche_de_code_se_limite_au_depot_demande(tokens, monkeypatch):
    payload = {"total_count": 1, "items": [{"path": "sql/ca.sql", "repository": {"full_name": "acme/data"},
               "html_url": "https://github.com/acme/data/blob/main/sql/ca.sql",
               "text_matches": [{"fragment": "SUM(ca) FROM ventes"}]}]}
    calls = _route(monkeypatch, lambda url, p, h: _Resp(200, payload))
    out = gh.tool_search_code("ventes", repository="acme/data")
    assert calls[0]["params"]["q"] == "ventes repo:acme/data"
    assert out["results"][0]["path"] == "sql/ca.sql"
    assert out["results"][0]["fragments"] == ["SUM(ca) FROM ventes"]


# ---------------------------------------------------------------- pull requests

def test_liste_les_pull_requests(tokens, monkeypatch):
    prs = [{"number": 12, "title": "Ajoute le mart ventes", "state": "open", "draft": False,
            "user": {"login": "octocat"}, "head": {"ref": "feat/ventes"}, "base": {"ref": "main"},
            "created_at": "2026-09-10T00:00:00Z", "updated_at": "2026-09-11T00:00:00Z",
            "html_url": "https://github.com/acme/data/pull/12"}]
    calls = _route(monkeypatch, lambda url, p, h: _Resp(200, prs))
    out = gh.tool_list_pull_requests("acme/data")
    assert calls[0]["params"]["state"] == "open"
    assert out["pull_requests"][0] == {
        "number": 12, "title": "Ajoute le mart ventes", "state": "open", "draft": False,
        "author": "octocat", "head": "feat/ventes", "base": "main",
        "created_at": "2026-09-10T00:00:00Z", "updated_at": "2026-09-11T00:00:00Z",
        "url": "https://github.com/acme/data/pull/12",
    }


def test_un_etat_de_pull_request_inconnu_est_refuse(tokens, monkeypatch):
    calls = _route(monkeypatch, lambda url, p, h: _Resp(200, []))
    out = gh.tool_list_pull_requests("acme/data", state="merged")
    assert out["status"] == "error"
    assert calls == []


def test_detail_d_une_pull_request_avec_son_diff_borne(tokens, monkeypatch):
    pr = {"number": 12, "title": "Ajoute le mart ventes", "body": "Nouveau modele.", "state": "open",
          "merged": False, "user": {"login": "octocat"}, "head": {"ref": "feat/ventes"}, "base": {"ref": "main"},
          "additions": 40, "deletions": 2, "changed_files": 1, "html_url": "https://github.com/acme/data/pull/12"}
    files = [{"filename": "models/ventes.sql", "status": "added", "additions": 40, "deletions": 2,
              "patch": "+" + "y" * (gh._MAX_PATCH_CHARS + 100)}]

    def _handler(url, params, headers):
        return _Resp(200, files if url.endswith("/files") else pr)

    _route(monkeypatch, _handler)
    out = gh.tool_get_pull_request("acme/data", 12)
    assert out["status"] == "success"
    assert out["pull_request"]["title"] == "Ajoute le mart ventes"
    assert out["files"][0]["filename"] == "models/ventes.sql"
    assert out["files"][0]["patch_truncated"] is True
    assert len(out["files"][0]["patch"]) == gh._MAX_PATCH_CHARS


def test_le_module_n_expose_que_des_outils_de_lecture():
    import inspect

    outils = sorted(n for n, o in inspect.getmembers(gh, inspect.isfunction) if n.startswith("tool_"))
    assert outils == [
        "tool_get_pull_request", "tool_list_pull_requests", "tool_list_repositories",
        "tool_read_file", "tool_search_code",
    ]
