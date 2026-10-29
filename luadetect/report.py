"""檢查結果的表示、輸出與基準檔。

**基準檔（baseline）是這個工具能不能被採用的關鍵。** 39 萬行的既有程式碼一定會掃出
一批既存問題，如果第一天就讓 CI 全紅，結論一定是把檢查關掉。所以先把當下的結果
存成基準，之後只有「新出現的」才算失敗；既存的那些照樣列在報告裡，慢慢還。

基準用的指紋刻意**不含行號**：在檔案上方插幾行不該讓下面每一筆都變成新問題。
換成 (檔案, 規則, 關鍵字) 三者，改到那一行本身才會重新出現。
"""
from __future__ import annotations

import json
import os
import time
import unicodedata

from . import fileio

ERROR = "error"
WARN = "warn"
INFO = "info"

_ORDER = {ERROR: 0, WARN: 1, INFO: 2}
_LABEL = {ERROR: "錯誤", WARN: "警告", INFO: "提醒"}


# 檔案 I/O 的韌性集中在 fileio；這兩個名字保留是因為外部與測試已經在用
replace_atomically = fileio.replace_atomically
relative = fileio.relative


def one_line(text: str, limit: int = 160) -> str:
    """壓成一行，太長就截斷。

    **訊息與指紋一定要是單行的。** 規則會把原始碼片段放進訊息（節點名、handle、
    事件的鍵），而那個片段可能是一整個 inline function：

        pageView:addEventListener( function( sender, eventType )
            ...
        end )

    整段塞進去會壞掉三個地方，實測全部發生過：文字報告一筆佔六行、markdown
    表格的欄數對不齊（GFM 會整張表垮掉）、以及基準檔的指紋含了那段函式內容
    ——callback 裡改一個空白就變成「新問題」，正好違反「指紋不含行號、
    插幾行不該讓下面每一筆都重新出現」這個設計。
    """
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class Finding:
    __slots__ = ("rule", "severity", "confidence", "file", "line", "message",
                 "key", "detail")

    def __init__(self, rule: str, severity: str, file: str, line: int, message: str,
                 key: str = "", confidence: str = "high", detail: "dict | None" = None):
        self.rule = rule
        self.severity = severity
        self.confidence = confidence
        self.file = file
        self.line = line
        # 在這裡收斂，而不是叫每條規則自己記得——漏一條就壞一次
        self.message = one_line(message, 400)
        self.key = one_line(key or message)
        self.detail = detail or {}

    def fingerprint(self, root: str) -> str:
        return f"{relative(self.file, root)}|{self.rule}|{self.key}"

    def to_dict(self, root: "str | None" = None) -> dict:
        path = relative(self.file, root) if root else self.file
        out = {"rule": self.rule, "severity": self.severity,
               "confidence": self.confidence, "file": path, "line": self.line,
               "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


def sort_key(finding: Finding) -> tuple:
    return (_ORDER.get(finding.severity, 3), finding.file, finding.line)


# ---------------------------------------------------------------- 統整表

def display_width(text: str) -> int:
    """這串字在終端機上佔幾格。

    中文是雙寬字元，用 `len()` 去對齊表格會歪掉——而這份報告的欄位名稱與訊息
    幾乎都是中文。`east_asian_width` 的 W（寬）與 F（全寬）算兩格。
    """
    total = 0
    for char in text:
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def _pad(text: str, width: int, right: bool = False) -> str:
    filler = " " * max(0, width - display_width(text))
    return (filler + text) if right else (text + filler)


def table(headers: list, rows: list, right: "set | None" = None) -> list:
    """把資料排成對齊的表格，回傳每一行字串。

    `right` 是要靠右對齊的欄位索引（數字欄靠右才讀得出大小）。
    """
    right = right or set()
    columns = list(zip(*([headers] + rows))) if rows else [[h] for h in headers]
    widths = [max(display_width(str(cell)) for cell in column) for column in columns]
    lines = ["  ".join(_pad(str(h), widths[i], i in right)
                       for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(_pad(str(cell), widths[i], i in right)
                               for i, cell in enumerate(row)).rstrip())
    return lines


# NODE_MISSING 沒被判成 error 的原因，由重到輕。順序就是報告裡的順序。
_DOWNGRADES = (
    ("existsElsewhere", "這個名字在別的 .csd 裡有（可能對到錯的版面）"),
    ("notRooted", "取用鏈的起點不確定（節點可能是外面傳進來的）"),
    ("notDereferenced", "存進欄位卻沒有人讀它（不會當機）"),
    ("nilGuarded", "有 nil 防護（取不到就自己建）"),
    ("unreadableInModule", "模組裡有 .lua 讀不到（可能漏收 setName）"),
)


def _downgrade_reasons(finding: Finding) -> list:
    detail = finding.detail
    out = []
    if detail.get("existsElsewhere"):
        out.append("existsElsewhere")
    if detail.get("rooted") is False:
        out.append("notRooted")
    if detail.get("dereferenced") is False:
        out.append("notDereferenced")
    if detail.get("nilGuarded"):
        out.append("nilGuarded")
    if detail.get("unreadableInModule"):
        out.append("unreadableInModule")
    return out


COVERAGE_HEADER = ["項目", "檢查了", "沒檢查", "總數", "沒檢查的是什麼"]


def coverage_rows(coverage: dict) -> list:
    """涵蓋率的每一列都要有**分母**。

    只給「資源路徑判斷 7」的話，看的人沒辦法分辨這是「7 條全驗過」還是
    「7 條驗過、另外 40 條根本沒交代」——而這張表存在的唯一理由就是回答
    那個問題。實測全庫 2541 條資源字串裡有 1147 條是動態組出來的，
    原本兩個桶加起來只交代了 1394 條，差額不在表上任何地方。

    「沒檢查」不代表有問題，但它必須看得見；看不見的跳過等於謊報涵蓋率。
    """
    files = coverage.get("files", 0)
    unreadable = coverage.get("unreadable", 0)

    checked_nodes = coverage.get("checkedNodes", 0)
    skipped_nodes = coverage.get("unresolvedCsb", 0)
    node_total = coverage.get("nodeFiles") or (checked_nodes + skipped_nodes)

    checked_res = coverage.get("checkedResources", 0)
    unresolved_res = coverage.get("unresolvedResources", 0)
    dynamic_res = coverage.get("dynamicResources", 0)
    res_total = coverage.get("resourcePaths") or (
        checked_res + unresolved_res + dynamic_res)

    # 只寫一個「動態組出來的 N」會把人導向錯的結論——實測 `Item` 模組 690 條
    # 裡只有 1 條是格式樣板，其餘 689 條是寫死的完整檔名、只差一個常數前綴。
    # 兩者能不能補救完全不同，所以分開寫。
    template_res = coverage.get("templateResources", 0)
    fragment_res = coverage.get("fragmentResources", dynamic_res - template_res)
    reasons = "、".join(part for part in (
        f"要接前綴的檔名片段 {fragment_res}" if fragment_res else "",
        f"格式樣板 {template_res}" if template_res else "",
        f"對不到資源根 {unresolved_res}" if unresolved_res else "") if part)

    return [
        [".lua 檔案", files, unreadable, files + unreadable,
         "讀不到（被鎖住或權限不足）" if unreadable else "（全部讀得到）"],
        ["有節點取用的檔案", checked_nodes, skipped_nodes, node_total,
         "查不出對應的 .csb，不猜" if skipped_nodes else "（全部對得到 .csd）"],
        ["資源路徑", checked_res, res_total - checked_res, res_total,
         reasons or "（全部判斷過）"],
    ]


def summarise(findings: list, stats: dict, known: "list | None" = None) -> dict:
    """檢測結果的統整：規則 × 嚴重度、涵蓋率、降級原因。

    **涵蓋率一定要一起給。** 只給「幾筆問題」的話，「沒有回報」會被讀成「都對」，
    而實際上有 32 個檔案查不出對應的 .csb、214 處資源路徑對不到資源根，那些是
    根本沒檢查，不是檢查過沒問題。
    """
    by_rule: dict = {}
    for finding in findings:
        row = by_rule.setdefault(finding.rule, {ERROR: 0, WARN: 0, INFO: 0})
        row[finding.severity] = row.get(finding.severity, 0) + 1

    reasons: dict = {}
    for finding in findings:
        if finding.rule == "NODE_MISSING" and finding.severity != ERROR:
            for key in _downgrade_reasons(finding):
                reasons[key] = reasons.get(key, 0) + 1

    totals = {level: sum(row.get(level, 0) for row in by_rule.values())
              for level in (ERROR, WARN, INFO)}
    return {
        "byRule": by_rule,
        "totals": totals,
        "total": sum(totals.values()),
        "baselined": len(known) if known else 0,
        "downgrades": reasons,
        "coverage": {
            "files": stats.get("files", 0),
            "unreadable": stats.get("unreadable", 0),
            "checkedNodes": stats.get("checkedNodes", 0),
            "unresolvedCsb": stats.get("unresolvedCsb", 0),
            "checkedResources": stats.get("checkedResources", 0),
            "unresolvedResources": stats.get("unresolvedResources", 0),
            "dynamicResources": stats.get("dynamicResources", 0),
            "templateResources": stats.get("templateResources", 0),
            "fragmentResources": stats.get("fragmentResources", 0),
            "nodeFiles": stats.get("nodeFiles", 0),
            "resourcePaths": stats.get("resourcePaths", 0),
        },
        "exitCode": exit_code(findings),
    }


def summary_markdown(summary: dict) -> str:
    """同一份統整，排成 markdown 表格——給 MCP 的呼叫端（agent）原樣呈現用。

    為什麼不讓 agent 自己從 `summary` 那份 JSON 重組：重組要花 token，而且數字
    抄錯了沒人看得出來。直接給一段可以貼出去的表格，它就只需要轉述。

    刻意用 markdown 而不是對齊過的 ASCII：agent 的回答是以 markdown 呈現的，
    markdown 表格會被正確排版；ASCII 表格貼進 markdown 反而會散掉。
    """
    def row(cells) -> str:
        return "| " + " | ".join(str(c) for c in cells) + " |"

    lines: list = ["### 檢測統整", "",
                   row(["規則", _LABEL[ERROR], _LABEL[WARN], _LABEL[INFO], "合計"]),
                   row(["---", "--:", "--:", "--:", "--:"])]
    for rule, counts in sorted(summary["byRule"].items(),
                               key=lambda item: (-item[1].get(ERROR, 0),
                                                 -sum(item[1].values()))):
        total = sum(counts.get(level, 0) for level in (ERROR, WARN, INFO))
        lines.append(row([f"`{rule}`", counts.get(ERROR, 0), counts.get(WARN, 0),
                          counts.get(INFO, 0), total]))
    totals = summary["totals"]
    lines.append(row(["**合計**", f"**{totals[ERROR]}**", f"**{totals[WARN]}**",
                      f"**{totals[INFO]}**", f"**{summary['total']}**"]))

    coverage = summary["coverage"]
    lines += ["", "**涵蓋率**——「沒有回報」不等於「都對」：", "",
              row(COVERAGE_HEADER), row(["---", "--:", "--:", "--:", "---"])]
    for entry in coverage_rows(coverage):
        lines.append(row(entry))

    if summary["downgrades"]:
        lines += ["", "**`NODE_MISSING` 為什麼不是 error**（可同時成立）：", "",
                  row(["原因", "筆數"]), row(["---", "--:"])]
        for key, label in _DOWNGRADES:
            if key in summary["downgrades"]:
                lines.append(row([label, summary["downgrades"][key]]))

    lines.append("")
    if summary["baselined"]:
        lines.append(f"另有 {summary['baselined']} 筆在基準檔內，不計入成敗。")
    lines.append("離開碼 `%d`（%s）。" % (
        summary["exitCode"],
        "有錯誤等級的問題" if summary["exitCode"] else "通過，沒有錯誤等級的問題"))
    return "\n".join(lines)


# NODE_MISSING 的等級是算出來的，所以要能講出「為什麼是這個等級」。
_WHY = (
    ("existsElsewhere", True, "名字在別的 .csd 裡有 → 可能對到錯的版面"),
    ("rooted", False, "取用鏈起點不確定 → 節點可能是外面傳進來的"),
    ("dereferenced", False, "存進欄位沒人讀 → 不會當機"),
    ("nilGuarded", True, "有 nil 防護 → 不會當機"),
    ("unreadableInModule", True, "模組裡有 .lua 讀不到 → 可能漏收 setName"),
)


_CONTEXT = {"enter": "寫在每次開啟都會跑的方法裡 → 開幾次疊幾份",
            "once": "寫在一輩子只跑一次的地方 → 多半是刻意的常駐",
            "other": "寫在一般方法裡", "toplevel": "寫在檔案最外層"}


def why_line(finding: Finding) -> str:
    """這一筆為什麼是這個等級——一句話，給表格用。

    每條規則的等級都是算出來的，所以「為什麼」才是可以拿去動手的資訊；
    只給等級的話，看的人還要自己回去讀判準。
    """
    detail = finding.detail
    if finding.rule == "NODE_MISSING":
        if finding.severity == ERROR:
            return "取用鏈成立、結果被使用、沒有防護、名字全庫都找不到"
        reasons = [text for key, expect, text in _WHY
                   if (detail.get(key) is expect
                       or (expect is True and detail.get(key)))]
        return "；".join(reasons) or "見訊息"
    if finding.rule == "NODE_WRONG_PARENT":
        return "名稱存在，但不在程式碼假設的父節點底下"
    if finding.rule in ("SCHED_LEAK", "SCHED_NO_HANDLE"):
        if detail.get("ownerProtected"):
            return "owner 有綁，wrapper 會自己解除 → 不是野指標，只是少了雙保險"
        return _CONTEXT.get(detail.get("context"), "找不到對應的解除")
    if finding.rule == "EVENT_LEAK":
        return _CONTEXT.get(detail.get("context"), "找不到對應的解除")
    if finding.rule == "RES_MISSING":
        return "資料夾找得到、檔案不在裡面 → 載不到只會少畫一個元素"
    if finding.rule == "NODE_CSB_UNRESOLVED":
        return "查不出對應的 .csb，節點檢查整份跳過"
    return "見訊息"


def severity_label(severity: str) -> str:
    return _LABEL.get(severity, severity)


def why_from_dict(item: dict) -> str:
    """`Finding.to_dict()` 出來的那份 dict 也要能問「為什麼是這個等級」。

    `explain` 回的是已經序列化的 dict，不是 Finding；共用同一套說法，
    兩張表才不會對同一筆事情講得不一樣。
    """
    return why_line(Finding(item.get("rule", ""), item.get("severity", ""),
                            item.get("file", ""), item.get("line", 0),
                            item.get("message", ""),
                            detail=item.get("detail") or {}))


def findings_markdown(findings: list, root: str, limit: int = 30) -> str:
    """一份逐筆的 markdown 表格，給 agent 原樣呈現。"""
    if not findings:
        return "沒有發現問題。"
    def row(cells):
        return "| " + " | ".join(str(c) for c in cells) + " |"
    lines = [row(["檔案:行", "規則", "等級", "為什麼是這個等級"]),
             row(["---", "---", "---", "---"])]
    for finding in sorted(findings, key=sort_key)[:limit]:
        lines.append(row([f"`{relative(finding.file, root)}:{finding.line}`",
                          f"`{finding.rule}`", _LABEL.get(finding.severity,
                                                          finding.severity),
                          why_line(finding)]))
    if len(findings) > limit:
        lines.append(f"\n（共 {len(findings)} 筆，只列前 {limit} 筆）")
    return "\n".join(lines)


def summary_lines(summary: dict) -> list:
    """把 `summarise` 的結果排成表格。"""
    lines: list = ["", "=" * 60, "統整", ""]

    rows = [[rule, row.get(ERROR, 0), row.get(WARN, 0), row.get(INFO, 0),
             row.get(ERROR, 0) + row.get(WARN, 0) + row.get(INFO, 0)]
            for rule, row in sorted(summary["byRule"].items(),
                                    key=lambda item: (-item[1].get(ERROR, 0),
                                                      -sum(item[1].values())))]
    totals = summary["totals"]
    rows.append(["合計", totals[ERROR], totals[WARN], totals[INFO], summary["total"]])
    lines += ["  " + line for line in table(
        ["規則", _LABEL[ERROR], _LABEL[WARN], _LABEL[INFO], "合計"], rows,
        right={1, 2, 3, 4})]

    coverage = summary["coverage"]
    lines += ["", "  涵蓋率——「沒有回報」不等於「都對」：", ""]
    lines += ["  " + line for line in table(
        COVERAGE_HEADER, coverage_rows(coverage), right={1, 2, 3})]

    if summary["downgrades"]:
        lines += ["", "  NODE_MISSING 為什麼不是 error（可同時成立）：", ""]
        lines += ["  " + line for line in table(
            ["原因", "筆數"],
            [[label, summary["downgrades"][key]]
             for key, label in _DOWNGRADES if key in summary["downgrades"]],
            right={1})]

    lines.append("")
    if summary["baselined"]:
        lines.append(f"  另有 {summary['baselined']} 筆在基準檔內，不計入成敗。")
    lines.append("  離開碼 %d（%s）" % (
        summary["exitCode"],
        "有錯誤等級的問題" if summary["exitCode"] else "通過，沒有錯誤等級的問題"))
    return lines


# ---------------------------------------------------------------- 基準

def load_baseline(path: str, timeout: float = 2.0) -> set:
    """讀基準檔。

    「檔案不存在」與「現在讀不到」是**兩件完全不同的事**，不能都回空集合：回空的話
    所有既存問題都會變成新問題，hook 擋下 commit、CI 全紅，然後重跑一次又好了。

    多開 agent 時這在 Windows 上真的會撞到——另一個行程正在 `os.replace` 這個檔案，
    讀取端的 `open()` 會拿到 sharing violation。實測 4398 次讀取裡有 49 次。
    所以檔案存在卻打不開時要短暫重試，而不是當成沒有基準。

    內容壞掉也一樣不能安靜跳過：那會變成一次無法解釋的 CI 全紅。直接報錯，
    訊息裡寫清楚怎麼處理。
    """
    if not os.path.isfile(path):
        return set()                    # 真的沒有基準檔，這才是回空集合的唯一理由
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            break
        except OSError as exc:
            if not os.path.isfile(path):
                return set()            # 這中間被刪掉了
            if time.monotonic() >= deadline:
                raise OSError(
                    f"讀不了基準檔 {path}：{exc}。"
                    "有別的行程壓著它的話關掉再試；"
                    "要先跳過基準檔就加 --no-baseline。") from exc
            time.sleep(0.01)
        except ValueError as exc:
            raise ValueError(
                f"基準檔 {path} 不是有效的 JSON：{exc}。"
                "重新產生（scan --write-baseline），或用 --no-baseline 先跳過。"
                "安靜忽略它會讓既存的問題全部變成新問題。") from exc
    if not isinstance(data, dict):
        raise ValueError(f"基準檔 {path} 的根節點不是物件。重新產生一份。")
    return set(data.get("fingerprints") or [])


def save_baseline(path: str, findings: list, root: str) -> dict:
    """寫基準檔。**原子寫入**：先寫暫存檔再 `os.replace`。

    直接 `open(path, "w")` 會讓檔案有一段時間是半截的 JSON。同一台機器上多開幾個
    agent 時，一邊在建基準、另一邊的 commit 觸發 hook 去讀，讀到半截就會被
    `load_baseline` 當成「沒有基準」——215 筆既存問題全部變成新問題，hook 擋下
    commit、CI 全紅，而且重跑一次就好了，完全查不出原因。
    """
    prints = sorted({f.fingerprint(root) for f in findings})
    payload = {"note": "luadetect 基準檔：這些是建立當下就存在的問題，"
                       "之後只有新出現的才會讓檢查失敗。修掉之後可以重新產生。",
               "root": root.replace("\\", "/"),
               "count": len(prints),
               "fingerprints": prints}
    full = os.path.abspath(path)
    folder = os.path.dirname(full)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temp = f"{full}.{os.getpid()}.tmp"          # 帶 pid，多個行程不會互相截斷
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        replace_atomically(temp, full)
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass
    return {"baseline": full, "count": len(prints)}


def split_baseline(findings: list, baseline: set, root: str) -> tuple:
    """分成 (新問題, 基準內既有的問題)。"""
    if not baseline:
        return list(findings), []
    fresh, known = [], []
    for finding in findings:
        (known if finding.fingerprint(root) in baseline else fresh).append(finding)
    return fresh, known


# ---------------------------------------------------------------- 輸出

def to_json(findings: list, root: str, stats: dict, known: "list | None" = None,
            counted: "list | None" = None) -> dict:
    """`findings` 是要列出來的；`counted` 是要計數的（截斷前的全部）。

    MCP 那邊預設只列最嚴重的 50 筆，計數卻要算全部——不然模型會看到
    「error 5、warn 0、info 0」而以為沒有警告，實際上總共有 219 筆。
    """
    counts = {ERROR: 0, WARN: 0, INFO: 0}
    for finding in (counted if counted is not None else findings):
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    payload = {
        "root": root.replace("\\", "/"),
        "counts": counts,
        "findings": [f.to_dict(root) for f in sorted(findings, key=sort_key)],
        "stats": stats,
    }
    if known is not None:
        payload["baselined"] = len(known)
    # 統整算的是**截斷前**的全部（跟 counts 同一個道理）
    payload["summary"] = summarise(counted if counted is not None else findings,
                                   stats, known)
    return payload


def to_text(findings: list, root: str, stats: dict, known: "list | None" = None) -> str:
    lines: list = []
    grouped: dict = {}
    for finding in sorted(findings, key=sort_key):
        grouped.setdefault(relative(finding.file, root), []).append(finding)

    for rel, items in grouped.items():
        lines.append("")
        lines.append(rel)
        for finding in items:
            mark = "?" if finding.confidence != "high" else " "
            lines.append("  L%-5d %s%-22s %s"
                         % (finding.line, mark, finding.rule, finding.message))

    lines += summary_lines(summarise(findings, stats, known))
    if any(f.confidence != "high" for f in findings):
        lines.append("  標了 ? 的那幾筆把握較低，訊息裡寫著是哪個條件沒過。")
    if stats.get("unresolvedCsb"):
        lines.append("  節點檢查跳過的那些：加 --explain 看是哪幾個檔案，"
                     "或用 explain 逐一診斷。")
    if stats.get("unresolvedResources"):
        lines.append("  資源路徑跳過的那些：在 .luadetect.json 補 resourceRoots "
                     "就能納入更多。")
    if stats.get("unreadable"):
        lines.append("  讀不到的 .lua 完全沒有檢查——被其他程式鎖住或權限不足，"
                     "處理掉再跑一次。")
    return "\n".join(lines)


def exit_code(findings: list) -> int:
    return 1 if any(f.severity == ERROR for f in findings) else 0
