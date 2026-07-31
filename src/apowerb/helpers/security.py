from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import HTTPException, status

from apowerb.configs.settings import get_settings

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_secret_key() -> str:
    """Clé de signature des JWT, résolue à l'usage et jamais vide.

    Auparavant une constante de module (``SECRET_KEY = settings.encrypt_key``).
    Depuis que ``encrypt_key`` a une valeur par défaut — nécessaire pour que le
    paquet soit importable sans configuration — cette constante valait
    silencieusement la chaîne vide quand ``ENCRYPT_KEY`` n'était pas renseignée,
    et des JWT pouvaient être signés avec.

    Le serveur refuse de démarrer sans la clé, donc la production ne l'a jamais
    été. Mais un secret vide ne doit jamais être une valeur *utilisable* : on
    refuse ici, au moment de s'en servir, plutôt que d'échouer à l'import.
    """
    key = get_settings().encrypt_key
    if not key:
        raise RuntimeError(
            "ENCRYPT_KEY n'est pas configurée — refus de signer ou de vérifier "
            "un jeton avec une clé vide."
        )
    return key


_ALGORITHMES_ACCEPTÉS = frozenset({"HS256", "HS384", "HS512"})


def get_algorithm() -> str:
    """Algorithme de signature des JWT — **seul** lecteur de ``settings.algorithm``.

    Il y avait deux sources de vérité : la constante ``ALGORITHM = "HS256"`` de
    ce module servait à *signer*, pendant que ``main.py``, ``auth/dependencies``,
    ``auth/service`` et ``routers/audio_stream`` *vérifiaient* avec
    ``settings.algorithm``. Les deux valent HS256 aujourd'hui, donc rien ne
    casse ; le jour où l'une des deux bouge, les jetons sont signés avec un
    algorithme et vérifiés avec un autre — tout le monde est déconnecté sans
    qu'aucun test ne l'annonce, parce que chaque moitié reste cohérente avec
    elle-même.

    On restreint à la famille HMAC : la clé est un secret symétrique
    (``ENCRYPT_KEY``). Accepter ``RS*`` reviendrait à passer ce secret comme
    clé publique, et ``none`` à ne plus rien vérifier du tout.
    """
    algorithme = get_settings().algorithm
    if algorithme not in _ALGORITHMES_ACCEPTÉS:
        raise RuntimeError(
            f"ALGORITHM={algorithme!r} n'est pas supporté — la clé de signature "
            f"est un secret symétrique, seuls {sorted(_ALGORITHMES_ACCEPTÉS)} "
            "sont acceptés."
        )
    return algorithme


def __getattr__(name: str):
    # Filet : quiconque importe encore l'ancienne constante reçoit la raison du
    # changement, pas un AttributeError sec.
    if name == "SECRET_KEY":
        raise AttributeError(
            "SECRET_KEY a ete remplacee par get_secret_key(). Une constante de "
            "module figeait la cle a l'import et pouvait valoir la chaine vide."
        )
    if name == "ALGORITHM":
        raise AttributeError(
            "ALGORITHM a ete remplacee par get_algorithm(). La constante etait "
            "une seconde source de verite face a settings.algorithm : on signait "
            "avec l'une et on verifiait avec l'autre."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# JWT settings
ACCESS_TOKEN_EXPIRE_MINUTES = 120
REFRESH_TOKEN_EXPIRE_DAYS = 7
AGENT_REFRESH_TOKEN_EXPIRE_DAYS = 90  


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT token.

    NOTE: despite the historical name, this helper is used to mint tokens of
    several types (``access``, ``refresh``, ``download``). The ``type`` claim
    in ``data`` is preserved; if absent, it defaults to ``"access"``.

    Callers that want an access token can either omit ``type`` or pass
    ``type="access"`` explicitly. Callers minting a non-access token (e.g.
    the refresh cookie in ``auth/service.py``) MUST pass ``type`` explicitly
    — previously this function silently overwrote the field, producing
    "fake" access tokens that the refresh endpoint then rejected (B8 fix).
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode["exp"] = expire
    to_encode.setdefault("type", "access")
    encoded_jwt = jwt.encode(to_encode, get_secret_key(), algorithm=get_algorithm())

    return encoded_jwt


def create_refresh_token(data: dict) -> str:
    """Create a JWT refresh token"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, get_secret_key(), algorithm=get_algorithm())

    return encoded_jwt


def decode_access_token(token: str) -> Dict:
    """Decode and validate a JWT token"""
    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[get_algorithm()])

        # Verify token type
        if payload.get("type") != "access":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type",
            )

        return payload

    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def decode_refresh_token(token: str) -> Dict:
    """Decode and validate a refresh token"""
    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[get_algorithm()])

        # Verify token type
        if payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type",
            )

        return payload

    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )


def create_agent_refresh_token(data: dict, expires_days: int = AGENT_REFRESH_TOKEN_EXPIRE_DAYS) -> str:
    """
    Create a refresh token for scheduled agent runs.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=expires_days)
    to_encode.update({"exp": expire, "type": "agent_refresh"})
    encoded_jwt = jwt.encode(to_encode, get_secret_key(), algorithm=get_algorithm())
    return encoded_jwt


def decode_agent_refresh_token(token: str) -> Dict:
    """
    Decode and validate an agent refresh token.

    """
    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[get_algorithm()])
        
        if payload.get("type") != "agent_refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type. Expected agent_refresh token.",
            )
        
        return payload
    
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Agent refresh token has expired. Please reschedule the agent run.",
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate agent refresh token",
        )


def refresh_access_token_from_agent_refresh(refresh_token: str) -> str:
    """
    """
    # Decode and validate refresh token
    payload = decode_agent_refresh_token(refresh_token)
    
    # Extract data (excluding exp, type, iat)
    access_data = {k: v for k, v in payload.items() if k not in ["exp", "type", "iat"]}
    # ADKAuthMiddleware requires a `sub` identity claim. Agent refresh tokens
    # carry the identity in `user_id`, so map it across — otherwise the minted
    # access token is rejected with "Missing identity claim" (401).
    if not access_data.get("sub") and access_data.get("user_id"):
        access_data["sub"] = access_data["user_id"]
    
    # Create fresh access token
    access_token = create_access_token(access_data)
    
    return access_token

def generate_download_token(
    sub: str,
    agent_id: str,
    filename: str,
    expiration_minutes: int = 30,
) -> str:
    """Generate a short-lived scoped JWT for file download URLs.

    The token is bound to a specific user (`sub`), agent (`agent_id`) and
    `filename` so a leaked token cannot be replayed for arbitrary files.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=expiration_minutes)
    payload = {
        "exp": expire,
        "type": "download",
        "sub": sub,
        "agent_id": agent_id,
        "filename": filename,
    }
    return jwt.encode(payload, get_secret_key(), algorithm=get_algorithm())


def verify_download_token(token: str) -> Optional[Dict]:
    """Verify a download token and return its payload if valid.

    Returns the decoded payload dict if the token is a valid, non-expired
    download token with scoping claims (`sub`, `agent_id`, `filename`).
    Returns None otherwise.
    """
    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[get_algorithm()])
    except JWTError:
        return None

    if payload.get("type") != "download":
        return None

    # Enforce scoping: legacy unscoped tokens (no sub/agent_id) are rejected.
    if not payload.get("sub") or not payload.get("agent_id") or not payload.get("filename"):
        return None

    return payload
