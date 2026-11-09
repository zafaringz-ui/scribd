"""build_pdf.py — assemble ordered page images into a single PDF."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


def build_pdf(image_paths: Iterable[Path], output_path: Path) -> Path:
    paths = [str(Path(p)) for p in image_paths]
    if not paths:
        raise ValueError("No images to assemble.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Preferred: img2pdf embeds the original JPEG bytes losslessly (small file,
    # no re-encode). Raises on unsupported formats -> fall through to Pillow.
    try:
        import img2pdf
        data = img2pdf.convert(paths)
        output_path.write_bytes(data)
        return output_path
    except Exception:
        pass

    # Fallback: Pillow re-encodes and normalises any format/alpha channel.
    from PIL import Image

    frames = []
    for p in paths:
        img = Image.open(p)
        if img.mode in ("RGBA", "P", "LA", "L"):
            img = img.convert("RGB")
        frames.append(img)

    first, rest = frames[0], frames[1:]
    first.save(output_path, "PDF", resolution=150.0,
               save_all=True, append_images=rest)
    return output_path