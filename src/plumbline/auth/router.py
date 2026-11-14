"""Auth routes. Mount with create_router(db_dependency, secure_cookie=...).

The host must install guard.install_guard and supply a yielding SQLModel Session
 dependency. Set secure_cookie=True when externally served over HTTPS; forwarded
headers are not trusted or inspected. Cookies use SameSite=Strict.
"""
import hmac
import secrets
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import select
from .models import ApiToken, AuditEvent, SessionRecord, User
from .service import authenticate, digest, event, lock_accounts, now, password_hash, revoke_all, verify_password


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class Password(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class NewUser(BaseModel):
    username: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=12, max_length=1024)
    role: Literal["viewer", "admin"]


class UserChange(BaseModel):
    active: bool
    role: Literal["viewer", "admin"]


class TokenRequest(BaseModel):
    role: Literal["viewer", "admin"]
    expires_days: int = Field(ge=1, le=365)


def create_router(db_dependency, *, secure_cookie=False):
    router = APIRouter(prefix="/auth", tags=["auth"])

    def cookie(response, name, value, httponly):
        response.set_cookie(name, value, secure=secure_cookie, httponly=httponly,
                            samesite="strict", path="/", max_age=43200)

    @router.get("/csrf")
    def csrf(response: Response):
        value = secrets.token_urlsafe(32)
        cookie(response, "plumbline_csrf", value, False)
        return {"csrf_token": value}

    @router.post("/login")
    def login(body: Login, request: Request, response: Response, db=Depends(db_dependency)):
        csrf_value = request.cookies.get("plumbline_csrf", "")
        header = request.headers.get("x-csrf-token", "")
        if not csrf_value or not header or not hmac.compare_digest(csrf_value, header):
            raise HTTPException(403, "CSRF validation failed")
        user, limited = authenticate(db, body.username, body.password)
        if limited:
            raise HTTPException(429, "Too many attempts")
        if not user:
            raise HTTPException(401, "Invalid credentials")
        token, csrf_value = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.add(SessionRecord(token_hash=digest(token), user_id=user.id, csrf_hash=digest(csrf_value),
                             expires_at=now() + timedelta(hours=12)))
        db.commit()
        cookie(response, "plumbline_session", token, True)
        cookie(response, "plumbline_csrf", csrf_value, False)
        return {"id": user.id, "username": user.username, "role": user.role,
                "force_change": user.force_change, "csrf_token": csrf_value}

    @router.get("/me")
    def me(request: Request, db=Depends(db_dependency)):
        user = db.get(User, request.state.principal["id"])
        return {"id": user.id, "username": user.username, "role": request.state.principal["role"],
                "force_change": user.force_change}

    @router.post("/logout")
    def logout(request: Request, response: Response, db=Depends(db_dependency)):
        principal = request.state.principal
        if principal["bearer"]:
            item = db.exec(select(ApiToken).where(ApiToken.token_hash == principal["credential"])).first()
        else:
            item = db.get(SessionRecord, principal["credential"])
        if item:
            db.delete(item)
        event(db, "logout", principal["id"], principal["id"])
        db.commit()
        response.delete_cookie("plumbline_session", path="/")
        response.delete_cookie("plumbline_csrf", path="/")
        return {"ok": True}

    @router.post("/password")
    def change_password(body: Password, request: Request, db=Depends(db_dependency)):
        user = db.get(User, request.state.principal["id"])
        if not verify_password(body.current_password, user.password_hash):
            raise HTTPException(400, "Current password is incorrect")
        user.password_hash = password_hash(body.new_password)
        user.force_change = False
        revoke_all(db, user.id)
        event(db, "password_changed", user.id, user.id)
        db.add(user)
        db.commit()
        return {"ok": True, "sign_in_required": True}

    @router.get("/users")
    def users(request: Request, db=Depends(db_dependency)):
        if request.state.principal["role"] != "admin":
            raise HTTPException(403, "Administrator required")
        return [{"id": u.id, "username": u.username, "role": u.role, "active": u.active,
                 "force_change": u.force_change} for u in db.exec(select(User)).all()]

    @router.post("/users", status_code=201)
    def create_user(body: NewUser, request: Request, db=Depends(db_dependency)):
        username = body.username.lower()
        if db.exec(select(User).where(User.username == username)).first():
            raise HTTPException(409, "Username already exists")
        user = User(id=str(uuid4()), username=username, password_hash=password_hash(body.password),
                    role=body.role, active=True, force_change=True)
        db.add(user)
        event(db, "user_created", request.state.principal["id"], user.id)
        db.commit()
        return {"id": user.id, "username": user.username, "role": user.role}

    @router.patch("/users/{user_id}")
    def update_user(user_id: str, body: UserChange, request: Request, db=Depends(db_dependency)):
        lock_accounts(db)
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(404, "User not found")
        if user.active and user.role == "admin" and (not body.active or body.role != "admin"):
            admins = db.exec(select(User).where(User.active == True, User.role == "admin")).all()  # noqa: E712
            if len(admins) <= 1:
                raise HTTPException(409, "Cannot remove the last active administrator")
        if user.active != body.active or user.role != body.role:
            revoke_all(db, user.id)
        user.active, user.role = body.active, body.role
        db.add(user)
        event(db, "user_updated", request.state.principal["id"], user.id)
        db.commit()
        return {"id": user.id, "active": user.active, "role": user.role}

    @router.post("/tokens", status_code=201)
    def create_token(body: TokenRequest, request: Request, db=Depends(db_dependency)):
        principal = request.state.principal
        if body.role == "admin" and principal["role"] != "admin":
            raise HTTPException(403, "Administrator required")
        raw = secrets.token_urlsafe(32)
        token = ApiToken(id=str(uuid4()), token_hash=digest(raw), user_id=principal["id"],
                         role=body.role, expires_at=now() + timedelta(days=body.expires_days))
        db.add(token)
        event(db, "token_created", principal["id"], token.id)
        db.commit()
        return {"id": token.id, "token": raw, "role": token.role, "expires_at": token.expires_at}

    @router.get("/tokens")
    def tokens(request: Request, db=Depends(db_dependency)):
        items = db.exec(select(ApiToken).where(ApiToken.user_id == request.state.principal["id"])).all()
        return [{"id": item.id, "role": item.role, "expires_at": item.expires_at} for item in items]

    @router.delete("/tokens/{token_id}")
    def delete_token(token_id: str, request: Request, db=Depends(db_dependency)):
        token = db.get(ApiToken, token_id)
        if not token or token.user_id != request.state.principal["id"]:
            raise HTTPException(404, "Token not found")
        db.delete(token)
        event(db, "token_revoked", request.state.principal["id"], token.id)
        db.commit()
        return {"ok": True}

    @router.get("/audit")
    def audit(request: Request, db=Depends(db_dependency)):
        if request.state.principal["role"] != "admin":
            raise HTTPException(403, "Administrator required")
        return db.exec(select(AuditEvent).order_by(AuditEvent.occurred_at.desc()).limit(500)).all()

    return router
