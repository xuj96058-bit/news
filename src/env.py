"""
自動載入本機 .env（不會提交到 Git）
"""
from pathlib import Path

try:
    from dotenv import load_dotenv
    # 從項目根目錄載入 .env
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
except ImportError:
    pass  # 沒安裝 python-dotenv 也可以，使用系統環境變數
