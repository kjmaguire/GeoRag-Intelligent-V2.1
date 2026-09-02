"""The Pillow pixel ceiling is raised only inside the context, then restored."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.services.ingest.trusted_image import (
    MAX_TRUSTED_IMAGE_PIXELS,
    open_trusted_image,
    trusted_pillow_image_limit,
)


def test_limit_is_raised_inside_and_restored_after() -> None:
    before = Image.MAX_IMAGE_PIXELS

    with trusted_pillow_image_limit(123_456):
        assert Image.MAX_IMAGE_PIXELS == 123_456

    assert before == Image.MAX_IMAGE_PIXELS


def test_limit_is_restored_even_when_the_body_raises() -> None:
    before = Image.MAX_IMAGE_PIXELS

    with pytest.raises(RuntimeError), trusted_pillow_image_limit():
        raise RuntimeError("boom")

    assert before == Image.MAX_IMAGE_PIXELS


def test_non_positive_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        with trusted_pillow_image_limit(0):
            pass


def test_open_trusted_image_decodes_within_the_ceiling() -> None:
    buf = io.BytesIO()
    Image.new("L", (40, 30)).save(buf, format="PNG")

    image = open_trusted_image(buf.getvalue())

    assert image.size == (40, 30)
    assert MAX_TRUSTED_IMAGE_PIXELS > 40 * 30


def test_open_trusted_image_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        open_trusted_image(b"")
