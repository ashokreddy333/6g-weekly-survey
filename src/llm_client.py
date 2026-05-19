"""
Provider-agnostic LLM client.

Supports three providers behind a single interface:
  - "anthropic"  (Claude)         env var: ANTHROPIC_API_KEY
  - "openai"     (ChatGPT / GPT)  env var: OPENAI_API_KEY
  - "google"     (Gemini)         env var: GOOGLE_API_KEY

To switch providers, change `provider` and `model` in config.yaml -- no code
changes required. All providers return parsed JSON via .generate_json().

Why use the official SDKs:
  - Each handles auth, retries, and request shape correctly.
  - System-prompt and JSON-output handling differ across providers; the SDK
    hides those quirks.

Install only the SDK you'll use:
  pip install anthropic     # for Claude
  pip install openai        # for ChatGPT
  pip install google-genai  # for Gemini
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# ---------- Abstract base ----------

class LLMClient(ABC):
    """Common interface every provider implements."""

    @abstractmethod
    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1024,
    ) -> dict:
        """Send a prompt, return parsed JSON. Raises on persistent failure."""
        ...


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response, tolerating ```json fences."""
    # Strip code fences
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    # Find the first balanced { ... } block in case the model prepended text
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON object found in model response: {text[:200]}")
    return json.loads(cleaned[start:end + 1])


# ---------- Anthropic Claude ----------

class AnthropicClient(LLMClient):
    def __init__(self, model: str = "claude-sonnet-4-5", api_key: str | None = None):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError("Install with: pip install anthropic") from e
        self._anthropic = anthropic
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    def generate_json(self, system_prompt: str, user_prompt: str,
                      max_tokens: int = 1024) -> dict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        return _extract_json(text)


# ---------- OpenAI ChatGPT ----------

class OpenAIClient(LLMClient):
    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("Install with: pip install openai") from e
        self.model = model
        self.client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])

    def generate_json(self, system_prompt: str, user_prompt: str,
                      max_tokens: int = 1024) -> dict:
        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},  # forces JSON
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = resp.choices[0].message.content or ""
        return _extract_json(text)


# ---------- Google Gemini ----------

class GeminiClient(LLMClient):
    def __init__(self, model: str = "gemini-2.0-flash", api_key: str | None = None):
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise ImportError("Install with: pip install google-genai") from e
        self._types = types
        self.model = model
        self.client = genai.Client(api_key=api_key or os.environ["GOOGLE_API_KEY"])

    def generate_json(self, system_prompt: str, user_prompt: str,
                      max_tokens: int = 1024) -> dict:
        resp = self.client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=self._types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",  # forces JSON
            ),
        )
        return _extract_json(resp.text)


# ---------- Factory ----------

def make_client(provider: str, model: str | None = None) -> LLMClient:
    """
    Factory used by summarizer.py. Keep this the only place that knows about
    concrete provider classes -- the rest of the code talks to LLMClient.
    """
    provider = provider.lower()
    if provider == "anthropic":
        return AnthropicClient(model=model or "claude-sonnet-4-5")
    if provider == "openai":
        return OpenAIClient(model=model or "gpt-4o-mini")
    if provider == "google":
        return GeminiClient(model=model or "gemini-2.0-flash")
    raise ValueError(
        f"Unknown LLM provider '{provider}'. Use 'anthropic', 'openai', or 'google'."
    )


# ---------- Retry wrapper ----------

def generate_json_with_retry(
    client: LLMClient,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1024,
    retries: int = 3,
    backoff_seconds: float = 2.0,
) -> dict:
    """Tolerates transient rate-limit and parse failures."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return client.generate_json(system_prompt, user_prompt, max_tokens)
        except Exception as e:
            last_err = e
            logger.warning("LLM attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
    raise RuntimeError(f"LLM call failed after {retries} retries: {last_err}")
