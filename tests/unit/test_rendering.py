from __future__ import annotations

from apps.api.app.domain.models import EmailArtifact, EmailSection
from apps.api.app.services.rendering import render_email_html, sms_metrics


def test_gsm7_basic_extension_and_segment_boundaries() -> None:
    assert sms_metrics("A" * 160).model_dump() == {
        "encoding": "GSM-7",
        "characters": 160,
        "code_units": 160,
        "septets": 160,
        "segments": 1,
        "units_per_segment": 160,
    }
    assert sms_metrics("A" * 161).segments == 2
    assert sms_metrics("^" * 80).segments == 1
    assert sms_metrics("^" * 81).segments == 2


def test_ucs2_cyrillic_and_emoji_code_unit_boundaries() -> None:
    assert sms_metrics("Я" * 70).segments == 1
    assert sms_metrics("Я" * 71).segments == 2
    emoji = sms_metrics("😀" * 36)

    assert emoji.encoding == "UCS-2"
    assert emoji.characters == 36
    assert emoji.code_units == 72
    assert emoji.segments == 2


def test_email_renderer_escapes_content_and_keeps_only_https_cta() -> None:
    email = EmailArtifact(
        subject="Синтетическое письмо",
        preheader="Без отправки",
        headline="Проверка",
        sections=[
            EmailSection(
                section_id="section_body",
                kind="body",
                heading="Описание",
                body='<script>alert("x")</script><img src=x onerror=alert(1)>',
                fact_refs=[],
                personalization_refs=[],
            )
        ],
        cta_label="Открыть",
        cta_url="https://safe.example.test/open",
        disclaimer_ids=[],
        plain_text="Проверка",
        fact_refs=[],
        personalization_refs=[],
    )

    rendered = render_email_html(email)

    assert "<script" not in rendered.lower()
    assert "<img" not in rendered.lower()
    assert "onerror" not in rendered.lower()
    assert "https://safe.example.test/open" in rendered
    assert "SYNTHETIC · NO SEND" in rendered
