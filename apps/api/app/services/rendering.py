from __future__ import annotations

import pathlib
from functools import lru_cache

import bleach  # type: ignore[import-untyped]
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from apps.api.app.domain.models import EmailArtifact
from apps.api.app.domain.quality import SmsMetrics

GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ"
    "ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = frozenset("^{}\\[~]|€")
TEMPLATE_DIR = pathlib.Path(__file__).resolve().parents[1] / "templates"
ALLOWED_HTML_TAGS = frozenset(
    {"html", "head", "meta", "title", "body", "main", "p", "strong", "h1", "h2", "section", "a"}
)
ALLOWED_HTML_ATTRIBUTES = {
    "html": ["lang"],
    "meta": ["charset", "name", "content"],
    "main": ["aria-label"],
    "section": ["data-kind"],
    "a": ["href", "rel"],
}


def sms_metrics(text: str) -> SmsMetrics:
    characters = len(text)
    if all(character in GSM7_BASIC or character in GSM7_EXTENSION for character in text):
        septets = sum(2 if character in GSM7_EXTENSION else 1 for character in text)
        units_per_segment = 160 if septets <= 160 else 153
        segments = 0 if septets == 0 else (septets + units_per_segment - 1) // units_per_segment
        return SmsMetrics(
            encoding="GSM-7",
            characters=characters,
            code_units=characters,
            septets=septets,
            segments=segments,
            units_per_segment=units_per_segment,
        )
    code_units = len(text.encode("utf-16-be")) // 2
    units_per_segment = 70 if code_units <= 70 else 67
    segments = 0 if code_units == 0 else (code_units + units_per_segment - 1) // units_per_segment
    return SmsMetrics(
        encoding="UCS-2",
        characters=characters,
        code_units=code_units,
        segments=segments,
        units_per_segment=units_per_segment,
    )


@lru_cache(maxsize=1)
def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(default=True),
        undefined=StrictUndefined,
        auto_reload=False,
    )


def _plain_text(value: str) -> str:
    return str(
        bleach.clean(
            value,
            tags=set(),
            attributes={},
            protocols={"https"},
            strip=True,
            strip_comments=True,
        )
    )


def render_email_html(email: EmailArtifact) -> str:
    safe_email = email.model_copy(
        update={
            "subject": _plain_text(email.subject),
            "preheader": _plain_text(email.preheader),
            "headline": _plain_text(email.headline),
            "cta_label": _plain_text(email.cta_label),
            "sections": [
                section.model_copy(
                    update={
                        "heading": _plain_text(section.heading),
                        "body": _plain_text(section.body),
                    }
                )
                for section in email.sections
            ],
        }
    )
    rendered = _environment().get_template("email.html.j2").render(email=safe_email)
    sanitized = bleach.clean(
        rendered,
        tags=ALLOWED_HTML_TAGS,
        attributes=ALLOWED_HTML_ATTRIBUTES,
        protocols={"https"},
        strip=True,
        strip_comments=True,
    )
    return str(sanitized).strip()
