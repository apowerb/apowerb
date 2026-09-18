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
        "tool_get_pull_request", "tool_index_repository_docs", "tool_list_pull_requests",
        "tool_list_repositories", "tool_read_file", "tool_search_code",
    ]


# ---------------------------------------------------------------- indexation RAG

def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _repo_handler(tree, files, default_branch="main", truncated=False):
    def _handler(url, params, headers):
        if url.endswith("/repos/acme/data"):
            return _Resp(200, {"default_branch": default_branch})
        if "/git/trees/" in url:
            return _Resp(200, {"tree": tree, "truncated": truncated})
        path = url.split("/contents/", 1)[1]
        if path in files:
            return _Resp(200, {"type": "file", "path": path, "encoding": "base64", "content": files[path]})
        return _Resp(404, {"message": "Not Found"})
    return _handler


@pytest.fixture()
def rag_spy(monkeypatch):
    """Remplace l'indexation : lit les fichiers AU MOMENT de l'appel, comme le vrai envoi."""
    seen = {"calls": []}

    def _create(name, description, files, prompt="", wait_for_completion=True, **kw):
        contents = {}
        for f in files:
            with open(f, encoding="utf-8") as fh:
                contents[f.rsplit("/", 1)[-1]] = fh.read()
        seen["calls"].append({"name": name, "description": description, "files": list(files),
                              "contents": contents, "wait": wait_for_completion})
        return {"status": "complete", "knowledge_id": "kb-42", "message": "ok"}

    monkeypatch.setattr(gh._rag, "tool_create_knowledge", _create)
    return seen


def test_indexe_la_documentation_d_un_depot(tokens, monkeypatch, rag_spy):
    tree = [
        {"path": "README.md", "type": "blob", "size": 40},
        {"path": "docs/guide/intro.md", "type": "blob", "size": 30},
        {"path": "docs/logo.png", "type": "blob", "size": 900},
        {"path": "node_modules/pkg/README.md", "type": "blob", "size": 20},
        {"path": "src/app.py", "type": "blob", "size": 50},
        {"path": "docs", "type": "tree"},
    ]
    files = {"README.md": _b64("# Data\nLe depot des modeles."), "docs/guide/intro.md": _b64("Intro au guide.")}
    calls = _route(monkeypatch, _repo_handler(tree, files))

    out = gh.tool_index_repository_docs("acme/data")

    assert out["status"] == "complete"
    assert out["knowledge_id"] == "kb-42"
    assert out["ref"] == "main"
    assert out["indexed_files"] == ["README.md", "docs/guide/intro.md"]
    assert out["skipped"]["excluded_folder"] == 1
    assert out["skipped"]["other_extension"] == 2
    # Un seul appel d'indexation, avec un fichier par document et sa source en tete.
    [call] = rag_spy["calls"]
    assert call["name"] == "GitHub acme/data"
    assert len(call["files"]) == 2
    intro = next(v for k, v in call["contents"].items() if "intro" in k)
    assert intro.startswith("Source: https://github.com/acme/data/blob/main/docs/guide/intro.md")
    assert intro.rstrip().endswith("Intro au guide.")
    # Rien ne subsiste sur le disque une fois l'envoi fait.
    import os
    assert not any(os.path.exists(f) for f in call["files"])
    # Les dependances ne sont jamais telechargees.
    assert not any("node_modules" in c["url"] for c in calls)


def test_un_sous_dossier_et_une_branche_restreignent_l_indexation(tokens, monkeypatch, rag_spy):
    tree = [
        {"path": "README.md", "type": "blob", "size": 40},
        {"path": "docs/intro.md", "type": "blob", "size": 30},
    ]
    files = {"docs/intro.md": _b64("Intro.")}
    calls = _route(monkeypatch, _repo_handler(tree, files))

    out = gh.tool_index_repository_docs("acme/data", path="docs", ref="v2", knowledge_name="Docs v2")

    assert out["indexed_files"] == ["docs/intro.md"]
    assert rag_spy["calls"][0]["name"] == "Docs v2"
    assert any("/git/trees/v2" in c["url"] for c in calls)
    assert not any(c["url"].endswith("/repos/acme/data") for c in calls)


def test_aucun_document_eligible_ne_cree_pas_de_base_vide(tokens, monkeypatch, rag_spy):
    tree = [{"path": "src/app.py", "type": "blob", "size": 50}]
    _route(monkeypatch, _repo_handler(tree, {}))
    out = gh.tool_index_repository_docs("acme/data")
    assert out["status"] == "empty"
    assert rag_spy["calls"] == []


def test_les_fichiers_trop_gros_et_binaires_sont_ecartes_et_comptes(tokens, monkeypatch, rag_spy):
    tree = [
        {"path": "big.md", "type": "blob", "size": gh._MAX_DOC_BYTES + 1},
        {"path": "weird.txt", "type": "blob", "size": 10},
        {"path": "ok.md", "type": "blob", "size": 10},
    ]
    binary = base64.b64encode(bytes([0xFF, 0xFE, 0x00, 0x81])).decode()
    files = {"weird.txt": binary, "ok.md": _b64("ok")}
    _route(monkeypatch, _repo_handler(tree, files))
    out = gh.tool_index_repository_docs("acme/data")
    assert out["indexed_files"] == ["ok.md"]
    assert out["skipped"]["too_large"] == 1
    assert out["skipped"]["binary"] == 1


def test_le_plafond_de_fichiers_est_respecte_et_signale(tokens, monkeypatch, rag_spy):
    tree = [{"path": f"doc{i:02d}.md", "type": "blob", "size": 5} for i in range(5)]
    files = {f"doc{i:02d}.md": _b64(f"doc {i}") for i in range(5)}
    _route(monkeypatch, _repo_handler(tree, files))
    out = gh.tool_index_repository_docs("acme/data", max_files=3)
    assert len(out["indexed_files"]) == 3
    assert out["skipped"]["over_limit"] == 2


def test_un_echec_d_indexation_est_rendu_tel_quel(tokens, monkeypatch):
    tree = [{"path": "README.md", "type": "blob", "size": 5}]
    _route(monkeypatch, _repo_handler(tree, {"README.md": _b64("x")}))
    monkeypatch.setattr(gh._rag, "tool_create_knowledge",
                        lambda **kw: {"status": "error", "message": "RAG authentication failed", "retry": False})
    out = gh.tool_index_repository_docs("acme/data")
    assert out["status"] == "error"
    assert "RAG authentication failed" in out["message"]
    assert out["indexed_files"] == ["README.md"]


def test_l_indexation_refuse_un_depot_invalide_sans_appel(tokens, monkeypatch, rag_spy):
    calls = _route(monkeypatch, lambda url, p, h: _Resp(200, {}))
    out = gh.tool_index_repository_docs("pas un depot")
    assert out["status"] == "error"
    assert calls == [] and rag_spy["calls"] == []

