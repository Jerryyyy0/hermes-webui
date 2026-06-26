---
name: hot-news-top5
description: "获取今日热点新闻Top5 — 使用Tavily搜索引擎检索当天最热门的5条新闻，含标题、摘要、来源和链接"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [news, search, daily, information]
    related_skills: []
---

# 今日热点新闻 Top5

## 概述

快速获取当天（或最近）最热门的5条新闻。使用 Tavily 搜索 API（通过 `mcp_tavily_search_tavily_search` 工具），以 `topic='news'` 模式检索，自动聚合并呈现 Top5 热门新闻。

## 何时使用

- 用户说「今天有什么热点新闻」、「看看新闻」、「今天的热点」
- 用户问「最近发生了什么」、「今天有什么大事」
- 每天早上例行查看新闻简报
- 用户要求搜索特定领域的今日热点（科技、财经、体育等）

## 工作流程

### Step 1: 理解用户场景

首先确认用户的场景偏好：
- **默认**：综合热点（涵盖时政、社会、科技、财经、体育等）
- **领域限定**：用户可能指定「科技热点」、「财经热点」、「体育热点」等
- **语言偏好**：中文用户用中文搜索词，英文用户用英文搜索词

> 注意：本技能默认面向中文用户，使用中文搜索词，输出中文结果。

### Step 2: 使用 Tavily 搜索新闻

使用 `mcp_tavily_search_tavily_search` 工具，参数如下：

```json
{
  "query": "今日热点新闻",
  "topic": "news",
  "search_depth": "advanced",
  "max_results": 10,
  "days": 1
}
```

**搜索词策略（根据场景选择）：**
| 场景 | 搜索词 |
|------|--------|
| 综合热点 | "今日热点新闻" |
| 科技 | "今日科技热点" 或 "tech news today" |
| 财经 | "今日财经热点" 或 "financial news today" |
| 体育 | "今日体育热点" 或 "sports news today" |
| 国际 | "今日国际新闻" 或 "world news today" |
| AI/科技 | "AI news today" 或 "人工智能 今日热点" |

**参数说明：**
- `topic='news'` — 必须，启用新闻搜索模式
- `search_depth='advanced'` — 深度搜索，获取更完整内容
- `max_results=10` — 获取10条结果以筛选Top5
- `days=1` — 搜索最近1天的新闻

### Step 3: 可选 — 提取详细内容

对于特别重要的新闻，可以调用 `mcp_tavily_search_tavily_extract` 提取原文内容以提供更详细的摘要：

```json
{
  "urls": ["结果URL"],
  "extract_depth": "basic"
}
```

仅在以下情况使用：
- 标题不够明确，需要更多上下文
- 用户要求「详细说说某条新闻」
- 结果总数少于5条时需要补充

### Step 4: 整理并呈现 Top5

从搜索结果中挑选最重要的5条，按热度/重要性排序。呈现格式如下：

---

## 📰 今日热点新闻 Top5 — 2026年6月25日

### 1️⃣ [标题]
**来源：** 媒体名称
**摘要：** 新闻摘要内容（1-2句话）
**链接：** URL

### 2️⃣ [标题]
**来源：** 媒体名称
**摘要：** 新闻摘要内容（1-2句话）
**链接：** URL

...（以此类推至第5条）

---

**呈现原则：**
- 保持简洁，每条新闻不超过3行描述
- 包含来源以增加可信度
- 链接可点击（Markdown格式）
- 如用户需要，可进一步深入某条新闻

### Step 5: 可选 — 保存到文件

如果用户要求「保存到文件」或「整理成文件」，保存到当前工作目录：

```
今日热点新闻_YYYY-MM-DD.md
```

格式保持与 Step 4 一致，添加文件头说明。

## 高级用法

### 多角度搜索

对同一热点话题，可以用不同搜索词从多个角度获取信息：

1. 先搜综合热点找到今天的重大新闻
2. 对其中1-2条特别重要的，用针对性搜索词获取更详细信息
3. 合并呈现

### 定时推送

如需每天早上自动推送新闻简报，配合 `cronjob` 技能使用：

```
cronjob action=create name="每日新闻简报" schedule="0 8 * * *" prompt="获取今日热点新闻Top5并整理成简报形式"
```

## 示例

### 用户：今天有什么热点新闻？

1. 调用 `mcp_tavily_search_tavily_search(query="今日热点新闻", topic="news", search_depth="advanced", max_results=10, days=1)`
2. 筛选Top5
3. 按格式呈现

### 用户：最近科技圈有什么大事？

1. 调用 `mcp_tavily_search_tavily_search(query="科技 新闻 今日", topic="news", search_depth="advanced", max_results=10, days=3)`
2. 筛选Top5
3. 按格式呈现

## 注意事项

- **时效性**：Tavily 的 `days` 参数控制搜索时间范围。日常使用 `days=1`，周末或节假日可用 `days=3` 覆盖更多内容
- **结果不全**：如果搜索结果少于5条，应如实呈现结果数量，并考虑更换搜索词重新搜索
- **中文优化**：搜索词使用中文可获得更好的中文新闻覆盖。如需英文新闻，使用英文搜索词
- **来源多样性**：尽量选择不同来源的新闻，避免同一事件的多条报道占据多个位置
- **无结果处理**：如果 Tavily 未返回结果（工具不可用或网络问题），告知用户并建议使用 `web_search` 工具作为备选
