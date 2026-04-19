"""
notes-classifier-web backend.

Serves a mobile-first swipe UI for triaging UNKNOWN-state conversations:
- GET  /api/unknown            list of UNKNOWN convos on active campaigns
- GET  /api/conversation       full message history for one convo
- POST /api/classify           change_crm_state for one customer
- GET  /                       static React frontend (built into ../frontend/dist)

Auth: a single shared bearer token `APP_TOKEN` from .env. All /api routes require
`Authorization: Bearer <token>`. The frontend picks the token up from `?token=…`
on first load and stores it in localStorage.
"""
from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Reuse notes-bot's mcp_client (installed alongside)
sys.path.insert(0, "/root/notes-bot")
from mcp_client import McpClient  # noqa: E402

load_dotenv(Path(__file__).parent / ".env")

CLIENT_ID = os.getenv("SF_CLIENT_ID", "signal-found_8f355b27")
APP_TOKEN = os.getenv("APP_TOKEN", "").strip()
ACTIVE_CAMPAIGNS_TTL = 120  # seconds

if not APP_TOKEN:
    print("WARNING: APP_TOKEN unset; /api endpoints will reject everything.", file=sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("classifier-web")

VALID_STATES = {"CONFIRMED", "CLOSE", "UNINTERESTED", "DISQUALIFIED", "UNKNOWN"}
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

app = FastAPI(title="notes-classifier-web")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- MCP session pool (one long-lived client) -----------------------------
_mcp: McpClient | None = None


def get_mcp() -> McpClient:
    global _mcp
    if _mcp is None:
        _mcp = McpClient(CLIENT_ID)
    return _mcp


# --- Auth dependency ------------------------------------------------------
async def require_token(request: Request):
    if not APP_TOKEN:
        raise HTTPException(status_code=503, detail="APP_TOKEN not configured on server")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth[7:].strip()
    if token != APP_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


# --- Campaign helpers -----------------------------------------------------
def _active_products(mcp: McpClient) -> list[str]:
    campaigns = mcp.call("list_campaigns", {"client_id": CLIENT_ID}) or {}
    active_uniques = [c.get("product_unique") for c in campaigns.get("campaigns", []) if c.get("active")]
    products = mcp.call("list_products", {"client_id": CLIENT_ID}) or {}
    unique_to_slug = {
        p.get("product_unique"): p.get("product_slug")
        for p in products.get("products", [])
        if p.get("product_unique") and p.get("product_slug")
    }
    return sorted({unique_to_slug[u] for u in active_uniques if u in unique_to_slug})


# --- Endpoints ------------------------------------------------------------
class ClassifyBody(BaseModel):
    product_slug: str
    customer_name: str
    new_state: str


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/unknown", dependencies=[Depends(require_token)])
async def list_unknown():
    mcp = get_mcp()
    slugs = _active_products(mcp)
    items: list[dict] = []
    for slug in slugs:
        r = mcp.call("crm_customers_by_state", {
            "product_slug": slug,
            "states": ["UNKNOWN"],
            "client_id": CLIENT_ID,
            "limit": 500,
            "include_conversations": False,
        })
        for c in (r or {}).get("customers", []):
            items.append({
                "product_slug": slug,
                "conversation_id": c.get("conversation_id"),
                "customer_name": c.get("customer_name"),
                "note": (c.get("notes") or "").strip(),
                "last_message_timestamp": c.get("last_message_timestamp") or "",
            })
    items.sort(key=lambda x: x.get("last_message_timestamp", ""), reverse=True)
    return {"count": len(items), "items": items}


@app.get("/api/conversation", dependencies=[Depends(require_token)])
async def get_conversation(product_slug: str, conversation_id: str):
    mcp = get_mcp()
    r = mcp.call("get_conversation_by_id", {
        "product_slug": product_slug,
        "conversation_id": conversation_id,
        "client_id": CLIENT_ID,
    })
    if not r:
        raise HTTPException(status_code=404, detail="conversation not found")
    strat = mcp.call("configure_product_strategy", {
        "product_slug": product_slug,
        "client_id": CLIENT_ID,
    }) or {}
    mp = (strat.get("current") or {}).get("market_position", {}).get("data", {})
    return {
        "messages": r.get("messages", []),
        "strategy": {
            "market_category": mp.get("market_category"),
            "one_line_pitch": mp.get("one_line_pitch"),
            "icp": (mp.get("icp") or "")[:600],
        },
    }


@app.post("/api/classify", dependencies=[Depends(require_token)])
async def classify(body: ClassifyBody):
    if body.new_state.upper() not in VALID_STATES:
        raise HTTPException(status_code=400, detail=f"new_state must be one of {sorted(VALID_STATES)}")
    mcp = get_mcp()
    result = mcp.call("change_crm_state", {
        "product_slug": body.product_slug,
        "customer_name": body.customer_name,
        "category": body.new_state.upper(),
        "client_id": CLIENT_ID,
    })
    if result is None:
        raise HTTPException(status_code=502, detail="change_crm_state failed")
    log.info(f"reclassified {body.product_slug}/{body.customer_name} -> {body.new_state.upper()}")
    return {"ok": True, "new_state": body.new_state.upper()}


# --- Static frontend ------------------------------------------------------
# Serve built React app; fall back to index.html for client-side routes.
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(FRONTEND_DIST / "index.html")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # Only catch non-API paths
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = FRONTEND_DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
else:
    @app.get("/")
    async def index_missing():
        return Response(
            "<h1>frontend not built</h1><p>Run <code>npm run build</code> in frontend/</p>",
            media_type="text/html",
        )
