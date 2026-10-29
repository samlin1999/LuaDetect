"""Lua 原始碼的輕量解析——只做這個工具需要的那幾件事，不寫完整剖析器。

為什麼不用現成的 Lua parser：需要的資訊很窄（常數表、`getChildByName` 的接收者與
引數、註冊/解除成對呼叫），但要處理的是 39 萬行**風格不統一**的既有程式碼。
完整剖析器遇到任何寫法不合它的預期就整檔失敗，反而比逐字掃描脆弱。

一個貫穿的原則：**位移要對得上原檔**。剝掉註解時用空白覆蓋、不刪字元，
行號與位移才能直接拿去回報，不必再做一次映射。
"""
from __future__ import annotations

import bisect
import re

# ---------------------------------------------------------------- 掃描器

_LONG_OPEN = re.compile(r"\[(=*)\[")


def _long_bracket_end(text: str, start: int, level: int) -> int:
    """回傳長括號 `]==]` 結尾之後的位移；找不到收尾時回傳 len(text)。"""
    closing = "]" + "=" * level + "]"
    found = text.find(closing, start)
    return len(text) if found < 0 else found + len(closing)


# 只跳到真正需要處理的位置。`[` 要連著第二個 `[`（中間可以有 `=`）才是長括號——
# 不這樣寫的話 `t[ i ]` 這種索引也會被跳過去看一眼，實測是 11.7 萬次無謂的
# Python 迴圈加 11.7 萬次 _LONG_OPEN.match。
_INTERESTING = re.compile(r"--|['\"]|\[=*\[")
_NOT_NEWLINE = re.compile(r"[^\n]")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def scan(text: str) -> tuple:
    """一次掃過原始碼，回傳 (註解已抹白的程式碼, 字串區間清單)。

    字串**保留原樣**：節點名稱與 .csb 路徑都藏在字串裡，抹掉就沒東西可查了。
    要判斷某個位移是不是在字串內，用 `in_string` 查回傳的區間清單。

    區間清單每一項是 `(起, 迄, 值)`，`值` 是去掉引號後的內容（長字串不處理轉義，
    短字串處理 `\\n` 這類常見轉義；節點名稱不會用到轉義，夠用）。

    **位移一定要對得上原檔**，所以抹註解是用同樣長度的空白覆蓋、不刪字元；
    輸出用片段拼回去（原本是 `list(text)` 逐字改再 join，1200 萬次 Python 迴圈）。
    絕大多數字元都是普通程式碼，用正則直接跳到下一個「有意義的位置」
    （`--`、引號、`[`），不要一個字一個字前進——這裡是整個掃描最貴的一步。
    """
    parts: list = []          # 片段長度總和必須等於 len(text)
    spans: list = []
    i = 0
    size = len(text)
    while i < size:
        found = _INTERESTING.search(text, i)
        if found is None:
            parts.append(text[i:])
            break
        start = found.start()
        if start > i:
            parts.append(text[i:start])
            i = start
        ch = text[i]

        # 註解：--[[ 區塊 ]] 或 -- 到行尾
        if ch == "-":
            match = _LONG_OPEN.match(text, i + 2)
            if match:
                end = min(_long_bracket_end(text, match.end(),
                                            len(match.group(1))), size)
                # 換行要留著，行號才不會跑掉
                parts.append(_NOT_NEWLINE.sub(" ", text[i:end]))
                i = end
                continue
            end = text.find("\n", i)
            end = size if end < 0 else end
            parts.append(" " * (end - i))
            i = end
            continue

        # 短字串
        if ch != "[":
            close = text.find(ch, i + 1)
            newline = text.find("\n", i + 1)
            if (close >= 0 and (newline < 0 or close < newline)
                    and "\\" not in text[i + 1:close]):
                # 快路徑：沒有轉義的字串（絕大多數），值就是引號之間那一段。
                # 慢路徑要逐字複製，1300 個檔案加起來是上百萬次 list.append。
                j = close + 1
                spans.append((i, j, text[i + 1:close]))
            else:
                j = i + 1
                buf: list = []
                while j < size:
                    cur = text[j]
                    if cur == "\\" and j + 1 < size:
                        nxt = text[j + 1]
                        buf.append(_ESCAPES.get(nxt, nxt))
                        j += 2
                        continue
                    if cur == ch:
                        j += 1
                        break
                    if cur == "\n":      # 未收尾的字串，當場停住不要吃掉整份檔案
                        break
                    buf.append(cur)
                    j += 1
                spans.append((i, j, "".join(buf)))
            parts.append(text[i:j])
            i = j
            continue

        # 長字串（_INTERESTING 已經確認過形狀，這裡一定對得上）
        match = _LONG_OPEN.match(text, i)
        level = len(match.group(1))
        end = _long_bracket_end(text, match.end(), level)
        spans.append((i, end, text[match.end():max(match.end(), end - (level + 2))]))
        parts.append(text[i:end])
        i = end
    return "".join(parts), spans


def in_string(spans: list, index: int) -> bool:
    """`index` 落在某個字串字面值裡面嗎。

    字串**不會**被抹白（節點名稱與 .csb 路徑都藏在裡面），所以結構性的正則會匹配到
    字串內的內容。實測全庫：`local X = {` 這種在字串裡命中 7 次、`X = "..."` 125 次，
    足以造出錯誤的常數對照，再由 `resolve_literal` 變成錯誤的節點名 → 誤報。

    spans 是依位移排好的，用二分；一個檔案要查上千次，線性掃會變成熱點。
    """
    if not spans:
        return False
    position = bisect.bisect_right(spans, (index, float("inf"))) - 1
    return position >= 0 and spans[position][0] <= index < spans[position][1]


def outside_strings(pattern, code: str, spans: "list | None" = None):
    """`pattern.finditer(code)`，但跳過起點落在字串裡的匹配。

    `finditer` 的結果本來就是遞增的，而 spans 也是——所以用**一次前進掃描**對齊
    兩邊，不必每個匹配都去二分。全庫掃描會呼叫這裡 8.6 萬次，用 `in_string` 逐個
    查是實測 2.07 秒（比剝註解還貴）；前進掃描把它壓到零點幾秒。
    """
    if not spans:
        return list(pattern.finditer(code))
    out: list = []
    index = 0
    total = len(spans)
    for match in pattern.finditer(code):
        start = match.start()
        # spans 不重疊且遞增：把游標推到第一個「結尾在 start 之後」的區間
        while index < total and spans[index][1] <= start:
            index += 1
        if index < total and spans[index][0] <= start:
            continue                   # 落在字串裡
        out.append(match)
    return out


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


# ---------------------------------------------------------------- 常數

# 引號裡不可以有換行。Lua 的短字串本來就不能跨行，允許換行的話一個沒收尾的引號
# 會把後面幾十行程式碼收成「值」，再被 resolve_literal 當成節點名字送出去；
# 在合成的 _CONST_DECL 裡更糟——貪婪吃過去會讓後面真正的宣告整段被跳過。
# 後面接 `..` 的不算字面常數：`local f = "%0" .. n .. "d"` 的值是組出來的，
# 只記第一段會變成一個假的字面值，再被當成節點名字送出去。
_QUOTED = r"""['"]([^'"\n]*)['"](?!\s*\.\.)"""
_STR_ITEM = re.compile(
    r"""(?:\[\s*['"](?P<bkey>[^'"\n]+)['"]\s*\]|(?P<key>[A-Za-z_]\w*))"""
    r"""\s*=\s*['"](?P<val>[^'"\n]*)['"]""")
# 三種「名字 = 右邊」合成一個：group 2 = `{`（表）、group 3 = 字串值、
# group 4 = 別名的來源。右邊的第一個字元互斥，所以分支不會互相搶。
# 拆開寫三個 pattern 的話，這個不便宜的共同前綴會被掃三遍（實測 1.07 秒）。
_CONST_DECL = re.compile(
    r"\b(?:local\s+)?([A-Za-z_][\w.]*)\s*=\s*"
    r"(?:(\{)|" + _QUOTED + r"|([A-Za-z_][\w.]*)\s*$)",
    re.MULTILINE)


def _match_brace(text: str, open_at: int) -> int:
    """從 `{` 的位移找出對應的 `}`（傳入的 text 必須是已抹白註解的程式碼）。"""
    depth = 0
    i = open_at
    size = len(text)
    while i < size:
        ch = text[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < size and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return size


def constants(code: str, spans: "list | None" = None) -> dict:
    """收集檔案裡「名字 → 字串」的對照，供解析 `NodeNames.BG` 這種引數。

    收三種東西：字串常數表（`local NodeNames = { BG = "bg" }`）、
    單獨的字串常數（`local CSBFile = "a/b.csb"`）、以及表的別名
    （`MainViewProperties.NodeNames = NodeNames`——Properties 檔都會這樣往外露）。

    回傳 `{"tables": {表名: {鍵: 值}}, "strings": {名字: 值}}`。

    三種都是「名字 = 右邊」的形狀，所以用**一個**正則掃一遍、看哪個群組有值來分，
    不是三個幾乎一樣的前綴各掃一遍全文（實測那樣是 1.07 秒，整個掃描最貴的一項）。
    右邊的第一個字元互斥（`{` / 引號 / 識別字），所以分支不會互相搶。
    """
    tables: dict = {}
    strings: dict = {}
    pending: list = []          # 別名要等表全部收完才處理，見下
    for match in outside_strings(_CONST_DECL, code, spans):
        name = match.group(1)
        if match.group(2):                      # `name = {`：字串常數表
            open_at = match.start(2)
            end = _match_brace(code, open_at)
            items = {m.group("key") or m.group("bkey"): m.group("val")
                     for m in _STR_ITEM.finditer(code[open_at:end])}
            if items:
                # 同名表出現兩次時合併，不覆蓋：既有程式碼有把表拆成兩段寫的
                tables.setdefault(name, {}).update(items)
        elif match.group(3) is not None:        # `name = "字串"`
            strings.setdefault(name, match.group(3))
        elif match.group(4):                    # `name = 別的名字`（整行就這樣）
            pending.append((name, match.group(4)))

    # 別名一定要在表收完之後才對——別名寫在表定義之前是常有的事
    for target, source in pending:
        if source in tables and target not in tables:
            tables[target] = tables[source]
        # 別名的尾段也要認得：`self.NodeNames` / `M.NodeNames` 都指同一張表
        tail = source.rsplit(".", 1)[-1]
        if tail in tables and target not in tables:
            tables[target] = tables[tail]

    return {"tables": tables, "strings": strings}


def resolve_literal(expr: str, consts: dict) -> "str | None":
    """把引數運算式解成字面字串；解不出來回傳 None（動態組出來的名字就跳過）。"""
    expr = (expr or "").strip()
    if not expr:
        return None
    if len(expr) >= 2 and expr[0] == expr[-1] and expr[0] in ("'", '"'):
        return expr[1:-1]

    tables = consts["tables"]
    strings = consts["strings"]

    # NodeNames["KEY"] / self.NodeNames["KEY"]
    bracket = re.match(r"^([A-Za-z_][\w.]*)\s*\[\s*['\"]([^'\"]+)['\"]\s*\]$", expr)
    if bracket:
        return _lookup(tables, bracket.group(1), bracket.group(2))

    # NodeNames.KEY / self.NodeNames.KEY / A.B.NodeNames.KEY
    dotted = re.match(r"^([A-Za-z_][\w.]*)\.([A-Za-z_]\w*)$", expr)
    if dotted:
        found = _lookup(tables, dotted.group(1), dotted.group(2))
        if found is not None:
            return found
        return strings.get(expr)

    if re.match(r"^[A-Za-z_]\w*$", expr):
        return strings.get(expr)
    return None


def _lookup(tables: dict, table_expr: str, key: str) -> "str | None":
    """查表；表名寫成 `self.NodeNames` 這種前綴時退回用尾段找。"""
    for name in (table_expr, table_expr.rsplit(".", 1)[-1]):
        table = tables.get(name)
        if table and key in table:
            return table[key]
    return None


# ---------------------------------------------------------------- 運算式

_RECV_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:")


def expr_before(code: str, index: int) -> str:
    """取出 `index` 之前緊接著的後綴運算式，也就是 `a:b()` 裡的接收者。

    往回走而不是往前剖析：接收者可以是 `self.m_bg`、`view`、`self:GetView()`、
    `list[ i ]`，共通點是都以識別字或收尾括號結束。往回收字元遇到分隔符就停，
    比正則列舉所有寫法可靠。
    """
    i = index
    while i > 0 and code[i - 1] in " \t":
        i -= 1
    end = i
    while i > 0:
        ch = code[i - 1]
        if ch in _RECV_CHARS:
            i -= 1
            continue
        if ch in (")", "]"):
            opener = "(" if ch == ")" else "["
            depth = 0
            j = i - 1
            while j >= 0:
                if code[j] == ch:
                    depth += 1
                elif code[j] == opener:
                    depth -= 1
                    if depth == 0:
                        break
                j -= 1
            if j < 0:
                break
            i = j
            continue
        break
    return code[i:end].strip()


def call_args(code: str, open_paren: int) -> str:
    """回傳 `(` 到對應 `)` 之間的內容。"""
    depth = 0
    i = open_paren
    size = len(code)
    while i < size:
        ch = code[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < size and code[j] != ch:
                j += 2 if code[j] == "\\" else 1
            i = j + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return code[open_paren + 1:i]
        i += 1
    return code[open_paren + 1:]


def split_args(text: str) -> list:
    """依最外層逗號切引數。"""
    parts: list = []
    depth = 0
    current: list = []
    i = 0
    size = len(text)
    while i < size:
        ch = text[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < size and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            current.append(text[i:min(j + 1, size)])
            i = j + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail or parts:
        parts.append(tail)
    return parts


_ASSIGN_TARGET = re.compile(
    r"^[ \t]*(?:local[ \t]+)?([A-Za-z_][\w.]*(?:\[[^\]]*\])?)[ \t]*=[ \t]*(?![=~])")


def assign_target(code: str, index: int) -> "str | None":
    """`index` 這個呼叫的結果是不是被指派給某個變數？是的話回傳目標名稱。

    以**整行**為單位判斷，不是只看緊鄰的字元。呼叫常常夾在一長串取值中間
    （`self.m_entry = cc.Director:getInstance():getScheduler():scheduleScriptFunc(`），
    只往回看幾個字元的話，等號早就被中間那串蓋過去了，handle 就抓不到——
    抓不到 handle 等於整條「排程沒解除」的規則失效。
    """
    start = code.rfind("\n", 0, index) + 1
    match = _ASSIGN_TARGET.match(code[start:index])
    if not match:
        return None
    # 呼叫要是等號右邊的**主體**，不能是包在別人引數裡、或包在建構式裡的那一個。
    # 三種括號都要算：`local a = foo( bar:getChildByName( "x" ) )` 指派給 a 的是
    # foo 的結果；`self.m_pack = { timer = ...:scheduleScriptFunc( ... ) }` 指派給
    # self.m_pack 的是那張表，不是 handle。只算圓括號會把後者誤判成 handle，
    # 於是解除寫 self.m_pack.timer 也對不上，變成一筆假的「排程沒解除」。
    between = code[start + match.end():index]
    for opener, closer in (("(", ")"), ("{", "}"), ("[", "]")):
        if between.count(opener) != between.count(closer):
            return None
    return match.group(1)


_FUNC_DECL = re.compile(r"\bfunction\s+([\w.:]+)\s*\(")


def function_index(code: str, spans: "list | None" = None) -> tuple:
    """整檔的函式宣告位置表 `(位移串, 名稱串)`，供 `enclosing_function` 二分查詢。

    一個檔案幾百個呼叫點是常態，每個都重掃一次前綴是 O(n·m)——實測 90KB、
    300 個呼叫點就要 0.29 秒。掃一次建表，之後每次查詢是 O(log n)。
    """
    offsets: list = []
    names: list = []
    for match in outside_strings(_FUNC_DECL, code, spans):
        offsets.append(match.start())
        names.append(match.group(1))
    return tuple(offsets), tuple(names)


def enclosing_function(code: str, index: int,
                       table: "tuple | None" = None) -> "str | None":
    """回報 `index` 落在哪個函式裡（取最後一個在它前面的 `function` 宣告）。

    同一份 `code` 要查很多次時，先用 `function_index` 建表再傳進來。
    """
    offsets, names = table if table is not None else function_index(code)
    position = bisect.bisect_left(offsets, index)
    return names[position - 1] if position else None
