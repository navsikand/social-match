"""Pure image pipeline (CONTRACT-photos.md §1).

No DB, no filesystem, no FastAPI: `normalise(bytes) -> (bytes, 'jpg'|'png')` is the whole
surface, so it is testable as a plain function. The re-encode *is* the privacy guarantee —
EXIF/GPS/XMP/ICC/PNG text all disappear because nothing but pixels survives the copy.
"""
from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB, enforced by the endpoint while streaming
MAX_INPUT_PX = 8000  # either dimension, before decode
MAX_OUTPUT_PX = 1600  # long edge of what we store
JPEG_QUALITY = 85

# Decompression-bomb guard. Pillow raises DecompressionBombWarning above this and
# DecompressionBombError above 2x it; both are mapped to 'too_big' below.
Image.MAX_IMAGE_PIXELS = MAX_INPUT_PX * MAX_INPUT_PX

_ACCEPTED = {"JPEG": "jpg", "PNG": "png"}


class PhotoRejected(Exception):
    """A refused upload. `code` is what the endpoint maps to a status."""

    code: str  # 'too_large' | 'unsupported' | 'too_big' | 'corrupt'
    detail: str

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail or code


def normalise(data: bytes) -> tuple[bytes, str]:
    """Validate and re-encode. Returns (image_bytes, 'jpg'|'png').

    Steps, all mandatory: PIL.Image.open -> verify() -> reopen -> check dimensions against
    MAX_INPUT_PX and Image.MAX_IMAGE_PIXELS -> load() -> exif_transpose -> convert
    (RGB, or RGBA when the source has alpha) -> thumbnail(MAX_OUTPUT_PX) -> build a FRESH
    Image.new(mode, size) and paste pixels into it (so .info is empty) -> save with no
    metadata arguments at all -> return bytes + extension.

    Raises PhotoRejected('unsupported') for anything that is not a decodable JPEG/PNG,
    ('too_big') for a dimension/pixel-cap violation, ('corrupt') for a decode error,
    ('too_large') if the caller passes more than MAX_UPLOAD_BYTES.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise PhotoRejected("too_large", f"upload exceeds {MAX_UPLOAD_BYTES} bytes")

    # 1. Identify: open + verify (verify leaves the object unusable, so we reopen next).
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            probe = Image.open(io.BytesIO(data))
            ext = _ACCEPTED.get(probe.format or "")
            if ext is None:
                raise PhotoRejected("unsupported", f"format {probe.format!r} not accepted")
            probe.verify()
    except PhotoRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PhotoRejected("too_big", "image exceeds the pixel cap") from exc
    except UnidentifiedImageError as exc:
        raise PhotoRejected("unsupported", "not a JPEG or PNG image") from exc
    except Exception as exc:  # noqa: BLE001 - identified as JPEG/PNG but truncated or broken
        raise PhotoRejected("corrupt", str(exc) or exc.__class__.__name__) from exc

    # 2. Reopen for real decoding, and enforce the dimension cap before load().
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PhotoRejected("too_big", "image exceeds the pixel cap") from exc
    except Exception as exc:  # noqa: BLE001
        raise PhotoRejected("corrupt", "image cannot be reopened") from exc

    if img.width > MAX_INPUT_PX or img.height > MAX_INPUT_PX:
        raise PhotoRejected(
            "too_big", f"{img.width}x{img.height} exceeds {MAX_INPUT_PX} px per side"
        )
    if img.width * img.height > MAX_INPUT_PX * MAX_INPUT_PX:
        raise PhotoRejected("too_big", "image exceeds the pixel cap")

    # 3. Full decode: truncated or broken pixel data dies here.
    try:
        img.load()
    except Exception as exc:  # noqa: BLE001 - OSError, struct.error, warnings-as-errors, ...
        raise PhotoRejected("corrupt", "image data could not be decoded") from exc

    # 4. Bake orientation into pixels, then drop the tag with the rest of the metadata.
    try:
        img = ImageOps.exif_transpose(img)
    except Exception as exc:  # noqa: BLE001 - malformed EXIF must not crash the request
        raise PhotoRejected("corrupt", "image orientation could not be read") from exc

    # 5. Colour model: keep alpha only when the source actually had it.
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )
    target_mode = "RGBA" if has_alpha else "RGB"
    if img.mode != target_mode:
        img = img.convert(target_mode)

    img.thumbnail((MAX_OUTPUT_PX, MAX_OUTPUT_PX), Image.Resampling.LANCZOS)

    # 6. Pixel-only boundary: a fresh image carries an empty .info, and the encoder below is
    #    given no metadata arguments at all. Passing no arguments is *not* enough on its own.
    clean = Image.new(target_mode, img.size)
    clean.paste(img)

    out = io.BytesIO()
    if ext == "jpg":
        clean.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    else:
        clean.save(out, format="PNG", optimize=True)
    return out.getvalue(), ext
