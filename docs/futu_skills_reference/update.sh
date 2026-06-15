#!/usr/bin/env bash
# update.sh — 同步富途官方 Skill 文档到本地备份
# 用途：定期抓取富途官方 Skill 安装/接入指引，防止官方页面变动导致信息丢失。
# 运行：bash update.sh   或加入 crontab：  0 9 * * 1  cd /path/to/stock-quant/docs/futu_skills_reference && bash update.sh

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
TS="$(date +%Y%m%d_%H%M%S)"
ARCHIVE="${DIR}/archive/${TS}"
mkdir -p "${ARCHIVE}"

declare -a URLS=(
  "https://www.futunn.com/skills/futu-install.md|futu-install.md"
  "https://openapi.futunn.com/futu-api-doc/intro/ai.html|openapi-intro-ai.html"
  "https://www.futunn.com/skillhub|skillhub.html"
)

echo "[$(date)] 开始同步富途 Skill 文档..."

for entry in "${URLS[@]}"; do
  url="${entry%%|*}"
  fname="${entry##*|}"
  echo "  -> ${fname}  <-  ${url}"
  if curl -fsSL --max-time 30 \
       -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36" \
       "${url}" -o "${DIR}/${fname}.tmp"; then
    # 覆盖最新版
    mv "${DIR}/${fname}.tmp" "${DIR}/${fname}"
    # 同时归档一份带时间戳
    cp "${DIR}/${fname}" "${ARCHIVE}/${fname}"
    echo "     OK  ($(wc -c < "${DIR}/${fname}" | tr -d ' ') bytes)"
  else
    rm -f "${DIR}/${fname}.tmp"
    echo "     FAIL"
  fi
done

# 仅保留最近 12 份归档
if [ -d "${DIR}/archive" ]; then
  count=$(ls -1 "${DIR}/archive" | wc -l | tr -d ' ')
  if [ "${count}" -gt 12 ]; then
    ls -1t "${DIR}/archive" | tail -n +13 | while read -r old; do
      rm -rf "${DIR}/archive/${old}"
      echo "  清理过期归档: ${old}"
    done
  fi
fi

echo "[$(date)] 完成。"
