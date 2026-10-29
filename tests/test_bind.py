"""從 .csd 反推 Properties（UIManager）綁定。

驗收標準只有一個、而且不是「長得像」：**產出來的程式碼要通得過 `nodes` 規則**。
同一份解析、同一組節點名，通不過就代表取用鏈算錯了。
"""
from __future__ import annotations

import json
import os

import pytest

from luadetect import bind, luasrc, projects
from luadetect.rules import nodes as node_rules

CSD = ('<GameFile><Content ctype="GameProjectContent"><Content>'
       '<ObjectData Name="{name}" ctype="GameLayerObjectData"><Children>{children}'
       '</Children></ObjectData></Content></Content></GameFile>')
NODE = '<AbstractNodeData Name="{name}" ctype="{ctype}">{inner}</AbstractNodeData>'


def _node(name, inner="", ctype="ImageViewObjectData"):
    return NODE.format(name=name, ctype=ctype, inner=inner)


def _children(*nodes):
    return "<Children>" + "".join(nodes) + "</Children>"


@pytest.fixture
def studio(tmp_path):
    """一個只有 cocosstudio 的專案，外加把 .csd 寫進去的小工具。"""
    repo = tmp_path / "repo"
    (repo / "InannaLua" / "Modules" / "Demo").mkdir(parents=True)
    root = tmp_path / "cocosstudio"
    (root / "ui").mkdir(parents=True)
    (repo / ".luadetect.json").write_text(
        json.dumps({"csdRoot": str(root)}), encoding="utf-8")

    def write(rel, children, layer="Layer"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CSD.format(name=layer, children=children), encoding="utf-8")
        return str(path)

    return repo, root, write


def _generate(studio, children, **kwargs):
    repo, _, write = studio
    path = write("ui/View.csd", children)
    workspace = projects.resolve(project=str(repo))
    return workspace, path, bind.build(workspace.csd, path, **kwargs)


def _check(workspace, csd, source):
    """拿真正的節點規則驗產出的程式碼。"""
    code, spans = luasrc.scan(source)[:2]
    node_rules.reset_caches()
    return node_rules.check(
        "generated.lua", source, workspace,
        sources={"csd": [csd], "csb": [], "origin": "config", "note": ""},
        code=code, spans=spans)


# ------------------------------------------------------------------ 形狀

def test_it_writes_the_house_style(studio):
    _, _, out = _generate(studio, _node("bg", _children(_node("btn_ok"))),
                          class_name="View", module="Demo")
    lua = out["lua"]
    assert "local NodeNames = {" in lua
    assert '    BG = "bg",' in lua
    assert '        BTN_OK = "btn_ok",' in lua          # 縮排跟著深度
    assert '---@class inn.Demo.ViewProperties' in lua
    assert 'local ViewProperties = IClass( "DemoViewProperties" )' in lua
    assert "ViewProperties.NodeNames = NodeNames" in lua
    assert "function ViewProperties:ctor( view )" in lua
    assert "return ViewProperties" in lua
    # 專案裡 73 個 Properties 檔沒有一個把字面字串寫在 getChildByName 裡
    assert 'getChildByName( "' not in lua


def test_children_bind_through_their_parent(studio):
    _, _, out = _generate(studio, _node("bg", _children(_node("btn_ok"))),
                          class_name="View")
    assert "self.m_bg     = view:getChildByName( NodeNames.BG )" in out["lua"]
    assert ("self.m_btn_ok = self.m_bg:getChildByName( NodeNames.BTN_OK )"
            in out["lua"])


# ------------------------------------------------- 唯一有意義的驗收：規則驗得過

def test_the_generated_code_passes_the_node_rule(studio):
    workspace, csd, out = _generate(
        studio,
        _node("bg", _children(
            _node("btn_ok"),
            _node("panel", _children(_node("txt_title"), _node("icon")),
                  "PanelObjectData"))),
        class_name="View")
    assert out["bindings"] == 5
    assert _check(workspace, csd, out["lua"]) == []


def test_a_default_named_node_with_named_children_is_still_bound(studio):
    """`getChildByName` **只找直接子層**。

    預設名的節點不能單純跳過——跳過會把它的子節點接到祖父層，取用鏈就斷了。
    實測：第一版真的這樣寫，拿產出的程式碼去跑 `nodes` 規則立刻 17 筆
    `NODE_WRONG_PARENT`（工具驗出了自己的生成器）。
    """
    workspace, csd, out = _generate(
        studio,
        _node("bg", _children(
            _node("Panel_1", _children(_node("txt_deep")), "PanelObjectData"))),
        class_name="View")
    assert '        PANEL_1 = "Panel_1",' in out["lua"]
    assert ("self.m_txt_deep = self.m_Panel_1:getChildByName( NodeNames.TXT_DEEP )"
            in out["lua"])
    assert out["skipped"] == []
    assert _check(workspace, csd, out["lua"]) == []


def test_a_default_named_leaf_is_skipped(studio):
    """`Panel_1` 這種沒改過名字的葉節點不會有人拿名字去取，綁出來只是雜訊。"""
    _, _, out = _generate(studio, _node("bg", _children(_node("Image_3"))),
                          class_name="View")
    assert out["skipped"] == ["Image_3"]
    assert "IMAGE_3" not in out["lua"]

    _, _, everything = _generate(studio, _node("bg", _children(_node("Image_3"))),
                                 class_name="View", include_default=True)
    assert 'IMAGE_3 = "Image_3",' in everything["lua"]


# ------------------------------------------------------------------ 撞名

def test_duplicate_names_get_a_distinct_field_and_are_reported(studio):
    """同名節點掛在不同父層下是合法的，但欄位名不能重複——而且要講出來。"""
    workspace, csd, out = _generate(
        studio,
        _node("left", _children(_node("title")), "PanelObjectData")
        + _node("right", _children(_node("title")), "PanelObjectData"),
        class_name="View")
    assert [(n, was, now) for n, was, now in out["renamed"]] \
        == [("title", "m_title", "m_title_2")]
    lua = out["lua"]
    assert "self.m_title   = self.m_left:getChildByName( NodeNames.TITLE )" in lua
    assert "self.m_title_2 = self.m_right:getChildByName( NodeNames.TITLE )" in lua
    # 兄弟不該被深度優先拆成各自對齊的孤行
    assert "self.m_left    = view:getChildByName( NodeNames.LEFT )" in lua
    assert _check(workspace, csd, out["lua"]) == []


def test_names_that_are_not_identifiers_are_cleaned_up(studio):
    _, _, out = _generate(studio, _node("btn-ok 2"), class_name="View")
    assert 'BTN_OK_2 = "btn-ok 2",' in out["lua"]
    assert "self.m_btn_ok_2 = view:getChildByName( NodeNames.BTN_OK_2 )" in out["lua"]


def test_an_unnamed_node_is_left_alone(studio):
    _, _, out = _generate(studio, _node("", _children(_node("inner"))),
                          class_name="View")
    assert out["bindings"] == 0


def test_a_missing_csd_says_so(studio):
    repo, _, _write = studio
    workspace = projects.resolve(project=str(repo))
    with pytest.raises(Exception) as err:
        bind.build(workspace.csd, "NoSuchCsd")
    assert "NoSuchCsd" in str(err.value)


# --------------------------------------------------------- bind --out 的併發

def test_out_refuses_to_overwrite_and_leaves_the_file_alone(studio, tmp_path):
    """`--out` 不覆蓋既有檔案，而且失敗時不能動到原本的內容。

    用 `open(path, "x")` 而不是「先 `exists` 再 `open("w")`」——後者中間有空隙，
    兩個 agent 同時跑會兩邊都通過檢查、然後互相蓋掉，產出半截的 Lua。
    實測 10 個行程搶寫同一個路徑：1 個成功、9 個乾淨失敗、檔案完整。
    """
    from luadetect import cli

    repo, _, write = studio
    csd = write("ui/View.csd", _node("bg", _children(_node("btn_ok"))))
    target = tmp_path / "Out.lua"

    assert cli.main(["bind", csd, "--project", str(repo), "--name", "View",
                     "--out", str(target)]) == 0
    first = target.read_text(encoding="utf-8")
    assert "return ViewProperties" in first

    # 第二次要失敗，而且**不能動到已經寫好的那份**
    assert cli.main(["bind", csd, "--project", str(repo), "--name", "Other",
                     "--out", str(target)]) == 2
    assert target.read_text(encoding="utf-8") == first


def test_out_writes_a_complete_file(studio, tmp_path):
    """寫出去的必須是完整的 Lua，不是半截。"""
    from luadetect import cli

    repo, _, write = studio
    csd = write("ui/View.csd", _node("bg", _children(_node("btn_ok"))))
    target = tmp_path / "Complete.lua"
    assert cli.main(["bind", csd, "--project", str(repo), "--name", "View",
                     "--out", str(target)]) == 0
    body = target.read_text(encoding="utf-8")
    assert body.startswith("--") and body.rstrip().endswith("return ViewProperties")
    assert "local NodeNames = {" in body and "function ViewProperties:ctor" in body
