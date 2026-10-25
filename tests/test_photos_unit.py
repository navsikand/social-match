"""Unit tests for server/photos.py — the pure image pipeline (CONTRACT-photos.md §1).

Fixtures are built in memory with Pillow; no network, no files, no DB. These tests assert the
privacy promise structurally (reopen the *output* bytes and inspect exif/info), never by
substring-searching the bytes for a keyword.
"""
from __future__ import annotations

import io
import os
import struct
import zlib

import pytest
from PIL import Image, ImageCms, PngImagePlugin

from server import photos


# --------------------------------------------------------------------------- fixtures
def _jpeg(size=(64, 48), mode="RGB", color="red", **save_kwargs) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="JPEG", **save_kwargs)
    return buf.getvalue()


def _png(size=(64, 48), mode="RGB", color="red", **save_kwargs) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG", **save_kwargs)
    return buf.getvalue()


def _icc_profile() -> bytes:
    """A real 588-byte sRGB ICC profile (Pillow ships the CMM, so no external file needed)."""
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _exif_with_gps(orientation: int | None = None) -> bytes:
    """Minimal EXIF with a real GPS IFD built through PIL.Image.Exif.get_ifd(0x8825).

    `get_ifd` returns (and registers) the sub-IFD mapping, so writing latitude/longitude into
    it is enough — `tobytes()` serialises it as an APP1 GPS IFD. piexif is not needed.
    """
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation  # Orientation
    exif[0x010F] = "TestCam"  # Make
    exif[0x010E] = "description that must not survive"  # ImageDescription
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"  # GPSLatitudeRef
    gps[2] = (37.0, 48.0, 30.0)  # GPSLatitude
    gps[3] = "W"  # GPSLongitudeRef
    gps[4] = (122.0, 25.0, 0.0)  # GPSLongitude
    return exif.tobytes()


def _jpeg_with_everything() -> bytes:
    """JPEG carrying EXIF+GPS, an ICC profile and a COM comment."""
    return _jpeg(
        size=(80, 60),
        exif=_exif_with_gps(),
        icc_profile=_icc_profile(),
        comment=b"SECRET-COMMENT-MARKER",
    )


def _png_with_everything() -> bytes:
    """PNG carrying a tEXt chunk (via PngInfo), an eXIf chunk and an iCCP profile."""
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "SECRET-TEXT-MARKER lat=37.7749 lon=-122.4194")
    return _png(
        size=(80, 60),
        pnginfo=info,
        icc_profile=_icc_profile(),
    )


def _png_claiming(width: int, height: int) -> bytes:
    """A structurally valid PNG whose IHDR lies about its dimensions (a decompression bomb).

    Built by taking a real 1x1 PNG and patching the IHDR width/height with a recomputed CRC, so
    the file passes chunk-CRC checks and Pillow only learns the true size from the header.
    """
    raw = _png(size=(1, 1))
    assert raw[12:16] == b"IHDR"
    ihdr = bytearray(raw[16:29])  # 13-byte IHDR payload
    ihdr[0:4] = struct.pack(">I", width)
    ihdr[4:8] = struct.pack(">I", height)
    crc = struct.pack(">I", zlib.crc32(b"IHDR" + bytes(ihdr)) & 0xFFFFFFFF)
    return raw[:16] + bytes(ihdr) + crc + raw[33:]


# --------------------------------------------------------------------------- happy path
def test_jpeg_roundtrip_returns_bytes_and_extension():
    data, ext = photos.normalise(_jpeg())
    assert ext == "jpg"
    assert isinstance(data, bytes) and data
    reopened = Image.open(io.BytesIO(data))
    assert reopened.format == "JPEG"
    assert reopened.mode == "RGB"
    reopened.load()  # must actually decode, not just open


def test_png_roundtrip_returns_bytes_and_extension():
    data, ext = photos.normalise(_png())
    assert ext == "png"
    reopened = Image.open(io.BytesIO(data))
    assert reopened.format == "PNG"
    reopened.load()


# --------------------------------------------------------------------------- metadata
def test_jpeg_metadata_is_stripped():
    source = _jpeg_with_everything()

    # Sanity-check the fixture: it really does carry GPS, ICC and a comment.
    probe = Image.open(io.BytesIO(source))
    assert dict(probe.getexif().get_ifd(0x8825)), "fixture lost its GPS IFD"
    assert probe.info.get("icc_profile")
    assert probe.info.get("comment")
    assert dict(probe.getexif()).get(0x010E)

    data, ext = photos.normalise(source)
    assert ext == "jpg"

    reopened = Image.open(io.BytesIO(data))
    reopened.load()
    assert dict(reopened.getexif()) == {}, reopened.getexif()
    for key in ("exif", "icc_profile", "comment", "pnginfo", "text"):
        assert key not in reopened.info, f"{key} survived normalise()"
    assert b"SECRET-COMMENT-MARKER" not in data


def test_png_metadata_is_stripped():
    source = _png_with_everything()
    probe = Image.open(io.BytesIO(source))
    assert "Comment" in probe.info, "fixture lost its tEXt chunk"
    assert probe.info.get("icc_profile")

    data, ext = photos.normalise(source)
    assert ext == "png"

    reopened = Image.open(io.BytesIO(data))
    reopened.load()
    assert dict(reopened.getexif()) == {}
    for key in ("exif", "icc_profile", "comment", "pnginfo", "text"):
        assert key not in reopened.info, f"{key} survived normalise()"
    # PNG text chunks never survive the pixel-only copy, in the bytes themselves either.
    assert b"tEXt" not in data
    assert b"iTXt" not in data
    assert b"eXIf" not in data
    assert b"SECRET-TEXT-MARKER" not in data


# --------------------------------------------------------------------------- orientation
def test_exif_orientation_is_applied_to_pixels():
    # Portrait 30x60 tagged Orientation=6 (rotate 90° CW) must come out 60x30 landscape:
    # the rotation was baked into pixels, not preserved as a tag.
    source = _jpeg(size=(30, 60), exif=_exif_with_gps(orientation=6))
    assert Image.open(io.BytesIO(source)).size == (30, 60)

    data, ext = photos.normalise(source)
    assert ext == "jpg"
    reopened = Image.open(io.BytesIO(data))
    reopened.load()
    assert reopened.size == (60, 30)
    exif = reopened.getexif()
    assert dict(exif) == {}
    assert exif.get(0x0112) is None  # no orientation tag left to undo the rotation


def test_orientation_1_is_a_no_op():
    source = _jpeg(size=(30, 60), exif=_exif_with_gps(orientation=1))
    data, _ = photos.normalise(source)
    assert Image.open(io.BytesIO(data)).size == (30, 60)


# --------------------------------------------------------------------------- resize
def test_large_image_is_thumbnailed_to_the_long_edge_cap():
    data, ext = photos.normalise(_jpeg(size=(4000, 3000)))
    assert ext == "jpg"
    out = Image.open(io.BytesIO(data))
    assert out.size == (photos.MAX_OUTPUT_PX, 1200)
    assert max(out.size) <= photos.MAX_OUTPUT_PX
    assert out.size[0] / out.size[1] == pytest.approx(4000 / 3000, rel=0.01)


def test_small_image_is_not_upscaled():
    data, _ = photos.normalise(_jpeg(size=(120, 90)))
    assert Image.open(io.BytesIO(data)).size == (120, 90)


# --------------------------------------------------------------------------- alpha
def test_rgba_png_keeps_transparency():
    source = io.BytesIO()
    img = Image.new("RGBA", (40, 40), (255, 0, 0, 128))
    img.putpixel((0, 0), (0, 0, 0, 0))
    img.save(source, format="PNG")

    data, ext = photos.normalise(source.getvalue())
    assert ext == "png"
    reopened = Image.open(io.BytesIO(data))
    reopened.load()
    assert reopened.mode == "RGBA"
    alpha = reopened.getchannel("A").tobytes()  # row-major, 40 px wide
    assert alpha[0] == 0  # the fully transparent corner survived
    assert alpha[20 * 40 + 20] == 128


def test_rgb_jpeg_has_no_alpha_channel():
    data, _ = photos.normalise(_jpeg())
    assert Image.open(io.BytesIO(data)).mode == "RGB"


# --------------------------------------------------------------------------- rejections
def test_text_bytes_are_unsupported():
    with pytest.raises(photos.PhotoRejected) as exc:
        photos.normalise(b"this is not an image, whatever the extension says\n" * 10)
    assert exc.value.code == "unsupported"
    assert exc.value.detail


def test_non_jpeg_png_format_is_unsupported():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="GIF")
    with pytest.raises(photos.PhotoRejected) as exc:
        photos.normalise(buf.getvalue())
    assert exc.value.code == "unsupported"


def test_truncated_jpeg_is_corrupt():
    with pytest.raises(photos.PhotoRejected) as exc:
        photos.normalise(_jpeg(size=(64, 48))[:200])
    assert exc.value.code == "corrupt"


def test_png_claiming_huge_dimensions_is_too_big():
    with pytest.raises(photos.PhotoRejected) as exc:
        photos.normalise(_png_claiming(20000, 20000))
    assert exc.value.code == "too_big"


def test_oversized_dimension_is_too_big():
    with pytest.raises(photos.PhotoRejected) as exc:
        photos.normalise(_jpeg(size=(photos.MAX_INPUT_PX + 1, 10)))
    assert exc.value.code == "too_big"


def test_six_megabyte_buffer_is_too_large():
    with pytest.raises(photos.PhotoRejected) as exc:
        photos.normalise(b"\x00" * (6 * 1024 * 1024))
    assert exc.value.code == "too_large"


def test_upload_cap_is_exclusive_at_the_boundary():
    # Fake PNG header of exactly MAX_UPLOAD_BYTES: accepted by the size check, then rejected
    # as unsupported — proving the cap comparison is `>` not `>=`.
    with pytest.raises(photos.PhotoRejected) as exc:
        photos.normalise(b"\x89PNG\r\n\x1a\n" + b"\x00" * (photos.MAX_UPLOAD_BYTES - 8))
    assert exc.value.code == "unsupported"


# --------------------------------------------------------------------------- surface
def test_module_surface_matches_the_contract():
    assert photos.MAX_UPLOAD_BYTES == 5 * 1024 * 1024
    assert photos.MAX_INPUT_PX == 8000
    assert photos.MAX_OUTPUT_PX == 1600
    assert photos.JPEG_QUALITY == 85
    assert issubclass(photos.PhotoRejected, Exception)
    assert os.path.basename(photos.__file__) == "photos.py"
