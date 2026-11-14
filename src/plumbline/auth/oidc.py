"""OIDC code flow. The host must explicitly expose only start and callback publicly."""
import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete
from sqlmodel import select

from .models import SessionRecord, User
from .oidc_models import OIDCIdentity, OIDCTransaction
from .service import digest, event, lock_accounts, now

_MAX_BODY = 262144
_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


def _https_url(url):
    parts = urlsplit(url)
    return (parts.scheme == "https" and bool(parts.hostname) and not parts.username
            and not parts.password and not parts.fragment)


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str = ""
    secure_cookie: bool = True
    success_path: str = "/ui/"

    def __post_init__(self):
        if (not _https_url(self.issuer) or self.issuer.endswith("/") or
                urlsplit(self.issuer).query or len(self.issuer) > 512 or
                not self.client_id or not self.client_secret or not _https_url(self.redirect_uri) or
                urlsplit(self.redirect_uri).query or
                not self.success_path.startswith("/") or self.success_path.startswith("//") or
                "\\" in self.success_path or "\r" in self.success_path or "\n" in self.success_path):
            raise ValueError("Invalid OIDC configuration")


def _json(client, method, url, **kwargs):
    if not _https_url(url):
        raise ValueError("Invalid provider endpoint")
    with client.stream(method, url, follow_redirects=False, timeout=_TIMEOUT, **kwargs) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > _MAX_BODY:
                raise ValueError("Provider response too large")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Invalid provider response")
    return result


def _metadata(client, config):
    data = _json(client, "GET", config.issuer + "/.well-known/openid-configuration")
    if data.get("issuer") != config.issuer:
        raise ValueError("Issuer mismatch")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(data.get(key), str) or not _https_url(data[key]):
            raise ValueError("Invalid metadata")
    return data


def _claims(client, config, metadata, token, nonce):
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ValueError("Invalid signing algorithm")
    jwks = _json(client, "GET", metadata["jwks_uri"])
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise ValueError("Invalid keys")
    matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]
               and key.get("kty") == "RSA" and key.get("use", "sig") == "sig"
               and key.get("alg", "RS256") == "RS256"]
    if len(matches) != 1:
        raise ValueError("Signing key unavailable")
    key = jwt.PyJWK.from_dict(matches[0], algorithm="RS256").key
    claims = jwt.decode(token, key, algorithms=["RS256"], audience=config.client_id,
                        issuer=config.issuer, options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]})
    sub, token_nonce, audience = claims["sub"], claims["nonce"], claims["aud"]
    if (not isinstance(sub, str) or not sub or len(sub) > 255 or
            not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce) or
            (isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != config.client_id) or
            ("azp" in claims and claims["azp"] != config.client_id)):
        raise ValueError("Invalid identity claims")
    return claims


def create_oidc_router(db_dependency, config: OIDCConfig, *, client_factory=httpx.Client):
    """Mount once; client_factory is injectable for tests and must enforce transport security."""
    router = APIRouter(prefix="/auth/oidc", tags=["auth"])

    @router.get("/start")
    def start(db=Depends(db_dependency)):
        try:
            with client_factory() as client:
                metadata = _metadata(client, config)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "Identity provider unavailable") from None
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        db.exec(delete(OIDCTransaction).where(OIDCTransaction.expires_at <= now()))
        db.add(OIDCTransaction(state_hash=digest(state), nonce=nonce, verifier=verifier,
                               expires_at=now() + timedelta(minutes=5)))
        db.commit()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"response_type": "code", "client_id": config.client_id, "redirect_uri": config.redirect_uri,
                  "scope": "openid", "state": state, "nonce": nonce, "code_challenge": challenge,
                  "code_challenge_method": "S256"}
        separator = "&" if urlsplit(metadata["authorization_endpoint"]).query else "?"
        response = RedirectResponse(metadata["authorization_endpoint"] + separator + urlencode(params), status_code=302)
        response.set_cookie("plumbline_oidc_state", state, max_age=300, path="/auth/oidc/callback",
                            secure=config.secure_cookie, httponly=True, samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/callback")
    def callback(request: Request, db=Depends(db_dependency)):
        state = request.query_params.get("state", "")
        cookie = request.cookies.get("plumbline_oidc_state", "")
        code = request.query_params.get("code", "")
        if (not state or len(state) > 128 or not state.isascii() or not cookie or not cookie.isascii()
                or not hmac.compare_digest(state, cookie)
                or not code or len(code) > 4096 or request.query_params.get("error")):
            raise HTTPException(400, "Invalid sign-in response")
        statement = (delete(OIDCTransaction).where(OIDCTransaction.state_hash == digest(state))
                     .returning(OIDCTransaction.nonce, OIDCTransaction.verifier, OIDCTransaction.expires_at))
        transaction = db.execute(statement).first()
        db.commit()
        if not transaction or transaction.expires_at <= now():
            raise HTTPException(400, "Invalid sign-in response")
        try:
            with client_factory() as client:
                metadata = _metadata(client, config)
                tokens = _json(client, "POST", metadata["token_endpoint"],
                               auth=httpx.BasicAuth(config.client_id, config.client_secret),
                               data={"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": config.redirect_uri, "code_verifier": transaction.verifier})
                id_token = tokens.get("id_token")
                if not isinstance(id_token, str) or len(id_token) > 32768:
                    raise ValueError("Invalid ID token")
                claims = _claims(client, config, metadata, id_token, transaction.nonce)
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
            raise HTTPException(401, "Identity verification failed") from None
        lock_accounts(db)
        identity = db.exec(select(OIDCIdentity).where(OIDCIdentity.issuer == config.issuer,
                                                     OIDCIdentity.subject == claims["sub"])).first()
        if identity:
            user = db.get(User, identity.user_id)
            if not user or not user.active:
                event(db, "oidc_login_denied", subject=identity.user_id)
                db.commit()
                raise HTTPException(403, "Account disabled")
        else:
            user_id = str(uuid4())
            user = User(id=user_id, username="oidc-" + user_id, password_hash="!oidc!",
                        role="viewer", active=True, force_change=False)
            db.add(user)
            db.flush()
            db.add(OIDCIdentity(id=str(uuid4()), issuer=config.issuer, subject=claims["sub"], user_id=user.id))
            event(db, "oidc_user_created", user.id, user.id)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.add(SessionRecord(token_hash=digest(token), user_id=user.id, csrf_hash=digest(csrf),
                             expires_at=now() + timedelta(hours=12)))
        event(db, "oidc_login", user.id, user.id)
        db.commit()
        response = RedirectResponse(config.success_path, status_code=303)
        response.delete_cookie("plumbline_oidc_state", path="/auth/oidc/callback")
        for name, value, httponly in (("plumbline_session", token, True), ("plumbline_csrf", csrf, False)):
            response.set_cookie(name, value, max_age=43200, path="/", secure=config.secure_cookie,
                                httponly=httponly, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
