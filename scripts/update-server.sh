#!/usr/bin/env bash
# nanobot 服务器一键更新（常规同步 + Grafana 全部前置）
#
# 用法（服务器 root）：
#   bash scripts/update-server.sh            # 自动探测 supervisor 服务名
#   bash scripts/update-server.sh <服务名>   # 指定 supervisor 程序名
#
# 做什么：git pull → 新代码导入自检 → WebUI 构建 → 报表状态库备份 +
#         flag 迁移（幂等）→ 装 uv（已装跳过）→ 预热 mcp-grafana →
#         supervisorctl 重启 → 健康检查
# 前提：本地仓库已 git push；服务器已装 git / node(npm) / python venv。
set -euo pipefail

APP_DIR="/root/nanobot-ai/nanobot-new"
GATEWAY_HEALTH="http://127.0.0.1:18790/health"
GRAFANA_PACKAGE="mcp-grafana@1.6.2"

cd "$APP_DIR"

step() { printf '\n==> %s\n' "$*"; }
die()  { printf '!! %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请以 root 运行（supervisorctl 与 /usr/bin 软链需要）"

step "1/7 拉取代码"
git pull --ff-only
[ -f nanobot/webui/grafana_api.py ] \
  || die "拉取后仍没有 grafana_api.py：本地还没 push？push 后重跑本脚本"
git log --oneline -1

step "2/7 新代码导入自检"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)" || die "找不到 python（.venv/bin/python 或 python3）"
"$PY" -c "import nanobot.webui.grafana_api, nanobot.agent.tools.mcp_confirm" \
  || die "新模块导入失败——若为非 editable 安装请先执行: $PY -m pip install -e ."

step "3/7 构建 WebUI"
npm --prefix webui run build

step "4/7 报表状态库备份 + flag 迁移（幂等）"
STATE_DB="$HOME/.nanobot/reports/state.db"
if [ -f "$STATE_DB" ]; then
  cp "$STATE_DB" "${STATE_DB}.bak-$(date +%Y%m%d-%H%M%S)"
  echo "   已备份 ${STATE_DB}.bak-*"
fi
"$PY" -m nanobot reports policy migrate-flags

step "5/7 安装 uv（Grafana 前置；已装则跳过）"
if ! command -v uvx >/dev/null 2>&1; then
  # 经 pip 从 PyPI 安装 uv（与项目依赖同源同信任级别），
  # 装进网关的 venv，再软链到系统 PATH 供网关子进程解析。
  "$PY" -m pip install --quiet uv
  VENV_BIN="$(cd "$(dirname "$PY")" && pwd)"
  # supervisor 服务进程的 PATH 通常极简（常不含 /usr/local/bin）——
  # 双保险：常规位置 + /usr/bin 软链，保证网关子进程总能找到 uvx。
  ln -sf "$VENV_BIN/uvx" /usr/local/bin/uvx
  ln -sf "$VENV_BIN/uv"  /usr/local/bin/uv
  ln -sf "$VENV_BIN/uvx" /usr/bin/uvx
  ln -sf "$VENV_BIN/uv"  /usr/bin/uv
fi
UVX="$(command -v uvx || true)"
[ -n "$UVX" ] || UVX=/usr/bin/uvx
echo "   $UVX ($("$UVX" --version))"

step "6/7 预热 ${GRAFANA_PACKAGE}（下载进缓存，首次约几十秒）"
"$UVX" "${GRAFANA_PACKAGE}" --help >/dev/null
echo "   预热完成"

step "7/7 重启服务 + 健康检查"
SERVICE="${1:-}"
if [ -z "$SERVICE" ]; then
  SERVICE="$(supervisorctl status | awk '$1 ~ /nanobot/ {print $1; exit}')"
fi
if [ -z "$SERVICE" ]; then
  supervisorctl status || true
  die "无法自动探测服务名：bash scripts/update-server.sh <服务名>"
fi
echo "   服务：$SERVICE"
supervisorctl restart "$SERVICE"
sleep 5
supervisorctl status "$SERVICE"
if curl -fsS --max-time 5 "$GATEWAY_HEALTH"; then
  echo "   /health OK"
else
  echo "⚠ /health 未就绪（可能仍在启动）：稍后手动 curl $GATEWAY_HEALTH"
fi

cat <<'EOF'

✅ 更新完成。Grafana 下一步：
  1. 打开服务器 WebUI → Settings → Grafana → Add connection
     （token 用服务器侧新建的 Service Account：读=Viewer 角色，写=Editor 及以上）
  2. Test before saving 应返回身份/org → Save（热重载生效，无需再重启）
  3. 写模式：编辑连接勾选写工具；聊天触发写操作后，需在确认卡上点"确认执行"
EOF
