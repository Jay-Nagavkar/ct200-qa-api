"""app/main.py
FastAPI application entrypoint for CT-200 QA API.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import init_db
from app.routers import documents, generations, nodes, selections

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize database on startup."""
    logger.info("Initializing CT-200 QA API...")
    init_db()
    yield
    logger.info("Shutting down CT-200 QA API...")


app = FastAPI(
    title="CardioTrack CT-200 QA Backend API",
    description="Structured versioned PDF document tree and QA test case generation backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documents.router)
app.include_router(nodes.router)
app.include_router(selections.router)
app.include_router(generations.router)


@app.get("/health", tags=["system"])
def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
        "llm_available": settings.llm_available,
    }
