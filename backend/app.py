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

import asyncio
import os
import sys
import time
import logging
import threading
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


@app.on_event("startup")
async def _on_startup():
    """Non-blocking: pool is lazy, cache keepalive fires in the background so
    the service accepts HTTP traffic immediately on startup."""
    _ensure_pool()
    asyncio.create_task(_cache_keepalive())
    log.info("startup complete; cache keepalive scheduled")


async def _cache_keepalive():
    """Periodically refresh the UNKNOWN cache so HTTP requests always hit warm
    data. TTL is the cadence — slightly faster than the HTTP-path TTL so users
    never catch a stale cache boundary."""
    period = max(10.0, _UNKNOWN_TTL - 5.0)
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _refresh_unknown_cache)
        except Exception as e:
            log.exception(f"cache keepalive error: {e}")
        await asyncio.sleep(period)


# --- MCP client pool ------------------------------------------------------
# MCP sessions are stateful; each must be used by one thread at a time. 14
# parallel sessions overwhelm the server (observed: 60s+ timeouts), so keep
# the pool small and gate concurrency with a semaphore.
_POOL_SIZE = 3
_pool: "queue.Queue[McpClient]" = None  # type: ignore  # initialised in startup


import queue  # noqa: E402


_pool_created = 0
_pool_create_lock = threading.Lock()


def _ensure_pool():
    """Lazy init: creates `queue.Queue` on first use, no blocking at startup."""
    global _pool
    if _pool is None:
        with _pool_create_lock:
            if _pool is None:
                _pool = queue.Queue()


class McpBorrow:
    """Context manager: lease one MCP client from the pool, return on exit.

    Clients are created on demand up to _POOL_SIZE. A borrow that finds the pool
    empty but hasn't hit the cap will build a new client (expensive: MCP init +
    login takes ~20s) rather than block. This lets the service accept traffic
    immediately on startup and pays the cost amortized across early requests.
    """
    def __enter__(self) -> McpClient:
        global _pool_created
        _ensure_pool()
        try:
            self.client = _pool.get_nowait()
        except queue.Empty:
            with _pool_create_lock:
                if _pool_created < _POOL_SIZE:
                    _pool_created += 1
                    log.info(f"Creating MCP client {_pool_created}/{_POOL_SIZE}")
                    self.client = McpClient(CLIENT_ID)
                    return self.client
            # Cap reached — wait for one to be returned
            self.client = _pool.get()
        return self.client

    def __exit__(self, *exc):
        _pool.put(self.client)


# Cache the UNKNOWN list; background task keeps it warm so the HTTP path is
# always instant once the service has been up for a few seconds.
_unknown_cache: dict | None = None
_unknown_cache_at: float = 0.0
_UNKNOWN_TTL = 45.0  # how stale before /api/unknown triggers a synchronous refresh
_refresh_lock = threading.Lock()


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


def _fetch_unknown_for(slug: str) -> list[dict]:
    with McpBorrow() as mcp:
        r = mcp.call("crm_customers_by_state", {
            "product_slug": slug,
            "states": ["UNKNOWN"],
            "client_id": CLIENT_ID,
            "limit": 500,
            "include_conversations": False,
        })
    out = []
    for c in (r or {}).get("customers", []):
        out.append({
            "product_slug": slug,
            "conversation_id": c.get("conversation_id"),
            "customer_name": c.get("customer_name"),
            "note": (c.get("notes") or "").strip(),
            "last_message_timestamp": c.get("last_message_timestamp") or "",
        })
    return out


def _refresh_unknown_cache() -> dict:
    """Blocking refresh. Coalesces concurrent callers via _refresh_lock so the
    MCP server sees at most one refresh in flight at a time."""
    global _unknown_cache, _unknown_cache_at
    from concurrent.futures import ThreadPoolExecutor
    with _refresh_lock:
        # Double-checked: a caller may have refreshed while we waited for the lock
        now = time.time()
        if _unknown_cache and (now - _unknown_cache_at) < _UNKNOWN_TTL:
            return _unknown_cache
        t0 = time.time()
        with McpBorrow() as mcp:
            slugs = _active_products(mcp)
        # Parallelize across pool — at most _POOL_SIZE concurrent MCP calls
        with ThreadPoolExecutor(max_workers=_POOL_SIZE) as pool:
            buckets = list(pool.map(_fetch_unknown_for, slugs))
        items: list[dict] = [it for bucket in buckets for it in bucket]
        items.sort(key=lambda x: x.get("last_message_timestamp", ""), reverse=True)
        _unknown_cache = {"count": len(items), "items": items}
        _unknown_cache_at = time.time()
        log.info(f"unknown cache refreshed: {len(items)} items in {time.time()-t0:.1f}s")
        return _unknown_cache


@app.get("/api/unknown", dependencies=[Depends(require_token)])
async def list_unknown(refresh: bool = False):
    """Stale-while-revalidate: if a cached snapshot exists, return it
    immediately even if it's past TTL. The background keepalive owns
    refreshing. Only block on a synchronous refresh when the cache has
    never been populated (cold boot), or when refresh=true is passed."""
    now = time.time()
    if _unknown_cache and not refresh:
        return {**_unknown_cache, "cached": True, "cache_age": int(now - _unknown_cache_at)}
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _refresh_unknown_cache)
    return {**data, "cached": False}


def _get_conversation(product_slug: str, conversation_id: str) -> dict:
    with McpBorrow() as mcp:
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


@app.get("/api/conversation", dependencies=[Depends(require_token)])
async def get_conversation(product_slug: str, conversation_id: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_conversation, product_slug, conversation_id)


def _change_state(product_slug: str, customer_name: str, category: str) -> bool:
    with McpBorrow() as mcp:
        return mcp.call("change_crm_state", {
            "product_slug": product_slug,
            "customer_name": customer_name,
            "category": category,
            "client_id": CLIENT_ID,
        }) is not None


@app.post("/api/classify", dependencies=[Depends(require_token)])
async def classify(body: ClassifyBody):
    if body.new_state.upper() not in VALID_STATES:
        raise HTTPException(status_code=400, detail=f"new_state must be one of {sorted(VALID_STATES)}")
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, _change_state, body.product_slug, body.customer_name, body.new_state.upper())
    result = {"ok": True} if ok else None
    if result is None:
        raise HTTPException(status_code=502, detail="change_crm_state failed")
    log.info(f"reclassified {body.product_slug}/{body.customer_name} -> {body.new_state.upper()}")
    # Don't invalidate cache — frontend already tracks the local list and
    # advances past the classified item. The background keepalive will drop
    # the item on its next refresh cycle (~40s).
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
