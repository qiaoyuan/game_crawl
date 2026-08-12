#!/bin/bash
# ==============================================
# G2G 爬虫 - 生产部署脚本
# 本机运行: ./deploy_prod.sh
# 部署到:   43.106.27.46:/www/wwwroot/game_crawl
# SSH 用户默认 root，可覆盖: SSH_USER=xxx ./deploy_prod.sh
# ==============================================
set -euo pipefail

SERVER_IP="43.106.27.46"
SSH_USER="${SSH_USER:-root}"
REMOTE_DIR="/www/wwwroot/game_crawl"
REMOTE_HOST="$SSH_USER@$SERVER_IP"

echo ">>> [1/5] 上传代码到 $REMOTE_HOST:$REMOTE_DIR"
mkdir -p logs
rsync -az --delete \
  --exclude 'venv/' \
  --exclude 'downloads/' \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  --exclude 'session.json' \
  -e ssh ./ "$REMOTE_HOST:$REMOTE_DIR/"

echo ">>> [2/5] 服务器端安装 Python 依赖 + Playwright Chromium"
ssh "$REMOTE_HOST" "bash -s" << 'REMOTE_SCRIPT'
set -euo pipefail
cd /www/wwwroot/game_crawl
mkdir -p logs

# 1. Python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "[*] 未找到 python3，开始安装..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip
  else
    echo "[x] 无法自动安装 python3，请手动安装后重试"; exit 1
  fi
fi
echo "[✓] $(python3 --version)"

# 2. 虚拟环境 + 依赖
[ -d venv ] || python3 -m venv venv
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 3. Playwright Chromium（含系统依赖，需 root，耗时较长）
echo "[*] 安装 Playwright Chromium..."
python -m playwright install --with-deps chromium

echo "[✓] 依赖安装完成"
REMOTE_SCRIPT

echo ">>> [3/5] 验证配置加载"
ssh "$REMOTE_HOST" "cd $REMOTE_DIR && source venv/bin/activate && python -c \"from g2g import config; print('APP_ENV:', config.APP_ENV, '| DB:', config.DB_HOST, '| headless:', config.BROWSER_HEADLESS, '| channel:', config.BROWSER_CHANNEL)\""

echo ">>> [4/5] 清理本机缓存"
rm -rf __pycache__ g2g/__pycache__ tools/__pycache__

echo ">>> [5/5] 部署完成"
cat << 'HELP'

部署完成！后续使用：
  1) 手动测试:  ssh root@43.106.27.46 "cd /www/wwwroot/game_crawl && source venv/bin/activate && python -m tools.crawl_from_db"
  2) 定时任务（宝塔计划任务 或 crontab -e）:
     */10 * * * * cd /www/wwwroot/game_crawl && /www/wwwroot/game_crawl/venv/bin/python -m tools.crawl_from_db >> /www/wwwroot/game_crawl/logs/crawl.log 2>&1

提示：脚本会多次要求输入 SSH 密码。推荐先配置免密登录:
  ssh-copy-id root@43.106.27.46
HELP
