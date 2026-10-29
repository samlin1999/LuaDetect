""".csd 解析的測試，用自己造的最小檔案，不依賴任何專案。

最重要的一條是巢狀展開：`ProjectNode` 引用的子檔，它的根節點在執行期**不會**多出
一層——搞錯的話所有跨檔案的取用鏈都會判成錯。
"""
import os

import pytest

from luadetect.csdtree import Project, find_chain, names

CSD = """<GameFile>
  <PropertyGroup Name="{name}" Type="Layer" Version="3.10.0.0" />
  <Content ctype="GameProjectContent">
    <Content>
      <Animation Duration="0" Speed="1.0000" />
      <ObjectData Name="Layer" ctype="GameLayerObjectData">
        <Size X="100" Y="100" />
        <Children>
{children}
        </Children>
      </ObjectData>
    </Content>
  </Content>
</GameFile>
"""

NODE = ('          <AbstractNodeData Name="{name}" ctype="{ctype}">'
        '<Size X="10" Y="10" />{inner}</AbstractNodeData>')


def _write(root, rel, children):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(CSD.format(name=os.path.splitext(os.path.basename(rel))[0],
                            children=children))
    return path


@pytest.fixture
def project(tmp_path):
    root = str(tmp_path / "cocosstudio")
    os.makedirs(root)
    _write(root, "sub/Panel.csd",
           NODE.format(name="inner_bg", ctype="ImageViewObjectData", inner="")
           + NODE.format(name="inner_txt", ctype="TextObjectData", inner=""))
    child = NODE.format(
        name="btn_ok", ctype="ButtonObjectData", inner="")
    _write(root, "main/View.csd",
           NODE.format(name="bg", ctype="ImageViewObjectData",
                       inner=f"<Children>{child}</Children>")
           + NODE.format(name="page", ctype="ProjectNodeObjectData",
                         inner='<FileData Type="Normal" Path="sub/Panel.csd" />'))
    return Project(root)


def test_index_and_resolve(project):
    assert len(project.index) == 2
    assert project.resolve("main/View.csb")
    # 跟專案無關的前綴內建就要剝得掉
    assert project.resolve("res/main/View.csb")
    assert project.resolve("cocosstudio/main/View.csb")
    assert project.by_stem("Panel")


def test_a_project_specific_prefix_only_works_when_it_is_handed_in(project):
    """資源容器的名字是專案的，不是 csdtree 的。

    以前 `inannaresource/inanna/` 寫死在這裡，換一個專案就永遠對不到——
    而對不到只會讓涵蓋率掉，不會有任何錯誤訊息。現在由 `projects` 算出來傳進來。
    """
    assert not project.resolve("AerithResource/Aerith/main/View.csb")
    aware = Project(project.root, ("aerithresource/aerith/",))
    assert aware.resolve("AerithResource/Aerith/main/View.csb")


def test_nested_project_node_has_no_extra_root_level(project):
    tree = project.tree(project.resolve("main/View.csd")[0])
    every = names(tree)
    assert {"bg", "btn_ok", "page", "inner_bg", "inner_txt"} <= every
    # 執行期是 page:getChildByName("inner_bg")，中間沒有子檔根節點那一層
    assert find_chain([tree], ["page", "inner_bg"]) == "ok"
    assert find_chain([tree], ["page", "Layer", "inner_bg"]) != "ok"


def test_find_chain_verdicts(project):
    tree = project.tree(project.resolve("main/View.csd")[0])
    assert find_chain([tree], ["bg", "btn_ok"]) == "ok"
    assert find_chain([tree], ["bg", "nope"]) == "missing"
    # 存在但父節點不對
    assert find_chain([tree], ["page", "btn_ok"]) == "parent"


def test_global_names_cached_and_complete(project, monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    every = project.global_names()
    assert "inner_txt" in every and "btn_ok" in every
    project._names = None
    assert project.global_names() == every        # 第二次走快取，結果要一致


def test_a_project_node_keeps_both_the_referenced_file_and_its_own_children(tmp_path):
    """ProjectNode 引用了別的 .csd，**引用處自己還可以再加子節點**——兩邊都要在。

    執行期 CSLoader 先把被引用檔案的根節點載進來、改名，然後照常把這一層自己的
    Children 也加上去。只取被引用那一邊會把引用處加的節點整批弄丟：實測
    `BottomBarGWS.csd` 的 `n_extraBet` 就是 ProjectNode 底下自己掛著
    `n_extraBetHint`，於是 6 筆 error 等級的誤報說那個節點不存在，而遊戲照樣跑。
    """
    root = str(tmp_path / "cocosstudio")
    os.makedirs(root)
    _write(root, "sub/Panel.csd",
           NODE.format(name="from_file", ctype="ImageViewObjectData", inner=""))
    _write(root, "main/View.csd",
           NODE.format(name="holder", ctype="ProjectNodeObjectData",
                       inner='<FileData Type="Normal" Path="sub/Panel.csd" />'
                             "<Children>"
                             + NODE.format(name="added_here",
                                           ctype="TextObjectData", inner="")
                             + "</Children>"))
    project = Project(root)
    tree = project.tree(project.resolve("main/View.csd")[0])

    assert {"holder", "from_file", "added_here"} <= names(tree)
    assert find_chain([tree], ["holder", "from_file"]) == "ok"
    assert find_chain([tree], ["holder", "added_here"]) == "ok"


def test_sharing_the_expanded_subtree_does_not_leak_inline_children(tmp_path):
    """`_expand` 回傳的是共用快取，接 inline children 時不能就地 extend。"""
    root = str(tmp_path / "cocosstudio")
    os.makedirs(root)
    _write(root, "sub/Panel.csd",
           NODE.format(name="shared", ctype="ImageViewObjectData", inner=""))
    _write(root, "main/A.csd",
           NODE.format(name="a", ctype="ProjectNodeObjectData",
                       inner='<FileData Type="Normal" Path="sub/Panel.csd" />'
                             "<Children>"
                             + NODE.format(name="only_in_a", ctype="TextObjectData",
                                           inner="") + "</Children>"))
    _write(root, "main/B.csd",
           NODE.format(name="b", ctype="ProjectNodeObjectData",
                       inner='<FileData Type="Normal" Path="sub/Panel.csd" />'))
    project = Project(root)
    tree_a = project.tree(project.resolve("main/A.csd")[0])
    tree_b = project.tree(project.resolve("main/B.csd")[0])

    assert "only_in_a" in names(tree_a)
    assert "only_in_a" not in names(tree_b)      # A 的 inline 不可以漏到 B
    assert "shared" in names(tree_b)
