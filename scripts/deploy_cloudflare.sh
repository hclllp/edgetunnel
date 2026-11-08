#!/usr/bin/env bash
# 部署脚本（工作流 B 调用）：将 generate_obfus 分支上的 _worker_obfus.js 部署到 Cloudflare。
#
# 需要的环境变量（由 GitHub Environment secrets 注入）：
#   CLOUDFLARE_ACCOUNT_ID     Cloudflare 账户 ID
#   CLOUDFLARE_API_TOKEN      Cloudflare API Token（Workers 编辑 + KV 编辑 + Zone 读取权限）
#   CLOUDFLARE_INSTANCE_NAME  Worker 实例名
#   UUID                      uuid 运行变量值
#   PASSWORD                  admin 运行变量值
#   CLOUDFLARE_DOMAIN         自定义域名
#
# 幂等语义：实例不存在则创建、已存在则更新；KV 命名空间按标题复用；
# secrets 每次覆盖写入；自定义域名重复绑定不报错。任何一步“值未改变”时直接继续。
set -euo pipefail

: "${CLOUDFLARE_ACCOUNT_ID:?环境变量 CLOUDFLARE_ACCOUNT_ID 未设置}"
: "${CLOUDFLARE_API_TOKEN:?环境变量 CLOUDFLARE_API_TOKEN 未设置}"
: "${CLOUDFLARE_INSTANCE_NAME:?环境变量 CLOUDFLARE_INSTANCE_NAME 未设置}"
: "${UUID:?环境变量 UUID 未设置}"
: "${PASSWORD:?环境变量 PASSWORD 未设置}"
: "${CLOUDFLARE_DOMAIN:?环境变量 CLOUDFLARE_DOMAIN 未设置}"

export CLOUDFLARE_API_TOKEN
export CLOUDFLARE_ACCOUNT_ID

ACCOUNT_ID="$CLOUDFLARE_ACCOUNT_ID"
INSTANCE="$CLOUDFLARE_INSTANCE_NAME"
DOMAIN="$CLOUDFLARE_DOMAIN"
API="https://api.cloudflare.com/client/v4"
CFG="wrangler.ci.toml"

echo "==> 部署账户 $ACCOUNT_ID / 实例 $INSTANCE"

# 1) KV 命名空间：按标题复用已存在的，否则新建
KV_ID=$(wrangler kv namespace list --json 2>/dev/null | python3 -c "
import json, sys
ns = json.load(sys.stdin)
m = next((x for x in ns if x.get('title') == 'KV'), None)
print(m['id'] if m else '')
")
if [ -z "$KV_ID" ]; then
  echo "==> 新建 KV 命名空间 KV"
  KV_ID=$(wrangler kv namespace create KV --json | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
fi
echo "==> KV 命名空间 ID: $KV_ID"

# 2) workers.dev 子域检测：未注册时关闭 workers_dev（实例仍可通过自定义域名访问）
SUBDOMAIN=$(curl -fsS "$API/accounts/$ACCOUNT_ID/workers/subdomain" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  | python3 -c "import json,sys;r=json.load(sys.stdin).get('result') or {};print(r.get('subdomain') or '')" 2>/dev/null || true)
WORKERS_DEV=false
if [ -n "$SUBDOMAIN" ]; then
  WORKERS_DEV=true
  echo "==> workers.dev 子域: $SUBDOMAIN.workers.dev"
else
  echo "==> 账户未注册 workers.dev 子域，实例仅通过自定义域名访问"
fi

# 3) 生成 CI 用 wrangler 配置（binding 名称 KV 不可修改）
cat > "$CFG" <<EOF
name = "$INSTANCE"
main = "_worker_obfus.js"
compatibility_date = "2025-11-04"
account_id = "$ACCOUNT_ID"
keep_vars = true
workers_dev = $WORKERS_DEV

[[kv_namespaces]]
binding = "KV"
id = "$KV_ID"
EOF

# 4) 部署（实例不存在则创建，存在则更新）
wrangler deploy --config "$CFG"

# 5) 运行变量/密钥：uuid / admin（每次覆盖写入，值相同即无变化）
printf '%s' "$UUID" | wrangler secret put uuid --config "$CFG" >/dev/null
printf '%s' "$PASSWORD" | wrangler secret put admin --config "$CFG" >/dev/null
echo "==> 已设置运行密钥 uuid / admin"

# 6) 绑定自定义域名（幂等；失败仅告警不阻断）
ROOT_DOMAIN=$(python3 -c "
import sys
parts = sys.argv[1].rstrip('.').split('.')
print('.'.join(parts[-2:]) if len(parts) >= 2 else parts[0])
" "$DOMAIN")
ZONE_ID=$(curl -fsS "$API/zones?name=$ROOT_DOMAIN" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  | python3 -c "import json,sys;z=json.load(sys.stdin).get('result') or [];print(z[0]['id'] if z else '')" 2>/dev/null || true)
if [ -n "$ZONE_ID" ]; then
  if curl -fsS -X PUT "$API/accounts/$ACCOUNT_ID/workers/domains" \
      -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"zone_id\":\"$ZONE_ID\",\"hostname\":\"$DOMAIN\",\"service\":\"$INSTANCE\",\"environment\":\"production\"}" \
      >/dev/null 2>&1; then
    echo "==> 已绑定自定义域名: $DOMAIN"
  else
    echo "::warning::自定义域名 $DOMAIN 绑定失败（检查账户计划与 Token 权限），已跳过"
  fi
else
  echo "::warning::未找到域名 $ROOT_DOMAIN 对应的 Zone（Token 需 Zone Read 权限），已跳过域名绑定"
fi

# 7) 打印访问链接
if [ "$WORKERS_DEV" = true ]; then
  echo "::notice::Worker 链接: https://$INSTANCE.$SUBDOMAIN.workers.dev"
fi
echo "::notice::自定义域名链接: https://$DOMAIN"
