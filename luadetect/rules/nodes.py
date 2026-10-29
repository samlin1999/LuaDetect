"""節點契約：`getChildByName( "字串" )` 拿到的東西在 .csd 裡真的存在嗎。

字串式的節點查找在編譯期完全沒有保護。美術在 Cocos Studio 把節點改名或搬一層，
Lua 這邊拿到的是 `nil`，要等玩家開到那個畫面、對 nil 呼叫方法時才炸——
CrashReport 裡「某某按鈕不見了」那一類幾乎都是這樣來的。

三個刻意的取捨，都是為了讓誤報少到人願意繼續用：

- **查不出對應 .csb 的檔案直接跳過**，不猜。猜錯會叫人去改本來正確的程式碼，
  比漏報糟。跳過的檔案會記在 stats 裡，用 `csb_of` 查為什麼。
- **名稱解不出字面值就跳過**（`string.format` 組出來的名字之類），不當成問題。
- **「名稱存在但父節點對不上」與「整份都找不到」分開報**。前者常常是同一個
  名稱在多個 .csd 裡都有、而這個檔案同時吃好幾份 .csb 造成的，把握較低。
"""
from __future__ import annotations

import os
import re

from .. import fileio, luasrc
from ..csdtree import CsdError, find_chain, names as tree_names
from ..report import ERROR, INFO, WARN, Finding
from ..fileio import relative

RULES = {
    "NODE_MISSING": "getChildByName 取的節點在對應的 .csd 裡不存在",
    "NODE_WRONG_PARENT": "節點存在，但不在程式碼假設的父節點底下",
    "NODE_CSB_UNRESOLVED": "查不出這個檔案對應哪個 .csb，節點檢查跳過",
}

# 直接取子節點（只看直接子節點）與深層搜尋（整棵樹找），語意不同要分開處理
_DIRECT = re.compile(r":\s*(getChildByName|seekWidgetByName)\s*\(")
_DEEP = re.compile(r"[:.]\s*GetNodeByName\s*\(")
_CSB = re.compile(r"['\"]([^'\"]*\.csb)['\"]")
# 執行期給節點命名的兩種寫法。引數**不一定是字面字串**——`setName( UIName.IMG_FISH )`
# 這種用常數的寫法很常見，原本的正則只認字面值，於是那些節點沒被排除掉，
# `getChildByName` 取它們就被冤枉成「.csd 裡不存在」（實測 AIAutoSettingCellGroup
# 的 Image_fish_child 就是這樣，而它下一行就是 `Img:setName( ...IMG_FISH_CHILD )`）。
_SET_NAME = re.compile(r":\s*setName\s*\(\s*([^();]*?)\s*\)")
_NAME_ASSIGN = re.compile(r"\.name\s*=\s*([^\s;]+)")

# 別名指派：`local content = self.m_info_content`，右邊是單純的名字才算
_ALIAS = re.compile(r"^[ \t]*(?:local\s+)?([A-Za-z_][\w.]*)\s*=\s*([A-Za-z_][\w.]*)[ \t]*$",
                    re.MULTILINE)
_FUNC = re.compile(r"\bfunction\b")


def csb_literals(path: "str | None" = None, code: "str | None" = None) -> list:
    """檔案裡出現的 .csb 字串。`code` 給了就不重讀檔（呼叫端多半已經剝好了）。"""
    if code is None:
        text = fileio.read_text(path)
        if text is None:
            return []
        code = luasrc.scan(text)[0]
    return list(dict.fromkeys(_CSB.findall(code)))


def _paired_file(path: str) -> "str | None":
    """`XxxProperties.lua` 是產生出來的綁定檔，csb 路徑寫在成對的 `Xxx.lua`。"""
    base = os.path.basename(path)
    if not base.endswith("Properties.lua"):
        return None
    paired = os.path.join(os.path.dirname(path),
                          base[: -len("Properties.lua")] + ".lua")
    return paired if os.path.isfile(paired) else None


def _module_dir(path: str, lua_root: str) -> str:
    """這個檔案屬於哪個模組——從它往上走到 `Modules` 的下一層。

    **絕對不能走出 `lua_root`。** 原本的終止條件是「parent 剛好等於 lua_root」，
    對放在 `lua_root` 正下方的檔案（Inanna 有 27 個，`CommonFun.lua` 這些）永遠
    不成立，於是一路爬到磁碟根目錄回傳 `C:\\`——接著 `_module_facts` 會
    `os.walk("C:\\")` 把整顆磁碟走一遍。目前剛好沒有檔案觸發（要同時有
    `getChildByName` 又對得到 .csd），但那是運氣，不是設計。
    """
    root = os.path.abspath(lua_root)
    low_root = os.path.normcase(root)
    current = os.path.dirname(os.path.abspath(path))
    if os.path.normcase(current) == low_root:
        return root                    # 檔案就放在 lua_root 正下方
    if not os.path.normcase(current).startswith(low_root + os.sep):
        return current                 # 根本不在 lua_root 底下，就以它自己的資料夾為界
    while True:
        parent = os.path.dirname(current)
        if os.path.basename(parent).lower() == "modules":
            return current
        if os.path.normcase(parent) == low_root or parent == current:
            return current
        current = parent


_module_cache: dict = {}


def reset_caches() -> None:
    """丟掉模組層級的快取。

    **每次掃描的進入點都要呼叫。** 這些快取的用途是「同一輪掃描裡不要把同一個模組
    走十遍」，不是跨次保存：長駐的 MCP 伺服器裡沒有失效條件的話，改完 Lua 再問一次
    會拿到改之前的答案（實測：補上 `setName` 之後那筆 NODE_MISSING 還在報，
    要重開伺服器才會消失），而「改完用 scan 確認」正是 MCP 的主要用法。
    """
    _module_cache.clear()


def _module_facts(path: str, workspace) -> dict:
    """整個模組讀一遍，同時收兩件事：`setName` 建出來的節點名，與用到的 .csb 資料夾。

    這兩件事本來各自走一趟模組、各自把每個 .lua 都剝一次註解，而剝註解是整個掃描
    最貴的一步。合成一趟之後同一個檔案只剝一次。

    - **runtime**：程式碼自己 `setName( "x" )` 建的節點名。這些名字不會出現在任何
      .csd 裡，但 `getChildByName( "x" )` 完全正確——大廳的推薦遊戲按鈕就是這樣掛
      上去的。不排掉它們，這一類每一行都會被冤枉。整個模組一起收：建的地方與取的
      地方常常不是同一個檔案。
    - **folders**：模組引用到的 .csb 所在的 cocosstudio 資料夾，也就是這個模組的地盤。
      給那些自己完全沒提到 .csb 的檔案當候選（`TipHelper` 那類拿外面傳進來的節點）。
    """
    module = _module_dir(path, workspace.lua_root)
    key = (os.path.normcase(module), os.path.normcase(workspace.lua_root),
           os.path.normcase(workspace.csd_root or ""))
    cached = _module_cache.get(key)
    if cached is not None:
        return cached

    runtime: set = set()
    literals: list = []
    unreadable: list = []
    module_files: list = []
    texts: dict = {}
    for dirpath, dirs, files in os.walk(module):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.lower().endswith(".lua"):
                continue
            full = os.path.join(dirpath, name)
            text = fileio.read_text(full)
            # 內容留著：`_usage` 要為每個候選名字掃這個模組，不留的話
            # 58 個名字 × 上百個檔案就是幾萬次重複讀檔（實測慢 5 秒）。
            # 生命週期跟 `_module_cache` 一樣，每次掃描開頭就清掉。
            texts[full] = text
            module_files.append(full)
            if text is None:
                # 讀不到的檔案裡可能有 setName，少收就會冤枉正確的程式碼。
                # 記下來讓呼叫端把把握度降級，不要安靜跳過。
                unreadable.append(full)
                continue
            # 便宜的子字串先篩：整個專案只有 47% 的檔案提到這幾件事，
            # 其餘的不必付剝註解的代價（剝註解是整個掃描最貴的一步）。
            names_here = "setName" in text or ".name" in text
            if not names_here and ".csb" not in text:
                continue
            code, spans = luasrc.scan(text)
            if names_here:
                # 引數可能是常數（`setName( UIName.IMG_FISH )`），所以要走
                # 跟 getChildByName 同一套字面值解析，不是只收引號裡的東西。
                consts = luasrc.constants(code, spans)
                for pattern in (_SET_NAME, _NAME_ASSIGN):
                    for expr in pattern.findall(code):
                        literal = luasrc.resolve_literal(expr, consts)
                        if literal:
                            runtime.add(literal)
            literals.extend(csb_literals(code=code))

    facts = {"runtime": runtime, "csb": list(dict.fromkeys(literals)),
             "scope": None, "unreadable": unreadable,
             "files": module_files, "texts": texts, "scanned": {},
             "usage": {}}
    _module_cache[key] = facts
    return facts


def _runtime_names(path: str, workspace) -> set:
    return _module_facts(path, workspace)["runtime"]


# 「被讀」的樣子：名字後面**不是**單獨的 `=`。`self.m_x = v` 是寫、`self.m_x == v`
# 是讀、`self.m_x:setVisible()` / `f( self.m_x )` / `if self.m_x then` 都是讀。
_WRITE_AFTER = re.compile(r"[ \t]*=[^=]")


def _usage(path: str, workspace, name: str) -> dict:
    """`name` 這個欄位／變數在這個模組裡怎麼被用：`{"read": bool, "guarded": bool}`。

    答案**按名字快取**。一開始寫成「把整個模組的程式碼接成一份、每筆 finding 各拿
    四個正則掃一遍」，實測整體從 3.2 秒掉到 45 秒——58 筆 finding × 4 個正則 ×
    幾 MB 的語料。現在同一個名字只算一次，而且用便宜的子字串先篩掉不含它的檔案
    （一個名字通常只出現在一兩個檔案裡）。
    """
    facts = _module_facts(path, workspace)
    cached = facts["usage"].get(name)
    if cached is not None:
        return cached

    ident = r"(?:[\w.]+\.)?" + re.escape(name) + r"\b"
    # 四種防護寫法併成一條交替正則，一遍掃完。原本是四條各掃一遍，而且每個名字
    # 都要重新編譯——實測 564 個名字 × 5 條 = 2820 次編譯、掃描 2435 次。
    # 併起來語意完全相同（`any(四條之一命中)` == `交替式命中`）。
    guard = re.compile("|".join((
        ident + r"\s*[=~]=\s*nil",                      # x == nil / x ~= nil
        r"\bif\s+not\s+" + ident,                       # if not x
        r"\b(?:if|elseif|while)\s+" + ident
        + r"\s*(?:then|and\b|or\b)",                    # if x then / if x and ...
        ident + r"\s+and\s+" + ident,                   # x and x:foo()
    )))
    plain = re.compile(r"\b" + re.escape(name) + r"\b")
    answer = {"read": False, "guarded": False}
    for full in facts["files"]:
        text = facts["texts"].get(full)
        if text is None or name not in text:
            continue                        # C 層的子字串測試，比正則便宜得多
        code = facts["scanned"].get(full)
        if code is None:
            # 同一個檔案會被好幾個名字查到，剝註解只做一次
            code = facts["scanned"][full] = luasrc.scan(text)[0]
        if not answer["guarded"]:
            answer["guarded"] = bool(guard.search(code))
        if not answer["read"]:
            for match in plain.finditer(code):
                if not _WRITE_AFTER.match(code, match.end()):
                    answer["read"] = True
                    break
        if answer["read"] and answer["guarded"]:
            break
    facts["usage"][name] = answer
    return answer


def _module_scope(path: str, workspace) -> list:
    """模組層級的候選 .csd。

    給那些自己完全沒提到 .csb 的檔案用——它們拿的是別處建好、再傳進來的節點
    （`TipHelper` 這類）。做法是把整個模組引用到的 .csb 所在的 cocosstudio 資料夾
    當成這個模組的地盤，該資料夾底下的 .csd 全部納入候選。

    只驗「名稱存在不存在」、不驗父子關係，會漏掉一些問題，但**不會冤枉任何一行
    正確的程式碼**——那才是這個工具能被留著用的前提。

    csb → 資料夾的解析是**用到才做**：絕大多數檔案自己就寫了 .csb，走不到這裡，
    而 `_runtime_names` 每個檔案都要用，兩者共用同一趟走訪的結果。
    """
    facts = _module_facts(path, workspace)
    if facts["scope"] is not None:
        return facts["scope"]

    project = workspace.csd
    scope: list = []
    if project is not None:
        folders: list = []
        for csb in facts["csb"]:
            for csd in project.resolve(csb):
                folder = os.path.dirname(csd)
                if folder not in folders:
                    folders.append(folder)
        for folder in folders:
            scope.extend(project.in_folder(folder))
    facts["scope"] = list(dict.fromkeys(scope))
    return facts["scope"]


def _field_name(target: str) -> str:
    return target.split(".")[-1].split("[")[0].strip()


def _is_nil_guarded(path: str, workspace, target: str) -> bool:
    """這個欄位／變數有沒有被 nil 防護過。

    有防護就**不會當機**，那就不是 error。這是「取不到就自己建」這個很常見、
    而且完全正確的慣用法：

        local img = parent:getChildByName( "img_main" )
        if img == nil then
            img = inn.UrlImageView:createFromImageView( ... )
            img.name = "img_main"
        end

    以及只防護不建的（節點沒了就少顯示一個提示，不是當機）：

        self.m_txt_ingame_tip = panel:getChildByName( UIName.TXT_INGAME_TIP )
        if self.m_txt_ingame_tip then
            self.m_txt_ingame_tip:setVisible( false )
        end

    實測 Inanna 最後剩下的 3 筆 error 全部是這兩種，一筆真的當機都沒有。
    """
    name = _field_name(target)
    return bool(name) and _usage(path, workspace, name)["guarded"]


def _is_dereferenced(path: str, workspace, target: str) -> bool:
    """`target` 這個欄位（或區域變數）在這個模組裡有沒有被**讀**過。

    這是 `error` 與 `warn` 的分界，而且是這條規則最重要的一個判斷：
    `getChildByName` 回 nil **本身不會當機**，對那個 nil 呼叫方法才會。
    產生出來的 `XxxProperties.lua` 綁定檔裡每一行都只是「存進欄位」，.csd 那邊
    把節點刪掉之後沒有重新產生，就留下一批賦值 nil、卻沒有人讀的欄位——
    畫面照樣開得起來，只是少了那個元素。

    實測 Inanna：42 筆 error 等級的 NODE_MISSING 裡有 26 筆是這種死綁定
    （`MaintainPage.lua` 的 `m_img_bg`、`m_txt_time`、`m_update_btn` 整個檔案
    各只出現一次，就是賦值那行）。把它們當 error 擋 CI 是錯的。

    範圍是整個模組：Properties 檔賦值、View 檔讀取是這個專案的常態寫法。

    **只有「完全找不到任何讀取」才降級。** 漏看一個讀取會把真的當機降成警告，
    所以這裡刻意寬鬆地認定什麼算讀——寧可維持 error。
    """
    name = _field_name(target)
    if not name:
        return True                     # 解不出名字就別降級
    return _usage(path, workspace, name)["read"]


def resolve_sources(path: str, workspace, code: "str | None" = None) -> dict:
    """找出這個 Lua 檔對應的 .csd，並說明是怎麼找到的。

    回傳 `{"csb": [...], "csd": [...], "origin": ..., ...}`。`origin` 決定把握度，
    由可信到勉強：

    | origin | 依據 | 父子關係也驗嗎 |
    | --- | --- | --- |
    | `config` | `.luadetect.json` 裡人工指定 | 是 |
    | `self` | 檔案自己寫的 .csb 字串 | 是 |
    | `paired` | 成對的 View 檔寫的 .csb | 是 |
    | `stem` | 檔名與 .csd 同名且全庫只有一份 | 是 |
    | `module` | 整個模組用到的 cocosstudio 資料夾 | 否，只驗存在 |
    """
    project = workspace.csd
    result = {"csb": [], "csd": [], "origin": None, "note": None}
    if project is None:
        result["note"] = "這個專案沒有設定 cocosstudio 根目錄，節點檢查無法進行。"
        return result

    rel = relative(path, workspace.lua_root)
    override = (workspace.config.get("csbMap") or {}).get(rel)
    if override:
        result["csb"] = list(override) if isinstance(override, list) else [override]
        result["origin"] = "config"
    else:
        found = csb_literals(path, code)
        if found:
            result["csb"], result["origin"] = found, "self"
        else:
            paired = _paired_file(path)
            found = csb_literals(paired) if paired else []
            if found:
                result["csb"], result["origin"] = found, "paired"
                result["from"] = os.path.basename(paired)

    if result["csb"]:
        unresolved: list = []
        for csb in result["csb"]:
            matches = project.resolve(csb)
            if not matches:
                unresolved.append(csb)
            result["csd"].extend(matches)
        result["csd"] = list(dict.fromkeys(result["csd"]))
        if unresolved:
            result["unresolved"] = unresolved
        if result["csd"]:
            return result
        result["note"] = (f"寫著 {'、'.join(result['csb'][:3])}，"
                          "但在 cocosstudio 根目錄底下找不到對應的 .csd。")

    # 檔名慣例：FooView.lua / FooViewProperties.lua ↔ FooView.csd
    stem = os.path.splitext(os.path.basename(path))[0]
    for candidate in (stem[: -len("Properties")] if stem.endswith("Properties") else stem,):
        hits = project.by_stem(candidate)
        if len(hits) == 1:
            result.update({"csd": hits, "origin": "stem", "csb": [],
                           "note": f"檔名慣例：{os.path.basename(path)} ↔ "
                                   f"{os.path.basename(hits[0])}（全庫只有這一份）"})
            return result

    scope = _module_scope(path, workspace)
    if scope:
        result.update({"csd": scope, "origin": "module", "note": None})
        return result

    if not result["note"]:
        result["note"] = "這個檔案與整個模組都沒有出現能對到 .csd 的 .csb 字串。"
    return result


def _resolve_receiver(code: str, index: int, env: dict, consts: dict) -> "list | None":
    """接收者對應到哪一串節點名稱；解不出來回傳 None（視為未知的根）。"""
    expr = luasrc.expr_before(code, index)
    if not expr:
        return None
    if expr in env:
        return env[expr]
    # 串接寫法：`a:getChildByName( "x" ):getChildByName( "y" )`
    inner = re.search(r":\s*(?:getChildByName|seekWidgetByName)\s*\($", expr)
    if inner:
        head = expr[: inner.start()]
        arg = luasrc.call_args(expr, expr.index("(", inner.start()))
        name = luasrc.resolve_literal(luasrc.split_args(arg)[0] if arg else "", consts)
        if name:
            base = _resolve_receiver(head + " ", len(head) + 1, env, consts) or []
            return base + [name]
    return None


def has_node_access(code: str) -> bool:
    """這個檔案有沒有節點取用——**涵蓋率的分母就是它**。

    不要用 `"getChildByName" in code` 去猜：規則認得的取用寫法不只那一種
    （`_DEEP`、別名、函式回傳），用子字串當分母會算出「完成 574 + 跳過 32 = 606
    比分母 605 還多」這種對不起來的數字。分母要跟 `check()` 的守門條件是同一個
    判斷，所以兩邊共用這個函式。
    """
    return bool(_DIRECT.search(code) or _DEEP.search(code))


def check(path: str, text: str, workspace, sources: "dict | None" = None,
          code: "str | None" = None, spans: "list | None" = None,
          consts: "dict | None" = None) -> list:
    """檢查單一 Lua 檔的節點取用。

    `code` 是剝好註解的原始碼、`spans` 是字串區間，給了就不重剝。字串**不抹白**
    （節點名稱藏在裡面），所以結構性的正則要靠 `spans` 排除字串內的匹配。
    """
    if code is None:
        code, spans = luasrc.scan(text)
    if not has_node_access(code):
        return []

    sources = (sources if sources is not None
               else resolve_sources(path, workspace, code))
    if not sources["csd"]:
        return [Finding("NODE_CSB_UNRESOLVED", INFO, path, 1,
                        sources.get("note") or "查不出對應的 .csb，節點檢查跳過。",
                        key="csb", confidence="high",
                        detail={k: v for k, v in sources.items() if v})]

    project = workspace.csd
    roots = []
    root_of: list = []      # 與 roots 對齊的 .csd 路徑（壞檔會被跳過，不能靠索引對）
    broken: list = []
    for csd in sources["csd"]:
        try:
            roots.append(project.tree(csd))
            root_of.append(csd)
        except CsdError as exc:
            broken.append(str(exc))
    if not roots:
        # 對到的 .csd 全都讀不了（壞檔、Cocos Studio 正在存檔、權限）。
        # 原本直接 return []：檢查整份跳過，而報告裡一個字都沒有，只會讓人以為
        # 這個檔案很乾淨。跟「查不出 .csb」一樣要留一筆紀錄。
        return [Finding("NODE_CSB_UNRESOLVED", INFO, path, 1,
                        "對到的 .csd 讀不了，節點檢查跳過："
                        + "；".join(broken[:2]),
                        key="csd-unreadable", confidence="high",
                        detail={"csd": [os.path.basename(c) for c in sources["csd"]],
                                "errors": broken[:5]})]

    every = set()
    for root in roots:
        every |= tree_names(root)

    # 一個檔案吃多份 .csb（橫/直版、或整個模組）時，名稱是聯集，父子關係就沒那麼硬——
    # 這種情況只報「整份都找不到」，把握度也降一級。
    anchored = sources["origin"] in ("self", "paired", "config", "stem")
    single = len(roots) == 1 and anchored
    confidence = "high" if anchored else "medium"
    if consts is None:
        consts = luasrc.constants(code, spans)

    facts = _module_facts(path, workspace)
    runtime = facts["runtime"]
    # 模組裡有 .lua 讀不到時，`setName` 建出來的節點名就收得不齊——那些名字不會出現
    # 在任何 .csd 裡，但取用它們是完全正確的。少收一個就會冤枉一行正確的程式碼，
    # 所以這種時候不給 error、把握度也降一級，並在訊息裡講出原因。
    blind = facts.get("unreadable") or []
    if blind:
        confidence = "medium"

    events: list = []
    for kind, pattern in (("child", _DIRECT), ("deep", _DEEP),
                          ("alias", _ALIAS), ("func", _FUNC)):
        for match in luasrc.outside_strings(pattern, code, spans):
            events.append((match.start(), kind, match))
    events.sort(key=lambda item: item[0])

    env: dict = {}
    findings: list = []
    seen: set = set()          # 這個檔案取用過的節點名（去重）
    missed: set = set()        # 其中對不上的
    for position, kind, match in events:
        if kind == "func":
            # 區域變數不跨函式；`self.` 這類欄位才留著
            for key in [k for k in env if "." not in k]:
                env.pop(key, None)
            continue

        if kind == "alias":
            target, source = match.group(1), match.group(2)
            if source in env:
                env[target] = env[source]
            else:
                env.pop(target, None)
            continue

        open_paren = code.index("(", match.end() - 1)
        args = luasrc.split_args(luasrc.call_args(code, open_paren))
        if kind == "deep":
            # Helper:GetNodeByName( parent, name )——深層搜尋，只驗存在
            name = luasrc.resolve_literal(args[1], consts) if len(args) > 1 else None
            chain = [name] if name else None
            base = []
        else:
            name = luasrc.resolve_literal(args[0], consts) if args else None
            base = _resolve_receiver(code, match.start(), env, consts) or []
            chain = base + [name] if name else None

        if not name or name in runtime:
            continue

        target = luasrc.assign_target(code, match.start())
        if target and kind == "child":
            env[target] = chain

        line = luasrc.line_of(text, position)
        if kind == "deep" or not base:
            verdict = "ok" if name in every else "missing"
        else:
            verdict = find_chain(roots, chain)

        seen.add(name)
        if verdict != "ok":
            missed.add(name)

        if verdict == "missing":
            # 這個名字在**別的** .csd 裡出現過嗎。這是「節點真的被刪了」與
            # 「我們對到的不是這個檔案真正的版面」最有力的分辨依據：
            # 名字在別處存在，最可能的解釋是這支 Lua 的版面其實是那一份
            # ——實測 `StatusPage.lua` 就是這樣，它自己只寫了三個裝飾用的 .csb，
            # 真正的 `lo_bg/btn_safeBox` 在 `present/presentpage.csd` 裡。
            elsewhere = name in project.global_names()
            # 沒有錨定到特定 .csd 的檔案（節點是別的畫面建好傳進來的），
            # 名字只要在全庫任何一份 .csd 裡出現過就不報——分不出來就別亂指。
            if not anchored and elsewhere:
                continue
            shown = list(dict.fromkeys(os.path.basename(c) for c in sources["csd"]))
            where = (f"這個模組的 {len(sources['csd'])} 份 .csd" if len(shown) > 3
                     else "、".join(shown))
            # 取用鏈的起點也在這份 .csd 裡，才敢說「就是這裡少了一個節點」。
            # 起點不在，代表這個節點多半掛在別的畫面上、由外面傳進來——
            # 那種情況只能提醒，不能當成錯誤擋 CI。
            #
            # **而且一定要 anchored。** `origin` 是 `module` 的時候，`every` 是整個
            # 模組十幾份不相干 .csd 的名稱聯集；「起點在聯集裡」幾乎不代表什麼，
            # 它很可能出現在另一份完全不同的版面裡。README 一直寫著這種推測只能算
            # warn，但程式碼漏了這個條件——實測 16 筆 error 裡有 12 筆是這樣來的，
            # 而其中 `SlotBottomBarViewGWS` 那組如果真的缺節點，下一行
            # `self.m_extraBetHint.txt = ...` 就會當場炸，遊戲卻跑得好好的
            # ——證明工具對到的不是這個檔案真正載入的那份 .csd。
            rooted = len(chain) > 1 and anchored and chain[0] in every
            # `getChildByName` 回 nil 本身不會當機，對那個 nil 呼叫方法才會。
            # 存進欄位卻整個模組沒人讀（產生出來的 Properties 綁定檔沒跟著更新，
            # .csd 那邊節點已經刪了）就只是死綁定——畫面照樣開，少一個元素而已。
            dead = bool(target) and not _is_dereferenced(path, workspace, target)
            guarded = bool(target) and _is_nil_guarded(path, workspace, target)
            # error 只留給全部條件同時成立的：取用鏈錨定在這份 .csd、結果真的會被
            # 拿去用、**沒有 nil 防護**、而且這個名字全庫都找不到（真的被刪了）。
            certain = (rooted and not blind and not dead and not elsewhere
                       and not guarded)
            findings.append(Finding(
                "NODE_MISSING", ERROR if certain else WARN, path, line,
                f"節點 '{name}' 在 {where} 裡不存在"
                + (f"（取用鏈 {'/'.join(chain)}）" if len(chain) > 1
                   else "；這一行的來源節點不確定，可能是別的畫面傳進來的")
                + (f"；存進 {target} 之後整個模組沒有人讀它，所以不會當機"
                   "——是沒跟著更新的綁定" if dead else "")
                + (f"；{target} 有做 nil 防護，取不到不會當機"
                   "（多半是「取不到就自己建」）" if guarded and not dead else "")
                + ("；這個名字在別的 .csd 裡有，所以更可能是對到了錯的版面"
                   "（先用 explain 確認）" if elsewhere else "")
                + (f"；注意這個模組有 {len(blind)} 個 .lua 讀不到，"
                   "裡面若有 setName 建出這個節點就是誤報" if blind else ""),
                key=f"{'/'.join(chain)}",
                confidence=confidence if certain else "medium",
                detail={"node": name, "chain": chain, "rooted": rooted,
                        "dereferenced": not dead, "existsElsewhere": elsewhere,
                        "nilGuarded": guarded,
                        "csd": [os.path.basename(c) for c in sources["csd"]],
                        **({"assignedTo": target} if target else {}),
                        **({"unreadableInModule": len(blind)} if blind else {})}))
        elif verdict == "parent" and single:
            findings.append(Finding(
                "NODE_WRONG_PARENT", WARN, path, line,
                f"節點 '{name}' 存在，但不在 '{'/'.join(chain[:-1])}' 底下",
                key=f"{'/'.join(chain)}", confidence="medium",
                detail={"node": name, "chain": chain}))

    # 自我校準：對到的 .csd 若跟程式碼取用的節點幾乎沒有交集，那多半不是這個畫面的版面
    # ——有些檔案只是順手載了幾個裝飾用的 .csb，真正的版面節點是外面傳進來的。
    # 這種時候整份報告都不可信，寧可什麼都不報，也不要送一整頁假的「節點不存在」。
    if len(seen) >= 4 and len(missed) / len(seen) > 0.5:
        hit = 1 - len(missed) / len(seen)
        # 「只吻合 0%」讀起來像「這些名字哪裡都沒有」，但實測最常見的情況是
        # **名字全都在**、只有取用鏈的根對不上：`TipHelper.Bind( root )` 這種
        # helper 收外面傳進來的節點，實例名寫在呼叫方的版面裡，於是 6 個名字裡
        # 有 5 個在 VoucherCenterTip.csd，鏈卻一條都對不上。
        # 只講 0% 的話，這個「跳過」等於叫人自己重查一遍；指出名字最齊的那一份，
        # 跳過才是可行動的線索（要補 csbMap 也知道該指到哪）。
        best, covered = None, 0
        for csd, root in zip(root_of, roots):
            found = len(seen & tree_names(root))
            if found > covered:
                best, covered = csd, found
        return [Finding(
            "NODE_CSB_UNRESOLVED", INFO, path, 1,
            f"對到的 {'、'.join(os.path.basename(c) for c in sources['csd'][:3])} "
            f"只吻合 {hit:.0%} 的取用鏈，判定不是這個檔案的版面，節點檢查跳過。"
            + (f"（不過 {os.path.basename(best)} 裡找得到 {covered}/{len(seen)} 個"
               "名字，對不上的是鏈的根——多半是外面傳進來的節點）"
               if covered else ""),
            key="mismatch",
            detail={"origin": sources["origin"], "hitRate": round(hit, 3),
                    "csd": [os.path.basename(c) for c in sources["csd"]],
                    **({"bestNameMatch": os.path.basename(best),
                        "namesFound": covered, "namesUsed": len(seen)}
                       if covered else {})})]
    return findings
