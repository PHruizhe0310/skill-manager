"""一次跑完全部回归自检。

    python tests\\run_all.py

每个脚本单独起进程，任何一个失败都会在最后汇总里标出来。
"""
import pathlib
import subprocess
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent.parent
SUITES = [
    ("命令行 + 数据一致性", "test_data.py"),
    ("Markdown 渲染 + XSS", "test_markdown.py"),
    ("网页接口", "test_api.py"),
    ("分发到项目", "test_send.py"),
    ("备份 / 体检 / 回收站", "test_backup.py"),
    ("AI 自动分类", "test_ai.py"),
    ("浏览器端交互", "test_ui.py"),
]

failed = []
for label, script in SUITES:
    print()
    print("#" * 72)
    print(f"# {label}  ({script})")
    print("#" * 72)
    started = time.perf_counter()
    proc = subprocess.run([sys.executable, str(BASE / "tests" / script)],
                          cwd=BASE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    tail = [line for line in proc.stdout.splitlines() if line.strip()][-3:]
    print("\n".join(tail))
    spent = time.perf_counter() - started
    ok = proc.returncode == 0
    print(f"→ {'通过' if ok else '失败'}（{spent:.1f} 秒, rc={proc.returncode}）")
    if not ok:
        failed.append(script)
        if proc.stderr.strip():
            print(proc.stderr.strip()[-800:])

print()
print("=" * 72)
if failed:
    print("有失败：" + "、".join(failed))
else:
    print(f"全部 {len(SUITES)} 套自检通过")
print("=" * 72)
sys.exit(1 if failed else 0)
