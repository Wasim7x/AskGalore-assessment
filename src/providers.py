"""LLM provider abstraction for the COA mapper.

Every provider exposes the same method:

    propose(system_prompt: str, user_prompt: str, tool_schema: dict) -> dict

and returns a dict shaped like:

    {
        "chosen_code":  Optional[str],
        "confidence":   float in [0, 1],
        "rationale":    str,
        "alternatives": list[str],
    }

Keeping the contract narrow means `mapper.py` doesn't branch on provider name
anywhere outside this module, and the hallucination guardrail (dropping any
code the model invents outside the candidate list) stays in one place.

Supported providers (selected via MODEL_PROVIDER in .env):

    anthropic   native tool-use                (ANTHROPIC_API_KEY)
    openai      native function calling        (OPENAI_API_KEY)
    groq        OpenAI-compatible endpoint     (GROQ_API_KEY)
    gemini      google-genai tool calling      (GEMINI_API_KEY)
    ollama      local JSON-mode chat endpoint  (OLLAMA_BASE_URL)
"""
from __future__ import annotations

import json
import os
from typing import Protocol


DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-5-5",
    "openai":    "gpt-4o-mini",
    "groq":      "llama-3.3-70b-versatile",
    "gemini":    "gemini-2.0-flash",
    "ollama":    "llama3.1:8b",
}

# Groq exposes an OpenAI-compatible REST surface, so we reuse the openai SDK.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class ProviderError(RuntimeError):
    """Raised on any provider-side failure (missing key, HTTP error, bad JSON)."""


class Provider(Protocol):
    name: str
    model_id: str

    def propose(self, system_prompt: str, user_prompt: str, tool_schema: dict) -> dict: ...


# ─── Anthropic ─────────────────────────────────────────────────────────────

class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model_id: str = "", max_tokens: int = 1024, temperature: float = 0.0):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ProviderError("ANTHROPIC_API_KEY is not set.")
        self.model_id = model_id or DEFAULT_MODELS["anthropic"]
        self.max_tokens = max_tokens
        self.temperature = temperature

    def propose(self, system_prompt: str, user_prompt: str, tool_schema: dict) -> dict:
        from anthropic import Anthropic
        client = Anthropic()
        resp = client.messages.create(
            model=self.model_id,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": tool_schema["name"]},
            messages=[{"role": "user", "content": user_prompt}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_schema["name"]:
                return _normalise(block.input)
        raise ProviderError("Anthropic returned no tool_use block.")


# ─── OpenAI (and Groq, via OpenAI-compatible endpoint) ─────────────────────

class _OpenAICompatible:
    """Shared base for OpenAI and Groq — identical SDK, different base_url/key."""

    name = "openai"
    env_key = "OPENAI_API_KEY"
    base_url: str | None = None

    def __init__(self, model_id: str = "", max_tokens: int = 1024, temperature: float = 0.0):
        if not os.environ.get(self.env_key):
            raise ProviderError(f"{self.env_key} is not set.")
        self.model_id = model_id or DEFAULT_MODELS[self.name]
        self.max_tokens = max_tokens
        self.temperature = temperature

    def _client(self):
        from openai import OpenAI
        kwargs: dict = {"api_key": os.environ[self.env_key]}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def propose(self, system_prompt: str, user_prompt: str, tool_schema: dict) -> dict:
        client = self._client()
        tool = {
            "type": "function",
            "function": {
                "name": tool_schema["name"],
                "description": tool_schema["description"],
                "parameters": tool_schema["input_schema"],
            },
        }
        resp = client.chat.completions.create(
            model=self.model_id,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": tool_schema["name"]}},
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            raise ProviderError(f"{self.name} returned no tool call.")
        args = msg.tool_calls[0].function.arguments
        try:
            return _normalise(json.loads(args))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{self.name} returned non-JSON arguments: {exc}") from exc


class OpenAIProvider(_OpenAICompatible):
    name = "openai"
    env_key = "OPENAI_API_KEY"
    base_url = None  # SDK default


class GroqProvider(_OpenAICompatible):
    name = "groq"
    env_key = "GROQ_API_KEY"
    base_url = GROQ_BASE_URL


# ─── Gemini ────────────────────────────────────────────────────────────────

class GeminiProvider:
    name = "gemini"

    def __init__(self, model_id: str = "", max_tokens: int = 1024, temperature: float = 0.0):
        if not os.environ.get("GEMINI_API_KEY"):
            raise ProviderError("GEMINI_API_KEY is not set.")
        self.model_id = model_id or DEFAULT_MODELS["gemini"]
        self.max_tokens = max_tokens
        self.temperature = temperature

    def propose(self, system_prompt: str, user_prompt: str, tool_schema: dict) -> dict:
        # google-genai (the new SDK). Tool/function calling is the closest
        # thing to Anthropic tool-use; we still force JSON parsing on the
        # returned args because some model versions emit stringified JSON.
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        tool = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=tool_schema["name"],
                    description=tool_schema["description"],
                    parameters=_jsonschema_to_gemini(tool_schema["input_schema"]),
                )
            ]
        )
        cfg = types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_tokens,
            system_instruction=system_prompt,
            tools=[tool],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
            ),
        )
        resp = client.models.generate_content(
            model=self.model_id,
            contents=user_prompt,
            config=cfg,
        )
        for cand in resp.candidates or []:
            for part in (cand.content.parts or []):
                fc = getattr(part, "function_call", None)
                if fc and fc.name == tool_schema["name"]:
                    args = dict(fc.args or {})
                    return _normalise(args)
        raise ProviderError("Gemini returned no function_call.")


def _jsonschema_to_gemini(schema: dict) -> dict:
    """Trim a JSON schema to the subset Gemini accepts (no nullable unions)."""
    out = json.loads(json.dumps(schema))  # deep copy
    for prop in out.get("properties", {}).values():
        t = prop.get("type")
        if isinstance(t, list):
            # Gemini wants a single type; collapse ["string","null"] -> "string"
            non_null = [x for x in t if x != "null"]
            prop["type"] = non_null[0] if non_null else "string"
            prop["nullable"] = "null" in t
    return out


# ─── Ollama (local) ────────────────────────────────────────────────────────

class OllamaProvider:
    name = "ollama"

    def __init__(self, model_id: str = "", max_tokens: int = 1024, temperature: float = 0.0):
        self.model_id = model_id or DEFAULT_MODELS["ollama"]
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")

    def propose(self, system_prompt: str, user_prompt: str, tool_schema: dict) -> dict:
        # Ollama's native tool-use coverage varies by model, so we go through
        # JSON mode: embed the tool's input_schema into the prompt and tell
        # the model to return ONLY an object matching it. Deterministic,
        # works on every model that supports format=json (most do).
        import httpx

        schema_text = json.dumps(tool_schema["input_schema"], indent=2)
        full_system = (
            f"{system_prompt}\n\n"
            "Return a single JSON object matching this schema exactly. "
            "No prose, no code fences, just the JSON.\n\n"
            f"SCHEMA:\n{schema_text}"
        )
        payload = {
            "model": self.model_id,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
            "messages": [
                {"role": "system", "content": full_system},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            with httpx.Client(timeout=60.0) as http:
                r = http.post(f"{self.base_url}/api/chat", json=payload)
                r.raise_for_status()
                body = r.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama HTTP error: {exc}") from exc

        content = (body.get("message") or {}).get("content", "")
        if not content:
            raise ProviderError("Ollama returned an empty message.")
        try:
            return _normalise(json.loads(content))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Ollama returned non-JSON content: {exc}") from exc


# ─── Factory + shared normalisation ────────────────────────────────────────

_REGISTRY: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "openai":    OpenAIProvider,
    "groq":      GroqProvider,
    "gemini":    GeminiProvider,
    "ollama":    OllamaProvider,
}


def get_provider(name: str, model_id: str = "", **kwargs) -> Provider:
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        raise ProviderError(
            f"Unknown MODEL_PROVIDER {name!r}. "
            f"Supported: {', '.join(sorted(_REGISTRY))}"
        )
    return _REGISTRY[key](model_id=model_id, **kwargs)


def _normalise(data: dict) -> dict:
    """Coerce every provider's response into the mapper's expected shape."""
    return {
        "chosen_code":  data.get("chosen_code"),
        "confidence":   float(data.get("confidence") or 0.0),
        "rationale":    str(data.get("rationale") or "").strip(),
        "alternatives": list(data.get("alternatives") or []),
    }
