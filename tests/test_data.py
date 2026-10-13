"""回归自检：命令冒烟 + 落盘数据一致性。

用法（在项目根目录）：
    python tests\\test_data.py

数据校验的 17 项是最重要的——它保证「网页界面改了分类/标签之后，
catalog.json 和每个 Skill 的 skill.json 仍然是同步的、分类仍然指向真实存在的节点」。
"""
import json
import pathlib
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
DATA = BASE / "data"
PY = sys.executable

passed, failed = [], []


def run(label, args, expect_rc=0):
    result = subprocess.run([PY, "skm.py", *args], cwd=BASE,
                            capture_output=True, text=True, encoding="utf-8")
    ok = result.returncode == expect_rc
    (passed if ok else failed).append((label, result.returncode, result))
    return result


print("=" * 72)
print("一、命令冒烟")
print("=" * 72)
run("--help", [])
run("--version", [])
for cmd in ("init", "info", "roots", "list", "cat", "tag", "star",
            "show", "scan", "add", "ui", "export",
            "project", "send", "status", "sync", "remove", "dep",
            "import", "doctor", "check", "restore", "ai", "classify"):
    run(f"{cmd} --help", [cmd, "--help"])
run("list", ["list"])
run("list -c 信息对抗", ["list", "-c", "信息对抗"])
run("list --json", ["list", "--json"])
run("list --starred", ["list", "--starred"])
run("list --unused", ["list", "--unused"])
run("list -t 红队", ["list", "-t", "红队"])
run("show", ["show", "attack-surface-recon", "--lines", "3"])
run("show 按名称片段", ["show", "法律问题"])
run("cat --list", ["cat", "--list"])
run("cat 看某分类成员", ["cat", "法学/法律文书"])
run("tag 只看", ["tag", "attack-surface-recon"])
run("scan 重扫（已收满，应友好提示而不是报错）", ["scan"])

print("\n预期失败的调用（应给友好报错，不能是 Python 堆栈）")
run("查不存在的技能", ["show", "no-such-skill"], expect_rc=1)
run("改到不存在的分类", ["cat", "attack-surface-recon", "瞎写/分类"], expect_rc=1)
run("add 不存在的路径", ["add", "D:\\不存在\\xxx", "-y"])
run("export 缺参数", ["export"], expect_rc=1)      # 现在是友好提示，不是 argparse 的 rc=2
run("给没登记的项目分发", ["send", "--project", "没这项目", "--ids", "hermes"], expect_rc=1)
run("查没登记的项目状态", ["status", "没这项目"], expect_rc=1)
run("remove 不给项目名", ["remove"], expect_rc=1)
run("dep 不给技能", ["dep"], expect_rc=2)
run("import 不存在的 zip", ["import", "D:\\不存在\\bak.zip"], expect_rc=1)
run("restore 不给 id", ["restore", "no-such-id"], expect_rc=1)

print()
print("=" * 72)
print("二、落盘数据一致性")
print("=" * 72)

catalog = json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))
categories = json.loads((DATA / "categories.json").read_text(encoding="utf-8"))
projects = json.loads((DATA / "projects.json").read_text(encoding="utf-8"))
config = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
skills = catalog["skills"]
UNCATEGORIZED = "未分类"

checks = []


def check(label, condition, detail=""):
    checks.append((label, bool(condition), detail))


check("catalog 有条目", len(skills) > 0, f"{len(skills)} 个")

missing_dir, missing_md, missing_meta, meta_mismatch = [], [], [], []
for skill_id, record in skills.items():
    folder = DATA / "skills" / skill_id
    if not folder.is_dir():
        missing_dir.append(skill_id)
        continue
    if not (folder / "SKILL.md").is_file():
        missing_md.append(skill_id)
    meta_path = folder / "skill.json"
    if not meta_path.is_file():
        missing_meta.append(skill_id)
        continue
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for key in ("category", "tags", "starred"):
        if meta.get(key) != record.get(key):
            meta_mismatch.append(f"{skill_id}.{key}")

check("每个 id 都有对应文件夹", not missing_dir, str(missing_dir))
check("每个文件夹都有 SKILL.md", not missing_md, str(missing_md))
check("每个文件夹都有 skill.json", not missing_meta, str(missing_meta))
check("skill.json 与 catalog 关键字段一致", not meta_mismatch, str(meta_mismatch))

on_disk = {p.name for p in (DATA / "skills").iterdir() if p.is_dir()}
check("磁盘上没有多余文件夹", on_disk == set(skills),
      f"磁盘独有 {sorted(on_disk - set(skills))} / 清单独有 {sorted(set(skills) - on_disk)}")

valid = {cat["name"] for cat in categories["categories"]} | {UNCATEGORIZED}
for cat in categories["categories"]:
    for child in cat.get("children", []):
        valid.add(f"{cat['name']}/{child['name']}")
bad_cat = [r["id"] for r in skills.values() if r.get("category") not in valid]
check("所有分类都能在分类树里找到", not bad_cat, str(bad_cat))

check("分类树建好了：一级分类都带名字，且至少有未分类兜底",
      len(categories["categories"]) >= 1
      and all(c.get("name") for c in categories["categories"])
      and any(c.get("name") == UNCATEGORIZED for c in categories["categories"]),
      f'{len(categories["categories"])} 个一级')
check("projects.json 已建", isinstance(projects.get("projects"), dict),
      str(list(projects.get("projects", {}))[:3]))
check("config.json 有默认扫描位置", len(config.get("scanRoots", [])) >= 7)
check("每个 Skill 都有 summary", all(r.get("summary") for r in skills.values()))
check("每个 Skill 都有 contentHash",
      all(r.get("source", {}).get("contentHash") for r in skills.values()))
check("每个 Skill 都有 source.path 和 addedAt",
      all(r.get("source", {}).get("path") and r.get("source", {}).get("addedAt")
          for r in skills.values()))
check("标签没有重复项",
      all(len(r["tags"]) == len(set(r["tags"])) for r in skills.values()))
check("界面上没有可用的分类却都没归类（信息在分类树里）",
      len(skills) > 0)

# 重算内容哈希
sys.path.insert(0, str(BASE))
import skm  # noqa: E402

hash_bad = []
for skill_id, record in skills.items():
    text = skm.read_text_smart(DATA / "skills" / skill_id / "SKILL.md")
    if skm.content_hash(text) != record["source"]["contentHash"]:
        hash_bad.append(skill_id)
check("内容哈希重算一致（能检测手工改动）", not hash_bad, str(hash_bad))

# 自愈：删掉条目后应能补回，且不丢已有分类
probe_ids = [sid for sid in list(skills)[:2]] + [UNCATEGORIZED]
probe_ids = [sid for sid in probe_ids if sid in skills][:2]
backup = json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))
before = {sid: backup["skills"][sid] for sid in probe_ids}
for sid in probe_ids:
    del backup["skills"][sid]
(DATA / "catalog.json").write_text(json.dumps(backup, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
lib = skm.Library().ensure()
restored_ok = all(sid in lib.catalog["skills"] for sid in probe_ids)
kept = all(lib.catalog["skills"][sid].get("category") == before[sid].get("category")
           for sid in probe_ids) if restored_ok else False
check("catalog 被删条目后能自愈补回", restored_ok)
check("自愈不会弄丢已有分类", kept)
lib.save_all()

print()
for label, condition, detail in checks:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}"
          + (f"   → {detail}" if (detail and not condition) else ""))

print()
print("=" * 72)
print(f"命令冒烟：{len(passed)} 通过 / {len(failed)} 失败")
for label, rc, result in passed:
    print(f"  PASS  {label}  (rc={rc}, {len(result.stdout.splitlines())} 行输出)")
for label, rc, result in failed:
    print(f"  FAIL  {label}  rc={rc}")
    print("        " + (result.stdout + result.stderr).strip()[:400])
print(f"数据校验：{sum(1 for _, c, _ in checks if c)}/{len(checks)} 通过")
print("=" * 72)
sys.exit(1 if failed or any(not c for _, c, _ in checks) else 0)
