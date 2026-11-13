"""Generates bpm_overlay.ico: a dark rounded tile with a green pulse line."""
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=52, fill=(18, 19, 22, 255),
                    outline=(46, 204, 113, 255), width=12)
pts = [(36, 150), (84, 150), (104, 92), (128, 196), (152, 62), (172, 150), (220, 150)]
d.line(pts, fill=(46, 204, 113, 255), width=18, joint="curve")
for p in (pts[0], pts[-1]):
    d.ellipse((p[0] - 9, p[1] - 9, p[0] + 9, p[1] + 9), fill=(46, 204, 113, 255))
img.save("bpm_overlay.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote bpm_overlay.ico")
