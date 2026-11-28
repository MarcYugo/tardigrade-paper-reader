# Tardigrade Paper Reader - Pdf 转 HTML 阅读应用

**简体中文** | [English](./README_en.md)

> AI 辅助工具为 Deepseek-v4-flash/Deepseek-v4.1-flash/GPT-6-Luna + VScode copilot

> 自用，需要者自取

## 更新

```mermaid
%%{init: {
  "theme": "base",
  "themeVariables": {
    "fontFamily": "Segoe UI, Microsoft YaHei, PingFang SC, sans-serif",
    "fontSize": "14px",
    "primaryColor": "#eef4fa",
    "primaryTextColor": "#26333f",
    "primaryBorderColor": "#d3e0ee",
    "lineColor": "#c9d8e8",
    "textColor": "#26333f",
    "cScale0": "#e5eef8",
    "cScale1": "#f7eee7",
    "cScaleLabel0": "#26333f",
    "cScaleLabel1": "#3b302a"
  },
  "themeCSS": ".timeline text { fill: #26333f !important; font-size: 14px !important; font-weight: 500 !important; line-height: 1.5 !important; } .timeline .section-title, .timeline text.sectionTitle, .timeline .section { fill: currentColor !important; opacity: 0.65 !important; font-weight: 600 !important; } .timeline-node circle { fill: #eef4fa !important; stroke: #bcd1e6 !important; } .timeline-node line, .timeline .lineWrapper line { stroke: currentColor !important; opacity: 0.25 !important; } .timeline-title, .timeline text.timeline-title, text.timeline-title, .chartTitleText, .flowchartTitleText, text.title { fill: currentColor !important; font-size: 17px !important; font-weight: 600 !important; }"
}}%%
timeline
    title Tardigrade Paper Reader 更新时间表
    section 2026-10
        10/8 : surya 与 pymupdf 解析分离
             : 解析与渲染路径独立
             : 修复公式 / 图片重影
             : 增加表格渲染
             : 支持多种翻译语言
             : AI 辅助阅读支持 Markdown
        10/11 : 新增 Unlimited-OCR 解析路径
              : 表格样式改为三线表
        10/16 : 本地 / surya / OCR 三条路径
              : 均支持参考文献链接显示
        10/30 : 新增本地解析方案
              : pymupdf + OXR + texify + SLANet
              : 适合 LaTeX 编译的 PDF
              : 参考文献卡片持久显示
              : 支持作者-年份引用
              : 翻译卡片支持公式
    section 2026-11
        11/7 : Unlimited-OCR 支持官网 API
             : 新增 AI 辅助阅读 Agent
             : 支持内容 / 笔记定位
             : 支持辅助写入笔记
             : 更换水熊虫风格 Logo
        11/9 : Agent 功能拆分
             : 新增笔记导出功能
             : 支持参考文献校验
             : AI 辅助阅读改为胶囊卡片
        11/12 : 前端页面与元素排布调整
              : 缩放改为 5% 间隔
              : 支持滑轮 / 拖动缩放
        11/20 : 学术检索迁移至导出界面
              : 新增笔记图片导出
              : 机械导出 + Agent 导出
              : 缩放过渡动画
        11/28 : Agent 通信模块
```

<details>
<summary>📋 查看详细更新说明（点击展开）</summary>

1. [26/10/8] surya 解析与pymupdf解析分离，解析pdf和渲染路径独立，解决了surya解析时公式和图片重影问题，增加表格渲染
   
   <center>
    <img src="./function_display/formulas.png" width=400px><img src="./function_display/tables.png" width=400px>
   </center>

2. [26/10/8] 增加了多种翻译目标语言
   
   <center>
   <img src="./function_display/translate.png" width=400px>
   </center>

3. [26/10/8] ai辅助阅读增加 markdown 样式渲染。
4. [26/10/11] 增加 Unlimited-OCR 解析和渲染路径，调整表格渲染样式为三线表。
   
   <center><img src="./function_display/table-style.png" width=400px></center>

5. [26/10/16] 为本地pymupdf解析/surya解析/unlimited-ocr解析增加了参考文献链接显示功能，避免查找繁琐。unlimited-ocr解析不稳定（unlimited-ocr在26/10/14更新后好像没有之前好用了）。
   
   <center><img src="./function_display/citation2.png" width=400px></center>
   <center><img src="./function_display/citation3.png" width=400px></center>

6. [26/10/30] a. 增加本地解析方案，使用pymupdf+OXR的版面分析模型+量化后的texify+SLANet，对latex代码编译的pdf有效；b. 优化参考文献显示功能，信息卡片持久显示和作者-年份引用显示效果；c. 翻译功能，翻译卡片可显示公式。
   
   <center><img src="./function_display/translate-formula2.png" width=400px></center>

7. [26/11/7] a. Unlimited-OCR增加官网api调用；b. 增加ai辅助阅读时的agent功能，可以通过聊天找到论文相关内容/笔记位置 和 帮助写入笔记内容；c. 更换logo（水熊虫三角立体艺术拼接风格）

8. [26/11/9] a. agent功能拆分 b. 增加笔记导出功能（agent分析总结；校验参考文献需要到设置里配置检索api key） c. ai辅助阅读形式更改为底部胶囊卡片（打开收起方式详见功能5）。 

9. [26/11/12] 前端页面显示和元素排布调整，阅读界面缩放改为5%间隔的滑轮缩放/拖动缩放。

10. [26/11/20] a. 学术检索界面迁移到导出界面中，agent合并转为一个agent的skills; b. 增加笔记图片导出功能（机械导出和Agent导出），示例见功能6; c. 增加缩放过度动画，包括论文页/设置和笔记缩进等动作;

11. [26/11/28] 增加 Agent 通信模块。

</details>


## 架构设计
架构设计详见 [paper-trans-to-html/README.md](./paper-trans-to-html/README.md)。

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'Inter, -apple-system, Segoe UI, PingFang SC, Microsoft YaHei, sans-serif','fontSize':'13px','lineColor':'#8fa1b3','textColor':'#243447','primaryColor':'#ffffff','primaryTextColor':'#243447','primaryBorderColor':'#cfd8e3','edgeLabelBackground':'#ffffff','clusterBkg':'#ffffff','clusterBorder':'#d8e0ea'},'flowchart':{'curve':'basis','nodeSpacing':30,'rankSpacing':44,'padding':12}}}%%
flowchart TB
    subgraph FE["① 表示层 · 浏览器 —— 原生 HTML / CSS / JS · 无框架 · 无构建"]
        FE1["登录 · 注册<br/>login.html / login.js"]
        FE2["论文列表 · 共享<br/>index.html / home.js"]
        FE3["阅读器装配<br/>reader.html / reader2.js"]
        FE4["版式渲染 · 三后端<br/>core / items"]
        FE5["切句缩放 · 选区翻译<br/>select / bar / translate"]
        FE6["笔记 · AI · 引用 · 目录<br/>r2-notes / r2-ai / r2-refs / katex"]
    end

    subgraph AG["⑤ Agent 运行时 · backend/agents"]
        AG1["运行时 agent.py<br/>工具循环 · 历史卫生"]
        AG2["LLM 接入 llm.py<br/>流式 · tool_calls · 三套配置"]
        AG3["技能 · 辅助阅读<br/>写整理卡片 · 定位页 / 笔记"]
        AG4["技能 · 笔记导出<br/>核验文献 · 生成报告"]
        AG5["技能 · 笔记图片<br/>生成绘图代码"]
        AG6["技能 · 通信桥<br/>transfer key · 消费 / 回包"]
        AG1 ~~~ AG2 ~~~ AG3 ~~~ AG4 ~~~ AG5 ~~~ AG6
    end

    subgraph CORE[" "]
        subgraph CHAIN["⑥ 解析链 · 三后端互不牵连"]
            direction LR
            P1["本地方案<br/>pymupdf + OXR + texify + SLANet"]
            P2["Surya 2<br/>推理服务 :8060"]
            P3["Unlimited-OCR<br/>:9010 / 官网 API"]
        end
        subgraph CAP["③ 能力层 · 领域模块"]
            C1["解析门面 pdf_parser<br/>统一契约 parse_pdf"]
            C2["文档装配 converter<br/>documents + 插图"]
            C3["引用映射 pdf_refs<br/>三套策略 · 纯本地"]
            C4["翻译 translate<br/>批量 · 缓存 · 兜底"]
            C5["AI 阅读 ai_read<br/>摘要 · 流式对话"]
            C6["笔记导出 notes_out<br/>数据 · 图片 · 文献核验"]
            C1 ~~~ C2 ~~~ C3 ~~~ C4 ~~~ C5 ~~~ C6
        end
        subgraph APP["② 应用层 · FastAPI 单体 app.py"]
            A1["HTTP API + 静态托管<br/>AuthGate 门禁 · 302 / 401"]
            A2["账号 / 会话 / 偏好<br/>auth.py · settings.py · PBKDF2"]
            A3["接口分组<br/>docs · anno · translate · ai · share"]
            A1 ~~~ A2 ~~~ A3
        end
        subgraph STOR["④ 存储层 · storage / mongo_store"]
            S1["存储装配<br/>APP_STORAGE auto / mongo / json"]
            S2["Mongo · GridFS<br/>集合读写 · 插图存取"]
            S3["本地 JSON 回退<br/>data/ · 热切换"]
            S1 ~~~ S2
            S1 ~~~ S3
        end
    end

    subgraph INFRA["⑦ 外部服务 · 沙盒 / 通信模块 / 数据库"]
        I4["LLM 服务<br/>DeepSeek · OpenAI · Ollama"]
        I1["draw-sandbox :8090<br/>绘图沙盒"]
        I2["agent_comm_service<br/>Redis Streams · 通信网关"]
        I5["学术检索<br/>OpenAlex · Semantic Scholar"]
        I3[("MongoDB · GridFS<br/>九集合 + 插图本体")]
    end

    %% ── 表示层 → 应用层 ──
    FE1 ==>|"HTTP"| A1
    FE2 ==>|"fetch"| A1
    FE3 ==>|"fetch / SSE"| A1

    %% ── 应用层内部 ──
    A1 ==>|"鉴权 / 偏好"| A2
    A2 ==>|"路由分发"| A3

    %% ── 应用层 → 能力层 ──
    A1 ==>|"parse_pdf"| C1
    A3 -->|"引用映射"| C3
    A3 -->|"批量翻译"| C4
    A3 -->|"摘要 / 对话"| C5
    A3 -->|"笔记导出"| C6

    %% ── 应用层 → 存储层 / Agent ──
    A3 ==>|"会话 / 偏好读写"| S1
    A1 ==>|"SSE / 报告"| AG1

    %% ── 能力层内部 → 存储层 ──
    C1 ==>|"版式 JSON"| C2
    C2 -->|"documents / 插图"| S2
    S1 ==>|"读写"| S2
    S1 -.->|"自动回退"| S3

    %% ── 解析链 → 能力层（A3 ~~~ P2 仅作布局约束：把解析链整体压在能力层上方；
    %%    边的声明顺序决定链内左右次序：本地方案 / Surya 2 / Unlimited-OCR） ──
    A3 ~~~ P2
    P3 ==> C1
    P2 ==> C1
    P1 ==>|"按账号切换"| C1

    %% ── Agent 内部 ──
    AG1 ==>|"流式 / tool_calls"| AG2
    AG1 ==> AG3
    AG1 ==> AG4
    AG1 ==> AG5
    AG1 ==> AG6

    %% ── Agent 技能 → 能力层（复用） ──
    AG3 -.->|"复用 ai_read"| C5
    AG4 -.->|"复用 notes_out"| C6

    %% ── 外部服务 ──
    AG2 -.->|"chat"| I4
    AG5 -.->|"绘图代码"| I1
    AG6 -.->|"消费 / 回包"| I2
    C6 -.->|"文献核验"| I5
    S2 ==>|"落库"| I3

    %% ── 布局骨架（不可见） ──
    AG6 ~~~ I4
    C6 ~~~ I1
    C6 ~~~ I2
    C6 ~~~ I3

    classDef feN fill:#f7f9fc,stroke:#c3cfdd,stroke-width:1.2px,color:#243447
    classDef agN fill:#faf8f5,stroke:#d6c9b8,stroke-width:1.2px,color:#3d3428
    classDef capN fill:#f4f7fa,stroke:#a8bccf,stroke-width:1.2px,color:#1f3242
    classDef appN fill:#f5f7f9,stroke:#9db0c0,stroke-width:1.2px,color:#22303c
    classDef stoN fill:#f2f5f4,stroke:#9fb5ab,stroke-width:1.2px,color:#22382f
    classDef chN fill:#f3f7f4,stroke:#a3bfa8,stroke-width:1.2px,color:#26382c
    classDef infN fill:#f4f3f1,stroke:#b8b0a4,stroke-width:1.2px,color:#33302a
    classDef dbN fill:#2e3a45,stroke:#2e3a45,stroke-width:1.2px,color:#eef2f6

    class FE1,FE2,FE3,FE4,FE5,FE6 feN;
    class AG1,AG2,AG3,AG4,AG5,AG6 agN;
    class C1,C2,C3,C4,C5,C6 capN;
    class A1,A2,A3 appN;
    class S1,S2,S3 stoN;
    class P1,P2,P3 chN;
    class I4,I1,I2,I5 infN;
    class I3 dbN;

    style FE fill:#fbfcfe,stroke:#dfe6ee,stroke-width:1.4px,color:#33475b
    style AG fill:#fbfaf8,stroke:#e6ddcf,stroke-width:1.4px,color:#4a4034
    style CORE fill:#ffffff,stroke:#d8e0ea,stroke-width:1.4px,color:#33475b
    style CAP fill:#f8fafc,stroke:#c6d3e0,stroke-width:1.2px,color:#33475b
    style APP fill:#f8f9fb,stroke:#c9d2dd,stroke-width:1.2px,color:#33475b
    style STOR fill:#f7faf8,stroke:#c4d6cd,stroke-width:1.2px,color:#33475b
    style CHAIN fill:#f8fbf8,stroke:#cfe0d2,stroke-width:1.4px,color:#33475b
    style INFRA fill:#faf9f7,stroke:#ddd6cb,stroke-width:1.4px,color:#4a4034
```

## Pdf 解析模块

把论文 PDF 解析成**可还原原版式的数据**（文字块 + 插图，保留原坐标、字号、字体）。提供三种解析方式，输出结构一致，前端无需改动。

<table>
	<thead>
		<tr>
			<th align="center" width="18%">解析方式</th>
			<th align="center" width="24%">依赖与部署</th>
			<th align="center">核心能力</th>
			<th align="center" width="26%">适用场景 / 备注</th>
		</tr>
	</thead>
	<tbody>
		<tr>
			<td align="center"><strong>🖥️ 本地解析</strong><br><sub>pymupdf + OXR + texify + SLANet</sub></td>
			<td>不依赖显卡和外部服务，装好即用<br><sub>模型需预先下载，见 <a href="./paper-trans-to-html/backend/pdf_parser/local_parse_pipeline/README.md">local_parse_pipeline/README.md</a></sub></td>
			<td>
				<ul>
					<li>文字按 PDF 原始行框、字号、字体、颜色还原</li>
					<li>自动提取插图（内嵌图片与矢量图表），过滤整页背景、过小贴图</li>
					<li>独立公式与行内公式靠字体识别 + texify 转换</li>
				</ul>
			</td>
			<td>其他方式不可用时的<strong>兜底</strong>；解析 LaTeX 编译生成的 PDF 效果最好</td>
		</tr>
		<tr>
			<td align="center"><strong>📄 Surya</strong><br><sub>Surya 2 推理服务</sub></td>
			<td>接入服务端版面分析与整页识别<br><sub>部署方式见 <code>services/surya_doc_parse_service</code></sub></td>
			<td>
				<ul>
					<li>按版面分块还原阅读顺序，插图自动裁剪保存</li>
					<li>表格保留结构，公式识别为 LaTeX 并渲染成数学公式</li>
					<li>电子版论文以原始精确版式为准，扫描件用识别结果还原</li>
				</ul>
			</td>
			<td>电子版与扫描件通用，版面结构还原能力强</td>
		</tr>
		<tr>
			<td align="center"><strong>🔍 Unlimited-OCR</strong><br><sub>Unlimited-OCR 推理服务 / 官网 API</sub></td>
			<td>本地服务部署见 <code>services/unlimited_ocr_service</code>；本地部署不存在时自动切换官网 API<br><sub>API 需在百度智能云平台创建应用，配置 <code>config.json</code> 中 <code>unlimited_ocr_online_api_key</code> 和 <code>unlimited_ocr_online_secret_key</code></sub></td>
			<td>
				<ul>
					<li>Unlimited-OCR 模型做版面分析、文本和图表的检测与识别</li>
					<li>逐页识别，覆盖文本、图片和表格</li>
				</ul>
			</td>
			<td>扫描件 OCR 识别；支持 Unlimited-OCR 2 官网 API 调用</td>
		</tr>
	</tbody>
</table>

## MongoDB 数据存储

论文数据全部存放在 MongoDB（部署方式见 `services/mongo_configuration/`）。默认只监听本机，只有本机上的应用能连，更适合个人使用。

存放内容：

- **论文** —— 元数据、页数、阅读进度与状态，以及论文归属的账号；
- **版式数据与插图** —— 解析结果与论文插图，重启、重建容器、换机器都不会丢；
- **高亮 / 笔记 / 译文 / 参考文献检查** —— 标注与译文缓存，相同句子不重复翻译；
- **账号与会话** —— 登录账号（口令加盐哈希存储）、登录会话（过期自动清理）、每个账号各自的偏好与 LLM 配置。

数据按账号隔离：每个账号只能看到并操作自己上传的论文。

数据库没启动也能正常用：应用会自动回退到本地文件存储并在页面上提示，点「重试连接」即可切回数据库，不用重启服务。

## Agent 通信模块
使用Redis数据库存储消息和传递的数据，使用redis streams传递消息，外部agent通过持有transfer key通过通信应用发消息给 tardigrade reader agent 调取对应账户内的论文内容和笔记。
通信模块必须在已部署 MongoDB 数据库（`services/mongo_configuration`）时使用（需要存放 transfer key）。
部署见`services/agent_comm_service`，模块设计和该模块的压力测试详见 [agent-comm-service/README.md](./services/agent_comm_service/README.md)。

使用其他agent产品与tardigrade 内置agent通信时需要配置skill，示例见 `opencode-sample`。

<table>
    <tr>
        <td align="center"><img src="./function_display/agent_comm_export.png" width=400px></td>
        <td align="center"><img src="./function_display/agent_comm.png" width=400px></td>
    </tr>
</table>

## LLM 设置
LLM 设置预设了OpenAI、Anthoripic、deepseek和本地模式可供选择，实现上来说没有固定base url，可以任意选择，能够保存的设置只有3套。

## 使用功能

### 1. 多用户和设置

<table>
	<tr>
		<td align="center"><img src="./function_display/1.log-in-switch-out.gif" width=400px></td>
		<td align="center"><img src="./function_display/2.config.gif" width=400px></td>
	</tr>
</table>

### 3. 论文载入和删除、标注、笔记和翻译

<table>
	<tr>
		<td align="center"><img src="./function_display/3.paper-load-del.gif" width=400px></td>
		<td align="center"><img src="./function_display/4.highlight-mark-translate.gif" width=400px></td>
	</tr>
</table>

### 4. 笔记整理和AI辅助、笔记共享

<table>
	<tr>
		<td align="center"><img src="./function_display/5.note-ai-assitant.gif" width=400px></td>
		<td align="center"><img src="./function_display/6.note-share.gif" width=400px></td>
	</tr>
</table>

### 5. 笔记和内容定位、笔记添加

<table>
	<tr>
		<td align="center"><img src="./function_display/7.agent-location.gif" width=400px></td>
		<td align="center"><img src="./function_display/8.agent-write-notes.gif" width=400px></td>
	</tr>
</table>

### 6. 笔记导出功能（图片导出示例）

Agent 导出笔记图片需要启动沙盒容器 `services/draw_sandbox_service`

<table>
	<tr>
		<td align="center"><img src="./function_display/note-out.png" height=240px></td>
		<td align="center"><img src="./function_display/note-out-agent.png" height=240px></td>
		<td align="center"><img src="./function_display/note-out-mecha.png" height=240px></td>
	</tr>
</table>

## TODO

- [x] ~~公式解析优化，重影缺失等问题~~
- [x] ~~图片显示，子图caption重影缺失等问题~~
- [x] ~~pymupdf 本地解析优化~~
- [x] ~~unlimited ocr 解析和渲染路径实现~~
- [x] ~~引用链接显示功能优化~~
- [x] ~~unlimited-ocr解析后端优化~~
- [x] ~~笔记导出功能~~
- [x] ~~Agent通信模块设计~~
- [ ] Agent功能优化
- [ ] 应用内部代码优化
- [ ] 笔记导入功能

<!-- https://github.com/MarcYugo/tardigrade-paper-reader -->