"""Generate Apple Touch Icon (180x180) from the existing 512px icon."""
from pathlib import Path
from PIL import Image

ICONS_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"

def generate():
    src = Image.open(ICONS_DIR / "icon-512.png")
    for size, name in [(180, "apple-touch-icon.png")]:
        img = src.resize((size, size), Image.LANCZOS)
        img.save(ICONS_DIR / name)
        print(f"  ✓ {name} ({size}x{size})")

if __name__ == "__main__":
    generate()
