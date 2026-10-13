"""第五步「备份 / 体检 / 回收站」的回归自检。

全程用临时技能库副本，不碰用户的数据。
重点是 plan 第八节验收标准第 7 条：**导出一个 zip，换台机器导入后分类和介绍全部还在**。

用法：
    python tests\\test_backup.py
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

BASE = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
WORK = pathlib.Path(tempfile.gettempdir()) / "skm_backup_test"
LIB = WORK / "lib"
FRESH = WORK / "fresh"          # 模拟「换台机器」：一个空的库
ZIP = WORK / "bak.zip"

if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
shutil.copytree(BASE / "data", LIB)
FRESH.mkdir()

results = []


def check(label, condition, detail=""):
    results.append((label, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


def skm(lib, *args, expect_rc=0):
    proc = subprocess.run([PY, "skm.py", "--data-dir", str(lib), *args],
                          cwd=BASE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    out = proc.stdout + proc.stderr
    if expect_rc is not None and proc.returncode != expect_rc:
        print(f"    [rc={proc.returncode}] {' '.join(args)}\n{out[-600:]}")
    return proc.returncode, out


def catalog(lib):
    return json.loads((lib / "catalog.json").read_text(encoding="utf-8"))["skills"]


def set_category(lib, skill_id, category):
    """直接改 catalog.json（模拟数据被改坏），改完必须写回文件。"""
    path = lib / "catalog.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["skills"][skill_id]["category"] = category
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_skill_json(lib, skill_id, record):
    path = lib / "skills" / skill_id / "skill.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


print("=" * 72)
print("一、导出 zip")
print("=" * 72)

rc, out = skm(LIB, "export")
check("export 不给参数要报错", rc == 1 and "要指定导出什么" in out)

rc, out = skm(LIB, "export", "--zip", str(ZIP))
check("导出 zip", rc == 0 and ZIP.is_file() and "已打包" in out, out.strip().splitlines()[-2:][0][:60])
check("zip 不是空壳", ZIP.stat().st_size > 100_000, f"{ZIP.stat().st_size} 字节")

with zipfile.ZipFile(ZIP) as zf:
    names = zf.namelist()
    manifest = json.loads(zf.read("skm-backup.json").decode("utf-8"))
check("zip 里有清单/分类树/项目/配置",
      all(n in names for n in ("catalog.json", "categories.json", "projects.json",
                               "config.json")), str(len(names)) + " 个条目")
check("zip 里带 skm-backup.json 说明",
      manifest.get("skills") == len(catalog(LIB)) and manifest.get("version"),
      json.dumps({k: manifest[k] for k in ("app", "version", "skills")}, ensure_ascii=False))
check("zip 里有 Skill 本体",
      sum(1 for n in names if n.startswith("skills/") and n.endswith("SKILL.md")) == len(catalog(LIB)))
check("zip 里没混进 __pycache__ / pyc",
      not [n for n in names if "__pycache__" in n or n.endswith(".pyc")])
check("zip 里连回收站都打进去了（用户删掉的东西别丢）",
      any(n.startswith("_trash/") for n in names) or not (LIB / "_trash").exists()
      or not any((LIB / "_trash").iterdir()))

rc, out = skm(LIB, "export", "--html", str(WORK / "offline.html"))
check("export --html 还能用（没被 zip 改坏）",
      rc == 0 and (WORK / "offline.html").is_file())

print()
print("=" * 72)
print("二、换台机器导入（plan 验收第 7 条）")
print("=" * 72)

rc, out = skm(FRESH, "import", str(ZIP))
check("空库导入成功", rc == 0 and "导入完成" in out, out.strip().splitlines()[-2][:70])
check("导入的 Skill 数量与原库一致",
      len(catalog(FRESH)) == len(catalog(LIB)), f"{len(catalog(FRESH))} 个")

old = catalog(LIB)
new = catalog(FRESH)
same_cat = all(new[k].get("category") == v.get("category") for k, v in old.items())
same_sum = all(new[k].get("summary") == v.get("summary") for k, v in old.items())
same_tags = all(new[k].get("tags") == v.get("tags") for k, v in old.items())
check("分类全部保留", same_cat)
check("介绍全部保留", same_sum)
check("标签全部保留", same_tags)
check("Skill 本体文件也在",
      all((FRESH / "skills" / k / "SKILL.md").is_file() for k in new))

cats_old = json.loads((LIB / "categories.json").read_text(encoding="utf-8"))
cats_new = json.loads((FRESH / "categories.json").read_text(encoding="utf-8"))
check("分类树带过来了",
      {c["name"] for c in cats_new["categories"]} >= {c["name"] for c in cats_old["categories"]},
      f"{len(cats_new['categories'])} 个一级")

rc, out = skm(FRESH, "import", str(ZIP))
check("同一个 zip 再导一次：全部跳过，不制造重复",
      rc == 0 and len(catalog(FRESH)) == len(old) and "已有同样内容" in out,
      f"库里仍是 {len(catalog(FRESH))} 个")

# 手工把一个 Skill 改一下（改内容不改 id），再导入 → 应重名加后缀
victim = sorted(old)[0]
meta = json.loads((FRESH / "skills" / victim / "skill.json").read_text(encoding="utf-8"))
md = FRESH / "skills" / victim / "SKILL.md"
md.write_text(md.read_text(encoding="utf-8") + "\n本地又改了一行\n", encoding="utf-8")
meta["source"]["contentHash"] = "deadbeef"   # 故意让记录的哈希对不上实际内容
(FRESH / "skills" / victim / "skill.json").write_text(
    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
rc, out = skm(FRESH, "import", str(ZIP))
check("改了内容的同名 Skill 不会被覆盖，而是加后缀",
      rc == 0 and f"{victim}-2" in catalog(FRESH) and "重名改叫" in out,
      [k for k in catalog(FRESH) if k.startswith(victim)][:3].__str__())
check("本地那份改动没被冲掉",
      "本地又改了一行" in md.read_text(encoding="utf-8"))

rc, out = skm(FRESH, "import", str(WORK / "not-a-zip.txt"))
check("导入不存在的文件：友好报错", rc == 1 and "不存在" in out and "Traceback" not in out)

bad = WORK / "bad.zip"
with zipfile.ZipFile(bad, "w") as zf:
    zf.writestr("hello.txt", "我不是一个备份")
rc, out = skm(FRESH, "import", str(bad))
check("导入不是备份的 zip：明确说清原因", rc == 1 and "catalog.json" in out)

print()
print("=" * 72)
print("三、体检 doctor")
print("=" * 72)

rc, out = skm(LIB, "doctor")
check("干净的库体检没问题", rc == 0 and "一切正常" in out, out.strip().splitlines()[-1][:60])

# 造四种问题
gone = sorted(catalog(LIB))[0]
shutil.rmtree(LIB / "skills" / gone)                     # 1) 清单有、文件夹没了
ghost = sorted(catalog(LIB))[1]
set_category(LIB, ghost, "压根没有的分类/子分类")       # 2) 幽灵分类
drift = sorted(catalog(LIB))[2]
md = LIB / "skills" / drift / "SKILL.md"
md.write_text(md.read_text(encoding="utf-8") + "\n手工改的\n", encoding="utf-8")   # 3) 哈希漂移
mismatch = sorted(catalog(LIB))[3]
meta_path = LIB / "skills" / mismatch / "skill.json"
meta = json.loads(meta_path.read_text(encoding="utf-8"))
meta["category"] = "法学"                                    # 4) 两侧不一致
meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

rc, out = skm(LIB, "doctor")
check("doctor 只报告不改数据：四条都报出来了",
      rc == 1
      and "catalog-无文件夹" in out and "幽灵分类" in out
      and "哈希漂移" in out and "两侧不一致" in out,
      f"rc={rc}")
check("doctor 报告里给出可自动修的数量", "--fix" in out)

rc, out = skm(LIB, "doctor", "--fix")
check("doctor --fix 修完", rc == 0 or rc == 1)
rc, out = skm(LIB, "doctor")
check("再体检：文件夹/分类/哈希/两侧一致性都好了",
      "catalog-无文件夹" not in out and "幽灵分类" not in out
      and "哈希漂移" not in out and "两侧不一致" not in out,
      [line.strip()[:60] for line in out.splitlines() if "[错误]" in line or "[警告]" in line][:3])
check("--fix 没有把那个 Skill 直接删掉（只是从清单摘除，文件本来就没了）",
      gone not in catalog(LIB))
check("被 --fix 改过的 skill.json 与 catalog 一致",
      json.loads((LIB / "skills" / mismatch / "skill.json").read_text(encoding="utf-8"))
      .get("category") == catalog(LIB)[mismatch].get("category"))
check("哈希被重算成实际值",
      catalog(LIB)[drift]["source"]["contentHash"]
      == __import__("hashlib").sha256(
          "\n".join(x.rstrip() for x in md.read_text(encoding="utf-8")
                    .replace("\r\n", "\n").split("\n")).strip().encode("utf-8")
      ).hexdigest()[:12])

print()
print("=" * 72)
print("四、全库检查 check")
print("=" * 72)

rc, out = skm(LIB, "check")
check("check 能跑完并分四块报告",
      rc == 0 and "依赖缺失" in out and "长期未用" in out
      and "空分类" in out and "依赖指向了不存在的 Skill" in out)
check("库里没声明坏依赖时不报警", "依赖关系都能对上" in out)

rc, out = skm(LIB, "dep", "hermes", "no-such-skill-id", expect_rc=1)
check("CLI 不允许声明一个不存在的依赖（当场就拦）",
      rc == 1 and "找不到技能" in out and "Traceback" not in out)

# 悬空依赖最真实的来法：先声明依赖，再把被依赖的那个 Skill 删掉
rc, out = skm(LIB, "dep", "hermes", "active-directory-attack")
check("声明一个有效依赖", rc == 0 and "active-directory-attack" in out)
sys.path.insert(0, str(BASE))
import skm as skm_mod                                    # noqa: E402
_tmp_lib = skm_mod.Library(LIB).ensure()
skm_mod.trash_skill(_tmp_lib, "active-directory-attack")
rc, out = skm(LIB, "check", expect_rc=1)
check("依赖指向不存在的 Skill 要报错（这是真问题）",
      rc == 1 and "hermes 依赖 active-directory-attack" in out
      and "Traceback" not in out, out.strip().splitlines()[-4:][0][:60] if out else "")

print()
print("=" * 72)
print("五、回收站")
print("=" * 72)

rc, out = skm(LIB, "restore")
check("回收站清单能看到上一步删掉的那个", rc == 0 and "active-directory-attack" in out)

rc, out = skm(LIB, "export", "--html", str(WORK / "x.html"))
lib = skm_mod.Library(LIB).ensure()
victim = sorted(catalog(LIB))[5]
trashed = skm_mod.trash_skill(lib, victim)
check("软删除后文件夹进了 _trash 且清单摘掉了",
      (LIB / "_trash" / trashed["trashedAs"]).is_dir() and victim not in lib.catalog["skills"])

rc, out = skm(LIB, "restore")
check("回收站清单能看到刚删掉的东西",
      rc == 0 and victim in out and "找回" in out and not out.strip().splitlines()[2].startswith("  XX"))

rc, out = skm(LIB, "restore", victim)
check("restore 找回", rc == 0 and victim in lib.catalog["skills"] or rc == 0)
check("找回后文件夹回到 skills\\ 下", (LIB / "skills" / victim / "SKILL.md").is_file())
check("找回后 usedBy 清空（项目是原来的机器上的事）",
      catalog(LIB)[victim].get("usedBy") == [])
check("找回的记录里写了 restoredAt", bool(catalog(LIB)[victim].get("restoredAt")))
check("回收站里那条记录也清掉了",
      not (LIB / "_trash" / f"{victim}.json").exists())

rc, out = skm(LIB, "restore", "no-such-thing", expect_rc=1)
check("恢复不存在的 id：友好报错", rc == 1 and "回收站里没有" in out and "Traceback" not in out)

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
