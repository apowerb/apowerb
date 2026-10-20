from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
import jwt
from jwt import InvalidTokenError
from passlib.context import CryptContext
from fastapi import HTTPException, status

from apowerb.configs.settings import get_settings

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_secret_key() -> str:
    """JWT signing key, resolved at call time and never empty.

    Previously a module constant (``SECRET_KEY = settings.encrypt_key``).
    Since ``encrypt_key`` gained a default value — necessary for the package
    to be importable without configuration — that constant silently held an
    empty string when ``ENCRYPT_KEY`` was not set, and JWTs could be signed
    with it.

    The server refuses to start without the key, so production was never
    affected. But an empty secret must never be a *usable* value: we refuse
    here, at the point of use, rather than failing at import time.
    """
    key = get_settings().encrypt_key
    if not key:
        raise RuntimeError(
            "ENCRYPT_KEY is not configured — refusing to sign or verify "
            "a token with an empty key."
        )
    return key


_ALGORITHMES_ACCEPTÉS = frozenset({"HS256", "HS384", "HS512"})


def get_algorithm() -> str:
    """JWT signing algorithm — the **sole** reader of ``settings.algorithm``.

    There used to be two sources of truth: the module constant ``ALGORITHM =
    "HS256"`` was used to *sign*, while ``main.py``, ``auth/dependencies``,
    ``auth/service`` and ``routers/audio_stream`` *verified* with
    ``settings.algorithm``. Both happened to be HS256, so nothing broke; the
    day either one changes, tokens are signed with one algorithm and verified
    with another — everyone is logged out with no test announcing it, because
    each half stays internally consistent.

    Restricted to the HMAC family: the key is a symmetric secret
    (``ENCRYPT_KEY``). Accepting ``RS*`` would pass that secret as a public
    key, and ``none`` would skip verification entirely.
    """
    algorithme = get_settings().algorithm
    if algorithme not in _ALGORITHMES_ACCEPTÉS:
        raise RuntimeError(
            f"ALGORITHM={algorithme!r} is not supported — the signing key "
            f"is a symmetric secret; only {sorted(_ALGORITHMES_ACCEPTÉS)} "
            "are accepted."
        )
    return algorithme


def __getattr__(name: str):
    # Safety net: anyone still importing the old constant gets a reason for the
    # change, not a bare AttributeError.
    if name == "SECRET_KEY":
        raise AttributeError(
            "SECRET_KEY has been replaced by get_secret_key(). A module "
            "constant froze the key at import time and could hold an empty string."
            )
    if name == "ALGORITHM":
        raise AttributeError(
            "ALGORITHM has been replaced by get_algorithm(). The constant was "
            "a second source of truth alongside settings.algorithm: signing used "
            "one and verification used the other."
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
    # `iat` is what makes a token revocable: without it, "issued before the
    # cut-off" is not a question anyone can answer. Not overwritten when a
    # caller sets its own.
    to_encode.setdefault("iat", datetime.now(timezone.utc))
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

    except InvalidTokenError:
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

    except InvalidTokenError:
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
    except InvalidTokenError:
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
    # PyJWT rejects a non-string sub claim at decode time, so coerce it here.
    if not access_data.get("sub") and access_data.get("user_id"):
        access_data["sub"] = str(access_data["user_id"])
    
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
    except InvalidTokenError:
        return None

    if payload.get("type") != "download":
        return None

    # Enforce scoping: legacy unscoped tokens (no sub/agent_id) are rejected.
    if not payload.get("sub") or not payload.get("agent_id") or not payload.get("filename"):
        return None

    return payload
