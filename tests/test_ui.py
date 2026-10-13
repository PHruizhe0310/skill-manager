"""网页界面自测：用无头浏览器跑 tests\\skmtest.js 的检查项。

为什么单独一个脚本：HTML5 拖放的 dragstart/dragover/drop 事件序列，
无头浏览器不会自己产生。tests\\skmtest.js 用合成的 DragEvent 走一遍**页面里
真实的监听器**（不是另写一套逻辑），把结果写进 DOM，这里用 --dump-dom 抓回来断言。

跑的是真实技能库副本 + 真实后端，拖拽改的分类落在副本里，不碰用户数据。

用法：
    python tests\\test_ui.py
"""
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
REAL_LIB = BASE_DIR / "data"
PORT = 8793
URL = f"http://127.0.0.1:{PORT}"
TESTDIR = pathlib.Path(tempfile.gettempdir()) / "skm_ui_test"

CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

results = []


def check(label, condition, detail=""):
    results.append((label, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


def find_browser():
    for path in CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def dump_dom(browser, url, profile, budget=20000):
    """跑一次无头浏览器，把最终 DOM 拿回来。"""
    args = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            f"--user-data-dir={profile}", f"--virtual-time-budget={budget}",
            "--dump-dom", url]
    proc = subprocess.run(args, capture_output=True, timeout=180)
    return proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def extract(dom):
    m = re.search(r'id="skmtest-out"[^>]*>(.*?)</pre>', dom, re.S)
    if not m:
        return None
    return json.loads(html.unescape(m.group(1)))


class FakeAI:
    """给界面自测用的假 AI 接口（免得真调云端、也不需要 key）。"""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    body = {}
                outer.requests.append(body)
                # 从提示词里把 id 抠出来，按 id 拼一个像样的回答
                prompt = ""
                for msg in body.get("messages", []):
                    if msg.get("role") == "user":
                        prompt = msg.get("content", "")
                ids = re.findall(r"^id: (.+)$", prompt, re.M)
                if not ids:
                    ids = re.findall(r"id: ([^\n]+)", prompt)
                answer = [{"id": one.strip(), "category": "信息对抗/侦察测绘",
                           "newCategory": False, "reason": "自测桩"}
                          for one in ids]
                data = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]},
                                 ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def main():
    browser = find_browser()
    if not browser:
        print("找不到 Chrome / Edge，跳过界面自测。")
        print("装一个 Chrome 或 Edge 再跑；或者看 tests\\test_api.py 的接口层覆盖。")
        return 0

    if TESTDIR.exists():
        shutil.rmtree(TESTDIR)
    shutil.copytree(REAL_LIB, TESTDIR)
    # 素材自己造：把 3 个 Skill 强行归到「未分类」，并清掉已登记的项目。
    # 用户随时在用这个工具，库是活的（第一版就因为"正好没有未分类的"红过）。
    cat_path = TESTDIR / "catalog.json"
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    probe = sorted(cat["skills"])[:3]
    for sid in probe:
        cat["skills"][sid]["category"] = "未分类"
        meta = TESTDIR / "skills" / sid / "skill.json"
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["category"] = "未分类"
        meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    cat_path.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")
    proj_path = TESTDIR / "projects.json"
    pj = json.loads(proj_path.read_text(encoding="utf-8"))
    pj["projects"] = {}
    proj_path.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
    fake_ai = FakeAI()
    conf_path = TESTDIR / "config.json"
    conf = json.loads(conf_path.read_text(encoding="utf-8"))
    conf["ai"] = {"enabled": True, "baseUrl": fake_ai.url, "model": "fake-model",
                  "apiKey": "sk-ui-test-key", "temperature": 0, "timeout": 30,
                  "bodyChars": 300, "createCategory": True, "batchSize": 1}
    conf_path.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")

    server = subprocess.Popen(
        [sys.executable, "skm.py", "--data-dir", str(TESTDIR),
         "ui", "--port", str(PORT), "--no-browser"],
        cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8")
    try:
        up = False
        for _ in range(40):                      # 等服务起来
            if server.poll() is not None:          # 进程已经退了就没必要等了
                break
            try:
                urllib.request.urlopen(URL + "/api/state", timeout=2).read()
                up = True
                break
            except Exception:
                time.sleep(0.25)
        if not up:
            server.terminate()
            out, _ = server.communicate(timeout=6)
            print("服务没起来。它的输出：")
            print(out or "(空)")
            fake_ai.close()
            return 1

        profile = pathlib.Path(tempfile.gettempdir()) / "skm_chrome_profile"
        proj = pathlib.Path(tempfile.gettempdir()) / "skm_ui_proj"
        if proj.exists():
            shutil.rmtree(proj)
        proj.mkdir(parents=True)
        url = f"{URL}/?skmtest=1&proj=" + urllib.parse.quote(str(proj))
        dom, err = dump_dom(browser, url, profile)
        payload = extract(dom)
        check("假 AI 接口被调用过（说明界面那条链路是通的）",
              len(fake_ai.requests) > 0, f"{len(fake_ai.requests)} 次请求")

        print("=" * 72)
        print("无头浏览器自测（tests\\skmtest.js）")
        print("=" * 72)
        check("自测脚本跑完并把结果写进 DOM", payload is not None,
              f"DOM {len(dom)} 字符，浏览器 {pathlib.Path(browser).name}")
        if payload is None:
            print(dom[-1500:])
            return 1

        for item in payload["results"]:
            check(item["label"], item["pass"], item["detail"])
        check("页面运行期间没有 JS 报错 / 未处理的 Promise 拒绝",
              not payload["errors"], "; ".join(payload["errors"][:3]))

        console = [line for line in err.splitlines()
                   if "ERROR:CONSOLE" in line or "Uncaught" in line]
        check("浏览器控制台没有报错", not console, "; ".join(console[:2]))

        # 顺带确认钩子在正常使用时是死的：不带 ?skmtest=1 不该跑自测脚本
        dom2, _ = dump_dom(browser, URL + "/", profile)
        check("不带 ?skmtest=1 时不会跑自测脚本", 'id="skmtest-out"' not in dom2)
        check("正常页面照样渲染出卡片",
              dom2.count('class="card"') >= len(
                  json.loads((REAL_LIB / "catalog.json").read_text(encoding="utf-8"))["skills"]),
              f'{dom2.count(chr(34) + "card" + chr(34))} 张卡片')
        check("设置面板里有 AI 配置区块，且不显示完整 key",
              'id="aiUrl"' in dom2 and "sk-ui-test-key" not in dom2)
    finally:
        server.terminate()
        try:
            server.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate()
        fake_ai.close()

    bad = [label for label, okk, _ in results if not okk]
    print()
    print("=" * 72)
    print(f"结果：{len(results) - len(bad)}/{len(results)} 项通过")
    if bad:
        print("失败项：", bad)
    print("=" * 72)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
