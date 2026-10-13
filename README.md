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

`--analysis-limit` 目前只限制**單次命令執行**的分析數量，並非每小時額度，也不能代替下述雲端方案的持久化限額。

`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`LLM_MODEL` 設定相容服務。範例指向 DeepSeek，實際服務／模型是否可用取決於你的帳號及設定。模型的置信度是自評，不能視為股價預測成功率。分析器也會讀本機指數追蹤名單，避免只保留原本少量美股代碼。

## 網頁、Investat 與日報

```bash
streamlit run app.py
python -m src.pipeline
python -m src.query "貼上新聞內容"
python -m src.report
```

網頁的「新聞來源」分頁可選來源、抓新聞、刷新 SEC 名單、抓 SEC 通知及明確啟動 AI 分析。

左側「AI 設定」依序選擇 **AI 服務商 → AI 模型 → API key**。key 欄位以密碼方式顯示；貼上後按 Enter，再點「分析」或「AI 分析待處理新聞」。選單另有「自訂模型 ID」，可填入所選服務商的其他文字模型 ID；模型必須支援目前的文字／JSON 分析介面，是否可用仍取決於帳戶權限。

| 服務商 | 選單模型範例 | 本機環境變數 | 官方介面 |
| --- | --- | --- | --- |
| DeepSeek | `deepseek-v4-pro`、`deepseek-flash` | `DEEPSEEK_API_KEY` | [相容 API](https://api-docs.deepseek.com/quick_start/pricing/) |
| OpenAI | `gpt-6.1-sol`、`gpt-6-sol`、`gpt-6-luna` | `OPENAI_API_KEY` | [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) |
| xAI / Grok | `grok-4.7` | `XAI_API_KEY` | [JSON 輸出](https://docs.x.ai/developers/model-capabilities/text/structured-outputs) |
| Anthropic / Claude | `claude-sonnet-5`、`claude-opus-5-5`、`claude-haiku-4-5` | `ANTHROPIC_API_KEY` | [原生 Messages API](https://platform.claude.com/docs/en/api/messages/create) |
| Google / Gemini | `gemini-3.8-flash` | `GEMINI_API_KEY` | [相容 API](https://ai.google.dev/gemini-api/docs/openai) |

頁面輸入的 key 只保留在目前工作階段，優先於所選服務商的本機設定，僅送往該服務商的官方 API。不寫入 `.env`、新聞資料庫或 Git，也不修改其他使用者的環境設定。**切換服務商會清除輸入的 key 並重設模型**；可隨時按「清除輸入的 key」。清除或未輸入時，只會使用所選服務商對應的本機 key；兩者皆未設定時停用分析按鈕，抓取新聞仍可使用。關閉或重新載入頁面後可能需要重新輸入。

`LLM_PROVIDER` 可設定網頁預設服務商：`deepseek`（預設）、`openai`、`xai`、`anthropic`、`gemini`。舊版把 DeepSeek key 放在 `OPENAI_API_KEY` 的設定，只有在 `OPENAI_BASE_URL` 指向 DeepSeek，或未設定端點且明確指定 `LLM_PROVIDER=deepseek` 時，網頁才會將它用於 DeepSeek；同一環境 key 不會同時提供給 OpenAI 和 DeepSeek。命令列既有流程仍使用 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 與 `LLM_MODEL` 的相容 API 設定。

更新程式後若頁面仍載入舊模組，請重新啟動 Streamlit。手動分析失敗時會分別提示 API 驗證、用量限制、模型空回應或格式錯誤；`TypeError` 本身不表示 key 無效。錯誤提示不顯示服務商的原始回應或密鑰。測試使用模擬回應驗證五家服務商的請求及解析，未用真實 key 驗證帳戶權限或產生付費分析。

### 在網頁抓取 Investat

1. 登入 [Investat 帳號設定](https://investat.ai/account)，取得個人 MCP token。帳號電郵／密碼不能代替 token；存取權限依 Investat 帳號及訂閱狀態決定。
2. 在左側「Investat 設定」貼上 token，按 Enter 套用。也可在本機 `.env` 設定 `INVESTAT_MCP_TOKEN`。
3. 在「新聞來源」勾選「Investat／機構研究」，選擇單次文章數（預設 20，最多 50），按「抓取最新新聞」。Investat 預設不勾選；僅打開或重新整理網頁不會呼叫它。
4. 文章會與其他來源一起保存在 `data/news.sqlite3`、顯示在新聞列表。按「AI 分析待處理新聞」才使用所選模型和 AI key；完成的分析也會進入現有日報。

頁面 token 只保留於目前工作階段，優先於本機環境設定，僅送往固定的 Investat MCP 端點。不寫入檔案、新聞資料庫或 Git，也不改動其他工作階段或 AI 服務商設定。按「清除輸入的 Investat token」會清空頁面輸入；若本機仍有環境 token，後續抓取會使用該設定。切換 AI 服務商不會清除 Investat token。

收集會取得最新文章摘要及接口提供的內文，按明確的內文游標繼續讀取；每篇最多 5 段。只有摘要、部分內文及接口提供的文字分別標示為 `summary`、`partial_text`、`provided_text`，不因收到文字就宣稱已取得全文。單篇詳情失敗仍會保存可用摘要，並提示部分抓取；驗證、訂閱或限流失敗不會影響其他新聞來源。

以 Investat 文章 ID 去重；重複抓取不會新增相同文章。機構名稱保留於來源標籤，不將機構研究當成官方公告。接口沒有提供文章連結時，頁面顯示文章 ID 及「前往 Investat」，不組合未經確認的文章網址。單次抓取上限只涵蓋當次接口回傳的最新文章，沒有歷史回補或背景排程。

命令列也可使用同一新聞庫，token 只從環境設定讀取：

```bash
# 只抓 Investat，不呼叫 AI
python -m src.rss_pipeline --investat-only --investat-limit 20

# 與已設定的公開新聞來源一起抓取
python -m src.rss_pipeline --investat

# 明確啟動分析；需要另外設定 LLM key，會產生 API 用量
python -m src.rss_pipeline --investat-only --analyze --analysis-limit 5
```

舊的 `python -m src.pipeline` 仍是直接抓取並分析的相容入口，需 `INVESTAT_MCP_TOKEN` 和 LLM key，結果保存於 `data/events.jsonl`。新網頁／命令列收集使用共用 SQLite 庫，既有手動及舊 Investat 結果保留。日報一起讀取這兩種已完成分析；目前仍按全部已保存分析的置信度篩選，沒有按當日日期限制；港股／A 股標的映射也尚未完整覆蓋。

`data/feed_status.json` 保存新聞來源抓取狀態，SEC 單獨執行時使用 `data/sec_status.json`。新增收集流程的錯誤紀錄只保留安全分類，不保存 LLM 回應中的原始錯誤文字或密鑰。

## 未來雲端方案（尚未實作或部署）

以下是已討論的後續設計；目前仍是手動執行的本機版本，尚未建立雲端服務或自動排程。

- **全天運行與收集：**雲端服務預計 24/7 運行，每 5 分鐘抓取一次新聞及 SEC 申報通知。收集與 AI 分析分開執行，先保存、去重，再排入分析佇列；超出分析額度的項目留待後續處理。來源發布延遲、抓取週期與排隊時間都會影響結果出現時間，不保證即時推送。
- **分析額度：**任何滾動 60 分鐘內最多 20 次模型分析嘗試。每次實際送出的模型請求都須計數，失敗請求及重試也佔用額度；額度紀錄需持久保存，服務重啟後仍須遵守同一視窗。
- **持久保存：**新聞、分析結果、佇列狀態及額度紀錄需保存在持久化資料庫。候選部署方式是 Railway 搭配 [Volume](https://docs.railway.com/volumes) 保存資料；尚未選定或部署。
- **SEC 名單維護：**定期刷新兩個指數的公司追蹤名單，在七日有效期屆滿前完成更新；若更新失敗或名單過期，仍須遵守現有覆蓋檢查。收集內容仍是申報通知及連結，並非申報全文；全文下載與分析不在目前已完成範圍。
- **網頁顯示：**未來網頁可自動刷新並顯示資料庫中已快取的新聞與分析結果；刷新頁面本身不應觸發新的模型請求。

部署時透過平台環境變數／Secrets 設定密鑰及 SEC 聯絡資料，不寫入程式碼、README 或 Git。以下僅為設定範本，沒有真實密鑰或聯絡資料：

```text
OPENAI_API_KEY=<your-deepseek-api-key>
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-pro
SEC_USER_AGENT=my-news-reader/1.0 contact <your-contact-email>
```

模型名稱與價格在部署前仍須核對帳戶實際可用性及 [DeepSeek 官方模型／價格說明](https://api-docs.deepseek.com/quick_start/pricing/)。以上設定範本不代表已驗證雲端部署或模型呼叫。

## 測試

```bash
python -m unittest discover -s tests -v
```

單元測試使用固定資料和模擬請求，不呼叫付費模型，也不依賴新聞網站即時可用。

