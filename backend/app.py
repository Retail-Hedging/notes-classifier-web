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
import json
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

VALID_STATES = {"CONFIRMED", "CLOSE", "UNINTERESTED", "DISQUALIFIED", "UNKNOWN", "REPLY"}
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
    """Non-blocking startup:
      - restore disk cache so the list is served instantly from cold boot
      - restore pending classify queue (in case a previous run enqueued but
        hadn't flushed before shutdown)
      - schedule hourly unknown-list refresh
      - schedule 5-minute classify-queue drain
    """
    _ensure_pool()
    _load_cache_from_disk()
    _load_queue()
    asyncio.create_task(_cache_keepalive())
    asyncio.create_task(_drain_queue_loop())
    if _unknown_cache and any(not it.get("messages") for it in _unknown_cache.get("items", [])):
        log.info("disk cache is missing messages; scheduling immediate fill")
        asyncio.get_event_loop().run_in_executor(None, _fill_messages_for_current_cache)
    log.info("startup complete; hourly refresh + 5-min queue drain scheduled")


def _fill_messages_for_current_cache():
    """Fill missing messages for items already in _unknown_cache. Used on
    startup when we loaded a list snapshot from disk but no messages yet."""
    from concurrent.futures import ThreadPoolExecutor
    if not _unknown_cache:
        return
    try:
        items_ref = _unknown_cache["items"]
        todo = [it for it in items_ref if not it.get("messages")]
        if not todo:
            return
        t0 = time.time()
        log.info(f"fill-on-startup: fetching messages for {len(todo)} items")
        with ThreadPoolExecutor(max_workers=_POOL_SIZE) as pool:
            pairs = list(pool.map(_fetch_messages, todo))
        msg_map = dict(pairs)
        for it in items_ref:
            cid = it.get("conversation_id")
            if not it.get("messages") and cid in msg_map:
                it["messages"] = msg_map[cid]
        _persist_cache_to_disk()
        log.info(
            f"fill-on-startup: {sum(len(m) for m in msg_map.values())} msgs "
            f"in {time.time()-t0:.1f}s"
        )
    except Exception as e:
        log.warning(f"fill-on-startup error: {e}")


async def _cache_keepalive():
    """Periodically refresh the UNKNOWN cache in the background. Doesn't race
    user-facing endpoints (those use the dedicated realtime client)."""
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _refresh_unknown_cache)
        except Exception as e:
            log.exception(f"cache keepalive error: {e}")
        await asyncio.sleep(_UNKNOWN_TTL)


# --- MCP client pool ------------------------------------------------------
# MCP sessions are stateful; each must be used by one thread at a time. 14
# parallel sessions overwhelm the server (observed: 60s+ timeouts), so keep
# the pool small and gate concurrency with a semaphore.
_POOL_SIZE = 3
_pool: "queue.Queue[McpClient]" = None  # type: ignore  # initialised in startup


import queue  # noqa: E402


_pool_created = 0
_pool_create_lock = threading.Lock()

# Dedicated client for user-facing requests (/api/conversation, /api/classify).
# Never borrowed by the list-refresh workers, so taps on the UI never wait on
# the slow MCP list fetch that saturates the pool.
_realtime_client: McpClient | None = None
_realtime_lock = threading.Lock()
_realtime_init_lock = threading.Lock()


def _ensure_pool():
    """Lazy init: creates `queue.Queue` on first use, no blocking at startup."""
    global _pool
    if _pool is None:
        with _pool_create_lock:
            if _pool is None:
                _pool = queue.Queue()


def _get_realtime() -> McpClient:
    """One long-lived MCP client reserved for interactive UI calls. Serialized
    with _realtime_lock (MCP sessions aren't thread-safe)."""
    global _realtime_client
    if _realtime_client is None:
        with _realtime_init_lock:
            if _realtime_client is None:
                log.info("creating realtime MCP client")
                _realtime_client = McpClient(CLIENT_ID)
    return _realtime_client


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


# Cache the UNKNOWN list + messages + strategies. Persisted to disk so
# restarts don't force a cold re-fetch (MCP is slow and flaky; one warm-up
# per hour is plenty — and survives process restarts).
_unknown_cache: dict | None = None
_unknown_cache_at: float = 0.0
_UNKNOWN_TTL = 3600.0  # refresh once an hour
_CACHE_FILE = Path(__file__).parent / "unknown_cache.json"
_refresh_lock = threading.Lock()

# Product strategy cache — pitch/ICP don't change during a session.
_strategy_cache: dict[str, dict] = {}
_strategy_cache_lock = threading.Lock()


# --- Classify queue --------------------------------------------------------
# /api/classify enqueues rather than hitting MCP inline — MCP's change_crm_state
# can take many seconds and was making the UI feel frozen on each tap. A
# background drainer flushes the queue every _QUEUE_FLUSH_INTERVAL seconds.
_QUEUE_FILE = Path(__file__).parent / "classify_queue.json"
_QUEUE_FLUSH_INTERVAL = 300.0  # 5 minutes
_classify_queue: list[dict] = []
_classify_queue_lock = threading.Lock()


def _persist_queue() -> None:
    try:
        tmp = _QUEUE_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(_classify_queue, f, ensure_ascii=False)
        tmp.replace(_QUEUE_FILE)
    except Exception as e:
        log.warning(f"persist queue failed: {e}")


def _load_queue() -> None:
    global _classify_queue
    try:
        if not _QUEUE_FILE.exists():
            return
        with _QUEUE_FILE.open("r", encoding="utf-8") as f:
            _classify_queue = json.load(f) or []
        if _classify_queue:
            log.info(f"loaded classify queue: {len(_classify_queue)} pending")
    except Exception as e:
        log.warning(f"load queue failed: {e}")
        _classify_queue = []


def _enqueue_classify(product_slug: str, customer_name: str, new_state: str) -> None:
    entry = {
        "product_slug": product_slug,
        "customer_name": customer_name,
        "new_state": new_state,
        "queued_at": time.time(),
    }
    with _classify_queue_lock:
        _classify_queue.append(entry)
        _persist_queue()
    # Also drop the item from the in-memory cache so the list shrinks on next
    # /api/unknown. MCP hasn't actually flipped state yet, but the user's
    # intent is clear and we don't want the same convo to keep appearing.
    global _unknown_cache
    if _unknown_cache:
        _unknown_cache["items"] = [
            it for it in _unknown_cache["items"]
            if not (it.get("product_slug") == product_slug
                    and it.get("customer_name") == customer_name)
        ]
        _unknown_cache["count"] = len(_unknown_cache["items"])
        _persist_cache_to_disk()


async def _drain_queue_loop():
    """Flush pending classifications to MCP every _QUEUE_FLUSH_INTERVAL seconds."""
    # Drain on startup once (short grace), then every interval
    await asyncio.sleep(10)
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _drain_queue_once)
        except Exception as e:
            log.exception(f"queue drain error: {e}")
        await asyncio.sleep(_QUEUE_FLUSH_INTERVAL)


def _drain_queue_once() -> None:
    with _classify_queue_lock:
        batch = list(_classify_queue)
    if not batch:
        return
    log.info(f"queue drain: applying {len(batch)} classification(s)")
    applied: list[dict] = []
    failed: list[dict] = []
    for entry in batch:
        try:
            ok = _change_state(
                entry["product_slug"], entry["customer_name"], entry["new_state"],
            )
        except Exception as e:
            log.warning(f"queue: change_state raised for {entry['customer_name']}: {e}")
            ok = False
        (applied if ok else failed).append(entry)
    with _classify_queue_lock:
        remaining: list[dict] = []
        for entry in _classify_queue:
            if entry in applied:
                continue
            remaining.append(entry)
        _classify_queue[:] = remaining
        _persist_queue()
    log.info(f"queue drain done: {len(applied)} applied, {len(failed)} still pending")


def _load_cache_from_disk() -> None:
    """Populate in-memory caches from the persisted JSON file if it exists.
    Called at startup so the service can serve immediately, before any MCP
    work has happened."""
    global _unknown_cache, _unknown_cache_at, _strategy_cache
    try:
        if not _CACHE_FILE.exists():
            log.info("no cache file on disk; will fetch cold on first refresh")
            return
        with _CACHE_FILE.open("r", encoding="utf-8") as f:
            snapshot = json.load(f)
        items = snapshot.get("items", [])
        # Retroactively dedupe cached messages so historic snapshots don't
        # show the CRM-duplicated bubbles
        for it in items:
            if it.get("messages"):
                it["messages"] = _dedupe_messages(it["messages"])
        _unknown_cache = {
            "count": snapshot.get("count", 0),
            "items": items,
            "strategies": snapshot.get("strategies", {}),
        }
        _unknown_cache_at = snapshot.get("saved_at", 0.0)
        _strategy_cache.update(snapshot.get("strategies", {}))
        age = time.time() - _unknown_cache_at
        log.info(
            f"loaded cache from disk: {_unknown_cache['count']} items, "
            f"age={int(age)}s"
        )
    except Exception as e:
        log.warning(f"failed to load cache from disk: {e}")


def _persist_cache_to_disk() -> None:
    """Atomic write so a crash mid-save doesn't corrupt the file."""
    if _unknown_cache is None:
        return
    try:
        tmp = _CACHE_FILE.with_suffix(".json.tmp")
        payload = {
            "count": _unknown_cache["count"],
            "items": _unknown_cache["items"],
            "strategies": _unknown_cache["strategies"],
            "saved_at": _unknown_cache_at,
        }
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp.replace(_CACHE_FILE)
    except Exception as e:
        log.warning(f"persist cache to disk failed: {e}")


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
    try:
        with McpBorrow() as mcp:
            r = mcp.call("crm_customers_by_state", {
                "product_slug": slug,
                "states": ["UNKNOWN"],
                "client_id": CLIENT_ID,
                "limit": 500,
                "include_conversations": False,
            })
    except Exception as e:
        log.warning(f"list UNKNOWN failed for {slug}: {type(e).__name__}: {e}")
        return []
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


from datetime import datetime

_OUTBOUND_TYPES = {"outbound", "sent", "agent"}


def _parse_ts(v: Any) -> float | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        # ISO-8601 with or without timezone
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _dedupe_messages(msgs: list[dict]) -> list[dict]:
    """Drop outbound duplicates: same text as a prior outbound message within
    1 hour. Preserves inbound (prospect) messages untouched — the CRM bug only
    duplicates our side. Order is preserved; the earliest copy wins."""
    out: list[dict] = []
    for m in msgs:
        is_outbound = (m.get("type") or "").lower() in _OUTBOUND_TYPES
        text = (m.get("message_text") or "").strip()
        if is_outbound and text:
            ts = _parse_ts(m.get("timestamp"))
            is_dup = False
            for prev in reversed(out):
                if (prev.get("type") or "").lower() not in _OUTBOUND_TYPES:
                    continue
                if (prev.get("message_text") or "").strip() != text:
                    continue
                pts = _parse_ts(prev.get("timestamp"))
                if ts is not None and pts is not None:
                    if abs(ts - pts) <= 3600:
                        is_dup = True
                        break
                else:
                    # Missing timestamps: still dedupe (adjacent same text)
                    is_dup = True
                    break
            if is_dup:
                continue
        out.append(m)
    return out


def _fetch_messages(item: dict) -> tuple[str, list[dict]]:
    """Returns (conversation_id, messages). Empty list on failure — UI is
    resilient to empty conversations. Messages are deduped to work around the
    CRM bug that occasionally emits identical outbound messages back-to-back."""
    cid = item["conversation_id"]
    try:
        with McpBorrow() as mcp:
            r = mcp.call("get_conversation_by_id", {
                "product_slug": item["product_slug"],
                "conversation_id": cid,
                "client_id": CLIENT_ID,
            })
        raw = (r or {}).get("messages", [])
        return cid, _dedupe_messages(raw)
    except Exception as e:
        log.warning(f"prefetch messages failed for {cid}: {e}")
        return cid, []


def _fetch_strategy_pool(slug: str) -> dict:
    """Pool-based strategy fetch used during cache refresh. Populates the
    realtime-side _strategy_cache too so later UI calls don't re-fetch."""
    if slug in _strategy_cache:
        return _strategy_cache[slug]
    try:
        with McpBorrow() as mcp:
            strat = mcp.call("configure_product_strategy", {
                "product_slug": slug,
                "client_id": CLIENT_ID,
            }) or {}
    except Exception:
        strat = {}
    mp = (strat.get("current") or {}).get("market_position", {}).get("data", {})
    payload = {
        "market_category": mp.get("market_category"),
        "one_line_pitch": mp.get("one_line_pitch"),
        "icp": (mp.get("icp") or "")[:600],
    }
    with _strategy_cache_lock:
        _strategy_cache[slug] = payload
    return payload


def _safe_active_products() -> list[str]:
    try:
        with McpBorrow() as mcp:
            return _active_products(mcp)
    except Exception as e:
        log.warning(f"_active_products failed: {type(e).__name__}: {e}")
        return []


def _refresh_unknown_cache() -> dict:
    """Two-phase refresh:
      Phase A (fast): list + strategies. Cache is populated immediately with
        empty messages per item, so /api/unknown can return the review list
        without waiting on per-conversation fetches.
      Phase B (slow): fills message arrays in-place, in the background. The
        in-memory cache item is the same object, so subsequent /api/unknown
        responses see messages as they land.
    Fault-tolerant at every step."""
    global _unknown_cache, _unknown_cache_at
    from concurrent.futures import ThreadPoolExecutor
    with _refresh_lock:
        now = time.time()
        if _unknown_cache and (now - _unknown_cache_at) < _UNKNOWN_TTL:
            return _unknown_cache
        t0 = time.time()

        try:
            slugs = _safe_active_products()
            if not slugs:
                log.warning("no active products; keeping previous cache")
                return _unknown_cache or {"count": 0, "items": [], "strategies": {}}

            with ThreadPoolExecutor(max_workers=_POOL_SIZE) as pool:
                buckets = list(pool.map(_fetch_unknown_for, slugs))
            items: list[dict] = [it for bucket in buckets for it in bucket]
            items.sort(key=lambda x: x.get("last_message_timestamp", ""), reverse=True)
            for it in items:
                it.setdefault("messages", [])

            slugs_referenced = sorted({it["product_slug"] for it in items})
            with ThreadPoolExecutor(max_workers=_POOL_SIZE) as pool:
                list(pool.map(_fetch_strategy_pool, slugs_referenced))
            strategies = {slug: _strategy_cache.get(slug, {}) for slug in slugs_referenced}

            # Phase A: publish list + strategies immediately (messages carry
            # over from the previous disk snapshot when we already have them)
            prev = _unknown_cache or {"items": []}
            prev_msgs: dict[str, list[dict]] = {
                it.get("conversation_id"): it.get("messages", [])
                for it in prev.get("items", [])
                if it.get("conversation_id")
            }
            for it in items:
                cid = it.get("conversation_id")
                if cid and prev_msgs.get(cid):
                    it["messages"] = prev_msgs[cid]  # preserve prior messages
            _unknown_cache = {
                "count": len(items),
                "items": items,
                "strategies": strategies,
            }
            _unknown_cache_at = time.time()
            _persist_cache_to_disk()
            log.info(
                f"unknown cache (list) ready: {len(items)} items, "
                f"{len(strategies)} strategies in {time.time()-t0:.1f}s"
            )
        except Exception as e:
            log.exception(f"_refresh_unknown_cache fatal in phase A: {e}")
            return _unknown_cache or {"count": 0, "items": [], "strategies": {}}

        # Phase B: populate messages in background. Only fetch for items that
        # don't already have messages cached from a previous refresh.
        def _fill_messages(items_ref: list[dict]):
            try:
                t1 = time.time()
                todo = [it for it in items_ref if not it.get("messages")]
                if not todo:
                    log.info("unknown cache messages: all cached from previous run, skip fetch")
                    return
                log.info(f"phase B: fetching messages for {len(todo)}/{len(items_ref)} items")
                with ThreadPoolExecutor(max_workers=_POOL_SIZE) as pool:
                    pairs = list(pool.map(_fetch_messages, todo))
                msg_map = dict(pairs)
                for it in items_ref:
                    cid = it.get("conversation_id")
                    if not it.get("messages") and cid in msg_map:
                        it["messages"] = msg_map[cid]
                _persist_cache_to_disk()  # save after messages filled
                log.info(
                    f"unknown cache messages filled: "
                    f"{sum(len(m) for m in msg_map.values())} msgs "
                    f"in {time.time()-t1:.1f}s"
                )
            except Exception as e:
                log.warning(f"phase B (fill messages) error: {e}")

        threading.Thread(
            target=_fill_messages, args=(items,), daemon=True, name="fill-msgs"
        ).start()

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


def _get_strategy(product_slug: str) -> dict:
    """Per-slug strategy cache. Product strategy doesn't change during a
    review session, so fetching once per deployment is plenty."""
    cached = _strategy_cache.get(product_slug)
    if cached is not None:
        return cached
    with _strategy_cache_lock:
        cached = _strategy_cache.get(product_slug)
        if cached is not None:
            return cached
        with _realtime_lock:
            mcp = _get_realtime()
            strat = mcp.call("configure_product_strategy", {
                "product_slug": product_slug,
                "client_id": CLIENT_ID,
            }) or {}
        mp = (strat.get("current") or {}).get("market_position", {}).get("data", {})
        payload = {
            "market_category": mp.get("market_category"),
            "one_line_pitch": mp.get("one_line_pitch"),
            "icp": (mp.get("icp") or "")[:600],
        }
        _strategy_cache[product_slug] = payload
        return payload


def _get_conversation(product_slug: str, conversation_id: str) -> dict:
    strategy = _get_strategy(product_slug)
    with _realtime_lock:
        mcp = _get_realtime()
        r = mcp.call("get_conversation_by_id", {
            "product_slug": product_slug,
            "conversation_id": conversation_id,
            "client_id": CLIENT_ID,
        })
    if not r:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"messages": _dedupe_messages(r.get("messages", [])), "strategy": strategy}


@app.get("/api/conversation", dependencies=[Depends(require_token)])
async def get_conversation(product_slug: str, conversation_id: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_conversation, product_slug, conversation_id)


def _change_state(product_slug: str, customer_name: str, category: str) -> bool:
    with _realtime_lock:
        mcp = _get_realtime()
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
    new_state = body.new_state.upper()
    _enqueue_classify(body.product_slug, body.customer_name, new_state)
    log.info(
        f"queued reclassify {body.product_slug}/{body.customer_name} -> {new_state} "
        f"(queue depth {len(_classify_queue)})"
    )
    return {
        "ok": True,
        "queued": True,
        "new_state": new_state,
        "queue_depth": len(_classify_queue),
    }


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
