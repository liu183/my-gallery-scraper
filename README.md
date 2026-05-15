# Image Gallery Scraper → Feishu Notifier

GitHub Actions 驱动的图集自动化抓取 + 飞书推送系统。

## 架构

```
4 个 scraper workflow (每小时各跑一次)
        ↓ 每次下载 1 个图集
   [GitHub Artifacts]  (保留 90 天)
        ↓
notify workflow (每 5 小时跑一次)
        ↓ 取最早 3 个未发送的图集
  → 按 site 选对应飞书 Bot
  → 发送 [卡片] + 逐张 [高清图]
        ↓
   state/sent.json (标记已发送)
   state/failed.json (记录失败)
```

## 站点 & 调度

| 站点 | Workflow | Cron | 策略 |
|------|----------|------|------|
| everiaclub.com | `scrape-everiaclub` | 每小时 :05 | 首页最新未处理帖子 |
| bestgirlsexy.com | `scrape-bestgirlsexy` | 每小时 :15 | japan / china 分类轮替 |
| eropuru.com | `scrape-eropuru` | 每小时 :25 | 依次遍历 zyoyu 索引中的女优 |
| geinou-nude.com | `scrape-geinou-nude` | 每小时 :35 | 首页最新未处理帖子 |
| 飞书推送 | `notify-feishu` | 每 5 小时 :45 | 取最早 3 个未发送图集 |

单图集图片上限：**500 张**。

## 必需 Secrets（4 站点 × 4 字段）

去 `Settings → Secrets and variables → Actions → New repository secret` 添加：

```
FEISHU_EVERIACLUB_APP_ID
FEISHU_EVERIACLUB_APP_SECRET
FEISHU_EVERIACLUB_WEBHOOK
FEISHU_EVERIACLUB_WEBHOOK_SECRET     (可选，若启用签名校验)

FEISHU_BESTGIRLSEXY_APP_ID
FEISHU_BESTGIRLSEXY_APP_SECRET
FEISHU_BESTGIRLSEXY_WEBHOOK
FEISHU_BESTGIRLSEXY_WEBHOOK_SECRET   (可选)

FEISHU_EROPURU_APP_ID
FEISHU_EROPURU_APP_SECRET
FEISHU_EROPURU_WEBHOOK
FEISHU_EROPURU_WEBHOOK_SECRET        (可选)

FEISHU_GEINOU_NUDE_APP_ID
FEISHU_GEINOU_NUDE_APP_SECRET
FEISHU_GEINOU_NUDE_WEBHOOK
FEISHU_GEINOU_NUDE_WEBHOOK_SECRET    (可选)
```

### 怎么拿这些凭据

1. **App ID / App Secret**
   - 飞书开放平台 https://open.feishu.cn/app
   - 创建「企业自建应用」→ 凭证与基础信息
   - 权限管理里加上 `im:resource`（上传图片）和 `im:message`（发送消息）

2. **Webhook URL**
   - 目标飞书群 → 设置 → 群机器人 → 添加机器人 → 「自定义机器人」
   - 复制 webhook 地址；如启用签名校验，签名密钥就是 `WEBHOOK_SECRET`

3. **把应用拉进群**
   - 在飞书群里 → 设置 → 群成员 → 添加机器人 → 选你创建的应用
   - 这样应用上传的 image_key 在该群内才可见

## 部署步骤

```bash
# 1. 创建 GitHub 仓库（建议 private）
gh repo create my-gallery-scraper --private --source . --push

# 2. 添加上面 16 个 secrets

# 3. 手动触发首次运行，确认能跑通
gh workflow run scrape-everiaclub.yml
gh workflow run notify-feishu.yml
```

## 项目结构

```
.
├── .github/workflows/
│   ├── everiaclub.yml
│   ├── bestgirlsexy.yml
│   ├── eropuru.yml
│   ├── geinou-nude.yml
│   └── notify.yml
├── scrapers/
│   ├── common.py            # 限速 / 重试 / UA轮换 / 下载 / manifest
│   ├── everiaclub.py
│   ├── bestgirlsexy.py
│   ├── eropuru.py
│   └── geinou_nude.py
├── notify/
│   ├── feishu_sender.py     # 飞书 API 客户端（auth + 上传 + 发送）
│   └── dispatch.py          # 主调度逻辑
├── state/
│   ├── everiaclub.json      # 每站点处理过的 URL 列表
│   ├── bestgirlsexy.json
│   ├── eropuru.json
│   ├── geinou_nude.json
│   ├── sent.json            # 已发送 artifact ID 列表
│   └── failed.json          # 失败记录
├── requirements.txt
└── README.md
```

## 行为细节

- **断点 & 去重**：每次 scraper 跑完会把处理过的 URL 写入 `state/*.json`，自动 commit 回主干。下一次会跳过已处理的。
- **失败处理**：单图集发送失败 → 记入 `failed.json`，**不会**加入 `sent.json`，下次仍会再次尝试。
- **重试上限**：HTTP 请求自动重试 3-4 次（指数退避）。
- **限速**：HTML 请求间隔 1-2s，图片下载间隔 0.4-1s，飞书图片消息间隔 1s。
- **超时**：scraper 每次 30 分钟，notify 每次 120 分钟。
- **图片上限**：单图集超过 500 张会截断（`manifest.truncated=true`）。

## 调整 HTML 选择器（重要）

各站点的 `scrapers/*.py` 中的 CSS 选择器基于常见 WordPress 模板编写。如果某站点用了自定义主题，可能需要调整：

- `list_post_urls()` / `list_actresses()` / `list_galleries()`：列表页选择器
- `extract_gallery()`：详情页图片选择器

调试方法：本地跑 `python -m scrapers.everiaclub`，看 `[info] images=N` 是否合理。

## 本地调试

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m scrapers.everiaclub
ls output/
```

## 注意事项

- 抓取的内容须遵守目标站点 ToS 及当地法律法规。
- 建议仓库设为 **private**，不要把图集内容暴露给公网。
- GitHub Actions 私有仓库免费额度：2000 分钟/月。本套工作流粗略估算月耗 ~600-900 分钟，在额度内。
- Artifact 保留 90 天后自动过期；如果某图集 90 天内未被 notify 触达，将永久丢失。
