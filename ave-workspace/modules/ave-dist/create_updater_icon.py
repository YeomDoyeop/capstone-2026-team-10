"""AVE 업데이터용 아래 방향 화살표 Windows ICO를 생성한다."""

from pathlib import Path
import sys

from PIL import Image, ImageDraw

output = Path(sys.argv[1])
image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((0, 0, 255, 255), radius=48, fill="#000000")
orange = "#f97316"
# 작은 크기에서도 형태가 흐트러지지 않는 단일 다운로드 화살표다.
draw.rectangle((108, 48, 148, 148), fill=orange)
draw.polygon(((64, 132), (192, 132), (128, 204)), fill=orange)
image.save(
    output,
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
