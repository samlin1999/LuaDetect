"""MCP 工具的定義與實作。

跟 CLI 走同一條 `scan.scan`——CI 上綠燈、在對話裡問卻是另一個答案，
沒有比這更難查的事。

兩個為了「給模型讀」而做的取捨：

- **預設只回錯誤等級、最多 50 筆。** 全庫掃出來的東西一次倒進對話會吃掉幾萬 token，
  而模型真正要動手處理的永遠是最嚴重的那幾筆。要看更多再調 `limit` 與 `severity`。
- **每一筆都帶 `confidence` 與取用鏈。** 只說「節點不存在」的話，模型沒有依據判斷
  該改程式碼還是該改 .csd；把「我是根據哪份 .csd 判斷的」一起講出來才有辦法接續。
"""
from __future__ import annotations

import os

from . import bind, projects, report, scan, verify
from .csdtree import CsdError
from .projects import ProjectError
from .verify import VerifyError
from .rules import ALL_RULES, GROUPS, resolve_groups

_PROJECT = {"type": "string",
            "description": "專案名稱或資料夾（可省略；省略時自動判斷）"}

TOOLS = [
    {
        "name": "list_projects",
        "description": "列出認得的專案，以及各自的 Lua 根目錄與 cocosstudio 根目錄。"
                       "不確定要對哪個專案動手、或懷疑對到錯的 .csd 時先叫這個。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rules",
        "description": "列出所有檢查規則與各自的意義。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "scan",
        "description": "掃描 Lua：節點契約（getChildByName 取的節點在 .csd 裡存不存在）、"
                       "生命週期對稱（排程、事件註冊了有沒有解除）、"
                       "共用資源契約（引用的圖檔／plist 在不在）。"
                       "改完一個模組後叫這個確認沒有做壞。"
                       "預設只回錯誤等級的問題；要看警告與提醒把 severity 調低。"
                       "**回傳的 summaryTable 是一份 markdown 統整表，回答時要原樣呈現出來**"
                       "——裡面的涵蓋率會講出「有幾個檔案根本沒被檢查」，"
                       "少了那一段，使用者會把「沒有回報」讀成「都對」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "檔案、資料夾或模組名稱（例如 Modules/Lobby "
                                        "或直接寫 Lobby）；省略則全掃"},
                "project": _PROJECT,
                "rules": {"type": "string",
                          "description": f"要跑的群組或規則，逗號分隔。群組："
                                         f"{'、'.join(GROUPS)}；省略則全跑"},
                "severity": {"type": "string", "enum": ["error", "warn", "info"],
                             "default": "error",
                             "description": "只回這個等級以上的問題"},
                "changed": {"type": "boolean", "default": False,
                            "description": "只檢查 git 上有異動的 .lua"},
                "limit": {"type": "integer", "default": 50,
                          "description": "最多回幾筆（其餘只計數）"},
                "useBaseline": {"type": "boolean", "default": True,
                                "description": "扣掉基準檔裡既有的問題，只看新出現的"},
            },
        },
    },
    {
        "name": "explain",
        "description": "單一 Lua 檔的診斷：它被判定對應到哪個 .csd、依據是什麼、"
                       "有哪些問題。**回報「節點不存在」之前先用這個確認對到的是正確的 "
                       ".csd**——對錯了的話問題出在解析，不是程式碼。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "要診斷的 .lua"},
                           "project": _PROJECT},
            "required": ["path"],
        },
    },
    {
        "name": "node_tree",
        "description": "印出 .csd 的節點樹。這裡的階層與**執行期**一致："
                       "ProjectNode 引用的子檔會直接接在引用處底下，"
                       "不像 csdview 會多出子檔根節點那一層。"
                       "要確認某個節點該怎麼取時用這個。",
        "inputSchema": {
            "type": "object",
            "properties": {"csd": {"type": "string",
                                   "description": ".csd 路徑、相對路徑或檔名"},
                           "project": _PROJECT,
                           "depth": {"type": "integer", "default": 99,
                                     "description": "只回到第幾層"}},
            "required": ["csd"],
        },
    },
    {
        "name": "bind_properties",
        "description": "從 .csd 產生 Properties（UIManager）綁定程式碼——"
                       "`local NodeNames = {...}` 加上 `self.m_xxx = "
                       "parent:getChildByName( NodeNames.XXX )`。"
                       "要新增或重寫一個模組的 XxxProperties.lua 時用這個，"
                       "**不要自己看著 node_tree 手打**：節點名直接取自 .csd，"
                       "取用鏈也是照樹算的，產出來的程式碼天生就通得過 nodes 規則"
                       "（實測 80 份 .csd、2139 個綁定，0 findings）。"
                       "欄位名是慣例的 m_<節點名>，改成更好讀的名字很正常。",
        "inputSchema": {
            "type": "object",
            "properties": {"csd": {"type": "string",
                                   "description": ".csd 路徑、相對路徑或檔名"},
                           "name": {"type": "string",
                                    "description": "類別名，省略時用 .csd 檔名"},
                           "module": {"type": "string",
                                      "description": "模組名，放進 IClass 與 ---@class"},
                           "includeDefault": {
                               "type": "boolean", "default": False,
                               "description": "連 Panel_1 這種沒改過名字的節點也綁"},
                           "project": _PROJECT},
            "required": ["csd"],
        },
    },
    {
        "name": "verify_parser",
        "description": "拿 csdview 當對照，檢查這裡的 .csd 解析有沒有語意缺口。"
                       "**懷疑某一筆「節點不存在」是解析錯的時候用這個**——"
                       "explain 告訴你對到哪份 .csd，這個告訴你那份 .csd 讀得對不對。"
                       "曾經漏過 ProjectNode 引用處自己加的子節點，造成 9 筆假的錯誤。"
                       "一份 .csd 約 0.5 秒，所以省略 csd 時只抽樣。",
        "inputSchema": {
            "type": "object",
            "properties": {"csd": {"type": "string",
                                   "description": ".csd 路徑或檔名；省略則隨機抽樣"},
                           "project": _PROJECT,
                           "sample": {"type": "integer", "default": 20,
                                      "description": "省略 csd 時抽幾份"}},
        },
    },
    {
        "name": "write_baseline",
        "description": "把當下的掃描結果存成基準檔。之後只有**新出現的**問題會讓檢查失敗，"
                       "既有的那些照樣列在報告裡。第一次把檢查接進 CI 時要先做這個，"
                       "不然既有的幾十筆會讓 CI 直接全紅。",
        "inputSchema": {
            "type": "object",
            "properties": {"project": _PROJECT,
                           "path": {"type": "string",
                                    "description": "基準檔要寫到哪（省略用預設位置）"}},
        },
    },
]


def _workspace(args: dict):
    return projects.resolve(args.get("project"), args.get("path"))


def _explain_table(payload: dict) -> str:
    """單一檔案的診斷表：先講「憑什麼對到這份 .csd」，再逐筆列問題。

    對 agent 來說這兩件事要一起看：等級再高，只要 .csd 對錯了就不該去動程式碼。
    """
    def row(cells):
        return "| " + " | ".join(str(c) for c in cells) + " |"

    csd = payload.get("csd") or []
    shown = "、".join(f"`{c}`" for c in csd[:3]) or "（無）"
    if payload.get("csdCount", len(csd)) > 3:
        shown += f" 等 {payload['csdCount']} 份"
    lines = ["### 診斷：`%s`" % payload["file"], "",
             row(["項目", "內容"]), row(["---", "---"]),
             row(["對到的 .csd", shown]),
             row(["依據", f"`{payload.get('origin') or '找不到'}`"
                  f"（把握 {payload.get('confidence')}）"]),
             row(["排程", f"Safe {payload.get('scheduleSafe', 0)} 次、"
                  f"原生 {payload.get('scheduleRaw', 0)} 次"])]
    if payload.get("note"):
        lines.append(row(["說明", payload["note"]]))
    lines += ["", "**這個檔案的問題**", ""]
    items = payload.get("findings") or []
    if not items:
        lines.append("沒有發現問題。")
        return "\n".join(lines)
    lines += [row(["行", "規則", "等級", "為什麼是這個等級", "訊息"]),
              row(["--:", "---", "---", "---", "---"])]
    for item in items:
        lines.append(row([item["line"], f"`{item['rule']}`",
                          report.severity_label(item["severity"]),
                          report.why_from_dict(item), item["message"]]))
    return "\n".join(lines)


def _depth(value) -> int:
    """`depth` 用 `or 99` 會把 0 也換成 99——0 的意思是「只要根節點」。"""
    try:
        return 99 if value is None else int(value)
    except (TypeError, ValueError):
        return 99


def _baseline_path(workspace, given: "str | None" = None) -> str:
    from .cli import DEFAULT_BASELINE
    return given or os.path.join(workspace.directory, DEFAULT_BASELINE)


def call(name: str, args: dict):
    """執行一個工具，回傳可序列化的結果。"""
    if name == "list_projects":
        return {"projects": [w.to_dict() for w in projects.known()]}

    if name == "rules":
        return {"groups": {k: {"title": v["title"], "doc": v["doc"]}
                           for k, v in GROUPS.items()},
                "rules": ALL_RULES}

    if name == "scan":
        workspace = _workspace(args)
        groups = resolve_groups(args.get("rules"))
        severity = args.get("severity") or report.ERROR
        if args.get("changed"):
            files = scan.changed_files(workspace)
            if not files:
                return {"findings": [], "note": "沒有異動到的 .lua。"}
        else:
            files = (projects.lua_files(workspace, args["path"])
                     if args.get("path") else None)

        result = scan.scan(workspace, files, groups, severity)
        findings, stats = result["findings"], result["stats"]

        known = None
        if args.get("useBaseline", True):
            path = _baseline_path(workspace)
            if os.path.isfile(path):
                findings, known = report.split_baseline(
                    findings, report.load_baseline(path), workspace.lua_root)

        limit = int(args.get("limit") or 50)
        ordered = sorted(findings, key=report.sort_key)
        payload = report.to_json(ordered[:limit], workspace.lua_root, stats, known,
                                 counted=findings)
        payload["total"] = len(findings)
        # 一份可以原樣貼出去的 markdown 表格。給 agent 自己從 summary 重組的話
        # 要花 token，而且數字抄錯沒人看得出來。
        payload["summaryTable"] = report.summary_markdown(payload["summary"])
        payload["findingsTable"] = report.findings_markdown(
            ordered[:limit], workspace.lua_root)
        payload["present"] = (
            "回答時**先把 summaryTable 原樣貼出來**（markdown 表格），再接你的重點。"
            "涵蓋率那一段不要省略——「沒有回報」不等於「都對」。"
            "要列出具體問題時用 findingsTable，它的最後一欄已經寫好"
            "「為什麼是這個等級」，不必自己從 detail 推。")
        if len(findings) > limit:
            payload["note"] = (f"共 {len(findings)} 筆，只列出最嚴重的 {limit} 筆。"
                               "調高 limit 或縮小 path 看其餘的。")
        return payload

    if name == "explain":
        workspace = _workspace(args)
        # 路徑解析跟 CLI 共用 scan.resolve_lua_path，兩邊不會有一邊行一邊不行
        payload = scan.explain_file(workspace, args.get("path") or "")
        payload["summaryTable"] = _explain_table(payload)
        payload["present"] = ("回答時把 summaryTable 原樣呈現出來（markdown 表格）。"
                              "「對到的 csd」那一列要一起講——對錯了的話問題出在解析，"
                              "不是程式碼。")
        return payload

    if name == "node_tree":
        workspace = _workspace(args)
        project = workspace.csd
        if project is None:
            raise ProjectError("這個專案沒有設定 cocosstudio 根目錄，看不了 .csd。")
        spec = args.get("csd") or ""
        matches = ([os.path.abspath(spec)] if os.path.isfile(spec)
                   else project.resolve(spec)
                   or project.by_stem(os.path.splitext(os.path.basename(spec))[0]))
        if not matches:
            raise ProjectError(f"找不到 '{spec}'。用 list_projects 確認 cocosstudio 根目錄。")
        if len(matches) > 1:
            listed = "、".join(report.relative(m, project.root) for m in matches[:8])
            raise ProjectError(f"'{spec}' 對到 {len(matches)} 個檔案，請指定完整相對路徑：{listed}")
        tree = project.tree(matches[0])
        return {"file": report.relative(matches[0], project.root),
                "tree": tree.to_dict(_depth(args.get("depth")))}

    if name == "bind_properties":
        workspace = _workspace(args)
        project = workspace.csd
        if project is None:
            raise ProjectError("這個專案沒有設定 cocosstudio 根目錄，看不了 .csd。")
        try:
            result = bind.build(project, args.get("csd") or "",
                                args.get("name") or "", args.get("module") or "",
                                bool(args.get("includeDefault")))
        except CsdError as exc:
            raise ProjectError(str(exc)) from exc
        notes = []
        if result["skipped"]:
            notes.append(f"跳過 {len(result['skipped'])} 個沒改過名字的節點"
                         "（Panel_1 這種）；要的話傳 includeDefault")
        for node, was, now in result["renamed"]:
            notes.append(f"節點 '{node}' 撞名，欄位改成 {now}（原本會是 {was}）"
                         "——建議手動改成看得懂的名字")
        return {"csd": report.relative(result["csd"], project.root),
                "lua": result["lua"],
                "bindings": result["bindings"],
                "notes": notes,
                "present": "把 `lua` 原樣貼出來當程式碼區塊。"
                           "`notes` 有東西的話要一起講——那些是需要人決定的地方。"}

    if name == "verify_parser":
        workspace = _workspace(args)
        project = workspace.csd
        if project is None:
            raise ProjectError("這個專案沒有設定 cocosstudio 根目錄，沒有 .csd 可比對。")
        spec = args.get("csd") or ""
        if spec:
            paths = ([os.path.abspath(spec)] if os.path.isfile(spec)
                     else project.resolve(spec)
                     or project.by_stem(os.path.splitext(os.path.basename(spec))[0]))
            if not paths:
                raise ProjectError(f"找不到 '{spec}'。")
        else:
            import random
            every = sorted(project.index.values())
            random.Random(0).shuffle(every)
            paths = every[:int(args.get("sample") or 20)]
        result = verify.compare(project, paths)
        result["note"] = ("沒有 gaps 就代表解析與 csdview 一致（刻意不保留的那層"
                          "被引用檔案根節點已經扣掉）。有 gaps 是**這個工具**的缺口，"
                          "不是專案的問題——別去改 Lua。")
        return result

    if name == "write_baseline":
        workspace = projects.resolve(args.get("project"))
        result = scan.scan(workspace)
        info = report.save_baseline(_baseline_path(workspace, args.get("path")),
                                    result["findings"], workspace.lua_root)
        info["note"] = ("之後 scan 預設會扣掉這些，只回新出現的問題。"
                        "修掉一批之後重跑這個工具就能收緊基準。")
        return info

    raise ProjectError(f"沒有這個工具：{name}")


# server.py 用來判斷「這是使用者做得到的修正」而不是程式碼出錯
USER_ERRORS = (ProjectError, CsdError, VerifyError, ValueError)
