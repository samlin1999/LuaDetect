"""拿 csdview 當對照，檢查這裡的 `.csd` 解析有沒有語意缺口。

**為什麼不是直接叫 csdview 來做檢查**：`csdview tree` 一份要 0.5 秒，全庫 1295 份
就是 11 分鐘，而自己解 XML 是 5 秒多。掃描每天要跑很多次，那個差距不能接受。

**但獨立實作會漂移。** 實際發生過：`ProjectNodeObjectData` 引用別的 `.csd` 時，
引用處自己還可以再加子節點，執行期兩邊都在——這裡漏了 inline 那一批，於是回報了
9 筆「節點不存在」的 error，而 csdview 一直是對的。那個 bug 是人工讀 XML 才發現的。
所以把那件事變成一個可以重複跑的指令：抽樣叫 csdview，比對節點名稱集合。

**有一個差異是刻意的、不算缺口**：csdview 為了畫面呈現保留了被引用檔案的根節點
那一層（路徑會是 `n_extraBet/Node/Bet_Icon`），執行期 `CSLoader` 沒有那一層
（`n_extraBet:getChildByName( "Bet_Icon" )` 直接拿得到）。拿 csdview 的路徑去對
Lua 的取用鏈會整串對不上，所以這裡不保留。比對時要把那些根節點名字扣掉。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from . import csdtree


class VerifyError(Exception):
    """沒辦法比對；訊息直接回給呼叫端。"""


def available() -> "str | None":
    """csdview 執行檔的位置；沒裝就回 None。"""
    for name in ("csdview", "csdview.exe", "csdview.cmd"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _csdview_names(executable: str, path: str, root: str,
                   timeout: float = 120.0) -> set:
    """csdview 眼中這份 .csd 的所有節點名稱。"""
    try:
        done = subprocess.run([executable, "tree", path, "--json", "--root", root],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerifyError(f"叫 csdview 失敗（{path}）：{exc}") from exc
    if done.returncode != 0 or not done.stdout.strip():
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise VerifyError(f"csdview 讀不了 {os.path.basename(path)}："
                          + (detail[0] if detail else f"離開碼 {done.returncode}"))
    try:
        payload = json.loads(done.stdout)
    except ValueError as exc:
        raise VerifyError(f"csdview 對 {os.path.basename(path)} 的輸出不是 JSON："
                          f"{exc}") from exc

    found: set = set()

    def walk(node: dict) -> None:
        if node.get("name"):
            found.add(node["name"])
        for child in node.get("children") or []:
            walk(child)

    walk(payload.get("tree") or {})
    return found


def _dropped_root_names(project, tree) -> set:
    """這棵樹裡「被引用檔案的根節點」名字——刻意不保留的那一層。

    每個節點都記著自己來自哪份 `.csd`（`Node.source`），所以把樹裡出現過的來源
    檔案各自的根節點名字收起來就是了，不必去掃全庫。
    """
    sources = {node.source for node, _ in csdtree.walk(tree) if node.source}
    names: set = set()
    for source in sources:
        try:
            obj = csdtree._object_data(source)
        except csdtree.CsdError:
            continue
        if obj is not None and obj.get("Name"):
            names.add(obj.get("Name"))
    return names


def compare_one(project, path: str, executable: str) -> dict:
    """比對一份 `.csd`，回傳 `{"file", "missing", "extra"}`。

    - `missing`：csdview 有、這裡沒有 → 會讓工具誤報「節點不存在」，是真缺口
    - `extra`：這裡有、csdview 沒有 → 憑空多出節點，同樣是缺口
    """
    theirs = _csdview_names(executable, path, project.root)
    tree = project.tree(path)
    mine = csdtree.names(tree)
    ignore = _dropped_root_names(project, tree)
    return {"file": os.path.relpath(path, project.root).replace("\\", "/"),
            "missing": sorted(theirs - mine - ignore),
            "extra": sorted(mine - theirs)}


def compare(project, paths: list, executable: "str | None" = None) -> dict:
    """比對一批 `.csd`。回傳 `{"checked", "failed", "gaps", "errors"}`。

    單一檔案讀不了不會中斷整批——一份壞掉的 `.csd` 不該讓整次比對白跑。
    """
    executable = executable or available()
    if not executable:
        raise VerifyError(
            "找不到 csdview。這個比對需要它（pip install csdview 或問工具的維護者）；"
            "沒有它也不影響 scan，只是少了這道交叉檢查。")
    gaps: list = []
    errors: list = []
    checked = 0
    for path in paths:
        try:
            result = compare_one(project, path, executable)
        except (VerifyError, csdtree.CsdError) as exc:
            errors.append(str(exc))
            continue
        checked += 1
        if result["missing"] or result["extra"]:
            gaps.append(result)
    return {"checked": checked, "failed": len(errors), "gaps": gaps,
            "errors": errors}
