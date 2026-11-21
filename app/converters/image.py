from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError

SUPPORTED_OUTPUTS = {"jpg", "png", "webp"}

def _open_normalized(source_path: Path) -> Image.Image:
    image = Image.open(source_path)
    image = ImageOps.exif_transpose(image)
    # Konversi animasi menjadi frame pertama; animasi GIF belum didukung.
    if getattr(image, "is_animated", False):
        image.seek(0)
    return image.copy()

def image_to_image(
    source_path: Path,
    output_path: Path,
    output_format: str,
    quality: int = 90,
    background: str = "white",
) -> None:
    output_format = output_format.lower()
    if output_format not in SUPPORTED_OUTPUTS:
        raise ValueError(f"Unsupported output format: {output_format}")

    with _open_normalized(source_path) as original:
        if output_format == "jpg":
            # JPEG tidak mendukung alpha/transparansi.
            rgba = original.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, background)
            canvas.paste(rgba, mask=rgba.getchannel("A"))
            result = canvas
            result.save(output_path, format="JPEG", quality=quality, optimize=True)
        elif output_format == "png":
            result = original.convert("RGBA" if "A" in original.getbands() else "RGB")
            result.save(output_path, format="PNG", optimize=True)
        else:
            result = original.convert("RGBA" if "A" in original.getbands() else "RGB")
            result.save(output_path, format="WEBP", quality=quality, method=6)

def images_to_pdf(source_paths: list[Path], output_path: Path) -> None:
    if not source_paths:
        raise ValueError("At least one image is required.")

    pages: list[Image.Image] = []
    try:
        for path in source_paths:
            with _open_normalized(path) as image:
                # PDF page images use RGB to ensure broad compatibility.
                if "A" in image.getbands():
                    rgba = image.convert("RGBA")
                    page = Image.new("RGB", rgba.size, "white")
                    page.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    page = image.convert("RGB")
                pages.append(page.copy())

        first, *rest = pages
        first.save(output_path, "PDF", save_all=True, append_images=rest, resolution=150.0)
    finally:
        for page in pages:
            page.close()
