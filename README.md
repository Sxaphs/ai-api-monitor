# AI API 中转站竞品监测系统（第一期）

FastAPI + SQLite 的竞品动态采集、预警核对和手动 Markdown 周报/月报系统。采集失败按对象隔离；第三方监控/榜单平台仅保存配置，上游供应商本期不采官网定价页。

## 功能

- 正规军竞品、野路子竞品的定价页和公开模型列表：每 6 小时采集，保留原文快照，与上一次成功且可解析的快照比较。
- 正规军竞品、上游供应商的 X 时间线：每 2 小时采集，只保留启用当天后的原创推文，不翻历史游标，按推文 ID 去重。
- 价格、模型、功能、营销分类；预告信号分级；14 天内预警与落地变动自动关联。
- `/changes` 变动筛选、`/review` 人工确认/误报；targets 的增删改和启停通过 JSON API 完成。
- `/reports` 手动生成近 7 天周报、近 30 天月报或自定义日期报告，固定五节结构，存入 `reports` 表并支持历史回看；无每日报告调度。

首次成功采集只建立基线，不产生 diff。失败快照不会成为 diff 基准；野路子竞品失败只写日志和快照，不生成预警。

## 安装

需要 Python 3.11+。在 Windows Git Bash 中：

```bash
cd /d/daily_update/ai-api-monitor
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
cp .env.example .env
```

编辑 `.env`，至少按需填写：

```dotenv
RAPIDAPI_KEY=你的_RapidAPI_密钥
SILICONFLOW_KEY=你的_SiliconFlow_密钥

# AiHubMix / SiliconFlow / API2D 定价提取使用 OpenAI 兼容协议
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=你的_LLM_密钥
LLM_MAX_CHARS=60000

DATABASE_URL=sqlite:///./data/monitor.db
SCHEDULER_ENABLED=true
LOG_LEVEL=INFO
PLAYWRIGHT_BROWSERS_PATH=./.playwright-browsers
```

没有 `RAPIDAPI_KEY` 时，X 采集会记录失败快照，不影响网站采集。没有 `LLM_API_KEY` 时，三个 LLM 定价对象和 SiliconFlow 页面兜底会明确报错；不会改用写死解析或模拟结果。密钥只从环境读取，不要写入 Python、种子数据或日志。

默认 `http` 采集不需要浏览器。把某个对象的 `collect_method` 设为 `playwright` 前，安装 Chromium：

```bash
PLAYWRIGHT_BROWSERS_PATH=./.playwright-browsers .venv/Scripts/python.exe -m playwright install chromium
```

## 初始化与启动

```bash
.venv/Scripts/python.exe -m app.cli init-db
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开：

- `http://127.0.0.1:8000/changes`：变动记录，可按分组、变动类型、信号类型筛选。
- `http://127.0.0.1:8000/review`：pending 记录人工核对。
- `http://127.0.0.1:8000/reports`：手动生成周报/月报、自定义报告和历史回看。
- `http://127.0.0.1:8000/docs`：FastAPI 自动接口文档。

Web 进程启动 APScheduler，关闭进程时停止。请只运行一个调度实例，不要用多 worker 同时启动定时任务。开发测试时可设 `SCHEDULER_ENABLED=false`。

## 手动命令

```bash
# 正规军/野路子竞品定价页和模型列表
.venv/Scripts/python.exe -m app.cli collect-sites

# 正规军竞品/上游供应商已配置 handle 的 X 账号
.venv/Scripts/python.exe -m app.cli collect-x

# 网站 + X 全量采集；单个站点失败不会改变其他站点的执行
.venv/Scripts/python.exe -m app.cli collect

# 查看各站双跑样本、95% 达标率和不一致字段
.venv/Scripts/python.exe -m app.cli migration-status

# 仅对达到 20 次成功双跑或满 7 天、且达标率 ≥95% 的站点执行切换
.venv/Scripts/python.exe -m app.cli migration-status --apply

# 近 7 天周报或近 30 天月报，打印 Markdown 并存库
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m app.cli report --days 7
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m app.cli report --days 30

# 任意时间段；不带时区的 ISO 时间按北京时间解释，结束时间不含
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m app.cli report \
  --start 2026-11-11T08:00:00 --end 2026-11-12T08:00:00

# 导入最近一个月结构化调研数据；重复执行只跳过已有记录
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m app.cli import-history \
  imports/2026-10-12_2026-11-12
```

`--days` 与自定义 `--start/--end` 二选一，自定义起止时间必须同时提供。报告固定保留五节，无数据板块写“本期无”。网页自定义日期按北京时间起日 00:00 至止日次日 00:00（结束排他）统计。

## 监测对象配置 API

种子包含全部 A/B/C/D 名称。只预填了可确认的公开地址和 OpenRouter 的实际 X handle `@OpenRouter`（早期使用的 `@OpenRouterAI` 经真实接口验证返回空响应）；不确定的 URL 与其他 X handle 留空，空字段会跳过。请先用列表接口取对象 ID，再补录实际地址。

```bash
# 查询全部对象，可用 ?group=A 或 ?enabled=true 筛选
curl http://127.0.0.1:8000/api/targets

# 新增
curl -X POST http://127.0.0.1:8000/api/targets \
  -H 'Content-Type: application/json' \
  -d '{"name":"example","group_code":"A","pricing_url":"https://example.com/pricing","collect_method":"http"}'

# 修改；空字符串会清空对应 URL/handle
curl -X PATCH http://127.0.0.1:8000/api/targets/1 \
  -H 'Content-Type: application/json' \
  -d '{"pricing_url":"https://example.com/models","models_url":"https://example.com/v1/models","x_handle":"@example"}'

# 启停
curl -X POST http://127.0.0.1:8000/api/targets/1/toggle

# 删除，同时删除该对象的历史快照和变动；日常停用优先用 toggle
curl -X DELETE http://127.0.0.1:8000/api/targets/1
```

补录或更换 X handle、重新启用对象时，会从当天开始监测，不回捞历史。模型接口认证通过 `auth_type + auth_env_key` 配置；数据库只保存环境变量名。SiliconFlow 未配置 key 或接口返回 401/403 时，自动改采公开模型中心 `https://www.siliconflow.cn/models`，API 与网页分别以 `api/webpage` 标记并使用独立 diff 基线。

## 数据和解析边界

- 默认数据库：`data/monitor.db`；原始 HTML、推文 JSON 和提取结果均在 `snapshots`。发送 LLM 前才剥离 script/style/nav/footer，不覆盖原始 HTML。
- AiHubMix、SiliconFlow、API2D 保持 `extraction_method=llm`；其余站点为 `dual`。dual 在同一 snapshot 保存 parser 与 LLM 两套结果，主入库使用 LLM，分歧字段会进入人工核对。
- 只有双跑满 20 个成功样本或满 7 天，且“结果一致或人工确认 LLM 正确”的比例 ≥95%，才通过 `migration-status --apply` 切换。普通站点优先，OpenRouter 与 302.AI 最后。
- 长正文按价格信息密度选择区块；`/changes` 底部按站点和北京时间日期展示近 7 天 LLM 调用次数与估算 token（UTF-8 字节数 / 3，仅作粗略观察；供应商 usage 原文另存 snapshot）。
- LLM 结果中每个数字必须在清洗原文中以完整数字形式逐字存在，否则对应字段置 null，相关变动进入人工核对；校验失败仍保存快照。
- 价格绝对变动幅度超过 50%，或旧值为零导致幅度不可计算时，记录强制 pending；正常网站 diff 自动 confirmed。
- 302.AI 保留 `price_data_missing`：不采登录后价格表，parser 对照侧只取口径文本，页面及报告显示“价格暂缺”说明。
- 当前仅执行双跑与逐站切换；旧解析器在全部站点稳定 LLM 运行 7 天前不会删除，第三天稳定性验收需要真实运行数据。
- `parse_ok=false` 表示已保存原文但无法可靠结构化，该快照不参与 diff；可检查页面结构后切换 Playwright 或补录正确 URL。
- 对价格使用 Decimal 计算 `(新值-旧值)/旧值×100%`。公告未披露完整数值时明确标为未披露。
- 报告变动明细排除人工误报；预警回顾只将误报计入数量。落地预警必须有关联的非误报 confirmed 记录，超过 14 天无落地记为超时，其余 pending warning 记为观察期。
- `imports/2026-10-12_2026-11-12/` 来自既有调研的最终确认区，导入时间使用来源中的事件/生效日期，来源 URL 和调研文件名保存在 `extra_json`。AICodeMirror 的充值券记录只是第三方待核对线索，不代表官方确认。

## 测试

```bash
.venv/Scripts/python.exe -m pytest -q
```

测试使用临时 SQLite 和模拟网络数据，不消耗 RapidAPI 配额，也不依赖真实竞品站点可用性。

## GitHub Pages 静态镜像

GitHub Pages 不运行 FastAPI、SQLite 或 APScheduler，因此公开站点是只读镜像，不提供在线生成、采集或人工核对操作。更新静态报告和变动记录后，将 `docs/` 一并提交：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m scripts.export_pages
```

入口为 `docs/index.html`，报告详情位于 `docs/reports/`，变动记录为 `docs/changes.html`。`.env`、`.venv/` 和 `data/` 均由 `.gitignore` 排除，不应发布密钥或数据库文件。
