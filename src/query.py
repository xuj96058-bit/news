"""
新聞查詢入口
用法：python -m src.query "新聞內容"
"""

import sys
import argparse
from . import env  # 自動載入本機 .env
from .analyzer import analyze_news, format_result


def main():
    parser = argparse.ArgumentParser(description="金融新聞分析查詢")
    parser.add_argument("news", nargs="*", help="新聞文本（可直接貼上）")
    parser.add_argument("--model", default=None, help="LLM 模型（默認讀取 LLM_MODEL 環境變數）")
    args = parser.parse_args()

    if args.news:
        text = " ".join(args.news)
    else:
        print("請輸入新聞內容（結束按 Ctrl+D）：")
        text = sys.stdin.read()

    if not text.strip():
        print("沒有輸入新聞")
        return

    print("分析中...\n")
    try:
        result = analyze_news(text, model=args.model)
        print(format_result(result))
    except Exception as e:
        print(f"分析失敗：{e}")
        raise


if __name__ == "__main__":
    main()
