# Tardigrade Paper Reader - PDF to HTML Reading App

[简体中文](./README.md) | **English**

> AI-assisted tools: Deepseek-v4-flash / Deepseek-v4.1-flash / GPT-6-Luna + VScode copilot

## Updates

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
    title Tardigrade Paper Reader Timeline
    section 2026-10
        10/8 : Separate surya and pymupdf parsing
             : Independent parsing and rendering paths
             : Fix formula / image ghosting
             : Add table rendering
             : Support multiple translation languages
             : Markdown rendering in AI-assisted reading
        10/11 : Add Unlimited-OCR parsing path
              : Change table style to three-line tables
        10/16 : Three paths: local / surya / OCR
              : All support reference link display
        10/30 : Add local parsing solution
              : pymupdf + OXR + texify + SLANet
              : Best for LaTeX-compiled PDFs
              : Persistent reference cards
              : Support author-year citations
              : Formula support in translation cards
    section 2026-11
        11/7 : Unlimited-OCR supports official API
             : Add AI-assisted reading Agent
             : Support content / note locating
             : Support assisted note writing
             : New tardigrade-style logo
        11/9 : Split Agent features
             : Add note export feature
             : Support reference validation
             : AI-assisted reading becomes capsule card
        11/12 : Frontend layout and element adjustments
              : Zoom step changed to 5%
              : Support wheel / drag zoom
        11/20 : Academic search moved to export view
              : Add note image export
              : Mechanical export + Agent export
              : Zoom transition animations
```

1. [26/10/8] Separated surya parsing from pymupdf parsing. The PDF parsing and rendering paths are now independent, which fixes formula and image ghosting in surya parsing, and adds table rendering.

   <center>
    <img src="./function_display/formulas.png" width=400px><img src="./function_display/tables.png" width=400px>
   </center>

2. [26/10/8] Added multiple target languages for translation.

   <center>
   <img src="./function_display/translate.png" width=400px>
   </center>

3. [26/10/8] Added Markdown-style rendering to AI-assisted reading.
4. [26/10/11] Added the Unlimited-OCR parsing and rendering path, and changed the table rendering style to three-line tables.

   <center><img src="./function_display/table-style.png" width=400px></center>

5. [26/10/16] Added reference link display for local pymupdf parsing / surya parsing / unlimited-ocr parsing, so references no longer need to be looked up manually. unlimited-ocr parsing is unstable (it seems less reliable after the 26/10/14 update).

   <center><img src="./function_display/citation2.png" width=400px></center>
   <center><img src="./function_display/citation3.png" width=400px></center>

6. [26/10/30] a. Added a local parsing solution using pymupdf + OXR layout analysis model + quantized texify + SLANet, which works well for PDFs compiled from LaTeX; b. Improved reference display: persistent info cards and author-year citation display; c. Translation cards can now display formulas.

   <center><img src="./function_display/translate-formula2.png" width=400px></center>

7. [26/11/7] a. Unlimited-OCR now supports calls to the official API; b. Added an Agent for AI-assisted reading that can locate related paper content / notes through chat and help write note content; c. New logo (tardigrade triangular 3D art collage style).

8. [26/11/9] a. Split Agent features; b. Added note export (Agent analysis and summary; validating references requires configuring a search API key in settings); c. AI-assisted reading is now shown as a bottom capsule card (see feature 5 for how to open and collapse it).

9. [26/11/12] Adjusted frontend page display and element layout. Reading view zoom now uses 5% steps with wheel zoom / drag zoom.

10. [26/11/20] a. Moved the academic search view into the export view, and merged the agents into skills of a single agent; b. Added note image export (mechanical export and Agent export), see feature 6 for examples; c. Added zoom transition animations, covering actions such as paper pages / settings and note indentation;


## PDF Parsing Module

Parses paper PDFs into **data that can reproduce the original layout** (text blocks + figures, preserving original coordinates, font sizes, and fonts). Three parsing methods are provided; their output structure is identical, so the frontend needs no changes.

### 1. Local Parsing Solution

Does not depend on a GPU or external services and works right after installation. It is also the **fallback** when other methods are unavailable, and gives the best results for PDFs compiled from LaTeX.

- Text is restored according to the original PDF line boxes, font sizes, fonts, and colors;
- Figures are extracted automatically (embedded images and vector charts), filtering out full-page backgrounds and overly small images;
- Math formulas (display and inline) are extracted via font recognition + texify conversion.
- Requires downloading before use; see [local_parse_pipeline/README.md](./paper-trans-to-html/backend/pdf_parser/local_parse_pipeline/README.md) in that module for details.

### 2. Surya

Connects to the **Surya 2 inference service**, which performs layout analysis and full-page recognition on the server side (see `services/surya_doc_parse_service` for deployment).

- Restores reading order by layout blocks, and figures are cropped and saved automatically;
- Tables keep their structure, and formulas are recognized as LaTeX and rendered as math formulas;
- Digital papers still follow the original exact layout, while scanned documents are restored from recognition results.

### 3. Unlimited-OCR

Connects to the **Unlimited-OCR inference service**. The Unlimited-OCR model performs layout analysis, text detection and recognition, and figure/table detection and recognition (see `services/unlimited_ocr_service` for deployment). It also supports calling the official API of Unlimited-OCR 2, which requires creating an app on the Baidu AI Cloud platform. When no local deployment is detected, the program automatically switches to API mode.

- Recognition page by page, covering text, images, and tables
- Requires configuring `unlimited_ocr_online_api_key` and `unlimited_ocr_online_secret_key` in `config.json`

## MongoDB Data Storage

All paper data is stored in MongoDB (see `services/mongo_configuration/` for deployment, one-click startup with Docker Compose). By default it only listens on localhost, so only apps on this machine can connect, which is more suitable for personal use.

Stored content:

- **Papers** — metadata, page count, reading progress and status, and the account each paper belongs to;
- **Layout data and figures** — parsing results and paper figures; they are not lost after restarting, rebuilding containers, or switching machines;
- **Highlights / Notes / Translations** — annotations and translation cache, so the same sentence is not translated twice;
- **Accounts and sessions** — login accounts (passwords stored as salted hashes), login sessions (expired ones are cleaned up automatically), and each account's own preferences and LLM settings.

Data is isolated by account: each account can only see and operate its own uploaded papers.

The app still works when the database is not started: it automatically falls back to local file storage and shows a notice on the page. Click "Retry connection" to switch back to the database without restarting the service.

## LLM Settings
The LLM settings preset OpenAI, Anthoripic, deepseek, and a local mode to choose from. In practice there is no fixed base URL and any can be chosen. Only up to 3 settings can be saved.

## Features

### 1. Multi-user and settings

<img src="./function_display/1.log-in-switch-out.gif" width=400px><img src="./function_display/2.config.gif" width=400px>

### 3. Paper loading and deletion, annotation, notes, and translation

<img src="./function_display/3.paper-load-del.gif" width=400px><img src="./function_display/4.highlight-mark-translate.gif" width=400px>

### 4. Note organization and AI assistance, note sharing

<img src="./function_display/5.note-ai-assitant.gif" width=400px><img src="./function_display/6.note-share.gif" width=400px>

### 5. Note and content locating, adding notes

<img src="./function_display/7.agent-location.gif" width=400px><img src="./function_display/8.agent-write-notes.gif" width=400px>

### 6. Note export (image export example)

Exporting note images with the Agent requires starting the sandbox container `services/draw_sandbox_service`.

<img src="./function_display/note-out.png" width=300px><img src="./function_display/note-out-agent.png" width=400px><img src="./function_display/note-out-mecha.png" width=400px>

## TODO

- [x] ~~Formula parsing optimization: ghosting and missing issues~~
- [x] ~~Image display: ghosting and missing subfigure captions~~
- [x] ~~pymupdf local parsing optimization~~
- [x] ~~Implement unlimited ocr parsing and rendering path~~
- [x] ~~Citation link display optimization~~
- [x] ~~unlimited-ocr parsing backend optimization~~
- [x] ~~Note export feature~~
- [ ] Agent feature optimization
- [ ] Agent communication module design

<!-- https://github.com/MarcYugo/tardigrade-paper-reader -->
