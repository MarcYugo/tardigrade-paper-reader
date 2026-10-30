## pdf 本地解析

### 解析模块

解析模块使用pymupdf和三个小模型构成整个解析流程。

对于公式，由于小模型检测行内公式的能力有限，所以本解析模块根据 pymupdf 解析出的公式字符的位置进行聚合得到行内公式的公式框，再根据其与正文字符的位置关系对行内公式框进行修正，得到最终的公式框，实现上述功能的模块称为公式聚合模块。公式聚合模块相较于使用检测模型来说，能够保证不会遗漏行内公式。之后根据公式框裁剪公式图片，使用识别模型转为latex公式代码。

对于图片和表格，使用 OXR 的 D-FINE 版面分析模型（`models/oxr_layout_analysis/`，12 类区域检测）定位表格框，之后使用 SLANet_plus 模型将裁剪的表格图片转化为 html 表格。（原 YOLOv13 检测器已被该版面模型取代。）

```mermaid
flowchart TD
    A["输入 PDF 文件"] --> B["pymupdf 打开文档"]
    B --> M["加载三个小模型<br/>texify（公式识别）/ OXR D-FINE（版面分析）/ SLANet_plus（表格结构）"]
    M --> C["逐页处理 page"]

    C --> F1
    C --> T1

    subgraph FG["公式解析：pymupdf 字符 + 公式聚合 + texify"]
        F1["get_text('dict') 提取字符与文本块"] --> F2["公式聚合模块<br/>筛选公式字符 → 聚合出候选公式框"]
        F2 --> F3["公式聚合模块<br/>结合上下行与正文字符位置<br/>修正行内公式框"]
        F3 --> F4["按公式框裁剪渲染页图片<br/>dpi + padding"]
        F4 --> F5["texify（ONNX int8）识别<br/>输出 LaTeX 代码"]
        F5 --> F6["回填公式 span：LaTeX + 公式截图"]
    end

    subgraph LG["版面分析 + 表格/插图：OXR D-FINE + SLANet_plus"]
        T1["OXR D-FINE 版面分析（12 类）<br/>输出 Text/Title/Formula/Table/Picture/Caption...<br/>像素坐标 → PDF 点坐标 + 去重"] --> T2["按 Table 框裁剪表格图片"]
        T2 --> T3["SLANet_plus 识别表格结构<br/>输出 td 的 colspan/rowspan 骨架"]
        T3 --> T4["PyMuPDF 文字层回填单元格文字<br/>输出 HTML 表格 + 截图 + det_score"]
        T1 --> T5["按 Picture 框裁剪图片"]
        T5 --> T6["输出图片 + base64"]
    end

    F6 --> R["组装 page_dict<br/>blocks / layout / tables / pictures"]
    T4 --> R
    T6 --> R
    R --> D{"还有下一页？"}
    D -- 是 --> C
    D -- 否 --> E["汇总为 doc_parse<br/>写出 JSON 结果"]
```

### 模型下载

```bash
bash download.sh
```

### 依赖安装

1. 安装依赖（`utils/dfine_la` 版面模型需要 `opencv-python-headless`）
   ```bash
   pip install -r requirements.txt
   ```
   > 公式识别已改为 `models/texify-quantized-onnx`（ONNX Runtime + int8 量化权重），
   > 只需要 `onnxruntime` + `tokenizers`；**不要**再安装 `texify` / `transformers`，
   > 也不需要 `models/texify` 的 torch 权重。
2. （可选，已不再需要）旧的 YOLOv13 检测器目录 `yolov13/` 保留用于历史版本，
   当前 pipeline 不再 import 它。