"""生命週期對稱：註冊了的東西有沒有解除。

排程回呼在節點被釋放之後才觸發，摸到已經沒了的 C++ 物件 → EXC_BAD_ACCESS。
這是 CrashReport 裡最穩定出現的一類，而且靜態就看得出來：
**同一個檔案裡註冊了、卻沒有任何一處解除**。

比對範圍限在單一檔案是刻意的。跨檔案追蹤要能判斷「誰的生命週期包住誰」，
在 39 萬行、風格不一的程式碼上做不準；同檔對稱這個規則簡單，但它抓到的
每一筆都值得看——註冊與解除本來就該寫在同一個類別的成對方法裡。

嚴重度看兩件事，都是為了只在「真的會炸」時才擋 CI：

**一、這段程式碼會不會重複執行**

- 寫在 `OnEnter` / `Show` 這種每次開啟都跑一次的方法裡 → 每開一次就多疊一份，是真的錯
- 寫在 `ctor` / `Init` 這種一輩子只跑一次的地方 → 多半是刻意的常駐註冊，只提醒

**二、排程有沒有綁 owner**

專案的 `SafeScheduleScriptFunc( owner, fn, ... )` 會在每次觸發前檢查 owner 是否還是
有效的 C++ 物件，失效就自己解除。所以綁了 owner 的排程漏解除**不會**變成野指標，
只是少了作者自己說的「雙保險」——那是 `warn`，不擋 CI。`owner` 寫 `nil` 就沒有這層
保護，跟原生 `scheduleScriptFunc` 一樣是 `error`。

（靜態分不出 owner 是 C++ userdata 還是純 Lua table。後者在執行期會被 wrapper 擋下、
退回無保護模式並 `LogError`，但這裡一律當成有保護——寧可少報一級。）
"""
from __future__ import annotations

import re

from .. import luasrc
from ..report import ERROR, INFO, WARN, Finding

RULES = {
    "SCHED_LEAK": "排程存進 handle 卻沒有任何一處解除"
                  "（有綁 owner 的 SafeScheduleScriptFunc 只算 warn，wrapper 會自己解除）",
    "SCHED_NO_HANDLE": "排程的回傳值沒有存下來，之後不可能解除",
    "EVENT_LEAK": "事件註冊了，同一個檔案裡找不到對應的解除",
    "LISTENER_LEAK": "用 FixedPriority 掛到 EventDispatcher 的監聽沒有解除"
                     "（SceneGraphPriority 綁在節點上會自動移除，不算）",
}

# 掛到 `cc.EventDispatcher` 的兩種寫法，只有一種需要手動解除：
#
# - `addEventListenerWithSceneGraphPriority( listener, node )`——監聽綁在那個
#   節點上，節點銷毀時 EventDispatcher 自己會移除。不報。
# - `addEventListenerWithFixedPriority( listener, priority )`——沒有節點綁定，
#   不手動 `removeEventListener` 就會一直收事件，回呼摸到已經沒了的東西。
#
# 實測 Inanna：SceneGraphPriority 25 處（16 處沒解除，但那是對的），
# FixedPriority 8 處。這兩個名字前綴相同，所以 `addEventListener` 那條規則的
# 正則（`\baddEventListener\s*\(`）本來一處都對不到——這一塊從來沒被檢查過。
_DISPATCH_FIXED = re.compile(r"\baddEventListenerWithFixedPriority\s*\(")
_DISPATCH_SCENE = re.compile(r"\baddEventListenerWithSceneGraphPriority\s*\(")
_DISPATCH_REMOVE = ("removeEventListener", "removeAllEventListeners",
                    "removeEventListenersForTarget")

# `cc.EventDispatcher` 一共有五種解除方式，而且**不是每一種都拿 listener 物件**：
#
#   removeEventListener( listener )              ← 物件
#   removeEventListenersForTarget( node )        ← 節點
#   removeAllEventListeners()                    ← 全部
#   removeEventListenersForType( type )          ← 整個型別
#   removeCustomEventListeners( 事件名 )          ← **事件名**
#
# 最後一種是 `cc.EventListenerCustom` 的正規解法：靠事件名，根本不需要留著
# listener 的參考。漏掉它的代價是實測全庫 5 筆 error 全部是誤報，而且訊息還
# 斷言「已經不可能解除」——把完全正確的程式碼指成必須重寫。
# （教訓：加規則前要把「解除」的**所有**寫法查齊，光看專案裡出現過的不夠。
#  這份清單是從 CCEventDispatcher.h 抄的。）
_DISPATCH_CUSTOM_REMOVE = "removeCustomEventListeners"
_DISPATCH_TYPE_REMOVE = "removeEventListenersForType"

# `listener = cc.EventListenerCustom:create( 事件名, 處理函式 )`
_CUSTOM_CREATE = re.compile(
    r"([A-Za-z_][\w.]*)\s*=\s*cc\.EventListenerCustom\s*[:.]\s*create\s*\(")

_TOSTRING = re.compile(r"^tostring\s*\((.*)\)$", re.DOTALL)


def _event_key(expr: str, groups: dict) -> str:
    """把事件名收斂成可比對的形式。

    `ModelLocator` 註冊時寫 `tostring(eventName)`、解除時寫 `eventName`，
    兩邊指的是同一個事件；`tostring` 這層包裝要剝掉才對得上。
    """
    text = (expr or "").strip()
    while True:
        stripped = _TOSTRING.match(text)
        if not stripped:
            break
        text = stripped.group(1).strip()
    return _same_object(text, groups)


# 每次開啟都會跑一次的方法：在這裡註冊而不解除，開幾次就疊幾份
_ENTER = {"onenter", "show", "onshow", "open", "start", "onstart",
          "onenable", "enter", "refresh", "onopen"}
# 一輩子只跑一次的地方：常駐註冊多半是刻意的
_ONCE = {"ctor", "init", "initialize", "oncreate", "new", "create", "setup"}

_SCHEDULE = re.compile(r"\b(SafeScheduleScriptFunc|scheduleScriptFunc)\s*\(")
_UNSCHEDULE = re.compile(r"\b(?:SafeUnschedule|unscheduleScriptEntry)\s*\(")

# (註冊函式, 解除函式們, 用第幾個引數當比對的鍵)
_EVENT_PAIRS = (
    ("AddListener", ("RemoveListener", "RemoveAllListener", "RemoveAllListeners"), 0),
    ("RegisterEvent", ("UnRegisterEvent", "UnregisterEvent", "UnRegisterEvents",
                       "UnregisterEvents", "RemoveEvent"), 0),
    ("addEventListener", ("removeEventListener", "removeAllEventListeners"), 0),
)

# `addEventListener` 是**兩個同名但完全不同的 API**，只有前者需要解除：
#
# - `dispatcher:addEventListener( listener, node )`——cc.EventDispatcher。
#   要用 `removeEventListener( listener )` 解除，專案裡確實有 11 處在做。
# - `widget:addEventListener( callback )`——ccui.Widget。Cocos 的 Lua 綁定
#   **沒有** widget 版的 removeEventListener；callback 存在 widget 上，
#   節點銷毀就跟著走，沒有東西要解除。
#
# 不分開的話，專案裡約 30 處 widget 監聽全部是誤報。判斷依據是接收者的名字：
# `dispatcher` / `eventDispatcher` / `...getEventDispatcher()` 這一類。
#
# 尾端要容許 `:`、`()`、`.`：`expr_before` 回的是「方法名之前那一段」，
# 而分隔符也算在裡面（`dispatcher:`、`getEventDispatcher():`）。忘了這件事的話
# 每一處 addEventListener 都會被當成 widget 而放過——26 個誤報換成 24 個漏報。
_DISPATCHER = re.compile(r"(?i)dispatcher\s*(?:\(\s*\))?[:.]?\s*$")


def _needs_release(register: str, code: str, start: int) -> bool:
    """這一處註冊真的有「要解除」這回事嗎。"""
    if register != "addEventListener":
        return True
    return bool(_DISPATCHER.search(luasrc.expr_before(code, start)))


def _method_kind(func: "str | None") -> str:
    """這個函式屬於「重複進入」、「只跑一次」還是其他。"""
    if not func:
        return "toplevel"
    tail = re.split(r"[.:]", func)[-1].lower()
    if tail in _ENTER:
        return "enter"
    if tail in _ONCE:
        return "once"
    return "other"


# `X = nil` 是**清除**，不是「X 跟 nil 是同一個物件」。不排除的話，同一個檔案裡
# 每個被 `= nil` 清掉的東西都會被併進同一類，解除其中一個就等於解除全部——
# 實測全庫有 529 個檔案會這樣併，是貨真價實的漏報來源。
# （數字與字串不會被下面的識別字正則吃到，所以只要擋這三個關鍵字。）
_LITERAL = r"(?!(?:nil|true|false)[ \t]*$)"

_TABLE_ALIAS = re.compile(
    r"^[ \t]*(?:local[ \t]+)?([A-Za-z_]\w*)[ \t]*=[ \t]*"
    + _LITERAL + r"([A-Za-z_][\w.]*)[ \t]*$",
    re.MULTILINE)


def _aliases(code: str, spans: "list | None" = None) -> dict:
    """`local t = self.m_timers` 這類別名：`t` → `self.m_timers`。

    給 `_normalise_handle` 把兩邊的寫法收斂到同一個名字用。
    """
    found: dict = {}
    for match in luasrc.outside_strings(_TABLE_ALIAS, code, spans):
        target, source = match.group(1), match.group(2)
        if target != source:
            found.setdefault(target, source)
    return found


_SAME_OBJECT = re.compile(
    r"^[ \t]*(?:local[ \t]+)?([A-Za-z_][\w.]*)[ \t]*=[ \t]*"
    + _LITERAL + r"([A-Za-z_][\w.]*)[ \t]*$",
    re.MULTILINE)


def _equivalence(code: str, spans: "list | None" = None) -> dict:
    """`X = Y` 代表兩個名字指同一個東西——回傳「名字 → 代表元」。

    跟 `_aliases` 不同的是這裡是**雙向**的。監聽的註冊與解除方向剛好相反：

        local touchListener = cc.EventListenerTouchOneByOne:create()
        dispatcher:addEventListenerWithFixedPriority( touchListener, -1 )
        self.m_pageViewLoopTouchListener = touchListener      -- 欄位 = local
        ...
        dispatcher:removeEventListener( self.m_pageViewLoopTouchListener )

    註冊寫 local、解除寫欄位，中間靠 `欄位 = local` 連起來。`_aliases` 的
    target 只收裸名字（`local t = self.m_timers` 那種），對不到這個方向。

    合併過度只會讓我們**以為已經解除**（漏報），不會冤枉正確的程式碼——
    對這個工具來說那是安全的方向。但「安全」不等於「可以不管」：
    `= nil` 被當成等價的時候，一個檔案裡所有被清空的東西會全部併成一類
    （`PlayInfo.lua` 就把 `touchBeganPos`、`touchListener`、
    `self.m_pageViewLoopTouchListener` 併在一起），漏報範圍會大到規則失效。
    所以 `nil` / `true` / `false` 在 `_LITERAL` 就先排除掉。
    """
    parent: dict = {}

    def find(name: str) -> str:
        seen: list = []
        while parent.get(name, name) != name:
            seen.append(name)
            name = parent[name]
        for item in seen:
            parent[item] = name
        return name

    for match in luasrc.outside_strings(_SAME_OBJECT, code, spans):
        left, right = match.group(1), match.group(2)
        if left == right:
            continue
        parent.setdefault(left, left)
        parent.setdefault(right, right)
        parent[find(left)] = find(right)
    return {name: find(name) for name in parent}


def _same_object(name: str, groups: dict) -> str:
    return groups.get(name.strip(), name.strip())


def _canonical(name: str, aliases: dict) -> str:
    name = name.strip()
    seen: set = set()
    while name in aliases and name not in seen:
        seen.add(name)
        name = aliases[name]
    return name


def _normalise_handle(handle: str, aliases: "dict | None" = None) -> str:
    """把 handle 收斂成可比對的形式。

    存進表格的 handle 兩邊常常寫得不一樣：註冊寫 `self.m_timers[ name ]`，
    解除時先 `local t = self.m_timers` 再寫 `t[ name ]`。字面比對會判成漏解除，
    但那是完全正確的程式碼。

    收斂的方式是把**表名經過別名解析**之後再接上索引，而不是把表名整個丟掉。
    原本只留索引部分的話，`self.m_tickTimer[k]` 與 `self.m_flyTimer[k]` 會變成
    同一個 handle，解除其中一個就把另一個的洩漏一起消掉了。
    """
    handle = handle.strip()
    aliases = aliases or {}
    bracket = handle.find("[")
    if bracket < 0:
        return _canonical(handle, aliases)
    base = _canonical(handle[:bracket], aliases)
    return base + handle[bracket:].replace(" ", "")


def _released_handles(code: str, aliases: dict, spans: "list | None" = None) -> set:
    found: set = set()
    for match in luasrc.outside_strings(_UNSCHEDULE, code, spans):
        args = luasrc.split_args(luasrc.call_args(code, code.index("(", match.end() - 1)))
        if args and args[0].strip():
            found.add(_normalise_handle(args[0], aliases))
    return found


def _call_pattern(name: str):
    return re.compile(r"\b" + re.escape(name) + r"\s*\(")


def _release_keys(code: str, functions: tuple, spans: "list | None" = None) -> set:
    """收集解除呼叫用到的鍵（第一個引數的字面文字）。

    比對用原始文字而不是解析後的值：事件型別多半是 `inn.Events.XXX` 這種常數，
    兩邊寫法一致就對得上；真的寫得不一致時本來就該人工看一眼。
    """
    keys: set = set()
    for name in functions:
        if name not in code:
            continue          # 便宜的子字串先篩，省掉一次全文正則掃描
        for match in luasrc.outside_strings(_call_pattern(name), code, spans):
            args = luasrc.split_args(luasrc.call_args(code, code.index("(", match.end() - 1)))
            keys.add(args[0].strip() if args else "")
            if not args or not args[0].strip():
                keys.add("*")          # RemoveAllListeners() 之類，視為全部解除
    return keys


def _owner_protected(code: str, match) -> bool:
    """`SafeScheduleScriptFunc( owner, ... )` 有沒有綁到 owner。

    綁了 owner 的話，wrapper 每次觸發前會檢查 owner 是否還活著，失效就自己解除
    ——也就是這條規則要防的野指標**不會發生**，漏解除只是少了雙保險。
    `owner` 寫 nil（或整個沒給）就沒有這層保護，跟原生排程一樣危險。

    靜態看不出 owner 是 C++ userdata 還是純 Lua table（後者在執行期會被 wrapper
    擋掉、退回無保護模式並 LogError）。這裡一律當成有保護，寧可少報一級。
    """
    if match.group(1) != "SafeScheduleScriptFunc":
        return False
    args = luasrc.split_args(luasrc.call_args(code, code.index("(", match.end() - 1)))
    return bool(args) and args[0].strip() not in ("", "nil")


def check(path: str, text: str, workspace, code: "str | None" = None,
          spans: "list | None" = None) -> list:
    if code is None:
        code, spans = luasrc.scan(text)
    findings: list = []

    # 函式位置表一個檔案只建一次、而且只有真的要用時才建：沒有排程也沒有事件註冊
    # 的檔案佔多數，那就完全不必掃（原本排程段與事件段各建一次，1300 個檔案）。
    functions = None

    def function_table():
        nonlocal functions
        if functions is None:
            functions = luasrc.function_index(code, spans)
        return functions

    # ---------------------------------------------------------- 排程
    # 用正則而不是 `"scheduleScriptFunc" in code`：後者大小寫敏感，對
    # `SafeScheduleScriptFunc`（大寫 S）不成立，於是只用 Safe 包裝的檔案
    # 整條排程規則會被靜默跳過——正好是專案裡建議的寫法。
    if _SCHEDULE.search(code):
        aliases = _aliases(code, spans)
        released = _released_handles(code, aliases, spans)
        for match in luasrc.outside_strings(_SCHEDULE, code, spans):
            start = match.start()
            handle = luasrc.assign_target(code, start)
            line = luasrc.line_of(text, start)
            func = luasrc.enclosing_function(code, start, function_table())
            kind = _method_kind(func)
            where = f"（在 {func}）" if func else ""
            protected = _owner_protected(code, match)

            if not handle:
                # 回傳值沒接住。SafeScheduleScriptFunc 會把 entry 傳給回呼，
                # 一次性邏輯可以自己解除，所以回呼裡出現 unschedule 就不算問題。
                if not released:
                    findings.append(Finding(
                        "SCHED_NO_HANDLE",
                        WARN if protected or kind != "enter" else ERROR,
                        path, line,
                        f"排程沒有把回傳的 entry 存下來，之後無法解除{where}"
                        + ("；owner 有綁，wrapper 會在 owner 失效時自己解除，"
                           "但這裡仍然收不回來" if protected else ""),
                        key=f"nohandle@{func or 'toplevel'}",
                        confidence="medium" if protected else "high",
                        detail={"function": func, "context": kind,
                                "ownerProtected": protected}))
                continue

            if _normalise_handle(handle, aliases) in released:
                continue
            if protected:
                # 綁了 owner：不是野指標，是少了雙保險。不擋 CI。
                findings.append(Finding(
                    "SCHED_LEAK", WARN, path, line,
                    f"排程存進 {handle}，整個檔案找不到對應的 SafeUnschedule"
                    f"{where}；owner 有綁，所以 owner 失效時 wrapper 會自己解除"
                    "——但在那之前排程照跑，該解的地方還是要解",
                    key=handle, confidence="medium",
                    detail={"handle": handle, "function": func, "context": kind,
                            "ownerProtected": True}))
                continue
            findings.append(Finding(
                "SCHED_LEAK",
                ERROR if kind in ("enter", "other", "toplevel") else WARN,
                path, line,
                f"排程存進 {handle}，整個檔案找不到對應的 "
                f"SafeUnschedule / unscheduleScriptEntry{where}",
                key=handle,
                detail={"handle": handle, "function": func, "context": kind,
                        "ownerProtected": False}))

    # ------------------------------------------- EventDispatcher（FixedPriority）
    if _DISPATCH_FIXED.search(code):
        groups = _equivalence(code, spans)
        released: set = set()
        for name in _DISPATCH_REMOVE:
            if name not in code:
                continue
            for match in luasrc.outside_strings(_call_pattern(name), code, spans):
                args = luasrc.split_args(
                    luasrc.call_args(code, code.index("(", match.end() - 1)))
                first = (args[0] if args else "").strip()
                # 無引數的 removeAllEventListeners 等於全部解除
                released.add(_same_object(first, groups) if first else "*")

        # 依事件名解除的那一種：記的是事件名，不是 listener 物件
        released_events: set = set()
        if _DISPATCH_CUSTOM_REMOVE in code:
            for match in luasrc.outside_strings(
                    _call_pattern(_DISPATCH_CUSTOM_REMOVE), code, spans):
                args = luasrc.split_args(
                    luasrc.call_args(code, code.index("(", match.end() - 1)))
                first = (args[0] if args else "").strip()
                released_events.add(_event_key(first, groups) if first else "*")
        # 整個型別一次解除——靜態比不出型別，一律當成全部解除（寧可少報）
        if _DISPATCH_TYPE_REMOVE in code:
            released.add("*")

        # listener 變數 → 它監聽的事件名
        custom_event: dict = {}
        for match in luasrc.outside_strings(_CUSTOM_CREATE, code, spans):
            args = luasrc.split_args(
                luasrc.call_args(code, code.index("(", match.end() - 1)))
            if args:
                custom_event[_same_object(match.group(1), groups)] = \
                    _event_key(args[0], groups)
        for match in luasrc.outside_strings(_DISPATCH_FIXED, code, spans):
            start = match.start()
            args = luasrc.split_args(
                luasrc.call_args(code, code.index("(", match.end() - 1)))
            listener = (args[0] if args else "").strip()
            if not listener or listener.startswith("function"):
                continue          # 解不出可比對的名字就不猜
            if "*" in released or _same_object(listener, groups) in released:
                continue
            # 自訂事件用事件名解除，不需要 listener 物件的參考
            event = custom_event.get(_same_object(listener, groups))
            if event is not None and (
                    "*" in released_events or event in released_events):
                continue
            line = luasrc.line_of(text, start)
            func = luasrc.enclosing_function(code, start, function_table())
            kind = _method_kind(func)
            findings.append(Finding(
                "LISTENER_LEAK",
                ERROR if kind in ("enter", "other", "toplevel") else WARN,
                path, line,
                f"監聽 {listener} 用 FixedPriority 掛到 EventDispatcher"
                + (f"（在 {func}）" if func else "")
                + "；FixedPriority 沒有節點綁定，不手動解除就會一直收事件"
                + (f"。這個檔案裡找不到 removeCustomEventListeners( {event} )"
                   if event is not None else
                   "，整個檔案找不到對應的 removeEventListener"),
                key=f"fixed:{listener}",
                detail={"listener": listener, "function": func, "context": kind,
                        "priority": "fixed",
                        **({"customEvent": event} if event is not None else {})}))

    # ---------------------------------------------------------- 事件
    for register, releases, key_index in _EVENT_PAIRS:
        if register not in code:
            continue
        keys = _release_keys(code, releases, spans)
        if "*" in keys:
            continue
        for match in luasrc.outside_strings(_call_pattern(register), code, spans):
            start = match.start()
            # 定義本身不算註冊（`function EventManager:AddListener(...)`）
            head = code[max(0, start - 40):start]
            if re.search(r"\bfunction\b[\s\w.:]*$", head):
                continue
            if not _needs_release(register, code, start):
                continue          # widget 的監聽沒有「解除」這回事
            args = luasrc.split_args(luasrc.call_args(code, code.index("(", match.end() - 1)))
            key = args[key_index].strip() if len(args) > key_index else ""
            if not key or key in keys:
                continue
            # 引數是 inline function 的話，那不是可以拿來比對的「鍵」——
            # 解除端寫的一定是別的東西（存起來的 listener 物件）。硬用它當鍵，
            # 訊息與指紋會塞進整段函式，而且永遠對不上任何解除呼叫。
            if key.startswith("function") or "\n" in key:
                continue
            func = luasrc.enclosing_function(code, start, function_table())
            kind = _method_kind(func)
            if kind == "once":
                severity = INFO       # 一輩子註冊一次，多半是刻意的常駐
            elif kind == "enter":
                severity = WARN
            else:
                severity = INFO
            findings.append(Finding(
                "EVENT_LEAK", severity, path,
                luasrc.line_of(text, start),
                f"{register}( {key} ) 在這個檔案裡找不到對應的 "
                f"{releases[0]}" + (f"（在 {func}）" if func else ""),
                key=f"{register}:{key}",
                confidence="high" if kind == "enter" else "medium",
                detail={"event": key, "function": func, "context": kind,
                        "expects": list(releases)}))
    return findings


def stats(text: str, code: "str | None" = None,
          spans: "list | None" = None) -> dict:
    """給總結報告用的數字，不是問題本身。

    `\\bscheduleScriptFunc` 大小寫敏感，本來就不會對到 `SafeScheduleScriptFunc`
    （大寫 S），所以原生的數量直接數就是了——原本又減一次 safe，等於把 safe
    扣了兩遍（1 raw + 3 safe 會回報 raw=0）。

    兩個數字用**同一個** `_SCHEDULE` 走一遍分組數，不是各掃一次全文：原本每個
    檔案多掃兩遍，1300 個檔案加起來是實測 0.3 秒，而這只是報告最後那一行的數字。
    """
    if code is None:
        code, spans = luasrc.scan(text)
    # 兩種寫法都含 "cheduleScriptFunc"；子字串測試是 C 層的 find，
    # 比正則掃便宜得多，而 1300 個檔案裡只有 126 個有排程
    if "cheduleScriptFunc" not in code:
        return {"scheduleRaw": 0, "scheduleSafe": 0}
    safe = 0
    total = 0
    for match in luasrc.outside_strings(_SCHEDULE, code, spans):
        total += 1
        if match.group(1) == "SafeScheduleScriptFunc":
            safe += 1
    return {"scheduleRaw": total - safe, "scheduleSafe": safe}
