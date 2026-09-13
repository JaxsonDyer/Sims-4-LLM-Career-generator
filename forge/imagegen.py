"""
OpenRouter Image API client - custom career icon art.

Separate from the chat client in llm.py because the endpoint, request and
response shapes all differ: POST /api/v1/images takes a prompt (and optional
reference images for style) and returns base64-encoded image bytes.

Billing is all-or-nothing: a failed generation costs nothing, so callers
should feel free to retry. Errors surface as ImageGenError with the HTTP
detail included, the same policy as llm.py.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

IMAGES_URL = "https://openrouter.ai/api/v1/images"
IMAGE_MODELS_URL = "https://openrouter.ai/api/v1/images/models"

# Sensible default: fast and cheap, accepts reference images.
DEFAULT_IMAGE_MODEL = "google/gemini-2.5-flash-image"

# A short allowlist for the settings dropdown; the full list is one API call
# away (list_models) but needs a key.
KNOWN_IMAGE_MODELS = [
    "google/gemini-2.5-flash-image",
    "openai/gpt-image-1",
    "bytedance-seed/seedream-4.5",
    "black-forest-labs/flux.2-pro",
]


class ImageGenError(Exception):
    """Raised when image generation fails or returns something unusable."""


class ImageClient:
    """Minimal OpenRouter image-generation client. urllib, no extra deps."""

    def __init__(self, api_key: str, timeout: int = 300):
        if not api_key or not api_key.strip():
            raise ImageGenError("no OpenRouter API key set")
        self.api_key = api_key.strip()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/local/career-forge",
            "X-Title": "Sims 4 Career Forge",
        }

    def generate(self, model: str, prompt: str, *, aspect_ratio: str = "1:1",
                 output_format: str = "png",
                 input_references: list[bytes] | None = None) -> bytes:
        """
        Generate one image and return the raw bytes.

        `input_references` are PNG/JPEG byte blobs passed as base64 data
        URLs; providers that support image-to-image use them to steer style.
        Providers that do not simply ignore them - the prompt carries the
        style description either way.
        """
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "output_format": output_format,
        }
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        if input_references:
            payload["input_references"] = [
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,"
                                      + base64.b64encode(blob).decode("ascii")}}
                for blob in input_references
            ]

        request = urllib.request.Request(
            IMAGES_URL, data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(), method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code == 401:
                raise ImageGenError("OpenRouter rejected the API key (401)") from exc
            if exc.code == 402:
                raise ImageGenError(
                    "OpenRouter says this account is out of credit (402)"
                ) from exc
            if exc.code == 429:
                raise ImageGenError("Rate limited by OpenRouter (429)") from exc
            if exc.code == 502:
                # All-or-nothing billing: a failed generation is never billed.
                raise ImageGenError(
                    "image generation failed upstream (502); not billed"
                ) from exc
            raise ImageGenError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ImageGenError(f"could not reach OpenRouter: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ImageGenError(
                f"OpenRouter did not respond within {self.timeout}s"
            ) from exc

        if "error" in body and body["error"]:
            raise ImageGenError(f"OpenRouter error: {body['error']}")

        data = body.get("data") or []
        if not data or not data[0].get("b64_json"):
            raise ImageGenError(f"image response had no image data: {body}")
        try:
            return base64.b64decode(data[0]["b64_json"])
        except (ValueError, TypeError) as exc:
            raise ImageGenError("image data was not valid base64") from exc

    def list_models(self) -> list[str]:
        """Image model ids, for the settings dropdown."""
        request = urllib.request.Request(IMAGE_MODELS_URL, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
            return sorted(
                m["id"] for m in body.get("data", [])
                if "id" in m and "image" in (
                    (m.get("architecture") or {}).get("output_modalities") or []
                )
            )
        except Exception as exc:
            raise ImageGenError(f"could not list image models: {exc}") from exc
