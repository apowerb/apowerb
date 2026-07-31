"""L'algorithme de signature des JWT n'a qu'une source de vérité.

Il y en avait deux : la constante ``helpers/security.py::ALGORITHM`` servait à
signer (jetons d'accès, de rafraîchissement, de téléchargement, MFA, reset de
mot de passe), pendant que ``main.py``, ``auth/dependencies``, ``auth/service``
et ``routers/audio_stream`` vérifiaient avec ``settings.algorithm``, lui
configurable par ``ALGORITHM`` dans l'environnement.

Les deux valent ``HS256``, donc rien n'a jamais cassé. C'est précisément le
problème : le jour où quelqu'un pose ``ALGORITHM=HS512`` — ce que la présence
même du réglage invite à faire — la moitié qui signe et la moitié qui vérifie
divergent, tous les jetons deviennent invalides, et aucun test ne l'annonce
puisque chaque moitié reste cohérente avec elle-même.

Ces tests verrouillent l'unicité, pas la valeur : ``HS512`` doit marcher.
"""

from __future__ import annotations

import pytest
from jose import jwt

from apowerb.configs.settings import get_settings
from apowerb.helpers import security
from apowerb.helpers.security import create_access_token, get_algorithm, get_secret_key


class TestSourceUnique:
    def test_l_ancienne_constante_explique_sa_disparition(self):
        """Un ``AttributeError`` sec enverrait chercher au mauvais endroit."""
        with pytest.raises(AttributeError, match="get_algorithm"):
            security.ALGORITHM

    def test_signature_et_vérification_suivent_le_même_réglage(self, monkeypatch):
        """Le test qui aurait attrapé la divergence.

        On déplace le réglage et on vérifie qu'un jeton fraîchement signé reste
        lisible : si un seul site gardait la constante, ce décodage échouerait.
        """
        monkeypatch.setattr(get_settings(), "algorithm", "HS512", raising=False)

        jeton = create_access_token({"sub": "u1"})

        entête = jwt.get_unverified_header(jeton)
        assert entête["alg"] == "HS512", "la signature ignore le réglage"

        charge = jwt.decode(jeton, get_secret_key(), algorithms=[get_algorithm()])
        assert charge["sub"] == "u1"

    def test_un_jeton_signé_avec_l_autre_algorithme_est_refusé(self, monkeypatch):
        """Contre-épreuve : la vérification ne doit pas tout accepter."""
        monkeypatch.setattr(get_settings(), "algorithm", "HS256", raising=False)
        jeton_hs256 = create_access_token({"sub": "u1"})

        monkeypatch.setattr(get_settings(), "algorithm", "HS512", raising=False)
        with pytest.raises(Exception):
            jwt.decode(jeton_hs256, get_secret_key(), algorithms=[get_algorithm()])


class TestFamilleHMACSeulement:
    """La clé est un secret symétrique (``ENCRYPT_KEY``), pas une paire.

    Le réglage était jusqu'ici une chaîne libre. ``none`` désactive la
    vérification ; ``RS256`` ferait passer le secret pour une clé publique.
    Aucun des deux ne doit être atteignable par une simple variable
    d'environnement.
    """

    @pytest.mark.parametrize("algorithme", ["none", "None", "RS256", "ES256", "", "HS128"])
    def test_les_algorithmes_hors_famille_sont_refusés(self, monkeypatch, algorithme):
        monkeypatch.setattr(get_settings(), "algorithm", algorithme, raising=False)
        with pytest.raises(RuntimeError, match="n'est pas supporté"):
            get_algorithm()

    @pytest.mark.parametrize("algorithme", ["HS256", "HS384", "HS512"])
    def test_la_famille_hmac_passe(self, monkeypatch, algorithme):
        monkeypatch.setattr(get_settings(), "algorithm", algorithme, raising=False)
        assert get_algorithm() == algorithme

    def test_le_défaut_reste_hs256(self):
        """Aucun déploiement existant ne change de comportement."""
        assert get_settings().model_fields["algorithm"].default == "HS256"
