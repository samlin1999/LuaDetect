"""共用資源契約：Lua 裡寫的資源路徑，磁碟上真的有那個檔案嗎。

跟節點契約是同一類問題、同一種證據：字串當鍵，編譯期沒有保護。圖檔被刪掉、
路徑打錯，要到玩家開到那個畫面才發現圖沒出來，或者 `create()` 回 nil 當場炸掉。

**只報一種情況：所在資料夾解析得到、檔案卻不在裡面。** 這個前提是這條規則能不能
被留著用的關鍵——專案裡的字面路徑一大半相對於執行期的資源搜尋路徑，或屬於根本
不在這台機器上的別的資源包。拿「檔案找不到」直接當問題實測 8.67% 誤報；
加上這個前提之後是 1.18%，而且剩下的每一筆都驗證過是真的缺檔。

為了不冤枉正確的程式碼，這些一律跳過（每一條都對應實測看到的東西）：

- **格式樣板**（`bs_img_tip_%d.png`）——名字是執行期組出來的
- **沒有路徑分隔的片段**（`_1_l.png`、`.png`）——那是拿去串接的尾巴，不是路徑
- **前後接著 `..` 的**——同上，只是這一段剛好長得像完整路徑
- **資料夾對不到任何已知資源根**——不猜，記在 `stats.unresolvedResources` 裡
"""
from __future__ import annotations

import os
import re

from .. import restree
from ..report import WARN, Finding
from ..fileio import relative

RULES = {
    "RES_MISSING": "Lua 引用的資源檔在資源根裡不存在（資料夾在、檔案不在）",
    "RES_PATTERN_EMPTY": "格式樣板指向的資料夾裡，沒有任何檔案符合它的形狀"
                         "（例如 img_logo_%s.png 對應不到任何 img_logo_*.png）",
}

# ---------------------------------------------------------------- 格式樣板
#
# `string.format( "mail/Img/Avatar/Avatar_%02d.png", n )` 的檔名要執行期才知道，
# 但**不必知道 `%02d` 會變成什麼，仍然有兩件事查得到**：
#
#   一、目錄那一段是靜態的 → 資料夾在不在
#   二、那個資料夾裡有沒有**任何**檔案符合這個形狀
#       （`Avatar_%02d.png` → `avatar_.+\.png`）
#
# 一個都不符合，代表這個前綴指錯地方了（資料夾被搬走、檔案整批改名）。
#
# 實測全庫 155 條樣板：119 條目錄是靜態的、36 條連目錄都是變數（`%s/%s.png`）
# 真的查不了。119 條裡 116 條形狀對得上、1 條資料夾對不到資源根（照舊不猜）。
# 也就是**今天一筆問題都抓不到**——這條規則的價值是迴歸守門，不是找現有 bug：
# 有人改了資料夾名或整批換命名，現在完全沒有東西擋。
# 誤報率 0 是願意加它的前提（對照 `ItemFilePath` 那種拼前綴的做法是 99% 誤報）。
_FORMAT_SPEC = re.compile(r"%[-+ #0]*[\d.]*[diouxXeEfgGqscaA]")

# 字串前面緊接著 `某個常數 ..`——`DIR_PATH .. "/%s.skel"` 的完整樣板是兩段合起來
_CONCAT_PREFIX = re.compile(r"([A-Za-z_][\w.]*)\s*\.\.\s*$")


def _shape(name: str) -> "re.Pattern | None":
    """把 `avatar_%02d.png` 變成 `^avatar_.*\\.png$`。沒有格式符就回 None。

    用 `.*` 而不是 `.+`：代入空字串在 Lua 是合法的（`string.format("ok%s.png", "")`），
    而且更寬鬆就更不會誤報——這條規則的價值在於誤報率 0，寧可少報。
    """
    parts = _FORMAT_SPEC.split(name)
    if len(parts) == 1:
        return None
    return re.compile("^" + ".*".join(re.escape(p) for p in parts) + "$")


def _template_path(value: str, code: str, start: int, strings: dict) -> "str | None":
    """這個字面值是格式樣板的話，回它（接上常數前綴之後）的完整樣板。

    前綴一定要接：`DIR_PATH .. "/%s.skel"` 單看後半段，目錄是 `/`（索引根），
    形狀當然對不上——那會變成兩筆假問題，實測就是這樣冒出來的。
    """
    if "%" not in value or not _FORMAT_SPEC.search(value):
        return None
    match = _CONCAT_PREFIX.search(code[max(0, start - 60):start])
    if match:
        prefix = strings.get(match.group(1))
        if not isinstance(prefix, str) or not prefix:
            return None        # 前綴解不出來就不猜
        pattern = prefix + value
    else:
        pattern = value
    # **格式符之前一定要有目錄**。`coin_%d.png`、`%s.atlas` 這種沒有目錄的樣板
    # 跟 `_skip_reason` 的「沒有 `/` 的片段」是同一種東西——它是要接前綴的尾巴。
    # 少了這道守門，它們會落到索引根（那一層剛好有 36 個檔案、形狀當然對不上），
    # 實測直接產生 30 筆誤報。可運作的專案冒出 30 筆警告，先懷疑自己。
    if "/" not in pattern.replace("\\", "/").split("%")[0]:
        return None
    return pattern


def _pattern_folder(bundle, pattern: str):
    """回 (資料夾, 那個資料夾的檔名集合, 形狀正則)；對不到資源根就回 None。"""
    for candidate in bundle.candidates(pattern):
        folder, _, name = candidate.rpartition("/")
        bucket = bundle.folders.get(folder)
        if bucket is None:
            continue
        shape = _shape(name)
        if shape is None:
            continue           # 格式符在目錄那一段，檔名本身是靜態的
        return folder, bucket, shape
    return None

# 為什麼是 warn 而不是 error：資源載不到在 Cocos 是「記一筆 log、什麼都不畫」，
# 不是當機。Inanna 目前帶著 7 個缺檔正常運作就是證據——畫面少一個元素，
# 玩家看得到、CI 不該擋。這跟 `getChildByName` 回 nil 之後被呼叫方法不一樣。


def _skip_reason(value: str, code: str, start: int, end: int) -> "str | None":
    """不拿去判斷的話，是**哪一種**不判斷。回 `None` 代表要判斷。

    分兩類而不是統稱「動態」，是因為統稱會把人導向錯的結論。實測 `Item` 模組
    700 條裡有 690 條沒檢查，涵蓋率只寫「動態組出來的 690」，看的人合理推論成
    「檔名是 `string.format` 拼出來的，靜態看不到」——但其中**只有 1 條**是格式
    樣板，另外 689 條是 `Protocol.lua` 裡寫死的完整檔名（`img_cash.png`），
    只差前面要接一個常數前綴。兩者能不能補救、要怎麼補救完全不同。

    （順帶一提，那 689 條就算拼出完整路徑也不該報：`item/img/` 磁碟上只有
    4 個 png，其餘的在這台機器的資源樹與所有 plist 裡都不存在。拼起來會產生
    683 筆沒人能處理的警告——涵蓋率的數字變好看，內容是廢的。）
    """
    if "%" in value:
        return "template"               # 格式樣板，執行期才組出來
    if "/" not in value:
        return "fragment"               # 沒有路徑分隔，是拿去串接的片段
    # 前後接著 `..`：這一段只是被串起來的一部分，完整路徑不是它
    if ".." in code[end:end + 6] or ".." in code[max(0, start - 6):start]:
        return "fragment"
    return None


def _skippable(value: str, code: str, start: int, end: int) -> bool:
    return _skip_reason(value, code, start, end) is not None


def check(path: str, text: str, workspace, code: "str | None" = None,
          spans: "list | None" = None, consts: "dict | None" = None) -> list:
    """檢查單一 Lua 檔引用的資源路徑。

    `spans` 就是 `luasrc.scan` 收的字串區間——資源路徑一定是字面字串，所以這條規則
    直接吃那份清單，不必再掃一次正則。註解裡的字串不會進 spans，所以也不會被誤判。
    """
    from .. import luasrc

    bundle = workspace.resources
    if bundle is None:
        return []
    if code is None:
        code, spans = luasrc.scan(text)
    if not spans:
        return []

    findings: list = []
    seen: set = set()
    # 惰性：只有真的遇到格式樣板時才需要常數表。無條件算的話，1304 個檔案裡
    # 有 530 個是白做的——而且呼叫端沒傳 consts 時，check 與 stats 會各算一次。
    holder: list = [] if consts is None else [consts.get("strings", {})]

    def strings_now() -> dict:
        if not holder:
            holder.append(luasrc.constants(code, spans).get("strings", {}))
        return holder[0]
    for start, end, value in spans:
        if not value or not value.lower().endswith(restree.EXTENSIONS):
            continue
        if _skip_reason(value, code, start, end) == "template":
            pattern = _template_path(value, code, start, strings_now())
            found = None if pattern is None else _pattern_folder(bundle, pattern)
            if found is None:
                continue                      # 目錄也是變的，或對不到資源根
            folder, bucket, shape = found
            if any(shape.match(existing) for existing in bucket):
                continue                      # 至少有一個檔案是這個形狀
            line = luasrc.line_of(text, start)
            if (pattern, line) in seen:
                continue
            seen.add((pattern, line))
            findings.append(Finding(
                "RES_PATTERN_EMPTY", WARN, path, line,
                f"樣板 '{pattern}' 指的資料夾 '{folder}' 找得到，"
                "裡面卻沒有任何檔案符合這個形狀"
                "——多半是資料夾搬走了或檔案整批改了命名"
                "；載不到只會少畫一個元素，不會當機",
                key=pattern, confidence="medium",
                detail={"pattern": pattern, "folder": folder,
                        "filesInFolder": len(bucket)}))
            continue
        if _skippable(value, code, start, end):
            continue
        if bundle.verdict(value) != "missing":
            continue
        line = luasrc.line_of(text, start)
        if (value, line) in seen:
            continue
        seen.add((value, line))
        folder = value.replace("\\", "/").rsplit("/", 1)[0]
        findings.append(Finding(
            "RES_MISSING", WARN, path, line,
            f"資源 '{value}' 不存在（'{folder}' 這個資料夾找得到，裡面沒有這個檔案）"
            "——載不到只會少畫一個元素，不會當機",
            key=value, confidence="high",
            detail={"resource": value, "folder": folder,
                    "roots": [relative(r, workspace.directory) for r in bundle.roots]}))
    return findings


def stats(text: str, code: "str | None" = None,
          spans: "list | None" = None, workspace=None,
          consts: "dict | None" = None) -> dict:
    """判斷得到幾筆、因為資料夾對不到而跳過幾筆——覆蓋率要看得見。

    目前實測只有 9% 的資源路徑判斷得到（其餘是別的資源包或執行期搜尋路徑）。
    這個數字不寫出來的話，使用者會以為「沒有回報」等於「資源都對」。
    """
    from .. import luasrc

    bundle = None if workspace is None else workspace.resources
    if bundle is None or not spans:
        return {"checkedResources": 0, "unresolvedResources": 0,
                "dynamicResources": 0, "resourcePaths": 0}
    if code is None:
        code, spans = luasrc.scan(text)
    checked = unresolved = template = fragment = 0
    # 惰性：只有真的遇到格式樣板時才需要常數表。無條件算的話，1304 個檔案裡
    # 有 530 個是白做的——而且呼叫端沒傳 consts 時，check 與 stats 會各算一次。
    holder: list = [] if consts is None else [consts.get("strings", {})]

    def strings_now() -> dict:
        if not holder:
            holder.append(luasrc.constants(code, spans).get("strings", {}))
        return holder[0]
    for start, end, value in spans:
        if not value or not value.lower().endswith(restree.EXTENSIONS):
            continue
        reason = _skip_reason(value, code, start, end)
        if reason == "template":
            # 目錄是靜態的樣板現在**有檢查**（形狀層），要算進「檢查了」，
            # 不然涵蓋率會低報自己實際做過的事。
            pattern = _template_path(value, code, start, strings_now())
            if pattern is not None and _pattern_folder(bundle, pattern) is not None:
                checked += 1
                continue
        if reason is not None:
            # 格式樣板、被 `..` 串起來的片段——不該猜，但**要計數**，而且要
            # 分開計。原本這裡直接 continue，於是全庫 2541 條資源字串裡有
            # 1147 條既不算「判斷完成」也不算「跳過」，表上兩個數字加起來
            # 對不上總數；只給一個「動態」總數又會把人導向錯的結論（見
            # `_skip_reason` 的說明）。
            if reason == "template":
                template += 1
            else:
                fragment += 1
            continue
        if bundle.verdict(value) == "unresolved":
            unresolved += 1
        else:
            checked += 1
    return {"checkedResources": checked, "unresolvedResources": unresolved,
            "templateResources": template, "fragmentResources": fragment,
            "dynamicResources": template + fragment,
            "resourcePaths": checked + unresolved + template + fragment}
