"""La liste blanche : fermée, alignée sur la checklist, et sans les noms
qui feraient de cet écran une porte de sabotage.

Ces tests ne touchent ni base ni réseau : le catalogue est une donnée pure,
et c'est délibéré — la garde la plus importante de ce chantier doit pouvoir
s'éprouver sans rien monter.
"""

from __future__ import annotations

import pytest

from apowerb.core import setup_status as ss
from apowerb.core.config_admin import catalog

# Marqueur manifestement factice, jamais une valeur de la forme d'un vrai
# secret : un test ne doit pas apprendre à un lecteur à quoi ressemble une
# clé, et un dépôt public garde ses fixtures pour toujours.
FACTICE = "xxxx-ne-doit-pas-fuir"


class _Settings:
    """Tout est vide : on veut que `capabilities()` nomme un maximum de
    variables manquantes, puisque c'est cette liste de NOMS qu'on compare."""

    default_llm_model = ""
    default_llm_api_key = ""
    storage_mode = "LOCAL"
    s3_bucket_name = ""
    s3_access_key = ""
    s3_access_key_secret = ""
    s3_endpoint = ""
    s3_region = ""
    microsoft_integration_client_id = ""
    microsoft_integration_client_secret = ""
    google_integration_client_id = ""
    google_integration_client_secret = ""
    google_webhook_audience = ""
    orchestrator = "th2etl"
    th2etl_base_url = "http://localhost:8009"
    th2etl_api_key = ""
    model_fields_set: set = set()


def _checklist_names() -> set[str]:
    """Les noms que la checklist annonce comme manquants, `STORAGE_MODE=S3`
    ramené à son nom de variable."""
    names: set[str] = set()
    for item in ss.capabilities(_Settings(), {}):
        for missing in item.missing:
            names.add(missing.split("=", 1)[0])
    return names


def test_le_catalogue_ne_contient_aucun_nom_interdit():
    """La liste des noms dont l'écriture depuis un écran web serait un
    sabotage ou une escalade. Nommés un par un, pas par préfixe : le 04/09,
    c'est un filtre par préfixe qui a déversé un mot de passe ERP."""
    interdits = {
        "ENCRYPT_KEY",
        "BYPASS_AUTH",
        "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT",
        "DB_SCHEMA", "DB_TYPE", "DB_SSLMODE",
        "DEFAULT_SUPERADMIN_EMAIL", "DEFAULT_SUPERADMIN_PASSWORD",
        "RAG_WEBHOOK_SECRET",
        # Les plafonds de jetons du modèle mutualisé (arrivés dans le cœur le
        # 09/09/2026, `core/run_gate.py`). Même classe que `BYPASS_AUTH` : ce
        # sont des garde-fous que le déploiement possède, pas de la
        # configuration de capacité. Un superadministrateur qui pourrait
        # relever son propre plafond depuis un écran web n'aurait plus de
        # plafond. `setup_status` ne les nomme pas, donc rien ne les réclamait
        # ici — cette ligne est là pour qu'on ne les ajoute pas par symétrie.
        "DEFAULT_LLM_USER_TOKEN_CAP",
        "DEFAULT_LLM_GLOBAL_TOKEN_CAP",
        "DEFAULT_LLM_CAP_WINDOW_HOURS",
        "DEFAULT_LLM_MONTHLY_TOKEN_QUOTA",
    }
    assert interdits & set(catalog.BY_NAME) == set()


def test_aucune_variable_de_base_de_donnees_ni_de_chiffrement():
    """Le test ci-dessus liste des noms connus ; celui-ci attrape ceux qu'on
    ajouterait demain. Un `DB_REPLICA_HOST` inventé plus tard doit échouer
    ici même si personne n'a pensé à l'ajouter à la liste des interdits."""
    for name in catalog.BY_NAME:
        assert not name.startswith("DB_"), name
        assert "ENCRYPT" not in name, name
        assert "SUPERADMIN" not in name, name
        assert "BYPASS" not in name, name


def test_le_catalogue_couvre_la_checklist():
    """Toute variable que la checklist demande à un administrateur de poser
    doit être posable depuis l'écran. Sans ça, l'écran nomme des variables
    et laisse l'administrateur dans le même cul-de-sac qu'avant."""
    manquantes = _checklist_names() - set(catalog.BY_NAME)
    assert manquantes == set(), (
        f"La checklist nomme {sorted(manquantes)} mais l'écran ne sait pas les poser."
    )


def test_le_catalogue_ne_deborde_pas_de_ses_capacites():
    """L'inverse : aucune variable ne s'invite hors des capacités servies
    par la checklist. C'est ce qui empêche la liste de grossir en douce."""
    connues = {item.key for item in ss.capabilities(_Settings(), {})}
    assert {v.capability for v in catalog.CATALOG} <= connues


def test_is_writable_est_sensible_a_la_casse():
    assert catalog.is_writable("SMTP_HOST")
    assert not catalog.is_writable("smtp_host")
    assert not catalog.is_writable("ENCRYPT_KEY")
    assert not catalog.is_writable("encrypt_key")


def test_normalize_refuse_un_nom_hors_liste():
    with pytest.raises(catalog.InvalidValue):
        catalog.normalize("ENCRYPT_KEY", "peu importe")


def test_normalize_stocke_la_valeur_nettoyee():
    """Le 04/09 : une garde qui nettoie sans stocker le nettoyé laisse
    passer un espace final. C'est le retour qui part en base."""
    assert catalog.normalize("SMTP_HOST", "  smtp.example.com  ") == "smtp.example.com"


def test_normalize_refuse_une_valeur_vide_ou_blanche():
    for blanc in ("", "   ", "\t"):
        with pytest.raises(catalog.InvalidValue):
            catalog.normalize("SMTP_HOST", blanc)


def test_normalize_refuse_les_sauts_de_ligne():
    """Le fichier d'overlay est sourcé par l'entrypoint : une valeur avec
    un saut de ligne y créerait des variables jamais demandées."""
    with pytest.raises(catalog.InvalidValue):
        catalog.normalize("SMTP_HOST", "smtp.example.com\nEXTRA=1")


def test_normalize_refuse_une_valeur_trop_longue():
    with pytest.raises(catalog.InvalidValue):
        catalog.normalize("SMTP_HOST", "a" * (catalog.MAX_VALUE_LENGTH + 1))


def test_les_validateurs_typent_ce_qui_peut_l_etre():
    assert catalog.normalize("SMTP_PORT", " 587 ") == "587"
    with pytest.raises(catalog.InvalidValue):
        catalog.normalize("SMTP_PORT", "587a")
    with pytest.raises(catalog.InvalidValue):
        catalog.normalize("SMTP_PORT", "70000")

    assert catalog.normalize("STORAGE_MODE", "s3") == "S3"
    with pytest.raises(catalog.InvalidValue):
        catalog.normalize("STORAGE_MODE", "gcs")

    assert catalog.normalize("TH2ETL_BASE_URL", "https://etl.example") == "https://etl.example"
    with pytest.raises(catalog.InvalidValue):
        catalog.normalize("TH2ETL_BASE_URL", "//etl.example")


def test_un_message_de_refus_ne_contient_jamais_la_valeur():
    """Le message part en 400 et souvent dans un journal côté client."""
    with pytest.raises(catalog.InvalidValue) as refus:
        catalog.normalize("SMTP_PORT", FACTICE)
    assert FACTICE not in str(refus.value)

    with pytest.raises(catalog.InvalidValue) as refus:
        catalog.normalize("INCONNUE", FACTICE)
    assert FACTICE not in str(refus.value)
