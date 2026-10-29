"""從 `.csd` 反推 Properties（UIManager）綁定程式碼。

檢查是「Lua 寫的名字，csd 裡有沒有」；這裡是反過來走同一條證據鏈：
**csd 裡有什麼，就把綁定寫出來**。同一份 `csdtree` 解析、同一組節點名，
所以產出來的東西天生就通得過 `nodes` 那組規則——手打才會打錯字。

格式是照著專案現有的 73 個 `*Properties.lua` 抄的，不是自己發明的。
實測那 73 個檔案：**全部**都用常數表，`getChildByName( "字面字串" )` 一處都沒有。

命名規則也是量出來的，不是猜的：

- **欄位名 = `m_` + 節點名**——738 處綁定裡 89.7% 完全相同、2.2% 只差大小寫
- **常數鍵 = 節點名轉大寫**（非英數換成 `_`）——1946 個鍵裡 90.7% 是這樣

剩下不到 10% 是人工改過的（`Node_PlatRoot` → `m_plat_root`、`labelWin` →
`m_txt_win`）。產出來之後照著改名是正常的，這份東西的用途是把 90% 的
機械勞動與打字錯誤拿掉，不是取代判斷。
"""
from __future__ import annotations

import os
import re

from . import csdtree

# Cocos Studio 沒改過名字的節點：`Panel_1`、`Image_23`。這些不會有人拿名字去取，
# 綁出來只是雜訊。實測抽 12 份 csd 的 299 個節點，只有 17 個（5.7%）是這種。
_DEFAULT_NAME = re.compile(
    r"^(?:Panel|Image|ImageView|Text|TextField|TextAtlas|TextBMFont|Button|"
    r"Sprite|Node|Layout|ListView|ScrollView|PageView|Slider|CheckBox|"
    r"LoadingBar|Particle|Armature|Widget)(?:_\d+)?$")

_INDENT = "    "


def _identifier(name: str) -> str:
    """節點名轉成合法的 Lua 識別字尾巴。"""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_")
    return cleaned or "node"


def is_default_name(name: str) -> bool:
    return bool(_DEFAULT_NAME.match(name or ""))


def _has_named_descendant(node) -> bool:
    """底下還有取過名字的節點嗎。

    `getChildByName` **只找直接子層**。所以預設名的節點不能單純跳過——
    跳過會把它的子節點接到祖父層，取用鏈就斷了。實測：拿產出的程式碼去跑
    `nodes` 規則，立刻 17 筆 `NODE_WRONG_PARENT`（工具驗出了自己的生成器）。
    有取名的後代時，這個中間層照樣要綁。
    """
    for child in getattr(node, "children", None) or []:
        name = (child.name or "").strip()
        if name and not is_default_name(name):
            return True
        if _has_named_descendant(child):
            return True
    return False


class _Binding:
    __slots__ = ("name", "key", "field", "receiver", "depth", "kind")

    def __init__(self, name, key, field, receiver, depth, kind):
        self.name = name
        self.key = key
        self.field = field
        self.receiver = receiver
        self.depth = depth
        self.kind = kind


def plan(tree, include_default: bool = False) -> dict:
    """走一遍節點樹，決定每個節點的常數鍵、欄位名與取用的父節點。

    回傳 `{bindings, skipped, renamed}`。`renamed` 是撞名之後被加尾碼的，
    一定要讓使用者看到——同名節點在不同父層下是合法的，但欄位名不能重複。
    """
    bindings: list = []
    skipped: list = []
    renamed: list = []
    used_fields: set = set()
    used_keys: dict = {}

    def visit(node, receiver: str, depth: int):
        for child in getattr(node, "children", None) or []:
            name = (child.name or "").strip()
            if not name:
                continue
            if (not include_default and is_default_name(name)
                    and not _has_named_descendant(child)):
                skipped.append(name)
                continue

            base = _identifier(name)
            key = base.upper()
            if used_keys.get(key, name) != name:
                # 兩個不同的節點名轉成同一個鍵（`btn-ok` 與 `btn_ok`）
                suffix = 2
                while used_keys.get(f"{key}_{suffix}", name) != name:
                    suffix += 1
                key = f"{key}_{suffix}"
            used_keys[key] = name

            field = f"m_{base}"
            if field in used_fields:
                suffix = 2
                while f"{field}_{suffix}" in used_fields:
                    suffix += 1
                renamed.append((name, field, f"{field}_{suffix}"))
                field = f"{field}_{suffix}"
            used_fields.add(field)

            bindings.append(_Binding(name, key, field, receiver, depth,
                                     child.type or ""))
            visit(child, f"self.{field}", depth + 1)

    visit(tree, "view", 0)
    return {"bindings": bindings, "skipped": skipped, "renamed": renamed}


def _names_table(bindings: list) -> list:
    """常數表，縮排跟著節點深度走——照現有檔案的樣子。"""
    lines = ["local NodeNames = {"]
    previous = None
    for item in bindings:
        # 回到比較淺的層級時空一行，讀起來才分得出群組
        if previous is not None and item.depth < previous:
            lines.append("")
        lines.append(f"{_INDENT * (item.depth + 1)}{item.key} = \"{item.name}\",")
        previous = item.depth
    lines.append("}")
    return lines


def _ctor_body(bindings: list) -> list:
    """綁定本體。

    順序是深度優先前序，這是**必要條件**不是風格：`self.m_bg` 要先被指派，
    下一行才拿得到它當接收者。

    對齊寬度取全體最長的一個，分段規則跟常數表一樣（回到比較淺的層級就空一行）。
    原本改成「同一個接收者算一組」，結果深度優先會把兄弟節點拆開
    ——`m_left` 與 `m_right` 中間隔著 `m_left` 的整棵子樹，兩個都變成孤零零
    一行、還各自對齊，讀起來比不對齊還糟。
    """
    if not bindings:
        return []
    width = max(len(item.field) for item in bindings)
    lines: list = []
    previous = None
    for item in bindings:
        if previous is not None and item.depth < previous:
            lines.append("")
        lines.append(
            f"{_INDENT}self.{item.field.ljust(width)} = "
            f"{item.receiver}:getChildByName( NodeNames.{item.key} )")
        previous = item.depth
    return lines


def properties_source(tree, class_name: str, module: str = "",
                      source: str = "", include_default: bool = False) -> dict:
    """產生一份 `XxxProperties.lua` 的內容。

    `class_name` 是不含 `Properties` 的名字（`MainView`），`module` 是模組名
    （`VoucherCenter`）——兩者合起來就是專案裡 `IClass` 的命名慣例。
    """
    outcome = plan(tree, include_default)
    bindings = outcome["bindings"]
    name = f"{class_name}Properties"
    lines = []
    if source:
        lines += [f"-- 由 luadetect bind {source} 產生。",
                  "-- 節點名直接取自 .csd，所以不會有打錯字的問題；",
                  "-- 欄位名是照慣例的 m_<節點名>，要改成更好讀的名字很正常。",
                  ""]
    lines += _names_table(bindings)
    lines += ["",
              f"---@class inn.{module}.{name}" if module else f"---@class {name}",
              f"local {name} = IClass( \"{module}{name}\" )",
              f"{name}.NodeNames = NodeNames",
              "",
              f"function {name}:ctor( view )"]
    lines += _ctor_body(bindings)
    lines += ["end", "", f"return {name}", ""]
    return {"lua": "\n".join(lines),
            "bindings": len(bindings),
            "skipped": outcome["skipped"],
            "renamed": outcome["renamed"]}


def build(project: "csdtree.Project", csd: str, class_name: str = "",
          module: str = "", include_default: bool = False) -> dict:
    """從專案裡挑一份 `.csd`，產生對應的 Properties 內容。"""
    path = (project.resolve(csd)
            or project.by_stem(os.path.splitext(os.path.basename(csd))[0]))
    if isinstance(path, list):
        path = path[0] if path else None
    if not path:
        raise csdtree.CsdError(f"找不到 .csd：{csd}")
    tree = project.tree(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    return dict(properties_source(
        tree, class_name or stem, module,
        source=os.path.relpath(path, project.root).replace("\\", "/"),
        include_default=include_default), csd=path)
