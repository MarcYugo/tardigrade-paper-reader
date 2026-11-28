"""技能注册表：论文阅读能力都挂在同一套 Agent 运行时上。

| 技能 | 模块 | 形态 |
| --- | --- | --- |
| 辅助阅读 `ai_read` | `skills/ai_read.py` | 流式对话 + 两个受限工具（写整理卡片 / 定位页面·笔记） |
| 笔记导出 `note_out` | `skills/note_out.py` | 阻塞工具循环（核验文献 / 找代码仓库）+ 结构化报告 |
| Agent 笔记图片 `note_out_image` | `skills/note_out_image.py` | 生成绘图 Python 并在 draw-sandbox 中执行 |
| 通信桥 `agent_comm` | `skills/agent_comm.py` | 消费 `agent_comm_service` 的 Redis Streams，按消息导出笔记 / 列出问答 |

循环、`tool_calls` 与 `tool` 消息成对、正文截断、页面动作与待确认事件都在
`agents/agent.py` 实现一次；技能只声明「提示词 + 工具 + 工具实现 + 收尾」。

`SKILLS` 是「名字 → 技能」的映射，`get_skill()` / `describe_skills()` 给以后的
技能路由（按用户意图挑技能）留口子；现在 `backend/server/api/*` 与 `note_out.py` 直接 import
各自的技能模块，不经过路由。
"""
from ..agent import Skill
from .agent_comm import SKILL as AGENT_COMM_SKILL
from .ai_read import SKILL as AI_READ_SKILL
from .note_out import SKILL as NOTE_OUT_SKILL
from .note_out_image import SKILL as NOTE_OUT_IMAGE_SKILL

SKILLS: dict[str, Skill] = {skill.name: skill
                            for skill in (AI_READ_SKILL, NOTE_OUT_SKILL,
                                          NOTE_OUT_IMAGE_SKILL, AGENT_COMM_SKILL)}


def get_skill(name: str) -> Skill:
    """按名字取技能；没有就 KeyError（调用方自己决定怎么报错）。"""
    try:
        return SKILLS[str(name)]
    except KeyError as exc:
        raise KeyError(f"没有这个技能：{name!r}") from exc


def describe_skills() -> list[dict]:
    """技能的公开描述（名字 / 标题 / 说明 / 工具名），给前端或调试用。"""
    return [{"name": s.name, "title": s.title, "description": s.description,
             "tools": sorted(s.tool_names)} for s in SKILLS.values()]


__all__ = ["AI_READ_SKILL", "NOTE_OUT_SKILL", "NOTE_OUT_IMAGE_SKILL",
           "AGENT_COMM_SKILL", "SKILLS", "get_skill", "describe_skills"]
