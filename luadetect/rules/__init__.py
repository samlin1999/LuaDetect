"""規則登錄表。

三組規則放在同一個工具裡，因為它們針對的是同一件事：**送出前就看得出來的當機來源**，
而且證據都是同一種——字面字串對上磁碟上的 ground truth。一次掃描讀一次檔就三組
都跑完，開發者只要記一個指令。
"""
from __future__ import annotations

from . import lifecycle, nodes, resources

GROUPS = {
    "nodes": {"module": nodes, "title": "節點契約",
              "doc": "getChildByName 取的節點在對應的 .csd 裡存不存在"},
    "lifecycle": {"module": lifecycle, "title": "生命週期對稱",
                  "doc": "排程與事件註冊了有沒有解除"},
    "resources": {"module": resources, "title": "共用資源契約",
                  "doc": "Lua 引用的資源檔在資源根裡存不存在"},
}

ALL_RULES = {}
for _key, _group in GROUPS.items():
    for _rule, _doc in _group["module"].RULES.items():
        ALL_RULES[_rule] = {"group": _key, "doc": _doc}


def resolve_groups(selection: "str | list | None") -> list:
    """把使用者給的 `--rules` 轉成要跑的群組名稱。"""
    if not selection:
        return list(GROUPS)
    if isinstance(selection, str):
        selection = [item.strip() for item in selection.split(",") if item.strip()]
    picked: list = []
    for item in selection:
        low = item.lower()
        if low in ("all", "*"):
            return list(GROUPS)
        if low in GROUPS:
            picked.append(low)
            continue
        rule = ALL_RULES.get(item.upper())
        if rule and rule["group"] not in picked:
            picked.append(rule["group"])
            continue
        raise ValueError(
            f"不認得的規則 '{item}'。可用的群組：{'、'.join(GROUPS)}；"
            f"規則：{'、'.join(sorted(ALL_RULES))}。")
    return picked or list(GROUPS)
