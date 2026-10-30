import pymupdf
from utils.crop_op import crop_from_page, image_encode_base64

def extrac_figures_pictures(page: pymupdf.Page, dpi: int = 600):
    images = page.get_drawings()
    full_figs = []
    for item in images:
        if item["items"][0][0] == "re":
            item = {
                "bbox": tuple(item["rect"]),
                "label": "figure",
                "dpi": dpi,
                "image": "",
            }
            img = crop_from_page(page=page, bbox=item["bbox"], dpi=dpi)
            item["image"] = image_encode_base64(image=img)
            full_figs.append(item)
    # images, full_pics = [], []
    # for img in page.get_images():
    #     images += page.get_image_rects(img[0])
    # for rect in images:
    #     item = {
    #         "bbox": tuple(rect),
    #         "label": "picture",
    #         "dpi": dpi,
    #         "image": ""
    #     }
    #     image = crop_from_page(page=page, bbox=tuple(rect), dpi=dpi)
    #     item["image"] = image_encode_base64(image=image)
    #     full_pics.append(item)
    return full_figs #, full_pics