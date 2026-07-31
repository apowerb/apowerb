import os
from cryptography.fernet import Fernet
from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging

_logger = setup_logging(__name__)


def _build_fernet() -> Fernet | None:
    """Construit le chiffreur, ou None si ``ENCRYPT_KEY`` est absente/invalide.

    Avant, ce module faisait ``Fernet(settings.encrypt_key)`` en colonne 1 :
    sans clé, l'*import* levait — ce qui rendait tout le paquet inimportable
    et laissait le garde-fou ``encryptor.fernet is None`` de ``main.py``
    inatteignable. On renvoie désormais None, et le refus de boot explicite
    (main.py) fait son travail.
    """
    settings = get_settings()
    if not settings.encrypt_key:
        return None
    try:
        return Fernet(settings.encrypt_key)
    except (ValueError, TypeError) as exc:
        _logger.warning("ENCRYPT_KEY invalide (%s) — chiffrement indisponible.", exc)
        return None


fernet = _build_fernet()


def _require_fernet() -> Fernet:
    if fernet is None:
        raise RuntimeError(
            "ENCRYPT_KEY n'est pas configurée — impossible de chiffrer ou "
            "déchiffrer les secrets d'intégration."
        )
    return fernet


def encrypt_value(value: str) -> str:
    encrypted_api_key = _require_fernet().encrypt(value.encode()).decode()
    return encrypted_api_key


def decrypt_value(encrypted_value: str) -> str:
    decrypted_api_key = _require_fernet().decrypt(encrypted_value.encode()).decode()
    return decrypted_api_key


def encrypt_value_in_dict(input_dict: dict, values_to_encrypt: list) -> dict:
    if input_dict is None:
        return {}
    for value in values_to_encrypt:
        if value in input_dict and input_dict[value] is not None:
            input_dict[value] = encrypt_value(input_dict[value])
    return input_dict


def decrypt_value_in_dict(input_dict: dict, values_to_decrypt: list) -> dict:
    if not input_dict:
        return input_dict or {}
    for value in values_to_decrypt:
        if value in input_dict and input_dict[value] is not None:
            input_dict[value] = decrypt_value(input_dict[value])
    return input_dict


def dict_to_envvar(env_dict: dict) -> None:
    if len(env_dict.items()) == 0:
        return None
    for key, value in env_dict.items():
        os.environ[key] = str(value)
