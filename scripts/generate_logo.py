from PIL import Image, ImageDraw, ImageFont
import os

width, height = 420, 140
img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Background pill with glowing border
draw.rounded_rectangle([(10, 10), (width - 10, height - 10)], radius=24, fill=(15, 23, 42, 220), outline=(56, 189, 248, 255), width=3)

# Inner badge accent
draw.rounded_rectangle([(24, 28), (74, 78)], radius=12, fill=(14, 165, 233, 240))
draw.polygon([(40, 40), (40, 66), (62, 53)], fill=(255, 255, 255, 255))

# Text
try:
    font_large = ImageFont.truetype("arial.ttf", 26)
    font_small = ImageFont.truetype("arial.ttf", 14)
except Exception:
    font_large = ImageFont.load_default()
    font_small = ImageFont.load_default()

draw.text((90, 30), "CLOUD VIDEO", font=font_large, fill=(255, 255, 255, 255))
draw.text((90, 62), "PROCESSING PLATFORM", font=font_small, fill=(56, 189, 248, 240))
draw.text((24, 94), "★ HIGH PERFORMANCE CLUSTER ★", font=font_small, fill=(148, 163, 184, 220))

os.makedirs("data/samples", exist_ok=True)
os.makedirs("frontend/public/samples", exist_ok=True)

img.save("data/samples/watermark_logo.png")
img.save("frontend/public/samples/watermark_logo.png")
print("Saved watermark_logo.png successfully!")
