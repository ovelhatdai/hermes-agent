"""Generate final thumbnail images for Hermes WhatsApp shortcuts.

This module is intentionally small and dependency-light:
- OpenAI uses the installed ``openai`` SDK.
- Gemini uses REST via aiohttp, so Hermes does not need google-genai.
- Outputs are normalized to 1080x1920 when Pillow is available.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or os.environ.get("HERMES_DIR") or (Path.home() / ".hermes"))
THUMB_DIR = Path(os.environ.get("HERMES_THUMB_OUTPUT_DIR") or (HERMES_HOME / "generated-thumbnails"))
OPENAI_MODEL = os.environ.get("HERMES_THUMB_OPENAI_MODEL", "gpt-image-1")
OPENAI_SIZE = os.environ.get("HERMES_THUMB_OPENAI_SIZE", "1024x1536")
OPENAI_QUALITY = os.environ.get("HERMES_THUMB_OPENAI_QUALITY", "high")
GEMINI_MODEL = os.environ.get("HERMES_THUMB_GEMINI_MODEL", "gemini-2.5-flash-image")
TARGET_SIZE = (1080, 1920)


class ThumbnailImageError(RuntimeError):
    """Raised when a provider cannot generate an image."""


def _load_env_file() -> None:
    env_path = HERMES_HOME / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _safe_slug(value: str) -> str:
    allowed = []
    for ch in (value or "").lower():
        if ch.isalnum():
            allowed.append(ch)
        elif ch in {" ", "-", "_"}:
            allowed.append("-")
    slug = "".join(allowed).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:60] or "thumb"


def _first_reference_image(reference_images: list[str]) -> str | None:
    for item in reference_images:
        path = Path(str(item))
        if path.exists() and path.is_file():
            return str(path)
    return None


def _output_path(person: str, provider: str, legenda: str) -> Path:
    day = time.strftime("%Y-%m-%d")
    stamp = time.strftime("%H%M%S")
    name = f"{_safe_slug(person)}-{_safe_slug(legenda)[:36]}-{provider}-{stamp}.png"
    return THUMB_DIR / day / _safe_slug(person) / name


def _write_base64_image(data: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(data))
    return _normalize_to_1080x1920(path)


def _normalize_to_1080x1920(path: Path) -> Path:
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return path

    with Image.open(path) as src:
        src = src.convert("RGB")
        target_w, target_h = TARGET_SIZE
        src_w, src_h = src.size
        src_ratio = src_w / src_h
        target_ratio = target_w / target_h

        if abs(src_ratio - target_ratio) < 0.01:
            final = src.resize(TARGET_SIZE, Image.LANCZOS)
        else:
            bg_scale = max(target_w / src_w, target_h / src_h)
            bg_size = (int(src_w * bg_scale), int(src_h * bg_scale))
            bg = src.resize(bg_size, Image.LANCZOS).filter(ImageFilter.GaussianBlur(32))
            left = (bg.width - target_w) // 2
            top = (bg.height - target_h) // 2
            canvas = bg.crop((left, top, left + target_w, top + target_h))

            fg_scale = min(target_w / src_w, target_h / src_h)
            fg_size = (int(src_w * fg_scale), int(src_h * fg_scale))
            fg = src.resize(fg_size, Image.LANCZOS)
            canvas.paste(fg, ((target_w - fg.width) // 2, (target_h - fg.height) // 2))
            final = canvas

        final.save(path, "PNG", optimize=True)
    return path


def _generate_openai_sync(prompt: str, output_path: Path, reference_image: str | None) -> Path:
    _load_env_file()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ThumbnailImageError("missing_openai_key")

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    if reference_image:
        with open(reference_image, "rb") as image_file:
            response = client.images.edit(
                model=OPENAI_MODEL,
                image=image_file,
                prompt=prompt,
                size=OPENAI_SIZE,
                quality=OPENAI_QUALITY,
                n=1,
            )
    else:
        response = client.images.generate(
            model=OPENAI_MODEL,
            prompt=prompt,
            size=OPENAI_SIZE,
            quality=OPENAI_QUALITY,
            n=1,
        )

    if not response.data or not getattr(response.data[0], "b64_json", None):
        raise ThumbnailImageError("openai_empty_image")
    return _write_base64_image(response.data[0].b64_json, output_path)


async def _generate_openai(prompt: str, output_path: Path, reference_image: str | None) -> Path:
    return await asyncio.to_thread(_generate_openai_sync, prompt, output_path, reference_image)


async def _generate_gemini(prompt: str, output_path: Path, reference_image: str | None) -> Path:
    _load_env_file()
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise ThumbnailImageError("missing_gemini_key")

    parts: list[dict[str, Any]] = [{"text": prompt}]
    if reference_image:
        mime_type = mimetypes.guess_type(reference_image)[0] or "image/jpeg"
        encoded = base64.b64encode(Path(reference_image).read_bytes()).decode("ascii")
        parts.append({"inline_data": {"mime_type": mime_type, "data": encoded}})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {"contents": [{"parts": parts}]}
    headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise ThumbnailImageError(f"gemini_http_{resp.status}: {text[:300]}")
            data = await resp.json()

    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return _write_base64_image(inline["data"], output_path)
    raise ThumbnailImageError("gemini_empty_image")


def _prompt_from_prompt_result(person: str, prompt_result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if person == "daiane":
        variants = prompt_result.get("variants") or []
        variant = variants[0] if variants else {}
        prompt = variant.get("prompt") or ""
        meta = {
            "theme": prompt_result.get("theme"),
            "variant": variant.get("id"),
            "block_word": variant.get("block_word"),
        }
    else:
        prompt = prompt_result.get("prompt") or prompt_result.get("alternative_prompt") or ""
        meta = {
            "template": prompt_result.get("template"),
            "headline": prompt_result.get("headline"),
            "gold_word": prompt_result.get("gold_word"),
        }
    if not prompt:
        raise ThumbnailImageError("missing_prompt")
    prompt = (
        f"{prompt} Final output must be a polished vertical 9:16 social media thumbnail. "
        "After generation, keep all essential text inside safe margins."
    )
    return prompt, meta


async def generate_thumbnail_image(
    *,
    person: str,
    legenda: str,
    prompt_result: dict[str, Any],
    reference_images: list[str] | None = None,
    provider: str = "auto",
) -> dict[str, Any]:
    reference_image = _first_reference_image(reference_images or [])
    prompt, meta = _prompt_from_prompt_result(person, prompt_result)
    requested = (provider or "auto").lower().strip()
    default_provider = os.environ.get("HERMES_THUMB_DEFAULT_PROVIDER", "").lower().strip()
    if requested in {"openai", "gemini"}:
        providers = [requested]
    elif default_provider in {"openai", "gemini"}:
        providers = [default_provider, "gemini" if default_provider == "openai" else "openai"]
    else:
        providers = ["openai", "gemini"]
    errors: list[str] = []

    for item in providers:
        output_path = _output_path(person, item, legenda)
        try:
            if item == "openai":
                image_path = await _generate_openai(prompt, output_path, reference_image)
            elif item == "gemini":
                image_path = await _generate_gemini(prompt, output_path, reference_image)
            else:
                continue
            return {
                "ok": True,
                "provider": item,
                "path": str(image_path),
                "size": "1080x1920",
                "aspect_ratio": "9:16",
                "prompt": prompt,
                "meta": meta,
            }
        except Exception as exc:
            errors.append(f"{item}: {exc}")

    raise ThumbnailImageError("; ".join(errors) or "no_provider_available")
