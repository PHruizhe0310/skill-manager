"""第四步「分发到项目」的回归自检。

用一个临时技能库副本 + 一个临时项目文件夹跑，**不碰用户的数据**。
覆盖 plan §4.4 要求的每一条：依赖顺带带上、缺依赖只提示不阻断、
同名旧版必须用户拍板（不静默覆盖）、记录可撤销。

用法：
    python tests\\test_send.py
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
WORK = pathlib.Path(tempfile.gettempdir()) / "skm_send_test"
LIB = WORK / "lib"
APP = WORK / "myapp"
OTHER = WORK / "website"

if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
shutil.copytree(BASE / "data", LIB)
APP.mkdir()
OTHER.mkdir()

results = []


def check(label, condition, detail=""):
    results.append((label, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


def skm(*args, expect_rc=0):
    """跑一条 skm 命令，返回 (rc, 输出文本)。"""
    proc = subprocess.run([PY, "skm.py", "--data-dir", str(LIB), *args],
                          cwd=BASE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    out = proc.stdout + proc.stderr
    if expect_rc is not None and proc.returncode != expect_rc:
        print(f"    [rc={proc.returncode}] {' '.join(args)}\n{out}")
    return proc.returncode, out


def installed(project, skill_id):
    return project / ".opencode" / "skills" / skill_id / "SKILL.md"


def catalog():
    data = json.loads((LIB / "catalog.json").read_text(encoding="utf-8"))
    return data["skills"]


def records(project="myapp"):
    path = LIB / "_projects" / project / "records.json"
    return json.loads(path.read_text(encoding="utf-8"))["records"] if path.is_file() else {}


print("=" * 72)
print("一、项目登记")
print("=" * 72)

rc, out = skm("project", "list")
check("列清单不报错（不管库里有没有登记过项目）", rc == 0, out.strip().splitlines()[-1][:50])

rc, out = skm("project", "add", "myapp", str(APP))
check("登记项目", rc == 0 and "已登记" in out)
rc, out = skm("project", "add", "website", str(OTHER))
check("登记第二个项目", rc == 0)

rc, out = skm("project", "list")
check("列清单显示两个项目和已装数量",
      rc == 0 and "myapp" in out and "website" in out and "已装" in out)

rc, out = skm("project", "add", "myapp", str(APP), expect_rc=1)
check("重复登记要拦下来并给提示", rc == 1 and "已登记" in out)

rc, out = skm("project", "add", "ghost", str(WORK / "no-such-dir"), expect_rc=1)
check("登记不存在的路径要报错", rc == 1 and "不存在" in out)

rc, out = skm("status", "not-registered", expect_rc=1)
check("查未登记的项目：友好报错而不是堆栈",
      rc == 1 and "没有登记过项目" in out and "Traceback" not in out)

print()
print("=" * 72)
print("二、依赖")
print("=" * 72)

rc, out = skm("dep", "attack-surface-recon", "--cmd", "amass,httpx")
check("声明外部命令依赖", rc == 0 and "amass" in out)
rc, out = skm("dep", "attack-surface-recon", "source-code-hunting")
check("声明 Skill 之间的依赖", rc == 0 and "source-code-hunting" in out)

record = catalog()["attack-surface-recon"]
check("依赖写进了 skill.json",
      record.get("dependsOn") == ["source-code-hunting"]
      and record.get("requires") == ["amass", "httpx"],
      json.dumps({"dependsOn": record.get("dependsOn"),
                  "requires": record.get("requires")}, ensure_ascii=False))

rc, out = skm("dep", "attack-surface-recon", "attack-surface-recon", expect_rc=1)
check("不能依赖自己", rc == 1 and "自己" in out)

print()
print("=" * 72)
print("三、分发（复制）")
print("=" * 72)

rc, out = skm("send", "--project", "myapp", "--ids",
              "attack-surface-recon,active-directory-attack", "-y")
check("分发前先打印计划", rc == 0 and "准备分发" in out and "新增" in out)
check("文件真的落到项目的 .opencode\\skills 下",
      installed(APP, "attack-surface-recon").is_file()
      and installed(APP, "active-directory-attack").is_file())
check("依赖被顺带带进来并标注",
      installed(APP, "source-code-hunting").is_file() and "因依赖" in out)
check("缺的外部命令只提示、不阻断",
      "amass" in out and "不影响分发" in out)
check("技能库本体没被动过（分发是复制，不是搬走）",
      (LIB / "skills" / "attack-surface-recon" / "SKILL.md").is_file())

rec = records()
check("写了分发记录", set(rec) >= {"attack-surface-recon", "active-directory-attack",
                                  "source-code-hunting"}, ", ".join(sorted(rec)))
check("记录里有哈希、时间和模式",
      rec["attack-surface-recon"].get("mode") == "copy"
      and rec["attack-surface-recon"].get("hash")
      and rec["attack-surface-recon"].get("sentAt"))
check("记录标注了谁是被依赖带进来的",
      rec["source-code-hunting"].get("viaDependency") == "attack-surface-recon")
check("usedBy 回写到 catalog 和 skill.json",
      catalog()["attack-surface-recon"].get("usedBy") == ["myapp"])
meta = json.loads((LIB / "skills" / "attack-surface-recon" / "skill.json").read_text(encoding="utf-8"))
check("catalog 与 skill.json 的 usedBy 一致", meta.get("usedBy") == ["myapp"])

rc, out = skm("send", "--project", "myapp", "--ids", "attack-surface-recon", "-y")
check("内容一致时不重复分发", "已一致" in out and "已装" not in out.split("已一致")[0][-20:])

print()
print("=" * 72)
print("四、同名旧版：绝不静默覆盖")
print("=" * 72)

target = installed(APP, "attack-surface-recon")
target.write_text(target.read_text(encoding="utf-8") + "\n本地手改的一行\n", encoding="utf-8")

rc, out = skm("status", "myapp")
check("status 看得出项目里那份已过期", "过期" in out)

rc, out = skm("send", "--project", "myapp", "--ids", "attack-surface-recon",
              "--overwrite", "skip", "-y")
check("--overwrite skip 保留本地那份",
      "本地手改的一行" in target.read_text(encoding="utf-8"))

rc, out = skm("send", "--project", "myapp", "--ids", "attack-surface-recon",
              "--overwrite", "backup", "-y")
check("--overwrite backup（默认）先把旧版备份再覆盖",
      "本地手改的一行" not in target.read_text(encoding="utf-8"))
backups = list((LIB / "_projects" / "myapp" / "backup").glob("attack-surface-recon-*"))
backups_dir = LIB / "_projects" / "myapp" / "backup"
check("旧版进了 data\\_projects\\<项目>\\backup",
      len(backups) == 1 and "本地手改的一行"
      in (backups[0] / "SKILL.md").read_text(encoding="utf-8"),
      str([p.name for p in backups]))

rc, out = skm("send", "--project", "myapp", "--ids", "attack-surface-recon",
              "--overwrite", "replace", "-y")
check("--overwrite replace 直接覆盖不留备份",
      rc == 0 and "本地手改的一行" not in target.read_text(encoding="utf-8"))

# 非交互（管道）环境下不给 --overwrite 也不该静默覆盖
target.write_text(target.read_text(encoding="utf-8") + "\n又手改一行\n", encoding="utf-8")
rc, out = skm("send", "--project", "myapp", "--ids", "attack-surface-recon", "-y",
              expect_rc=0)
check("非交互 + 没指定策略时默认走备份覆盖（不是默默跳过）",
      "又手改一行" not in target.read_text(encoding="utf-8"))

# 同一秒里备份两次也不能撞名失败（自动化测试跑得快，这个坑是实测出来的）
target.write_text(target.read_text(encoding="utf-8") + "\n再手改\n", encoding="utf-8")
rc, out = skm("send", "--project", "myapp", "--ids", "attack-surface-recon", "-y")
target.write_text(target.read_text(encoding="utf-8") + "\n又再手改\n", encoding="utf-8")
rc2, out2 = skm("send", "--project", "myapp", "--ids", "attack-surface-recon", "-y")
check("一秒钟内连续两次覆盖：备份目录不撞名，两次都成功",
      rc == 0 and rc2 == 0 and "再手改" not in target.read_text(encoding="utf-8")
      and "又再手改" not in target.read_text(encoding="utf-8"),
      f"rc={rc}/{rc2} 备份 {len(list(backups_dir.glob('*')))} 份")

print()
print("=" * 72)
print("五、status / sync")
print("=" * 72)

rc, out = skm("status", "myapp")
check("status 列出全部并统计", rc == 0 and "共" in out and "一致" in out)

installed(APP, "active-directory-attack").unlink()
rc, out = skm("status", "myapp")
check("项目里的文件被删了要能看出来", "文件没了" in out)

rc, out = skm("sync", "myapp", "-y")
check("sync 把丢了的补回来", installed(APP, "active-directory-attack").is_file())
rc, out = skm("sync", "myapp", "-y")
check("已经一致时 sync 不做多余动作", "不用动" in out)

manual = APP / ".opencode" / "skills" / "手工放进去的"
manual.mkdir(parents=True)
(manual / "SKILL.md").write_text("# 手工放的\n", encoding="utf-8")
rc, out = skm("status", "myapp")
check("手工放进项目的 Skill 会被认出来（不是本工具装的）", "手工放入" in out)

print()
print("=" * 72)
print("六、撤销")
print("=" * 72)

rc, out = skm("remove", "myapp", "--ids", "attack-surface-recon")
check("从项目移除：文件没了", rc == 0 and not installed(APP, "attack-surface-recon").exists())
check("技能库里的本体还在（分发是复制，撤销不该动库）",
      (LIB / "skills" / "attack-surface-recon" / "SKILL.md").is_file())
check("usedBy 里不再有这个项目", catalog()["attack-surface-recon"].get("usedBy") == [])
check("记录里也摘掉了", "attack-surface-recon" not in records())
rc, out = skm("remove", "myapp", "--ids", "attack-surface-recon")
check("再移一次：友好提示本来就没有", rc == 0 and "本来就没有" in out)

rc, out = skm("project", "remove", "myapp")
check("从清单移除项目", rc == 0 and "已从清单移除" in out)
check("移除项目不动项目里的文件", installed(APP, "active-directory-attack").is_file())

print()
print("=" * 72)
print("七、链接模式与分类分发")
print("=" * 72)

rc, out = skm("project", "add", "myapp", str(APP), "--force")
skm("send", "--project", "website", "--category", "法学", "-y")
law = [p.name for p in (OTHER / ".opencode" / "skills").iterdir()]
check("按分类整批分发", rc == 0 and len(law) >= 2, "、".join(law))

rc, out = skm("send", "--project", "website", "--ids", "attack-surface-recon",
              "--mode", "link", "-y")
linked = OTHER / ".opencode" / "skills" / "attack-surface-recon"
is_link = linked.is_symlink() or getattr(linked, "is_junction", lambda: False)()
if is_link:
    check("link 模式建成了链接（改库立刻生效）", rc == 0 and is_link,
          "symlink" if linked.is_symlink() else "junction")
    (LIB / "skills" / "attack-surface-recon" / "SKILL.md").write_text(
        (LIB / "skills" / "attack-surface-recon" / "SKILL.md")
        .read_text(encoding="utf-8") + "\n链接测试\n", encoding="utf-8")
    check("库里一改，项目里立刻变（链接的意义）",
          "链接测试" in linked.joinpath("SKILL.md").read_text(encoding="utf-8"))
else:
    warn = "link 模式没建成（多半是 Windows 没给建链接权限），已跳过这两项"
    print(f"[SKIP] {warn}")
    check("link 失败时必须给友好提示而不是堆栈",
          rc == 0 and ("junction" in out or "权限" in out or "copy" in out), out[-120:])

rc, out = skm("send", "--project", "website", "--ids", "no-such-skill", "-y", expect_rc=1)
check("分发不存在的技能：友好报错", rc == 1 and "找不到技能" in out and "Traceback" not in out)

rc, out = skm("send", "--project", "website", expect_rc=1)
check("不给任何选择：友好报错", rc == 1 and "没指定" in out)

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
