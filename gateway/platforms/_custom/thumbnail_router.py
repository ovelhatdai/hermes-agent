"""SPEC-124 — prompt-only thumbnail generator for @viniciusnocode.

This router exposes the first Hermes thumbnail workflow:
legend -> thumb-lovart rules -> prompt ready for Lovart/Gemini/OpenAI.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

SERVICE = "thumbnail_vinicius"
DAIANE_SERVICE = "thumbnail_daiane"
MODE = "prompt_only"
MAX_LEGENDA_CHARS = 5000

FORMAT_LOCK = (
    "STRICT FORMAT: vertical Instagram Reels cover, 9:16 portrait aspect ratio, "
    "1080x1920 composition. Never landscape, never horizontal, never 16:9, "
    "never YouTube thumbnail format."
)


TEMPLATES = {
    "A": "Frase de Impacto",
    "B": "Tutorial / How-To",
    "C": "Resultado / Numero",
    "D": "Bastidores",
}


STOPWORDS = {
    "a", "agora", "ainda", "amanha", "ao", "aos", "as", "com", "como",
    "da", "das", "de", "do", "dos", "e", "em", "essa", "esse", "esta",
    "isso", "ja", "mais", "mas", "me", "meu", "minha", "na", "nao", "no",
    "nos", "o", "os", "ou", "pra", "que", "quem", "se", "sem", "sobre",
    "ta", "te", "tem", "um", "uma", "vai", "voce",
}


def _expected_token() -> str:
    return (
        os.environ.get("HERMES_THUMBNAIL_TOKEN")
        or os.environ.get("HERMES_GATEWAY_TOKEN")
        or ""
    ).strip()


def _check_auth(request: web.Request) -> web.Response | None:
    expected = _expected_token()
    if not expected:
        return None
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != expected:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    return None


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÿ0-9]+", text, flags=re.UNICODE)


def _clean_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" \t\n\r\"'“”‘’")
    return text


def _first_sentence(legenda: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+|\n+", legenda.strip(), maxsplit=1)
    return _clean_sentence(parts[0] if parts else legenda)


def _classify_template(legenda: str) -> str:
    plain = _strip_accents(legenda.lower())
    has_number = bool(re.search(r"(\d+|r\$|%|\bk\b|\bmil\b|\bmilhao\b|\bmilhoes\b)", plain))
    tutorial_terms = (
        "como ", "passo", "tutorial", "ferramenta", "workflow", "construi",
        "criei", "usando", "aprenda", "aprender", "codigo", "claude code",
        "codex", "ia ",
    )
    bts_terms = (
        "bastidor", "palestra", "evento", "setup", "dia a dia", "gravando",
        "palco", "escritorio", "viagem",
    )
    provocation_terms = (
        "fim", "nao existe", "nao e", "ficar pra tras", "acabou",
        "modelo antigo", "pra tras", "mentindo",
    )
    high_priority_phrase_terms = (
        "fim do gestor de trafego",
        "modelo antigo",
        "ficar pra tras",
        "ficou pra tras",
    )
    if has_number:
        return "C"
    if any(term in plain for term in high_priority_phrase_terms):
        return "A"
    if any(term in plain for term in tutorial_terms):
        return "B"
    if any(term in plain for term in bts_terms):
        return "D"
    if any(term in plain for term in provocation_terms):
        return "A"
    return "A"


def _headline_from_sentence(sentence: str, template: str) -> str:
    sentence = _clean_sentence(sentence)
    lowered = _strip_accents(sentence.lower())

    if "fim do gestor de trafego" in lowered:
        return "O FIM DO GESTOR DE TRÁFEGO"
    if "gestor de trafego" in lowered and "fim" in lowered:
        return "O FIM DO GESTOR DE TRÁFEGO"
    if "claude" in lowered and "codex" in lowered:
        return "HOJE CLAUDE. AMANHÃ CODEX."

    words = _words(sentence)
    if not words:
        return "IA NO COMANDO"

    if template == "B":
        selected = words[:6]
    else:
        selected = words[:8]
    return " ".join(selected).upper()


def _gold_word(headline: str) -> str:
    tokens = _words(headline)
    if not tokens:
        return "IA"
    preferred = ("FIM", "IA", "CODEX", "CLAUDE", "TRÁFEGO", "TRAFEGO", "COMANDO", "EXECUTAR")
    upper_tokens = [t.upper() for t in tokens]
    for item in preferred:
        if item in upper_tokens:
            return item
    candidates = [t for t in upper_tokens if _strip_accents(t.lower()) not in STOPWORDS]
    return max(candidates or upper_tokens, key=len)


def _subtitle(legenda: str, headline: str) -> str:
    plain = _strip_accents(legenda.lower())
    if "claude" in plain or "codex" in plain:
        return "saber o que pedir"
    if "gestor de trafego" in plain:
        return "o jogo mudou"
    if "ia" in plain:
        return "na prática"
    return "sem enrolação"


def _face_clause(face_mode: str, reference_context: str) -> str:
    if face_mode == "sem_rosto":
        return "No face, typography-only layout."
    context = reference_context.strip() or "using the provided reference photo as inspiration"
    return (
        "Small circular headshot of a man with glasses in the bottom-right corner "
        f"with a thin warm golden border, confident serious expression, dark shirt, {context}."
    )


def _split_headline(headline: str) -> tuple[str, str]:
    words = headline.split()
    if len(words) <= 3:
        return headline, ""
    midpoint = max(2, min(len(words) - 2, len(words) // 2))
    return " ".join(words[:midpoint]), " ".join(words[midpoint:])


def _build_prompt(
    template: str,
    headline: str,
    gold: str,
    subtitle: str,
    face_mode: str,
    reference_context: str,
) -> str:
    line1, line2 = _split_headline(headline)
    face = _face_clause(face_mode, reference_context)

    if template == "B":
        return (
            f"{FORMAT_LOCK} Professional Instagram thumbnail, solid black background (#0A0A0A). "
            'Small warm golden yellow badge at top reading "IA + WORKFLOW" in uppercase bold sans-serif. '
            f'Large bold white sans-serif text centered: "{headline}", with "{gold}" in warm golden yellow tone. '
            f'Smaller gray text below: "{subtitle}". Faint command-line cursor icon in the background at 10% opacity. '
            "Ultra-minimalist tech aesthetic, lots of negative space, clean premium layout. "
            "No gradients, no neon, no decorative elements."
        )
    if template == "C":
        return (
            f"{FORMAT_LOCK} Professional Instagram thumbnail, solid black background (#0A0A0A) with a very subtle warm golden yellow glow at the bottom edge. "
            f'Massive bold warm golden yellow text "{gold}" as the dominant element taking 40% of the image. '
            f'Below in bold white sans-serif: "{headline}". Smaller gray text below: "{subtitle}". '
            "Ultra-minimalist data-driven aesthetic, no faces, no icons, no gradients, no neon, no decorative elements."
        )
    if template == "D":
        optional_text = f'Bold white sans-serif text overlay at top: "{headline}", with "{gold}" in warm golden yellow tone.'
        return (
            f"{FORMAT_LOCK} Professional Instagram thumbnail, cinematic black and white photo of a man with glasses, "
            "dark moody lighting, shallow depth of field, slightly desaturated, looking directly at camera with confident expression. "
            f"{optional_text} Premium editorial feel, no gradients, no neon, no decorative elements."
        )
    if line2:
        text_clause = f'Large bold white sans-serif text centered: "{line1}" on the first line, "{line2}" on the second line, with the word "{gold}" in warm golden yellow tone.'
    else:
        text_clause = f'Large bold white sans-serif text centered: "{line1}", with the word "{gold}" in warm golden yellow tone.'
    return (
        f"{FORMAT_LOCK} Professional Instagram thumbnail, solid black background (#0A0A0A), clean minimalist design. "
        f"{text_clause} {face} Small gray text at bottom reading \"@viniciusnocode\". "
        "Lots of negative space, minimalist, clean, premium editorial feel. "
        "No gradients, no neon, no decorative elements."
    )


def _alternative_prompt(headline: str, gold: str, subtitle: str) -> str:
    return (
        f"{FORMAT_LOCK} Professional Instagram thumbnail, solid black background (#0A0A0A). "
        f'Small gray sans-serif text at top: "{subtitle}". '
        f'Large bold white sans-serif text centered: "{headline}", with "{gold}" in warm golden yellow tone. '
        "Typography-only composition, generous negative space above and below, ultra-minimalist, clean, premium. "
        "No faces, no icons, no gradients, no neon, no decorative elements. Bold sans-serif typography."
    )


def generate_thumbnail_prompt(payload: dict[str, Any]) -> dict[str, Any]:
    legenda = _clean_sentence(str(payload.get("legenda") or payload.get("caption") or ""))
    if not legenda:
        raise ValueError("missing_legenda")
    if len(legenda) > MAX_LEGENDA_CHARS:
        legenda = legenda[:MAX_LEGENDA_CHARS]

    face_mode = str(payload.get("face_mode") or "auto").strip().lower()
    if face_mode not in {"auto", "com_rosto", "sem_rosto"}:
        face_mode = "auto"

    reference_context = _clean_sentence(str(payload.get("reference_context") or ""))
    template = _classify_template(legenda)
    first = _first_sentence(legenda)
    headline = _headline_from_sentence(first, template)
    gold = _gold_word(headline)
    subtitle = _subtitle(legenda, headline)
    prompt = _build_prompt(template, headline, gold, subtitle, face_mode, reference_context)
    alt = _alternative_prompt(headline, gold, subtitle)

    return {
        "ok": True,
        "service": SERVICE,
        "mode": MODE,
        "template": template,
        "template_name": TEMPLATES[template],
        "headline": headline,
        "gold_word": gold,
        "subtitle": subtitle,
        "aspect_ratio": "9:16",
        "size": "1080x1920",
        "prompt": prompt,
        "alternative_prompt": alt,
        "notes": [
            "Prompt-only MVP baseado na skill thumb-lovart.",
            "Usa warm golden yellow tone para evitar que Lovart renderize hex como texto.",
        ],
    }


def _daiane_theme(legenda: str) -> str:
    plain = _strip_accents(legenda.lower())
    if any(term in plain for term in ("patagonia", "argentina", "internacional", "agosto", "esqui", "snowboard")):
        return "patagonia_internacional"
    return "diary_of_ceo"


def _daiane_reference_clause(payload: dict[str, Any]) -> str:
    reference_context = _clean_sentence(str(payload.get("reference_context") or ""))
    if reference_context:
        return reference_context
    return (
        "use the attached reference image as the base visual; clean every original overlay, "
        "caption, UI element, icon, sticker, logo or text from the frame before composing"
    )


def generate_daiane_thumbnail_prompt(payload: dict[str, Any]) -> dict[str, Any]:
    legenda = _clean_sentence(str(payload.get("legenda") or payload.get("caption") or ""))
    if not legenda:
        raise ValueError("missing_legenda")
    if len(legenda) > MAX_LEGENDA_CHARS:
        legenda = legenda[:MAX_LEGENDA_CHARS]

    reference = _daiane_reference_clause(payload)
    theme = _daiane_theme(legenda)

    base = (
        f"{FORMAT_LOCK} LOVART.AI vertical thumbnail, premium Diary of CEO editorial style, "
        "high contrast, readable in 1 second. Use extra-bold uppercase white sans-serif typography. "
        "Exactly one word must be inside a solid colored block with white text. "
        "Background slightly darkened/blurred with strong vignette, clean realistic premium finish. "
        f"Reference: {reference}. "
        "NEGATIVE: no emojis, no seals, no arrows, no brush strokes, no torn-paper effects, "
        "no small text, no poster look, no screenshot look, no UI, no captions, no icons, no distortions."
    )

    if theme == "patagonia_internacional":
        variants = [
            {
                "id": "V1",
                "block_word": "PATAGÔNIA",
                "block_color": "vermelho",
                "headline_lines": ["ADVOGANDO", "COM", "PROPÓSITO", "NA", "PATAGÔNIA"],
                "prompt": (
                    f"{base} Composition: keep snowy landscape and houses in the lower part; reserve the upper half "
                    "sky/mountains for giant text. Treatment: darken sky/mountains slightly for contrast, strong vignette, sharp snow. "
                    'Main text giant white uppercase: "ADVOGANDO" "COM" "PROPÓSITO" "NA" "PATAGÔNIA". '
                    'Colored block: only "PATAGÔNIA" in solid red block, white text.'
                ),
            },
            {
                "id": "V2",
                "block_word": "INTERNACIONAL",
                "block_color": "amarelo",
                "headline_lines": ["1ª EDIÇÃO", "INTERNACIONAL", "EM", "AGOSTO"],
                "prompt": (
                    f"{base} Composition: crop slightly tighter on the mountains, less ground and more horizon. "
                    "Keep a large clean upper area for the headline without covering important peaks. "
                    'Main text giant white uppercase: "1ª EDIÇÃO" "INTERNACIONAL" "EM" "AGOSTO". '
                    'Colored block: only "INTERNACIONAL" in solid yellow block, white text.'
                ),
            },
            {
                "id": "V3",
                "block_word": "FAMÍLIA",
                "block_color": "azul",
                "headline_lines": ["EVENTO", "COM", "FAMÍLIA", "BEM-VINDA."],
                "prompt": (
                    f"{base} Composition: landscape at the base, sky and mountains as breathing room for giant text in the upper half. "
                    "Treatment: colder darker premium clean look, strong vignette. "
                    'Main text giant white uppercase: "EVENTO" "COM" "FAMÍLIA" "BEM-VINDA.". '
                    'Colored block: only "FAMÍLIA" in solid blue block, white text.'
                ),
            },
        ]
    else:
        headline = _headline_from_sentence(_first_sentence(legenda), "A")
        key = _gold_word(headline)
        variants = [
            {
                "id": "V1",
                "block_word": key,
                "block_color": "vermelho",
                "headline_lines": headline.split(),
                "prompt": (
                    f"{base} Create a clean vertical editorial thumbnail for Daiane Elisa. "
                    "Serious premium mentor/CEO mood, strong portrait or contextual reference image, darkened background, giant white text. "
                    f'Headline: "{headline}". Colored block: only "{key}" in solid red block, white text.'
                ),
            },
            {
                "id": "V2",
                "block_word": key,
                "block_color": "amarelo",
                "headline_lines": headline.split(),
                "prompt": (
                    f"{base} Create a more aspirational vertical editorial thumbnail for Daiane Elisa. "
                    "Premium documentary look, clean contrast, giant white text with one impact word highlighted. "
                    f'Headline: "{headline}". Colored block: only "{key}" in solid yellow block, white text.'
                ),
            },
            {
                "id": "V3",
                "block_word": key,
                "block_color": "azul",
                "headline_lines": headline.split(),
                "prompt": (
                    f"{base} Create a calmer premium vertical editorial thumbnail for Daiane Elisa. "
                    "Family/purpose tone, realistic, clean, strong vignette, giant readable typography. "
                    f'Headline: "{headline}". Colored block: only "{key}" in solid blue block, white text.'
                ),
            },
        ]

    return {
        "ok": True,
        "service": DAIANE_SERVICE,
        "mode": MODE,
        "theme": theme,
        "aspect_ratio": "9:16",
        "size": "1080x1920",
        "style": "Diary of CEO editorial premium",
        "variants": variants,
        "notes": [
            "Prompt-only MVP baseado na skill thumb-daiane.",
            "Sempre gera 3 variações com exatamente 1 palavra em bloco colorido.",
        ],
    }


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": SERVICE, "mode": MODE})


async def _daiane_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": DAIANE_SERVICE, "mode": MODE})


async def _prompt(request: web.Request) -> web.Response:
    auth_error = _check_auth(request)
    if auth_error is not None:
        return auth_error
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    try:
        result = generate_thumbnail_prompt(payload)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("[thumbnail] prompt generation failed: %s", exc)
        return web.json_response({"ok": False, "error": "internal_error"}, status=500)
    return web.json_response(result)


async def _daiane_prompt(request: web.Request) -> web.Response:
    auth_error = _check_auth(request)
    if auth_error is not None:
        return auth_error
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    try:
        result = generate_daiane_thumbnail_prompt(payload)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("[thumbnail-daiane] prompt generation failed: %s", exc)
        return web.json_response({"ok": False, "error": "internal_error"}, status=500)
    return web.json_response(result)


def mount_thumbnail_subapp(app: Any, adapter: Any) -> None:
    app.router.add_get("/api/thumbnail/vinicius/health", _health)
    app.router.add_post("/api/thumbnail/vinicius/prompt", _prompt)
    app.router.add_get("/api/thumbnail/daiane/health", _daiane_health)
    app.router.add_post("/api/thumbnail/daiane/prompt", _daiane_prompt)
    logger.info("[custom_extensions] thumbnail routes mounted under /api/thumbnail")
