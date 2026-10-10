#!/usr/bin/env bash
# nanobot 服务器一键更新（幂等；按需构建、按需重启）
#
# 用法：
#   bash scripts/update-server.sh                  # 默认服务名 nanobot-gateway
#   bash scripts/update-server.sh <supervisor服务名>
#
# 行为要点：
#   - 只有 webui 有改动才构建；package-lock 变了才 npm ci，否则 npm run build
#   - 只有 Python 代码变化（或新装 uv）才重启——前端是静态文件，
#     构建落盘即生效，不必为它重启网关
#   - 部署完整性兜底（2026-10-10 实例）：代码可能在此前某次操作中已被拉到
#     当前 HEAD 而服务从未重启（仅对比本次 pull 前后会漏判）——网关进程
#     启动时间早于 HEAD 提交时间、或进程根本不在跑时，即使本次无更新也
#     补跑迁移并重启，杜绝"✅ 打在旧进程上"的假阳性
#   - 重启后校验 supervisor 必须回到 RUNNING，否则带排查指引地终止
#   - 代码无更新时跳过构建/迁移/重启，只做环境自检 + 健康检查
#   - 非 root 运行时自动加 sudo（supervisorctl / 软链 / venv 安装），
#     状态库与 uv 缓存固定按网关运行身份（root）处理
set -euo pipefail

APP_DIR="/root/nanobot-ai/nanobot-new"
SERVICE="${1:-nanobot-gateway}"
GATEWAY_HEALTH="http://127.0.0.1:18790/health"
GRAFANA_PACKAGE="mcp-grafana@1.6.2"
# 网关以 root 运行：状态库与 uv 缓存都在 /root 下，与本脚本执行者无关。
STATE_DB="/root/.nanobot/reports/state.db"

cd "$APP_DIR"

step() { printf '\n==> %s\n' "$*"; }
die()  { printf '!! %s\n' "$*" >&2; exit 1; }

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

step "1/6 拉取代码"
HEAD_BEFORE="$(git rev-parse HEAD)"
git pull --ff-only
HEAD_AFTER="$(git rev-parse HEAD)"
[ -f nanobot/webui/grafana_api.py ] \
  || die "没有 grafana_api.py：本地 push 了吗？push 后重跑本脚本"
if [ "$HEAD_BEFORE" = "$HEAD_AFTER" ]; then
  echo "   代码无更新（$(git log --oneline -1)）"
else
  git log --oneline "$HEAD_BEFORE..$HEAD_AFTER"
fi
PY_CHANGED="$(git diff --name-only "$HEAD_BEFORE" "$HEAD_AFTER" -- nanobot/)"
WEBUI_CHANGED="$(git diff --name-only "$HEAD_BEFORE" "$HEAD_AFTER" \
  -- webui/src webui/public webui/index.html webui/vite.config.ts webui/package.json)"
LOCK_CHANGED="$(git diff --name-only "$HEAD_BEFORE" "$HEAD_AFTER" -- webui/package-lock.json)"
DEPS_CHANGED="$(git diff --name-only "$HEAD_BEFORE" "$HEAD_AFTER" -- pyproject.toml requirements.txt)"
# 首次部署或 dist 缺失时强制构建。
[ -d nanobot/web/dist ] || WEBUI_CHANGED="dist-missing"

# 部署完整性兜底：最老网关进程的启动时间 vs HEAD 提交时间。pgrep 匹配
# `.venv/bin/nanobot gateway` 与 `python -m nanobot gateway` 两种形态。
HEAD_COMMIT_EPOCH="$(git log -1 --format=%ct HEAD)"
STALE_PROCESS=0
NO_PROCESS=0
GATEWAY_PIDS="$(pgrep -f 'nanobot gateway' || true)"
if [ -z "$GATEWAY_PIDS" ]; then
  NO_PROCESS=1
else
  OLDEST_ETIMES=""
  for _pid in $GATEWAY_PIDS; do
    _t="$(ps -o etimes= -p "$_pid" 2>/dev/null | tr -d '[:space:]')"
    case "$_t" in ''|*[!0-9]*) continue ;; esac
    if [ -z "$OLDEST_ETIMES" ] || [ "$_t" -gt "$OLDEST_ETIMES" ]; then
      OLDEST_ETIMES="$_t"
    fi
  done
  if [ -z "$OLDEST_ETIMES" ]; then
    echo "   ⚠ 读不到网关进程启动时长（ps 无 etimes？），跳过进程年龄校验"
  elif [ -n "$HEAD_COMMIT_EPOCH" ] \
    && [ "$(( $(date +%s) - OLDEST_ETIMES ))" -lt "$HEAD_COMMIT_EPOCH" ]; then
    STALE_PROCESS=1
    echo "   ⚠ 网关进程启动早于 HEAD 提交（进程落后于代码），将补迁移并重启"
  else
    echo "   网关进程与代码同步（已运行 ${OLDEST_ETIMES}s）"
  fi
fi

step "2/6 构建 WebUI（有改动才构建）"
if [ -n "$WEBUI_CHANGED$LOCK_CHANGED" ]; then
  if [ -n "$LOCK_CHANGED" ]; then
    npm --prefix webui ci
  fi
  npm --prefix webui run build
  echo "   构建完成（前端静态文件即时生效，无需为此重启）"
else
  echo "   webui 无改动，跳过"
fi
if [ -n "$DEPS_CHANGED" ]; then
  echo "⚠ 依赖清单变更（$DEPS_CHANGED）——本脚本不自动安装，请按需执行："
  echo "   $SUDO .venv/bin/python -m pip install -e ."
fi

step "3/6 导入自检 + flag 迁移（代码有更新时）"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)" || die "找不到 python（.venv/bin/python 或 python3）"
"$PY" -c "import nanobot.webui.grafana_api, nanobot.agent.tools.mcp_confirm" \
  || die "新模块导入失败——若是非 editable 安装: $SUDO $PY -m pip install -e ."
if [ "$HEAD_BEFORE" != "$HEAD_AFTER" ] || [ "$STALE_PROCESS" = 1 ]; then
  if [ -f "$STATE_DB" ]; then
    $SUDO cp "$STATE_DB" "${STATE_DB}.bak-$(date +%Y%m%d-%H%M%S)"
    echo "   已备份 state.db.bak-*"
  fi
  # env HOME=/root：无论是否经 sudo，都按网关运行身份解析配置与状态库。
  # migrate-flags 幂等（expand-only），进程落后于代码时补跑同样安全。
  $SUDO env HOME=/root "$PY" -m nanobot reports policy migrate-flags
else
  echo "   代码无更新且网关进程不落后，跳过迁移"
fi

step "4/6 安装 uv（Grafana 前置；已装则跳过）"
UV_INSTALLED=0
if ! command -v uvx >/dev/null 2>&1; then
  # 经 pip 从 PyPI 装（与项目依赖同源同信任级），装进网关 venv 后软链到系统 PATH。
  $SUDO "$PY" -m pip install --quiet uv
  VENV_BIN="$(cd "$(dirname "$PY")" && pwd)"
  # supervisor 服务进程的 PATH 通常极简（常不含 /usr/local/bin）——
  # 双保险：常规位置 + /usr/bin 软链，保证网关子进程总能找到 uvx。
  $SUDO ln -sf "$VENV_BIN/uvx" /usr/local/bin/uvx
  $SUDO ln -sf "$VENV_BIN/uv"  /usr/local/bin/uv
  $SUDO ln -sf "$VENV_BIN/uvx" /usr/bin/uvx
  $SUDO ln -sf "$VENV_BIN/uv"  /usr/bin/uv
  UV_INSTALLED=1
fi
UVX="$(command -v uvx || true)"
[ -n "$UVX" ] || UVX=/usr/bin/uvx
echo "   $UVX ($("$UVX" --version))"

step "5/6 预热 ${GRAFANA_PACKAGE}（写入 root 的 uv 缓存，首次约几十秒）"
$SUDO env HOME=/root "$UVX" "${GRAFANA_PACKAGE}" --help >/dev/null
echo "   预热完成"

step "6/6 重启（仅必要时）+ 健康检查"
if [ -n "$PY_CHANGED" ] || [ "$UV_INSTALLED" = 1 ] \
  || [ "$STALE_PROCESS" = 1 ] || [ "$NO_PROCESS" = 1 ]; then
  # restart 对已停止的服务可能报 not running，回落到 start 兜底。
  $SUDO supervisorctl restart "$SERVICE" || $SUDO supervisorctl start "$SERVICE"
  sleep 5
  if ! $SUDO supervisorctl status "$SERVICE" | grep -q RUNNING; then
    die "supervisor 服务未回到 RUNNING（常见原因：旧手工进程占用 18790/8765 或启动即崩）。排查：pgrep -af 'nanobot gateway' 找出非 supervisor 的旧进程先 kill；supervisorctl tail -50 $SERVICE stderr 看启动报错"
  fi
  $SUDO supervisorctl status "$SERVICE"
  if curl -fsS -o /dev/null --max-time 5 "$GATEWAY_HEALTH"; then
    echo "   /health OK"
  else
    echo "⚠ /health 未就绪（可能仍在启动）：稍后手动 curl $GATEWAY_HEALTH"
  fi
else
  echo "   Python 无改动、uv 已就位、网关进程与代码同步，跳过重启"
  curl -fsS -o /dev/null --max-time 5 "$GATEWAY_HEALTH" \
    && echo "   /health OK" \
    || echo "⚠ /health 无响应（网关可能没在跑）：supervisorctl status $SERVICE 看看"
fi

echo
echo "✅ 完成。Grafana 连接管理在：WebUI → Settings → Grafana（token 需服务器侧新建：读=Viewer，写=Editor 及以上）"
