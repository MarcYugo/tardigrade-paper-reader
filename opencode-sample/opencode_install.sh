#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# opencode 安装脚本（Linux / macOS，x64 / arm64）
#
# 特点：
#   * 默认下载官方发布的二进制包，开箱即用，不依赖 Node.js
#   * 默认走 npmmirror 国内镜像（失败自动回退官方 npm 源）
#   * 自动识别 glibc / musl、是否支持 AVX2，选择对应构建（含 baseline）
#   * 支持安装 OpenCode 2（默认，最新版）或 OpenCode 1
#   * 备用方式：npm 全局安装、官方安装脚本
#   * 自动把安装目录写入 PATH，装完直接可以运行 opencode
#
# 用法：
#   bash opencode_install.sh                     # 安装最新 OpenCode 2
#   bash opencode_install.sh --channel 1         # 安装 OpenCode 1
#   bash opencode_install.sh -v 2.4.2            # 安装指定版本
#   bash opencode_install.sh -m npm              # 用 npm 全局安装
#   bash opencode_install.sh -m official         # 用官方安装脚本安装
#   bash opencode_install.sh --gh-proxy https://ghfast.top   # OpenCode 1 走 GitHub 加速
#   bash opencode_install.sh --dry-run           # 只打印安装计划，不真正安装
#
# 环境变量（可替代同名参数）：
#   OPENCODE_REGISTRY       npm 镜像地址，默认 https://registry.npmmirror.com
#   OPENCODE_INSTALL_DIR    安装目录，默认 $HOME/.opencode/bin
#   OPENCODE_GH_PROXY       GitHub 加速前缀（仅 OpenCode 1 用到）
# ---------------------------------------------------------------------------

set -euo pipefail

C_RESET=$'\033[0m'
C_RED=$'\033[0;31m'
C_GREEN=$'\033[0;32m'
C_YELLOW=$'\033[0;33m'
C_DIM=$'\033[0;2m'

info() { printf '%s[信息]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s[警告]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[错误]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
die()  { err "$*"; exit 1; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

usage() {
    # 打印文件头部的注释说明
    awk 'NR > 2 { if ($0 ~ /^#/) { sub(/^# ?/, ""); print; next } exit }' "$0"
    cat <<'EOF'

可用参数：
  -c, --channel <1|2>     安装 OpenCode 1 或 OpenCode 2（默认 2）
  -v, --version <版本>     指定版本号，例如 2.4.2 或 1.19.6
  -m, --method <方式>      binary（默认）| npm | official
  -r, --registry <URL>     npm 镜像地址（默认 https://registry.npmmirror.com）
  -d, --dir <目录>         安装目录（默认 $HOME/.opencode/bin）
      --gh-proxy <URL>     GitHub 加速前缀，用于 OpenCode 1 下载
      --no-modify-path     不修改 shell 配置文件
      --link               额外创建 /usr/local/bin/opencode 软链接（可能需要 sudo）
      --force              已安装相同版本时也强制重装
      --dry-run            只打印安装计划
  -h, --help               显示帮助
EOF
}

# ------------------------------- 参数解析 ----------------------------------
CHANNEL=2
VERSION=""
METHOD="binary"
REGISTRY="${OPENCODE_REGISTRY:-https://registry.npmmirror.com}"
INSTALL_DIR="${OPENCODE_INSTALL_DIR:-$HOME/.opencode/bin}"
GH_PROXY="${OPENCODE_GH_PROXY:-}"
MODIFY_PATH=true
MAKE_LINK=false
FORCE=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)        usage; exit 0 ;;
        -c|--channel)     CHANNEL="${2:-}"; shift 2 ;;
        -v|--version)     VERSION="${2:-}"; shift 2 ;;
        -m|--method)      METHOD="${2:-}"; shift 2 ;;
        -r|--registry)    REGISTRY="${2:-}"; shift 2 ;;
        -d|--dir)         INSTALL_DIR="${2:-}"; shift 2 ;;
        --gh-proxy)       GH_PROXY="${2:-}"; shift 2 ;;
        --no-modify-path) MODIFY_PATH=false; shift ;;
        --link)           MAKE_LINK=true; shift ;;
        --force)          FORCE=true; shift ;;
        --dry-run)        DRY_RUN=true; shift ;;
        *)                err "未知参数: $1"; usage; exit 1 ;;
    esac
done

[[ "$CHANNEL" =~ ^[12]$ ]] || die "--channel 只能是 1 或 2"
case "$METHOD" in
    binary|npm|official) ;;
    *) die "--method 只能是 binary、npm 或 official" ;;
esac

REGISTRY="${REGISTRY%/}"
INSTALL_DIR="${INSTALL_DIR%/}"
VERSION="${VERSION#v}"

# 镜像优先，官方源兜底
REGISTRIES=("$REGISTRY")
if [ "$REGISTRY" != "https://registry.npmjs.org" ]; then
    REGISTRIES+=("https://registry.npmjs.org")
fi

WORKDIR=""
cleanup() { if [ -n "$WORKDIR" ] && [ -d "$WORKDIR" ]; then rm -rf "$WORKDIR"; fi; }
trap cleanup EXIT

# ------------------------------- 工具函数 ----------------------------------
need() { command -v "$1" >/dev/null 2>&1 || die "缺少依赖：$1"; }

http_get() { curl -fsSL --max-time 20 "$1" 2>/dev/null || true; }

http_code() {
    curl -sIL --max-time 15 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true
}

# 从一行 JSON 里取 "key":"value"
json_value() {
    printf '%s' "$1" | sed -n "s/.*\"$2\":\"\([^\"]*\)\".*/\1/p" | head -n1
}

# 从 dist-tags 里取 latest
json_latest() {
    printf '%s' "$1" | grep -o '"dist-tags":{[^}]*}' | grep -o '"latest": *"[^"]*"' \
        | sed 's/.*"\([^"]*\)"$/\1/' | head -n1
}

extract_version() {
    printf '%s' "$1" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*' | head -n1
}

# ------------------------------- 平台识别 ----------------------------------
detect_platform() {
    case "$(uname -s)" in
        Linux)  OS=linux ;;
        Darwin) OS=darwin ;;
        MINGW*|MSYS*|CYGWIN*) die "Windows 请在 WSL / Git Bash 之外使用官方 MSI，或不支持本脚本" ;;
        *)      die "不支持的系统：$(uname -s)" ;;
    esac

    case "$(uname -m)" in
        x86_64|amd64) ARCH=x64 ;;
        aarch64|arm64) ARCH=arm64 ;;
        *) die "不支持的架构：$(uname -m)" ;;
    esac

    TARGET="$OS-$ARCH"
    IS_MUSL=false
    if [ "$OS" = "linux" ]; then
        if [ -f /etc/alpine-release ]; then IS_MUSL=true; fi
        if command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; then
            IS_MUSL=true
        fi
    fi

    NEED_BASELINE=false
    if [ "$ARCH" = "x64" ]; then
        if [ "$OS" = "linux" ]; then
            if ! grep -qwi avx2 /proc/cpuinfo 2>/dev/null; then NEED_BASELINE=true; fi
        else
            if [ "$(sysctl -n hw.optional.avx2_0 2>/dev/null || echo 0)" != "1" ]; then
                NEED_BASELINE=true
            fi
        fi
    fi

    if [ "$NEED_BASELINE" = true ]; then TARGET="$TARGET-baseline"; fi
    if [ "$IS_MUSL" = true ]; then TARGET="$TARGET-musl"; fi
}

# ------------------------------- 版本解析 ----------------------------------
resolve_version() {
    if [ -n "$VERSION" ]; then
        return 0
    fi

    local meta ver
    if [ "$CHANNEL" = "2" ]; then
        meta=$(http_get "https://opencode.ai/update/api/latest/cli/npm")
        ver=$(json_value "$meta" version)
        if [ -z "$ver" ]; then
            ver=$(json_latest "$(http_get "$REGISTRY/@opencode%2fcli")")
        fi
        if [ -n "$ver" ] && [[ "$ver" == 1.* ]]; then
            warn "官方元数据返回的是 1.x（$ver），请确认网络环境"
        fi
    else
        ver=$(json_value "$(http_get "$REGISTRY/opencode-ai/latest")" version)
        if [ -z "$ver" ] || [[ "$ver" != 1.* ]]; then
            # 回退到 GitHub 上最新的 v1 Release
            local tags
            tags=$(http_get "https://api.github.com/repos/anomalyco/opencode/releases?per_page=100")
            ver=$(printf '%s' "${tags}" | grep -oE '"tag_name": *"v1\.[0-9]+\.[0-9]+"' \
                | head -n1 | grep -oE '1\.[0-9]+\.[0-9]+')
        fi
    fi

    [ -n "$ver" ] || die "无法获取版本号，请用 -v 指定具体版本（例如 -v 2.4.2）"
    VERSION="$ver"
}

# ----------------------------- 已安装检查 ----------------------------------
check_existing() {
    if [ "$FORCE" = true ]; then return 0; fi
    if [ ! -x "$INSTALL_DIR/opencode" ]; then return 0; fi
    local cur
    cur=$(extract_version "$("$INSTALL_DIR/opencode" --version 2>/dev/null | head -n1 || true)")
    if [ -n "$cur" ] && [ "$cur" = "$VERSION" ]; then
        info "已存在相同版本 v$cur（$INSTALL_DIR/opencode），跳过安装。"
        dim "如需强制重装：bash $0 --force"
        finish
        exit 0
    fi
    if [ -n "$cur" ]; then info "检测到已安装 v$cur，将升级为 v$VERSION"; fi
    return 0
}

# --------------------------- 方式一：二进制 ---------------------------------
install_binary() {
    local url="" reg pkg slug archive bin

    if [ "$CHANNEL" = "2" ]; then
        for reg in "${REGISTRIES[@]}"; do
            for pkg in "@opencode/cli-$TARGET" "@opencode-ai/cli-$TARGET"; do
                slug="${pkg##*/}"
                if [ "$(http_code "$reg/$pkg/$VERSION")" = "200" ]; then
                    url="$reg/$pkg/-/$slug-$VERSION.tgz"
                    break 2
                fi
            done
        done
        [ -n "$url" ] || die "没有找到 $TARGET 平台 v$VERSION 的二进制包（可尝试 --channel 1 或 -m official）"
        archive="tar.gz"
    else
        archive="tar.gz"
        if [ "$OS" = "darwin" ]; then archive="zip"; fi
        url="${GH_PROXY:+${GH_PROXY%/}/}https://github.com/anomalyco/opencode/releases/download/v$VERSION/opencode-$TARGET.$archive"
    fi

    info "下载地址：$url"
    if [ "$DRY_RUN" = true ]; then
        dim "（dry-run，未实际下载）"
        return 0
    fi

    need tar
    if [ "$archive" = "zip" ]; then need unzip; fi

    WORKDIR=$(mktemp -d)
    local file="$WORKDIR/pkg.$archive" out="$WORKDIR/x"
    mkdir -p "$out"

    info "开始下载（约 100MB 左右，请耐心等待）..."
    curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 --progress-bar -o "$file" "$url" \
        || die "下载失败：$url"
    [ -s "$file" ] || die "下载文件为空：$url"

    if [ "$archive" = "zip" ]; then
        unzip -oq "$file" -d "$out" || die "解压失败"
    else
        tar -xzf "$file" -C "$out" || die "解压失败"
    fi

    bin=$(find "$out" -type f -name 'opencode' ! -name '*.exe' | head -n1)
    if [ -z "$bin" ]; then
        bin=$(find "$out" -type f -name 'opencode.exe' | head -n1)
    fi
    if [ -z "$bin" ]; then die "压缩包内未找到 opencode 可执行文件"; fi

    mkdir -p "$INSTALL_DIR"
    install -m 0755 "$bin" "$INSTALL_DIR/opencode"
    info "已安装到 $INSTALL_DIR/opencode"
}

# --------------------------- 方式二：npm ------------------------------------
install_npm() {
    local pkg="@opencode/cli"
    if [ "$CHANNEL" = "1" ]; then pkg="opencode-ai"; fi

    info "npm 全局安装 ${pkg}@${VERSION}（源：$REGISTRY）"
    if [ "$DRY_RUN" = true ]; then
        dim "（dry-run，未实际安装）"
        return 0
    fi

    need node
    need npm

    if ! npm install -g "${pkg}@${VERSION}" --registry="$REGISTRY" --no-fund --no-audit; then
        err "npm 安装失败"
        dim "常见原因：无写权限 -> sudo npm install -g ...；或换源 -> -r https://registry.npmjs.org"
        exit 1
    fi
}

# ------------------------- 方式三：官方安装脚本 ------------------------------
install_official() {
    local url="https://opencode.ai/install"
    if [ "$CHANNEL" = "2" ]; then url="https://opencode.ai/v2/install"; fi

    info "使用官方安装脚本：$url"
    if [ "$DRY_RUN" = true ]; then
        dim "（dry-run，未实际安装）"
        return 0
    fi

    if [ "$INSTALL_DIR" != "$HOME/.opencode/bin" ]; then
        warn "官方脚本固定安装到 \$HOME/.opencode/bin，忽略 -d $INSTALL_DIR"
    fi

    local args=()
    if [ -n "$VERSION" ]; then args+=(--version "$VERSION"); fi
    if [ "$MODIFY_PATH" = false ]; then args+=(--no-modify-path); fi

    if [ ${#args[@]} -gt 0 ]; then
        curl -fsSL "$url" | bash -s -- "${args[@]}"
    else
        curl -fsSL "$url" | bash
    fi
    INSTALL_DIR="$HOME/.opencode/bin"
}

# ------------------------------ PATH 处理 ----------------------------------
ensure_path() {
    case ":$PATH:" in
        *":$INSTALL_DIR:"*) return 0 ;;
    esac

    if [ "$MODIFY_PATH" = false ]; then return 0; fi
    if [ "$DRY_RUN" = true ]; then return 0; fi

    local rc added=false line
    line="export PATH=\"$INSTALL_DIR:\$PATH\""
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        if [ ! -f "$rc" ]; then continue; fi
        if grep -qF "$INSTALL_DIR" "$rc"; then continue; fi
        printf '\n# opencode\n%s\n' "$line" >> "$rc"
        info "已把 $INSTALL_DIR 写入 $rc"
        added=true
    done

    if [ "$added" = false ]; then
        printf '\n# opencode\n%s\n' "$line" >> "$HOME/.profile"
        info "已把 $INSTALL_DIR 写入 $HOME/.profile"
    fi
}

make_link() {
    if [ "$MAKE_LINK" != true ]; then return 0; fi
    if [ "$DRY_RUN" = true ]; then return 0; fi

    local target="/usr/local/bin/opencode"
    if [ -w /usr/local/bin ]; then
        ln -sf "$INSTALL_DIR/opencode" "$target" && info "已创建软链接 $target"
    elif command -v sudo >/dev/null 2>&1; then
        sudo ln -sf "$INSTALL_DIR/opencode" "$target" && info "已创建软链接 $target"
    else
        warn "无权限创建 $target，跳过"
    fi
}

# -------------------------------- 收尾 -------------------------------------
verify() {
    local bin="${1:-$INSTALL_DIR/opencode}"
    if [ "$DRY_RUN" = true ]; then return 0; fi
    if [ ! -x "$bin" ]; then die "未找到可执行文件：$bin"; fi

    local v
    v=$("$bin" --version 2>&1 | head -n1 || true)
    if [ -z "$v" ]; then
        warn "opencode --version 无输出，可能是缺少运行库（如 libstdc++ / glibc 版本过低）"
    else
        info "安装成功：$v"
    fi
}

finish() {
    if [ "$DRY_RUN" = true ]; then return 0; fi
    ensure_path
    make_link
    cat <<EOF

$(printf '%s' "$C_GREEN")完成！$(printf '%s' "$C_RESET")下一步：

  source ~/.bashrc            # 或重新登录终端，使 PATH 生效
  opencode --version          # 验证
  opencode                    # 启动（在 TUI 中执行 /connect 配置模型）
  opencode auth login         # 需要 API Key 时登录

EOF
    dim "安装目录：$INSTALL_DIR"
    dim "卸载：rm -rf \"$INSTALL_DIR/opencode\""
}

# -------------------------------- 主流程 -----------------------------------
main() {
    need curl
    detect_platform
    info "平台：$TARGET    方式：$METHOD    OpenCode 大版本：$CHANNEL"

    case "$METHOD" in
        binary)
            resolve_version
            info "目标版本：v$VERSION"
            check_existing
            install_binary
            verify
            ;;
        npm)
            resolve_version
            info "目标版本：v$VERSION"
            check_existing
            install_npm
            if [ "$DRY_RUN" = false ]; then
                verify "$(npm prefix -g 2>/dev/null)/bin/opencode"
            fi
            ;;
        official)
            if [ -z "$VERSION" ]; then resolve_version; fi
            INSTALL_DIR="$HOME/.opencode/bin"
            install_official
            verify
            ;;
    esac

    finish
}

main "$@"
