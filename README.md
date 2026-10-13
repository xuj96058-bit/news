# news

金融新聞收集與影響分析：全球宏觀、美股、港股／中國新聞，以及標普 500＋納斯達克 100 公司申報。

RSS 抓取不需要 LLM API key。先保存新聞，只有明確啟動分析時才呼叫你設定的模型。

## 安裝

使用 Python 3.10 或以上版本，在專案根目錄執行：

```bash
pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell 可使用 `Copy-Item .env.example .env`。真實密鑰和 SEC 聯絡資料只填入本機 `.env`；它與 `data/` 已被 Git 忽略。`.gitignore` 不能撤回已公開的內容，也不能阻止強制加入檔案，提交前仍應檢查 diff。

## 抓取新聞

```bash
# 抓取 18 個已設定來源，預設不呼叫 AI
python -m src.rss_pipeline

# 列出來源 ID，或只抓指定來源
python -m src.rss_pipeline --list-sources
python -m src.rss_pipeline --source cnbc_top --source rthk_finance

# 抓取後匯出新聞庫
python -m src.rss_pipeline --export data/news.jsonl
```

設定檔是 `config/news_sources.json`。預設只收 feed 中最近 30 天的項目，每個來源最多 50 則；無日期項目會保留並標示日期未知。用 `--max-age-days 0` 關閉日期篩選，`--limit-per-source` 調整上限。這不是歷史新聞回補：來源 feed 本身可能只保留最近數十條。

| 類型 | 已設定來源 |
| --- | --- |
| 官方公告／數據 | Fed 貨幣政策、BLS CPI／就業、BEA、ECB、EIA、國家統計局、人民銀行、港交所、香港金管局 |
| 媒體報道 | CNBC 頭條／財報／經濟、BBC Business、RTHK 財經、FT Global Economy |
| 公司官方新聞 | NVIDIA、Apple |

除了金管局使用官方 JSON API，其餘使用 RSS／Atom。抓取只取得來源實際提供的標題、摘要、內文片段及連結，不追蹤原文頁面、不繞過付費牆，也不把 feed 內容標作完整文章。

單一來源失敗不會中斷其他來源。CLI 回傳 0 表示成功、2 表示部分來源、覆蓋範圍或分析失敗、1 表示所有選定來源均失敗且沒有保存項目。403 不會重試；暫時性網路／伺服器錯誤最多重試一次。來源可能限制自動存取，例如 BLS 的 feed 在部分環境會回傳 403。

每次命令執行一輪後退出；沒有默默建立背景排程。

## 標普 500＋納斯達克 100 的 SEC 公告

在本機 `.env` 設定自己的應用名稱和真實聯絡 email，例如：

```text
SEC_USER_AGENT=my-news-reader/1.0 contact 你的聯絡email
```

然後執行：

```bash
# 更新兩個指數的追蹤名單及 SEC 官方 CIK 對照，完成後退出
python -m src.rss_pipeline --refresh-sec-universe

# 只抓追蹤公司 SEC 公告
python -m src.rss_pipeline --sec-only

# 一起抓新聞來源和 SEC 公告
python -m src.rss_pipeline --sec
```

名單 `config/sec_universe.json` 記錄來源、取得時間、原始頁面雜湊及合併結果。指數成分取自標示清楚的 Wikipedia 公開成分表，CIK 則全部對照 SEC 官方 `company_tickers.json`；這不是指數供應商授權或核驗的生效日成分名單。名單超過七日、成分不完整或有股票未映射時，SEC 抓取會要求先更新，避免假裝完整覆蓋。

SEC 收集 `8-K、10-Q、10-K、6-K、20-F` 及其修訂。先分頁取得公告，再按兩個指數合併後的 CIK 篩選，重疊公司只追蹤一次。請求上限為每秒 5 次；遇到 403／429 即停止其餘 SEC 請求。

`--sec-max-pages` 預設每種表單最多 10 頁，支援 1–100。達到上限會標示 `partial`，不宣稱完整覆蓋。SEC current feed 只有有限保留視窗，無法保證回補兩次執行間的所有公告。這裡保存的是申報通知與原文連結，**不是財報全文**；通知不會自動送往 AI 分析。

官方說明：[SEC RSS](https://www.sec.gov/about/rss-feeds)、[SEC 自動存取規範](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)。

## 去重、保存與 AI 分析

新聞保存於專案下 `data/news.sqlite3`，包含發布時間、取得時間、來源類型、原文連結及 feed 提供的完整片段。

- 先按去除追蹤參數的 URL 去重，再合併同日標題及正文片段完全相同的跨來源副本。
- 保留所有來源；不會僅憑相似標題把不同事件合併。跨語言或不同寫法的同一事件仍可能分開。
- 已分析且內容未變的項目不會再呼叫模型；來源內容更新後才重新排入分析。
- 未嘗試的新聞優先於失敗重試；只有標題或 SEC 申報通知的項目暫不分析。
- 官方公告、媒體報道和公司新聞保留各自來源標籤，不將媒體報道當成官方結論。

```bash
# 只分析本機佇列，最多 10 則；需要本機 LLM key，會產生 API 用量
python -m src.rss_pipeline --analysis-only --analysis-limit 10

# 抓新聞後分析，最多 5 則
python -m src.rss_pipeline --analyze --analysis-limit 5
```

`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`LLM_MODEL` 設定相容服務。範例指向 DeepSeek，實際服務／模型是否可用取決於你的帳號及設定。模型的置信度是自評，不能視為股價預測成功率。分析器也會讀本機指數追蹤名單，避免只保留原本少量美股代碼。

## 網頁、Investat 與日報

```bash
streamlit run app.py
python -m src.pipeline
python -m src.query "貼上新聞內容"
python -m src.report
```

網頁的「新聞來源」分頁可選來源、抓新聞、刷新 SEC 名單、抓 SEC 通知及明確啟動 AI 分析。

Investat 原有批次流程仍需 `INVESTAT_MCP_TOKEN` 和 LLM key。原有手動／Investat 分析保存在 `data/events.jsonl`；日報會一起讀取已完成的 RSS 分析。日報目前仍按全部已保存分析的置信度篩選，沒有按當日日期限制；港股／A 股標的映射也尚未完整覆蓋。

`data/feed_status.json` 保存新聞來源抓取狀態，SEC 單獨執行時使用 `data/sec_status.json`。新增收集流程的錯誤紀錄只保留安全分類，不保存 LLM 回應中的原始錯誤文字或密鑰。

## 測試

```bash
python -m unittest discover -s tests -v
```

單元測試使用固定資料和模擬請求，不呼叫付費模型，也不依賴新聞網站即時可用。
