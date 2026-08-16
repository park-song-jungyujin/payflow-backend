import os

from fastapi import FastAPI, Header, HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from .guards.routes import router as guards_router
from .payouts.routes import router as payouts_router

app = FastAPI()
app.include_router(guards_router)
app.include_router(payouts_router)
_google_request = google_requests.Request()


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/tasks/ping")
def tasks_ping(authorization: str = Header(default="")):
    """Cloud Tasks OIDC 관통 확인용 스텁. schema-contract.md §10 Cloud Tasks 전용 라우트와
    동일한 검증 방식을 쓴다 — 실제 라우트는 A/C가 각자 만든다."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    token = authorization.removeprefix("Bearer ")
    audience = os.environ["OIDC_AUDIENCE"]
    try:
        claims = id_token.verify_oauth2_token(token, _google_request, audience=audience)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    return {"status": "ok", "sub": claims.get("sub")}
