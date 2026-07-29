from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from th2agent.configs.settings import get_settings

settings = get_settings()


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip auth for certain paths if needed, e.g., health checks, docs
        if request.url.path in ["/docs", "/redoc", "/openapi.json", "/favicon.ico"]:
            return await call_next(request)

        authorization = request.headers.get("Authorization")
        if not authorization or not authorization.startswith("Bearer "):
            return JSONResponse(
                status_code=401, content={"detail": "Missing or invalid token"}
            )

        token = authorization.split(" ")[1]
        # `test_token` a désormais une valeur par défaut vide (le paquet doit
        # s'importer sans configuration) : sans ce garde, un en-tête
        # `Authorization: Bearer ` suffirait à passer. Ce middleware n'est monté
        # nulle part, mais il porte le même nom de classe que celui
        # d'`auth/middleware.py`, qui l'est — mieux vaut qu'il refuse.
        if not settings.test_token or token != settings.test_token:
            return JSONResponse(status_code=401, content={"detail": "Invalid token"})

        response = await call_next(request)
        return response
