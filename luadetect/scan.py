"""掃描的主流程：挑檔案、跑規則、收統計。

CLI 與 MCP 都走這裡，兩邊行為一致——CI 上綠燈、在對話裡問卻是另一個答案，
沒有比這更難查的事。
"""
from __future__ import annotations

import os
import subprocess

from . import fileio, luasrc, projects, report
from .rules import GROUPS
from .rules import lifecycle as lifecycle_rules
from .rules import nodes as node_rules
from .rules import resources as resource_rules

_SEVERITY_RANK = {report.ERROR: 0, report.WARN: 1, report.INFO: 2}


def changed_files(workspace, base: "str | None" = None,
                  staged: bool = False) -> list:
    """git 上有異動的 .lua——pre-commit 與 PR 檢查用這個，只看碰過的檔案。

    `staged=True` 只列這次要提交的（`--cached`），pre-commit hook 用這個：
    工作目錄裡還沒 `git add` 的改動不屬於這次 commit，拿它們去擋 commit 是錯的。
    檔案**內容**也一樣要從索引讀（見 `index_reader`），不然 `git add -p` 只暫存一半時
    檢查的是工作目錄那份——會放過已經暫存的問題，也會為沒要提交的改動擋下 commit。

    git 跑不起來、或 `base` 指到不存在的 ref 時**拋例外**，不能回空清單：
    回空的話呼叫端會印「沒有異動到的 .lua」然後 exit 0，於是 CI 從接進去的第一天
    就永遠綠燈。CI 的淺層 clone（GitHub Actions 預設 fetch-depth: 1）沒有
    `origin/main`，正好會踩到這條。
    """
    repo = workspace.directory
    # --no-optional-locks：只是要讀 diff，不必為了刷新 index 去搶 index.lock，
    # 免得跟另一個正在 commit 的行程互相卡住。
    prefix = ["git", "--no-optional-locks", "diff", "--name-only",
              "--diff-filter=ACMR"]
    if base:
        commands = [prefix + [f"{base}...HEAD"]]
    elif staged:
        commands = [prefix + ["--cached"]]
    else:
        commands = [prefix, prefix + ["--cached"]]

    found: list = []
    for command in commands:
        try:
            done = subprocess.run(command, cwd=repo, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            raise projects.ProjectError(
                f"叫不動 git（{exc}）。--changed 需要 git 在 PATH 上。") from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            hint = detail[0] if detail else f"離開碼 {done.returncode}"
            raise projects.ProjectError(
                f"git 在 {repo} 執行 {' '.join(command[1:])} 失敗：{hint}"
                + (f"\n  '{base}' 這個 ref 在這個 clone 裡不存在的話，CI 要先"
                   " fetch 到足夠深度（例如 actions/checkout 的 fetch-depth: 0）。"
                   if base else ""))
        for line in done.stdout.splitlines():
            line = line.strip()
            if line.lower().endswith(".lua"):
                full = os.path.join(repo, line)
                if os.path.isfile(full):
                    found.append(os.path.abspath(full))
    lua_root = os.path.normcase(workspace.lua_root)
    return sorted({f for f in found
                   if os.path.normcase(f).startswith(lua_root + os.sep)})


class index_reader:
    """從 git 索引（staged 內容）讀檔，而不是從工作目錄。

    pre-commit 要檢查的是**這次要提交的內容**。`git add -p` 只暫存一半的時候，
    工作目錄那份跟要進 commit 的那份不是同一個東西：暫存了有問題的版本、工作目錄
    已經修好，讀工作目錄就會放過那個問題；反過來也會為沒要提交的改動擋下 commit。

    用一個 `git cat-file --batch` 子行程處理全部檔案，不是每個檔案開一次 git——
    pre-commit 的延遲會被直接感受到，這條規則裡「不會有人想拔掉」是設計目標。

    讀不到的檔案回 None，呼叫端就退回讀磁碟（例如剛好被 unstage 掉）。

    **邊界講清楚**：被檢查的那個檔案的內容來自索引，但它的**周邊**——成對的
    `XxxProperties.lua`、整個模組的 `setName` 與 .csb 字串——還是讀工作目錄。
    要連周邊也精確就得改成從索引列檔案（`git ls-files`）再逐個讀，那是另一個
    規模的改動；而周邊在實務上是「等於或新於」索引（先改檔再 git add），
    所以拿工作目錄那份當上下文不會漏掉問題。真正危險的「暫存了壞的、工作目錄
    已經修好」那一種，被檢查的檔案本身已經走索引了。
    """

    def __init__(self, repo: str):
        self.repo = repo
        self._process = None

    def _start(self):
        if self._process is None:
            try:
                self._process = subprocess.Popen(
                    ["git", "--no-optional-locks", "cat-file", "--batch"],
                    cwd=self.repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise projects.ProjectError(
                    f"叫不動 git（{exc}）。--staged 需要 git 在 PATH 上。") from exc
        return self._process

    def __call__(self, path: str) -> "str | None":
        rel = os.path.relpath(os.path.abspath(path), self.repo).replace("\\", "/")
        if rel.startswith(".."):
            return None                      # 不在這個倉庫裡
        process = self._start()
        try:
            process.stdin.write(f":{rel}\n".encode("utf-8"))
            process.stdin.flush()
            header = process.stdout.readline().decode("utf-8", "replace").strip()
            parts = header.split(" ")
            # `<sha> <type> <size>`，或 `<物件> missing` / `ambiguous`
            if len(parts) != 3 or parts[1] != "blob":
                return None                  # 不是檔案（可能是目錄）或根本沒這個物件
            size = int(parts[2])
            blob = process.stdout.read(size)
            process.stdout.read(1)           # 物件後面那個換行
        except (OSError, ValueError, IndexError, AttributeError):
            self.close()
            return None
        # 換行統一成 \n，跟讀磁碟那條路（text 模式會做 universal newlines）一致，
        # 這樣行號與位移在兩種來源之間是同一套。
        return blob.decode("utf-8", "replace").replace("\r\n", "\n")

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def resolve_lua_path(workspace, path: str) -> str:
    """把使用者給的 .lua 路徑解成絕對路徑。

    相對路徑先照工作目錄試，再試 Lua 根與專案根——CLI 與 MCP 走同一個函式，
    不然 `explain Modules/Lobby/LobbyView.lua` 在對話裡可以、在命令列卻說檔案
    不存在（CLI 只以 cwd 為準，而 cwd 通常是工具自己的資料夾）。
    """
    candidates = [path] if os.path.isabs(path) else [
        os.path.join(os.getcwd(), path),
        os.path.join(workspace.lua_root, path),
        os.path.join(workspace.directory, path),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    raise projects.ProjectError(
        f"找不到 {path}。試過："
        + "、".join(os.path.abspath(c) for c in candidates))


def scan(workspace, paths: "list | None" = None, groups: "list | None" = None,
         min_severity: str = report.INFO, explain: bool = False,
         read=None) -> dict:
    """跑一次掃描，回傳 {findings, stats}。

    `read(path)` 給了就用它取檔案內容（回 None 表示它取不到，退回讀磁碟）。
    pre-commit 用 `index_reader` 從索引讀，檢查的才是這次真的要提交的內容。
    """
    groups = groups or list(GROUPS)
    files = paths if paths is not None else projects.lua_files(workspace)
    limit = _SEVERITY_RANK.get(min_severity, 2)
    node_rules.reset_caches()

    findings: list = []
    stats = {"files": 0, "unresolvedCsb": 0, "unreadable": 0, "scheduleRaw": 0,
             "scheduleSafe": 0, "checkedNodes": 0, "nodeFiles": 0,
             "checkedResources": 0, "unresolvedResources": 0,
             "dynamicResources": 0, "templateResources": 0,
             "fragmentResources": 0, "resourcePaths": 0, "groups": list(groups)}

    for path in files:
        text = read(path) if read is not None else None
        if text is None:
            text = fileio.read_text(path)     # 被鎖住會短暫重試
        if text is None:
            # 讀不到就跳過，但要計數：原本連數字都沒有，一個沒被檢查到的檔案
            # 在報告上跟一個乾淨的檔案長得一模一樣。
            stats["unreadable"] += 1
            continue
        stats["files"] += 1
        produced: list = []
        # 剝註解是整個掃描最貴的一步，一個檔案只做一次，兩組規則共用。
        # 原本兩組規則加 stats 加 csb 字串各自剝一遍，實測是每個檔案 4.9 次。
        code, spans = luasrc.scan(text)
        # 常數解析跟剝註解一樣，一個檔案只做一次、三組規則共用。
        # 原本 nodes 一次、resources 的 check 與 stats 各一次——實測全庫
        # 3363 次呼叫（每個檔案 2.6 次）、3.10 秒，其中三分之二是白做的。
        # 而且只有真的用得到的檔案才算：`nodes` 要有節點取用、`resources` 要有
        # 格式樣板（`%` 是便宜又安全的過度估計）。實測 1304 個檔案裡有 530 個
        # 兩者都不是，那些的常數解析是純浪費。
        consts = None
        if (("nodes" in groups and node_rules.has_node_access(code))
                or ("resources" in groups and "%" in code)):
            consts = luasrc.constants(code, spans)

        if "nodes" in groups:
            # 分母跟規則的守門條件用同一個判斷，「完成 + 跳過」才會剛好等於它。
            # 原本這裡是 `"getChildByName" in code`，規則卻還認得別的取用寫法，
            # 於是全庫算出 574 + 32 = 606 > 605，兩個數字對不起來。
            has_nodes = node_rules.has_node_access(code)
            if has_nodes:
                stats["nodeFiles"] += 1
            found = node_rules.check(path, text, workspace, code=code,
                                     spans=spans, consts=consts)
            unresolved = [f for f in found if f.rule == "NODE_CSB_UNRESOLVED"]
            if unresolved:
                stats["unresolvedCsb"] += 1
                if not explain:
                    found = [f for f in found if f.rule != "NODE_CSB_UNRESOLVED"]
            elif has_nodes:
                stats["checkedNodes"] += 1
            produced.extend(found)

        if "lifecycle" in groups:
            produced.extend(lifecycle_rules.check(path, text, workspace,
                                                  code=code, spans=spans))
            numbers = lifecycle_rules.stats(text, code, spans)
            stats["scheduleRaw"] += numbers["scheduleRaw"]
            stats["scheduleSafe"] += numbers["scheduleSafe"]

        if "resources" in groups:
            produced.extend(resource_rules.check(path, text, workspace,
                                                 code=code, spans=spans,
                                                 consts=consts))
            numbers = resource_rules.stats(text, code, spans, workspace,
                                           consts=consts)
            stats["checkedResources"] += numbers["checkedResources"]
            stats["unresolvedResources"] += numbers["unresolvedResources"]
            for name in ("dynamicResources", "templateResources",
                         "fragmentResources", "resourcePaths"):
                stats[name] = stats.get(name, 0) + numbers.get(name, 0)

        findings.extend(f for f in produced
                        if _SEVERITY_RANK.get(f.severity, 3) <= limit)

    return {"findings": findings, "stats": stats}


def explain_file(workspace, path: str) -> dict:
    """單一檔案的診斷：對到哪些 .csb / .csd、解出哪些節點鏈、有哪些問題。

    做 resolver 出錯時的第一站——回報「找不到節點」之前，先確認它讀的是對的 .csd。
    """
    path = resolve_lua_path(workspace, path)
    text = fileio.read_text(path)
    if text is None:
        raise projects.ProjectError(f"讀不了 {path}（被別的程式鎖住或權限不足）。")
    node_rules.reset_caches()
    code, spans = luasrc.scan(text)

    sources = node_rules.resolve_sources(path, workspace, code)
    found = node_rules.check(path, text, workspace, sources, code=code, spans=spans)
    found.extend(lifecycle_rules.check(path, text, workspace, code=code, spans=spans))
    found.extend(resource_rules.check(path, text, workspace, code=code, spans=spans))
    payload = {
        "file": report.relative(path, workspace.lua_root),
        "csb": sources.get("csb") or [],
        # module 來源的候選 .csd 可以到幾百份（實測中位數 239）。全部列出來對
        # MCP 是純浪費 token，而且看的人只需要知道「範圍有多大」。
        "csd": [report.relative(c, workspace.csd_root) if workspace.csd_root
                else c for c in (sources.get("csd") or [])[:20]],
        "csdCount": len(sources.get("csd") or []),
        "origin": sources.get("origin"),
        "confidence": ("high" if sources.get("origin") in
                       ("self", "paired", "config", "stem") else "medium"),
        "findings": [f.to_dict(workspace.lua_root) for f in sorted(found, key=report.sort_key)],
    }
    for key in ("note", "from", "unresolved"):
        if sources.get(key):
            payload[key] = sources[key]
    payload.update(lifecycle_rules.stats(text, code, spans))
    payload.update(resource_rules.stats(text, code, spans, workspace))
    return payload
