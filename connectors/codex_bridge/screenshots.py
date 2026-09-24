"""Decode uploaded screenshots into bounded PNGs in the paper's private directory."""
from io import BytesIO
from pathlib import Path
import re
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_SCREENSHOTS = 4


def screenshot_path(directory, identifier):
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ValueError('截图编号无效。')
    parent = Path(directory).resolve()
    folder = (parent / 'screenshots').resolve()
    path = (folder / (identifier + '.png')).resolve()
    if folder.parent != parent or path.parent != folder:
        raise ValueError('截图路径无效。')
    return path


def save_screenshot(content, directory, filename):
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError('截图为空或超过 8 MB，请选择较小的图片。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as probe:
                if probe.format not in ('PNG', 'JPEG', 'WEBP'):
                    raise ValueError('请上传 PNG、JPEG 或 WebP 截图。')
                if probe.width * probe.height > MAX_IMAGE_PIXELS or getattr(probe, 'n_frames', 1) != 1:
                    raise ValueError('截图需为单帧图片，且不超过 1600 万像素。')
                probe.verify()
            with Image.open(BytesIO(content)) as original:
                original.load()
                image = ImageOps.exif_transpose(original).convert('RGBA')
                background = Image.new('RGB', image.size, 'white')
                background.paste(image, mask=image.getchannel('A'))
                output = BytesIO()
                background.save(output, format='PNG')  # Drop EXIF and other uploaded metadata.
                normalized = output.getvalue()
                if len(normalized) > 12 * 1024 * 1024:
                    raise ValueError('截图展开后过大，请裁剪后再上传。')
                width, height = background.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('无法读取截图，请检查图片是否损坏或另存为 PNG。') from None
    identifier = uuid.uuid4().hex
    path = screenshot_path(directory, identifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(normalized)
    name = re.split(r'[/\\]', filename or '截图.png')[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)[:120] or '截图.png'
    return {'id': identifier, 'name': name, 'width': width, 'height': height}
