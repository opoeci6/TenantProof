"""Build demo/landlord_text_zh.png: a synthetic phone screenshot of a chat (see demo/README.md).

    uv run --directory backend python ../demo/prepare_screenshot.py

Drawn with the app's bundled Noto Sans SC, so it is the same on every machine with the same Pillow.
Everything in it is made up. It is there to show OCR: the app reads the text in the image on its
own server, and the agent can cite it as "text in the photo (OCR)". The PNG carries no metadata."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONT = HERE.parent / "backend" / "app" / "assets" / "fonts" / "NotoSansSC-Regular.ttf"
OUT = HERE / "landlord_text_zh.png"
W, H = 1080, 1300
BG, BAR, WHITE, GREEN, INK, GREY = "#EDEDED", "#F7F7F7", "#FFFFFF", "#95EC69", "#111111", "#888888"

# (side, lines): the landlord on the left, the tenant (陈梅) on the right.
MESSAGES = [
    ("left", ["水管工会在10月10日上午", "来修浴室的墙。", "请那天上午在家。"]),
    ("right", ["好的，我那天在家。"]),
    ("left", ["墙上的霉点我们会一起处理。"]),
]


def main() -> None:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    title, body, small = (ImageFont.truetype(str(FONT), s) for s in (48, 44, 32))
    d.rectangle((0, 0, W, 140), fill=BAR)
    d.text((W // 2, 70), "房东", font=title, fill=INK, anchor="mm")
    d.text((W // 2, 210), "2026年10月3日 下午2:22", font=small, fill=GREY, anchor="mm")
    y = 280
    for side, lines in MESSAGES:
        width = max(int(d.textlength(line, font=body)) for line in lines) + 60
        height = 66 * len(lines) + 40
        x0 = 60 if side == "left" else W - 60 - width
        d.rounded_rectangle((x0, y, x0 + width, y + height), radius=18,
                            fill=WHITE if side == "left" else GREEN)
        for n, line in enumerate(lines):
            d.text((x0 + 30, y + 22 + n * 66), line, font=body, fill=INK)
        y += height + 50
    img.save(OUT, "PNG", optimize=True)  # a fresh image: no metadata of any kind
    print(f"wrote {OUT} ({W}x{H})")


if __name__ == "__main__":
    main()
