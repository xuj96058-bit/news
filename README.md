# news

金融新聞分析工具 — 面向「金融分析師水平」的可解釋新聞影響分析。

已接入 **Investat 金融信息流**（MCP），可拉取机构研究报告并生成结构化分析师结论。

## 重要：API Key 不会上传到 GitHub

- 所有密钥（DeepSeek、OpenRouter、Investat MCP Token）**只存在你本機的 `.env` 文件**
- `.env` 已加入 `.gitignore`，**绝不会**被提交到仓库
- 仓库里只有 `.env.example`（占位符），没有任何真实 key
- 程序启动时会自动读取本機 `.env`

## 目标

不是堆关键词或黑箱分数，而是能说清楚：

- 这则新闻影响谁
- 为什么
- 幅度多大
- 置信度多高

每个结论都有依据（事实、传导路径、来源）。

## 核心功能

1. **用户查询**：贴上新闻 → 结构化分析师结论
2. **自动报告**：定时从 Investat 拉取、去重、只展开高置信度事件
3. **可解释**：事件类型、受影响标的、方向、传导路径、理由、置信度
4. **标的校验**：中英实体映射 + 后处理，避免 LLM 乱编标的

## 架构

```
Investat MCP ──→ list_articles / get_article ──→ LLM 分析器 ──→ 后处理校验 ──→ 存储 + 日报
用户贴上新闻 ──→ 同上
```

## 目前进度

- [x] 查询核心模块
- [x] 标的映射 + 后处理校验
- [x] Investat MCP 客户端
- [x] 管道：拉取 → 分析 → 去重存储 → 日报
- [x] Streamlit 前端
- [x] 自动读取本机 `.env`（密钥不上传）

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env
# 用编辑器打开 .env，填入你的真实 key（不会被提交）
```

`.env` 范例：
```
OPENAI_API_KEY=sk-你的DeepSeek_key
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-pro
INVESTAT_MCP_TOKEN=ift_你的令牌
```

### 从 Investat 拉取并分析
```bash
python -m src.pipeline
```

### 命令列查询
```bash
python -m src.query "贴上新闻内容"
```

### 前端
```bash
streamlit run app.py
```

### 日报
```bash
python -m src.report
```

## 文件结构

- `src/analyzer.py` — 核心分析 + 映射 + 校验
- `src/investat.py` — Investat MCP 客户端
- `src/pipeline.py` — 拉取 + 分析 + 存储管道
- `src/collector.py` — 去重存储
- `src/report.py` — 日报生成
- `src/env.py` — 自动载入本机 `.env`
- `app.py` — Streamlit 前端

**记得：只要把 key 写在本机 `.env`，GitHub 仓库就会一直保持干净。**
