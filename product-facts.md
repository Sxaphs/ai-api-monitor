# 产品事实核查 · AI API 竞品监测系统

核查日期：2026-11-12

## 已确认事实

- 现有系统为本地 FastAPI + SQLite 监测台，已有 `/changes`、`/review` 和 targets JSON API。
- 当前种子对象共 26 个：A 组 8、B 组 8、C 组 4、D 组 6。
- 当前数据库有 25 条快照、2 条有效变动记录；两条均属于 **A 组 OpenRouter**，来自官方 X 账号 `@OpenRouter`，检测时间均为北京时间 2026-11-12 22:59。
- 已入库原文链接：
  - Gemini Decision 1 公告：https://x.com/OpenRouter/status/2120583084005605816
  - MAI Transcribe 2 使用量公告：https://x.com/OpenRouter/status/2120569774124200265
- OpenRouter 官方品牌资产页确认存在，提供横版、竖版和 glyph 的 SVG/PNG：https://openrouter.ai/brand

## 设计使用边界

- 视觉初稿只使用上述真实数据库内容，不虚构价格、营销或预警数量。
- 初稿中的“暂无已入库价格变动”仅描述当前数据库，不代表竞品本月没有价格变化。
- 不把 OpenRouter 的品牌色扩展成整个监测系统的品牌色；其 logo 只用于标识对应变动记录。
