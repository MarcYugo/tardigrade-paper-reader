import re
FORMULA_FONT_RE = re.compile(r'cmmi|cmsy|cmex|msam|msbm|cmr|lmmi|lmsy|lmex|euler|stmary', re.I)

def is_formula_char(font_type: str):
    font_type = font_type.lower()
    if FORMULA_FONT_RE.search(string=font_type):
        return True
    return False

def merge_chars(c1: dict, c2: dict):
    c1["text"] += c2["text"]
    x0, y0 = min(c1["bbox"][0], c2["bbox"][0]), min(c1["bbox"][1], c2["bbox"][1])
    x1, y1 = max(c1["bbox"][2], c2["bbox"][2]), max(c1["bbox"][3], c2["bbox"][3])
    c1["bbox"] = (x0, y0, x1, y1)
    return c1

def merge_unformula_chars_inline(block: dict):
    # 对同行非公式字符进行合并，合并的非公式字符需要size/font一致
    if not block.get("lines", []):
        return block
    for i in range(len(block["lines"])):
        lines = block["lines"][i]["spans"]
        stack = []
        while lines:
            item = lines.pop(0)
            if stack and ((stack[-1]["size"] == item["size"] and stack[-1]["font"] == item["font"]) or \
                        (is_formula_char(stack[-1]["font"]) and is_formula_char(item["font"]))):
                stack[-1] = merge_chars(c1=stack[-1], c2=item)
            else:
                stack.append(item)
        # print(stack)
        block["lines"][i]["spans"] = stack
        # 对block中的span添加标签
    return block

def merge_unformula_chars_to_string(blocks: list):
    # 逐块完成合并
    for i in range(len(blocks)):
        blocks[i] = merge_unformula_chars_inline(block=blocks[i])
    return blocks