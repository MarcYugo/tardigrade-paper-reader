"""模块五 · LLM 服务配置（**按账号存**）

每个账号有自己的一套「服务地址 / API Key / 模型」，和论文数据一样互相隔离：
`users.prefs.llm = {provider, base_url, model, api_key, profiles, active_id, updated_at, updated_by}`，
与账号密码同一份记录（删号即随之清除）。

**一个账号最多保留 `MAX_PROFILES`（3）套配置并可随时切换**：

- `profiles`：保存下来的若干套「服务类型 / 地址 / Key / 模型」（每套带 id 与可选的名字）；
- `active_id`：其中正在生效的那套；
- 顶层 `provider/base_url/model/api_key`：始终是**当前生效那套的镜像**，
  所以 `merged()` / `translator_config()` 这些老逻辑不用改。

老记录（只有顶层一份、没有 `profiles`）会在读取时自动当作一个名为“默认”的配置槽，
无需数据迁移；3 套都删光时会连账号级覆盖一起清掉，回落到环境变量 / `config.json`。

> 早期版本这里是**全局一份**（MongoDB `settings` 集合 `_id="llm"` / `data/llm.json`）。
> 升级时那份配置会被**归给最早创建的账号**（见 `adopt_legacy()`），其它账号重新填，
> 不会互相借用。

取值优先级（逐字段，高 → 低）：

| 来源 | 说明 |
| --- | --- |
| 本账号设置 | `users.prefs.llm`；`api_key` 只要写过就以它为准（写成空串表示清空） |
| 环境变量 | `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` / `TRANSLATOR_BACKEND` |
| `config.json` | 安装级默认（兼容旧配置）：`deepseek_api_key` 等 |
| 内置默认 | DeepSeek 官方地址 + `deepseek-chat` |

**隔离说明**：A 账号保存的 Key 只存在于 A 的账号记录里，B 账号读到的是 B 自己的
（没有就落到环境变量/`config.json` 的安装级默认），绝不会用到 A 的 Key。
明文 Key 只在服务端使用，接口一律回打码串。

**翻译目标语言**（`target_lang`）另存在 `users.prefs.target_lang`（与 `prefs.llm` 平级），
不放进上面那套配置槽里 —— 切/删 LLM 配置不会把语言带走。原文语言不用配，由 LLM 识别。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .envfile import env_value
from .translate import (DEFAULT_TARGET_LANG, TARGET_LANGS, lang_name,
                        normalize_lang, resolve_lang)

# 每个账号最多保留几套 LLM 配置（可随时切换）
MAX_PROFILES = 3

DEFAULTS = {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "api_key": "",
}

# 账号记录里除 DEFAULTS 之外还允许出现的键（多套配置用）
_META_KEYS = ("updated_at", "updated_by", "profiles", "active_id")


def _new_profile_id() -> str:
    """配置槽 id（仅用于前端选中/切换，不对外暴露含义）。"""
    return "p" + uuid.uuid4().hex[:8]


def _profile_label(p: dict, idx: int) -> str:
    """配置槽的显示名：优先用自定义名，否则用“配置 N（模型/地址）”。"""
    name = (p.get("name") or "").strip()
    if name:
        return name
    tail = (p.get("model") or "").strip() or (p.get("base_url") or "").strip()
    return f"配置 {idx}" + (f"（{tail}）" if tail else "")


# 字段 -> 环境变量名
ENV_MAP = {
    "provider": "TRANSLATOR_BACKEND",
    "base_url": "DEEPSEEK_BASE_URL",
    "model": "DEEPSEEK_MODEL",
    "api_key": "DEEPSEEK_API_KEY",
}

# 字段 -> config.json 里的旧键名
LEGACY_KEYS = {
    "provider": "backend",
    "base_url": "deepseek_base_url",
    "model": "deepseek_model",
    "api_key": "deepseek_api_key",
}

PROVIDERS = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "anthropic": "Anthropic (Claude)",
    "custom": "OpenAI 兼容服务(自建/中转)",
    "google": "Google 免费兜底(无需 Key)",
}

# 生效来源（给前端展示用）
SRC_ACCOUNT = "account"     # 本账号自己配的
SRC_ENV = "env"             # 环境变量
SRC_CONFIG = "config"       # config.json（安装级默认）
SRC_DEFAULT = "default"     # 内置默认
SRC_NONE = "none"           # 没有可用值（如 Key 为空）

SRC_TEXT = {
    SRC_ACCOUNT: "本账号配置",
    SRC_ENV: "环境变量",
    SRC_CONFIG: "config.json（安装级默认）",
    SRC_DEFAULT: "内置默认",
    SRC_NONE: "未配置",
}

# ---------------- 学术库检索（参考文献核验）配置 ----------------
# 与 LLM 配置一样**按账号存**（`users.prefs.scholar`），没配就回落环境变量 / `.env`。
SCHOLAR_FIELDS = ("openalex_key", "semantic_scholar_key", "s2_base_url", "mailto")
# Semantic Scholar 默认走兼容代理（官方直连经常 429）：地址只到 `/s2`，
# 后面接 `/graph/v1/…`，鉴权是 `Authorization: Bearer <令牌>`。
S2_DEFAULT_BASE = "https://s2api.ominiai.cn/s2"
SCHOLAR_DEFAULTS = {"openalex_key": "", "semantic_scholar_key": "",
                    "s2_base_url": S2_DEFAULT_BASE, "mailto": ""}
SCHOLAR_ENV = {
    "openalex_key": "OPENALEX_API_KEY",
    "semantic_scholar_key": "SEMANTIC_SCHOLAR_API_KEY",
    "s2_base_url": "SEMANTIC_SCHOLAR_BASE_URL",
    "mailto": "SCHOLARLY_MAILTO",
}
SCHOLAR_LABELS = {
    "openalex_key": "OpenAlex API Key",
    "semantic_scholar_key": "Semantic Scholar 令牌（Key）",
    "s2_base_url": "Semantic Scholar 服务地址（官方 / 代理）",
    "mailto": "联系邮箱（Crossref / OpenAlex polite pool）",
}


def mask_key(key: str) -> str:
    """`sk-abcdefghijkl` → `sk-a****ijkl`；太短则全打码。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return key[0] + "****"
    return f"{key[:4]}****{key[-4:]}"


def _read_json(path: Path | str | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class JsonSettingsBackend:
    """**旧版**全局设置文件 `data/llm.json`（只用于升级迁移读取 / JSON 回退模式下的历史值）。"""

    name = "json"

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> dict:
        with self._lock:
            return _read_json(self.path)

    def save(self, data: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def exists(self) -> bool:
        return self.path.exists()

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


class SettingsStore:
    """按账号的 LLM 配置读写。

    - `prefs`：提供 `get_prefs(username)` / `set_prefs(username, patch)` 的对象（通常是 `AuthStore`）；
    - `legacy`：旧版“全局一份”的存储（`MongoSettingsBackend` / `JsonSettingsBackend`），
      只在升级迁移时读一次。
    """

    def __init__(self, data_dir: Path | str, config_file: Path | str | None = None,
                 prefs=None, legacy=None):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "llm.json"
        self.config_file = config_file
        self.prefs = prefs
        self.legacy = legacy
        self._lock = threading.RLock()
        self._cfg_mtime: float | None = None
        self._cfg_cache: dict = {}

    # ---------------- 安装级默认（config.json） ----------------
    def _config_json(self) -> dict:
        p = Path(self.config_file) if self.config_file else None
        if not p or not p.exists():
            return {}
        try:
            mtime = p.stat().st_mtime
            if self._cfg_cache is not None and self._cfg_mtime == mtime:
                return self._cfg_cache
            data = json.loads(p.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
            self._cfg_mtime, self._cfg_cache = mtime, data
            return data
        except Exception:
            return {}

    # ---------------- 读 ----------------
    def _account_llm(self, username: str) -> dict:
        """本账号保存过的字段（没写过的键不出现，便于逐字段回退）。"""
        if not self.prefs or not username:
            return {}
        try:
            data = (self.prefs.get_prefs(username) or {}).get("llm") or {}
        except Exception:
            return {}
        return {k: v for k, v in data.items()
                if k in DEFAULTS or k in _META_KEYS}

    def raw(self, username: str) -> dict:
        return self._account_llm(username)

    def saved_by_ui(self, username: str) -> bool:
        return bool(self._account_llm(username))

    # ---------------- 多套配置（最多 MAX_PROFILES 套，可切换） ----------------
    #
    # 账号记录里：`profiles` = 保存下来的若干套「地址 / Key / 模型」，
    # `active_id` = 其中正在生效的那套。顶层那几个字段（provider/base_url/...）
    # 始终是**当前生效那份的镜像**，`merged()` 读的就是它 —— 这样老代码不用改。
    # 老账号（只有顶层一份、没有 profiles）会在内存里自动补出一个 `default` 槽。
    @staticmethod
    def _clean_profile(p: dict) -> dict:
        provider = str(p.get("provider") or "deepseek").strip().lower()
        if provider not in PROVIDERS:
            provider = "custom"
        return {
            "id": str(p.get("id") or "").strip(),
            "name": str(p.get("name") or "").strip(),
            "provider": provider,
            "base_url": str(p.get("base_url") or "").strip().rstrip("/"),
            "model": str(p.get("model") or "").strip(),
            "api_key": str(p.get("api_key") or "").strip(),
            "updated_at": p.get("updated_at"),
        }

    def _profiles_raw(self, username: str) -> list[dict]:
        """本账号保存的配置槽（**含明文 Key**，只在服务端内部用）。"""
        data = self._account_llm(username)
        raws = data.get("profiles")
        profs: list[dict] = []
        if isinstance(raws, list):
            for p in raws:
                if isinstance(p, dict):
                    profs.append(self._clean_profile(p))
        # 从“全局一份/账号一份”升级上来的老记录：把顶层那份补成一个槽（形如 id=default）
        if not profs and any(str(data.get(f) or "").strip()
                             for f in ("base_url", "model", "api_key")):
            profs = [self._clean_profile({
                "id": "default", "name": "默认",
                "provider": data.get("provider"), "base_url": data.get("base_url"),
                "model": data.get("model"), "api_key": data.get("api_key"),
                "updated_at": data.get("updated_at"),
            })]
        return profs[:MAX_PROFILES]

    def _active_id(self, username: str, profs: list[dict] | None = None) -> str:
        """当前生效的配置槽 id（记录里的 active_id 失效时，按顶层值反查一份）。"""
        profs = self._profiles_raw(username) if profs is None else profs
        if not profs:
            return ""
        ids = [p["id"] for p in profs]
        active = str(self._account_llm(username).get("active_id") or "").strip()
        if active in ids:
            return active
        raw = self._account_llm(username)
        base = str(raw.get("base_url") or "").strip().rstrip("/")
        model = str(raw.get("model") or "").strip()
        key = str(raw.get("api_key") or "").strip()
        for p in profs:
            if p["base_url"] == base and p["model"] == model and p["api_key"] == key:
                return p["id"]
        return ids[0]

    def profile_list(self, username: str) -> list[dict]:
        """给接口/前端用的配置槽列表：Key 一律打码，并标出哪套在用。"""
        profs = self._profiles_raw(username)
        active = self._active_id(username, profs)
        out = []
        for i, p in enumerate(profs, 1):
            out.append({
                "id": p["id"],
                "name": p["name"],
                "label": _profile_label(p, i),
                "provider": p["provider"],
                "provider_text": PROVIDERS.get(p["provider"], p["provider"]),
                "base_url": p["base_url"],
                "model": p["model"],
                "api_key_set": bool(p["api_key"]),
                "api_key_masked": mask_key(p["api_key"]),
                "active": p["id"] == active,
                "updated_at": p.get("updated_at"),
            })
        return out

    def profile_key(self, username: str, profile_id: str | None) -> str | None:
        """某个配置槽已保存的 Key；槽不存在（或要新建）返回 None。"""
        pid = (profile_id or "").strip()
        if not pid or pid == "new":
            return None
        for p in self._profiles_raw(username):
            if p["id"] == pid:
                return p["api_key"]
        return None

    # ---------------- 翻译目标语言（跟账号走，与上面几套 LLM 配置无关） ----------------
    def target_lang(self, username: str) -> str:
        """本账号的翻译目标语言（没设置过 / 存了脏值都回落到默认的简体中文）。"""
        if not self.prefs or not username:
            return DEFAULT_TARGET_LANG
        try:
            v = (self.prefs.get_prefs(username) or {}).get("target_lang")
        except Exception:
            return DEFAULT_TARGET_LANG
        return normalize_lang(v)

    def set_target_lang(self, username: str, lang) -> str:
        """保存本账号的翻译目标语言（`users.prefs.target_lang`），返回规范后的语言键。

        不支持的语言抛 `ValueError`。原文语言不在这里配：由 LLM 自己识别。
        """
        if not self.prefs:
            raise RuntimeError("没有配置存储后端（prefs），无法保存翻译语言")
        key = resolve_lang(lang)
        self.prefs.set_prefs(username, {"target_lang": key})
        return key

    def _write_profiles(self, username: str, profs: list[dict], active_id: str,
                        mirror: dict | None, user: str = "") -> dict:
        """落盘：写入配置槽 + 生效槽 + 顶层镜像（顶层镜像决定实际用哪套）。

        `mirror=None` 表示**不再覆盖账号级配置**（留给环境变量 / `config.json` 生效），
        只在“最后一套配置也被删掉”时用。
        """
        if not self.prefs:
            raise RuntimeError("没有配置存储后端（prefs），无法保存 LLM 设置")
        data = ({f: str(mirror.get(f) or DEFAULTS[f]) for f in DEFAULTS}
                if mirror is not None else {})
        data["profiles"] = profs
        data["active_id"] = active_id
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data["updated_by"] = user or username
        self.prefs.set_prefs(username, {"llm": data})
        return data

    @staticmethod
    def _mirror_of(p: dict) -> dict:
        return {f: p.get(f) or DEFAULTS[f] for f in DEFAULTS}

    def sources(self, username: str) -> dict:
        """每个字段的生效来源（account/env/config/default），前端据此提示。"""
        account = self._account_llm(username)
        cfg = self._config_json()
        out = {}
        for field in DEFAULTS:
            v = account.get(field)
            if isinstance(v, str) and (field == "api_key" or v.strip()):
                out[field] = SRC_ACCOUNT
                continue
            if (os.environ.get(ENV_MAP[field]) or "").strip():
                out[field] = SRC_ENV
                continue
            legacy = cfg.get(LEGACY_KEYS[field])
            if isinstance(legacy, str) and legacy.strip():
                out[field] = SRC_CONFIG
                continue
            out[field] = SRC_DEFAULT
        if not out.get("api_key") or out["api_key"] == SRC_DEFAULT:
            out["api_key"] = SRC_NONE
        return out

    def merged(self, username: str) -> dict:
        """按优先级逐字段取值，返回该账号实际生效的完整 LLM 配置。"""
        account = self._account_llm(username)
        cfg = self._config_json()
        out: dict = {}
        for field, default in DEFAULTS.items():
            # 1) 本账号设置：api_key 以“是否写过”为准(允许清空)，其余要求非空
            v = account.get(field)
            if isinstance(v, str):
                if field == "api_key" or v.strip():
                    out[field] = v.strip()
                    continue
            # 2) 环境变量
            env = (os.environ.get(ENV_MAP[field]) or "").strip()
            if env:
                out[field] = env
                continue
            # 3) 安装级默认 config.json
            legacy = cfg.get(LEGACY_KEYS[field])
            if isinstance(legacy, str) and legacy.strip():
                out[field] = legacy.strip()
                continue
            out[field] = default
        return out

    def translator_config(self, username: str) -> dict:
        """给 `Translator` 用的配置：config.json 原样 + 该账号的 LLM 覆盖项 + 目标语言。

        注意要把 config.json 里的旧键(`deepseek_api_key` 等)删掉：它们已经被
        `merged()` 按优先级吸收过了，留着会让“显式清空 Key”又被旧键救回来。
        """
        cfg = dict(self._config_json())
        for legacy in LEGACY_KEYS.values():
            cfg.pop(legacy, None)
        llm = self.merged(username)
        cfg.update({
            "backend": llm["provider"],
            "api_key": llm["api_key"],
            "base_url": llm["base_url"],
            "model": llm["model"],
            "target_lang": self.target_lang(username),
        })
        return cfg

    def describe(self, username: str, *, active_key: str = "", status_text: str = "") -> dict:
        """接口返回：明文 Key 一律打码，并带上“这个值是谁的/从哪来”。

        另外带上翻译目标语言：`target_lang` 是当前值，`target_langs` 是可选清单
        （前端下拉直接用，加语言不用改前端），`default_target_lang` 是默认值。
        """
        llm = self.merged(username)
        raw = self._account_llm(username)
        src = self.sources(username)
        key = active_key if active_key else llm["api_key"]
        profs = self.profile_list(username)
        active = self._active_id(username)
        lang = self.target_lang(username)
        return {
            "owner": username,                 # 这份配置属于哪个账号
            "provider": llm["provider"],
            "providers": PROVIDERS,
            "profiles": profs,                 # 已保存的配置槽（Key 打码，最多 MAX_PROFILES 个）
            "active_id": active,               # 当前生效的槽 id
            "max_profiles": MAX_PROFILES,
            "base_url": llm["base_url"],
            "model": llm["model"],
            "api_key_set": bool(key),
            "api_key_masked": mask_key(key),
            "from_ui": self.saved_by_ui(username),
            "updated_at": raw.get("updated_at"),
            "updated_by": raw.get("updated_by"),
            "status_text": status_text,
            "target_lang": lang,                       # 译文语言（原文语言由 LLM 识别）
            "target_lang_text": lang_name(lang),
            "target_langs": TARGET_LANGS,              # 可选清单：键 -> 显示名
            "default_target_lang": DEFAULT_TARGET_LANG,
            "sources": src,
            "source_text": {k: SRC_TEXT.get(v, v) for k, v in src.items()},
            "using_account_key": src.get("api_key") == SRC_ACCOUNT,
        }

    # ---------------- 写（只写当前账号） ----------------
    def save_llm(self, username: str, *, provider: str, base_url: str, model: str,
                 api_key: str | None = None, user: str = "",
                 profile_id: str | None = None, profile_name: str | None = None) -> dict:
        """保存**当前账号**的 LLM 配置。

        - `api_key=None` 表示沿用该配置槽已保存的 Key；传空串表示清空。
        - `profile_id`：要写入的配置槽；`None`/`"new"` 表示**新建**一个
          （最多 `MAX_PROFILES` 个，超了抛 `ValueError`）。
        - 保存的这份会成为当前生效的配置（相当于保存即切换）。
        """
        if not self.prefs:
            raise RuntimeError("没有配置存储后端（prefs），无法保存 LLM 设置")

        provider = (provider or "deepseek").strip().lower()
        if provider not in PROVIDERS:
            provider = "custom"
        base_url = (base_url or "").strip().rstrip("/")
        model = (model or "").strip()

        profs = self._profiles_raw(username)
        want = (profile_id or "").strip()
        target = None
        if want and want != "new":
            target = next((p for p in profs if p["id"] == want), None)
            if target is None:
                raise ValueError("要保存的配置不存在（可能已被删除），请刷新后重试")
        if target is None:                     # 新建一套
            if len(profs) >= MAX_PROFILES:
                raise ValueError(f"最多只能保留 {MAX_PROFILES} 套 LLM 配置，请先删除一套再新建")
            target = self._clean_profile({"id": _new_profile_id(),
                                          "name": (profile_name or "").strip()})
            profs.append(target)
        elif profile_name is not None:
            target["name"] = profile_name.strip()

        target["provider"] = provider
        target["base_url"] = base_url
        target["model"] = model
        if api_key is not None:
            target["api_key"] = api_key.strip()
        target["updated_at"] = datetime.now().isoformat(timespec="seconds")

        self._write_profiles(username, profs, target["id"],
                             self._mirror_of(target), user=user)
        return target

    def activate_profile(self, username: str, profile_id: str) -> dict:
        """切换当前生效的配置（顶层镜像随之更新，下一次调用就用新的）。"""
        profs = self._profiles_raw(username)
        pid = (profile_id or "").strip()
        target = next((p for p in profs if p["id"] == pid), None)
        if target is None:
            raise ValueError("配置不存在（可能已被删除），请刷新后重试")
        self._write_profiles(username, profs, target["id"],
                             self._mirror_of(target), user=username)
        return target

    def delete_profile(self, username: str, profile_id: str) -> str:
        """删除一套配置，返回删除后仍在生效的槽 id（可能为空）。

        删掉的若是当前生效那套，会自动切到剩下的第一套；**一套都不剩时连账号级
        配置一起清掉**，回落到环境变量 / `config.json` / 内置默认。
        """
        pid = (profile_id or "").strip()
        profs = self._profiles_raw(username)
        if not any(p["id"] == pid for p in profs):
            raise ValueError("配置不存在（可能已被删除），请刷新后重试")
        raw = self._account_llm(username)
        was_active = pid == self._active_id(username, profs)
        profs = [p for p in profs if p["id"] != pid]
        if not profs:
            active_id, mirror = "", None
        elif was_active:
            mirror = self._mirror_of(profs[0])
            active_id = profs[0]["id"]
        else:
            mirror = {f: (raw.get(f) if isinstance(raw.get(f), str) else DEFAULTS[f])
                      for f in DEFAULTS}
            active_id = self._active_id(username, profs)
        self._write_profiles(username, profs, active_id, mirror, user=username)
        return active_id

    # ---------------- 旧版“全局一份”的迁移 ----------------
    def legacy_global(self) -> dict:
        """读旧版的全局 LLM 配置（MongoDB `settings` 或 `data/llm.json`）。"""
        if not self.legacy:
            return {}
        try:
            data = self.legacy.load() or {}
        except Exception:
            return {}
        return {k: v for k, v in data.items() if k in DEFAULTS}

    def adopt_legacy(self, username: str) -> bool:
        """把旧版全局配置归给**最早创建的账号**（仅当该账号还没配过）。"""
        legacy = self.legacy_global()
        if not legacy or not username or self.saved_by_ui(username):
            return False
        data = dict(legacy)
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data["updated_by"] = f"{username}（自旧版全局配置迁移）"
        self.prefs.set_prefs(username, {"llm": data})
        return True

    # ---------------- 学术库检索（参考文献核验）配置 ----------------
    #
    # 存在 `users.prefs.scholar`，与 LLM 配置完全同构：按账号隔离、Key 一律打码回前端；
    # 没配就回落环境变量 / `.env`（`OPENALEX_API_KEY` / `SEMANTIC_SCHOLAR_API_KEY` /
    # `SCHOLARLY_MAILTO`）。Agent 导出时用 `scholar_merged()` 取明文传进去。
    def _account_scholar(self, username: str) -> dict:
        """本账号保存过的学术库字段（没写过的键不出现，便于逐字段回退）。"""
        if not self.prefs or not username:
            return {}
        try:
            data = (self.prefs.get_prefs(username) or {}).get("scholar") or {}
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: str(v) for k, v in data.items()
                if k in SCHOLAR_DEFAULTS or k in ("updated_at", "updated_by")}

    def scholar_merged(self, username: str) -> dict:
        """该账号实际生效的学术库配置（**明文**，只给服务端用）。"""
        account = self._account_scholar(username)
        out: dict[str, str] = {}
        for field in SCHOLAR_FIELDS:
            # 1) 本账号写过就以它为准（写空 = 明确清空，不回落到环境变量）
            if isinstance(account.get(field), str):
                out[field] = account[field].strip()
                continue
            # 2) 环境变量 / .env
            out[field] = (env_value(SCHOLAR_ENV[field])
                          or SCHOLAR_DEFAULTS[field]).strip()
        return out

    def scholar_sources(self, username: str) -> dict:
        """每个字段的生效来源（account / env / none）。"""
        account = self._account_scholar(username)
        out: dict[str, str] = {}
        for field in SCHOLAR_FIELDS:
            if isinstance(account.get(field), str):
                out[field] = SRC_ACCOUNT if account[field].strip() else SRC_NONE
            elif env_value(SCHOLAR_ENV[field]):
                out[field] = SRC_ENV
            else:
                out[field] = SRC_NONE
        return out

    def scholar_describe(self, username: str) -> dict:
        """接口返回：Key 打码 + 来源说明（前端「设置 → 学术检索」用）。"""
        merged = self.scholar_merged(username)
        account = self._account_scholar(username)
        src = self.scholar_sources(username)
        return {
            "owner": username,
            "openalex_key_set": bool(merged["openalex_key"]),
            "openalex_key_masked": mask_key(merged["openalex_key"]),
            "semantic_scholar_key_set": bool(merged["semantic_scholar_key"]),
            "semantic_scholar_key_masked": mask_key(merged["semantic_scholar_key"]),
            "s2_base_url": merged["s2_base_url"],
            "mailto": merged["mailto"],
            "fields": {f: {"label": SCHOLAR_LABELS[f], "env": SCHOLAR_ENV[f]}
                       for f in SCHOLAR_FIELDS},
            "sources": src,
            "source_text": {k: SRC_TEXT.get(v, v) for k, v in src.items()},
            "from_ui": bool(account),
            "updated_at": account.get("updated_at"),
            "updated_by": account.get("updated_by"),
        }

    def save_scholar(self, username: str, *, openalex_key: str | None = None,
                     semantic_scholar_key: str | None = None,
                     s2_base_url: str | None = None,
                     mailto: str | None = None, user: str = "") -> dict:
        """保存本账号的学术库配置。

        `None` = 这项不改；`""` = 明确清空（服务地址清空则回到默认代理）。
        """
        if not self.prefs:
            raise RuntimeError("没有配置存储后端（prefs），无法保存学术库设置")
        if s2_base_url is not None and s2_base_url.strip() \
                and not s2_base_url.strip().startswith(("http://", "https://")):
            raise ValueError("Semantic Scholar 服务地址必须是 http(s):// 开头")
        data = {k: v for k, v in self._account_scholar(username).items()
                if k in SCHOLAR_FIELDS}
        for field, value in (("openalex_key", openalex_key),
                             ("semantic_scholar_key", semantic_scholar_key),
                             ("s2_base_url", s2_base_url),
                             ("mailto", mailto)):
            if value is not None:
                data[field] = str(value).strip()
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data["updated_by"] = user or username
        self.prefs.set_prefs(username, {"scholar": data})
        return data

    def clear_scholar(self, username: str) -> None:
        """删掉本账号的学术库设置，回落到环境变量 / `.env`。"""
        if not self.prefs:
            raise RuntimeError("没有配置存储后端（prefs），无法修改学术库设置")
        self.prefs.set_prefs(username, {"scholar": {}})
