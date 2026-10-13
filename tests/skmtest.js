/* tests/skmtest.js —— 界面自测脚本（只有 ?skmtest=1 才会加载）
 *
 * 为什么需要它：HTML5 拖放的 dragstart/dragover/drop 事件序列，
 * 无头浏览器不会自动产生，只能用脚本合成事件走一遍真实处理函数。
 * 这就是开发日志 3.5 里遗留的「拖拽没法自动化测试」的解法：
 * 用真实页面 + 真实事件监听器，只把事件本身合成出来。
 *
 * 结果写进 #skmtest-out，tests\test_ui.py 用 --dump-dom 抓回去断言。
 * 全程只用页面里已有的 S / api / bulkRun，不另外造一套逻辑。
 */
(function () {
  "use strict";

  var out = [];
  var errors = [];

  /* 无头浏览器没有对话框：confirm()/prompt() 会永远阻塞（第一次跑就卡了 180 秒）。
   * 所以这里直接 stub 掉——顺便记一笔，免得忘了自己 stub 过。 */
  window.confirm = function () { return true; };
  window.prompt = function (msg, def) { return def || "自测新建分类"; };

  window.addEventListener("error", function (e) {
    errors.push("Error: " + (e.message || "?") + " @" + (e.lineno || "?"));
  });
  window.addEventListener("unhandledrejection", function (e) {
    errors.push("UnhandledRejection: " + ((e.reason && e.reason.message) || e.reason));
  });

  function ok(label, cond, detail) {
    out.push({ label: label, pass: !!cond, detail: detail === undefined ? "" : String(detail) });
  }
  function eq(label, got, want) {
    var same = JSON.stringify(got) === JSON.stringify(want);
    ok(label, same, same ? "" : "得到 " + JSON.stringify(got) + "，期望 " + JSON.stringify(want));
  }

  /* ---- 抓网络请求：改不改真数据都靠它判断「发了哪些 PUT」 ---- */
  var realFetch = window.fetch;
  var calls = [];
  window.fetch = function (path, opts) {
    opts = opts || {};
    if ((opts.method || "GET").toUpperCase() !== "GET") {
      calls.push({ method: opts.method, path: String(path), body: opts.body || null });
    }
    return realFetch.apply(window, arguments);
  };
  function puts() {
    return calls.filter(function (c) { return c.method === "PUT"; });
  }

  /* ---- 合成事件：走页面自己的监听器，不直接调函数 ---- */
  var dt = new DataTransfer();
  function fire(el, type) {
    var ev;
    try {
      ev = new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt });
    } catch (err) {
      ev = document.createEvent("Event");
      ev.initEvent(type, true, true);
    }
    el.dispatchEvent(ev);
    return ev;
  }
  function card(id) {
    return document.querySelector('.card[data-id="' + cssEscape(id) + '"]');
  }
  function cssEscape(v) { return String(v).replace(/"/g, '\\"'); }
  function node(cat) {
    return document.querySelector('#tree .node[data-c="' + cssEscape(cat) + '"]');
  }
  function toastText() { return (document.getElementById("toast") || {}).textContent || ""; }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  /* 等 boot() 把 /api/state 拉回来 */
  function waitReady() {
    return new Promise(function (resolve) {
      var tries = 0;
      (function tick() {
        if (S.skills.length || ++tries > 100) return resolve();
        setTimeout(tick, 50);
      })();
    });
  }

  function run() {
    var cards = document.querySelectorAll("#grid .card");
    var nodes = document.querySelectorAll("#tree .node");

    /* ---------- 1. 页面结构 ---------- */
    ok("卡片数量与技能数一致", cards.length === S.skills.length,
      cards.length + " 张卡片 / " + S.skills.length + " 个技能");
    ok("每张卡片都带 draggable", Array.prototype.every.call(cards, function (c) {
      return c.getAttribute("draggable") === "true";
    }));
    ok("分类树节点齐全（全部/收藏/未使用 + 9 个一级）",
      nodes.length >= 12 && !!node("") && !!node("__star") && !!node("__unused"),
      nodes.length + " 个节点");
    ok("每张卡片都有「移动到…」按钮（触屏拖不动时的替代入口）",
      document.querySelectorAll("#grid .card .movebtn").length === cards.length);

    /* ---------- 2. dragstart：只拖自己 ---------- */
    var first = S.skills[0];
    var c1 = card(first.id);
    fire(c1, "dragstart");
    eq("dragstart 后 S.dragging = 自己", S.dragging, [first.id]);
    ok("被拖的卡片有 .dragging", c1.classList.contains("dragging"));
    ok("卡片墙进入 dragging-active",
      document.getElementById("grid").classList.contains("dragging-active"));

    /* ---------- 3. dragTargets：勾了几个就拖几个 ---------- */
    var picked = S.skills.slice(1, 4).map(function (s) { return s.id; });
    S.sel.clear();
    picked.forEach(function (id) { S.sel.add(id); });
    fire(card(picked[0]), "dragstart");
    eq("勾选 3 个后拖其中 1 个 = 拖这 3 个", (S.dragging || []).slice().sort(),
      picked.slice().sort());

    /* ---------- 4. dragover：合法节点高亮 / 非法节点变红 ---------- */
    var goodCat = pickCategory(S.skills[0]);
    var goodNode = node(goodCat);
    fire(goodNode, "dragover");
    ok("拖到真分类上：节点高亮 .drop",
      goodNode.classList.contains("drop") && !goodNode.classList.contains("drop-bad"),
      goodCat);
    var allNode = node("");
    fire(allNode, "dragover");
    ok("拖到「全部」上：标成 .drop-bad",
      allNode.classList.contains("drop-bad") && !allNode.classList.contains("drop"));

    /* ---------- 5. drop 到「全部」：必须拒绝，且一个请求都不发 ---------- */
    calls = [];
    S.sel.clear();
    S.dragging = [first.id];
    fire(allNode, "drop");
    ok("拖到「全部」被拒绝并给出提示",
      toastText().indexOf("不是分类节点") >= 0, toastText());
    ok("被拒绝时一个 PUT 都没发", puts().length === 0);

    /* ---------- 6. drop 到真分类：改分类 + 批量一起改 ---------- */
    calls = [];
    S.sel.clear();
    var moving = S.skills.filter(function (s) { return s.category !== goodCat; })
      .slice(0, 3).map(function (s) { return s.id; });
    S.dragging = moving.slice();
    fire(node(goodCat), "drop");
    return sleep(700).then(function () {
      var sent = puts();
      eq("drop 发出的 PUT 就是拖的那几个（缺一个就是少改了）",
        sent.map(function (c) { return decodeURIComponent(c.path.split("/").pop()); }).sort(),
        moving.slice().sort());
      ok("PUT 的分类值是拖过去的那个分类",
        sent.every(function (c) {
          var b = JSON.parse(c.body || "{}");
          return b.category === goodCat;
        }), goodCat);
      ok("拖完后界面切到目标分类",
        S.filterCat === goodCat, S.filterCat);
      ok("成功的从选中里清掉了", S.sel.size === 0, S.sel.size);

      /* ---------- 7. 已经在目标分类里：不再发请求 ---------- */
      return reload().then(function () {
        calls = [];
        S.dragging = [S.skills.filter(function (s) {
          return s.category === goodCat;
        })[0].id];
        var sameNode = node(goodCat);
        fire(sameNode, "drop");
        return sleep(400);
      }).then(function () {
        ok("已经在目标分类里：不再发 PUT，只提示一句",
          puts().length === 0 && toastText().indexOf("已经在") >= 0,
          toastText() || "（没有提示）");
      });
    }).then(function () {
      /* ---------- 8. dragend 清理 ---------- */
      S.dragging = moving.slice();
      fire(card(moving[0]), "dragstart");
      fire(node(goodCat), "dragover");
      fire(card(moving[0]), "dragend");
      ok("dragend 后 S.dragging 清空", S.dragging === null, String(S.dragging));
      ok("dragend 后高亮样式全清了",
        !document.querySelector(".dragging") &&
        !document.querySelector(".node.drop,.node.drop-bad"));

      /* ---------- 9. 「移动到…」按钮：不开拖拽也能改分类 ---------- */
      return reload().then(function () {
        calls = [];
        S.sel.clear();
        S.filterCat = "";          /* 前面拖完切到了目标分类，先回到全部，否则目标卡片没渲染 */
        renderAll();
        var mvId = S.skills.filter(function (s) { return s.category !== goodCat; })[0].id;
        var btn = document.querySelector('#grid .card[data-id="' + cssEscape(mvId)
          + '"] .movebtn');
        btn.click();
        ok("点「移动到…」打开弹窗并列出分类",
          document.getElementById("mMove").className.indexOf("on") >= 0 &&
          document.getElementById("mvCat").options.length > 5);
        var sel = document.getElementById("mvCat");
        for (var i = 0; i < sel.options.length; i++) {
          if (sel.options[i].value === goodCat) { sel.selectedIndex = i; break; }
        }
        S.moving = [mvId];
        document.getElementById("mvOk").click();
        return sleep(600).then(function () {
          var sent = puts();
          ok("「移动到…」改成了目标分类",
            sent.length === 1 &&
            decodeURIComponent(sent[0].path.split("/").pop()) === mvId &&
            JSON.parse(sent[0].body || "{}").category === goodCat,
            JSON.stringify(sent.map(function (c) { return c.body; })));
          ok("弹窗在成功后关掉了",
            document.getElementById("mMove").className.indexOf("on") < 0);
        });
      }).then(function () {
        /* ---------- 11. 筛选状态进地址栏（刷新不丢） ---------- */
        return reload().then(function () {
          S.filterCat = ""; S.tags = []; S.source = ""; S.q = "";
          S.onlyStar = false; S.onlyUnused = false;
          var tag = (S.skills[0].tags || [])[0];
          var cat = S.skills[0].category;
          var src = S.skills[0].sourceType || "";
          S.filterCat = cat; S.tags = tag ? [tag] : [];
          S.source = src; S.q = "red"; S.onlyUnused = true;
          writeHash();
          var h = location.hash;
          ok("筛选写进了地址栏",
            h.indexOf("c=" + encodeURIComponent(cat)) >= 0 &&
            h.indexOf("t=" + encodeURIComponent(tag || "")) >= 0 &&
            h.indexOf("src=" + encodeURIComponent(src)) >= 0 &&
            h.indexOf("q=red") >= 0 && h.indexOf("unused=1") >= 0, h);
          /* 把地址栏原样喂回去，状态必须还原（这就是刷新页面走的那条路） */
          S.filterCat = ""; S.tags = []; S.source = ""; S.q = "";
          S.onlyUnused = false;
          applyHash();
          eq("从地址栏还原分类", S.filterCat, cat);
          eq("从地址栏还原标签", S.tags, tag ? [tag] : []);
          eq("从地址栏还原来源", S.source, src);
          eq("从地址栏还原搜索词", S.q, "red");
          ok("从地址栏还原「只看未使用」", S.onlyUnused === true);
          ok("搜索框里的文字也跟着还原", $("search").value === "red", $("search").value);
          /* 老深链不能坏：#/skill/<id> 仍然直接开抽屉 */
          S.filterCat = ""; S.tags = []; S.source = ""; S.q = ""; S.onlyUnused = false;
          writeHash();
          var deep = S.skills[1].id;
          location.hash = "#/skill/" + encodeURIComponent(deep);
          return sleep(400).then(function () {
            ok("老的 #/skill/<id> 深链照样能直接开抽屉",
              S.detail === deep && $("drawer").className.indexOf("on") >= 0,
              String(S.detail));
            closeDrawer();
            /* 清空筛选后地址栏也要跟着干净 */
            S.filterCat = cat; writeHash();
            S.filterCat = ""; S.tags = []; S.source = ""; S.q = ""; S.onlyUnused = false;
            writeHash();
            ok("清空筛选后地址栏不留残渣",
              location.hash === "#" || location.hash === "", location.hash);
          });
        });
      });
    })
      .then(function () {
        /* ---------- 12. 复制到项目（第四步） ---------- */
        return reload().then(function () {
          return api("GET", "/api/projects").then(function (d) {
            ok("项目清单接口可用", Array.isArray(d.projects), JSON.stringify(d.projects).slice(0, 80));
          });
        }).then(function () {
          /* 没登记项目时：要说清楚先登记，而不是给个空下拉框让人干瞪眼 */
          S.sel.clear();
          S.sel.add(S.skills[0].id);
          openSend(Array.from(S.sel));
          return sleep(400).then(function () {
            ok("没登记项目时给出明确指引",
              $("mSend").className.indexOf("on") >= 0 &&
              $("sdOk").disabled === true &&
              $("sdList").innerHTML.indexOf("先在「项目」里登记") >= 0,
              $("sdList").innerHTML.slice(0, 60));
            closeModal("mSend");
            /* 登记一个真项目（目录由 test_ui.py 建好，路径从网址参数传进来） */
            var projDir = (/[?&]proj=([^&]*)/.exec(location.search) || [])[1];
            if (!projDir) return null;
            return api("POST", "/api/project",
              { name: "uitest", path: decodeURIComponent(projDir) }).catch(function () { return null; });
          });
        }).then(function () {
          return api("GET", "/api/projects").then(function (d) {
            var mine = (d.projects || []).filter(function (p) { return p.name === "uitest"; })[0];
            if (!mine) {
              /* 登记不上（没给 proj 参数/路径不可写）就跳过，别让整套测试红 */
              ok("能登记一个项目用于分发测试", false, "没有可写的项目目录，跳过分发界面测试");
              return false;
            }
            ok("项目登记后出现在清单里", !!mine, mine.path);
            openSend([S.skills[0].id]);
            return sleep(500).then(function () {
              ok("分发弹窗列出了项目和落点",
                $("sdProj").options.length >= 1 &&
                $("sdHint").textContent.indexOf("落点") >= 0,
                $("sdHint").textContent);
              ok("分发前先给出预演清单",
                $("sdList").innerHTML.indexOf("新增") >= 0
                || $("sdList").innerHTML.indexOf("已一致") >= 0,
                $("sdList").innerHTML.slice(0, 80));
              calls = [];
              $("sdOk").click();
              return sleep(800);
            }).then(function () {
              ok("点确认后真的发出了分发请求并关掉弹窗",
                calls.length === 1 && calls[0].method === "POST"
                && calls[0].path === "/api/send"
                && JSON.parse(calls[0].body || "{}").mode === "copy"
                && $("mSend").className.indexOf("on") < 0,
                JSON.stringify(calls.map(function (c) { return c.method + " " + c.path; })));
              return reload();
            }).then(function () {
              var first = S.skills[0];
              ok("分发后卡片的「用在 N 个项目」变了",
                (first.usedBy || []).indexOf("uitest") >= 0,
                JSON.stringify(first.usedBy));
              openProjects();
              return sleep(400).then(function () {
                ok("项目面板列出了已装的东西",
                  $("pjList").innerHTML.indexOf("uitest") >= 0
                  && $("pjList").innerHTML.indexOf("已装") >= 0,
                  $("pjList").innerHTML.slice(0, 80));
                closeModal("mProj");
                return api("DELETE", "/api/project/uitest").catch(function () { return null; });
              });
            });
          });
        }).then(function () {
          /* ---------- 13. 备份 / 体检 / 回收站（第五步） ---------- */
          openTools();
          return sleep(500).then(function () {
            ok("「备份」面板能打开，回收站区块在",
              $("mTools").className.indexOf("on") >= 0 &&
              $("tTrash").innerHTML.indexOf("回收站是空的") >= 0,
              $("tTrash").innerHTML.slice(0, 50));
            ok("下载备份的链接指向 zip 接口",
              $("tBackup").getAttribute("href") === "/api/backup.zip"
              && $("tBackup").hasAttribute("download"));
            $("tDoctor").click();
            return sleep(700);
          }).then(function () {
            ok("体检能跑完并给结论",
              $("tOut").innerHTML.indexOf("体检") >= 0 || $("tOut").innerHTML.indexOf("一切正常") >= 0
              || $("tOut").innerHTML.indexOf("[") >= 0,
              $("tOut").innerHTML.slice(0, 70));
            $("tCheck").click();
            return sleep(700);
          }).then(function () {
            ok("全库检查列出四块内容",
              $("tOut").innerHTML.indexOf("依赖缺失") >= 0
              && $("tOut").innerHTML.indexOf("依赖指向了不存在的 Skill") >= 0
              && $("tOut").innerHTML.indexOf("长期未用") >= 0
              && $("tOut").innerHTML.indexOf("空分类") >= 0,
              $("tOut").innerHTML.slice(0, 60));
            return api("GET", "/api/backup.zip").then(function () { return null; })
              .catch(function (err) {
                ok("备份 zip 能下载（200，不是 4xx/5xx）", false, err.message);
                return null;
              });
          }).then(function () {
            return fetch("/api/backup.zip").then(function (r) {
              ok("备份 zip 接口返回 200 且是 zip",
                r.status === 200 && r.headers.get("Content-Type") === "application/zip",
                r.status + " " + r.headers.get("Content-Type"));
            });
          }).then(function () {
            closeModal("mTools");
            return api("POST", "/api/restore", { id: "no-such-id" })
              .then(function () {
                ok("找回不存在的 Skill 要报错", false, "居然成功了");
              })
              .catch(function (err) {
                ok("找回不存在的 Skill：友好报错", /回收站里没有|回收站是空的/.test(err.message),
                  err.message);
              });
          });
        }).then(function () {
          /* ---------- 14. AI 自动分类 ---------- */
          return api("GET", "/api/ai").then(function (d) {
            ok("AI 配置接口可用且已脱敏",
              d.ai && d.ai.ready && !("apiKey" in d.ai)
              && typeof d.uncategorized === "number" && d.uncategorized > 0,
              JSON.stringify({ model: d.ai.model, uncat: d.uncategorized }));
            openClassify(false);
            return sleep(4000);
          }).then(function () {
            var items = S.aiItems || [];
            ok("点「✨ AI 分类」拿到建议清单",
              items.length > 0 && $("mAi").className.indexOf("on") >= 0,
              items.length + " 条");
            ok("每条建议都带一个可选的下拉框（人能改）",
              document.querySelectorAll("#aiOut select.aicat").length === items.length,
              document.querySelectorAll("#aiOut select.aicat").length + " 个下拉");
            ok("建议里没有「未分类」这种敷衍答案",
              items.every(function (x) { return !x.error ? !!x.category : true; }));
            ok("确认按钮可点", $("aiApply").disabled === false);
            calls = [];
            $("aiApply").click();
            return sleep(1500);
          }).then(function () {
            var apply = calls.filter(function (c) { return c.path === "/api/classify/apply"; });
            ok("点确认后调用的是 apply 接口且带上了逐条建议",
              apply.length === 1 && (JSON.parse(apply[0].body || "{}").items || []).length > 0,
              JSON.stringify(calls.map(function (c) { return c.path; })));
            ok("成功后弹窗关掉了", $("mAi").className.indexOf("on") < 0);
          });
        }).then(function () {
          /* ---------- 10. 批量操作部分失败（2.14 提到的 allSettled 隐患） ---------- */
          return bulkFailCase(goodCat);
        }).then(finish);
      });
  }

  /* 勾 3 个，其中 1 个故意让请求失败：成功的要清出选中、失败的必须留下 */
  function bulkFailCase(cat) {
    var ids = S.skills.slice(0, 3).map(function (s) { return s.id; });
    var bad = ids[1];
    var realApi = window.api;
    var called = [];
    window.api = function (method, path) {
      if (String(path).indexOf("/api/skill/") !== 0) {
        return realApi.apply(window, arguments);   /* reload() 的 /api/state 不算 */
      }
      var id = decodeURIComponent(String(path).split("/").pop());
      called.push(id);
      if (id === bad) return Promise.reject(new Error("故意失败"));
      return realApi.apply(window, arguments);
    };
    S.sel.clear();
    ids.forEach(function (id) { S.sel.add(id); });
    return bulkRun(ids, "PUT", { category: cat }, "已把 %d 个移到「" + cat + "」")
      .then(function (res) {
        window.api = realApi;
        eq("三个都调到了接口（前端不偷偷跳过失败的那个）",
          called.slice().sort(), ids.slice().sort());
        ok("成功 2 个、失败 1 个", res.ok === 2 && res.failed.length === 1,
          JSON.stringify(res));
        ok("失败的留在选中里方便直接重试",
          S.sel.size === 1 && S.sel.has(bad),
          Array.from(S.sel).join(","));
        ok("toast 同时报出成功与失败数",
          toastText().indexOf("成功 2 个") >= 0 &&
          toastText().indexOf("失败 1 个") >= 0, toastText());
        ok("失败原因带上了 Skill 名，不是干巴巴一句「失败」",
          toastText().indexOf(bad) >= 0, toastText());
      });
  }

  function pickCategory(like) {
    /* 找一个当前没人用的分类，用来验证「真的改过去了」 */
    var used = {};
    S.skills.forEach(function (s) { used[s.category] = 1; });
    var all = [];
    S.cats.forEach(function (c) {
      all.push(c.name);
      (c.children || []).forEach(function (k) { all.push(c.name + "/" + k.name); });
    });
    for (var i = 0; i < all.length; i++) {
      if (!used[all[i]]) return all[i];
    }
    return all[all.length - 1];
  }

  function finish() {
    if (document.getElementById("skmtest-out")) return;   /* 幂等：看门狗可能先到 */
    clearTimeout(watchdog);
    var pre = document.createElement("pre");
    pre.id = "skmtest-out";
    pre.textContent = JSON.stringify({ results: out, errors: errors });
    document.body.appendChild(pre);
    document.title = "skmtest " + out.filter(function (r) { return r.pass; }).length
      + "/" + out.length + (errors.length ? " ERRORS=" + errors.length : "");
  }

  /* 看门狗：某一节卡住（接口不响应之类）时也要把已有结果写出去，
   * 否则整个自测只能等 dump-dom 超时，什么都看不到。 */
  var watchdog = setTimeout(function () {
    ok("自测脚本在超时前没跑完（有一节卡住了）", false,
      "已写出 " + out.length + " 项结果，看上面哪一节断在了后面");
    errors.push("watchdog: timeout");
    finish();
  }, 60000);

  window.addEventListener("load", function () {
    waitReady().then(function () { return sleep(150); }).then(run).catch(function (e) {
      errors.push("自测脚本自己挂了: " + (e && e.message ? e.message : e));
      finish();
    });
  });
})();