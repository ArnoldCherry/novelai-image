"""将聊天上传图片校验并转换为官方 Precise Reference 画布。"""
from io import BytesIO
from PIL import Image, ImageOps
import base64

from .client import validate_image


def prepare_uploaded_reference(encoded: str, max_bytes: int) -> str:
    data = base64.b64decode(validate_image(encoded, max_bytes), validate=True)
    with Image.open(BytesIO(data)) as source:
        source = ImageOps.exif_transpose(source).convert("RGBA")
        size = (1536, 1024) if source.width > source.height * 1.2 else ((1024, 1536) if source.height > source.width * 1.2 else (1472, 1472))
        source = ImageOps.contain(source, size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size, "black")
        canvas.paste(source, ((size[0] - source.width) // 2, (size[1] - source.height) // 2), source.getchannel("A"))
        output = BytesIO()
        canvas.save(output, format="PNG")
        return base64.b64encode(output.getvalue()).decode("ascii")
