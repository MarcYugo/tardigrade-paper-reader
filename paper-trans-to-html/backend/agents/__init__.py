"""AI 相关模块。

* `llm.py` —— LLM 接入层：OpenAI 兼容 chat/completions 的客户端（对话 / 结构化 JSON /
  流式 / tool_calls 解析 / 测试连接）。业务层（`translate.py`、`ai_read.py`）与技能
  都通过它发请求。
* `agent.py` —— **统一的 Agent 运行时**：技能声明（`Skill`）+ 工具调用循环
  （`run_tool_loop`，流式 / 阻塞两种适配）+ 历史卫生（tool_calls 与 tool 消息成对）。
  两个 Agent 原来各写一套循环，现在只有这一套。
* `skills/ai_read.py` —— 技能「辅助阅读」：把用户要的内容写进右侧「笔记整理」卡片、
  定位论文页 / 某条笔记（跳转前先征求用户确认）。
* `skills/note_out.py` —— 技能「笔记导出」：核验参考文献真实性、提取代码仓库地址，
  再形成分析报告（工具只读、离线失败不打断导出）。
* `skills/note_out_image.py` —— 技能「Agent 笔记图片」：生成 Python 绘图代码并在
  draw-sandbox 中执行。
* `skills/__init__.py` —— 技能注册表（`SKILLS` / `get_skill` / `describe_skills`）。

本文件**不 import 技能**：`skills/ai_read.py` 会反向依赖 `backend/ai_read.py`，
在这里提前导入容易绕成循环；需要技能的模块直接 `from .agents.skills import ...`。
"""
