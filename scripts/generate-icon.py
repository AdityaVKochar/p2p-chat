from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "desktop" / "assets"
SIZE = 1024


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = 84
    draw.rounded_rectangle(
        (inset, inset, SIZE - inset, SIZE - inset),
        radius=238,
        fill="#315f50",
    )
    font_path = Path("C:/Windows/Fonts/segoeuib.ttf")
    font = ImageFont.truetype(str(font_path), 500) if font_path.exists() else ImageFont.load_default()
    bounds = draw.textbbox((0, 0), "P", font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.text(
        ((SIZE - width) / 2, (SIZE - height) / 2 - bounds[1] - 18),
        "P",
        font=font,
        fill="white",
    )
    image.save(ASSETS / "icon.png", optimize=True)
    image.save(
        ASSETS / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()

