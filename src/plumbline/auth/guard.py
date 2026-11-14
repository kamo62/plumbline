"""Default-deny HTTP guard. Install once on the application before serving requests.

Only /healthz, /auth/csrf and /auth/login are public. Safe methods require a
viewer; all other methods require an admin, except the account self-service
routes. Cookie mutations require the session CSRF header. Bearer credentials
never use cookies. Docs, UI and newly added routes are guarded by default.
"""
import hmac
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from plumbline.persistence.models import session_for
from .models import ApiToken, SessionRecord, User
from .service import digest, now

PUBLIC = {("GET", "/healthz"), ("GET", "/auth/csrf"), ("POST", "/auth/login")}
SELF_SERVICE = {("GET", "/auth/me"), ("POST", "/auth/logout"), ("POST", "/auth/password")}
SAFE = {"GET", "HEAD", "OPTIONS"}


def install_guard(app, database_url, public_ui=None):
    @app.middleware("http")
    async def auth_guard(request, call_next):
        route = (request.method, request.url.path)
        if route in PUBLIC or (request.method in {"GET", "HEAD"} and public_ui and public_ui(request.url.path)):
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        bearer = authorization.startswith("Bearer ")
        raw = authorization[7:] if bearer else request.cookies.get("plumbline_session", "")
        principal = None
        if not raw:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        url = database_url(request) if callable(database_url) else database_url
        if not url:
            return JSONResponse({"detail": "Database unavailable; run plumbline migrate"}, status_code=503)
        try:
            return await authenticate_request(request, call_next, route, raw, bearer, url)
        except SQLAlchemyError:
            return JSONResponse({"detail": "Database unavailable or not migrated"}, status_code=503)

    async def authenticate_request(request, call_next, route, raw, bearer, url):
        principal = None
        with session_for(url) as db:
            if raw and bearer:
                from sqlmodel import select
                credential = db.exec(select(ApiToken).where(ApiToken.token_hash == digest(raw))).first()
            else:
                credential = db.get(SessionRecord, digest(raw)) if raw else None
            if credential and credential.expires_at > now():
                user = db.get(User, credential.user_id)
                if user and user.active:
                    role = credential.role if bearer else user.role
                    if user.role == "viewer":
                        role = "viewer"
                    if not bearer and request.method not in SAFE:
                        csrf = request.headers.get("x-csrf-token", "")
                        if not csrf or not hmac.compare_digest(digest(csrf), credential.csrf_hash):
                            return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
                    principal = {"id": user.id, "role": role, "force_change": user.force_change,
                                 "bearer": bearer, "credential": digest(raw)}
        if not principal:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        if principal["force_change"] and route not in SELF_SERVICE:
            return JSONResponse({"detail": "Password change required"}, status_code=403)
        token_self_service = (request.method == "POST" and request.url.path == "/auth/tokens") or (
            request.method == "DELETE" and request.url.path.startswith("/auth/tokens/")
        )
        if request.method not in SAFE and route not in SELF_SERVICE and not token_self_service and principal["role"] != "admin":
            return JSONResponse({"detail": "Administrator required"}, status_code=403)
        request.state.principal = principal
        return await call_next(request)
