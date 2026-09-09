"""클라이언트 런타임 아이콘과 같은 Windows ICO를 생성한다."""

from pathlib import Path
import sys

from PIL import Image, ImageDraw

output = Path(sys.argv[1])
image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((0, 0, 255, 255), radius=48, fill="#000000")
draw.polygon(((92, 68), (92, 188), (196, 128)), fill="#f97316")
image.save(
    output,
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
