"""Silknet chatbot: FastAPI + Claude Haiku on Amazon Bedrock, with auth, compaction,
rate limiting and abusive-IP blocking."""

import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import chat, config, db, security
from .security import SESSION_COOKIE, guard, limiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("silknet")
STATIC = Path(__file__).parent / "static"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    security.purge_expired_sessions()
    guard.refresh()
    log.info("Started. model=%s db=%s", config.MODEL_ID, config.DB_PATH)
    yield


app = FastAPI(title="Silknet Chatbot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def too_many(retry_after: float, message: str = "Too many requests. Slow down.") -> HTTPException:
    return HTTPException(429, message, headers={"Retry-After": str(int(retry_after + 0.999))})


# ---------------------------------------------------------------- middleware


@app.middleware("http")
async def shield(request: Request, call_next):
    ip = security.client_ip(request)
    request.state.ip = ip

    if guard.is_blocked(ip):
        return JSONResponse(
            {"detail": "Your IP is temporarily blocked because of suspicious activity."}, status_code=403
        )
    if request.url.path != "/health":
        retry = limiter.hit(f"ip:{ip}", config.RATE_IP_PER_MIN, 60)
        if retry:
            guard.strike(ip, "global rate limit")
            return JSONResponse(
                {"detail": "Too many requests. Slow down."},
                status_code=429,
                headers={"Retry-After": str(int(retry + 0.999))},
            )

    response = await call_next(request)

    if response.status_code == 404:  # path scanning / probing other users' chats
        guard.strike(ip, f"404 {request.url.path[:80]}")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError):
    guard.strike(getattr(request.state, "ip", "unknown"), "invalid payload")
    return await request_validation_exception_handler(request, exc)


# ---------------------------------------------------------------- auth


class SignupBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=8, max_length=256)


class LoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    user = security.user_for_token(token) if token else None
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
        path="/",
    )


def _check(request: Request, key: str, limit: int, window: float, message: str) -> None:
    retry = limiter.hit(key, limit, window)
    if retry:
        guard.strike(request.state.ip, f"rate limit {key.split(':')[0]}")
        raise too_many(retry, message)


@app.post("/api/auth/signup")
def signup(body: SignupBody, request: Request, response: Response):
    if not config.ALLOW_SIGNUP:
        raise HTTPException(403, "Sign-ups are disabled")
    _check(request, f"signup:{request.state.ip}", config.RATE_SIGNUP_PER_HOUR, 3600,
           "Too many accounts created from this network. Try again later.")
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address")
    try:
        with db.connect() as conn:
            uid = conn.execute(
                "INSERT INTO users (email, pw_hash, created_at) VALUES (?, ?, ?)",
                (email, security.hash_password(body.password), time.time()),
            ).lastrowid
            token = security.create_session(conn, uid)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with this email already exists")
    _set_session_cookie(response, token)
    log.info("New user %s from %s", email, request.state.ip)
    return {"email": email}


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    email = body.email.strip().lower()
    _check(request, f"login:{request.state.ip}", config.RATE_LOGIN_PER_MIN, 60,
           "Too many login attempts. Wait a minute and try again.")
    _check(request, f"login-email:{email}", config.RATE_LOGIN_PER_EMAIL, 900,
           "Too many login attempts for this account. Try again in a few minutes.")
    with db.connect() as conn:
        user = conn.execute("SELECT id, pw_hash FROM users WHERE email = ?", (email,)).fetchone()
        ok = security.verify_password(body.password, user["pw_hash"] if user else security.DUMMY_HASH)
        if not (user and ok):
            guard.strike(request.state.ip, "failed login", weight=2)
            raise HTTPException(401, "Wrong email or password")
        token = security.create_session(conn, user["id"])
    _set_session_cookie(response, token)
    return {"email": email}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        security.delete_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"email": user["email"], "model": config.MODEL_ID}


# ---------------------------------------------------------------- conversations


class NewMessage(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


@app.get("/api/conversations")
def conversations(user=Depends(current_user)):
    return chat.list_conversations(user["id"])


@app.post("/api/conversations")
def new_conversation(request: Request, user=Depends(current_user)):
    _check(request, f"newconv:{user['id']}", 30, 60, "You're creating chats too quickly.")
    return chat.create_conversation(user["id"])


@app.get("/api/conversations/{cid}")
def conversation(cid: int, user=Depends(current_user)):
    try:
        return chat.get_conversation(user["id"], cid)
    except chat.NotFound:
        raise HTTPException(404, "Chat not found")


@app.delete("/api/conversations/{cid}")
def remove_conversation(cid: int, user=Depends(current_user)):
    try:
        chat.delete_conversation(user["id"], cid)
    except chat.NotFound:
        raise HTTPException(404, "Chat not found")
    return {"ok": True}


@app.post("/api/conversations/{cid}/messages")
def send(cid: int, body: NewMessage, request: Request, user=Depends(current_user)):
    _check(request, f"chat-min:{user['id']}", config.RATE_CHAT_PER_MIN, 60,
           "You're sending messages too fast. Take a breath and try again shortly.")
    _check(request, f"chat-day:{user['id']}", config.RATE_CHAT_PER_DAY, 86400,
           "Daily message limit reached. Come back tomorrow.")
    try:
        return chat.send_message(user["id"], cid, body.content)
    except chat.NotFound:
        raise HTTPException(404, "Chat not found")
    except chat.ChatError:
        raise HTTPException(502, "The model is unavailable right now. Please try again.")


# ---------------------------------------------------------------- pages


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#7c6cff"/><stop offset="1" stop-color="#3ec5ff"/></linearGradient></defs>'
    '<circle cx="16" cy="16" r="14" fill="url(#g)"/></svg>'
)


@app.get("/favicon.ico")
def favicon():
    # Served explicitly so browsers' automatic favicon request doesn't 404 and count as a strike.
    return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/health")
def health():
    return {"status": "ok", "model": config.MODEL_ID, "region": config.REGION}
