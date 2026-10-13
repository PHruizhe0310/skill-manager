"""网页界面接口测试：起一个后台服务，打 18 类请求验证。

关键点：
- 用真实技能库的**副本**跑，既有 29 个真实 Skill 又有意义，又不会弄脏用户的库
- 安全项必须被拦住（伪造 Host / 非 JSON 写请求 / 路径穿越 / XSS 协议）
- 写操作后要校验 catalog.json 与 skill.json 两侧同步
- 正文搜索（/api/search）必须能搜到**只在代码块里**出现的词——
  这是阶段 3 修过的真 bug（markdown_to_text 剥代码块导致渗透测试类关键词一个都搜不到）

用法：
    python tests\\test_api.py
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
REAL_LIB = BASE_DIR / "data"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"

TESTDIR = pathlib.Path(tempfile.gettempdir()) / "skm_api_test"
if TESTDIR.exists():
    shutil.rmtree(TESTDIR)
shutil.copytree(REAL_LIB, TESTDIR)

server = subprocess.Popen(
    [sys.executable, "skm.py", "--data-dir", str(TESTDIR),
     "ui", "--port", str(PORT), "--no-browser"],
    cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8")
time.sleep(2.5)

results = []


def req(path, method="GET", body=None, headers=None, expect_rc=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def check(label, condition, detail=""):
    results.append((label, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


FENCE = re.compile(r"```.*?```", re.S)
# 两边加负向断言：只取被非字母数字符夹住的整词，
# 否则 `.../minio/invalidR` 这种路径会被拼成 "xminioinvalidr" 这样的假词。
WORD = re.compile(r"(?<![a-z0-9])[a-z]{5,14}(?![a-z0-9])")
BORING = {"return", "import", "print", "class", "const", "false", "python",
          "https", "http", "bash", "shell", "admin", "superuser", "localhost"}


def code_only_tokens():
    """只出现在 ```代码块``` 里的词 → 命中它的 Skill id 集合。

    自己用正则剥代码块，不复用 skm.py 里的 markdown_to_text——
    测的就是「剥代码块之后搜不到」这个 bug，用被测代码去准备素材等于自己骗自己。
    只收纯字母的词：代码块里那些十六进制串/长路径当搜索词没有意义，
    而且换个库就失效了。
    """
    found = {}
    for md in sorted((TESTDIR / "skills").glob("*/SKILL.md")):
        try:
            raw = md.read_text(encoding="utf-8-sig", errors="replace").lower()
        except OSError:
            continue
        outside = FENCE.sub(" ", raw)
        for word in set(WORD.findall(raw)):
            if word in outside or word in BORING:
                continue
            found.setdefault(word, set()).add(md.parent.name)
    return {word: ids for word, ids in found.items() if 1 <= len(ids) <= 4}


def pick_token(tokens):
    """挑一个够长、够少见的词当搜索词：太常见的词满库都是，测不出东西。"""
    word = max(tokens, key=lambda w: (-len(tokens[w]), len(w), w))
    return word, tokens[word]


def pick_pair(tokens, first):
    """挑两个都命中同一个 Skill 的词，才能验证「两词都命中」这个 AND 语义。"""
    best = None
    for word, ids in sorted(tokens.items()):
        if word == first or not ids:
            continue
        shared = len(ids & tokens[first])
        if shared and (best is None or shared > best[0]):
            best = (shared, word)
    if best:
        return best[1]
    return next(w for w in sorted(tokens) if w != first)


def body_hits(word):
    """正文里（含代码块）出现该词的全部 Skill id。

    这是「搜索结果应该等于什么」的唯一权威答案：服务端把 Markdown 转纯文本时
    会额外去掉标记符号，所以按原文全文找反而更准，不依赖测试去复刻那套规则。
    """
    hits = set()
    for md in sorted((TESTDIR / "skills").glob("*/SKILL.md")):
        try:
            raw = md.read_text(encoding="utf-8-sig", errors="replace").lower()
        except OSError:
            continue
        if word in raw:
            hits.add(md.parent.name)
    return hits


try:
    print("=" * 70)
    status, body = req("/")
    text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
    check("首页可访问", status == 200 and "技能管家" in text, f"HTTP {status}, {len(text)} 字符")

    status, state = req("/api/state")
    skills = state.get("skills", [])
    check("/api/state 返回全部 Skill",
          status == 200 and len(skills) == len(json.loads(
              (REAL_LIB / "catalog.json").read_text(encoding="utf-8"))["skills"]),
          f"{len(skills)} 个")
    check("分类数量父级已汇总",
          state["counts"].get("信息对抗") == 16,
          f"信息对抗={state['counts'].get('信息对抗')}")

    cn = [s for s in skills if any(ord(c) > 127 for c in s["id"])]
    if cn:
        status, detail = req("/api/skill/" + urllib.parse.quote(cn[0]["id"]))
        check("中文 id 的 URL 能正确解码", status == 200 and detail.get("html"),
              f"{cn[0]['id']} → html {len(detail.get('html', ''))} 字符")

    status, preview = req("/api/scan/preview")
    check("扫描预览可用", status == 200 and "pending" in preview,
          f"待收录 {len(preview.get('pending', []))}，已收录 {preview.get('alreadyCount')}")

    print("\n--- 安全 ---")
    status, msg = req("/api/state", headers={"Host": "evil.example.com"})
    check("伪造 Host 头被拒（防 DNS rebinding）", status == 403, f"HTTP {status}")

    request = urllib.request.Request(BASE + "/api/collect", data=b"paths=1", method="POST")
    request.add_header("Content-Type", "text/plain")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            check("非 JSON 写请求被拒", False, f"竟然返回 {resp.status}")
    except urllib.error.HTTPError as exc:
        check("非 JSON 写请求被拒", exc.code == 400, f"HTTP {exc.code}")

    for evil in ("../../../../Windows/win.ini", "..\\..\\skm.py"):
        status, _ = req("/api/file?path=" + urllib.parse.quote(evil))
        check(f"路径穿越被拒（{evil[:24]}）", status in (403, 404), f"HTTP {status}")

    status, _ = req("/api/file?path=" + urllib.parse.quote("categories.json"))
    check("读技能库内文件放行", status == 200, f"HTTP {status}")

    print("\n--- 写操作 ---")
    target = skills[0]["id"]
    status, result = req("/api/skill/" + urllib.parse.quote(target), "PUT",
                         {"category": "开发工程/后端", "tags": ["测试标签"],
                          "starred": True, "summary": "被接口测试改过"})
    record = result.get("record", {})
    check("PUT 改分类/标签/收藏",
          status == 200 and record.get("category") == "开发工程/后端"
          and record.get("tags") == ["测试标签"] and record.get("starred") is True)

    catalog = json.loads((TESTDIR / "catalog.json").read_text(encoding="utf-8"))["skills"][target]
    meta = json.loads((TESTDIR / "skills" / target / "skill.json").read_text(encoding="utf-8"))
    check("catalog.json 与 skill.json 同步",
          all(catalog.get(k) == meta.get(k) for k in ("category", "tags", "starred", "summary")))

    status, created = req("/api/skill/text", "POST", {
        "name": "接口测试技能", "summary": "临时建的",
        "content": "# 接口测试技能\n\n## 步骤\n1. 一\n2. 二\n\n```bash\necho hi\n```",
        "category": "未分类"})
    new_id = created.get("record", {}).get("id")
    check("POST 粘贴新建并落盘",
          status == 200 and (TESTDIR / "skills" / str(new_id) / "SKILL.md").is_file(),
          f"id={new_id}")

    status, cat_result = req("/api/category", "POST", {"name": "接口测试分类", "parent": ""})
    check("POST 新建分类",
          status == 200 and any(c["name"] == "接口测试分类"
                                for c in cat_result.get("categories", [])))

    status, deleted = req("/api/skill/" + urllib.parse.quote(str(new_id)), "DELETE")
    check("DELETE 软删除到 _trash",
          status == 200 and (TESTDIR / "_trash" / str(deleted.get("trashedAs"))).is_dir(),
          f"trashedAs={deleted.get('trashedAs')}")
    left = json.loads((TESTDIR / "catalog.json").read_text(encoding="utf-8"))["skills"]
    check("删除后 catalog 已摘除该条目", str(new_id) not in left)

    print("\n--- 批量改分类：部分失败不能连坐 ---")
    good_ids = [s["id"] for s in skills if s["id"] not in (target, str(new_id))][:2]
    batch = [good_ids[0], "no-such-skill-xyz", good_ids[1]]
    ok, failed = 0, []
    for one in batch:
        status, _ = req("/api/skill/" + urllib.parse.quote(one), "PUT",
                        {"category": "未分类"})
        if status == 200:
            ok += 1
        else:
            failed.append(one)
    check("一批里混一个坏 id：另外两个照样改成功",
          ok == 2 and len(failed) == 1,
          f"成功 {ok}，失败 {failed}")
    after = json.loads((TESTDIR / "catalog.json").read_text(encoding="utf-8"))["skills"]
    check("成功的两个已落盘为未分类",
          all(after[i]["category"] == "未分类" for i in good_ids))

    print("\n--- 正文搜索 /api/search ---")
    status, none = req("/api/search?q=")
    check("空搜索词直接返回空，连索引都不建",
          status == 200 and none["ids"] == [] and none["scanned"] == 0)

    status, miss = req("/api/search?q=" + urllib.parse.quote("zzz查不到的词9f3a"))
    check("搜不到时正常返回空列表（不是报错）",
          status == 200 and miss["ids"] == [] and miss["scanned"] > 0,
          f"扫了 {miss.get('scanned')} 个")

    tokens = code_only_tokens()
    check("测试素材就位：技能库里有只在代码块里出现的词",
          len(tokens) >= 2, f"找到 {len(tokens)} 个")

    # 开发日志 3.2 记的那几个词就是当初复现这个 bug 的证据，优先用它们（报错时好读）；
    # 库内容变了导致不在了就退回自动挑一个。
    KNOWN = ("kerberoast", "bloodhound", "nuclei", "openssl", "amass", "shodan")
    word = next((w for w in KNOWN if w in tokens), None)
    code_only = tokens[word] if word else set()
    if word is None:
        word, code_only = pick_token(tokens)
    status, found = req("/api/search?q=" + urllib.parse.quote(word))
    expected = body_hits(word)
    got = set(found.get("ids", []))
    check("能搜到只在 ```代码块```里出现的词（剥代码块的老 bug 已修）",
          status == 200 and got == expected and code_only <= got,
          f"{word} → {sorted(got)}（全文含这个词的是 {sorted(expected)}）")
    check("命中片段里能看到这个词（说明是真在正文里找到的）",
          all(word in found["snippets"].get(i, "").lower() for i in got)
          and found.get("ms") is not None,
          f"用时 {found.get('ms')} ms")

    other = pick_pair(tokens, word)
    _, hit_a = req("/api/search?q=" + urllib.parse.quote(word))
    _, hit_b = req("/api/search?q=" + urllib.parse.quote(other))
    _, hit_both = req("/api/search?q=" + urllib.parse.quote(f"{word} {other}"))
    check("多词按 AND：结果必须落在两个词各自命中集的交集里",
          set(hit_both["ids"]) <= set(hit_a["ids"]) & set(hit_b["ids"])
          and hit_both["words"] == [word, other]
          and len(set(hit_both["ids"]) & set(hit_a["ids"]) & set(hit_b["ids"])) > 0,
          f"{word}({len(hit_a['ids'])}) ∩ {other}({len(hit_b['ids'])}) "
          f"= {len(hit_both['ids'])}")

    print("\n--- 分发到项目（第四步）---")
    proj = TESTDIR / "_fake_project"
    proj.mkdir(exist_ok=True)
    status, none = req("/api/projects")
    check("项目清单可读", status == 200 and isinstance(none.get("projects"), list),
          f'已登记 {len(none.get("projects", []))} 个')

    status, bad = req("/api/project", "POST", {"name": "空项目", "path": ""}, expect_rc=400)
    check("新建项目缺参数被拒", status == 400, str(bad)[:60])
    status, bad = req("/api/project", "POST",
                      {"name": "x", "path": str(TESTDIR / "no-such-dir")}, expect_rc=400)
    check("新建项目路径不存在被拒", status == 400 and "不存在" in str(bad), str(bad)[:60])

    status, made = req("/api/project", "POST", {"name": "myapp", "path": str(proj)})
    check("POST 新建项目", status == 200
          and any(p["name"] == "myapp" for p in made.get("projects", [])))
    saved = json.loads((TESTDIR / "projects.json").read_text(encoding="utf-8"))
    check("项目写进了 projects.json", "myapp" in saved.get("projects", {}))

    two = [s["id"] for s in skills[:2]]
    ids_q = urllib.parse.quote(",".join(two))
    status, prev = req(f"/api/send/preview?project=myapp&ids={ids_q}")
    check("分发预览只报告不落盘",
          status == 200 and [p["action"] for p in prev["plan"]] == ["add", "add"]
          and not (proj / ".opencode" / "skills").exists(),
          str([p["action"] for p in prev.get("plan", [])]))

    status, sent = req("/api/send", "POST", {"project": "myapp", "ids": two, "mode": "copy"})
    check("POST 分发：文件落到项目的 .opencode\\skills 下",
          status == 200 and all((proj / ".opencode" / "skills" / i / "SKILL.md").is_file()
                                for i in two),
          str(sent.get("added")))
    check("分发后 usedBy 里出现这个项目",
          all("myapp" in json.loads(
              (TESTDIR / "skills" / i / "skill.json").read_text(encoding="utf-8"))
              .get("usedBy", []) for i in two))
    rec = json.loads((TESTDIR / "_projects" / "myapp" / "records.json").read_text(encoding="utf-8"))
    check("写了分发记录（含哈希与模式）",
          set(rec["records"]) == set(two)
          and all(v.get("hash") and v.get("mode") == "copy" for v in rec["records"].values()))

    status, again = req(f"/api/send/preview?project=myapp&ids={ids_q}")
    check("再分发时预判为「已一致」",
          all(p["action"] == "same" for p in again["plan"]))

    status, st = req("/api/project/status?project=myapp")
    check("项目状态：一致", status == 200
          and all(r["state"] == "ok" for r in st["rows"] if r["id"] in two), str(st)[:80])

    (proj / ".opencode" / "skills" / two[0] / "SKILL.md").unlink()
    status, st = req("/api/project/status?project=myapp")
    check("项目里文件被删了状态能看出来",
          any(r["state"] == "missing" for r in st["rows"]))

    status, rm = req("/api/project/remove", "POST", {"project": "myapp", "ids": two})
    check("从项目移除（库里的还在）",
          status == 200 and len(rm.get("removed", [])) == 2
          and (TESTDIR / "skills" / two[0] / "SKILL.md").is_file())
    check("移除后 usedBy 里不再有这个项目",
          all("myapp" not in json.loads(
              (TESTDIR / "skills" / i / "skill.json").read_text(encoding="utf-8"))
              .get("usedBy", []) for i in two))

    status, gone = req("/api/project/myapp", "DELETE")
    check("DELETE 移除项目登记",
          status == 200 and all(p["name"] != "myapp" for p in gone.get("projects", [])))

    status, err = req("/api/send/preview?project=nope&ids=x", expect_rc=404)
    check("对没登记的项目要友好报错",
          status == 404 and "没有登记过项目" in str(err) and "Traceback" not in str(err))

    print("\n--- 备份 / 体检（第五步）---")
    status, doc = req("/api/doctor")
    check("GET /api/doctor：干净的库没毛病",
          status == 200 and doc.get("errors") == 0 and doc.get("issues") == [],
          f"{len(doc.get('issues', []))} 条")
    status, chk = req("/api/check")
    check("GET /api/check 四块都在",
          status == 200 and all(k in chk for k in
                                ("needs", "dangling", "unused", "emptyCategories")))
    status, tr = req("/api/trash")
    check("GET /api/trash 可读", status == 200 and isinstance(tr.get("items"), list))

    status, fixed = req("/api/doctor", "POST", {"fix": True})
    check("POST /api/doctor 能修（干净库上修了 0 条）",
          status == 200 and fixed.get("fixed") == 0, f"fixed={fixed.get('fixed')}")

    status, zipped = req("/api/backup.zip")
    check("GET /api/backup.zip 返回 zip",
          status == 200 and isinstance(zipped, bytes) and zipped[:2] == b"PK",
          f"{len(zipped) if isinstance(zipped, bytes) else '?'} 字节")

    # 把某个 Skill 的 skill.json 改坏，doctor 要能看出来。
    # 注意只能改 skill.json：服务端的 catalog 在内存里，改磁盘上的 catalog.json 不生效。
    # 「幽灵分类」那类要动 catalog 的问题由 tests\test_backup.py 在进程之间覆盖。
    victim = skills[4]["id"]
    meta_path = TESTDIR / "skills" / victim / "skill.json"
    backup_meta = meta_path.read_text(encoding="utf-8")
    meta = json.loads(backup_meta)
    meta["category"] = "不存在的分类/子分类"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    status, doc = req("/api/doctor")
    codes = [i["code"] for i in doc.get("issues", [])]
    check("doctor 看得出 catalog 与 skill.json 不一致",
          status == 200 and "两侧不一致" in codes, str(codes))
    check("doctor 标明哪些能自动修",
          any(i["fixable"] for i in doc.get("issues", []) if i["code"] == "两侧不一致"))
    status, fixed = req("/api/doctor", "POST", {"fix": True})
    after = json.loads(meta_path.read_text(encoding="utf-8"))
    record = json.loads((TESTDIR / "catalog.json").read_text(encoding="utf-8"))["skills"][victim]
    check("doctor --fix 以 catalog 为准重写 skill.json",
          status == 200 and fixed.get("fixed", 0) >= 1
          and after.get("category") == record.get("category"),
          f"fixed={fixed.get('fixed')}，现在是 {after.get('category')}")

    status, err = req("/api/restore", "POST", {}, expect_rc=400)
    check("restore 不给 id 被拒", status == 400 and "指定" in str(err), str(err)[:50])
    status, err = req("/api/restore", "POST", {"id": "no-such-id"}, expect_rc=400)
    check("restore 不存在的 id：友好报错",
          status == 400 and "回收站" in str(err) and "Traceback" not in str(err))

    print("\n--- 错误处理 ---")
    status, msg = req("/api/skill/no-such-skill-xyz")
    check("查不存在的 Skill 给友好报错",
          status == 404 and "技能库里没有" in str(msg), str(msg)[:50])
    status, msg = req("/api/nope")
    check("未知接口给友好报错", status == 404 and "没有这个接口" in str(msg))

finally:
    server.terminate()
    try:
        out, _ = server.communicate(timeout=6)
    except subprocess.TimeoutExpired:
        server.kill()
        out, _ = server.communicate()

    # 服务端 stderr 里不该有客户端断开造成的堆栈
    noise = [line for line in (out or "").splitlines()
             if "Traceback" in line or "ConnectionReset" in line]
    print(f"\n[{'PASS' if not noise else 'FAIL'}] 服务端无噪音堆栈"
          + (f"   {noise[:2]}" if noise else ""))

    bad = [label for label, ok, _ in results if not ok]
    print(f"\n{'=' * 70}")
    print(f"结果：{len(results) - len(bad)}/{len(results)} 项通过")
    if bad:
        print("失败项：", bad)
    print("=" * 70)
    sys.exit(1 if bad or noise else 0)
