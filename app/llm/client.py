"""app/llm/client.py
Thin LLM adapter supporting Google Gemini and Groq via httpx, with mock client support for tests.
"""

import logging
from typing import Protocol

import httpx
from fastapi import HTTPException

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMClientProtocol(Protocol):
    """Protocol defining the LLM client interface."""

    def generate(self, system_prompt: str, user_prompt: str) -> str: ...


class GeminiClient:
    """Adapter for Google Gemini models via REST API."""

    def __init__(self, api_key: str | None, model: str = "gemini-2.0-flash"):
        self.api_key = api_key
        self.model = model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise HTTPException(
                status_code=503,
                detail="LLM API key is not configured. Please set LLM_API_KEY in your .env file (Gemini or Groq).",
            )

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json",
            },
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, json=payload)
                if resp.status_code == 429:
                    raise HTTPException(
                        status_code=503, detail="LLM rate limit reached. Please retry in a few moments."
                    )
                resp.raise_for_status()
                data = resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    raise HTTPException(status_code=502, detail="LLM returned empty candidate list.")
                text = candidates[0]["content"]["parts"][0]["text"]
                return text
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="LLM request timed out.")
        except httpx.HTTPStatusError as e:
            logger.error("Gemini API error: %s - %s", e.response.status_code, e.response.text)
            raise HTTPException(status_code=502, detail=f"LLM API error: {e.response.text}")


class GroqClient:
    """Adapter for Groq models via OpenAI-compatible REST API."""

    def __init__(self, api_key: str | None, model: str = "llama-3.3-70b-versatile"):
        self.api_key = api_key
        self.model = model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise HTTPException(
                status_code=503,
                detail="LLM API key is not configured. Please set LLM_API_KEY in your .env file.",
            )

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, json=payload, headers=headers)
                if resp.status_code == 429:
                    raise HTTPException(status_code=503, detail="Groq rate limit reached.")
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="LLM request timed out.")
        except httpx.HTTPStatusError as e:
            logger.error("Groq API error: %s - %s", e.response.status_code, e.response.text)
            raise HTTPException(status_code=502, detail=f"Groq API error: {e.response.text}")


class MockLLMClient:
    """Mock LLM client for tests."""

    def __init__(self, responses: list[str] | None = None):
        self.responses: list[str] = responses or []
        self.call_count: int = 0
        self.last_system_prompt: str | None = None
        self.last_user_prompt: str | None = None

    def add_response(self, response: str) -> None:
        self.responses.append(response)

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        self.call_count += 1

        if not self.responses:
            raise HTTPException(status_code=500, detail="MockLLMClient has no responses queued.")

        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


_client_instance: LLMClientProtocol | None = None


def get_llm_client() -> LLMClientProtocol:
    """Get or create singleton LLM client based on settings."""
    global _client_instance
    if _client_instance is None:
        settings = get_settings()
        if settings.LLM_PROVIDER == "groq":
            _client_instance = GroqClient(api_key=settings.LLM_API_KEY, model=settings.LLM_MODEL)
        else:
            _client_instance = GeminiClient(api_key=settings.LLM_API_KEY, model=settings.LLM_MODEL)
    return _client_instance


def set_llm_client(client: LLMClientProtocol) -> None:
    """Override LLM client (used in tests)."""
    global _client_instance
    _client_instance = client
