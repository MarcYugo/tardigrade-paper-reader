# Targigrade 论文 PDF 转 HTML 阅读器

把论文 PDF 转成可在浏览器里阅读、且**保留原文版式**的网页，并提供**整段翻译（目标语言可切换，默认简体中文）**、**高亮标注**与**笔记**功能，以及论文阅读记录管理、多账号共享与 AI 阅读 Agent。

## 功能

1. **登录验证 + 首次配置**：每个功能页面（首页/阅读页）都要先登录；服务端用中间件把未登录的页面请求 302 到登录页、接口请求返回 401（前端自动跳登录），不是只靠前端藏页面。**首次使用没有任何账号时**，登录页会引导「创建管理员账号」→ 接着引导「设置 LLM 服务（服务地址 / API Key / 模型）」并自带「测试连接」，填完即生效；可点「稍后配置」跳过。配置入口也常驻在右上角「⚙ 设置」（LLM 服务 / 翻译目标语言 / 存储与 OCR / 账号四个页签），**每个账号的 LLM 配置是各自一份**（地址/Key/模型），与论文数据一样隔离；同一个账号还能**保留最多 3 套不同服务的配置**（比如官方 + 中转化 + 本地 Ollama），在设置里一键切换。
   - **多账号 / 切换账号**：登录页本身就有「登录 / 注册新账号」两个页签 —— **不登录也能注册**（默认开启，见 `ALLOW_SIGNUP`）、也能直接换另一个账号登录（登录页的「使用其他账号登录 →」会清空输入框，避开浏览器回填）；右上角头像 →「账号」页签可以查看全部账号（含各自论文数）、新增账号、删除其他账号（可选是否连论文一起删）。**切换账号有两种方式**：点列表里的某个账号（整行可点，登录页会把**账号名自动填好**，只需输密码），或用「切换账号（退出登录）」按钮。**论文数据按账号隔离**，各账号互不可见（详见下方「数据存储」与功能 8）。
   - **注销账号（自助删除）**：同一个「账号」页签底部有「⚠️ 注销当前账号」——需重输**当前密码 + 用户名**并二次确认，之后账号与它名下的**全部论文数据**（版式数据/高亮/笔记/译文/图片）一起永久删除、无法恢复，随后自动退出到登录页。若注销的是最后一个账号，服务会回到「首次使用 · 创建管理员账号」状态。
2. **版式还原**：文字块与图片按原 PDF 坐标/字号/字体/颜色重建；每条文字行还会自动按 PDF 线框适配宽度，双栏/紧排时不会因字体宽度差异而侵入中间空隙造成文字重合。
3. **图片与公式识别有三个可用后端**：`pymupdf`/`auto` = **本地解析方案**（PyMuPDF 文字层 + `local_parse_pipeline` 的 texify 公式识别、OXR（D-FINE）版面分析定位表格/插图、SLANet_plus 表格转 HTML、矢量图/位图提取，前端逐行绝对定位渲染）；`surya` = 整页 OCR（文字 / 公式 `<math>`→LaTeX / 插图裁图全部来自 Surya 2，前端按 Surya 数据独立渲染）；`unlimited_ocr` = 整页 OCR（文字 / 公式 LaTeX / 表格 / 插图裁图全部来自 Unlimited-OCR，前端按它自己的数据独立渲染）。
4. **翻译（非实时，目标语言可切换）**：
   - **目标语言跟账号走**：「⚙ 设置 → LLM 服务 → 翻译目标语言」可选 12 种语言（简繁中文、英、日、韩、法、德、西、葡、俄、阿、印地），存在 `users.prefs.target_lang`；译文缓存按「语言 + 文本」分开存（`translate.cache_key()`），换语言不会命中旧语言的译文，切回来仍在；换语言后阅读页会自动清掉旧语言的译文重新拉取。
   - 点击任意句子 → “译本段”，在**该段之后**显示整段目标语言译文，并自动把后续版式内容顶开、不遮挡；
   - **多选句子时只翻译所选句**：拖选多句后点 `翻译所选句子`，只把**选中的那几个句子**送去翻译，结果显示在一张带 `所选句译文` 标题的卡片里（虚线黄框，可点 `清除`）；选区跨段落时按段落切成多段分别翻译再合并展示，再点一次同一选区即可收起；
   - **两种译文互斥**：显示“整段译文”时不显示“所选句译文”（进入整段模式会自动收掉后者）；反之翻译所选句时也会收起所有整段译文卡（译文都在缓存里，可随时再开）。但**多张“所选句译文”可以同时留着**，分别对应不同选区；
   - 译文卡**就在论文平面里**：字号用同一个 `--page-scale` 跟着页面渲染比例一起缩放（顶部缩放 / 自动适宽都会同步），所以放大缩小时译文与原文观感一致；
   - 窗口尺寸 / 缩放 / 批注栏开关引起的重排**不会收起已展开的译文卡与所选句译文**，选中句子也会保留；
   - 支持“翻译本页 / 翻译全文”（批量、可取消、结果缓存，避免重复计费）；
   - “显示译文”总开关可一次展开/收起全部已知译文；译文只在需要时显示。
5. **句子级标注与笔记**：
   - 点击/选中一句话即可对其**整句高亮**：调色盘共 **6 色** —— 3 个默认色（黄/绿/粉）+ **3 个自定义色**（点调色盘下方色块用取色器自由设定）；自定义色**随账号存在数据库里**（`users.prefs`，与账号密码同一份记录），换账号会各用各的，不会串；同色再点或“清除高亮”可取消；
   - **支持多句选择**：在正文上**按住左键拖拽**（或 Shift+点击扩展）即可一次选中连续多句，跨段落/跨页都行；工具条会显示“已选 N 句”，此时：`标注` 一次给所有选中句上色（全部同色时再点同色 = 整段取消）、`复制` 复制整段文字、`翻译所选句子` **只翻译选中的句子**（不整段翻译，详见上方「翻译」）、`笔记` 挂在所选第 1 句上；
   - 笔记细化到**句子**，卡片默认贴在**页面右侧**、与对应句子**同一高度**（不占版面、不把正文顶下去），一眼就能左右对照；卡片内可随时编辑/删除（`Ctrl+Enter` 保存、`Esc` 取消）；
   - 同一页多条笔记会自动上下避让、互不遮挡；有笔记时会**给页面右侧预留批注栏**：窗口够宽则页面保持原尺寸整体左移，窗口不够宽则页面等比缩到适配宽度（顶部比例标签显示的是**实际**渲染比例），保证总能并排；只有极窄窗口（可用宽 < 520px）或打开了“全部笔记”抽屉时才回到句子下方内嵌；
   - 卡片字号跟随页面渲染比例缩放（`--page-scale`，夹在 0.78~1.2 之间），页面缩小时笔记字也跟着变小，观感与正文一致（译文卡同一套比例）；
   - 正文中该句同时带下划线标记；顶部“显示笔记”总开关可临时隐藏全部笔记卡片；
   - 顶部“📝 笔记”打开右侧笔记栏：句子笔记可一览/定位，另可创建、编辑、删除不绑定正文的独立笔记；两种笔记均可被 AI 引用。
6. **阅读记录**：首页管理全部论文——上传、进度、页数、标注/笔记数、未读/在读/读完状态（可按状态筛选）；进度自动保存（含 `last_pos`：第几页 + 页内比例），再次打开自动回到上次的**精确**位置。阅读页顶栏的**论文名称可以直接点开重命名**（Enter 保存 / Esc 取消 / 点别处即保存），改完首页列表同步显示新名字（名字存在阅读记录与版式文档两处，与解析数据无关；只读共享的论文不能改）。
7. **数据存 MongoDB**：论文元数据/版式数据/**插图**/高亮/笔记/译文缓存，以及**账号、登录会话、LLM 配置**都落在 MongoDB（集合划分见下方「数据存储」）；连接参数直接读 `mongo_configuration/.env`，不用再配一遍。连不上数据库时自动回退本地 JSON 并在页面提示，点「重试连接」可热切回（不用重启）。插图存在 GridFS（`images.files`/`images.chunks`）：服务重启、重建容器、换机器都不会再丢图，`mongodump` 也能连图一起备走。
8. **论文数据按账号隔离**：每篇论文记一个 `owner`（上传者），列表/打开/翻译/标注/删除都按当前账号过滤 —— 新账号进去就是**空列表**，只能看到并操作自己上传的论文（别人的论文一律 404，不泄露存在性）。删账号时可选“连它名下的论文一起删”。想看别人的，让对方在首页点「🔗 共享」（见功能 9）。

9. **多用户共享（笔记共享）**：用户 A 可以把**自己的某一篇论文**（连同它的**笔记、高亮、译文、AI 整理与对话**）共享给用户 B，并决定 B 的权限：
   - **只读**：B 能打开这篇论文、看 A 的笔记/高亮/译文/AI 整理与对话，也能**自己翻译**（译文是共享缓存，顺手也给 A 补上）、记**自己的**阅读进度；不能改笔记/高亮，也不能生成或编辑 AI 整理、不能发消息；
   - **可写**：上面那些写操作 B 都能做（相当于**共同阅读同一份笔记**：笔记、高亮、目录、AI 整理与对话都是同一个副本，谁改都生效）。
   - **入口**：首页每张自己的卡片上有「🔗 共享」→ 弹窗里选账号 + 选权限（只读 / 可写）→ 添加。账号下拉是**卡片式**的（头像 + 账号名 + 该账号论文数，点空白处收起），权限是**整行分段按钮**（只读 / 可写，下面跟一句说明），已共享名单里也能**就地改权限**（小号分段按钮）或取消共享；阅读器顶栏也有「🔗 共享」（只有所有者看得到），顶栏会用徽标显示「已共享给 N 人」/「来自 alice · 只读共享」。
   - **B 看到的**：首页多出一块「**共享给我的**」（虚线卡片、标注来自谁 + 权限），点「阅读」即可打开；B 可以点卡片上的「退出共享」**自己退出**（只会移出共享名单，论文本身不动）。
   - **进度互不干扰**：共享论文里每个人记**自己的**阅读进度/读完状态（存在记录的 `reading.<用户>`，所有者仍用顶层字段），B 读到哪一页不会把 A 的进度顶掉。
   - **笔记标作者**：论文一旦共享给别人，每条笔记都会带一个**作者小标签**（`alice` / `bob`），正文内嵌卡片与右侧「全部笔记」里都有；自己写的那条高亮成蓝色（浅色底 + 主题色字），别人的是灰色，扫一眼就能分出哪些是自己写的。笔记存 `by`（写它的账号），**谁最后改就是谁的**（同一句只有一条笔记，B 改写 A 的那条后标 B）；**加这个标识之前的老笔记**算论文所有者的；**没共享给别人时不下发作者、界面上也没有标签**（那种情况只有自己能看）。高亮不标（它没有文字内容，标了反而吵）。
   - 权限在**服务端**层层校验（不只是前端藏按钮）：只读账号对任何写接口都会拿到 403；没有权限的论文一律 404（不泄露存在性）；**删除论文**与**管理共享名单**只有所有者能做。

10. **AI 阅读 Agent（阅读悬浮卡片）**：阅读页提供随视口固定显示的 AI 对话与笔记整理面板，不必滚到论文末尾即可使用。AI 对话与笔记整理包在同一个卡片中，可整体折叠/展开，状态按论文保存在当前浏览器；**展开时宽度为窗口宽的 95%**（在避开左右抽屉后的可用区间里居中，底边贴在页码指示器上方），**收起时缩成一行标题贴在页码指示器左侧**。从目录侧栏的「打开 AI 阅读与笔记 →」入口打开时会自动展开。Agent 带着全文、公式、句子笔记和独立笔记回答（独立笔记也会被拼进上下文，`page` 记 0），并有两个**受限工具**：
    - `add_note_to_summary`：把用户要求的内容**写进右侧「笔记整理」卡片**（按 section 追加到总览 / 主题梳理 / 关联 / 待想清楚 / 下一步；`themes` 可带 `title`，同名主题追加要点），**不新建正文旁的独立笔记**；卡片还没生成时也能先按分区建起来。
    - `navigate_to_content`：定位到论文页（可给 `page`，或给 `query` 由后端按标题/正文/公式 LaTeX 找页）、指定笔记，或直接打开右侧笔记栏；定位论文时还会匹配相关文本块，跳转后对应文本框短暂亮起。论文页/笔记导航会**先返回确认目标**，前端必须在用户明确同意后以 `pending_navigation` + `navigation_answer:"yes"` 续问；拒绝传 `"no"`，目标不明则继续询问。
    写入/导航前会验证参数与文档权限（只读共享拿不到写权限，导航也只能执行刚确认过的同一目标）。读完（进度 **≥ 95%**）会自动整理笔记（总览 / 主题 / 关联 / 待想清楚 / 下一步），也可手动重新整理；整理、对话都会持久化，笔记更新后会提示摘要过期。需要 OpenAI 兼容的 LLM（Google 免费兜底做不了工具调用）。左边对话、右边整理结果的布局、固定高度与框内滚动、流式对话、Markdown/KaTeX 渲染及可编辑整理结果等原有交互保持不变。

    - **读完论文时展示 AI 面板**：对没有精确阅读位置的已读完论文，打开阅读页会展开悬浮 AI 面板；阅读位置仍按上次保存的正文页恢复。

11. **重排不跳页**：缩放 / 窗口变化 / 目录栏与笔记栏开关都会改变页面尺寸，进而**整页重建**；重建后按「**第几页 + 页内比例**」还原滚动位置，所以目录跳转之后再缩放、拉窗口，都不会被无缘无故弹回「上次读到的那一页」。

12. **暗夜模式**：右上角（登录页在右上角角落）点 `🌙` / `☀️` 一键切换。
    - **背景走枪灰**（`#22262d`，卡片 `#2b3038`、页面 `#262b32`），**文字反成近白**；**论文正文里的字色是 PDF 写死的**，所以按「保留色相、反转明度」在 JS 里换算（黑字→近白、彩色标题→同色系提亮），插图保持原样只压一点亮度。
    - **高亮改成荧光**：暗色下不用半透明色块，而是「很淡的同色底 + 高亮度霓虹字 + 外发光（`text-shadow`）」，调色盘里的色块也跟着发光——所以暗色下高亮看着像荧光笔。日间仍是原来的半透明底色。
    - 主题**两级存储**：`localStorage` 让首屏由 `<head>` 里一行小脚本先套上 class（不会闪一下白），登录后以**账号偏好**（`/api/prefs` 的 `theme`）为准、跟着账号走。整套配色靠 CSS 变量（`:root` 与 `html.dark` 各一份语义色板），组件里不再写死浅色。

13. **引用跳转（正文引用 ↔ 参考文献）**：正文里的 `[12]` / `[3,5]` / `[10–12]` 以及作者-年份式引用（`[Smith et al., 2020]` 方括号写法与 `(Smith et al., 2020)` 圆括号写法）会变成**可点击的小标记**，点一下**就地弹卡片**显示对应参考文献条目（条目原文 + `定位原文 ↧`（滚到参考文献那一条并闪烁）+ `复制` + 条目带 DOI/arXiv 链接时 `打开链接`），不用再往后翻。数字令牌会展开成编号能对上的全部条目。引用解析按后端分发：PyMuPDF 与 Unlimited-OCR 使用通用内部链接/数字令牌与作者-年份令牌回退逻辑；Surya 独立处理 OCR 块，并可从同一块拆分多条参考文献（编号制、`[作者, 年份]` 标签的作者-年份制，以及**无标签**（行首直接是作者名）的作者-年份制都拆）；数字与作者-年份引用扫描都不依赖 PDF 是否带内部跳转。所有映射均由后端纯本地计算（零 LLM 成本），前端 `r2-refs.js` 按映射打标记、弹卡片。
    ```mermaid
    flowchart LR
    A[正文引用候选<br>正则扫描] --> C{关联层}
    B[参考文献条目<br>定位+切条] --> C
    C -->|编号/作者-年份 直接映射| D[映射表]
    C -->|PDF 内部链接 权威| D
    C -->|作者-年份 回退匹配| D
    D --> E[GET /api/doc/:id/refs<br>纯本地现算、不缓存]
    E --> F[前端 .cite 点击<br>浮层卡片 / 跳到条目]
    ```

14. **阅读页逐页加载**：进入阅读页只先取「看得见的那几页」（`FIRST_PAGES=3`），其余页在后台按「离当前阅读位置最近优先」一批批补齐（`PAGE_CHUNK=3`，用 `requestIdleCallback` 让出主线程）。后端 `GET /api/doc/{id}?page_from=&page_to=` 只下发这一段页，另给 `page_total` / `page_from` / `page_to` / `page_sizes`（给未到的页留占位宽高）/ `needs_math`（**全文**口径，避免后补的页里公式显示成 LaTeX 源码）。页码、目录、目录跳转与滚动位置从第一帧起就是整篇的；目录跳页、笔记定位、AI 点亮文本框会按需先把对应页拉下来（`ensurePage` / `ensureBlockLoaded`），「翻译全文」会先把整篇补齐。不带切片参数时接口行为与以前完全一致（整篇下发）。

15. **左侧目录栏**：建论文时就把 **PDF 自带书签**存进记录（`toc` / `toc_source="pdf"`，零成本、最准）；PDF 没书签时，首次展开目录会自动让 **LLM 读全文排一份目录**（每页只喂「候选标题行」，省 token，后端 `POST /api/doc/{id}/toc`，需写权限），结果存库、不重复花 token，底部有「重新提取」。点目录项直接跳页，滚动时自动高亮当前小节；目录与笔记栏打开时正文整体右移并自动适宽，不会被盖住。

16. **笔记导出**：分为agent模式和机械导出模式，二者的相同功能点是将文章分为title/abstract/main text/references/appendix，但是agent模式可以通过agent调用工具搜索和检验references文献的有效性、提取项目代码地址，形成报告。agent数据对接接口，每个账号通过设置transfer key的方式使得其他agent，可以通过持有这个key来访问自己账户的笔记。
    - **已实现（本阶段）**：`backend/note_out.py` 提供机械导出与 Agent 导出的统一 JSON 结构（论文五段分节 + 句子/独立笔记 + 高亮 + 分析报告）；`backend/agents/note_out_agent.py` 是 Agent 部分（只读工具：核验参考文献、提取并确认代码仓库地址，再生成报告）。网页走 `POST /api/doc/{id}/note-out`（`mode=mechanical|agent`，`?download=1` 直接下载 JSON）。
    - **待做**：`transfer key` 数据对接（其他 Agent 持 key 访问账号笔记）。

## 后续计划（尚未实现）

- **参考文献真实性校验**：在阅读 Agent 里增加「联网搜索 / 学术库检索」工具，按 DOI / arXiv / 标题检索并标记参考文献是否真实存在（当前引用跳转是**纯本地**解析，不联网）。
- **笔记导出**：把笔记/高亮导出为 JSON（带页码、句子原文、作者）或图片（当前只能逐条查看/复制）。
- **AI 面板弹窗化**：把 AI 辅助阅读与笔记栏改为弹窗式 Agent，并支持把任意笔记块作为上下文继续检索（当前是视口底部悬浮卡片 + 「笔记整理」卡片写入）。

## 项目结构

```
backend/           后端(FastAPI)
  auth.py          模块四 · 身份认证(账号 + 登录会话，口令 PBKDF2 加盐哈希)
  db.py            模块六 · MongoDB 连接层(连接参数读 mongo_configuration/.env 或环境变量)
  mongo_store.py   模块七 · MongoDB 存储实现(集合/索引/各存储后端)
  storage.py       存储装配(按 APP_STORAGE 选 MongoDB 或本地 JSON 回退)
  settings.py      模块五 · 运行时设置(LLM 服务地址/Key/模型；每账号最多 3 套可切换；译文目标语言)
  pdf_parser/      模块一 · 文档解析(PDF → 版式布局数据；文字版式 = PyMuPDF / Surya 2 / Unlimited-OCR)
    __init__.py    门面：设计说明 + 对外接口(parse_pdf / status) + 内部名字再导出
    options.py     解析配置 / OCR 服务探测 / status()
    text_layer.py  文字层抽取(字体族、行框、行内样式)
    sentences.py   按句子重切块（一个块 = 恰好一句）
    images.py      插图检测区域去重影 / 挖洞
    backend/       三条版式后端（同一份 parse_pdf 契约，按 backend 取值分发）
      backend_pymupdf.py      版式后端 · 本地解析方案（PyMuPDF + local_parse_pipeline）
      backend_surya.py        版式后端 · Surya 2 整页 OCR（块级数据，前端独立渲染）
      backend_unlimited_ocr.py  版式后端 · Unlimited-OCR 整页 OCR（块级数据，前端独立渲染，与 Surya 各自维护）
    surya_parser.py  Surya 2 推理服务客户端（整页 OCR / 插图裁剪 / 块 HTML→文本·runs·LaTeX）
    unlimited_ocr.py  Unlimited-OCR 推理服务客户端（整页 OCR / 插图裁剪 / 版式项解析，只用标准库+PyMuPDF）
    unlimited_ocr_online.py  Unlimited-OCR **在线解析**客户端（百度智能云异步接口；本地服务不可用时自动启用）
    math_text.py / math_font.py   公式：文本工具 / 字体判据与字形→空格
    links.py       链接注释采集（内部跳转/URI；各后统一，引用跳转的数据源）
    entry.py       入口 parse_pdf(+ 公式后处理挂载点)
  converter.py     模块二 · 文档转换(解析结果落盘为论文文档+图片)
  records.py       模块三 · 论文阅读记录(本地 JSON 版，MongoDB 不可用时回退)
  translate.py     译文服务(翻译提示词 + 批量 + Google 兜底；对话/流式转发给 agents/llm.py)
  ai_read.py       模块八 · AI 辅助阅读(把笔记整理成主题/关联/疑问 + 带整篇正文(含每页公式 LaTeX)的对话，支持流式)
  agents/llm.py    LLM 接入层(LlmClient：OpenAI 兼容 chat/completions、流式与 tool_calls 解析、测试连接)
  agents/ai_read_agent.py  AI 阅读 Agent(受限工具调用：写整理卡片 / 定位论文页·笔记；工具循环 + 事件收敛)
  note_out.py      模块十 · 笔记导出(机械导出 / Agent 导出；论文分节 + 笔记/高亮 + 报告，统一 JSON)
  agents/note_out_agent.py  笔记导出 Agent(核验参考文献真实性 / 提取代码仓库地址 / 生成报告；只读、离线失败不中断)
  pdf_refs/refs.py         模块九 · 引用跳转**线上实现**(按 parser 分发；`app.py` 从这里取 build_refs)
  pdf_refs/refs_pymupdf.py / pdf_refs/refs_unlimited_ocr.py  PyMuPDF / Unlimited-OCR 通用引用解析策略
  pdf_refs/refs_surya.py    Surya 引用解析策略（在 OCR 块内拆分多条参考文献）
  refs.py          早期副本（线上不再引用；逻辑与 `pdf_refs/refs.py` 同源，改它不生效）
  app.py           HTTP 接口 + 登录门禁中间件 + 前端静态托管
frontend/          前端(原生 HTML/CSS/JS)
  login.html       登录页 / 首次使用向导(创建账号 → 配置 LLM)
  index.html       上传 + 论文阅读记录首页
  reader.html      阅读器(版式还原 + 翻译/标注/笔记/目录/AI Agent)
  css/style.css    样式（CSS 变量双主题色板 + 各模块样式）
  js/
    common.js      工具函数 / 登录态 / 顶栏用户区 / LLM 设置与共享弹窗
    login.js       登录页 / 首次使用向导
    home.js        首页：上传 + 阅读记录列表 + 筛选 + 共享入口
    reader2.js     阅读器**装配入口**（把下面各模块拼起来，保持原有全局状态）
    core/          无状态工具模块（装配一次即可）
      r2-text.js   句子切分 / 字符区间 / 可复制文本 / 句子 key（与后端切句口径一致）
      r2-color.js  HSL 换算 / 暗夜反色 / hex→rgba
      r2-fit.js    本地解析方案的逐行横向压缩（只缩不放）
      r2-view.js   缩放 / 视口坐标换算 / 滚动位置还原
    items/         三种解析后端的块渲染（各自一套，互不牵连）
      item-local.js      PyMuPDF 逐行绝对定位 + 行内/公式框
      item-surya.js      Surya 2 块级流式排版
      item-unlimited.js  Unlimited-OCR 块级流式排版（与 Surya 独立）
    select/        选中与工具条
      selection.js  点/拖拽取句、公式框句子锚定、选中态重绘
      bar.js        高亮调色盘 + 浮动工具条 + “所选句译文”卡
      translate.js  段落 / 本页 / 全文翻译与译文卡
    layout.js      渲染后的摆位：页面宽度与批注栏、卡片顶开版式、右侧笔记卡避让、AI 面板堆叠
    r2-nav.js      页码导航 / 目录(TOC) / 阅读进度 / 缩放
    r2-math.js     公式渲染（KaTeX / 公式框 / 复制为 LaTeX）
    r2-md.js       Markdown 渲染（AI 面板用，自带实现无构建）
    r2-notes.js    句子笔记与独立笔记、右侧笔记栏
    r2-ai.js       AI 阅读与笔记整理面板（流式对话 + 工具状态）
    r2-refs.js     引用跳转：正文引用标记 + 参考文献卡片
  checks/          自检页（_refs_check / _surya_check / _unlimited_check）
  logo/ vendor/katex/   图标 / 内置 KaTeX（离线可用）
run.py             启动入口
config.example.json  翻译服务 + PDF 解析配置示例
mongo_configuration/  MongoDB 的 docker compose + 连接参数(.env) + 使用说明
data/              运行时数据（连上 MongoDB 后基本只是个上传临时目录）
  tmp/               上传的 PDF 临时文件（转完即删）
  docs/<id>/images/  插图——**仅“未连上 MongoDB 回退本地 JSON”时**才落这里
  *.json             同上，仅回退模式用到
tools/             本地小工具(可自行运行验证)
  make_sample_pdf.py   生成样例论文 PDF(samples/sample.pdf)
  migrate_to_mongo.py  把 data/ 下的本地 JSON 数据 + 插图搬进 MongoDB
  mongo_check.py       MongoDB 连接体检(参数从哪读/能否连上/怎么修)
  relink_images.py     论文插图丢了？用原 PDF 重新解析并挂回同一个 doc_id
  reparse_layout.py    解析器升级后，用原 PDF 原地重解析版式（笔记/译文保留）
  refresh_font_size.py 只用原 PDF 文字层刷新字号/行距（不重新 OCR，适合 Unlimited-OCR 文档）
  split_ref_blocks.py  解析器升级后，给已入库论文补跑“参考文献分块”（不重新 OCR/上传）
  selftest.py          不启动服务，直接跑“模块一·文档解析”自检
tests/             单元/集成测试（python -m unittest discover tests）
  test_refs.py             引用解析：编号/作者-年份/标题并块/链接+回退合并
  test_ai_agent_notes.py   AI Agent 工具：写整理卡片 / 导航确认
  test_backend_local_convert.py / test_merge_formula_boxes.py / test_merge_line_runs_color.py
  test_backend_surya_font.py / test_parser_account_options.py / test_table_cell_text.py / test_vertical_text.py
```

## 快速开始

```bash
# 0. 启动 MongoDB（推荐；没起也能跑，会自动回退本地 JSON）
cd mongo_configuration && cp .env.example .env && docker compose up -d && cd ..
#    连接参数就在这份 .env 里，后端会自动读取（细节见 mongo_configuration/README.md）

# 1. 安装依赖(建议虚拟环境)
python -m venv .venv
.venv/Scripts/activate          # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt

# 2.(可选) 先在配置文件里配翻译服务
cp config.example.json config.json      # Windows: copy config.example.json config.json
#    在 config.json 填 deepseek_api_key；或安装 deep-translator 走免费 Google 兜底
#    也可以只设环境变量 DEEPSEEK_API_KEY —— 不配也行，首次登录时在网页里填

# 3. 启动
python run.py
```

打开浏览器访问 <http://127.0.0.1:8000>：

1. 还没账号 → 直接跳到登录页，看到「首次使用 · 创建管理员账号」，填用户名/密码创建；
2. 接着是第二步「配置 LLM 服务」：填服务地址、API Key、模型（可点「测试连接」当场验证），
   保存后立即生效；也可以点「稍后配置，先进入」先跳过；
3. 之后每次进首页/阅读页都先登录；登录成功才进得去。

> 已有 `config.json` 或环境变量时，这两步会**预填当前生效值**，不填也能直接进（沿用原配置）。
> 配置入口也常驻在页面右上角：`⚙ 设置`（LLM 服务 / 翻译目标语言 / 存储与 OCR / 账号）、`退出`。

## 登录与 LLM 服务配置

### 账号与会话

账号与会话默认存在 MongoDB 的 `users` / `sessions` 集合（没连上数据库时回退 `data/users.json`、`data/sessions.json`）：

- 口令用 **PBKDF2-SHA256 + 随机盐 + 20 万轮** 哈希后保存，**不存明文**；校验用常量时间比较。
- 登录成功后签发随机 token，写进 **HttpOnly + SameSite=Lax** 的 Cookie（`ptsid`，默认 14 天）—— 重启服务不用重新登录；Mongo 里的会话还带 TTL 索引，过期自动清理。
- 登录失败限流：同一「用户名 + IP」连续失败 8 次，锁定 5 分钟（计数在内存，重启即清）。
- 服务端有 `AuthGate` 中间件兜底：未登录时**页面请求 302 到 `login.html`**（带 `?next=` 回跳），**接口请求返回 401**。所以直接敲 `reader.html` 也进不去，不是只靠前端藏按钮。
- 忘了密码：把 `users` 集合（或回退模式下的 `data/users.json`）里对应账号删掉，重启后就回到“首次使用”流程重新创建账号（论文数据不受影响）。改密码接口为 `POST /api/auth/password`（需登录，参数 `old_password`/`new_password`）。

### LLM 服务配置的优先级（**按账号**）

每个账号一套 LLM 配置，存在该账号自己的记录里（MongoDB `users.prefs.llm`，与账号密码同一份文档；JSON 回退模式则是 `data/users.json` 里那份）。**逐字段**按下面的顺序取值（高 → 低）：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | **本账号设置** | 右上角「⚙ 设置 → LLM 服务」保存的值；只对该账号生效，别的账号看不到也用不到 |
| 2 | 环境变量 | `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` / `TRANSLATOR_BACKEND` |
| 3 | `config.json` | **安装级默认**（兼容旧配置，所有账号共享的后备） |
| 4 | 内置默认 | DeepSeek 官方地址 + `deepseek-chat` |

> 举例：A 账号存了自己的 Key，B 账号没存 —— B 用的是环境变量/`config.json` 的安装级默认，**绝不会用到 A 的 Key**。
> 设置面板会直接写明“属于账号 xxx”和“Key 来源：本账号配置 / config.json（安装级默认）”，一眼能看出当前用的是谁的。
> 升级自旧版（那时 LLM 配置是全局一份）时，那份配置会**自动归给最早创建的账号**（启动日志会写 “已把旧版全局 LLM 配置归给账号 X”），其它账号重新填即可。

服务类型可选 `DeepSeek` / `OpenAI` / `OpenAI 兼容服务(自建/中转)` / `Google 免费兜底(无需 Key)`；
只要不是“免费兜底”，都会按 **OpenAI 兼容的 `POST {base_url}/chat/completions`** 调用，因此 vLLM、Ollama、各类中转都只需改地址与模型名。
API Key 只存在服务端，接口返回一律是打码串（`sk-4****8cca`）。

同一个「⚙ 设置」弹窗里还有两个**跟账号走**的选项：

- **翻译目标语言**（12 种，存 `users.prefs.target_lang`，译文缓存按语言分开）；
- **PDF 解析后端**（`auto` / `pymupdf` / `surya` / `unlimited_ocr`，存 `users.prefs.parser.backend`，环境变量 `PDF_PARSER_BACKEND` 仍优先）；
- 以及「存储 / OCR」页签里的连接状态、重试连接与各 OCR 服务就绪情况。

#### 一个账号可以保留多套 LLM 配置（最多 3 套，可随时切换）

「⚙ 设置 → LLM 服务」顶部的下拉框就是多套配置的入口：

- 每套配置 =「服务类型 / 服务地址 / API Key / 模型」+ 一个可选的名字（如“官方”“中转化”“本地 Ollama”）；
- **保存** 会写进当前选中的那套，并把它设为**当前生效**；点 **＋ 新建** 再填一套（最多 3 套，够了就先删一套）；
- **切换使用** 一键把选中的那套设为当前生效（不改写内容），下拉里标有「（使用中）」的即是正在用的；
- **删除** 只删这一套；删的若是正在用的那套，会自动切到剩下的一套；**3 套都删光**就回落到环境变量 / `config.json` 的安装级默认；
- 「测试连接」会按**所选那套**已保存的 Key/地址/模型试一次（页面只显示打码串，不填也能测）。

存储上就是一个账号记录里的 `prefs.llm = {profiles: [...], active_id, provider, base_url, model, api_key}`：
`profiles` 是保存下来的若干套，`active_id` 标出哪套在用，顶层那几个字段始终是**当前生效那套的镜像** ——
所以老配置（只有顶层一份、没有 `profiles`）会被自动当成一个名为“默认”的配置槽，不用做数据迁移。

### 认证与设置相关接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/auth/status` | 是否已初始化 / 是否已登录 / 是否已配 LLM（登录页先问这个） |
| POST | `/api/auth/setup` | 首次创建管理员账号（已初始化则 409） |
| POST | `/api/auth/register` | **未登录也能调**：自助注册并直接登录（`ALLOW_SIGNUP=0` 时 403） |
| POST | `/api/auth/login` | 登录，成功写入会话 Cookie |
| POST | `/api/auth/logout` | 退出登录 |
| POST | `/api/auth/password` | 修改密码 |
| GET | `/api/prefs` | 当前账号的偏好（`hl_colors` 自定义高亮色、`show_zh` 显示译文开关、`theme` 明/暗主题） |
| POST | `/api/prefs` | 保存当前账号的偏好（**随账号存库**，换账号互不影响） |
| POST | `/api/auth/deactivate` | **注销当前账号**：需 `{password, confirm: 用户名}`，账号与其名下论文数据一并永久删除（不可恢复） |
| GET | `/api/settings/llm` | 读**当前账号**的 LLM 配置（Key 打码；附 `owner`、字段来源 `sources`、已保存的配置槽 `profiles` 与 `active_id`、翻译目标语言 `target_lang` / `target_langs`） |
| POST | `/api/settings/llm` | 保存**当前账号**的一套 LLM 配置（存进 `users.prefs.llm`；`api_key` 不传=保留，传空串=清空；`profile_id` 不传/`"new"`=新建一套、上限 3 套，保存即切换；`target_lang` 同时保存该账号的译文目标语言） |
| POST | `/api/settings/llm/activate` | 切换到已保存的另一套配置（`{profile_id}`） |
| POST | `/api/settings/llm/delete` | 删除一套已保存的配置（`{profile_id}`；删的是当前生效那套会自动切到剩下的一套） |
| POST | `/api/settings/llm/test` | 用一次极小请求验证 地址/Key/模型 是否可用（表单值优先，没填 Key 时用所选配置已保存的那把） |
| GET | `/api/settings/parser` | 当前 PDF 解析(OCR)后端状态（含真探测；结果缓存 15s） |
| POST | `/api/settings/parser` | 切换**当前账号**的解析后端（`auto` / `pymupdf` / `surya` / `unlimited_ocr`），存 `users.prefs.parser.backend` |
| GET | `/api/config` | 当前账号的翻译/解析/存储状态（翻译目标语言、解析后端就绪情况、存储后端） |
| GET | `/api/docs` | 当前账号能看到的论文（自己的 + 共享给我的），带权限/共享数/我的阅读进度 |
| POST | `/api/convert` | 上传 PDF 并解析转换（multipart） |
| POST | `/api/doc/{id}/reparse` | 用原 PDF 原地重解析（只换 pages / parser / parser_info / 插图） |
| GET | `/api/storage/status` | 当前存储后端/连接状态/论文数 |
| POST | `/api/storage/reconnect` | 重新探测 MongoDB 并热切换存储后端（前端「重试连接」按钮） |
| GET | `/api/auth/users` | 账号列表（需登录；不返回口令哈希） |
| POST | `/api/auth/users` | 新增账号（需登录，用于“先建好第二个账号再切换”） |
| DELETE | `/api/auth/users/{name}?purge=1` | 删除账号（需登录；不能删自己，也不能删最后一个）。`purge=1`（默认）连它名下的论文数据一起删，`purge=0` 只删账号 |
| GET | `/api/doc/{id}` | 版式文档。带 `page_from` / `page_to` 时只下发这一段页（逐页加载），另给 `page_total` / `page_from` / `page_to` / `page_sizes` / `needs_math`；不带参数 = 整篇 |
| GET | `/api/doc/{id}/img/{fname}` | 取插图（优先 GridFS，其次磁盘旧文件） |
| DELETE | `/api/doc/{id}` | 删除论文及它名下的标注/笔记/译文/插图（仅所有者） |
| GET | `/api/doc/{id}/progress` | 当前账号可写的阅读进度（含 `last_pos` 精确位置） |
| POST | `/api/doc/{id}/progress` | 保存阅读进度（`last_page` / `progress` / `status` / `last_pos`；共享论文存 `reading.<用户>`） |
| POST | `/api/doc/{id}/title` | 给论文**改名**（`{title}`，≥「可写」权限）：同时写回阅读记录与版式文档的 `title`，首页列表随之更新；只动名字，不碰解析数据，笔记/译文/进度都不受影响 |
| GET | `/api/doc/{id}/toc` | 论文目录（转换时存的 PDF 书签，或之前用 LLM 提取的结果） |
| POST | `/api/doc/{id}/toc` | 用 LLM 从全文提取目录（`{force:true}` 重新提取）；结果存记录、需要写权限 |
| GET | `/api/doc/{id}/seltrans` | 该论文的「所选句译文」卡片列表 |
| POST | `/api/doc/{id}/seltrans` | 保存一张卡片（同 `key` 覆盖） |
| DELETE | `/api/doc/{id}/seltrans?key=…` | 删一张卡片；不传 `key` 则清空该论文的全部卡片 |
| GET | `/api/doc/{id}/anno` | 获取高亮、正文句子笔记和独立笔记 |
| POST | `/api/doc/{id}/anno` | 更新标注/笔记；独立笔记使用 `add_free_note`、`update_free_note`、`remove_free_note` 动作 |
| GET | `/api/doc/{id}/refs` | 引用跳转映射：`entries`（参考文献条目）+ `links`（正文引用标记 →「块 id + 字符区间 + 条目下标」）。纯本地现算、不缓存；`scan:"no-links-data"` 表示旧解析数据缺少 PDF 链接注释（数字式与作者-年份式引用仍会回退匹配） |
| GET | `/api/doc/{id}/zh_all` | 本篇全部已缓存译文（按**当前账号的译文目标语言**过滤） |
| POST | `/api/translate` | 批量翻译（按「目标语言 + 文本」缓存，`blocks[]` 里用前端同口径的复制文本作键） |
| GET | `/api/doc/{id}/ai` | AI 阅读面板状态：已生成的笔记整理、对话记录、`stale`（笔记是否又改过）、`trigger_progress` |
| POST | `/api/doc/{id}/ai/summary` | 把笔记整理成结构化摘要并保存（`{notes:[{sent,note,page}], force?}`；笔记没改且已有整理时直接返回缓存，不重复花 token。**重新整理时会带上上一版整理（含你手改过的内容）+ 已发生的对话**当素材） |
| POST | `/api/doc/{id}/ai/summary/save` | 保存**手工编辑**后的整理（`{summary:{…}}`，整份覆盖；用宽松上限清洗，不会把写长的句子截掉） |
| POST | `/api/doc/{id}/ai/chat` | Agent 对话（`{message, notes?}`；`notes` 不传就用生成整理时存下来的那份）。上下文含**整篇正文 + 每页公式 LaTeX**、句子笔记、独立笔记与整理；工具是 `add_note_to_summary`（写进右侧整理卡片）与 `navigate_to_content`（定位论文页/笔记/笔记栏）。论文页/笔记导航会先返回确认目标，前端须在用户明确同意后以 `pending_navigation` + `navigation_answer:"yes"` 续问，拒绝则传 `"no"`；论文定位确认事件可能包含匹配的 `block_id`，跳转后用于短暂点亮对应文本框。**流式（SSE）返回**正文、工具状态与导航/整理更新事件，最后 `{"type":"done","chat":[…]}`；出错则是 `{"type":"error","message":"…"}` |
| POST | `/api/doc/{id}/share` | 新增/修改一条共享：`{user, perm}`，`perm` 是 `read`（只读）/ `write`（可写）；只能由**所有者**操作 |
| GET | `/api/doc/{id}/share` | 这篇的共享名单（所有有权限的人都能看） |
| DELETE | `/api/doc/{id}/share/{user}` | 取消某条共享（只能由所有者操作；顺手清掉那个人的阅读状态） |
| POST | `/api/doc/{id}/share/leave` | 被共享的人**自己退出**（不需要所有者权限） |
| POST | `/api/doc/{id}/ai/chat/reset` | 清空这篇的对话（整理结果保留） |
| POST | `/api/doc/{id}/note-out` | 笔记导出（JSON）。`{mode:"mechanical"|"agent", instructions?}`：机械导出纯本地；Agent 导出会核验参考文献、提取代码地址并生成报告（需当前账号配置 OpenAI 兼容 LLM）。`?download=1` 返回带文件名的附件。只读权限即可导出；transfer key 数据对接待做 |

> **AI 辅助阅读**的句子笔记上下文由前端提供（`[{id,sent,note,page}]`）—— 只有前端知道「这条笔记挂在哪句话上」；
> 后端补入已保存的独立笔记，并负责收敛长度（条数/单条/总长都有上限）与拼提示词。摘要要求模型返回 **JSON**（而非 Markdown），
> 逐字段清洗后再渲染，所以模型偶发的字段缺失/类型不对不会把页面弄花。
>
> **对话带着整篇正文**：`/ai/chat` 的上下文（历史最前）除了标题/笔记/整理，还有后端用
> `ai_read.article_text()` 现拼的**全文与每页公式 LaTeX**（`pages[].formula_boxes`；旧数据的
> 公式块也认）—— 正文里公式位置是解析时擦出的空格，所以 LaTeX 按阅读顺序附在每页末尾，
> 模型才能答「这篇讲了什么 / 这个公式是什么」。超长原文按页截断（上限 `MAX_ARTICLE_CHARS`）。
>
> **对话是流式的**：`/ai/chat` 返回 `text/event-stream`（`X-Accel-Buffering: no`，nginx 后面也不会被攒着），
> 后端用 `Translator.chat_stream()`（内部是 `agents/llm.py` 的 `LlmClient`：`httpx.stream` + `iter_sse_deltas()`）把模型的增量原样转发；
> 前端用 `fetch` + `ReadableStream` 边收边画（`pushAiDelta()` 只改那一个气泡的文字，不整框重画）。
> 能提前看出来的问题（没配 LLM、消息为空/超长）在发头之前就是 HTTP 503；
> 已经开流之后才出的问题（连不上上游、上游中途断开）走 `{"type":"error"}` 事件 ——
> 前端此时**保留已收到的文字**（不把字收回去），一个字都没收到才把提问退回输入框。
>
> **整理结果是可编辑的**：前端把整份结构（`{overview, themes[], connections[], questions[], next[]}`）
> 改完后 POST 给 `/ai/summary/save`，服务端用 `clean_summary(loose=True)` 清洗（条数/字数上限放宽，
> 不会把用户写的长句子截掉）。「重新整理」时 `build_summary(previous=…, chat=…)` 会把**旧版整理**
> 与**已发生的对话**（`history_text()`，按轮数与总长收敛）一并当素材喂给模型，所以讨论成果会沉淀进新版。
> “笔记有没有变”用**内容指纹**判断（不是时间戳），同一秒改笔记也看得出来。
> 整理与对话存进记录文档的 `ai` 字段：`{summary, summary_at, notes_count, notes, notes_fp, chat[]}`。

## 数据存储（MongoDB）

### 用什么连

连接参数由 `backend/db.py` 读取，**不用在项目里重复配**（优先级高 → 低）：

1. 环境变量 `MONGO_URI`（完整连接串，云端 Atlas 也行）
2. 环境变量 `MONGO_HOST` / `MONGO_PORT` / `MONGO_USER` / `MONGO_PASSWORD` / `MONGO_DB` / `MONGO_AUTH_DB`
3. 下面这份 `.env`（`docker compose` 用的那份：`MONGO_PORT`、`MONGO_APP_USER`、`MONGO_APP_PASSWORD`、`MONGO_APP_DB`）
4. 内置默认：`paper:paper123@127.0.0.1:27017/paper?authSource=paper`

参数文件按顺序找**第一个存在的**：`mongo_configuration/.env` → `docker/.env` → 仓库根 `.env`；
compose 目录改名了也不用改代码，还可以用 `MONGO_ENV_FILE=/path/to/.env` 插到最前面。

默认用 `.env` 里的**业务账号**（对本库 readWrite）连接，不用 root。

> **连不上时先跑体检**：`python tools/mongo_check.py`
> 它会打印实际生效的连接串（口令打码）、参数是从哪个文件读的、连通性结果；
> 如果是 **Authentication failed**（最常见：数据卷是旧的、初始化脚本被跳过，
> 业务账号根本没建出来，而应用只会报一句 `Authentication failed.` 然后悄悄回退 JSON），
> 它会直接给出修复步骤，并用 `--create-user` 按 `.env` 里的 root 账号把业务账号补建出来。

### 集合划分

| 集合 | 内容 |
| --- | --- |
| `records` | 论文元数据/阅读进度/状态（原先的 `records.json`）；**`owner` 字段 = 归属账号，论文按账号隔离**，索引建在 `(owner, last_read_at)` 上。共享信息也在这一行里：`shared: [{user, perm, by, at}]`（权限名单）、`reading: {用户: {progress,…}}`（非所有者的阅读进度）。阅读进度含 `last_pos`（`{pi, ratio}` = 第几页 + 页内比例，覆盖写）用于精确回位；目录存在 `toc` / `toc_source`（`pdf` / `llm`）/ `toc_at`；AI 整理与对话存在 `ai` |
| `documents` | 版式数据（原先的 `doc.json`，放在 `doc` 子字段） |
| `highlights` / `notes` | 高亮 / 笔记：**一条一个文档**，增删改都是单文档原子操作（笔记在 `(doc, block_id)` 上有唯一索引，同一句重复添加=更新）；笔记还带 `by`（写它的账号），共享论文里前端据此标作者 |
| `translations` | 译文缓存：**一段一条**（`{doc, h, zh}`，`h` 是「目标语言 + 文本」的哈希），可按论文统计/清理；换语言不会命中旧语言的缓存 |
| `images.files` / `images.chunks` | **插图本体**（GridFS 桶）：`_id = "<doc_id>/<fname>"`，`metadata.doc_id` 上建了索引。插图也进库，服务重启/重建容器/换机器都不会丢，前端仍用 `/api/doc/{id}/img/{fname}` 取图（接口内部改成从存储后端读字节） |
| `seltrans` | 「所选句译文」卡片（`{doc, key, item_id, si_from, text}`）—— 卡片的正文与锚点存这里，**刷新/重新登录后会自动恢复**（片段译文的 key 是片段文本哈希，不在 `translations`→块 的映射里，所以必须单独存锚点） |
| `users` | 账号（口令 PBKDF2 加盐哈希）；`prefs` 子字段存该账号的偏好与配置 —— `prefs.llm`（该账号的 LLM 配置，含最多 3 套可切换的 `profiles` 与当前生效的 `active_id`）、`prefs.target_lang`（译文目标语言）、`prefs.parser.backend`（解析后端）、`prefs.hl_colors`（自定义高亮色）、`prefs.show_zh`（显示译文开关）、`prefs.theme`（`light` / `dark`） |
| `sessions` | 登录会话（带 TTL 索引自动过期） |
| `settings` | **旧版**全局 LLM 配置（`_id="llm"`）；现在只在升级时读一次（自动归给最早创建的账号），不再写入 |

插图与其它数据一样**存在库里**（`images` 是 GridFS 桶），所以备份只需一条 `mongodump`，
不用再单独打包 `data/docs/`；磁盘上的 `data/docs/<id>/images/` 只在「回退本地 JSON」时使用
（Mongo 模式下读图时仍兼容留在磁盘上的老图片，迁移前不迁移都能看）。

### 连不上数据库时：回退 + 一键重连

`APP_STORAGE` 控制存储后端：

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | 能连上就用 MongoDB；连不上回退本地 JSON，**日志 + 登录页 + `⚙ 设置` 弹窗都会提示** |
| `mongo` | 强制 MongoDB，连不上直接启动报错（避免“以为在写库其实写了文件”） |
| `json` | 强制本地 JSON（不需要数据库的轻量场景） |

回退后启动数据库，点右上角「⚙ 设置 → 存储 / OCR → 重试连接 MongoDB」即可**热切换**回数据库（不用重启服务）。
切换后端时旧会话在新库里不存在，会自动跳回登录页（账号需已迁移/重建）。

### 迁移已有的本地数据

```bash
python tools/migrate_to_mongo.py --dry-run   # 先看要迁什么（含插图张数）
python tools/migrate_to_mongo.py             # 真正迁移（同 id 覆盖；账号/设置/插图一并搬）
```

插图会一并存进 GridFS（`--skip-images` 可跳过）。迁完确认阅读页图片正常，`data/docs/` 就可以删了。

**插图丢了怎么办**（比如以前图片还在磁盘、目录被清掉）：只要还有原始 PDF，就能不重传地补回来 ——
版式数据/高亮/笔记/译文/进度全部保留：

```bash
python tools/relink_images.py --list                        # 看哪几篇缺图
python tools/relink_images.py <doc_id> 原始PDF路径           # 重新解析并写回同一个 doc_id
```

### 清理与备份

应用使用的业务账号**没有** `dropDatabase` 权限（这是好事）。清空数据用 root：

```bash
docker compose -f mongo_configuration/docker-compose.yml exec mongo \
  mongosh -u root -p changeme --authenticationDatabase admin \
  --eval 'db.getSiblingDB("paper").dropDatabase()'
```

备份/恢复命令见 `mongo_configuration/README.md`（`mongodump`/`mongorestore`）。

## 用 Docker 部署（可选）

想把服务打包成容器（与宿主机环境隔离）用 `docker/` 下那套：

```bash
cd mongo_configuration && docker compose up -d   # ① 先起 MongoDB
cd ../docker && docker compose up -d --build     # ② 再起应用，打开 http://127.0.0.1:8000
```

两份 compose 共用网络 `paper-mongo-net`，应用通过服务名 `mongo` 连库，**看到的是同一份数据**；
插图等运行时文件放在命名卷 `paper-app-data`（容器内 `/app/data`）；用 MongoDB 时插图其实存在库里，
这个卷主要装上传临时文件与回退模式的数据。详见 `docker/README.md`
（环境变量、数据卷、挂载 `config.json`、回退模式、注意事项都写在里面）。

## 快速自检(可选)

```bash
python tools/make_sample_pdf.py   # 生成样例 PDF
python tools/selftest.py          # 校验“文档解析”模块(文字块/图片抽取)
python tools/selftest.py xxx.pdf --backend surya   # 走 Surya 2 整页 OCR（需服务已起 + 本机已装 surya-ocr）
python tools/selftest.py xxx.pdf --backend unlimited_ocr   # 走 Unlimited-OCR 整页 OCR（需服务已起）
python tools/reparse_layout.py --list               # 看已上传的论文用哪个后端/有多少公式框
python tools/mongo_check.py       # MongoDB 连接体检(参数从哪读/能否连上/怎么修)
python tools/mongo_check.py --create-user   # 用 .env 里的 root 账号补建业务账号
python -m unittest discover tests  # 全量测试（引用解析 / AI Agent / 版式后处理等）
python -m unittest tests.test_refs # 只跑引用：编号/作者-年份、标题并块、链接+回退合并
# 然后把 samples/sample.pdf 拖到首页即可体验完整阅读流程
# （旧的「空位 × OCR LaTeX」配对自检已随 PaddleOCR 链路一起删除，不再提供）
```

## 文档解析后端（本地解析方案 / Surya 2 / Unlimited-OCR）

> `auto` / `pymupdf` 两个取值都走**本地解析方案**：
> PyMuPDF 文字层 + `local_parse_pipeline.process_pages()`（texify 公式识别、
> OXR（D-FINE）版面分析定位表格/插图、SLANet_plus 表格转 HTML、矢量图/位图提取）。前端按
> `parser === "pymupdf"` 逐行绝对定位渲染，公式用页面级 `.fbox` 覆盖层（KaTeX，
> 失败时用裁图兜底）。模型/依赖缺失时 `parse_pdf` 直接抛
> `LocalParseUnavailable`，不会悄悄回退其它后端。
>
> 旋转文字（竖排页边章、倒排文字）按 PyMuPDF 的 `line["dir"]` 识别：行上额外下发
> `rot`（CSS 旋转角）/ `ox`·`oy`（未旋转时的左上角）/ `rw`·`rh`（未旋转时的宽高），
> 前端用 `transform: rotate()` 摆回去；这些行不参与按句切块，整块带 `vertical: True`。

解析服务参数由 `config.json` 的 `parser` 段控制（环境变量优先于配置文件）；解析后端
可在网页「⚙ 设置 → 存储 / OCR」中按账号切换并保存到账号偏好
（`users.prefs.parser.backend`）。未单独设置时沿用 `config.json` 默认值，
`PDF_PARSER_BACKEND` 环境变量优先于账号选择：

| 配置 | 说明 |
| --- | --- |
| `backend` | `pymupdf` / `auto`(默认) = **本地解析方案**（PyMuPDF + `local_parse_pipeline`，依赖与模型见 `backend/pdf_parser/local_parse_pipeline/`）；`surya` = 整页 OCR（文字/公式 `<math>`→LaTeX/插图全部来自 Surya 2，前端按 `parser === "surya"` 独立渲染，需本机装 `surya-ocr` 客户端）；`unlimited_ocr` = 整页 OCR（文字/公式 LaTeX/表格/插图全部来自 Unlimited-OCR，前端按 `parser === "unlimited_ocr"` 独立渲染）。未知值会按 `auto` 处理并记一条 warning |
| `local_device` | 本地方案推理设备：空/`auto` = 自动（cuda > mps > cpu），也可填 `cpu` / `cuda` / `mps` |
| `local_dpi` | 本地方案公式框裁剪的渲染 dpi（默认 300） |
| `local_padding` | 公式框裁剪四周外扩（pt，默认 2.0） |
| `local_keep_crops` | 是否把公式裁图落盘（KaTeX 失败时的兜底，默认开） |
| `local_temperature` / `local_max_tokens` | 本地方案 texify 公式识别的解码温度（默认 0 = 贪心）与单条公式最大 token（默认 384） |
| `local_min_figure_size` / `local_max_figures` | 插图（OXR 检测框 / 矢量图）最小宽高（pt，默认 24）与每页最多保留张数（默认 24） |
| `local_use_vector_figures` | `false`（默认）= 插图只用 OXR 检出的 Picture 框；`true` = 再叠加 `get_drawings()` 的矢量矩形 |
| `image_pad` / `min_image_size` | 插图裁剪四周外扩像素（默认 4）/ 宽或高小于该值的插图丢弃（默认 32） |
| `surya_url` | Surya 推理服务地址，默认 `http://127.0.0.1:8060/v1`（服务端见仓库根 `surya_doc_parse_service/`） |
| `surya_backend` | 服务端类型：`llamacpp`(默认) / `vllm` |
| `unlimited_ocr_url` | Unlimited-OCR 推理服务地址，默认 `http://127.0.0.1:9010/v1`（服务端见仓库根 `unlimited_ocr_service/`） |
| （在线兜底） | `backend=unlimited_ocr` 时**本地服务没配置 / 没起来会自动改用在线解析**（百度智能云「文档解析（Unlimited-OCR）」，异步提交 + 轮询，按页计费）；**网页设置面板不新增项**，凭据写在 `config.json` 的 `parser` 段（`unlimited_ocr_online_api_key` / `unlimited_ocr_online_secret_key`），也可用环境变量 `UNLIMITED_OCR_ONLINE_API_KEY` / `UNLIMITED_OCR_ONLINE_SECRET_KEY` 覆盖；需要排障时可用 `unlimited_ocr_route`（或 `UNLIMITED_OCR_ROUTE`）填 `local` / `online` 强制指定（默认 `auto`）。实际走的哪条记在 `doc.json` 的 `parser_info.unlimited_ocr_route` |
| `unlimited_ocr_model` | 请求用的模型名（要和服务端 served-model-name 对得上），默认 `Unlimited-OCR` |
| `unlimited_ocr_timeout` | 单页请求超时秒数（默认 600） |
| `unlimited_ocr_api_key` | 服务端设了 `VLLM_API_KEY` 时才需要，默认空 |
| `paddle_url` / `paddle_model` / `paddle_prompt` / `paddle_max_tokens` / `paddle_timeout` / `paddle_api_key` / `paddle_ocr_math` | **已删除**：PaddleOCR-VL 的旧客户端与配对链路已随代码移除（本次起**不再被读取、不再被识别**）；键与 compose 里的 `PADDLE_OCR_*` 环境变量保留，仅作历史记录 |
| `dpi` | 页面渲染分辨率，读取 `config.json` 的 `parser.dpi`（示例值 150）；越大越慢，可按模型对目标 PDF 的识别表现调整 |
| `image_dpi` | **插图裁剪**的独立渲染分辨率（`parser.image_dpi`，0=与 `dpi` 相同）。只影响插图清晰度：对该页按更高 dpi 重渲一次再裁，OCR 速度与版式坐标不变；建议 300 左右 |
| `image_labels` | 视为插图的检测类别，默认 `Picture/Figure/Diagram/ChemicalBlock` |
| `keep_html` | 是否把 block 的 HTML(表格/公式)写进 `doc.json`；Surya 后端渲染需要 html，始终保留（旧配置项仍被读取但不再影响两条新链路） |
| `fallback` | [已失效] 旧「后端失败回退」开关 |
| `local_image_fallback` | [已停用] 旧 PyMuPDF 图片回退选项；配置中仍存在时会在解析告警里提示 |
| `drop_text_in_images` | 是否剔除“已经画在插图里”的文字行，避免与图内文字叠字重影(默认开) |
| `text_in_image_overlap` | 判定“整行落在插图里”的覆盖率阈值，默认 `0.6`(60%) |

环境变量：`PDF_PARSER_BACKEND`、`SURYA_INFERENCE_URL`、`SURYA_INFERENCE_BACKEND`、
`UNLIMITED_OCR_URL`、`UNLIMITED_OCR_MODEL`、`UNLIMITED_OCR_TIMEOUT`、`UNLIMITED_OCR_API_KEY`、`PDF_PARSER_DPI`、`PDF_PARSER_IMAGE_DPI`、
`PDF_PARSER_DROP_TEXT_IN_IMAGES`；`PADDLE_OCR_*` 已随代码删除（键若保留仅作历史记录，不再被读取）。
设置面板会显示**实际生效**的后端以及 Surya 与 Unlimited-OCR 的就绪状态。

优先级是 `parse_pdf(backend=...)` > 环境变量 `PDF_PARSER_BACKEND` > `config.json`，**但环境变量只在
真的设了非空值时才参与**（空字符串=没设）。Docker 部署下 compose 默认**不再**注入这个变量，所以面板
里切换即时生效；要强制锁定后端才在 `docker/.env` 里写 `PDF_PARSER_BACKEND=...` —— 此时面板里保存的
值会被覆盖（页面上会给出提示）。

### 现在的分工（2026-11）

- **三条解析链可用，在「⚙ 设置 → 存储 / OCR」里选**：
  - `pymupdf` / `auto`：**本地解析方案**（PyMuPDF 版式
    + texify 公式 + OXR 版面 / SLANet 表格 + 插图提取，见 `backend/pdf_parser/backend/backend_pymupdf.py`
    与 `local_parse_pipeline/`）；
  - `surya`：Surya 2 整页 OCR（见下节）；
  - `unlimited_ocr`：Unlimited-OCR 整页 OCR（见下节）。
- 本地解析方案依赖本地模型权重（`local_parse_pipeline/models/`）与本机 Python 依赖
  （texify / paddleocr / torch），状态面板会用 `local_ready` / `local_message` 如实显示。

### Surya 2 后端（`backend=surya`：整页 OCR + 前端独立渲染）

**整条链路完全使用 Surya 的数据**（不读 PyMuPDF 的文字层/行框/字体）：

1. **客户端**（`backend/pdf_parser/surya_parser.py`，需 `pip install surya-ocr`）：PyMuPDF 只负责把页面渲染成
   图像 → `RecognitionPredictor` 通过 `SURYA_INFERENCE_URL` 调用服务端（`surya_doc_parse_service/`）
   做整页 OCR → 每页拿到按阅读顺序排好的 blocks（`label` / `polygon` / `html`）；
   图片类 block（`Picture/Figure/Diagram/ChemicalBlock`）按 polygon 裁剪落盘。
   ⚠️ 图片类 block 一定是 `skipped=True`/空 html（模型不对图 OCR）——不能当过滤条件。
2. **转换**（`backend/pdf_parser/backend/backend_surya.py`）：像素坐标 × `72/dpi` 换算成 PDF 点，
   写进页面 `texts[]`（**块级数据**：`kind`/`label`/`level`/`size`/`line_h`/`text`/`lines[0].runs`/
   `math`/`html`；公式块是 `kind="formula"` + `latex`），插图写进 `pages[].images`。
   字号不来自 OCR：按块框与文本量估算（正文统一页基准字号 + 按块框缩字适配）。
3. **渲染**（`frontend/js/items/item-surya.js::buildSuryaItem`，`DOC.parser === "surya"` 时启用）：块按 bbox
   绝对定位、**块内正常流式排版**（标题加粗放大、图注/脚注小一号），与 PyMuPDF 路线的
   「逐行绝对定位 + 行宽适配」是两套；行内公式用解析侧给的精确字符区间就地渲染 KaTeX，
   独立公式走 KaTeX 块，表格 `html` 白名单渲染（不参与句子选中）。选句/高亮/笔记/翻译
   沿用同一套句子机制（`.rn` span + canonical 字符区间）。
4. **失败语义**：Surya 不可用（服务没起 / 本机缺 `surya-ocr`）时**直接报错**，
   不会悄悄回退 PyMuPDF —— 免得看起来像解析成功了。状态面板里 `surya_ready` 是
   「服务端 + 客户端都就绪」。

### Unlimited-OCR 后端（`backend=unlimited_ocr`：整页 OCR + 前端独立渲染）

与 Surya 2 那条链**完全独立**（解析模块、排版估算、前端渲染函数与 CSS 类名都不共用，
一边改动不会牵连另一边）：

1. **客户端**（`backend/pdf_parser/unlimited_ocr.py`）：PyMuPDF 把页面渲染成 PNG → 逐页串行调
   `POST {unlimited_ocr_url}/chat/completions`（vLLM 的 OpenAI 兼容接口；服务端见仓库根
   `unlimited_ocr_service/`，默认 `http://127.0.0.1:9010/v1`）；回包是模型的 grounding
   输出（`类型 [x0, y0, x1, y1] 文本`），解析成结构化版面项；`image`/`chart` 项按 bbox
   从 PDF 页面裁剪落盘。**不需要额外 `pip install`**（只用标准库 + PyMuPDF）。
   - ⚠️ 坐标是 **0–1000 归一化值**（x、y 各自除以页面宽高再乘 1000），不是渲染图像素
     —— 换渲染 dpi 不影响坐标，也不影响插图裁剪（从 PDF 按 pt 直接渲矩形）。
   - ⚠️ 提示词必须以字面量 `<image>` 开头，且请求必须带 `skip_special_tokens=false`
     与 `vllm_xargs`（缺了输出为空 / 会在 `<|det|>` 上打转）。
2. **转换**（`backend/pdf_parser/backend/backend_unlimited_ocr.py`，**独立实现**）：归一化
   坐标 ÷ 1000 × 页面 pt 尺寸 → PDF 点；写进页面 `texts[]`（**块级数据**：`kind` /
   `utype`（模型原始类型）/ `size` / `line_h` / `text` / `lines[0].runs` / `math` / `html`）。
   字号由本模块自己的估算器反解：全 PDF 统一正文基准，标题放大、图注/脚注/参考文献
   小一号；双栏页自动把被挤没的列间空隙修回 ~10pt。公式块是 `kind="formula"` + `latex`
   （`\[ … \]` 定界符已剥掉），表格块是 `kind="table"` + 原始 `<table>` HTML（另附
   纯文本供翻译/复制）；插图写进 `pages[].images`（`source="unlimited_ocr"`）。
3. **渲染**（`frontend/js/items/item-unlimited.js::buildUnlimitedItem`，`DOC.parser === "unlimited_ocr"`
   时启用）：块按 bbox 绝对定位、**块内正常流式排版**（`.blk.ul` / `.ul-*` 是独立一套
   样式，与 Surya 的 `.blk.sy` 各一份）；行内公式用解析侧给的精确字符区间就地渲染 KaTeX，
   独立公式走 KaTeX 块，表格 HTML 白名单渲染（不参与句子选中）；纵向避让量走独立字段
   `ulShift`。选句/高亮/笔记/翻译沿用同一套句子机制（`.rn` span + canonical 字符区间）。
4. **失败语义**：服务不可用（没起 / 超时 / 回包为空）时**直接报错**，不会悄悄回退
   PyMuPDF。状态面板里 `unlimited_ocr_ready` 是 `GET /models` 真探测结果。

### 文字数据：一行只有一个 run、一个块只有一句

解析结果里**每行只有一个 run**（`text_layer._merge_line_runs`）—— 一块文字就是
「块 `text` + 每行几何 + 每行文本」三层，行内不再有碎片 span。为此做了两处补偿：

- **补间距**：PyMuPDF 按 span 切字，同一视觉行里两截文字只要字体/字号不同就是两个
  span，**中间的空隙不体现在字符上**（表格把 `Method` / `Accuracy` 并进同一行最常见）。
  解析时按 bbox 空隙补一个空格（`text_layer._insert_gap_spaces`，阈值 `0.25 × 字号`），
  否则合并后会粘成 `MethodAccuracy`。公式字形两侧**一律不补**：那会把一处空格区切断，
  一条公式在配对时会被拆成两条。
- **行级样式**：合并取「本行字符数最多的那个 run」的字号/字体（与前端 `bodyRunOf` 同口径），
  于是行内上标/小字号的碎片不会再让整行字号忽大忽小（"公式后正文变小"的老问题）。

代价（已知并接受）：行内的粗体/斜体/上标与颜色退化成行级样式，真正的上标（`x²` 的 `2`）
会与正文同号显示；高亮/笔记的字符↔DOM 映射在**含公式的**行上可能略有偏移。
换来的是渲染与公式内联都退化成「一行一段文字」，实现简单得多。

#### 按句子重切块：一个块 = 恰好一句（`sentences.py`）

**一行不是一句，一块也不一定是一句**：一段话常被公式空位切成好几块，而且两块的
**行在同一视觉行上左右相接**（实测样本 `p1b45` = `In mathematics, … using the formula`、
`p1b47` = `, where … the roots.`）。前端 `splitSentences()` 是按**块**切句的，
所以点一下选中的「句子」就是半句碎片。解析侧现在把它修好：

- `sentences.cut_blocks_by_sentence`（在 `_blank_math_spans` **之前**跑）把一页的块
  重切成「一句话一块」：
  1. **行链**：同一视觉行上横向相接（≤ 16pt，或中间只夹着公式字形）的片段并成一组；
     双栏页的**栏间走廊**（同一 x 带在 ≥3 行上都是空的）不算相接，公式编号 `(12)`/页码也不并；
  2. **续句**：上组末行与下组首行同栏（左边距差 ≤ 12pt）+ 纵向邻近 + 「上面还没写完
     （末尾不是 `.!?。！？:`）+ 下面接着写（首字符是小写字母或 `,;)`）」→ 同一条**流**；
     证据不足就断开（宁缺勿错：断开只是保持现状，接错会把标题/页码粘进句子）；
  3. **切句**：口径与前端 `reader2.splitSentences` 一致（另加「小数点 `1.1` 不切」与 `。！？`）；
  4. **落块**：每条句子一块；句子端点落在行内部时按**逐字符几何**（`text_layer._CHAR_X`，
     来自 `rawdict` 的 `chars[*].bbox`）把行**拆成两段** —— 拆点精确落在字符边界上，
     run 的 `x0/x1` 也跟着重算，所以公式空位原宽（`_line_gap_widths`）算出来还是原值。
- 公式判读用「探针文本」（数学字形换成等长空格），公式里的 `.`/`!` 不会被当成句末；
  整行都是公式字形的行不参与切句，作为**乘客**挂在邻近句子单元上（几何要留给
  `_absorb_orphan_gaps` 与整行公式回填的落点）。
- 统计：`sentence_units` / `sentence_merged` / `sentence_line_splits` / `sentence_tail_units`
  （最后一项 = 末尾没有句末标点的单元数，即「合并不了的碎片」，如标题、表格、页眉）。
- 不变式：**一个字都不增删**（重解析摘要实测「内容变了 0 处」），公式统计
  （`formula_spans/chars/spaces/lines/absorbed_lines`、空位处数）**逐项不变**。
- ⚠️ **块 id**：没被拆、没被合的块沿用原 id（笔记/高亮按 `"<块 id>#<句号>"` 存），
  其余用 `p{页}s{序号}` —— 所以重解析后，**被切碎过的那几块**上的旧笔记会显示
  「原文句已不存在」（笔记正文不丢）。译文缓存键是块文本的 sha1，不受影响。

#### 本地公式识别

本地解析方案使用 `local_parse_pipeline` 做公式区域聚合、裁剪与 texify 识别；表格与插图
由 OXR 版面分析和 SLANet 处理。依赖与模型见
`backend/pdf_parser/local_parse_pipeline/README.md`。

### 解析器升级后：原地重解析（笔记/译文都保留）

**库里存的版式数据不会自动跟着解析器变** —— 改了代码、换了后端、调了 DPI 之后，
已经上传过的论文还是旧数据，所以「改了半天没看到效果」通常就是没重解析。
不用重新上传（那会生成新论文、笔记译文全丢），有两条路：

1. **网页里**：打开那篇论文 → 顶栏「🔄 重解析」→ 选中**同一份 PDF** → 解析完自动刷新。
2. **命令行**（连的是应用那个库，Docker 部署建议在容器里跑）：

```bash
python tools/reparse_layout.py --list                        # 看有哪些论文、什么后端
python tools/reparse_layout.py <doc_id> 原PDF.pdf --dry-run   # 先看会变什么（不写库）
python tools/reparse_layout.py <doc_id> 原PDF.pdf             # 原地重解析
# Docker: docker compose exec app python tools/reparse_layout.py <doc_id> /app/x.pdf
```

> 如果只是**解析器新增了“参考文献分块”这类纯派生步骤**（文字本身不用重新 OCR），
> 可改用更轻的 `python tools/split_ref_blocks.py --all`（只补新增的拆分块，不碰 OCR）。

只换 `pages` / `parser` / `parser_info` / 插图；笔记、高亮、译文、阅读进度、分享状态一个不动。
译文是按「块文本的 sha1」缓存的，解析升级可能改块文本（重切句等）→ 工具会自动把老译文的键
**迁到新文本**上（对得上就不用重译，迁移条数会打印出来）。
> ⚠️ 旧版「OCR 整页解析 + 把 LaTeX 注入正文 + 合成 `kind="formula"` 块」已删除；
> 本地解析方案现在是「PyMuPDF 版式 + `local_parse_pipeline` 的 texify 公式框/表格/
> 插图」，见 `backend/pdf_parser/backend/backend_pymupdf.py` 与
> `backend/pdf_parser/local_parse_pipeline/README.md`。

### 引用跳转（正文引用 → 参考文献卡片）

正文里的 `[12]` 点一下**就地弹参考文献卡片**，不用再往后翻。优先使用
**PDF 自带的内部跳转链接**（LaTeX/hyperref 排的论文里，引用标记通常是一个
`LINK_GOTO` 注释、目标指向参考文献列表里那一条）；如果没有可用的内部跳转，
则按正文引用令牌与参考文献条目做文本回退匹配（数字编号式与作者-年份式都支持）。
两种方式都不使用 LLM：

1. **采集**（解析时，各后端统一）：`pdf_parser/links.py` 用 `page.get_links()`
   把内部跳转（源侧 `rect` + 目标 `page`/`to`）与 URI 外链写进 `pages[].links`
   （PDF 坐标 pt，与块 bbox 同口径）。链接来自 PDF 注释本身、与“文字版式来自
   哪个后端”无关，`pymupdf` / `surya` / `unlimited_ocr` 一条都不落。
2. **映射**（`GET /api/doc/{id}/refs`，纯本地毫秒级、**不缓存、每次现算**）：
   `backend/pdf_refs/refs.py`（`app.py` 引用的是这个包，`backend/refs.py` 是早期副本、线上不再使用）按 `parser` 分发到各策略。PyMuPDF 与 Unlimited-OCR 使用通用流程：
   找参考文献区（全文档**最后一个** `References`/`Bibliography`/`参考文献` 标题块；
   标题与第一条并进同一个块时（本地解析方案常见）按前缀识别并只保留标题后的正文；
   Unlimited-OCR 还能用 `kind="reference"` 兜底）→ 条目化（编号制 `[n]`/`n.` 起新条目、
   不带编号的块并进上一条；作者-年份制优先按 `[作者, 年份]` 条目标签切分，标签落在块中间
   也能切开；**没有标签时（ACL/arXiv 风格，条目行首直接是作者名）按“悬挂缩进”切**
   —— 区里缩进 8~10pt 的续行/标题/出版社并进上一条、齐左边缘的块起新条目；判不出缩进
   时退回“行首像作者姓名”的文本规则）→ 先按内部链接的目标点、
   源矩形匹配引用令牌；作者-年份回退扫描**始终与链接映射合并**（PDF 只给部分引用做了
   hyperref 时其余引用也能标出），没有映射出内部跳转时再回退扫描数字令牌 —— 数字令牌
   （`[n]`）关联编号条目，作者-年份令牌（`[Smith et al., 2020]` 与 `(Smith et al., 2020)`）
   按“年份 + 姓氏/实词”关联无编号条目（校验从严、宁缺勿错），并分三档：令牌首姓名
   **等于条目第一作者** > 令牌里的姓名**全落在条目作者段** > 年份 + 实词/姓氏的弱匹配；
   多引用令牌 `(作者, 年份; 作者, 年份)` 先按 `;` 拆成单条引用、**逐段各自匹配**，
   同一段内只保留最高档，避免共同作者让一批同年文献都挂到同一个引用上。Surya 使用独立策略
   （`backend/pdf_refs/refs_surya.py`）：保留 OCR 块内换行，在行首识别并拆分同一块中的多条
   文献（编号制、`[作者, 年份]` 标签的作者-年份制，以及**无标签**作者-年份制 —— Surya 的
   参考文献常是一条一行、行首直接是作者名，按“行首像作者姓名 **且** 上一行已写完
   （句末标点/右括号，或已有年份）”切，折行条目的续行（`… Advances in Neural` /
   `Processing Systems, 35:…`）不会被劈开）；然后按正文数字/作者-年份令牌直接
   映射，不会因为 PDF 中另有无关内部链接而跳过其余引用。单条文献独占一个 OCR 块时，
   仍可用 PDF 内部跳转映射。
3. **展示**（前端 `r2-refs.js`）：按 `links[].block/start/end`（块 canonical 文本的
   字符区间，与笔记/高亮同口径）把引用包成 `.cite` 标记；点击弹卡片（条目原文 + 
   `定位原文`（滚到参考文献那一条并闪烁）+ `复制` + 条目带 DOI/arXiv 链接时
   `打开链接`）。编号条目显示 `[n]`，作者-年份条目显示“第一作者姓氏 + 年份”
   （如 `Chen 2019`）而不是笼统的圆点。标记是**幂等**的：选句/高亮/缩放导致块重建后
   自动重打；点引用**不会**触发“选中该句”（点击事件在捕获阶段接管、拦住冒泡）。
   **一条引用 = 一个整体**：解析侧按颜色切 run 后，一条引用常横跨好几个 `[data-g]` 片段
   （编号制连 `[` / `12` / `]` 都可能落在不同字体段），前端会把这些**同一行内连续**的
   片段收进同一个 `.cite` —— 悬停高亮、点击弹卡都是一整组，不会碎成几块。

已知边界：

* 编号式方括号引用在 PDF 没有内部跳转时会按 `[n]` 与参考文献编号回退匹配；作者-年份
  引用（`[Smith et al., 2020]` / `(Smith et al., 2020)`）按“年份 + 姓氏/实词”回退匹配
  （校验从严，两类都对不上时不标）。作者-年份回退**不要求 PDF 带内部跳转**，也会与
  已有的链接映射合并；纯上标数字式引用（`…text¹`）不支持。
* 引用令牌跨行断开时（如 `[Whit-` 换行 `ley, 1994]`，或长多引用 `…Movahedi` 换行
  `et al., 2026)`）按**每条行内一片**下发锚点（后端 `pdf_refs/refs.py::_range_pieces`），前端每一片
  各自包成 `.cite`、共用同一批条目 —— 不再整段放弃（前端 `wrapRange` 遇换行“洞”仍会
  拒绝单跨行标记，所以必须拆片）。
* Surya 的同块多条文献拆分依赖 OCR 保留每条文献开头的换行；如果模型把不同条目的
  开头和正文合并到同一行，当前无法可靠判断条目边界（编号制、带 `[作者, 年份]` 标签与
  无标签的作者-年份制都拆不动）。无标签条目的拆条规则是“行首像作者姓名 + 上一行已写完”，
  因此以出版信息开头的续行（`Machine Learning, pages …`）不会被误当成新条目；反过来，
  缺句末标点且上一行也没年份时，两条相邻文献可能仍并成一条。
* 回退匹配缺少 PDF 链接注释提供的几何校验，因此正文中与有效参考文献编号相同的数学
  方括号表达式也可能被识别为引用；PDF 有内部跳转时仍优先使用精确映射。
* `[10–12]` 这类数字令牌会展开成**范围内能对上的全部条目**：卡片里列出 10/11/12
  三条（即使 hyperref 只把整段引用链接到其中一条）；列表里没有对应编号的逐个跳过。
  一条令牌挂多条链接（如 `[3,5]`）会合并成一个标记，卡片里按条目分条列出。

### 阅读页逐页加载（首屏只看头几页，其余后台补）

阅读页不再等整篇版式都传完才渲染：

1. **首屏**：`reader2.js` 先按上次阅读位置（`last_pos` / `last_page`）请求看得见的那几页
   （`FIRST_PAGES = 3`，文末则以 `page_from=-3` 取倒数几页），用 `page_sizes` 先把
   **所有页的占位壳**建出来（页码、宽高、滚动高度从第一帧起就是全篇的），再把有数据的页
   逐页 `mountPage()`（建块、批注区间、公式框）。
2. **后台补齐**：`pumpPages()` 按「离当前页最近优先」一批批请求（`PAGE_CHUNK = 3`，
   用 `requestIdleCallback` 让出主线程）；每批只对**本批页**跑收尾（`finalize* / relayoutAll(only)`），
   不会每补一批就把全篇重量一遍。
3. **按需加载**：目录跳页（`ensurePage`）、笔记定位 / AI 点亮文本框（`ensureBlockLoaded` /
   `flashRelatedBlock`）、「翻译本页」「翻译全文」（后者先把整篇补齐）都会先把对应页拉下来。
4. **公式与目录**：`needs_math` 是**全文**口径（只看首批切片会漏判，KaTeX 就拉不起来）；
   目录、页码、进度与位置还原从一开始就是整篇的。

后端 `GET /api/doc/{id}?page_from=&page_to=`（`app.py::_doc_slice`）：只下发 `[from,to)` 的页，
同时给 `page_total / page_from / page_to / page_sizes / needs_math`；`page_from` 可为负
（-3 = 倒数 3 页）。**不带参数时与以前完全一致**（整篇下发）。

### 目录与精确阅读位置

- **目录**：转换时把 **PDF 自带书签**存进记录（`toc` / `toc_source="pdf"`）；PDF 没书签时，
  首次展开目录面板会自动调 `POST /api/doc/{id}/toc`，让 **LLM 读全文排一份目录**（`_outline_context()`
  每页只喂「候选标题行」，省 token），结果存库（`toc_source="llm"`、`toc_at`），不再重复花 token；
  底部「重新提取」可强制重排。目录存在记录里、**共享论文大家共用**，所以需要写权限。
- **阅读位置**：进度记录里新增 `last_pos = {pi, ratio}`（第几页 + 页内比例，**覆盖写**），
  滚动时防抖保存，并在 `pagehide` / `visibilitychange` 补存；滚到正文末尾时记作 `{pi:-1, fromBottom:距底部距离}`。
  重进阅读页按它精确回位（老数据退回 `last_page`），缩放 / 目录开关 / 窗口变化重建版式后也按
  「页号 + 页内比例」还原，不会被弹回页首。

### 翻译目标语言（多语言）

- 可选语言表在 `backend/translate.py::TARGET_LANGS`（12 种），前端下拉由服务端下发，
  加语言只改这一处；目标语言存在 `users.prefs.target_lang`（与 `prefs.llm` 平级，**跟账号走**）。
- 译文缓存键 = `cache_key(text, lang)`：默认语言（简体中文）沿用原来的纯文本 sha1（历史缓存仍可用），
  其它语言把语言键混进哈希 —— 换语言不会命中上一门语言的译文，切回去也还在。
- 设置里换语言后，阅读页会收到 `langchange` 事件、清掉当前显示的旧译文并按新语言重新拉取；
  `GET /api/doc/{id}/zh_all` 也只回**当前目标语言**的缓存。

### 数学公式（KaTeX 渲染）

公式由 **KaTeX** 渲染（同步 API，随项目内置），落地方式：

- **页面公式框覆盖层（本地解析方案）**：解析侧下发 `pages[].formula_boxes`
  （`bbox_norm` + `latex` + 裁图 `file` + `copy` 字符区间），前端
  `r2-math.js::buildFormulaBoxes` 按页面显示宽高定位 `.fbox`，把 KaTeX 等比缩放到框内；
  KaTeX 未就绪/渲染失败时用裁图铺满框兜底，再不行显示 LaTeX 源码。
  选句/高亮会同步到公式框，复制时还原成 LaTeX。
- **旧数据的兼容路径**（重解析前的 doc.json）：
  * **独立公式块**（`kind="formula"` + `latex`）：用 `katex.render(..., {displayMode:true})`
    渲染到块覆盖层上，成功才隐藏原来的字形文本；**渲染失败保留原字形**。
  * **行内公式**：`\(…\)` / `$$…$$` / 单 `$…$` 定界符由 `renderInlineMath()` 扫描文本节点
    **原地替换**成 KaTeX（单 `$` 容易和货币符号撞车，所以额外要求内容“像数学”）；
    Surya 时代还有块 HTML `<math>` 对齐回字符区间的行内覆盖层（`inlineMathOf`），
    保留只为兼容旧 doc.json。

> ⚠️ 下面这些是**旧数据 / 旧渲染路径**（Surya 块行内对齐、早期 OCR 链路）踩过的坑，改代码时别踩回去：
> ① **公式块不能走行内路径**：公式块的 `html` 就是 `<math display="block">…</math>`（一整块数学），
> 行内对齐会把它当成“一条行内公式”再渲染一遍 → 行外覆盖层 + 行内覆盖层叠在一起，
> 就是**行外公式重影**。所以 `item.inlineMath = mb ? null : …`。
> ② **隐藏原字形要逐条公式控制**，且必须用 **class** 而不是内联样式：选句 / 高亮 / 标注都会调用
> `rebuildItem()` 重建所有字形 span，重建后内联样式就没了，原字形会从 KaTeX 底下冒出来变成
> **重影**（实测点一下块就有 47/305 个 span 掉色）。但也不能用块级 class——同一块里只要有一条
> 渲染成功，块内所有 `mspan`（含渲染失败那条的）都会变透明，那条公式就**直接消失**。
> 正确做法：`item.mathDone` 记录真正渲染成功的序号，只给它们的 span 加 `.mspan-hidden`。
> ③ **`renderInlineMath()` 不能按 `parts.length < 2` 跳过**：注入出来的 `\(…\)` 常常独自占一个
> run（样式与前后文不同，合并不了），这时 `_splitInlineMath()` 只返回 1 个元素，按长度跳过就会
> 把 `\(T^t\)` 原样显示成一串反斜杠。真正的「没有公式」是返回 `[原文]` 这一个**字符串**
> —— 所以要写成 `if (parts.length < 2 && typeof parts[0] === "string") continue;`。

#### 行内公式对齐（`_alignInlineMath()`，全在前端做，不用重新解析）

思路：把块的 HTML 拆成 `[文字, {latex}, …]`，逐个文字段在块的 canonical 文本（规整后）里
顺序定位，则**公式区间 = 前一段文字结束 → 后一段文字开始**。坑与对策：

- **不能用顺序相似度**：LaTeX 里 `r_t^{(h)}` 的上下标顺序与 PDF 字形顺序不一致
  （PDF 可能先出 `(h)`），所以用**乱序字符多重集**相似度（`_bagRatio()`）判定切出来的
  原文是不是这条公式，阈值 0.85。
- **短锚点会撞车**：后一段是 `.` 时，`norm.find(".")` 会命中公式内部 `...` 的第一个点，
  区间提前结束 → 取多个候选终点，挑第一个通过相似度检查的。
- **PDF 会在词中间插碎片**：`formulaic` 被切成 `formu-` + `t`（一个游离的下标）+ `laic`，
  整段锚点永远匹配不上 → `_findLit()` 退化为**最长前缀**命中，`_consumeLit()` 再把剩余的
  **最长后缀**找回来（否则游标停在词中间，后面的公式区间会多圈进一段正文）。
- **安全阀**：任何一项对不上就**整块放弃**（返回 null），保持原字形渲染——宁可不变，不可切错；
  渲染失败同理。结果按 `docId#块id` 缓存，只算一次。

> 注意：对齐用的是块 HTML 的**纯文本**（`<b>/<i>` 等标签会被剥掉），只影响定位，不影响原渲染。

行内公式的**字号/间距**还有五个坑：

1. **区间必须去掉首尾空白**：区间含空格的话，覆盖层按“含空格的矩形”定位 → 整条公式左移一个
   空格宽、右边多出一截，与前后文字的间隔就不对了。
2. **基准字号要用块内主字号夹一道**：只取“区间内最大 run 字号”时，区间恰好只盖住下标 run
   （全是 7pt）就按 7pt 渲染 → 公式明显偏小。夹到 `[0.85, 1.15]×` 块内主字号。
3. **字号要“一刀切”（不按宽度缩），宽度装不下就“让位”——不要压扁、不要缩字号。**
   KaTeX 字距比原文宽 10~40%，若按“装进原字形框”缩整条公式，同页公式就会大小不一：
   **不带上下标的几乎不缩**（看着偏大）、**带上下标的要缩 30%+**（看着偏小）。
   压扁（`transform: scaleX`）虽然能保字号，但字形走形，括号/分式一看就歪，也已弃用。
   现在的做法（`_inlineMathRoom()`）：**公式一律按基准字号渲染**，装不下就把空间从周围借出来——
   ① 横向：公式比原字形框宽出来的那点，在**公式后面**补一个 `.mspacer`（把同行后面的正文推开），
   而不是压扁公式；② 纵向：公式带上标/下标/分式时比一行高，把**上一行往上、下一行往下**
   各挪 1~6px（只动紧邻的两行，行距变化最小；块的译文卡跟着往下让）。

   - 让位上/下限按 `em` 算：横向最多推 `2.2em`，纵向最多挪 `0.45em`，小于 `2px` 的“重叠”
     是字体盒子的空隙、不值得动行距。
   - **必须幂等**：重排 + 选中/高亮/标注都会重建 `.ln`，所以每次都先按基准值
     （`ld.baseTop` / `box._x0`）复原再重算；横向用 spacer 元素（随 `.ln` 一起被清掉，不残留）。
   - **跨行公式不推**：Surya 的 `<math>` 区域可能横跨换行（实测 5 条），覆盖层是单行的，
     跟续行后面的正文没有可比性。
   - 同一行有多条公式时**从右往左**处理：往右推只影响它右边的东西，推完把右边那条的
     `left` 跟着挪同样的距离（行内有 `fitLineEl` 的 `scaleX`，spacer 宽度要除回去）。
   实测：55 条里只有 1 条（`SR=(R_p-R_f)/\sigma_p`）需要 27px 的让位，其余中位数 4px；
   让位后**没有一条**再压住后面的正文。纵向共 40 多处、每次 1~6px。
4. **垂直对齐要“渲染后实测”，不能靠字体度量推算**：KaTeX 盒子带 `line-height`，用
   “盒顶→基线”的比例去反推定位，那个比例受**宿主元素行高**影响 —— 在 `body` 里量出来的值
   换到 `.blk` 里就不准（实测公式整体下沉 **7~11px**）。正确做法（`finalizeInlineMath()`）：
   先把盒子放上去（初始位置取原字形矩形顶），**再量两个基线求差平移**：
   - 该行正文基线：`_lineBaseline(lnEl)` 往行首插一个 **0 尺寸 inline-block 探针**，
     它的底边正好落在行基线上（inline-block 无内容时基线=底边）；
   - KaTeX 基线：`_katexBaseline(box)` 取 `.katex-html .strut` 的底边（strut 就是 KaTeX
     用来给盒子定基线的，本身没有降部）。
   两处都必须在宽度适配 **之后**量 —— 缩放会改变行盒高度，基线跟着变。改完实测 55/55 误差 ≤ 1px。

5. **KaTeX 内部的 `1.21em` 要除回去（`KATEX_EM`）**：KaTeX 的 `.katex{font-size:1.21em}`
   是给“典型网页正文字体”配的；本项目的“正文字号”是 PDF 的 pt 换算值、正文又用 Georgia
   顶替原字体，两头折算下来要再压 `1/1.15`，公式字高才和正文字高对得上。
   实测（canvas 量 x-height/cap，正文字体 Georgia）：不压 → x-height **+13%**、cap **+18%**
   （一眼就能看出“公式的字比正文大一号”）；压完 → x-height **−2%**、cap **+3%**。
   顺带把宽度也缩回 15%——KaTeX 比原文宽的问题大半出在这个系数上（宽度比从 1.17~2.10
   降到 1.00~1.83）。行外公式同样除（它们本来就是按宽度缩到装下，除不除渲染结果一样，
   实测字号差 <0.02px，但两边口径一致更好维护）。

> 曾试过“按 x-height 把公式墨迹校准到与正文等高”（`_inkK`）：实测系数被正文实际落到的小 x-height
> 衬线字体拉到 **0.74**，公式明显变小，弃用。现在只除 KaTeX 自带的 `1.21em`，不额外按字体度量校准。

> ⚠️ **公式后面的正文突然变小**：PyMuPDF 会把上标标记**泄漏**到公式后面的 run 上
> （`…^{(i)}, r_t^{(h)})` 后面的 `" is the value vector of…"` 被标成 `up=true` + 7pt），
> 阅读器按 `s × 0.7` 渲染就成了 0.7 倍。`_rightAfterMath()` 判定“紧跟在公式之后”（含跨公式
> run 的尾部），对**写着像单词**的片段按块内主样式（`item.bodyRun`）重绘；真正的上标
> （`2`、`(i)`）不动。

| 解析后端 | 公式来源（解析时抽成 `latex`） |
| --- | --- |
| `surya` | 公式块标签是 `Equation`（`raw_label` 为 `Equation-Block`），其 `<math>` 里**装的就是 LaTeX 源码**，用 `latex_from_html()` 抽出 |
| `unlimited_ocr` | 正文里 `\(…\)` / `\[…\]` / `$$…$$`（少见单 `$…$`，保守判据）摘成字符区间；`equation` 块剥掉 `\[ … \]` 定界符 → `latex`；解析侧不猜、不注入 |
| `pymupdf` | 本地解析方案：PyMuPDF 字体/位置聚合公式框 → texify 识别成 LaTeX，写入 `pages[].formula_boxes`；正文里对应字形替换为占位空格 |

> ⚠️ Surya 的 `<math>` **不是 MathML** —— 例如
> `<math display="block">\ell_t(W)=\text{CE}(f(x_{t-1};W),x_t).\quad(1)</math>`。
> 如果把它当 MathML 直接塞进 DOM，`\ell_t(W)` 会被当成纯文本显示（这正是早期版本公式“渲染不出来”的原因）。

#### 字号与“比例”（这里最容易出问题）

1. **基准字号取块内最大的 run 字号，不是第一个 run。** 公式块里夹着下标/上标（7pt 之类的小号
   run），`IC_i=\frac{1}{T}\sum…` 的第一行就是求和上限的小号 `T`——按它渲染整条公式会明显偏小。
2. **再用本页正文字号夹一道**（`pageBodySize()`：该页非公式文字块里字数最多的那个 run 字号，
   来自 PyMuPDF 真实字体信息）。公式块自己的字号常是 OCR/合成行估出来的（同一页能给 8~17pt），
   不夹就容易忽大忽小。范围取 `[0.8×, 1.25×]` 正文号。
3. **按“公式主体宽度”等比缩放**（`fitMathBox()`）：KaTeX 的字距/字体与原文排版不同，**同一字号**
   下常比原框宽 20~30%，而公式块的外框就是 PDF 里这条公式的原始外框 → 不处理就会撑出框外。
   缩放下限 `0.6`；每次都从基准字号重算，反复调用不会越缩越小。
   缩放基准用 `formulaBodyWidth()`：**公式编号（贴栏右的 `(1)`）不算入主体宽度**，
   否则框被编号撑宽、带编号的公式永远轮不到缩小，看上去就比同页同类公式大一号。
4. **尾部编号交给 `\tag`**（`splitTailTag()`）：很多解析结果把编号直接写进 LaTeX（`…\qquad(1)`），
   直接渲染会让**内容**也宽出一截，与“主体宽度”口径不一致。摘出来交给 KaTeX 的 `\tag`：
   它贴右排放（`width:100%` 让覆盖层拉满块宽 → 编号正好落在原框右边缘），且不参与内容宽度。
   守卫：编号前必须有明确的空白命令（`\qquad`/`\quad`/`\hspace`/三连空格），免得把
   `f(x)=g(1)` 结尾的真括号误当编号。
5. 公式覆盖层**左对齐**（不是居中）：块的外框就是原文公式的框，居中会把它从原位挪走。
6. 适配后**不出滚动条**：`.mathbox` 用 `overflow: visible`（不裁也不滚），
   万一还超宽就让它溢到栏间空白里。

> 实现上还有三个坑，改代码时别踩回去：
> ① **必须等 `document.fonts` 里的 KaTeX 字体就绪再渲染** —— 字体没到位时量出来的宽度是后备
> 字体的，宽度适配会算错，等字体一换公式就撑出框外；
> ② 字体迟到时用 `document.fonts.ready` 再补量一次（`refitMathBoxes()`，幂等）；
> ③ 量内容宽度**不能直接读 `box.scrollWidth`**：`.mathbox` 是 shrink-to-fit，内容窄时它也窄；
> 而带 `\tag` 的盒子被拉成 `100%` 宽，`scrollWidth` 会返回盒子宽度而不是内容宽度
> → 先临时切成 `width: max-content` 再量。

#### 复制公式时给 LaTeX 源码，不是显示内容

公式块在 DOM 里有三份“内容”：隐藏的 PDF 原文字行（`.ln`）、KaTeX 的 MathML 注解、KaTeX 的
HTML 渲染结果。直接复制会把三份都带上（字形重复），而且拿到的不是源码。
`r2-math.js` 里接管了 `copy` 事件（`onCopyMath()`）：

- **整段选区都在一个公式块里** → 直接给解析出来的 `latex` 字段（**裸 LaTeX**，不带定界符）；
- **跨块选区**（正文 + 公式） → 克隆选区，剔除公式块里隐藏的 `.ln`，把公式元素换回源码，
  再自己序列化成文本；`.katex` 的源码就存在 `annotation[encoding="application/x-tex"]` 里，
  所以正文里内联的 `\(…\)` / `$$…$$` / `$…$` 也会一并还原成 `$…$`（行内不包定界符就分不清）；
- **没碰到公式** → 完全不插手，走浏览器默认复制（保持原有手感）。

阅读器选中公式块后那个「复制」按钮走同一套口径（`mathTexOf(item)` 优先于 `sentenceText()`），
不会再把 PDF 抽出来的字形串（可能是乱码）复制出去。

KaTeX 只在需要时**懒加载**（先判断文档里有没有 `formula_boxes`、`latex` 或带定界符的文本），已随项目内置在
`frontend/vendor/katex/`（v0.16.11，MIT，见同目录 `LICENSE`）：`katex.min.js` + `katex.min.css`
+ 20 个 woff2 字体（共 ~600KB），**运行时完全离线、不发任何网络请求**。

> 选用 KaTeX 而不是 MathJax 的原因：KaTeX 是**同步** API，构建 DOM 时当场就能渲完，没有加载竞态，
> 也不会出现“元素还没插进文档就排版 → 量不到尺寸 → `width="NaNex"`”那类问题；
> MathJax 的 SVG 输出还需要在元素挂载后异步补排版。

> 公式块仍会照常参与整页 / 全文翻译（送翻译的是块的原文字，不改变现有行为）。
> `latex` 是**新解析才有**的字段：库里的旧文档不会自动变，要在网页顶栏点「🔄 重解析」
> 选中同一份 PDF（或用 `python tools/reparse_layout.py <doc_id> 原PDF`），公式才会被渲染。
> 注意 `relink_images.py` **只补插图**、不刷新版式，别拿它当重解析用。

> 已删除：PaddleOCR-VL 的旧客户端与配对链路已随代码移除；`config.json` 里的 `paddle_*`
> 与 compose 里的 `PADDLE_OCR_*` 不再被读取（键若保留仅作历史记录）。
> `paddle_ocr_doc_parse_service/` 是独立的 OCR 服务，与本应用不再对接。

### 插图文字重影（去重影）

插图里本来就有的文字（Surya 裁剪图、矢量图栅格化图、扫描件/图片自带的 OCR 文字层），如果又按文字层叠一遍，就会与图里的字形错开、上下两层都对得上又都不重合，看起来就是**重影**。解析时会自动剔除这类文字行：

- 整行落在**页面渲染类插图**里（Surya 裁剪图/矢量图栅格化图都是该区块的像素快照，文字已烘焙进 PNG）；
- 整行落在**内嵌位图**里，且该行是**不可见文字**（渲染模式 3 / 透明度 0，即扫描件的 OCR 文字层）。

> ⚠️ **矢量图识别的三个坑（都踩过，别调回去）**：图要是没被识别成插图，
> 读者就只能看到散落的文字标签（图没了、字还在）。
> ① **线宽门槛不能高**：真图的线条往往很细 —— 实测一篇论文的线宽分布是
>    `{0.19:79, 0.27:1, 0.4:2, 0.54:31, 0.88:20}`，按 `≥0.8` 卡的话**真图几乎全被排除**
>    （只有 20 条 0.88 的能过），图就只能靠“白色大底板被误当成矢量图”兜底 —— 而那条路
>    会把底板罩住的正文当“图里的字”删掉。所以取 `≥0.15`。
> ② 识别前先**剔掉“背景型”大色块**（宽 > 半页且面积 > 页面 25% 的填充矩形）：这种矩形是
>    页面底色/装饰框，留着它会把周围零散的小图形（真图）**串成一大片“伪图”**
>    —— 实测一个 536×310 的浅色填充矩形让 **141 行正文**凭空消失。
> ③ **文字覆盖率阈值要分大小看**：小区域（≤3% 页面积）常常是“图里带框的文字/标签”，
>    文字占比本来就高（实测 39%~43%）→ 放宽到 **70%**；大区域才可能是“把正文圈进来”的
>    伪图（实测 31%~35%）→ 保持 **25%**。只看一个固定阈值的话，不是丢图就是删正文。
> ④ **“点阵底图 + 矢量叠加”的图要合成渲染**：坐标轴、折线、标注常常是**矢量**画在点阵
>    底图之上的。直接 `doc.extract_image()` 只拿到底图，会得到一张空背景 —— 实测一张红
>    折线图的底图是一块灰格纹，抽出来就只剩灰格纹、**红线和坐标轴全丢**。现在只要检测到
>    有矢量图形叠在该图的 bbox 上，就改按页面区域**栅格化**（`zoom` 按底图原生像素数取，
>    尽量不掉清晰度）；纯照片/整页扫描件仍走原来的“抽原图”，不损失分辨率也不额外开销。
> 实测（7 页真论文）：识别到的插图 9 → 13 张，被误删的**长正文行始终为 0**；
> 被删掉的行只是图内的刻度/标签（那些本来就烘培进插图 PNG 里了）。

Annualized return calculates the rate of return for a given holding period scaled down to a 12-month period: AR = exp n 365/T ′ × log (ST /S0) o −1,

为避免误删贴边的正文，还要求“行框被插图覆盖 ≥ `text_in_image_overlap`”且“行中心点也在插图内”。被删的行数会记在每页的 `ghost_text_removed` 与 `parser_info.ghost_text_removed` 里，便于核对。若你的 PDF 属于“图只是底图、文字确实浮在图上”的特殊情况，把 `drop_text_in_images` 设为 `false` 即可恢复旧行为。

启动 Surya 2 推理服务：

```bash
cd surya_doc_parse_service
docker compose up -d --build                 # CPU 版
# GPU 版: docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
curl http://127.0.0.1:8060/health            # 验证服务
```

安装客户端依赖（不装则 `surya` 后端直接报错，不会再回退本地 PyMuPDF）：

```bash
pip install -r requirements.txt
```

`requirements.txt` 里已经**先锁死 CPU 版 torch/torchvision，再装 surya-ocr**（顺序写在前面便于阅读，pip 靠下面的精确锁版本保证选到 CPU 轮子）：

```
--extra-index-url https://download.pytorch.org/whl/cpu
torch==2.14.0+cpu
torchvision==0.29.0+cpu
surya-ocr>=0.22
Pillow>=10
```

为什么不能直接 `pip install surya-ocr`：它会从 PyPI 拉**CUDA 版** torch（单轮子 554MB，还带一堆 `nvidia-*` 依赖，多占 2GB+）。关键是版本要带 **`+cpu` 本地标签**——PyPI 上没有 `2.14.0+cpu`，所以只有 CPU 索引的候选能命中，不会被上游更新的 CUDA 版顶掉。（`torch 2.14.0` ↔ `torchvision 0.29.0` 是官方配套版本，与服务端 `surya_doc_parse_service/` 装的一致；实测 `nvidia-*` 依赖数为 0。）

### 在 Docker 里用 Surya（两个条件必须同时满足）

`auto` 要**同时**满足两条才会用 Surya：**① 服务连得上；② 跑应用的那个进程里装了 `surya-ocr` + `Pillow`**（推理在服务端，但渲染页面、构造请求、解析 block 都在本地）。只满足一条就会**静默回退 PyMuPDF**——最容易被当成“配置没生效”。Docker 里默认两条都不满足，所以本项目的镜像是**默认带客户端 + 默认走 host-gateway** 的：

| 坑 | 原因 | 默认值 |
| --- | --- | --- |
| 连不上服务 | app 容器与 `surya-server` 不在同一网络；容器里的 `127.0.0.1` 是它自己 | `SURYA_INFERENCE_URL=http://host.docker.internal:8060/v1` + `extra_hosts: host.docker.internal:host-gateway` |
| 报“缺少 Surya 客户端依赖” | `surya-ocr`/`Pillow` 以前是注释掉的可选依赖，镜像不会装 | 已写进 `requirements.txt`（含 CPU 版 torch 锁版本），镜像构建时一并装好 |

```bash
cd docker && docker compose up -d --build
```

- 已把 surya 接进同一个 Docker 网络时，可用服务名直连（少绕一跳宿主机）：`SURYA_INFERENCE_URL=http://surya-server:8060/v1`。
- **Unlimited-OCR 同理**：app 容器默认用 `UNLIMITED_OCR_URL=http://host.docker.internal:9010/v1` 连宿主机上的 `unlimited_ocr_service`（宿主端口 9010）；接进同一网络则改成 `http://<服务名>:9010/v1`。它同样没有本机客户端依赖（只需 PyMuPDF，镜像里已有）。
- PaddleOCR-VL 的 `PADDLE_OCR_*` 环境变量保留在 compose 里，但对应的客户端与配对链路已随代码删除、不再被读取（键保留仅作历史记录）。
- 只想要 PyMuPDF 的瘦镜像（不要 torch，约省 1GB）：把 `requirements.txt` 里 torch/torchvision/`surya-ocr` 三行拆到单独的 `requirements-surya.txt`，镜像里只装核心依赖即可。
- **容器内没有 `config.json`**（`Dockerfile` 只 `COPY config.example.json`），`parser` 段靠上面的环境变量注入；想用文件配置就取消 `docker-compose.yml` 里 `../config.json:/app/config.json:ro` 那行注释（宿主机上该文件必须已存在，否则会被建成同名目录）。
- 排障入口：网页右上角「⚙ 设置 → 存储 / OCR」会显示**实际生效**的后端和原因；解析产物 `doc.json` 的 `warnings` 也会写明回退原因。

验证与排障：

```bash
python tools/selftest.py samples/sample.pdf --backend pymupdf         # 纯本地、不连服务
curl http://127.0.0.1:8000/api/config        # 查看解析后端/服务就绪状态
```

解析产物 `doc.json` 会记录实际使用的 `parser`、`parser_info`（服务地址/DPI/版面标签统计/去重影行数）与 `warnings`；每个文字块额外带 `label` / `kind`（text、heading、table、formula、caption、footnote…），表格与公式块的 `html` 也一并保留，便于后续做富文本渲染。插图统一命名为 `p{页}_i{序号}.png`，与 `/api/doc/{id}/img/{fname}` 路由一致。

## 使用提示

- 在阅读页**点击一句话**，即可选中该句并弹出工具条：`译本段` / `标注` / `笔记` / `复制`。
- 在正文上**按住左键拖拽**即可选多句（松手后自动吸附到整句），也可以 `Shift+点击` 从当前选区扩展；工具条上的操作会一次作用于所有选中句。
- `标注` 弹出调色盘：**6 个色块**＝黄/绿/粉 + 3 个自定义色（虚线描边的三个是自定义位，用下方「自定义」里的三个取色器改颜色，改完自动存到**当前账号**名下）；点击色块即用**半透明底色**高亮整句，当前句已用的颜色会加白圈标出；同色再点一次即取消，或点 `清除高亮`。句内的**公式框会跟着同色高亮**（与文字同款底色；暗夜下同款霓虹字色 + 外发光）—— 公式是页面级覆盖层，由 `reader2.js` 的 `paintFormulaMarks()` 按几何与高亮句匹配后上色。
- `笔记` 直接在页面右侧弹出卡片（与选中句子同高，不挤占正文），写完保存即可；卡片上带句子原文摘要，正文里同时有下划线标记。顶部“显示笔记”可一键收起全部笔记卡片。
- 顶部“📑 目录”打开**左侧目录栏**：建论文时就存下了 **PDF 自带书签**（零成本、最准）；PDF 没书签时，首次展开面板会自动让 **LLM 读全文排一份目录**（每页只喂“候选标题行”，省 token），结果存库、不再重复提取；点目录项直接跳页，滚动时自动高亮当前所在小节，底部有「重新提取」。展开目录时正文会整体右移并自动适宽，不会被盖住。
- 阅读页的 **AI 阅读与笔记**是随视口悬浮的卡片（也可从目录侧栏打开）：可以直接提问（上下文带全文、公式与笔记），也可以让它「把这个结论写进右侧整理卡片」；要求它定位论文页或某条笔记时会**先问你是否跳转**，确认后才动。进度 ≥ 95% 会自动整理一次，也可手动「重新整理」。
- 正文里的**引用标记**（`[12]`、`[3,5]`、`[10–12]`）可以**点击**：就地弹出参考文献卡片，`定位原文 ↧` 滚到参考文献那一条并闪烁，`复制` 复制条目原文；条目带 DOI/arXiv 链接时还能直接打开。优先使用 PDF 内部链接；没有时按编号回退匹配。一条引用（多个作者 / 多个编号）**整体标记**：悬停高亮、点击弹卡都是一整组。条目多时卡片**自己能滚**（滚轮、触控板、拖卡片里的滚动条都行，滚到两端也不会带着页面一起动），鼠标在卡片里操作时卡片**不会消失** —— 只有点卡片外的区域、或按 `Esc` 才收起。
- 顶部“翻译本页 / 翻译全文”用于批量离线翻译；配合“显示译文”开关查看双语；译文卡字号会随页面缩放一起变化。
- **换译文语言**：右上角「⚙ 设置 → LLM 服务 → 翻译目标语言」选完保存即生效，阅读页会自动清掉旧语言的译文并按新语言重新拉取；已翻过的语言切回去仍命中缓存。
- 底部居中的**页码可以点**：直接输入页号按 `回车` 跳到该页（`Esc` 取消）；页码超出范围会提示并留在原页。滚动时它会自动跟着更新。
- **首屏只加载前几页**：打开阅读页先只看得到的那几页（默认 3 页），其余页在后台按离当前位置最近的顺序补齐；页码/目录/位置还原从第一帧起就是全篇的。目录跳页、笔记定位、“翻译全文”会自动把需要的页拉下来。
- 选中**多个句子**后，工具条按钮变为 `翻译所选句子`：只翻译选中的句子（选区跨段落时按段落分片），结果显示为独立卡片，挂在**最后一个被选句子**下方（多句连选不会插在所选句中间）；不同选区的卡片**可以同时留着**，卡内 `清除` 或再点一次按钮收起。它与整段译文**互斥**（两者不同时显示，切换时另一边自动收起）。单选一句仍是原来的 `译本段`（整段翻译）。
- 无可用 LLM（没 Key 且没装 `deep-translator`）时，阅读页会提示“未配置翻译服务”，点右上角「⚙ 设置」填完即生效（不用重启）；`config.json` 仍支持 `deepseek_base_url` / `deepseek_model` 等自定义。
- 顶部「显示译文」是**带记忆**的开关，而且**记忆也跟账号走**（存在 `users.prefs.show_zh`）：勾一次以后，刷新/重新登录都会自动把已缓存的整段译文展开；“所选句译文”卡片与它是互斥的（有卡片时会自动取消勾选，卡片清掉后再勾即可）。

## 已知限制

- **登录面向单机自用**：账号之间**数据默认是隔离的**（论文/标注/笔记/译文/LLM 配置都只归上传者所有，共享前别人打不开），账号本身也**没有管理员/只读角色**（只读/可写是**论文级**的共享权限），没有找回密码与邮箱验证；会话 Cookie 未开 `Secure`（本地 http 直连），所以**别把这个服务直接暴露到公网**，需要外网访问请自己在前面加一层 HTTPS 反向代理与访问控制。
- **首个账号 = 管理员**：服务一旦初始化完成，`/api/auth/setup` 只会返回 409，不会被别人抢注；反之若你把服务端口开放给了别人，请先自己完成初始化。
- 前端对 API Key 输入框做了防自动填充处理（`autocomplete="new-password"` + “只有用户真输过才提交”）：浏览器自动填入的值不会被当成你的输入保存，想保存就必须手动输入（粘贴算输入）。
- **数据库是单机单副本**：没有副本集/分片，数据可靠性靠宿主机的 `paper-mongo-data` 卷（`mongo_configuration/README.md` 里有备份命令）；`records`/`notes` 这类数据量很小，`documents`（版式数据）每篇几百 KB～数 MB，单机完全够用。
- **账号之间数据隔离，但支持主动共享**：论文/标注/笔记/译文都挂在**上传者账号**下，别的账号看不到也改不了（接口一律 404）；所有者在首页点「🔗 共享」可把论文（连同笔记/高亮/译文/AI 整理与对话）共享给指定账号，权限只有**只读 / 可写**两种（没有管理员角色）；删论文与管理共享名单只有所有者能做，被共享者只能退出共享。另外**升级到隔离机制前的老论文**会被自动归给最早创建的那个账号（启动日志会有 “已把 N 篇历史论文归属给账号 X”）。
- **注销/删除账号是硬删**：账号与它名下的论文数据会直接从数据库和磁盘删除，**无法恢复、也没有回收站**；删别人要重输用户名 + 网址弹窗确认，注销自己还要再验证一次当前密码。想留后路就先备份（`mongo_configuration/README.md` 里的 `mongodump` + 打包 `data/docs/`）。
- **LLM 配置/Key 按账号隔离**：A 账号的 Key 只存在 A 的记录里；B 没配就落到环境变量/`config.json` 的**安装级默认**（不是 A 的 Key）。想彻底禁止共享，就把 `config.json` 里的 `deepseek_api_key` 删掉并让每个账号各自填。翻译只按“发起请求的那个账号”的配置计费/调用。
- **未登录也能自助注册**（默认开启，`ALLOW_SIGNUP=0` 关闭；另设 `MAX_ACCOUNTS`，默认 50 个上限）：本机自用方便，但**若要把服务暴露到公网，请设 `ALLOW_SIGNUP=0`**（改用“登录后在「账号」里新增”），并自行加 HTTPS 与访问控制。
- **MongoDB 不可用时会回退本地 JSON**（`auto` 模式）：这是可用性妥协，不是“双写”。想要数据只在库里，就设 `APP_STORAGE=mongo`；回退期间产生的数据留在 `data/*.json`，需要时用 `tools/migrate_to_mongo.py` 搬进去。
- 句子切分基于英文标点启发式(对 Fig./e.g./et al. 等常见缩写做了保护)，极少数学术句可能切分不准。
- **译文缓存按目标语言分开**：换目标语言后旧语言的译文不再显示（切回去仍在）；极短片段（纯公式/符号）可能被翻译服务原样返回。
- “所选句译文”按**段落内连续句片段**切片翻译：选区跨段落时会切成多片分则调用翻译、再合并展示（每片按文本哈希缓存，重复翻译不重复计费）；极短片段（纯公式/符号）可能被翻译服务原样返回。卡片自身（锚点+译文）会存到 `seltrans`，所以刷新/重新登录后会自动回来；点卡上的「清除」或切到“整段译文”模式会把它从服务端一并删掉。
- 译文内联显示采用“顶开版式”的近似布局：译文/笔记卡把正文顶开时，**页面级公式框覆盖层（`.fbox`）会跟着下方正文一起移动**，卡片收起/删除后也随正文一起还原（`layout.js` 的 `relayoutPage()` 每次重排都会重算位移，**没有卡片时也会重算以便清掉旧位移**）。位移分两步：① 每张卡把**同栏**（x 重叠）下方内容顶开；② **传递**——被顶开的宽元素（典型是跨栏插图）会把压在它正下方的内容（如插图下方的标题/页码）也一起带下去，页面高度随之自动增长；含复杂多栏混排时仍可能有轻微位置偏移。
- 译文卡与笔记卡统一用 `--page-scale = clamp(pageW / 900, 0.78, 1.2)` 字号基准：页面因窗口窄而自动适宽时，卡内文字同步缩小；如需完全等比（不设上下限），把 `layout.js` 里 `applyNoteChrome()` 的 `clamp(...)` 去掉即可。
- AI 悬浮面板的两个框高度由 CSS 变量 `--ai-panel-h` 控制（在 `style.css` 的 `.ai-end` 里）；内容再多也不会把框撑长，超出部分框内滚动。
- 左右结构时两个框宽度**恒为 2 : 3**（左对话 : 右整理）：`.ai-chat` / `.ai-sum` 的 `flex-basis`（300 / 450）与 `flex-grow`（2 / 3）按同一比例写、收缩也按 basis 等比，所以任何可用宽度下比例都不变（**不要给它们加 `max-width`**，那会打断比例）。换行阈值 = 两个 basis 之和（约 764px），再窄就由 JS 换成上下**等宽**结构。
- 「进行中」状态只作为一条 `#aiMsgs .ai-msg.pending` 气泡追加在对话流末尾（样式在 `style.css` 的 `.ai-msg.pending` / `@keyframes aiPulse`）；前端用 `aiBusyWhat`（`""` / `"summary"` / `"chat"`）区分忙碌类型，`renderAiSummary()` 不会因为忙碌而覆盖已有整理结果，并会保留 `scrollTop`。
- 流式回复用 `aiDraft`（`null` = 不在流式 / 字符串 = 已收到文本）描述：`renderAiChat()` 把 `pending` 气泡换成 `.streaming` 气泡，后续增量由 `pushAiDelta()` 重画那一个气泡的 `.txt`（Markdown 整体重渲，节流到每帧一次）；`.streaming .txt::after` 是个闪烁的 `▍` 光标。流到一半失败时**保留已收到的文字**（不把字收回去），一个字都没收到才把提问退回输入框。
- **AI 文字按 Markdown 渲染**：渲染器是前端自带的 `r2-md.js`（`window.R2Md`，不引第三方库、无构建步骤）：标题 / 无序·有序列表（可嵌套）/ 粗体·斜体·删除线 / 行内代码 / 围栏代码块（角标显示语言名）/ 引用 / 分隔线 / 链接 / 表格 / 任务框（另 `R2Md.inline()` 供主题标题这种单行文字用）。安全上**先转义再组装**，链接只放行 http / https / mailto / 站内相对地址，渲染器出意外时退回纯文本。对话气泡（AI 侧）与整理里的总览 / 要点 / 关联 / 疑问 / 下一步都过它；**提问保持原样显示**（pre-wrap）。文字里带 `$…$` / `\(…\)` / `$$…$$` 时，由 `r2-ai.js` 再调 r2-math 的 `renderInlineMath()` 就地换 KaTeX（`ensureKatex()` 懒加载，加载完重画面板；`.md code / .md pre` 里的定界符不转换，代码块保持原样）。
- 高亮自定义色、「显示译文」开关、译文目标语言、解析后端都是**账号级偏好**（存在数据库的 `users.prefs` 里，与账号密码同一份记录；不回退到浏览器本地）：同一账号在任何浏览器/设备上都是这套设置；文档里存的是**色位**（`c1`/`c2`/`c3`），所以改了某个自定义色位，之前用该色位的高亮会一起变色（想单次固定颜色就先设好再用）。
- **改配色请优先改 CSS 变量**（`style.css` 顶部 `:root` 与 `html.dark` 两份语义色板：`--bg / --card / --ink / --mut / --line / --accent-soft / --bg-soft / --bg-chip / --page-bg / --ti-*`（译文卡）/ `--nt-*`（笔记卡）/ `--warn-* / --err-* / --ok-* / --tag-*` …）。组件里再写死浅色就会在暗夜模式下“一块白”。正文颜色与荧光高亮是 inline 样式，切主题时会通过 `window` 的 `themechange` 事件重算（见 `core/r2-color.js` 的 `pageInk()` / `reader2.js` 的 `hlPaint()`）。
- 「AI 辅助阅读」的图标是**自家 sparkle SVG**（不用 emoji）：`style.css` 里的 `--ai-mark-svg` 只当遮罩（`mask-image`），填色走渐变 `--ai-1 → --ai-2`，所以换色/换主题只改这两个变量；用量处写 `<i class="ai-mark"></i>`（悬浮 AI 面板、目录侧栏、整理空白态）。暗色下额外加了 `drop-shadow` 外发光。
- 笔记卡默认贴在页面右侧、纵向对齐句子首行。有笔记时会预留批注栏宽度（`--note-reserve`，按窗口宽度在 210–330px 间自适应）：放得下就保持页面原尺寸，放不下则**自动适宽**（页面等比缩小，顶部比例标签同步显示实际比例，悬停有提示）；可用宽 < 520px 或右侧抽屉展开时改为句子下方内嵌。同页多张卡自上而下依次避让（间距 10px）。
- Surya 后端每页一次 VLM 调用，速度取决于服务端算力；扫描件(无文本层)的行框按块高近似还原，句子级高亮的定位可能略有偏差。
- 去重影会把“已烘焙进插图”的文字行整行删除：观感不再叠字，但这些行也**不再可选/可翻译**（扫描件尤其明显）。旧文档需要重新上传转换才会生效。
- **引用跳转**优先使用 PDF 内部链接；没有内部链接时，编号式方括号引用按参考文献编号回退匹配，作者-年份引用（`[Smith et al., 2020]` / `(Smith et al., 2020)`）按“年份 + 姓氏/实词”回退匹配（校验从严，宁缺勿错）。纯上标数字式引用（`…text¹`）识别不了；无标签又无编号的作者-年份条目依赖参考文献区的悬挂缩进分条，整区都没有缩进信息时可能只能落到整块。
- **正文里引用/交叉引用带链接色（蓝）是原文档的样式**：`\cite`/`\ref` 被 hyperref 染色时，只染引用那几个字（解析侧按颜色切段保留该边界）。所以看到蓝字不等于“引用标记标多了”——`.cite` 的高亮底与蓝字是两回事，悬停/点击时以底色和弹卡为准。旧文档需要重新解析才会按颜色切段。
