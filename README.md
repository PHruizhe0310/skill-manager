# 技能管家 skm

把散落在电脑各处的大模型 Skill 收集起来 → 分好类 → 一次性打包发给项目。

**当前进度**
- ✅ 第一步「收集」—— `scan` / `add` / `list` + 分类树 + 命令行改分类
- ✅ 第二步「网页界面」—— `skm ui` 分类树 + 卡片墙 + 详情抽屉 + 搜索 + 批量操作 + 离线导出
- ✅ 第三步「搜索 / 筛选 / 拖拽」—— 正文搜索、按标签/来源筛选、拖拽改分类（含自动化测试）
- ✅ 第四步「分发到项目」—— 项目登记、批量分发、依赖带过去、覆盖前先备份、`status`/`sync`/`remove`
- ✅ 第五步「备份体检」—— `export --zip` / `import` / `doctor --fix` / `check` / `restore` 回收站
- ✅ 附加：AI 自动分类——配好接口后让模型给「未分类」的 Skill 提分类建议（可新建分类），逐条确认后落盘

- 单个 Python 文件 + 一个 HTML，**只用标准库**，不装任何第三方依赖。
- 数据全是 JSON 明文，随时可以打开看、手工改。
- 工具**不改动**任何 Skill 的内容，只做「复制 + 记账」。

---

## 快速开始

```
cd <skm安装目录>

skm ui                    打开网页界面（推荐，浏览器里点着操作）
skm scan                  扫描本机已有 Skill，勾选收录
skm scan --yes            不提问，全部收录
skm add <路径>            手动收录（SKILL.md 文件或 Skill 文件夹）
skm add --text            粘贴文本，新建一个 Skill
skm list                  看已收录的清单
skm show <id>             看某个 Skill 的完整内容
skm cat --list            看分类树和各分类数量
skm cat <id> <分类>       改分类
skm tag <id> <标签...>    改标签
skm export --html <路径>   导出单文件离线页面，可发给同事
skm info / skm roots / skm init

skm project add myapp D:\code\myapp     登记一个项目
skm project list                        看已登记的项目和已装数量
skm send --project myapp --ids a,b      把选中的 Skill 复制到项目
skm send --project myapp --category 信息对抗   整类分发
skm status myapp                        这个项目里有哪些、哪些过期了
skm sync myapp                          按记录对齐（补上缺的、重发过期的）
skm remove myapp --ids a                从项目里移除（库里的还在）
skm dep a b --cmd python,openpyxl       声明依赖（分发时会自动带上 / 只提示）

skm export --zip D:\bak\skills.zip      打包整个技能库（含清单、分类树、回收站）
skm import D:\bak\skills.zip            换台机器导入（重名自动加后缀，不覆盖）
skm doctor / skm doctor --fix           体检 / 自动修
skm check                               依赖缺失、长期未用、空分类
skm restore                             回收站（找回删掉的 Skill）
skm ai / skm ai --test                  看 AI 配置 / 测连通
skm classify --dry-run                  让 AI 给未分类的提建议（只看不改）
```

没有 `skm` 命令就用 `python skm.py <子命令>`，效果完全一样。
（把 `<skm安装目录>` 加进 PATH 就能直接敲 `skm`。）
**双击 `skm.cmd`** 会弹一个菜单，选 `5` 就是打开网页界面。

---

## AI 自动分类

「✨ AI 分类」把**未分类**的 Skill 交给大模型，让它从你现有的分类树里挑一个；
都不合适就**提出新的一级/二级分类**。模型只给建议，**落盘前你能逐条改**。

```
skm ai                    看配置（API Key 只显示后 4 位）
skm ai --test             发一句话测连通
skm classify --dry-run    只看建议，不改任何数据
skm classify              确认后落盘（新建分类前会再问一次）
skm classify --all -y     连已归好类的也重新提一遍
skm classify --ids a,b    只给指定的几个提建议
```

网页界面：顶栏「⚙ 设置 → AI 接口」填**接口地址 / 模型 / API Key**，点「测试连接」确认通了，
再点顶栏「✨ AI 分类」，选「只处理未分类」或「全部重新提建议」。

配置写在 `data\config.json` 的 `ai` 一节：

```json
"ai": {
  "baseUrl": "https://api.opencode.ai/v1",
  "model": "gpt-4o-mini",
  "apiKey": "",
  "temperature": 0,
  "bodyChars": 1200,
  "createCategory": true,
  "batchSize": 1
}
```

要点：

- **只用标准库**（`urllib` 调 OpenAI 兼容的 `/chat/completions`），不装任何第三方包；
- **API Key 不下发到浏览器**：`/api/config` 与 `/api/ai` 只返回「配了没有」和后 4 位。
  也可以不写进文件，改用环境变量 `SKM_AI_API_KEY`；
- **模型不直接改数据**：preview 只读，apply 才写盘，而且走的是和拖拽一样的 `set_category`，
  所以 `catalog.json` 与 `skill.json` 自动同步；
- 一个 Skill 一次请求（`batchSize` 可调），**某一个失败不影响其它**，失败项在界面上单独显示原因；
- 正文只送 `bodyChars` 个字符的节选——发全文既慢又贵；
- 每次的建议连同模型给的理由留在 `data\_ai\classify-<时间戳>.json`，事后能查它当时说了什么。

## 网页界面（`skm ui`）

```
skm ui                      默认 http://127.0.0.1:8765
skm ui --port 9000          换端口（被占用时会自动往后找 9001、9002…）
skm ui --no-browser         不自动开浏览器
```

浏览器里能做：

| 区域 | 能做什么 |
| --- | --- |
| 顶部搜索框 | 搜名称、介绍、标签；再点一下还能搜**全部 SKILL.md 正文** |
| 左侧分类树 | 点一级分类看整个大类，点二级看小类；另有「★ 收藏」「未使用」两个快捷项，每级都带数量 |
| 中间卡片墙 | 一张卡片 = 一个 Skill，显示名称、简介、标签、所属分类、被几个项目用过；勾选框用于批量操作 |
| 右侧详情抽屉 | 点卡片滑出：完整介绍、全部元信息（来源路径/收录日期/文件清单）、**渲染好的 SKILL.md 全文**；按钮：编辑 / 收藏 / 复制到项目 / 删除 / 从项目移除 |
| 顶部按钮 | 「+ 收录」扫描并勾选收录、「+ 新建」粘贴文本新建、「项目」登记项目并看状态、「分类管理」建新分类、「⚙ 设置」改扫描位置 |
| 批量工具条 | 勾选卡片后出现：改分类 / 加标签 / 复制到项目 / 收藏 / 删除 |
| 筛选栏 | 按标签（多个之间是「或」）、按来源、只看未使用、只看收藏；**所有筛选都写进地址栏，刷新不丢** |

其他：
- 地址栏可以是 `http://127.0.0.1:8765/#/skill/attack-surface-recon`，直接打开某个 Skill 的详情，可以收藏。
- 筛选也在地址栏里：`#/c/信息对抗/侦察测绘?t=红队&src=scan&q=钓鱼`，可以整条链接发给同事。
- **拖拽改分类**：把卡片拖到左边的分类树上。勾选了多个再拖任意一张，就是这一批一起移动。
  拖到「全部 / ★收藏 / 未使用」上会被拒绝并提示（它们不是分类节点）。
- **手机上用「移动到…」**：每张卡片底部有个小按钮，等价于拖拽（触屏设备上 HTML5 拖拽不工作）。
- **复制到项目**：勾选后点「复制到项目」，先看预演清单（哪些新增、哪些覆盖、哪些因依赖带进来、
  哪些缺环境），确认后批量复制过去。详见下面「复制到项目」。
- 卡片**双击**直接进编辑。
- 关掉命令行窗口或按 `Ctrl+C` 停止服务。

## 复制到项目（`skm send`）

```
skm project add myapp D:\code\myapp          # 登记项目（先做这步）
skm project list                             # 看已登记的项目和各自装了几个

skm send --project myapp --ids a,b,c         # 分发指定的
skm send --project myapp --category 信息对抗   # 整类分发（一级大类也行）
skm send --project myapp --all --mode link   # 全部，软链接方式
skm send --project myapp --ids a --overwrite skip   # 项目里有旧版就跳过

skm status myapp                             # 一致 / 过期 / 文件没了 / 手工放入
skm sync myapp                               # 补上丢的、重发过期的
skm remove myapp --ids a                     # 从项目移除（技能库里的还在）
skm dep ocr-fallback pdf-form-filler         # 声明 Skill 之间的依赖
skm dep excel-clean --cmd python,openpyxl    # 声明需要的外部命令
```

落点固定是 `<项目>\.opencode\skills\<技能id>\`。分发时按这套规则走：

| 情况 | 处理 |
| --- | --- |
| 项目里没有 | 新增 |
| 项目里已有、**内容一样** | 不动（省一次写盘） |
| 项目里已有、内容不同 | **不静默覆盖**：命令行问你选「备份后覆盖（默认）/ 直接覆盖 / 跳过」，网页上先给预演清单 |
| 这个 Skill 依赖别的 Skill | 顺带带过去，清单里标注「因依赖 X 被带进来」 |
| 这个 Skill 需要某个命令而当前没有 | **只提示，不阻断** |

覆盖前的旧版本不会丢，挪到 `data\_projects\<项目名>\backup\<技能id>-<时间戳>\`。

`copy` 和 `link` 的区别：`copy` 是复制一份（默认，可提交进 Git、别人能直接用）；
`link` 建软链接/目录联接指向技能库，**在库里改完项目里立刻生效**，适合自己开发调试。

## 备份 / 体检（`export --zip` / `doctor` / `restore`）

```
skm export --zip D:\bak\skills.zip    打包整个技能库（Skill 本体 + 清单 + 分类树 + 分发记录 + 回收站）
skm import D:\bak\skills.zip          在新机器上导入；重名自动加 -2 后缀，绝不覆盖你已有的
skm doctor                            只报告：清单与文件对不上、幽灵分类、哈希漂移、项目失效
skm doctor --fix                      能自动修的顺手修（会写盘）
skm check                             依赖缺失、依赖指向不存在、长期未用、空分类
skm restore                           回收站：找回删掉的 Skill
```

`import` 的三条规矩：**内容一样就跳过**、**id 撞了自动加后缀**、
分类树只**合并**不删除（绝不覆盖你在本机整理好的分类）。
项目和扫描位置是机器特有的，默认不导，要导得显式加 `--with-projects` / `--with-config`。

判重看的是**文件实际内容**而不是 `skill.json` 里记的哈希——
有人拿记事本手工改过 `SKILL.md` 而没更新哈希时，备份里那份原版不会被无声跳过。

网页界面里点顶栏「备份」：下载 zip、体检 + 自动修复、全库检查、看回收站并找回。

### 安全设计

这是个会读写文件的本地服务，所以：

| 措施 | 防什么 |
| --- | --- |
| 只绑 `127.0.0.1`，没有 `--host` 参数 | 局域网里的其他机器访问不到 |
| 校验 `Host` 头必须是 `127.0.0.1`/`localhost` | DNS rebinding（恶意网站把域名解析到本机来打这个服务） |
| 写操作要求 `Content-Type: application/json`，且不接受跨域 `Origin` | 跨站请求伪造。浏览器对非简单请求会先发 OPTIONS，我们不回 CORS 头，跨源请求自然被拦 |
| `/api/file` 限定只能读技能库内的文件 | 路径穿越（`../../` 读不到 `win.ini`） |
| Markdown 渲染全部先 HTML 转义；链接只放行 `http/https/mailto`，图片只放行 `http/https` | XSS。`javascript:` / `data:` / `vbscript:` 一律降级成纯文本 |
| 响应带 `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer` | 顺手堵住 sniffing 和 referrer 泄漏本机路径 |

### 离线导出（`skm export --html`）

生成一个**单文件 HTML**（内嵌了全部 Skill 的介绍、分类和渲染好的正文），双击就能看，
可以发给同事，**不需要装本工具、不需要联网**。

⚠️ 页面里含有 Skill 的本机来源路径。给外人看之前如果介意，可以先 `skm export --zip`（第五步）
或手动确认一下。

---

## 目录结构

```
<skm安装目录>
├── skm.py                程序（单文件，只用标准库）
├── ui.html               网页界面（内联 CSS/JS，不引 CDN）
├── skm.cmd               Windows 启动器 / 菜单，双击可用
├── tests\                回归自检脚本（改完代码跑一下）
│   ├── test_data.py      命令冒烟 + 落盘数据一致性（18 项）
│   ├── test_markdown.py  Markdown 渲染结构 + XSS（29 个真实文件 + 11 个安全用例）
│   ├── test_api.py       网页接口（含分发接口，42 项，用真实库的副本跑）
│   ├── test_send.py      分发到项目：覆盖检查 / 备份 / 撤销 / 记录（46 项）
│   ├── test_backup.py    备份导出导入 / 体检修复 / 回收站（43 项）
│   ├── test_ai.py        AI 自动分类：调用/解析/提示词/密钥不外泄（60 项，不联网、用假 AI 服务）
│   ├── test_ui.py        浏览器端交互自测：拖拽、改分类、分发、备份体检、AI 分类（64 项）
│   └── run_all.py        一次跑完上面 7 套
├── README.md             本文件
├── 开发日志.md            开发过程记录
└── data\                 ← 技能库（整个文件夹拷走就能换机器）
    ├── skills\           所有收录进来的 Skill 本体（一个 Skill 一个文件夹）
    │   ├── attack-surface-recon\
    │   │   ├── SKILL.md  原始内容（只读拷贝，不改）
    │   │   └── skill.json  这个 Skill 的介绍/标签/分类/来源
    │   └── ...
    ├── catalog.json      总清单：谁属于哪个分类、有哪些标签、被哪些项目用了
    ├── categories.json   分类树 ← 想加/改/删分类直接改这个文件（界面上也能改，改完刷新）
    ├── projects.json     项目清单（第四步才用）
    ├── config.json       扫描位置配置
    ├── _projects\        每个项目的分发记录 + 覆盖前的旧版备份
    │   └── myapp\
    │       ├── records.json   分发了什么、哈希、模式、时间（撤销/对齐靠它）
    │       └── backup\       覆盖前挪走的旧版本
    └── _trash\           删除的 Skill 放这里，可恢复（第五步）
```

---

## 收录时自动做的事

| 动作 | 说明 |
| --- | --- |
| 读介绍 | 取 frontmatter 的 `description`；没有就取正文第一段 |
| 取标签 | 优先用 `metadata.tags`；没有就从正文抓关键词（渗透测试、红队、法律检索…） |
| 查重复 | 文件夹同名 **或** 内容哈希一样 → 提示，不静默处理 |
| 拍快照 | 记下来源路径、收录时间、内容哈希，以后能追溯 |

**内容哈希**做了归一化（忽略行尾空白、换行风格），所以只改了空格不会误报「内容不同」。

### SKILL.md 的 frontmatter 支持哪些写法

标准库没有 `yaml` 模块，程序内置了一个够用的极简解析器，支持：

```yaml
---
name: attack-surface-recon
description: >-            # 折叠块（多个 Skill 在用）
  第一行
  第二行
argument-hint: <file>      # 普通标量、引号字符串都可以
metadata:
  tags: [渗透测试, 红队]     # 行内列表
---
```

没有 frontmatter 的文件也能收：退回用第一个 `#` 标题作名称、正文第一段作介绍。

---

## 分类树

默认 9 个一级分类（信息对抗 / 安全研究 / AI安全 / 数字取证与反制 / 法学 / 网络与运维 / 开发工程 / 办公与自动化 / 未分类），
共 40 个二级分类。

**分类树完全由你控制**：直接编辑 `data\categories.json`，加节点、删节点、改名字都行，改完下次运行就生效；
网页界面上也有「分类管理」按钮可以直接建。命令行还可以 `--create` 自动补：

```
skm cat attack-surface-recon 新分类/子分类 --create
```

---

## 一些约定

- **Skill 的 id = 它所在文件夹的名字**。`attack-surface-recon` 就是 `attack-surface-recon`。
  含中文的名字会保留中文（`法律问题研究分析`），纯英文名会转小写连字符风格。
  同一个名字只能有一个，重名时会自动加 `-2`、`-3`。
- **新收录的 Skill 一律先进「未分类」**，不会替你猜分类。
- **重复的 Skill 不会静默覆盖**：同名不同内容会让你选「覆盖 / 跳过 / 另存为新技能」，
  内容完全一样则直接跳过。
- `skm add <某个SKILL.md 文件>` 的 id 取**它所在文件夹的名字**，不是 frontmatter 里的 name。
  想要好认的名字，就把文件放进一个叫好名字的文件夹里。
- 网页界面上删掉的 Skill 是**软删除**：整个文件夹移到 `data\_trash\`，不会真删。

---

## 常见问题

**Q：`skm scan` 说没发现任何 Skill？**
A：本机标准位置（`~\.config\opencode\skills`、`~\.claude\skills` 等）确实都没有。
用 `skm scan --path <路径>` 临时扫，或 `skm scan --add-root <路径>` 记住这个位置以后每次都扫
（也可以在网页界面右上角「⚙ 设置」里加）。

**Q：网页界面打不开 / 端口被占？**
A：会自动往后找端口（8766、8767…），命令行里会打印实际地址。
还是不行就 `skm ui --port 9000`。

**Q：JSON 改坏了怎么办？**
A：所有写入都是「先写临时文件再替换」，不会写出半个文件。
真改坏了就把 `data\` 里的 JSON 删掉，下次运行会自动重建（`skills\` 里的 Skill 本体不受影响，会被重新登记）。

**Q：Windows 弹「已阻止」/ 火绒报毒把 `skm.py` 隔离了？**
A：见下面这条。

**Q：编码乱码？**
A：程序启动时会把 stdout/stderr 强制切到 UTF-8，并开启 Windows 控制台颜色。
读文件时依次尝试 utf-8-sig / utf-8 / gb18030，中文 GBK 文件也能正常读。

**Q：杀毒软件报毒把 `skm.py` 隔离了？**
A：见下面「关于杀毒软件误报」那条。

---

## ⚠️ 关于杀毒软件误报（重要）

`skm.py` 里有三样东西凑在一起时，**火绒等国产杀毒软件的启发式引擎可能把它误判成木马**：

1. 会监听网络端口的代码（`skm ui` 的本地服务）
2. 渗透测试 / 免杀 / 木马这类词汇（`TAG_RULES` 标签词表，是业务需要）
3. 大量文件复制、移动操作（收录和分发就是干这个的）

实测报告名是 `HEUR:Trojan/Python.ShellLoader.y`，`HEUR:` 前缀表示**启发式启发式命中，不是确认的病毒**。
单独测试过：只有 HTTP 服务、只有 socket、只有文件操作、只有敏感词表——**任何单项都不会触发**，
是整体特征密度够高才被误判。

如果你遇到这种情况（文件写进去几秒就没了）：

1. 在火绒里打开「安全区 / 信任区」，把 `<skm安装目录>` 整个目录加进去；
2. 或者从火绒的「安全日志 → 隔离区」里找到 `skm.py` 选「信任并恢复」。

不要为了躲杀毒软件去删标签词表里的词，那些是你分类体系的骨架。

### 已经被隔离过一次怎么还原（本机已发生 2 次）

隔离区里的文件是加密存着的，**不用研究怎么解**——opencode 的会话数据库里留着当初每一次
`write` / `edit` 工具调用的**完整原文**，重放一遍就能拿回逐字相同的源码：

```
C:\Users\<你>\.local\share\opencode\opencode.db     ← SQLite
  part 表：每行一条工具调用，data 列是 JSON
           data.state.input.content / oldString / newString 就是原文
```

还原流程（`replay_skm.py`，留在 temp 目录）：

1. 取出所有 `filePath` 指向 `skm.py` 的调用，按 `rowid` 排序；
2. 第一条 `write` 的 `content` 是基准版本；
3. 之后每条 `edit` 就是一次 `oldString → newString` 替换，依次套上去；
4. 套不上的照着 `newString` 手工补齐即可（两次各缺 3 处 / 4 处）。

缺的这些都是当年用 PowerShell 直接改源码造成的——**这类改动不在数据库里**。
所以定了两条规矩：改源码只用 edit 工具；重要的 `.py` 在 temp 留一份备份
（`%TEMP%\skm_backup.py`，火绒不管那儿）。

---

## 改完代码怎么验证

```
cd <skm安装目录>

python tests\test_data.py        # 命令冒烟 + catalog/skill.json 是否还同步
python tests\test_markdown.py    # Markdown 渲染结构 + XSS 拦截
python tests\test_api.py         # 网页接口（会起一个临时服务，用库的副本，不动你的数据）
python tests\test_ui.py          # 浏览器端交互（无头 Chrome/Edge，需要装了其中一个）
python tests\test_backup.py     # 备份 / 导入 / 体检 / 回收站
python tests\test_ai.py         # AI 分类（本地假 AI 服务，不联网不用 key）
python tests\run_all.py         # 或者一次跑完上面七套
```

全部退出码 0 才算没问题。`test_data.py` 里那 18 项数据校验最重要——
它专门盯「网页界面改了分类之后，`catalog.json` 和每个 Skill 的 `skill.json` 是不是还一致」，
这是整个数据层最容易悄悄坏掉的地方。

`test_send.py` 用临时技能库副本 + 临时项目文件夹跑，重点盯 plan §4.4 那几条：
依赖顺带带上、缺依赖只提示不阻断、同名旧版必须拍板、撤销不牵连技能库。

`test_ui.py` 靠 `tests\skmtest.js` 驱动真实页面：它用合成的 DragEvent 走一遍
`ui.html` 里真实的 dragstart/dragover/drop 处理函数，结果写进 DOM 供断言。
网页要带 `?skmtest=1` 打开才会加载这个脚本，正常使用时不会有任何请求
（`test_ui.py` 里有一条专门验证这点）。它已经抓到过一个真 bug：
批量操作并发 PUT 时，多线程抢同一个 `catalog.json.tmp` 导致 `Permission denied`。
