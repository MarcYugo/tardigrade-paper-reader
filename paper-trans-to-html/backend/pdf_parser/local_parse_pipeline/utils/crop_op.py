import io
import re
import base64
import pymupdf
from typing import Sequence, Any
from PIL import Image, ImageDraw, ImageOps

def image_encode_base64(image: Image.Image, fmt="png"):
    out_buffer = io.BytesIO()
    image.save(out_buffer, format=fmt)
    byte_data = out_buffer.getvalue()
    base64_str = base64.b64encode(byte_data).decode("utf-8")
    return f"data:image/{fmt};base64," + base64_str

def base64_to_image(base64_str: str) -> Image.Image:
    base64_data = re.sub('^data:image/.+;base64,', '', base64_str)
    byte_data = base64.b64decode(base64_data)
    image_data = io.BytesIO(byte_data)
    img = Image.open(image_data)
    return img

def crop_formula_image(
    page: pymupdf.Page,
    bbox: Sequence[float],
    *,
    dpi: int = 300,
    padding: float = 2.0,
    clearance: Sequence[float | None] | None = None,
    margin: int = 6,
    mask_rects: Sequence[Sequence[float]] = (),
    keep_rects: Sequence[Sequence[float]] = (),
) -> tuple[Image.Image | None, pymupdf.Rect | None]:
    r"""把公式框渲染成 PIL 图片。

    Args:
        page: PyMuPDF 页面对象。
        bbox: 公式框 ``(x0, y0, x1, y1)``，PDF 点坐标（y 向下）。
        dpi: 渲染分辨率；300 对 7~10pt 的上下标够用，400 更清楚但更慢。
        padding: 裁剪时向外扩的边距（点）。
        clearance: 可选的 ``(left, top, right, bottom)`` 四边可用留白（点）。
            公式框贴到同行/相邻行文字时，用它限制该侧 padding，避免把
            文字带进裁剪图；``None`` 表示该侧不限制。
        margin: 图片四周再补的纯白像素，给 texify 一点呼吸空间。
        mask_rects: 要涂白的矩形（PDF 点坐标）——通常是**同一页其它公式框**
            的碎片 bbox。行内分式的分母会挂到下一行，下一行公式的上半截
            就会落进本框的裁剪矩形里（实测 `n(a₁+aₙ)/2` 框里混进了下一行
            的 `∑∞`，识别成 `\frac{…}{\infty}`），涂白后裁剪图只留本公式。
        keep_rects: 本公式自己的碎片 bbox，涂白时从这里扣除（外扩几像素，
            保住分数线、上下标的抗锯齿边缘）。

    Returns:
        ``(图片, 实际裁剪矩形)``；框退化时返回 ``(None, None)``。
    """
    rect = pymupdf.Rect(bbox)

    pads = [float(padding)] * 4
    if clearance is not None and len(clearance) == 4:
        for i, value in enumerate(clearance):
            if value is None:
                continue
            if i in (0, 2):
                # 左右：正文字符可能有 ink overhang（如 f 的右钩），
                # 留 1.5pt 安全边距；贴边时允许向内收 0.3pt。
                pads[i] = min(pads[i], max(-0.3, float(value) - 1.5))
            else:
                # 上下：留 0.3pt 余量，避免字形抗锯齿边缘被带进来
                pads[i] = min(pads[i], max(0.0, float(value) - 0.3))

    # 负 padding 会向内裁；极小的框不能被裁没
    if rect.width + pads[0] + pads[2] < 1.0:
        pads[0] = pads[2] = 0.0
    if rect.height + pads[1] + pads[3] < 1.0:
        pads[1] = pads[3] = 0.0

    rect = pymupdf.Rect(
        rect.x0 - pads[0],
        rect.y0 - pads[1],
        rect.x1 + pads[2],
        rect.y1 + pads[3],
    )
    rect = rect & page.rect  # 不能越界，否则 get_pixmap 会报错

    if rect.is_empty or rect.width < 0.5 or rect.height < 0.5:
        return None, None

    pix = page.get_pixmap(clip=rect, dpi=int(round(float(dpi))), alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")

    if mask_rects:
        # 把别的公式框伸进裁剪矩形里的碎片涂白；本公式自己的碎片
        # （外扩 1pt / 纵向 1.5pt，保住分数线）保留。
        sx = image.width / rect.width
        sy = image.height / rect.height
        mask = Image.new("L", image.size, 0)
        draw = ImageDraw.Draw(mask)

        def to_px(r):
            return (
                (float(r[0]) - rect.x0) * sx,
                (float(r[1]) - rect.y0) * sy,
                (float(r[2]) - rect.x0) * sx,
                (float(r[3]) - rect.y0) * sy,
            )

        for r in mask_rects:
            if float(r[2]) <= rect.x0 or float(r[0]) >= rect.x1:
                continue
            if float(r[3]) <= rect.y0 or float(r[1]) >= rect.y1:
                continue
            draw.rectangle(to_px(r), fill=255)

        for r in keep_rects:
            x0, y0, x1, y1 = to_px(r)
            draw.rectangle(
                (x0 - 0.4 * sx, y0 - 1.4 * sy, x1 + 0.4 * sx, y1 + 1.4 * sy),
                fill=0,
            )

        image.paste(Image.new("RGB", image.size, "white"), mask=mask)

    if margin > 0:
        image = ImageOps.expand(image, border=int(margin), fill="white")

    return image, rect

def crop_from_page(
    page: Any,
    bbox: Sequence[float],
    *,
    dpi: int = 300,
    padding: float = 2.0,
    margin: int = 6,
) -> Image.Image | None:
    """按 PDF 点坐标框裁剪页面；框退化时返回 None。"""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        import fitz as pymupdf  # type: ignore

    rect = pymupdf.Rect(bbox)
    rect = pymupdf.Rect(
        rect.x0 - padding,
        rect.y0 - padding,
        rect.x1 + padding,
        rect.y1 + padding,
    )
    rect = rect & page.rect
    if rect.is_empty or rect.width < 0.5 or rect.height < 0.5:
        return None

    pix = page.get_pixmap(
        clip=rect,
        dpi=int(round(float(dpi))),
        colorspace=pymupdf.csRGB,
        alpha=False,
    )
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if margin > 0:
        image = ImageOps.expand(image, border=int(margin), fill="white")
    return image