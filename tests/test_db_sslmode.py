"""Le mode TLS de la connexion Postgres doit être configurable.

``ToolConfigStore`` construisait son moteur avec ``?sslmode=require`` en dur.
Conséquence découverte en montant la pile Docker open source : un
auto-hébergeur avec un Postgres local sans TLS **ne peut pas démarrer** —
``server does not support SSL, but SSL was required``, en boucle.

Tout le reste du code lisait déjà un ``db_sslmode`` configurable ; ce site était
le seul à l'imposer. Le défaut reste ``require`` : les déploiements existants
tournent contre un Postgres géré qui l'exige, et rien ne change pour eux.
"""

from __future__ import annotations

from pathlib import Path

NOYAU = Path(__file__).resolve().parents[1] / "src" / "apowerb"


def test_aucun_sslmode_en_dur_dans_le_noyau():
    """Le verrou. Un seul site suffit à rendre l'auto-hébergement impossible."""
    fautifs = []
    for fichier in NOYAU.rglob("*.py"):
        for num, ligne in enumerate(fichier.read_text(encoding="utf-8").splitlines(), 1):
            if "sslmode=require" in ligne and not ligne.strip().startswith("#"):
                fautifs.append(f"{fichier.relative_to(NOYAU)}:{num}: {ligne.strip()}")
    assert not fautifs, (
        "sslmode impose en dur — un Postgres local sans TLS ne pourra pas "
        "demarrer :\n  " + "\n  ".join(fautifs)
    )


def test_le_defaut_reste_require():
    """Aucun deploiement existant ne change de comportement."""
    from apowerb.configs.settings import Settings

    assert Settings.model_fields["db_sslmode"].default == "require"


def test_le_reglage_est_honore(monkeypatch):
    from apowerb.configs.settings import get_settings

    monkeypatch.setattr(get_settings(), "db_sslmode", "disable", raising=False)
    assert get_settings().db_sslmode == "disable"
