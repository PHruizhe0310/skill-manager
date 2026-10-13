"""Markdown 渲染器测试。

用库里真实的内容当样本（那些 SKILL.md 里有嵌套列表、表格、大段 bash 代码块、
YAML 折叠块，是很好的压力测试），再单独测 XSS 和危险协议。

用法：
    python tests\\test_markdown.py
"""
import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import skm  # noqa: E402

samples = sorted((BASE / "data" / "skills").glob("*/SKILL.md"))
print(f"共 {len(samples)} 个真实 SKILL.md\n")

bad = []
for path in samples:
    raw = skm.read_text_smart(path)
    html = skm.render_markdown(raw)
    problems = []

    # 1. 只允许白名单里的标签
    for tag in re.findall(r"<[^>]{0,80}>", html):
        if not re.match(r"</?(?:h[1-6]|p|ul|ol|li|pre|code|strong|em|del|a|img|hr|"
                        r"table|thead|tbody|tr|th|td|blockquote|br|span)\b[^>]*>$", tag):
            problems.append(f"可疑标签 {tag[:60]}")

    # 2. 列表项数量必须与源文件严格一致（先剥代码块，否则代码块里的 - 会被误算）
    no_code = re.sub(r"```.*?```", "", raw, flags=re.S)
    lis = len(re.findall(r"<li[ >]", html))
    src_items = len(re.findall(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+", no_code, re.M))
    if lis != src_items:
        problems.append(f"<li> 数量不符：渲染 {lis} vs 源 {src_items}")

    # 3. 标签闭合平衡
    for tag in ("ul", "ol", "table", "blockquote", "pre", "li"):
        opened = len(re.findall(rf"<{tag}[ >]", html))
        closed = len(re.findall(rf"</{tag}>", html))
        if opened != closed:
            problems.append(f"<{tag}> 不平衡 {opened}/{closed}")

    status = "OK  " if not problems else "FAIL"
    print(f"[{status}] {path.parent.name:<32} html {len(html):>6} 字符  li={lis:<4}"
          + ("  " + "; ".join(problems) if problems else ""))
    if problems:
        bad.append(path.parent.name)

print("\n安全用例（检查生成的标签内部有没有危险特征）：")
attacks = [
    ("<script>alert(1)</script>", "裸 script 标签"),
    ("<img src=x onerror=alert(1)>", "事件属性"),
    ("[点我](javascript:alert(1))", "javascript: 协议"),
    ("[点我](JaVaScRiPt:alert(1))", "大小写混写 javascript:"),
    ("[点我](vbscript:msgbox)", "vbscript: 协议"),
    ("[点我](data:text/html,<script>)", "data: 协议"),
    ("![x](javascript:alert(1))", "图片 javascript:"),
    ("![x](data:image/svg+xml,<svg>)", "图片 data:"),
    ("<iframe src=evil></iframe>", "iframe"),
    ("<https://ok.example.com>", "正常自动链接（应放行）"),
    ("[正常](https://ok.example.com)", "正常链接（应放行）"),
]
unsafe = 0
for text, label in attacks:
    html = skm.render_markdown(text)
    real_tags = " ".join(re.findall(r"<[a-zA-Z][^>]*>", html))
    danger = re.search(r"<\s*script|<\s*iframe|on\w+\s*=|javascript:|vbscript:|data:",
                       real_tags, re.IGNORECASE)
    # 正常链接本来就该有 href，放行
    expected_ok = "应放行" in label
    if danger and not expected_ok:
        unsafe += 1
    print(f"  [{'危险!' if (danger and not expected_ok) else '安全 '}] {label:<22} {html[:88]}")

print(f"\n结构：{len(samples) - len(bad)}/{len(samples)} 个真实文件渲染正常")
print(f"安全：{len(attacks) - unsafe}/{len(attacks)} 个用例通过")

# ── 列表项里嵌代码块（真实文件里非常常见，踩过一次坑） ──
print("\n专项：列表项里的代码块不能被当成列表项")
cases = [
    ("- 建清单，包含：\n  ```markdown\n  - 幻灯片 0：封面\n  - 幻灯片 1：正文\n  ```\n- 第二步",
     2, "缩进 2 格的围栏"),
    ("1. 第一步：\n   * 子项\n     ```bash\n     rm -rf /tmp/x\n     ls -la | grep x\n     ```\n   * 另一个子项",
     3, "有序列表 + 缩进 3 格的围栏"),
    ("- 外层\n  - 内层\n    ```\n    - 不是列表项\n    ```\n- 收尾",
     3, "两层嵌套里的围栏"),
    ("- 说明\n\n  ```\n  - 也不是列表项\n  ```\n\n- 下一段",
     2, "围栏和列表项之间有空行"),
]
case_bad = 0
for text, want, label in cases:
    html = skm.render_markdown(text)
    got = len(re.findall(r"<li[ >]", html))
    inside = len(re.findall(r"<pre><code>(?:(?!</code>).)*<li", html, re.S))
    good = got == want and inside == 0
    case_bad += not good
    print(f"  [{'OK  ' if good else 'FAIL'}] {label:<28} 期望 {want} 个 <li>，实际 {got}"
          + (f"，代码块里混进了 <li> {inside} 个" if inside else ""))
    if not good:
        print("      " + html.replace("\n", " ")[:220])

print(f"专项：{len(cases) - case_bad}/{len(cases)} 个用例通过")
if bad or unsafe or case_bad:
    print("有问题的文件：", bad)
sys.exit(1 if (bad or unsafe or case_bad) else 0)
