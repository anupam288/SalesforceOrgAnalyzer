"""
tools/llm_client.py

Vendor-agnostic LLM client for the Salesforce Org Intelligence Agent.

Supports:
  - Anthropic  (Claude Sonnet, Opus, Haiku)
  - OpenAI     (GPT-4o, GPT-4-turbo, GPT-3.5-turbo)
  - Azure OpenAI (any deployed model via Azure endpoint)
  - Google     (Gemini 1.5 Pro, Gemini 1.5 Flash)
  - Ollama     (any local model: llama3, mistral, qwen, etc.)

All providers expose the same two methods:
  - ask(prompt, system)      → str
  - ask_json(prompt, system) → dict

Usage in config.yaml:

  llm:
    provider: anthropic          # or openai / azure / google / ollama
    api_key: "sk-ant-..."
    model: "claude-sonnet-4-6"
    max_tokens: 4096
    # provider-specific extras:
    # azure_endpoint: "https://mydeployment.openai.azure.com"
    # azure_api_version: "2024-02-01"
    # ollama_base_url: "http://localhost:11434"
    # verify_ssl: false
"""
import json
import logging
import re
import time
import warnings
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# BASE CLASS  — all providers implement this interface
# ─────────────────────────────────────────────────────────────────────

class LLMClient(ABC):
    """
    Abstract base class. The rest of the codebase only ever calls
    .ask() and .ask_json() — provider differences are hidden here.
    """

    def __init__(self, max_tokens: int = 4096, verify_ssl: bool = False):
        self.max_tokens = max_tokens
        self.verify_ssl = verify_ssl
        self.total_input_tokens = 0
        self.total_output_tokens = 0

        if not verify_ssl:
            warnings.filterwarnings("ignore", message="Unverified HTTPS request")
            warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

    @abstractmethod
    def _call(self, prompt: str, system: str) -> tuple[str, int, int]:
        """
        Send one request to the provider.
        Returns (response_text, input_tokens, output_tokens).
        Raise an exception on non-retriable errors.
        For retriable errors (rate limit, connection, overload), also raise —
        the retry loop in ask() will handle it.
        """

    def ask(self, prompt: str, system: str = "", max_retries: int = 5) -> str:
        """Send a prompt, return text. Retries on transient errors."""
        last_exc = None
        for attempt in range(max_retries):
            try:
                text, inp, out = self._call(prompt, system)
                self.total_input_tokens  += inp
                self.total_output_tokens += out
                return text
            except _RetriableError as e:
                wait = min(2 ** attempt * e.base_wait, 120)
                logger.warning(
                    f"[{self.__class__.__name__}] {e.kind} — "
                    f"waiting {wait}s (attempt {attempt+1}/{max_retries})"
                )
                time.sleep(wait)
                last_exc = e
            except Exception:
                raise   # non-retriable — propagate immediately

        raise RuntimeError(
            f"LLM call failed after {max_retries} attempts. Last: {last_exc}"
        )

    def ask_json(self, prompt: str, system: str = "", max_retries: int = 5) -> dict:
        """Ask for a JSON response. Strips fences, retries on parse failures."""
        json_instruction = (
            "CRITICAL: Respond ONLY with a valid JSON object. "
            "No markdown fences, no preamble, no explanation. Raw JSON only."
        )
        full_system = (system + "\n\n" + json_instruction) if system else json_instruction

        raw = self.ask(prompt, system=full_system, max_retries=max_retries)
        return _parse_json(raw)

    def usage_summary(self) -> str:
        total = self.total_input_tokens + self.total_output_tokens
        return (
            f"{self.total_input_tokens:,} in + {self.total_output_tokens:,} out "
            f"= {total:,} tokens"
        )


# ─────────────────────────────────────────────────────────────────────
# INTERNAL: unified retriable error
# ─────────────────────────────────────────────────────────────────────

class _RetriableError(Exception):
    def __init__(self, kind: str, base_wait: int, original: Exception):
        self.kind = kind
        self.base_wait = base_wait
        self.original = original
        super().__init__(str(original))


# ─────────────────────────────────────────────────────────────────────
# PROVIDER 1 — ANTHROPIC
# ─────────────────────────────────────────────────────────────────────

class AnthropicClient(LLMClient):
    """
    Anthropic Claude. Models: claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5
    """
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6",
                 max_tokens: int = 4096, verify_ssl: bool = False):
        super().__init__(max_tokens, verify_ssl)
        import anthropic
        http = httpx.Client(verify=verify_ssl)
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, http_client=http)
        self.model = model

    def _call(self, prompt: str, system: str) -> tuple[str, int, int]:
        import anthropic
        try:
            kw = {"model": self.model, "max_tokens": self.max_tokens,
                  "messages": [{"role": "user", "content": prompt}]}
            if system:
                kw["system"] = system
            r = self.client.messages.create(**kw)
            return r.content[0].text, r.usage.input_tokens, r.usage.output_tokens
        except anthropic.RateLimitError as e:
            raise _RetriableError("rate_limit", 10, e)
        except anthropic.APIConnectionError as e:
            raise _RetriableError("connection_error", 3, e)
        except anthropic.APIStatusError as e:
            if e.status_code in (429, 500, 529):
                raise _RetriableError(f"http_{e.status_code}", 5, e)
            raise
        except anthropic.APIError as e:
            raise _RetriableError("api_error", 2, e)


# ─────────────────────────────────────────────────────────────────────
# PROVIDER 2 — OPENAI
# ─────────────────────────────────────────────────────────────────────

class OpenAIClient(LLMClient):
    """
    OpenAI. Models: gpt-4o, gpt-4o-mini, gpt-4-turbo, gpt-3.5-turbo
    pip install openai
    """
    def __init__(self, api_key: str, model: str = "gpt-4o",
                 max_tokens: int = 4096, verify_ssl: bool = False):
        super().__init__(max_tokens, verify_ssl)
        from openai import OpenAI
        http = httpx.Client(verify=verify_ssl)
        self.client = OpenAI(api_key=api_key, http_client=http)
        self.model = model

    def _call(self, prompt: str, system: str) -> tuple[str, int, int]:
        from openai import RateLimitError, APIConnectionError, APIStatusError
        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            r = self.client.chat.completions.create(
                model=self.model, messages=messages, max_tokens=self.max_tokens
            )
            usage = r.usage
            return (r.choices[0].message.content,
                    usage.prompt_tokens, usage.completion_tokens)
        except RateLimitError as e:
            raise _RetriableError("rate_limit", 10, e)
        except APIConnectionError as e:
            raise _RetriableError("connection_error", 3, e)
        except APIStatusError as e:
            if e.status_code in (429, 500, 503):
                raise _RetriableError(f"http_{e.status_code}", 5, e)
            raise


# ─────────────────────────────────────────────────────────────────────
# PROVIDER 3 — AZURE OPENAI
# ─────────────────────────────────────────────────────────────────────

class AzureOpenAIClient(LLMClient):
    """
    Azure OpenAI Service. Requires a deployment name (model) and endpoint.
    pip install openai

    config.yaml:
      llm:
        provider: azure
        api_key: "your-azure-key"
        model: "gpt-4o"           # your deployment name
        azure_endpoint: "https://myresource.openai.azure.com"
        azure_api_version: "2024-02-01"
    """
    def __init__(self, api_key: str, model: str, azure_endpoint: str,
                 azure_api_version: str = "2024-02-01",
                 max_tokens: int = 4096, verify_ssl: bool = False):
        super().__init__(max_tokens, verify_ssl)
        from openai import AzureOpenAI
        http = httpx.Client(verify=verify_ssl)
        self.client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=azure_endpoint,
            api_version=azure_api_version,
            http_client=http,
        )
        self.model = model

    def _call(self, prompt: str, system: str) -> tuple[str, int, int]:
        from openai import RateLimitError, APIConnectionError, APIStatusError
        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            r = self.client.chat.completions.create(
                model=self.model, messages=messages, max_tokens=self.max_tokens
            )
            u = r.usage
            return r.choices[0].message.content, u.prompt_tokens, u.completion_tokens
        except RateLimitError as e:
            raise _RetriableError("rate_limit", 10, e)
        except APIConnectionError as e:
            raise _RetriableError("connection_error", 3, e)
        except APIStatusError as e:
            if e.status_code in (429, 500, 503):
                raise _RetriableError(f"http_{e.status_code}", 5, e)
            raise


# ─────────────────────────────────────────────────────────────────────
# PROVIDER 4 — GOOGLE GEMINI
# ─────────────────────────────────────────────────────────────────────

class GoogleClient(LLMClient):
    """
    Google Gemini via google-generativeai SDK.
    Models: gemini-1.5-pro, gemini-1.5-flash, gemini-2.0-flash
    pip install google-generativeai
    """
    def __init__(self, api_key: str, model: str = "gemini-1.5-pro",
                 max_tokens: int = 4096, verify_ssl: bool = False):
        super().__init__(max_tokens, verify_ssl)
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        self.genai = genai
        self.model_name = model

    def _call(self, prompt: str, system: str) -> tuple[str, int, int]:
        try:
            model = self.genai.GenerativeModel(
                model_name=self.model_name,
                system_instruction=system if system else None,
                generation_config={"max_output_tokens": self.max_tokens},
            )
            r = model.generate_content(prompt)
            # Gemini token counting
            inp = getattr(r.usage_metadata, "prompt_token_count", 0) or 0
            out = getattr(r.usage_metadata, "candidates_token_count", 0) or 0
            return r.text, inp, out
        except Exception as e:
            err_str = str(e).lower()
            if "quota" in err_str or "429" in err_str or "rate" in err_str:
                raise _RetriableError("rate_limit", 15, e)
            if "connection" in err_str or "timeout" in err_str:
                raise _RetriableError("connection_error", 3, e)
            if "500" in err_str or "503" in err_str:
                raise _RetriableError("server_error", 5, e)
            raise


# ─────────────────────────────────────────────────────────────────────
# PROVIDER 5 — OLLAMA  (local models)
# ─────────────────────────────────────────────────────────────────────

class OllamaClient(LLMClient):
    """
    Ollama — run any model locally (llama3, mistral, qwen2.5, codellama, etc.)
    No API key needed. Ollama must be running: https://ollama.ai

    config.yaml:
      llm:
        provider: ollama
        model: "llama3.1"             # or mistral, qwen2.5, codellama, etc.
        ollama_base_url: "http://localhost:11434"   # default

    Start a model: ollama pull llama3.1 && ollama serve
    """
    def __init__(self, model: str = "llama3.1",
                 base_url: str = "http://localhost:11434",
                 max_tokens: int = 4096, verify_ssl: bool = False):
        super().__init__(max_tokens, verify_ssl)
        self.model = model
        self.api_url = f"{base_url.rstrip('/')}/api/chat"
        self.http = httpx.Client(verify=verify_ssl, timeout=300.0)

    def _call(self, prompt: str, system: str) -> tuple[str, int, int]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": self.max_tokens},
        }
        try:
            r = self.http.post(self.api_url, json=payload)
            r.raise_for_status()
            data = r.json()
            text = data["message"]["content"]
            inp = data.get("prompt_eval_count", 0)
            out = data.get("eval_count", 0)
            return text, inp, out
        except httpx.ConnectError as e:
            raise _RetriableError(
                "connection_error (is Ollama running? try: ollama serve)", 5, e
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 503):
                raise _RetriableError(f"http_{e.response.status_code}", 5, e)
            raise


# ─────────────────────────────────────────────────────────────────────
# FACTORY — build the right client from config
# ─────────────────────────────────────────────────────────────────────

def build_llm_client(llm_config: dict) -> LLMClient:
    """
    Build the correct LLMClient from the llm: section of config.yaml.
    """
    provider    = llm_config.get("provider", "anthropic").lower().strip()
    api_key     = llm_config.get("api_key", "")
    model       = llm_config.get("model", "")
    max_tokens  = int(llm_config.get("max_tokens", 4096))
    verify_ssl  = bool(llm_config.get("verify_ssl", False))

    KEY_HINTS = {
        "anthropic": "console.anthropic.com  →  llm.api_key  or  env ANTHROPIC_API_KEY",
        "openai":    "platform.openai.com/api-keys  →  llm.api_key  or  env OPENAI_API_KEY",
        "google":    "aistudio.google.com/app/apikey  →  llm.api_key  or  env GOOGLE_API_KEY",
        "azure":     "Azure portal  →  llm.api_key  or  env AZURE_OPENAI_API_KEY",
    }

    if provider in ("anthropic", "openai", "google") and not api_key:
        hint = KEY_HINTS.get(provider, "your provider dashboard")
        raise ValueError(
            f"\n\nMissing API key for provider '{provider}'.\n"
            f"  Where to get it: {hint}\n"
            f"  Then add to config.yaml:\n\n"
            f"    llm:\n"
            f"      provider: {provider}\n"
            f"      api_key: \"your-key-here\"\n"
        )

    if provider == "anthropic":
        return AnthropicClient(
            api_key=api_key,
            model=model or "claude-sonnet-4-6",
            max_tokens=max_tokens,
            verify_ssl=verify_ssl,
        )

    elif provider == "openai":
        return OpenAIClient(
            api_key=api_key,
            model=model or "gpt-4o",
            max_tokens=max_tokens,
            verify_ssl=verify_ssl,
        )

    elif provider == "azure":
        endpoint    = llm_config.get("azure_endpoint", "")
        api_version = llm_config.get("azure_api_version", "2024-02-01")
        if not endpoint:
            raise ValueError("azure provider requires 'azure_endpoint' in llm config")
        return AzureOpenAIClient(
            api_key=api_key,
            model=model,
            azure_endpoint=endpoint,
            azure_api_version=api_version,
            max_tokens=max_tokens,
            verify_ssl=verify_ssl,
        )

    elif provider == "google":
        return GoogleClient(
            api_key=api_key,
            model=model or "gemini-1.5-pro",
            max_tokens=max_tokens,
            verify_ssl=verify_ssl,
        )

    elif provider == "ollama":
        base_url = llm_config.get("ollama_base_url", "http://localhost:11434")
        return OllamaClient(
            model=model or "llama3.1",
            base_url=base_url,
            max_tokens=max_tokens,
            verify_ssl=verify_ssl,
        )

    else:
        supported = ["anthropic", "openai", "azure", "google", "ollama"]
        raise ValueError(
            f"Unknown LLM provider: '{provider}'. "
            f"Supported: {supported}"
        )


# ─────────────────────────────────────────────────────────────────────
# SHARED HELPER — JSON parsing (same for all providers)
# ─────────────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    # Strip ```json ... ``` or ``` ... ``` fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse error: {e} — attempting extraction from partial response")
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        raise ValueError(
            f"Could not parse JSON from LLM response.\n"
            f"Error: {e}\nFirst 300 chars: {raw[:300]}"
        )
