"""Application configuration loaded from environment variables."""

import logging
from pathlib import Path

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    # Database
    DATABASE_URL: str = "sqlite:///./ct200qa.db"
    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "ct200qa"

    # LLM
    LLM_PROVIDER: str = "gemini"  # "groq" or "gemini"
    LLM_MODEL: str = "gemini-2.0-flash"
    LLM_API_KEY: str | None = None

    # Optional OCR
    TESSERACT_CMD: str | None = None

    # Data directory
    DATA_DIR: Path = Path("data")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def llm_available(self) -> bool:
        """Check if LLM is configured."""
        return bool(self.LLM_API_KEY)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Get cached settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
        logger.info(
            "Settings loaded: LLM=%s/%s available=%s",
            _settings.LLM_PROVIDER,
            _settings.LLM_MODEL,
            _settings.llm_available,
        )
    return _settings
