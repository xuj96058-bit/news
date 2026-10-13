"""Start the local inbox from a Windows checkout without storing credentials."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8502/"
HEALTH_URL = URL + "_stcore/health"


def healthy() -> bool:
    # Local traffic should not go through a configured external proxy.
    try:
        with build_opener(ProxyHandler({})).open(Request(HEALTH_URL), timeout=1) as response:
            return response.status == 200 and response.read(16).strip() == b"ok"
    except (OSError, ValueError):
        return False


def occupied() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8502), timeout=1):
            return True
    except OSError:
        return False


def matching_server() -> bool:
    """Do not reuse an older checkout or an unrelated Streamlit on this port."""
    if sys.platform != "win32":
        return False
    script = (
        "$taskPort=Get-NetTCPConnection -LocalPort 8502 -State Listen -ErrorAction Stop "
        "| Select-Object -First 1; "
        "$taskProcess=Get-CimInstance Win32_Process -Filter "
        "('ProcessId = '+$taskPort.OwningProcess) -ErrorAction Stop; "
        "@{command=$taskProcess.CommandLine; executable=$taskProcess.ExecutablePath} "
        "| ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        owner = json.loads(result.stdout)
        command = owner.get("command", "").casefold()
        return ("-m streamlit run" in command
                and str(ROOT / "app.py").casefold() in command
                and Path(owner.get("executable", "")).name.casefold() in {"python.exe", "pythonw.exe"})
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Open the local News inbox")
    parser.add_argument("--no-browser", action="store_true", help="Check/start without opening a browser")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("This launcher is for Windows. Run: streamlit run app.py --server.port 8502")
        return 1
    if healthy():
        if not matching_server():
            print("Port 8502 belongs to a different or older checkout. Close it before starting this version.")
            return 1
    else:
        if occupied():
            print("Port 8502 is occupied by another service. Close that service before starting News.")
            return 1
        modules = ("openai", "pydantic", "streamlit", "requests", "dotenv", "feedparser", "tzdata")
        if sys.version_info < (3, 10) or any(importlib.util.find_spec(name) is None for name in modules):
            print("Python 3.10+ with the project dependencies is required.")
            print(f'Run: "{sys.executable}" -m pip install -r "{ROOT / "requirements.txt"}"')
            return 1
        data = ROOT / "data"
        data.mkdir(exist_ok=True)
        command = [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
                   "--server.headless", "true", "--server.address", "127.0.0.1",
                   "--server.port", "8502", "--browser.gatherUsageStats", "false"]
        flags = 0
        if sys.platform == "win32":
            flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.CREATE_NO_WINDOW)
        with (data / "news-server.stdout.log").open("ab") as stdout, \
                (data / "news-server.stderr.log").open("ab") as stderr:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, close_fds=True,
                                       creationflags=flags)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if healthy():
                if not matching_server():
                    print("Another service took port 8502. See data/news-server.stderr.log.")
                    return 1
                break
            if process.poll() is not None:
                print("News stopped during startup. See data/news-server.stderr.log.")
                return 1
            time.sleep(0.25)
        else:
            print("News is still starting. Retry shortly or see data/news-server.stderr.log.")
            return 1
    print("News is ready: " + URL)
    if not args.no_browser:
        webbrowser.open(URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
