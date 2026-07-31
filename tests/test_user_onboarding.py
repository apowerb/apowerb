"""Onboarding par user (flag serveur) — schema User/UserUpdate.

Bug : le modal de bienvenue etait gate UNIQUEMENT sur localStorage (par navigateur,
pas par compte) ; `user.onboarding_completed` etait du code mort (colonne absente).
On materialise le flag serveur pour un onboarding par compte.
"""
from datetime import datetime
from apowerb.users.schemas import User, UserUpdate


def test_user_schema_expose_onboarding_defaut_false():
    u = User(user_id=1, first_name="A", last_name="B", email="a@b.com", role="USER",
             created_at=datetime.now())
    assert u.onboarding_completed is False


def test_user_schema_lit_onboarding_true():
    u = User(user_id=1, first_name="A", last_name="B", email="a@b.com", role="USER",
             onboarding_completed=True)
    assert u.onboarding_completed is True


def test_userupdate_accepte_onboarding():
    upd = UserUpdate(onboarding_completed=True)
    assert upd.model_dump(exclude_unset=True) == {"onboarding_completed": True}


def test_userupdate_n_inclut_pas_onboarding_si_absent():
    # PATCH d'un autre champ ne doit PAS toucher onboarding_completed
    upd = UserUpdate(first_name="X")
    assert "onboarding_completed" not in upd.model_dump(exclude_unset=True)
