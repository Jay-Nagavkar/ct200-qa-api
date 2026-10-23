"""app/mongo.py
MongoDB client management for LLM test case generations.
"""

import logging

from fastapi import HTTPException
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_mongo_client: MongoClient | None = None


def get_mongo_client() -> MongoClient:
    """Get or create singleton MongoDB client."""
    global _mongo_client
    if _mongo_client is None:
        try:
            _mongo_client = MongoClient(
                settings.MONGODB_URI,
                serverSelectionTimeoutMS=2000,
                connectTimeoutMS=2000,
            )
        except Exception as e:
            logger.error("Failed to initialize MongoDB client: %s", e)
            raise HTTPException(
                status_code=503,
                detail="MongoDB connection failed. Please ensure MongoDB is running via 'docker compose up -d'.",
            )
    return _mongo_client


def set_mongo_client(client: MongoClient) -> None:
    """Override MongoDB client (e.g., with mongomock in tests)."""
    global _mongo_client
    _mongo_client = client


def get_mongo_db():
    """Get MongoDB database with health check."""
    client = get_mongo_client()
    try:
        # Ping server to verify connection
        client.admin.command("ping")
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        logger.error("MongoDB ping failed: %s", e)
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable. Please ensure MongoDB is running via 'docker compose up -d'.",
        )
    return client[settings.MONGODB_DB]
