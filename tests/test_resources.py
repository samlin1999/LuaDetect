"""共用資源契約的測試。

這條規則的成立條件只有一個：**只在「所在資料夾解析得到、檔案卻不在裡面」時才報**。
拿掉這個前提實測是 8.67% 誤報（別的資源包根本不在這台機器上），加上之後是 1.18%。
所以「資料夾對不到就跳過」跟「資料夾在才判斷」這兩條測試是這個檔案的核心。
"""
import json
import os

import pytest

from luadetect import projects, restree, scan
from luadetect.report import WARN


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x")


@pytest.fixture
def project(tmp_path):
    """一個有資源根的專案：`InannaResource/Inanna/ui/` 底下有 ok.png。"""
    repo = tmp_path / "repo"
    lua_root = repo / "InannaLua" / "Modules" / "Demo"
    os.makedirs(lua_root)
    _touch(str(repo / "InannaResource" / "Inanna" / "ui" / "ok.png"))
    _touch(str(repo / "InannaResource" / "Inanna" / "ui" / "atlas.plist"))
    _touch(str(repo / "InannaResource" / "Sound" / "bgm.mp3"))
    return repo, lua_root


def _scan(repo, body, name="V.lua"):
    path = os.path.join(str(repo), "InannaLua", "Modules", "Demo", name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    workspace = projects.resolve(project=str(repo))
    return scan.scan(workspace, [path], ["resources"])


def test_a_missing_file_in_a_folder_that_exists_is_reported(project):
    """是 warn 不是 error：資源載不到在 Cocos 只會少畫一個元素，不會當機。

    Inanna 帶著 7 個缺檔正常運作就是證據。
    """
    repo, _ = project
    result = _scan(repo, 'local A = "ui/ok.png"\nlocal B = "ui/gone.png"\n')
    assert [(f.rule, f.severity, f.detail["resource"]) for f in result["findings"]] \
        == [("RES_MISSING", WARN, "ui/gone.png")]
    assert result["stats"]["checkedResources"] == 2


def test_a_folder_that_resolves_nowhere_is_skipped_not_reported(project):
    """別的資源包（或執行期搜尋路徑）根本不在這台機器上——不猜。"""
    repo, _ = project
    result = _scan(repo, 'local A = "VolcanoResource/Common/Img/loading.png"\n')
    assert result["findings"] == []
    assert result["stats"]["unresolvedResources"] == 1
    assert result["stats"]["checkedResources"] == 0


def test_the_bundle_sub_level_is_searched_too(project):
    """執行期用 addSearchPath 掛 `InannaResource/Inanna`，那一層在路徑裡看不到。

    只把 `InannaResource` 當根的話，`ui/ok.png` 會被判成缺檔——實測 30 筆誤報。
    """
    repo, _ = project
    assert _scan(repo, 'local A = "ui/ok.png"\n')["findings"] == []
    assert _scan(repo, 'local A = "Sound/bgm.mp3"\n')["findings"] == []
    # 前綴寫全的也要認得
    assert _scan(repo, 'local A = "InannaResource/Inanna/ui/ok.png"\n')["findings"] == []


@pytest.mark.parametrize("body, why", [
    ('local A = "bs_img_tip_%d.png"\n', "沒有目錄的樣板：那是要接前綴的尾巴"),
    ('local A = "%s/%s.png"\n', "連目錄都是變的樣板"),
    ('local A = ".png"\n', "沒有路徑分隔的片段"),
    ('local A = "_1_l.png"\n', "狀態尾巴"),
    ('local A = "ui/" .. name .. ".png"\n', "串接"),
    ('local A = "ui/gone.png" .. suffix\n', "後面接串接"),
    ('local A = prefix .. "ui/gone.png"\n', "前面接串接"),
    ('-- local A = "ui/gone.png"\n', "註解裡"),
    ('local A = "ui/config.lua"\nlocal B = "ui/data.txt"\n', "不是資源副檔名"),
])
def test_these_are_never_reported(project, body, why):
    repo, _ = project
    assert _scan(repo, body)["findings"] == [], why


def test_csb_is_left_to_the_node_rule(project):
    """`.csb` 是 Cocos Studio 編出來的產物，原始碼樹裡不一定有；節點規則已經在驗它。"""
    repo, _ = project
    assert _scan(repo, 'local A = "ui/View.csb"\n')["findings"] == []
    assert ".csb" not in restree.EXTENSIONS


def test_no_resource_root_means_the_whole_group_is_skipped(tmp_path):
    repo = tmp_path / "bare"
    lua_root = repo / "InannaLua"
    os.makedirs(lua_root)
    path = str(lua_root / "V.lua")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('local A = "ui/gone.png"\n')
    workspace = projects.resolve(project=str(repo))
    assert workspace.resources is None
    assert scan.scan(workspace, [path], ["resources"])["findings"] == []


def test_resource_roots_can_be_configured(tmp_path):
    repo = tmp_path / "repo"
    os.makedirs(repo / "InannaLua")
    _touch(str(repo / "art" / "ui" / "ok.png"))
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        json.dump({"resourceRoots": ["art"]}, fh)
    path = str(repo / "InannaLua" / "V.lua")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('local A = "ui/ok.png"\nlocal B = "ui/gone.png"\n')
    workspace = projects.resolve(project=str(repo))
    result = scan.scan(workspace, [path], ["resources"])
    assert [f.detail["resource"] for f in result["findings"]] == ["ui/gone.png"]


def test_verdict_tries_every_candidate_before_saying_missing(project):
    """試到第一個存在的資料夾就回答，會把根目錄底下的檔案判成缺檔。"""
    repo, _ = project
    bundle = projects.resolve(project=str(repo)).resources
    assert bundle.verdict("ui/ok.png") == "ok"
    assert bundle.verdict("InannaResource/Inanna/ui/ok.png") == "ok"
    assert bundle.verdict("ui/gone.png") == "missing"
    assert bundle.verdict("nowhere/at/all/x.png") == "unresolved"


def test_dynamic_paths_are_counted_not_dropped(project):
    """不該猜的資源路徑要**計數**，不能靜靜丟掉。

    原本 `_skippable` 直接 continue，於是全庫 2541 條資源字串裡有 1147 條
    既不算「判斷完成」也不算「跳過」——表上兩個數字加起來對不上總數，
    看的人沒辦法分辨「7 條全驗過」與「7 條驗過、還有 40 條沒交代」。
    """
    repo, _ = project
    result = _scan(repo,
                   'local A = "ui/ok.png"\n'                # 判斷得到
                   'local B = "ui/%d_icon.png"\n'           # 格式樣板
                   'local C = "prefix_" .. "tail/x.png"\n'  # 被 `..` 串起來
                   'local D = "bare.png"\n')                # 沒有 `/`，是片段
    stats = result["stats"]
    # "ui/%d_icon.png" 的目錄是靜態的 → 現在走形狀檢查，算「檢查了」
    assert stats["checkedResources"] == 2, stats
    assert stats["templateResources"] == 0, stats
    assert stats["fragmentResources"] == 2, stats     # 串接片段、沒有 /
    assert stats["dynamicResources"] == 2, stats
    total = (stats["checkedResources"] + stats["unresolvedResources"]
             + stats["dynamicResources"])
    assert stats["resourcePaths"] == total == 4, stats


def test_node_coverage_denominator_matches_the_rule_gate(project):
    """分母要跟 `check()` 的守門條件是同一個判斷。

    原本 scan 用 `"getChildByName" in code` 當分母，規則卻還認得別的取用寫法，
    於是全庫算出「完成 574 + 跳過 32 = 606」比分母 605 還多一個
    （`GameCommon/GameViewBase.lua` 就是那一個）。
    """
    from luadetect.rules import nodes as node_rules

    assert node_rules.has_node_access('a:getChildByName( "x" )')
    assert not node_rules.has_node_access("local x = 1")

    repo, lua_root = project
    with open(os.path.join(str(lua_root), "Plain.lua"), "w", encoding="utf-8") as fh:
        fh.write("local Plain = {}\nreturn Plain\n")
    with open(os.path.join(str(lua_root), "Uses.lua"), "w", encoding="utf-8") as fh:
        fh.write('local V = {}\nfunction V:B( v )\n'
                 '    self.m_a = v:getChildByName( "a" )\n'
                 'end\n')

    workspace = projects.resolve(project=str(repo))
    stats = scan.scan(workspace, projects.lua_files(workspace))["stats"]
    assert stats["nodeFiles"] == 1, stats
    assert stats["checkedNodes"] + stats["unresolvedCsb"] == stats["nodeFiles"]


# ------------------------------------------------- 格式樣板的形狀檢查

def test_a_template_whose_folder_has_no_matching_file_is_reported(project):
    """不必知道 `%d` 會變成什麼，仍然查得到「這個資料夾裡沒有這種形狀的檔案」。

    守的是一個真實的失敗模式：資料夾被搬走、或檔案整批改了命名。
    實測注入——把 `banner/img/bs_img_tip_%d.png` 改成 `bs_img_hint_%d.png`
    ——立刻被抓到；而全庫跑起來 0 誤報。
    """
    repo, _ = project
    found = _scan(repo, 'local A = "ui/no_such_prefix_%d.png"\n')["findings"]
    assert [(f.rule, f.severity) for f in found] == [("RES_PATTERN_EMPTY", WARN)]
    assert found[0].detail["pattern"] == "ui/no_such_prefix_%d.png"
    assert found[0].detail["folder"] == "ui"


def test_a_template_with_a_matching_file_is_quiet(project):
    """`ui/` 裡有 ok.png，`ok%s.png` 這個形狀對得上就不報。"""
    repo, _ = project
    assert _scan(repo, 'local A = "ui/ok%s.png"\n')["findings"] == []


def test_a_template_without_a_folder_is_not_checked(project):
    """`coin_%d.png` 沒有目錄——它是要接前綴的尾巴，不是路徑。

    少了這道守門，它會落到索引根然後形狀對不上，實測直接產生 30 筆誤報。
    """
    repo, _ = project
    assert _scan(repo, 'local A = "coin_%d.png"\n')["findings"] == []


def test_a_template_reached_through_a_constant_prefix_is_joined(project):
    """`DIR .. "/%s.png"` 要先接上前綴才知道目錄是哪個。

    不接的話目錄會被當成 `/`（索引根），形狀當然對不上——`SkeletonViewer.lua`
    的兩筆假問題就是這樣冒出來的。
    """
    repo, _ = project
    quiet = _scan(repo, 'local DIR = "ui"\nlocal A = DIR .. "/ok%s.png"\n')
    assert quiet["findings"] == [], [f.message for f in quiet["findings"]]

    noisy = _scan(repo, 'local DIR = "ui"\nlocal A = DIR .. "/nope_%d.png"\n')
    assert [f.rule for f in noisy["findings"]] == ["RES_PATTERN_EMPTY"]
    assert noisy["findings"][0].detail["pattern"] == "ui/nope_%d.png"


def test_an_unresolvable_prefix_is_not_guessed(project):
    """前綴是變數就不猜——寧可少報。"""
    repo, _ = project
    assert _scan(repo, 'local A = dir .. "/nope_%d.png"\n')["findings"] == []
