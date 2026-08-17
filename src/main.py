from dotenv import load_dotenv
from fastapi import FastAPI, Header

load_dotenv()

from .guards.agent_drafts import router as agent_drafts_router  # noqa: E402  (load_dotenv 이후 import)
from .guards.oidc import verify_oidc  # noqa: E402
from .guards.routes import router as guards_router  # noqa: E402
from .payouts.routes import router as payouts_router  # noqa: E402
from .settlements.routes import router as settlements_router  # noqa: E402

app = FastAPI()
app.include_router(guards_router)
app.include_router(payouts_router)
app.include_router(agent_drafts_router)
app.include_router(settlements_router)


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/tasks/ping")
def tasks_ping(authorization: str = Header(default="")):
    """Cloud Tasks OIDC 관통 확인용 스텁. schema-contract.md §10 Cloud Tasks 전용 라우트와
    동일한 검증 방식을 쓴다 — 실제 라우트는 A/C가 각자 만든다."""
    claims = verify_oidc(authorization)
    return {"status": "ok", "sub": claims.get("sub")}
