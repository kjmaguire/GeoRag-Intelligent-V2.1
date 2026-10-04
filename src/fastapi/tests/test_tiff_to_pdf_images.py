"""Standalone images, frame handling and orientation in the PDF wrap.

PNG / BMP / GIF / WebP join JPEG as scanned-image uploads that take the same
Pillow -> PDF -> ingest_pdf path as a TIFF. The wrap must also be bounded by
one frame (it used to decode every frame into a list), say when it dropped
frames, and rotate EXIF-oriented photos upright.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image, features

from app.services.ingest import tiff_to_pdf as mod
from app.services.ingest.tiff_to_pdf import TiffNormalizeError, tiff_to_pdf


def _bytes(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _pdf_pages(pdf: bytes):
    import pypdf

    return pypdf.PdfReader(io.BytesIO(pdf)).pages


def _text_like(size=(120, 80), mode="RGB") -> Image.Image:
    """Black 'text' stripe on white, so flattening bugs are visible."""
    img = Image.new("RGB", size, "white")
    for x in range(10, size[0] - 10):
        for y in range(30, 36):
            img.putpixel((x, y), (0, 0, 0))
    return img.convert(mode) if mode != "RGB" else img


class TestStandaloneImagesBecomeOnePagePdf:
    def test_png_is_a_one_page_pdf(self) -> None:
        r = tiff_to_pdf(_bytes(_text_like(), "PNG"))
        assert r.pdf_bytes.startswith(b"%PDF-")
        assert r.page_count == 1
        assert len(_pdf_pages(r.pdf_bytes)) == 1
        assert r.truncated_at_cap is False
        assert r.frames_ignored == 0

    @pytest.mark.skipif(not features.check("webp"), reason="Pillow built without WebP")
    def test_webp_is_a_one_page_pdf(self) -> None:
        r = tiff_to_pdf(_bytes(_text_like(), "WEBP", lossless=True))
        assert r.page_count == 1
        assert len(_pdf_pages(r.pdf_bytes)) == 1

    def test_bmp_is_a_one_page_pdf(self) -> None:
        r = tiff_to_pdf(_bytes(_text_like(), "BMP"))
        assert r.page_count == 1
        assert len(_pdf_pages(r.pdf_bytes)) == 1

    def test_palette_gif_converts(self) -> None:
        gif = _text_like().convert("P", palette=Image.Palette.ADAPTIVE, colors=8)
        assert gif.mode == "P"
        r = tiff_to_pdf(_bytes(gif, "GIF"))
        assert r.page_count == 1
        assert len(_pdf_pages(r.pdf_bytes)) == 1

    def test_jpeg_still_works(self) -> None:
        r = tiff_to_pdf(_bytes(_text_like(), "JPEG"))
        assert r.page_count == 1


class TestModesAreFlattenedForPdf:
    @pytest.mark.parametrize("mode", ["P", "PA", "LA", "RGBA", "I;16", "I", "YCbCr", "F"])
    def test_normalise_yields_a_pdf_writable_mode(self, mode: str) -> None:
        try:
            img = Image.new(mode, (16, 16))
        except Exception:  # noqa: BLE001
            pytest.skip(f"Pillow cannot create {mode}")
        out = mod._normalise_frame_mode(img)
        assert out.mode in ("1", "L", "RGB", "CMYK")

    def test_bilevel_stays_bilevel(self) -> None:
        assert mod._normalise_frame_mode(Image.new("1", (8, 8))).mode == "1"

    def test_transparent_png_text_is_black_on_white_not_black_on_black(self) -> None:
        """Dropping alpha exposes the (black) colour under transparent pixels."""
        img = Image.new("RGBA", (40, 20), (0, 0, 0, 0))  # fully transparent
        for x in range(5, 35):
            img.putpixel((x, 10), (0, 0, 0, 255))      # opaque black 'text'
        out = mod._normalise_frame_mode(img)
        assert out.mode == "RGB"
        assert out.getpixel((0, 0)) == (255, 255, 255)
        assert out.getpixel((10, 10)) == (0, 0, 0)

    def test_la_is_flattened_to_white_grey(self) -> None:
        img = Image.new("LA", (10, 10), (0, 0))
        out = mod._normalise_frame_mode(img)
        assert out.mode == "L"
        assert out.getpixel((0, 0)) == 255

    def test_gif_palette_with_transparent_index_is_flattened_on_white(self) -> None:
        img = Image.new("P", (10, 10), 0)
        img.putpalette([0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254)
        img.info["transparency"] = 0
        out = mod._normalise_frame_mode(img)
        assert out.mode == "RGB"
        assert out.getpixel((0, 0)) == (255, 255, 255)

    def test_sixteen_bit_grey_is_scaled_not_clipped(self) -> None:
        """convert('L') on I;16 saturates every sample >255, i.e. a white page."""
        import numpy as np

        arr = np.full((6, 6), 40000, dtype="uint16")
        img = Image.fromarray(arr)
        assert img.mode == "I;16"
        assert img.convert("L").getpixel((0, 0)) == 255     # the old behaviour
        out = mod._normalise_frame_mode(img)
        assert out.mode == "L"
        assert out.getpixel((0, 0)) == 156                  # 40000 / 256

    def test_rgba_png_end_to_end(self) -> None:
        img = Image.new("RGBA", (50, 30), (0, 0, 0, 0))
        r = tiff_to_pdf(_bytes(img, "PNG"))
        assert r.page_count == 1


class TestExifOrientation:
    def _jpeg(self, orientation: int, size=(120, 60)) -> bytes:
        exif = Image.Exif()
        exif[0x0112] = orientation
        return _bytes(_text_like(size), "JPEG", exif=exif)

    def test_a_sideways_photo_is_rotated_upright(self) -> None:
        # Orientation 6: stored landscape (120x60), displays portrait (60x120).
        r = tiff_to_pdf(self._jpeg(6))
        page = _pdf_pages(r.pdf_bytes)[0]
        assert float(page.mediabox.height) > float(page.mediabox.width)
        assert r.pages_reoriented == 1

    def test_an_upright_photo_is_untouched(self) -> None:
        r = tiff_to_pdf(self._jpeg(1))
        page = _pdf_pages(r.pdf_bytes)[0]
        assert float(page.mediabox.width) > float(page.mediabox.height)
        assert r.pages_reoriented == 0

    def test_no_exif_is_untouched(self) -> None:
        r = tiff_to_pdf(_bytes(_text_like((120, 60)), "JPEG"))
        assert r.pages_reoriented == 0

    def test_a_damaged_exif_block_does_not_lose_the_page(self, monkeypatch) -> None:
        def _boom(self):
            raise ValueError("bad exif")

        monkeypatch.setattr(Image.Image, "getexif", _boom)
        r = tiff_to_pdf(_bytes(_text_like(), "JPEG"))
        assert r.page_count == 1

    def test_png_orientation_is_applied_too(self) -> None:
        exif = Image.Exif()
        exif[0x0112] = 8
        r = tiff_to_pdf(_bytes(_text_like((120, 60)), "PNG", exif=exif))
        page = _pdf_pages(r.pdf_bytes)[0]
        assert float(page.mediabox.height) > float(page.mediabox.width)


def _tiff(n: int, size=(60, 40), mode="L") -> bytes:
    imgs = [Image.new(mode, size, i * 10 % 256) for i in range(n)]
    buf = io.BytesIO()
    imgs[0].save(buf, "TIFF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


class TestFrameCap:
    def test_truncation_is_reported_with_the_real_frame_count(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "MAX_FRAMES", 3)
        r = tiff_to_pdf(_tiff(5))
        assert r.truncated_at_cap is True
        assert r.page_count == 3
        assert r.total_frames == 5
        assert len(_pdf_pages(r.pdf_bytes)) == 3

    def test_exactly_at_the_cap_is_not_truncated(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "MAX_FRAMES", 4)
        r = tiff_to_pdf(_tiff(4))
        assert r.truncated_at_cap is False
        assert r.total_frames == 4
        assert r.page_count == 4

    def test_multi_page_tiff_page_count(self) -> None:
        r = tiff_to_pdf(_tiff(7))
        assert (r.page_count, r.total_frames) == (7, 7)
        assert len(_pdf_pages(r.pdf_bytes)) == 7

    def test_page_sizes_follow_each_frame(self) -> None:
        a = Image.new("L", (100, 50), 0)
        b = Image.new("L", (50, 100), 255)
        buf = io.BytesIO()
        a.save(buf, "TIFF", save_all=True, append_images=[b])
        pages = _pdf_pages(tiff_to_pdf(buf.getvalue()).pdf_bytes)
        assert float(pages[0].mediabox.width) > float(pages[0].mediabox.height)
        assert float(pages[1].mediabox.width) < float(pages[1].mediabox.height)


class TestAnimationIsNotPages:
    def test_an_animated_gif_wraps_the_first_frame_only_and_says_so(self) -> None:
        frames = []
        for i in range(6):
            f = Image.new("RGB", (40, 30), (i * 40, 255 - i * 40, 7))
            f.putpixel((i, i), (255, 255, 255))
            frames.append(f.convert("P", palette=Image.Palette.ADAPTIVE, colors=16))
        buf = io.BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=50)
        r = tiff_to_pdf(buf.getvalue())
        assert r.page_count == 1
        assert r.total_frames == 6
        assert r.frames_ignored == 5
        assert r.truncated_at_cap is False
        assert len(_pdf_pages(r.pdf_bytes)) == 1


class TestMemoryIsBoundedByOneFrame:
    def test_each_page_is_released_before_the_next_is_decoded(self, monkeypatch) -> None:
        """The old code kept every decoded frame in a list until one save().
        Now a prepared page must be closed before the next one exists."""
        prepared: list[Image.Image] = []
        real = mod._prepare_frame

        def _tracking(frame):
            # By the time frame N is prepared, every earlier page is closed.
            for earlier in prepared:
                with pytest.raises(ValueError):
                    earlier.getpixel((0, 0))
            page, rotated = real(frame)
            prepared.append(page)
            return page, rotated

        monkeypatch.setattr(mod, "_prepare_frame", _tracking)
        r = tiff_to_pdf(_tiff(6))
        assert r.page_count == 6
        assert len(prepared) == 6

    def test_pages_are_written_one_by_one_not_via_append_images(self, monkeypatch) -> None:
        data = _tiff(4)   # built BEFORE the spy: building it also calls save()
        seen: list[dict] = []
        real_save = Image.Image.save
        written: list[int] = []
        real_page = mod._frame_to_pdf

        def _spy(self, fp, format=None, **params):  # noqa: A002
            seen.append(params)
            return real_save(self, fp, format, **params)

        def _count(page, dpi, *args, **kwargs):
            written.append(1)
            return real_page(page, dpi, *args, **kwargs)

        monkeypatch.setattr(Image.Image, "save", _spy)
        monkeypatch.setattr(mod, "_frame_to_pdf", _count)
        tiff_to_pdf(data)
        assert len(written) == 4, "one one-page PDF per frame"
        assert all("append_images" not in p and "save_all" not in p for p in seen)


class TestFailures:
    def test_garbage_is_a_normalize_error(self) -> None:
        with pytest.raises(TiffNormalizeError, match="PIL.Image.open failed"):
            tiff_to_pdf(b"definitely not an image")

    def test_a_frame_that_fails_midway_refuses_the_whole_wrap(self, monkeypatch) -> None:
        real = mod._frame_to_pdf
        calls = {"n": 0}

        def _flaky(page, dpi, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("disk full")
            return real(page, dpi, *args, **kwargs)

        monkeypatch.setattr(mod, "_frame_to_pdf", _flaky)
        with pytest.raises(TiffNormalizeError, match="frame 2 could not be read or wrapped"):
            tiff_to_pdf(_tiff(5))

    @pytest.mark.parametrize("dpi", [(0, 0), (1e7, 1e7), (-5, -5)])
    def test_a_placeholder_dpi_does_not_break_the_page_size(self, dpi) -> None:
        img = Image.new("L", (40, 40))
        img.info["dpi"] = dpi
        assert mod._effective_dpi(img) == 300.0

    def test_a_real_dpi_is_kept(self) -> None:
        img = Image.new("L", (40, 40))
        img.info["dpi"] = (200.0, 200.0)
        assert mod._effective_dpi(img) == 200.0


# ---------------------------------------------------------------------------
# Audit 2026-10-04 (F7): bit depth, and what "lossless" really is
# ---------------------------------------------------------------------------


def _page_pil(pdf: bytes, index: int = 0) -> Image.Image:
    """The decoded pixels of page *index*'s image XObject."""
    import pikepdf
    from pikepdf import PdfImage

    with pikepdf.Pdf.open(io.BytesIO(pdf)) as doc:
        xobj = next(iter(doc.pages[index].Resources.XObject.values()))
        return PdfImage(xobj).as_pil_image().copy()


def _page_filter(pdf: bytes, index: int = 0) -> str:
    import pikepdf

    with pikepdf.Pdf.open(io.BytesIO(pdf)) as doc:
        return str(next(iter(doc.pages[index].Resources.XObject.values())).Filter)


def _i16_tiff(values: list[int], size=(8, 4)) -> bytes:
    img = Image.new("I;16", size)
    img.putdata([values[i % len(values)] for i in range(size[0] * size[1])])
    buf = io.BytesIO()
    img.save(buf, "TIFF")
    return buf.getvalue()


class TestBitDepth:
    def test_a_12_bit_scan_in_16_bit_words_is_not_near_black(self) -> None:
        data = _i16_tiff([0, 1000, 2000, 3000, 4000])
        r = tiff_to_pdf(data)
        px = _page_pil(r.pdf_bytes).convert("L")
        # /256 would have made the brightest sample 15. Scaled by its own
        # maximum (4000) it reaches full range.
        assert px.getextrema()[1] == 255
        assert px.getpixel((4, 0)) == 255          # the 4000 sample
        assert 100 <= px.getpixel((2, 0)) <= 130   # 2000/4000 of full scale

    def test_a_true_16_bit_scan_still_divides_by_256(self) -> None:
        r = tiff_to_pdf(_i16_tiff([0, 32768, 65535]))
        px = _page_pil(r.pdf_bytes).convert("L")
        values = {px.getpixel((x, 0)) for x in range(3)}
        assert values == {0, 128, 255}

    def test_a_scan_that_already_fits_in_eight_bits_is_unchanged(self) -> None:
        r = tiff_to_pdf(_i16_tiff([0, 100, 200, 255]))
        px = _page_pil(r.pdf_bytes).convert("L")
        assert [px.getpixel((x, 0)) for x in range(4)] == [0, 100, 200, 255]

    def test_an_all_zero_frame_stays_black_and_does_not_divide_by_zero(self) -> None:
        r = tiff_to_pdf(_i16_tiff([0]))
        assert _page_pil(r.pdf_bytes).convert("L").getextrema() == (0, 0)

    def test_the_scale_boundary_is_the_frames_own_maximum(self) -> None:
        img = Image.new("I;16", (2, 1))
        img.putdata([0, 4095])
        assert mod._to_eight_bit(img).getpixel((1, 0)) == 255   # 12-bit: stretched
        img.putdata([0, 4096])
        assert mod._to_eight_bit(img).getpixel((1, 0)) == 16    # 16-bit: / 256


class TestWhatTheWrapActuallyPreserves:
    def _noisy(self, mode: str) -> Image.Image:
        import random

        rnd = random.Random(7)
        size = (64, 48)
        bands = len(mode)
        img = Image.new(mode, size)
        data = [
            tuple(rnd.randrange(256) for _ in range(bands)) if bands > 1 else rnd.randrange(256)
            for _ in range(size[0] * size[1])
        ]
        img.putdata(data)
        return img

    @pytest.mark.parametrize("mode", ["L", "RGB"])
    def test_grey_and_colour_pages_round_trip_every_pixel(self, mode: str) -> None:
        src = self._noisy(mode)
        buf = io.BytesIO()
        src.save(buf, "TIFF")
        r = tiff_to_pdf(buf.getvalue())

        assert "FlateDecode" in _page_filter(r.pdf_bytes)
        assert _page_pil(r.pdf_bytes).convert(mode).tobytes() == src.tobytes()
        assert r.pages_jpeg_encoded == 0

    def test_a_bilevel_page_stays_ccitt(self) -> None:
        img = Image.new("1", (64, 48), 1)
        for x in range(10, 50):
            img.putpixel((x, 20), 0)
        buf = io.BytesIO()
        img.save(buf, "TIFF", compression="group4")
        r = tiff_to_pdf(buf.getvalue())
        assert "CCITTFaxDecode" in _page_filter(r.pdf_bytes)

    def test_the_page_size_follows_pixels_and_dpi(self) -> None:
        img = Image.new("L", (300, 150), 255)
        buf = io.BytesIO()
        img.save(buf, "TIFF", dpi=(300, 300))
        page = _pdf_pages(tiff_to_pdf(buf.getvalue()).pdf_bytes)[0]
        assert float(page.mediabox.width) == pytest.approx(72.0, abs=0.01)
        assert float(page.mediabox.height) == pytest.approx(36.0, abs=0.01)

    def test_past_the_lossless_budget_pages_are_jpeg_and_counted(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_LOSSLESS_BUDGET_FRACTION", 0.0)
        r = tiff_to_pdf(_tiff(3, mode="L"))
        assert r.pages_jpeg_encoded == 3 and r.page_count == 3
        assert "DCTDecode" in _page_filter(r.pdf_bytes, 0)
        assert len(_pdf_pages(r.pdf_bytes)) == 3

    def test_the_budget_is_spent_page_by_page_not_all_or_nothing(self, monkeypatch) -> None:
        one_page = len(tiff_to_pdf(_tiff(1, size=(200, 200), mode="RGB")).pdf_bytes)
        monkeypatch.setattr(mod, "_LOSSLESS_BUDGET_FRACTION", one_page * 2.5 / mod.MAX_TIFF_BYTES)
        r = tiff_to_pdf(_tiff(4, size=(200, 200), mode="RGB"))
        assert 0 < r.pages_jpeg_encoded < 4

    def test_a_cmyk_page_past_the_budget_is_still_counted(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_LOSSLESS_BUDGET_FRACTION", 0.0)
        buf = io.BytesIO()
        Image.new("CMYK", (40, 30), (0, 0, 0, 255)).save(buf, "TIFF")
        r = tiff_to_pdf(buf.getvalue())
        assert r.pages_jpeg_encoded == 1 and r.page_count == 1

    def test_a_failing_pikepdf_writer_falls_back_to_pillows(self, monkeypatch) -> None:
        def _boom(*_a, **_k):
            raise RuntimeError("pikepdf broke")

        monkeypatch.setattr(mod, "_image_page_pdf", _boom)
        r = tiff_to_pdf(_tiff(2, mode="L"))
        assert r.page_count == 2 and len(_pdf_pages(r.pdf_bytes)) == 2
        assert r.pages_jpeg_encoded == 2     # Pillow's writer is JPEG for L

    def test_the_spool_is_removed_on_success_and_on_failure(self, monkeypatch) -> None:
        cleaned: list[str] = []
        real = mod.tempfile.TemporaryDirectory

        class _Spy(real):  # type: ignore[misc, valid-type]
            def cleanup(self) -> None:
                cleaned.append(self.name)
                super().cleanup()

        monkeypatch.setattr(mod.tempfile, "TemporaryDirectory", _Spy)
        tiff_to_pdf(_tiff(3))
        assert len(cleaned) == 1 and not __import__("os").path.exists(cleaned[0])

        real_frame = mod._frame_to_pdf
        calls = {"n": 0}

        def _flaky(page, dpi, *a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real_frame(page, dpi, *a, **k)

        monkeypatch.setattr(mod, "_frame_to_pdf", _flaky)
        with pytest.raises(TiffNormalizeError):
            tiff_to_pdf(_tiff(3))
        assert len(cleaned) == 2 and not __import__("os").path.exists(cleaned[1])


class TestPageSizeLimits:
    def test_a_page_wider_than_14400_points_is_still_flate_not_a_silent_fallback(self) -> None:
        # pikepdf's add_blank_page refuses > 14400 units; a 60,000-px survey
        # sheet at 300 dpi is 14,400 pt and larger ones exist.
        img = Image.new("L", (30000, 4), 200)
        buf = io.BytesIO()
        img.save(buf, "TIFF", dpi=(72, 72))
        r = tiff_to_pdf(buf.getvalue())
        assert "FlateDecode" in _page_filter(r.pdf_bytes)
        assert r.pages_jpeg_encoded == 0

    def test_a_thumbnail_page_under_three_points_is_still_flate(self) -> None:
        r = tiff_to_pdf(_tiff(1, size=(8, 4), mode="L"))
        assert "FlateDecode" in _page_filter(r.pdf_bytes)
