"""Minimal client for OpenAI-compatible chat completions with JSON-schema structured output."""
from __future__ import annotations

import json
import re
import time

import requests

from .config import Config

# JSON-schema helpers. Models generate object fields in the listed order, so put reasoning fields
# before the verdict they should inform.
STR, INT, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
ISO_LANG = {"type": "string", "pattern": "^[a-z]{2}$"}  # ISO 639-1


def arr(item: dict) -> dict:
    return {"type": "array", "items": item}


def obj(**props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


class LLMError(RuntimeError):
    pass


def parse_json(content: str):
    """Parse a reply, tolerating a <think> block or a ```json fence from servers without strict decoding."""
    content = re.sub(r"(?s)<think>.*?</think>", "", content or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    return json.loads(content)


class LLM:
    def __init__(self, cfg: Config):
        self.url = cfg.ai_url.rstrip("/")
        self.extra = cfg.ai_extra_body
        self.timeout = cfg.ai_timeout
        self._model = cfg.ai_model
        self.http = requests.Session()
        if cfg.ai_api_key:
            self.http.headers["Authorization"] = f"Bearer {cfg.ai_api_key}"

    @property
    def model(self) -> str:
        if not self._model:
            r = self.http.get(f"{self.url}/models", timeout=30)
            r.raise_for_status()
            models = r.json().get("data") or []
            if not models:
                raise LLMError(f"{self.url}/models lists no models: pull/load one or set AI_MODEL")
            self._model = models[0]["id"]
        return self._model

    def ask(self, prompt: str, schema: dict, tries: int = 3) -> dict:
        body = {"model": self.model, "temperature": 0, "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "response", "schema": schema}},
                **self.extra}
        err = None
        for attempt in range(tries):
            r = self.http.post(f"{self.url}/chat/completions", json=body, timeout=self.timeout)
            if 400 <= r.status_code < 500 and r.status_code != 429:  # bad key, model or request: retrying won't help
                raise LLMError(f"{self.url} answered {r.status_code}: {r.text[:300]}")
            try:
                r.raise_for_status()
                return parse_json(r.json()["choices"][0]["message"]["content"])
            except (requests.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
                err = e  # server hiccup or malformed JSON from the model
                time.sleep(2 ** attempt)
        raise LLMError(f"no valid JSON from the model after {tries} tries: {err}")
