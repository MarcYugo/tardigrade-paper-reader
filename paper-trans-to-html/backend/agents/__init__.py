"""AI 相关模块。

* `llm.py` —— LLM 接入层：OpenAI 兼容 chat/completions 的客户端（对话 / 结构化 JSON /
  流式 / tool_calls 解析 / 测试连接）。业务层（`translate.py`、`ai_read.py`）与 Agent
  都通过它发请求。
* `ai_read_agent.py` —— 可调工具的论文阅读 Agent（受限工具调用：把内容写进右侧
  「笔记整理」卡片、定位论文页 / 某条笔记）。
* `note_out_agent.py` —— 笔记导出的 Agent（核验参考文献真实性、提取代码仓库地址，
  再形成分析报告；工具只读、离线失败不打断导出）。
"""
