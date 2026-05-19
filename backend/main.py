"""
ComplAI MVP1 — FastAPI entry point
Invoice extraction tool for Indian CA firms.

Start with: uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import logging
import os

from models.db import engine, Base
from routes import auth, clients, upload, extract, bank, export, dashboard, cost

# ── Logging ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)


# ── Startup / Shutdown ─────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create DB tables on first run (Alembic handles migrations after)."""
    logger.info("ComplAI starting up…")
    Base.metadata.create_all(bind=engine)

    # Ensure uploads directory exists
    upload_dir = os.getenv("UPLOAD_DIR", "./uploads")
    os.makedirs(upload_dir, exist_ok=True)
    logger.info(f"Upload directory ready: {upload_dir}")

    yield
    logger.info("ComplAI shutting down…")


# ── App ────────────────────────────────────────────────────
app = FastAPI(
    title="ComplAI MVP1",
    description="Invoice extraction system for Indian CA firms",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow the single-file frontend (served from /frontend) to call the API.
# In production, restrict origins to your actual domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API routes (all prefixed /api) ─────────────────────────
app.include_router(auth.router,      prefix="/api/auth",      tags=["auth"])
app.include_router(clients.router,   prefix="/api/clients",   tags=["clients"])
app.include_router(upload.router,    prefix="/api/upload",    tags=["upload"])
app.include_router(extract.router,   prefix="/api/extract",   tags=["extract"])
app.include_router(bank.router,      prefix="/api/bank",      tags=["bank"])
app.include_router(export.router,    prefix="/api/export",    tags=["export"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(cost.router,      prefix="/api/cost",      tags=["cost"])


# ── Serve the single-page frontend ─────────────────────────
# The vanilla HTML file lives one level up at ../frontend/index.html
FRONTEND_PATH = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.get("/", include_in_schema=False)
async def serve_frontend():
    """Serve the single-file frontend (no-cache so JS changes apply immediately)."""
    return FileResponse(
        os.path.join(FRONTEND_PATH, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ComplAI MVP1"}
