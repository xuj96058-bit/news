"""Optional public evidence for research reports; these are not RSS feeds.

Fetch only a small explicit catalog. Preserve document dates, scope and missing
data instead of claiming that page extraction is independent fact verification.
"""
from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
import re
from urllib.parse import urljoin, urlsplit

import requests

CATALOG = {
    "finra": ("FINRA｜證券保證金餘額", "https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics"),
    "cftc": ("CFTC｜股指期貨倉位", "https://www.cftc.gov/dea/futures/financial_lf.htm"),
    "scoos": ("聯儲｜交易商融資調查", "https://www.federalreserve.gov/data/scoos.htm"),
    "sec_pf": ("SEC｜私募基金統計", "https://www.sec.gov/data-research/statistics-data-visualizations/private-fund-statistics"),
    "sector_pack": ("State Street｜行業圖表月報", "https://www.ssga.com/library-content/pdfs/etf/us/state-street-sector-chart-pack-monthly.pdf"),
    "spy": ("State Street｜標普500權重", "https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy"),
}
_HOSTS = {urlsplit(value[1]).hostname for value in CATALOG.values()}
MAX_BYTES = 8 * 1024 * 1024
MAX_CHARS = 14000


class EvidenceError(ValueError):
    def __init__(self, reason="fetch"):
        self.reason = reason
        super().__init__({"fetch": "公開資料讀取失敗；請稍後重試或手動加入資料。",
                          "format": "來源版面或資料格式已改變，未取得可用的相關摘錄。",
                          "size": "來源超過讀取上限，請手動選取相關資料。",
                          "pdf": "PDF讀取需要 pypdf；請更新依賴後重試。"}.get(reason, "公開資料讀取失敗。"))


class TextDocument(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links = [], []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag == "a":
            self.links += [value for key, value in attrs if key == "href"]
        if not self.hidden and tag in {"p", "div", "tr", "br", "li", "h1", "h2", "h3", "h4", "pre"}:
            self.parts.append("\n")
        if not self.hidden and tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    @property
    def text(self):
        text = "".join(self.parts)
        return "\n".join(re.sub(r"[ \t\r]+", " ", line).strip() for line in text.splitlines() if line.strip())


def _fetch(url, get=None):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _HOSTS or parsed.username or parsed.password:
        raise EvidenceError("format")
    response = None
    try:
        response = (get or requests.get)(url, headers={"User-Agent": "news-research-reader/1.0"},
                                         timeout=(5, 25), allow_redirects=False, stream=True)
        if response.status_code != 200:
            raise EvidenceError()
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > MAX_BYTES:
                raise EvidenceError("size")
            chunks.append(chunk)
        return b"".join(chunks)
    except EvidenceError:
        raise
    except Exception:
        raise EvidenceError() from None
    finally:
        if response is not None:
            response.close()


def _html(data):
    parser = TextDocument()
    parser.feed(data.decode("utf-8", errors="replace"))
    return parser


def _pdf(data, markers, first_pages=1):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise EvidenceError("pdf") from None
    try:
        reader = PdfReader(BytesIO(data))
        if len(reader.pages) > 120:
            raise EvidenceError("size")
        chosen, front = [], []
        for index, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            hits = [match.start() for marker in markers
                    if (match := re.search(r"\s+".join(re.escape(part) for part in marker.split()), text, re.I))]
            if hits:
                chosen.append((index, text[max(0, min(hits) - 150):]))
            elif index < first_pages:
                front.append((index, text[:1000]))
        if not chosen:
            raise EvidenceError("format")
        if len(chosen) > 12:
            raise EvidenceError("size")
        # Relevant tables have priority over long front matter/TOCs. Reserve an
        # equal share for every selected page rather than silently losing the
        # last table when an earlier page is long.
        front_text = "\n\n".join(f"PDF第{i + 1}頁（背景）\n{text}" for i, text in front)[:1200]
        allowance = (MAX_CHARS - len(front_text) - 100 * len(chosen) - 100) // len(chosen)
        blocks = [f"PDF第{i + 1}頁（相關摘錄）\n{text[:allowance]}" +
                  ("\n[本頁文字已截短，請核對原PDF]" if len(text) > allowance else "") for i, text in chosen]
        return "\n\n".join(blocks + [front_text])[:MAX_CHARS]
    except EvidenceError:
        raise
    except Exception:
        raise EvidenceError("format") from None


def fetch_evidence(key, *, get=None):
    if key not in CATALOG:
        raise EvidenceError("format")
    title, url = CATALOG[key]
    observation = "原文各指標日期見摘錄；抓取日不等於觀測日"
    note = "自動摘錄，尚需人工核對數字與口徑。"
    if key == "sector_pack":
        content = _pdf(_fetch(url, get), ("Sector ETF flows", "Institutional investor positioning", "Institutional investor\npositioning"), first_pages=1)
        note += "基金流量、持倉與估值是不同指標，不自行補出圖中無法抽取的數值。"
    else:
        doc = _html(_fetch(url, get))
        content = doc.text
        if key == "finra":
            match = re.search(r"FINRA Statistics", content, flags=re.I)
            if not match:
                raise EvidenceError("format")
            content = content[max(0, match.start() - 1200):match.start() + 6500]
            latest = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-(\d{2})\b", content)
            if not latest:
                raise EvidenceError("format")
            month = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split().index(latest[1]) + 1
            year = 2000 + int(latest[2])
            observation = f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
            note += "證券帳戶借方餘額，沒有行業分項或對應資產淨值分母。"
        elif key == "cftc":
            lines, excerpts = content.splitlines(), []
            date = re.search(r"Positions as of ([A-Za-z]+ \d{1,2}, \d{4})", content)
            if date:
                observation = date[1]
            for index, line in enumerate(lines):
                if re.search(r"S&P 500 Consolidated|NASDAQ-100 Consolidated", line, re.I):
                    excerpts.extend(lines[max(0, index - 8):index + 14])
            if not excerpts:
                raise EvidenceError("format")
            content = "\n".join(excerpts)
            note += "Futures-only；Leveraged Funds淨空頭可能對沖現貨，不能當作總槓桿。"
        elif key == "scoos":
            links = [(match[1], urljoin(url, href)) for href in doc.links
                     if (match := re.search(r"scoos-(\d{6})\.htm$", href))]
            if not links:
                raise EvidenceError("format")
            period, url = max(links)
            content = _html(_fetch(url, get)).text
            start = content.find("Summary")
            if start >= 0:
                content = content[start:]
            observation = f"報告期{period[:4]}-{period[4:]}；實際調查期見正文"
            note += "交易商定性回答不是基金比例；特別問題未必每季重複。"
        elif key == "sec_pf":
            links = [(int(match[1]), int(match[2]), urljoin(url, href)) for href in doc.links
                     if (match := re.search(r"private-fund-statistics-(\d{4})-q([1-4])(?:-accessible)?\.pdf$", href, re.I))]
            if not links:
                raise EvidenceError("format")
            year, quarter, url = max(links)
            content = _pdf(_fetch(url, get), ("Table 8.13", "Table 8.14", "Table 8.27", "Table 8.28"))
            observation = f"{year}Q{quarter}"
            note += "合資格對沖基金樣本；prime-broker借款跨策略，不能等同美股行業槓桿。"
        elif key == "spy":
            start = content.find("Index Top Holdings")
            if start < 0 or not re.search(r"NVIDIA.*?\d[.]\d+%", content, re.S):
                raise EvidenceError("format")
            content = content[start:start + 10000]
            note += "必須區分ETF持倉和指數權重；Alphabet不同股類會分列。"
    if not content.strip():
        raise EvidenceError("format")
    return {"title": title, "publisher": title.split("｜")[0], "url": url,
            "publication_date": None, "observation_date": observation,
            "content": note + "\n\n" + content[:MAX_CHARS], "source_type": "official",
            "retrieved_at": datetime.now(timezone.utc).isoformat()}


def collect_evidence(keys):
    evidence, outcomes = [], []
    selected = list(dict.fromkeys(keys))
    if not selected:
        return {"evidence": [], "sources": []}
    with ThreadPoolExecutor(max_workers=min(6, len(selected))) as pool:
        jobs = [(key, pool.submit(fetch_evidence, key)) for key in selected]
        for key, job in jobs:
            try:
                item = job.result()
                evidence.append(item)
                outcomes.append({"id": key, "status": "ok", "name": CATALOG[key][0]})
            except EvidenceError as exc:
                outcomes.append({"id": key, "status": "error", "name": CATALOG.get(key, (str(key),))[0], "error": str(exc)})
    return {"evidence": evidence, "sources": outcomes}
