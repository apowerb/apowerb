"""Les invariants de sécurité de l'écran de configuration, éprouvés.

Écriture seule, superadministrateur seul, liste fermée, rien dans les
journaux. Chacun a son test, et chacun échoue pour la bonne raison : on
pose une valeur reconnaissable et on la cherche partout où elle pourrait
ressortir, plutôt que de vérifier qu'une fonction a été appelée.
"""

from __future__ import annotations

import logging
import os
import stat
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from apowerb.core.config_admin import overlay, store
from apowerb.core.config_admin import router as router_module
from apowerb.models import UserRole

# Reconnaissable, et manifestement pas un vrai secret : ce dépôt est public
# et une fixture y reste pour toujours.
TEMOIN = "valeur-temoin-a-ne-jamais-ressortir"


def _result(**attrs):
    result = MagicMock()
    for name, value in attrs.items():
        getattr(result, name).return_value = value
    return result


def _db(rows=(), superadmins=1):
    """Une session dont `execute` s'attend et dont le Result est synchrone,
    comme un vrai Result SQLAlchemy (cf. test_admin_router_rules)."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(all=list(rows), scalar=superadmins))
    return db


def _superadmin(email="chef@example.com"):
    u = MagicMock()
    u.role = UserRole.ADMIN.value
    u.email = email
    return u


@pytest.fixture
def chiffreur(monkeypatch):
    """Le vrai Fernet demande ENCRYPT_KEY ; ce qui se teste ici est ce que
    le module fait de la valeur, pas la cryptographie de la bibliothèque."""
    vus: list[str] = []

    def _encrypt(value: str) -> str:
        vus.append(value)
        return "gAAAAA-chiffre"

    monkeypatch.setattr(
        "apowerb.core.config_admin.store.encryptor.encrypt_value", _encrypt
    )
    return vus


# --------------------------------------------------------------------------
# Superadministrateur seul
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["USER", "user", "", None])
async def test_un_non_admin_est_refuse(role):
    from apowerb.admin.guard import require_superadmin

    user = MagicMock()
    user.role = role
    user.email = "quidam@example.com"
    with pytest.raises(HTTPException) as refus:
        await require_superadmin(db=_db(), current_user=user)
    assert refus.value.status_code == 403


@pytest.mark.asyncio
async def test_un_admin_non_superadmin_est_refuse():
    """ADMIN ne suffit pas. `admin_superadmin` est peuplée (count = 2) et
    la jointure sur l'e-mail ne rend rien : cet admin n'y est pas."""
    from apowerb.admin.guard import require_superadmin

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(scalar=2, first=None))
    with pytest.raises(HTTPException) as refus:
        await require_superadmin(db=db, current_user=_superadmin("simple@example.com"))
    assert refus.value.status_code == 403


@pytest.mark.asyncio
async def test_ecrire_est_refuse_tant_qu_aucun_superadmin_n_est_nomme():
    """Le repli d'amorçage de `is_superadmin` (tout admin en est un tant que
    la table est vide) est acceptable pour créer une organisation, pas pour
    poser une clé d'API : il ferait de « superadmin seul » un invariant qui
    ne tient justement pas sur une installation neuve."""
    with pytest.raises(HTTPException) as refus:
        await router_module._assert_superadmin_named(_db(superadmins=0))
    assert refus.value.status_code == 409
    assert "DEFAULT_SUPERADMIN_EMAIL" in refus.value.detail


# --------------------------------------------------------------------------
# Liste fermée
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nom", ["ENCRYPT_KEY", "DB_PASSWORD", "BYPASS_AUTH", "encrypt_key", "N_IMPORTE_QUOI"]
)
async def test_une_variable_hors_liste_est_refusee(nom):
    with pytest.raises(HTTPException) as refus:
        await router_module._assert_writable(nom)
    # 404 et pas 403 : répondre « interdit » sur ENCRYPT_KEY et « inconnu »
    # sur N_IMPORTE_QUOI dirait au sondeur lesquels de ses noms existent ici.
    assert refus.value.status_code == 404


@pytest.mark.asyncio
async def test_une_variable_hors_liste_n_ecrit_rien():
    """Le refus doit tomber AVANT la moindre écriture. Un 404 rendu après
    l'INSERT protégerait l'affichage, pas la base."""
    db = _db()
    with pytest.raises(HTTPException):
        await router_module.put_variable(
            name="ENCRYPT_KEY",
            payload=router_module.ValueIn(value=TEMOIN),
            db=db,
            current_user=_superadmin(),
        )
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


# --------------------------------------------------------------------------
# Écriture seule
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_la_reponse_a_une_ecriture_ne_contient_jamais_la_valeur(chiffreur):
    db = _db(rows=[("SMTP_HOST", None, "chef@example.com")])
    reponse = await router_module.put_variable(
        name="SMTP_HOST",
        payload=router_module.ValueIn(value=TEMOIN),
        db=db,
        current_user=_superadmin(),
    )
    assert TEMOIN not in reponse.model_dump_json()
    # La valeur est bien arrivée au chiffreur : le test ne passe pas parce
    # que rien ne s'est produit.
    assert chiffreur == [TEMOIN]


def test_le_modele_de_sortie_n_a_aucun_champ_de_valeur():
    """`extra="forbid"` : personne ne pourra ajouter un champ `value` plus
    tard sans que ce test le dise."""
    champs = set(router_module.VariableOut.model_fields)
    assert not (champs & {"value", "value_enc", "secret_value", "masked"})
    with pytest.raises(Exception):
        router_module.VariableOut(
            name="SMTP_HOST", capability="system_mail", secret=True,
            source="database", value=TEMOIN,
        )


def test_aucune_route_ne_peut_dechiffrer():
    """La seule fonction du store capable de déchiffrer appartient à
    l'entrypoint (`overlay.py`), pas au serveur HTTP.

    Sur les identifiants que le module UTILISE, pas sur son texte : une
    garde qui lirait le fichier entier se déclencherait sur le commentaire
    qui explique la règle, et on la relâcherait pour cette raison. Ici,
    ajouter l'import ferait échouer le test ; en parler, non.
    """
    import ast
    import inspect

    arbre = ast.parse(inspect.getsource(router_module))
    identifiants = {
        n.id for n in ast.walk(arbre) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(arbre) if isinstance(n, ast.Attribute)
    } | {
        alias.asname or alias.name
        for n in ast.walk(arbre)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for alias in n.names
    }
    assert "read_all_decrypted" not in identifiants
    assert not [i for i in identifiants if "decrypt" in i.lower()]


# --------------------------------------------------------------------------
# Rien dans les journaux
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aucun_journal_ne_contient_la_valeur(chiffreur, caplog):
    """Tout l'appel sous capture, au niveau le plus bas. On fouille le
    message rendu ET les attributs `extra` : l'audit passe par `extra`, et
    un `caplog.text` seul ne les verrait pas."""
    caplog.set_level(logging.DEBUG)
    await router_module.put_variable(
        name="SMTP_HOST",
        payload=router_module.ValueIn(value=TEMOIN),
        db=_db(rows=[("SMTP_HOST", None, "chef@example.com")]),
        current_user=_superadmin(),
    )
    assert TEMOIN not in caplog.text
    for record in caplog.records:
        assert TEMOIN not in str(record.__dict__)


@pytest.mark.asyncio
async def test_l_audit_porte_le_nom_et_l_acteur_jamais_la_valeur(chiffreur, caplog):
    caplog.set_level(logging.INFO, logger="apowerb.audit")
    await router_module.put_variable(
        name="SMTP_HOST",
        payload=router_module.ValueIn(value=TEMOIN),
        db=_db(rows=[("SMTP_HOST", None, "chef@example.com")]),
        current_user=_superadmin(),
    )
    audits = [r for r in caplog.records if getattr(r, "audit", False)]
    assert audits, "aucun enregistrement d'audit émis"
    enregistrement = audits[0]
    assert enregistrement.event == "config.set"
    assert enregistrement.variable == "SMTP_HOST"
    assert enregistrement.user_id == "chef@example.com"
    assert TEMOIN not in str(enregistrement.__dict__)


# --------------------------------------------------------------------------
# Précédence : l'environnement gagne
# --------------------------------------------------------------------------


def test_l_environnement_gagne_sur_la_base():
    lignes = overlay.overlay_lines(
        {"SMTP_HOST": "depuis-la-base", "SMTP_PORT": "587"},
        env={"SMTP_HOST": "depuis-le-deploiement"},
    )
    # Le marqueur accompagne l'export : il nomme ce que l'overlay a posé, et
    # c'est ce qui permet au processus de ne pas confondre sa propre empreinte
    # avec celle du déploiement au redémarrage suivant.
    assert lignes == ["SMTP_PORT='587'", f"{store.OVERLAY_MARKER}='SMTP_PORT'"]


def test_une_variable_declaree_vide_ne_reprend_pas_la_main():
    """Une variable présente mais vide se lit comme configurée et se
    comporte comme rien — le plus vieux piège de ce dépôt. Elle ne doit
    donc PAS faire taire l'overlay."""
    lignes = overlay.overlay_lines({"SMTP_HOST": "depuis-la-base"}, env={"SMTP_HOST": "  "})
    assert lignes == ["SMTP_HOST='depuis-la-base'", f"{store.OVERLAY_MARKER}='SMTP_HOST'"]


def test_env_holds_ignore_une_variable_vide(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "   ")
    assert not store.env_holds("SMTP_HOST")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    assert store.env_holds("SMTP_HOST")


def test_le_quotage_survit_a_une_apostrophe():
    """Le fichier est sourcé par le shell de l'entrypoint : une apostrophe
    mal échappée y ouvre une chaîne et avale les lignes suivantes."""
    ligne = overlay.overlay_lines({"SMTP_USER": "o'brien"}, env={})[0]
    assert ligne == "SMTP_USER='o'\\''brien'"


def test_le_fichier_d_overlay_est_cree_en_0600(tmp_path, monkeypatch):
    """0600 posé À LA CRÉATION : créer puis protéger laisse une fenêtre
    pendant laquelle le contenu est lisible par toute la machine."""
    monkeypatch.setattr(
        overlay, "read_all_decrypted", lambda schema, conn: {"SMTP_PORT": "587"}
    )
    monkeypatch.setattr(overlay, "create_engine", lambda *a, **k: _engine_bidon())
    cible = tmp_path / "overlay.env"
    noms = overlay.write_overlay(cible)

    assert noms == ["SMTP_PORT"]
    assert stat.S_IMODE(os.stat(cible).st_mode) == 0o600
    assert cible.read_text() == "SMTP_PORT='587'\nAPOWERB_CONFIG_APPLIED='SMTP_PORT'\n"


def _engine_bidon():
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = MagicMock()
    engine.connect.return_value.__exit__.return_value = False
    return engine


def test_read_all_decrypted_ignore_une_ligne_hors_liste(monkeypatch):
    """La liste fermée protège la sortie comme l'entrée : une ligne posée
    par du SQL direct, ou restaurée d'une sauvegarde dont la liste était
    plus large, ne devient pas une variable d'environnement."""
    monkeypatch.setattr(
        "apowerb.core.config_admin.store.encryptor.decrypt_value", lambda c: "clair"
    )
    conn = MagicMock()
    conn.execute.return_value.all.return_value = [
        ("SMTP_HOST", "chiffre"),
        ("ENCRYPT_KEY", "chiffre"),
        ("DB_PASSWORD", "chiffre"),
    ]
    assert store.read_all_decrypted("public", conn) == {"SMTP_HOST": "clair"}
