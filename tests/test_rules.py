"""規則的測試。

每一條「不該報」的測試都對應到一個實際踩過的誤報：
執行期 setName 建的節點、表格索引存的排程 handle、對到錯的 .csd。
誤報比漏報更會讓工具被關掉，所以這些案例要一直留著。
"""
import json
import os

import pytest

from luadetect import projects, scan
from luadetect.report import ERROR, WARN
from tests.test_csdtree import CSD, NODE


@pytest.fixture
def workspace(tmp_path):
    csd_root = tmp_path / "cocosstudio"
    os.makedirs(csd_root / "ui")
    inner = NODE.format(name="txt", ctype="TextObjectData", inner="")
    with open(csd_root / "ui" / "View.csd", "w", encoding="utf-8") as fh:
        fh.write(CSD.format(name="View", children=NODE.format(
            name="bg", ctype="ImageViewObjectData",
            inner=f"<Children>{inner}</Children>")))

    repo = tmp_path / "repo"
    lua_root = repo / "InannaLua"
    os.makedirs(lua_root / "Modules" / "Demo")
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        json.dump({"csdRoot": str(csd_root)}, fh)
    return repo, lua_root / "Modules" / "Demo"


def _write(folder, name, body):
    path = os.path.join(str(folder), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def _run(repo, groups, explain=False):
    workspace = projects.resolve(project=str(repo))
    return scan.scan(workspace, projects.lua_files(workspace), groups,
                     explain=explain)


def _scan(repo, groups):
    return _run(repo, groups)["findings"]


def test_missing_node_that_gets_used_is_an_error(workspace):
    """取到之後真的被用（對 nil 呼叫方法）——這才是會當機的那一種。"""
    repo, module = workspace
    _write(module, "View.lua",
           'local CSB = "ui/View.csb"\n'
           'function View:Bind( view )\n'
           '    self.m_bg = view:getChildByName( "bg" )\n'
           '    self.m_txt = self.m_bg:getChildByName( "txt" )\n'
           '    self.m_gone = self.m_bg:getChildByName( "nope" )\n'
           'end\n'
           'function View:Show()\n'
           '    self.m_gone:setVisible( true )\n'
           'end\n')
    found = _scan(repo, ["nodes"])
    assert [f.rule for f in found] == ["NODE_MISSING"]
    assert found[0].severity == ERROR
    assert found[0].detail["node"] == "nope"
    assert found[0].detail["dereferenced"] is True
    assert found[0].line == 5


def test_a_missing_node_nobody_reads_is_only_a_warning(workspace):
    """存進欄位卻沒有任何一處讀它——產生出來的綁定檔沒跟著更新，不會當機。

    實測 Inanna：42 筆 error 等級的 NODE_MISSING 裡有 26 筆是這一種
    （`MaintainPage.lua` 的 m_img_bg / m_txt_time / m_update_btn 整個檔案
    各只出現一次，就是賦值那行）。當成 error 擋 CI 是錯的。
    """
    repo, module = workspace
    _write(module, "View.lua",
           'local CSB = "ui/View.csb"\n'
           'function View:Bind( view )\n'
           '    self.m_bg = view:getChildByName( "bg" )\n'
           '    self.m_gone = self.m_bg:getChildByName( "nope" )\n'
           'end\n')
    found = _scan(repo, ["nodes"])
    assert [f.rule for f in found] == ["NODE_MISSING"]
    assert found[0].severity == WARN
    assert found[0].detail["dereferenced"] is False
    assert found[0].detail["assignedTo"] == "self.m_gone"
    assert "沒有人讀它" in found[0].message


def test_a_read_in_another_file_of_the_module_still_counts(workspace):
    """Properties 檔賦值、View 檔讀取是這個專案的常態寫法，範圍要是整個模組。"""
    repo, module = workspace
    _write(module, "ViewProperties.lua",
           'local CSB = "ui/View.csb"\n'
           'function P:Bind( view )\n'
           '    self.m_bg = view:getChildByName( "bg" )\n'
           '    self.m_gone = self.m_bg:getChildByName( "nope" )\n'
           'end\n')
    _write(module, "Other.lua",
           'function Other:Use( p )\n'
           '    p.m_gone:setVisible( false )\n'
           'end\n')
    found = [f for f in _scan(repo, ["nodes"]) if f.rule == "NODE_MISSING"]
    assert [f.severity for f in found] == [ERROR]


def test_runtime_set_name_is_not_reported(workspace):
    repo, module = workspace
    _write(module, "View.lua",
           'local CSB = "ui/View.csb"\n'
           'function View:Build( view )\n'
           '    local made = cc.Node:create()\n'
           '    made:setName( "made_at_runtime" )\n'
           '    view:addChild( made )\n'
           '    local back = view:getChildByName( "made_at_runtime" )\n'
           'end\n')
    assert _scan(repo, ["nodes"]) == []


def test_wrong_csd_suppresses_the_whole_file(workspace):
    """對到的 .csd 跟取用的節點幾乎沒交集時，整份都不報——那不是這個檔案的版面。"""
    repo, module = workspace
    _write(module, "View.lua",
           'local DECOR = "ui/View.csb"\n'
           'function View:Bind( node )\n'
           '    self.a = node:getChildByName( "alpha" )\n'
           '    self.b = node:getChildByName( "beta" )\n'
           '    self.c = node:getChildByName( "gamma" )\n'
           '    self.d = node:getChildByName( "delta" )\n'
           'end\n')
    result = _run(repo, ["nodes"], explain=True)
    assert [f.rule for f in result["findings"]] == ["NODE_CSB_UNRESOLVED"]
    assert result["stats"]["unresolvedCsb"] == 1
    # 預設不 explain 時連這一筆都不出現，報告裡不會有任何雜訊
    assert _scan(repo, ["nodes"]) == []


def test_scheduler_without_unschedule_is_reported(workspace):
    repo, module = workspace
    _write(module, "Timer.lua",
           'function Timer:OnEnter()\n'
           '    self.m_entry = cc.Director:getInstance():getScheduler()'
           ':scheduleScriptFunc( tick, 1, false )\n'
           'end\n')
    found = [f for f in _scan(repo, ["lifecycle"]) if f.rule == "SCHED_LEAK"]
    assert len(found) == 1
    assert found[0].severity == ERROR
    assert found[0].detail["handle"] == "self.m_entry"


def test_scheduler_released_through_a_table_alias_is_not_reported(workspace):
    """註冊寫 self.m_timers[k]、解除時先取別名再寫 t[k]——是正確的程式碼。"""
    repo, module = workspace
    _write(module, "Timer.lua",
           'function Timer:Stop( key )\n'
           '    local timers = self.m_timers\n'
           '    cc.Director:getInstance():getScheduler()'
           ':unscheduleScriptEntry( timers[ key ] )\n'
           'end\n'
           'function Timer:Start( key )\n'
           '    self.m_timers[ key ] = cc.Director:getInstance():getScheduler()'
           ':scheduleScriptFunc( tick, 1, false )\n'
           'end\n')
    assert [f for f in _scan(repo, ["lifecycle"]) if f.rule == "SCHED_LEAK"] == []


def test_event_registered_on_enter_without_release(workspace):
    repo, module = workspace
    _write(module, "Panel.lua",
           'function Panel:Show()\n'
           '    inn.EventManager:AddListener( inn.Events.COIN_CHANGED, self.OnCoin, self )\n'
           'end\n')
    found = [f for f in _scan(repo, ["lifecycle"]) if f.rule == "EVENT_LEAK"]
    assert len(found) == 1
    assert found[0].severity == WARN


def test_event_with_matching_release_is_not_reported(workspace):
    repo, module = workspace
    _write(module, "Panel.lua",
           'function Panel:Show()\n'
           '    inn.EventManager:AddListener( inn.Events.COIN_CHANGED, self.OnCoin, self )\n'
           'end\n'
           'function Panel:Hide()\n'
           '    inn.EventManager:RemoveListener( inn.Events.COIN_CHANGED, self.OnCoin )\n'
           'end\n')
    assert [f for f in _scan(repo, ["lifecycle"]) if f.rule == "EVENT_LEAK"] == []


def test_a_nil_guarded_lookup_is_not_an_error(workspace):
    """`if x == nil then x = ...create() end` 是「取不到就自己建」，完全正確。

    Inanna 最後剩下的 3 筆 error 全部是這個或它的變體（只防護不建），
    一筆真的當機都沒有。
    """
    repo, module = workspace
    _write(module, "View.lua",
           'local CSB = "ui/View.csb"\n'
           'function View:Bind( view )\n'
           '    self.m_bg = view:getChildByName( "bg" )\n'
           '    local img = self.m_bg:getChildByName( "img_main" )\n'
           '    if img == nil then\n'
           '        img = ccui.ImageView:create()\n'
           '    end\n'
           '    img:setVisible( true )\n'
           'end\n')
    found = [f for f in _scan(repo, ["nodes"]) if f.rule == "NODE_MISSING"]
    assert [f.severity for f in found] == [WARN]
    assert found[0].detail["nilGuarded"] is True
    assert "nil 防護" in found[0].message


def test_a_runtime_name_from_a_constant_is_recognised(workspace):
    """`setName( UIName.IMG_FISH )` 跟 `x.name = "..."` 都算執行期建出來的節點。

    原本只認字面字串，於是 AIAutoSettingCellGroup 的 Image_fish_child
    被冤枉成「.csd 裡不存在」——而它下一行就是 setName 建出它。
    """
    repo, module = workspace
    _write(module, "View.lua",
           'local UIName = { IMG_FISH = "img_fish_child" }\n'
           'local CSB = "ui/View.csb"\n'
           'function View:Bind( view )\n'
           '    self.m_bg = view:getChildByName( "bg" )\n'
           '    local img = self.m_bg:getChildByName( UIName.IMG_FISH )\n'
           '    img = ccui.ImageView:create()\n'
           '    img:setName( UIName.IMG_FISH )\n'
           'end\n')
    assert [f for f in _scan(repo, ["nodes"]) if f.rule == "NODE_MISSING"] == []

    _write(module, "Other.lua",
           'local CSB = "ui/View.csb"\n'
           'function Other:Bind( view )\n'
           '    self.m_bg = view:getChildByName( "bg" )\n'
           '    local made = self.m_bg:getChildByName( "made_by_property" )\n'
           '    made = cc.Node:create()\n'
           '    made.name = "made_by_property"\n'
           'end\n')
    assert [f for f in _scan(repo, ["nodes"])
            if f.rule == "NODE_MISSING"
            and f.detail["node"] == "made_by_property"] == []


def test_a_name_that_exists_in_another_csd_is_only_a_warning(workspace, tmp_path):
    """名字在別的 .csd 裡有 → 更可能是我們對到了錯的版面，不是節點被刪了。"""
    repo, module = workspace
    # 在同一個 cocosstudio 根底下再放一份含 btn_elsewhere 的 .csd
    csd_root = tmp_path / "cocosstudio"
    inner = NODE.format(name="btn_elsewhere", ctype="ButtonObjectData", inner="")
    with open(csd_root / "ui" / "Other.csd", "w", encoding="utf-8") as fh:
        fh.write(CSD.format(name="Other", children=inner))
    _write(module, "View.lua",
           'local CSB = "ui/View.csb"\n'
           'function View:Bind( view )\n'
           '    self.m_bg = view:getChildByName( "bg" )\n'
           '    self.m_btn = self.m_bg:getChildByName( "btn_elsewhere" )\n'
           'end\n'
           'function View:Show()\n'
           '    self.m_btn:setVisible( true )\n'
           'end\n')
    found = [f for f in _scan(repo, ["nodes"]) if f.rule == "NODE_MISSING"]
    assert [f.severity for f in found] == [WARN]
    assert found[0].detail["existsElsewhere"] is True
    assert "對到了錯的版面" in found[0].message
