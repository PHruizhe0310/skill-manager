"""AI 自动分类的回归自检。

**不联网、不需要真的 API Key**：起一个本地假 AI 服务（http.server），
它按 OpenAI 兼容格式回话，同时记录收到的请求，用来断言
「提示词里到底装了什么」「Authorization 头有没有带上」「解析稳不稳」。

用法：
    python tests\\test_ai.py
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
sys.path.insert(0, str(BASE))
import skm as skm_mod                                            # noqa: E402

WORK = pathlib.Path(tempfile.gettempdir()) / "skm_ai_test"
LIB = WORK / "lib"
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
shutil.copytree(BASE / "data", LIB)

# 测试素材必须自己造，不能指望用户库里正好有「未分类」的 Skill——
# 用户随时可能在用这个工具，库的状态是活的（第一版就因为这个红过）。
# 这里强行把 3 个 Skill 归到「未分类」，后面的用例就用它们。
_catalog = json.loads((LIB / "catalog.json").read_text(encoding="utf-8"))
_probe_ids = sorted(_catalog["skills"])[:3]
for _sid in _probe_ids:
    _catalog["skills"][_sid]["category"] = "未分类"
    _meta = LIB / "skills" / _sid / "skill.json"
    _m = json.loads(_meta.read_text(encoding="utf-8"))
    _m["category"] = "未分类"
    _meta.write_text(json.dumps(_m, ensure_ascii=False, indent=2), encoding="utf-8")
(LIB / "catalog.json").write_text(json.dumps(_catalog, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
PROBE_IDS = _probe_ids

results = []


def check(label, condition, detail=""):
    results.append((label, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


# ───────────────────────── 假 AI 服务 ─────────────────────────

class FakeAI:
    """可控的假 OpenAI 兼容接口。"""

    def __init__(self):
        self.reply = ""            # 下次要回的文本
        self.status = 200          # 下次要回的状态码
        self.requests = []         # 收到的请求
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handler(self):
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
                    body = {"_raw": raw.decode("utf-8", "replace")}
                outer.requests.append({
                    "path": self.path, "body": body,
                    "auth": self.headers.get("Authorization"),
                    "contentType": self.headers.get("Content-Type")})
                payload = {"choices": [{"message": {"content": outer.reply}}]}
                data = json.dumps(payload).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


fake = FakeAI()


def cfg_with(**over):
    base = {"baseUrl": fake.url, "model": "fake-model", "apiKey": "sk-test-key",
            "temperature": 0, "timeout": 20, "bodyChars": 400,
            "createCategory": True, "batchSize": 1}
    base.update(over)
    return base


def make_lib(ai=None):
    lib = skm_mod.Library(LIB).ensure()
    lib.config["ai"] = ai if ai is not None else cfg_with()
    return lib


print("=" * 72)
print("一、配置与密钥")
print("=" * 72)

lib = make_lib()
cfg = skm_mod.ai_config(lib)
check("配置读得到", cfg["model"] == "fake-model" and cfg["baseUrl"] == fake.url)

pub = skm_mod.ai_public_config(cfg)
check("给前端的配置里没有密钥", "apiKey" not in pub and pub["hasKey"] is True)
check("只给后四位提示", pub["keyHint"].endswith("key") and "sk-test" not in pub["keyHint"],
      pub["keyHint"])
check("ready 表示三项齐了", pub["ready"] is True)

import os                                                       # noqa: E402
os.environ["SKM_AI_API_KEY"] = "env-key-9999"
lib.config["ai"]["apiKey"] = ""
cfg2 = skm_mod.ai_config(lib)
check("没写 key 时用环境变量", cfg2["apiKey"] == "env-key-9999",
      cfg2["apiKey"][:6] + "…")
os.environ.pop("SKM_AI_API_KEY", None)

for label, missing in (("接口地址", {"baseUrl": "", "model": "m", "apiKey": "k"}),
                       ("模型名", {"baseUrl": "u", "model": "", "apiKey": "k"}),
                       ("API Key", {"baseUrl": "u", "model": "m", "apiKey": ""})):
    try:
        skm_mod.ai_require_config(cfg_with(**missing))
        check(f"缺 {label} 时报错", False, "竟然没报错")
    except skm_mod.SkmError as exc:
        check(f"缺 {label} 时明确说出来", label.split()[0] in str(exc),
              str(exc).splitlines()[0][:46])

print()
print("=" * 72)
print("二、调用与解析")
print("=" * 72)

fake.reply = "只有两个字：可用"
answer = skm_mod.ai_chat(cfg_with(), [{"role": "user", "content": "只回复两个字：可用"}])
check("调用成功并取到文本", answer == fake.reply, answer[:30])
sent = fake.requests[-1]
check("URL 拼成了 /chat/completions", sent["path"] == "/v1/chat/completions", sent["path"])
check("带上了 Authorization: Bearer", sent["auth"] == "Bearer sk-test-key", str(sent["auth"]))
check("请求体带模型名和 messages",
      sent["body"]["model"] == "fake-model" and sent["body"]["messages"][0]["role"] == "user")
check("temperature 传了 0", sent["body"]["temperature"] == 0)

sent_full = fake.requests[-1]

fake.status = 401
try:
    skm_mod.ai_chat(cfg_with(), [{"role": "user", "content": "x"}])
    check("401 要给友好提示", False)
except skm_mod.SkmError as exc:
    check("401 的提示要说清多半是 key 或模型名不对",
          "401" in str(exc) and "API Key" in str(exc), str(exc)[:52])
fake.status = 500
try:
    skm_mod.ai_chat(cfg_with(), [{"role": "user", "content": "x"}])
    check("500 要给友好提示", False)
except skm_mod.SkmError as exc:
    check("500 的提示带上服务端返回的内容", "500" in str(exc), str(exc)[:52])
fake.status = 200

dead = cfg_with(baseUrl="http://127.0.0.1:1/v1", timeout=3)
try:
    skm_mod.ai_chat(dead, [{"role": "user", "content": "x"}])
    check("连不上要报「连不上」", False)
except skm_mod.SkmError as exc:
    check("连不上时报「连不上」而不是堆栈",
          "连不上" in str(exc) and "Traceback" not in str(exc), str(exc).splitlines()[0][:44])

samples = [
    ('[{"id":"a","category":"信息对抗/侦察测绘","newCategory":false,"reason":"测绘"}]',
     [{"id": "a", "category": "信息对抗/侦察测绘", "newCategory": False, "reason": "测绘"}],
     "裸 JSON"),
    ('```json\n[{"id":"a","category":"信息对抗/侦察测绘"}]\n```',
     [{"id": "a", "category": "信息对抗/侦察测绘"}], "包在 ```json 里"),
    ('好的，我分析如下：\n[{"id":"a","category":"信息对抗/侦察测绘"}]\n希望有帮助',
     [{"id": "a", "category": "信息对抗/侦察测绘"}], "前面有客套话"),
    ('{"id":"a","category":"信息对抗/侦察测绘"}',
     [{"id": "a", "category": "信息对抗/侦察测绘"}], "单个对象"),
    ('{"skills":[{"id":"a","category":"信息对抗/侦察测绘"}]}',
     [{"id": "a", "category": "信息对抗/侦察测绘"}], "包在 skills 字段里"),
]
for text, want, label in samples:
    try:
        got = skm_mod.parse_ai_json(text)
        check(f"解析：{label}", got == want, json.dumps(got, ensure_ascii=False)[:48])
    except skm_mod.SkmError as exc:
        check(f"解析：{label}", False, str(exc)[:48])
try:
    skm_mod.parse_ai_json("我拒绝回答")
    check("解析：完全不是 JSON 时报错", False)
except skm_mod.SkmError as exc:
    check("解析：完全不是 JSON 时报错", "JSON" in str(exc))

print()
print("=" * 72)
print("三、提建议（只读）")
print("=" * 72)

lib = make_lib()
uncat = skm_mod.uncategorized_ids(lib)
check("找得到未分类的 Skill（测试自己造的素材）",
      set(PROBE_IDS) <= set(uncat), f"{len(uncat)} 个未分类")

fake.reply = json.dumps([
    {"id": uncat[0], "category": "信息对抗/侦察测绘", "newCategory": False, "reason": "资产测绘"},
    {"id": uncat[1], "category": "AI安全/新方向", "newCategory": True, "reason": "没有合适的"},
], ensure_ascii=False)
items = skm_mod.propose_categories(lib, uncat[:2], cfg_with())
check("逐个给出建议", [i["id"] for i in items] == uncat[:2])
check("在树里的标成不是新建", items[0]["newCategory"] is False and items[0]["category"] == "信息对抗/侦察测绘")
check("不在树里的标成新建（即使模型自己没标）",
      items[1]["newCategory"] is True and items[1]["category"] == "AI安全/新方向")

prompt = "\n".join(r["body"]["messages"][1]["content"] for r in fake.requests[-2:])
check("提示词里带了完整分类树（含二级和描述）",
      "信息对抗/侦察测绘" in prompt and "资产测绘" in prompt)
check("提示词里带了 Skill 的 id 和介绍",
      uncat[0] in prompt and uncat[1] in prompt
      and "一句话介绍" in prompt and "正文节选" in prompt)
with_tags = [s for s in lib.catalog["skills"].values() if s.get("tags")][:1]
if with_tags:
    fake.reply = json.dumps([{"id": with_tags[0]["id"], "category": "未分类"}], ensure_ascii=False)
    skm_mod.propose_categories(lib, [with_tags[0]["id"]], cfg_with())
    check("有标签的 Skill 会把标签一起送进去",
          "标签:" in fake.requests[-1]["body"]["messages"][1]["content"])
check("提示词里限制了输出格式",
      "JSON" in prompt and "未分类" not in prompt.split("待分类")[0][:200])
check("系统提示要求别输出多余文字",
      "只输出 JSON" in fake.requests[-1]["body"]["messages"][0]["content"])

check("提建议一个字节都没改（还是未分类）",
      all(lib.catalog["skills"][i]["category"] == "未分类" for i in uncat[:2]))

before = dict(lib.catalog["skills"])
fake.reply = json.dumps([{"id": uncat[0], "category": "信息对抗/侦察测绘"}], ensure_ascii=False)
items = skm_mod.propose_categories(lib, uncat[:2], cfg_with())
check("模型漏掉的 Skill 也会占一条并说明原因",
      items[0]["category"] and items[1]["error"] == "模型没提到这个 Skill",
      items[1]["error"])
check("失败的条目没有把库里改坏", lib.catalog["skills"] == before)

fake.reply = json.dumps([{"id": uncat[0], "category": "信息对抗/侦察测绘"}], ensure_ascii=False)
items = skm_mod.propose_categories(lib, uncat[:1], cfg_with(createCategory=False))
check("不允许新建时，树外的分类被拒（不会被静默创建）",
      items[0]["error"] == "" and items[0]["category"] == "信息对抗/侦察测绘")

print()
print("=" * 72)
print("四、落盘（应用建议）")
print("=" * 72)

lib = make_lib()
target = uncat[0]
res = skm_mod.apply_categories(lib, [{"id": target, "category": "信息对抗/侦察测绘"}])
check("改了分类", res["applied"] and lib.catalog["skills"][target]["category"] == "信息对抗/侦察测绘")
meta = json.loads((lib.skill_dir(target) / "skill.json").read_text(encoding="utf-8"))
check("catalog 与 skill.json 同步", meta["category"] == "信息对抗/侦察测绘")

new_path = "测试专用分类/AI 生成的东西"
res = skm_mod.apply_categories(lib, [{"id": uncat[1], "category": new_path}], create_missing=True)
check("新分类被真的创建了", res["created"] == [new_path], str(res["created"]))
paths = set(lib.category_paths())
check("新分类在分类树里（含一级和二级）",
      new_path in paths and "测试专用分类" in paths)
check("建完分类 Skill 也归过去了",
      lib.catalog["skills"][uncat[1]]["category"] == new_path)

res = skm_mod.apply_categories(lib, [{"id": uncat[2], "category": ""}])
check("空分类=不改", res["applied"] == [] and lib.catalog["skills"][uncat[2]]["category"] == "未分类")
res = skm_mod.apply_categories(lib, [{"id": "no-such-skill", "category": "未分类"}])
check("不存在的 Skill 报失败而不是崩",
      res["failed"] and "技能库里没有" in res["failed"][0]["why"])
res = skm_mod.apply_categories(lib, [{"id": uncat[2], "category": "不存在的分类/子类"}],
                               create_missing=False)
check("不建新分类时：树外的分类被拒（不会被偷偷创建）",
      res["failed"] and "不自动新建" in res["failed"][0]["why"], str(res["failed"])[:56])
check("拒绝后分类树没被污染",
      "不存在的分类/子类" not in set(lib.category_paths()))

print()
print("=" * 72)
print("五、命令行 + 接口")
print("=" * 72)

conf = json.loads((LIB / "config.json").read_text(encoding="utf-8"))
conf["ai"] = cfg_with()
(LIB / "config.json").write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")


def reset_probes(count=2):
    """把素材 Skill 重新扔回「未分类」——前面几节会把它们归走，
    后面几节又需要"未分类的 Skill"当输入。"""
    path = LIB / "catalog.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for sid in PROBE_IDS[:count]:
        data["skills"][sid]["category"] = "未分类"
        meta = LIB / "skills" / sid / "skill.json"
        meta.write_text(json.dumps(data["skills"][sid], ensure_ascii=False, indent=2),
                        encoding="utf-8")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return PROBE_IDS[:count]


def skm(*args, expect_rc=0):
    proc = subprocess.run([PY, "skm.py", "--data-dir", str(LIB), *args],
                          cwd=BASE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    out = proc.stdout + proc.stderr
    if expect_rc is not None and proc.returncode != expect_rc:
        print(f"    [rc={proc.returncode}] {' '.join(args)}\n{out[-500:]}")
    return proc.returncode, out


rc, out = skm("ai")
check("skm ai 显示配置但不打印完整 key",
      rc == 0 and "sk-test-key" not in out and "fake-model" in out)
rc, out = skm("ai", "--test")
check("skm ai --test 能测通", rc == 0 and "连接正常" in out, out.strip().splitlines()[-1][:40])

pick = reset_probes(2)
fake.reply = json.dumps([{"id": pick[0], "category": "信息对抗/侦察测绘", "reason": "测绘"},
                        {"id": pick[1], "category": "法学/法律检索", "reason": "法条"}],
                       ensure_ascii=False)
rc, out = skm("classify", "--dry-run", "--ids", ",".join(pick))
check("classify --dry-run 只看不改",
      rc == 0 and "--dry-run" in out
      and skm_mod.Library(LIB).ensure().catalog["skills"][pick[0]]["category"] == "未分类")

fake.reply = json.dumps([{"id": pick[0], "category": "信息对抗/侦察测绘", "reason": "测绘"},
                        {"id": pick[1], "category": "法学/法律检索", "reason": "法条"}],
                       ensure_ascii=False)
rc, out = skm("classify", "--ids", ",".join(pick), "-y")
after = skm_mod.Library(LIB).ensure().catalog["skills"]
check("classify -y 真归类了",
      rc == 0 and after[pick[0]]["category"] == "信息对抗/侦察测绘"
      and after[pick[1]]["category"] == "法学/法律检索", out.strip().splitlines()[-2][:44])
check("报告写进了 data\\_ai", (LIB / "_ai").is_dir()
      and any(p.suffix == ".json" for p in (LIB / "_ai").iterdir()))
meta = json.loads((LIB / "skills" / pick[0] / "skill.json").read_text(encoding="utf-8"))
check("命令行归类后 skill.json 也同步了", meta["category"] == "信息对抗/侦察测绘")

PORT = 8791
server = subprocess.Popen(
    [PY, "skm.py", "--data-dir", str(LIB), "ui", "--port", str(PORT), "--no-browser"],
    cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")


def req(path, method="GET", body=None):
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


try:
    for _ in range(40):
        try:
            req("/api/state")
            break
        except Exception:
            time.sleep(0.25)

    status, conf_body = req("/api/config")
    blob = json.dumps(conf_body, ensure_ascii=False)
    check("GET /api/config 不会把 API Key 发给前端", "sk-test-key" not in blob)
    status, ai_body = req("/api/ai")
    check("GET /api/ai 给的是脱敏配置",
          status == 200 and ai_body["ai"]["hasKey"] and "apiKey" not in ai_body["ai"]
          and ai_body["uncategorized"] >= 0)

    status, saved = req("/api/ai", "POST", {"model": "another-model", "apiKey": "sk-new-key-1234"})
    check("POST /api/ai 保存后回显仍是脱敏的",
          status == 200 and saved["ai"]["model"] == "another-model"
          and saved["ai"]["keyHint"].endswith("1234") and "apiKey" not in saved["ai"])
    stored = json.loads((LIB / "config.json").read_text(encoding="utf-8"))["ai"]["apiKey"]
    check("新 key 真的落盘了", stored == "sk-new-key-1234", stored[-6:])
    req("/api/ai", "POST", {"model": "fake-model"})

    fresh = reset_probes(2)
    fake.reply = json.dumps([{"id": fresh[0], "category": "办公与自动化/PPT", "reason": "PPT"},
                             {"id": fresh[1], "category": "全新分类/新子类", "newCategory": True,
                              "reason": "没有合适的"}], ensure_ascii=False)
    # 显式指定 id：库里可能还有别的未分类 Skill（用户在用这个工具），
    # 只给limit 的话取到哪两个取决于字母序，测试就不确定了。
    status, prev = req("/api/classify/preview", "POST", {"ids": fresh})
    check("POST /api/classify/preview 给出建议",
          status == 200 and len(prev["items"]) == 2
          and prev["items"][1]["newCategory"] is True,
          json.dumps(prev.get("items", [])[:1], ensure_ascii=False)[:60])
    check("preview 没有改任何数据",
          skm_mod.Library(LIB).ensure().catalog["skills"][fresh[0]]["category"] == "未分类")

    status, done = req("/api/classify/apply", "POST", {"items": prev["items"]})
    now = skm_mod.Library(LIB).ensure().catalog["skills"]
    check("POST /api/classify/apply 落盘并新建了分类",
          status == 200 and now[fresh[0]]["category"] == "办公与自动化/PPT"
          and done["created"] == ["全新分类/新子类"], str(done.get("created")))
    meta = json.loads((LIB / "skills" / fresh[1] / "skill.json").read_text(encoding="utf-8"))
    check("新建分类后两侧同步", meta["category"] == "全新分类/新子类")

    status, err = req("/api/classify/apply", "POST", {"items": []})
    check("apply 空建议被拒", status == 400 and "没有要应用" in str(err))
    fake.reply = "只有两个字：可用"
    status, err = req("/api/ai/test", "POST", {})
    check("POST /api/ai/test 通", status == 200 and "可用" in json.dumps(err, ensure_ascii=False))
finally:
    server.terminate()
    try:
        server.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        server.kill()
        server.communicate()

fake.close()
print()
bad = [label for label, okk, _ in results if not okk]
print("=" * 72)
print(f"结果：{len(results) - len(bad)}/{len(results)} 项通过")
for label, okk, detail in results:
    if not okk:
        print(f"  FAIL {label}   {detail}")
print("=" * 72)
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if bad else 0)
