# news

金融新聞分析工具 — 面向「金融分析師水平」的可解釋新聞影響分析。

## 目標

不是堆關鍵詞或黑箱分數，而是能說清楚：

- 這則新聞影響誰
- 為什麼
- 幅度多大
- 置信度多高

每個結論都有依據（事實、傳導路徑、來源）。

## 核心功能

1. **用戶查詢**：貼上新聞 → 結構化分析師結論
2. **自動報告**：定時收集、去重、只展開高置信度事件
3. **可解釋**：事件類型、受影響標的、方向、傳導路徑、理由、置信度
4. **標的校驗**：中英實體映射 + 後處理，避免 LLM 亂編標的

## 架構（最小可用）

```
用戶輸入新聞 ──→ 事件抽取（LLM）──→ 影響推理 ──→ 後處理校驗 ──→ 結構化結論
                ↑
系統定時收集 ──→ 去重 ──→ 同上 ──→ 日報 / 儀表板
```

## 目前進度

- [x] 查詢核心模組（LLM 抽取 + 影響推理 + 結構化輸出）
- [x] 加強標的映射（中英實體 → ticker，可擴展詞典）
- [x] 後處理與校驗（避免 LLM 亂編標的）
- [x] 自動收集 + 去重 + 日報骨架
- [x] 簡單前端（Streamlit 查詢框 + 日報頁）

## 快速開始

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=your_key   # 或兼容的 endpoint
```

### 命令列查詢
```bash
python -m src.query "貼上新聞內容"
```

### 前端
```bash
streamlit run app.py
```

### 日報
```bash
python -m src.report
```

## 檔案結構

- `src/analyzer.py` — 核心分析 + 映射 + 校驗
- `src/collector.py` — 收集與去重骨架
- `src/report.py` — 日報生成
- `app.py` — Streamlit 前端

詞典與校驗邏輯在 `analyzer.py` 的 `ENTITY_TO_TICKER` 與 `post_process_impacts`，可按需擴充。
