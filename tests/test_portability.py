"""換一個專案還會不會動。

這個工具長在 Inanna 上，所以「Inanna 的形狀」曾經被寫死在三個地方：
Lua 根的候選名（`InannaLua`）、資源容器的名字（`InannaResource`）、
以及剝路徑前綴用的 `inannaresource/inanna/`。三個的失敗方式都一樣糟——
**不會有錯誤訊息**，只是 Lua 根退回整個倉庫、資源規則整組跳過、`.csb` 對不到
`.csd`，通通被算進「沒檢查」。掃出來一片乾淨，看起來就像通過了。

所以這一份用一個叫 `Aerith` 的假專案，確認同一套慣例在別的名字上也成立。
"""
from __future__ import annotations

import json
import os

import pytest

from luadetect import csdtree, projects, restree


def _touch(path: str, text: str = "x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


@pytest.fixture
def aerith(tmp_path):
    """一個照 Inanna 慣例命名、但名字完全不同的專案。"""
    root = tmp_path / "Aerith"
    _touch(str(root / "AerithLua" / "app" / "Main.lua"), "-- lua\n")
    _touch(str(root / "AerithResource" / "Aerith" / "ui" / "btn.png"))
    _touch(str(root / "AerithResource" / "Sound" / "bgm.mp3"))
    _touch(str(root / "cocosstudio" / "main" / "View.csd"), "<GameFile/>")
    return str(root)


def test_the_lua_root_is_found_by_convention_not_by_project_name(aerith):
    assert projects.lua_dir_candidates(aerith) == ["AerithLua"]


def test_a_lowercase_lua_dir_is_still_the_plain_candidate(tmp_path):
    """`lua/` 要走既有的固定清單，不可以被 `*Lua` 的規則重複收一次。"""
    root = tmp_path / "plain"
    os.makedirs(root / "lua")
    assert projects.lua_dir_candidates(str(root)) == ["lua"]


def test_falling_back_to_the_repo_root_is_still_distinguishable(tmp_path):
    """沒有任何 Lua 根候選時要看得出來是「退回」——這是假專案偵測的依據。"""
    root = tmp_path / "nothing"
    os.makedirs(root)
    assert projects.lua_dir_candidates(str(root)) == []
    assert not projects._lua_root_is_certain(str(root), {})


def test_resource_bundles_are_found_by_suffix(aerith):
    assert restree.bundle_dirs(aerith) == ["AerithResource"]


def test_bundle_dirs_are_sorted_so_the_prefix_order_is_stable(tmp_path):
    """順序飄掉 = 前綴順序飄掉 = 尾段比對的對象飄掉，而且完全沒有徵兆。"""
    root = tmp_path / "many"
    for name in ("ZedResource", "AlphaResource", "MidResources"):
        os.makedirs(root / name)
    assert restree.bundle_dirs(str(root)) == ["AlphaResource", "MidResources", "ZedResource"]


def test_prefixes_are_derived_from_the_real_resource_roots(aerith):
    roots = restree.default_roots(aerith, os.path.join(aerith, "cocosstudio"))
    found = projects.path_prefixes(aerith, os.path.join(aerith, "cocosstudio"), roots)
    assert "aerithresource/aerith/" in found
    assert "aerithresource/sound/" in found
    assert "aerithresource/" in found
    # 跟專案無關的那兩個永遠都在
    assert "res/" in found and "cocosstudio/" in found
    # 由長到短：`csdtree.resolve()` 的尾段比對吃的是 tries[-1]，順序不是裝飾
    assert list(found) == sorted(found, key=lambda p: (-len(p), p))


def test_a_resource_root_outside_the_project_is_not_turned_into_a_prefix(tmp_path):
    """cocosstudio 常常在另一個倉庫；`../..` 當前綴剝不掉任何東西，只會變雜訊。"""
    root = tmp_path / "proj"
    os.makedirs(root)
    outside = tmp_path / "elsewhere" / "cocosstudio"
    os.makedirs(outside)
    found = projects.path_prefixes(str(root), str(outside), [str(outside)])
    assert not any(p.startswith("..") for p in found)


def test_path_prefixes_can_be_extended_from_the_config(aerith):
    found = projects.path_prefixes(aerith, None, [],
                                   {"pathPrefixes": ["Legacy/Old/", "assets"]})
    assert "legacy/old/" in found and "assets/" in found


def test_a_renamed_project_resolves_its_csb_end_to_end(aerith, monkeypatch):
    """整條接起來：專案名不同，`AerithResource/Aerith/...` 一樣要對到 .csd。"""
    with open(os.path.join(aerith, ".luadetect.json"), "w", encoding="utf-8") as fh:
        json.dump({"csdRoot": os.path.join(aerith, "cocosstudio")}, fh)
    workspace = projects.resolve(aerith)
    assert os.path.basename(workspace.lua_root) == "AerithLua"
    assert workspace.csd.resolve("AerithResource/Aerith/main/View.csb")


@pytest.fixture
def nested(tmp_path):
    """同一套慣例、但多包一層的倉庫，而且裡面不只一個客戶端。

    照著 ManganDahen 造：`ClientCocos/MDLua`、`ClientCocos/MDResource`，
    外加一個跟 Lua 無關的 `ClientFlash/resource`。
    """
    root = tmp_path / "Dahen"
    _touch(str(root / "ClientCocos" / "MDLua" / "play" / "View.lua"), "-- lua\n")
    _touch(str(root / "ClientCocos" / "MDResource" / "Sound" / "bgm.mp3"))
    _touch(str(root / "ClientFlash" / "resource" / "Lobby" / "old.png"))
    _touch(str(root / "Gt2Server" / "server.lua"), "-- 不是客戶端的\n")
    return str(root)


def test_a_lua_root_one_level_down_is_still_found(nested):
    """只看頂層的話 Lua 根會退回整個倉庫，連 Gt2Server 一起掃——而且不會有訊息。"""
    assert projects.lua_dir_candidates(nested) == ["ClientCocos/MDLua"]


def test_resource_roots_do_not_cross_into_another_client(nested):
    """`ClientFlash/resource` 是 Flash 版的資源。多掛一棵不相干的樹只會製造誤報：
    資料夾突然存在了，本來 unresolved（安靜跳過）的路徑就變成 missing（報出來）。
    """
    lua_root = os.path.join(nested, "ClientCocos", "MDLua")
    roots = restree.default_roots(nested, None, lua_root)
    assert roots, "應該要找得到 MDResource"
    assert any(r.endswith("MDResource") for r in roots)
    assert not any("ClientFlash" in r for r in roots)


def test_a_folder_full_of_projects_is_not_itself_a_project(tmp_path, monkeypatch):
    """往下找兩層讓「容器」也長得像專案根——而且從任何地方往上走都會撞到它。

    實測壞掉的樣子：`Mangan_git/`（底下十幾個 repo）因為兩層下有
    `Inanna/InannaLua` 被認成專案根，於是 lua_root 變成別人整個倉庫。
    """
    container = tmp_path / "workspace"
    for name in ("ProjA", "ProjB"):
        os.makedirs(container / name / f"{name}Lua")
    # 已經確定是專案根之後，往下兩層是對的
    assert projects.lua_dir_candidates(str(container / "ProjA")) == ["ProjALua"]
    # 但容器本身不是
    assert projects.lua_dir_candidates(str(container), depth=1) == []
    monkeypatch.chdir(container)
    assert projects._find_upwards(str(container)) != str(container)


def test_the_search_does_not_descend_into_another_repo(tmp_path):
    """鑽進隔壁 repo 撿它的資源根 = 憑空多一棵資源樹 = 誤報。"""
    root = tmp_path / "outer"
    os.makedirs(root / "vendor" / "TheirResource")
    os.makedirs(root / "vendor" / ".git")
    assert restree.bundle_dirs(str(root)) == []


def test_the_scope_never_climbs_out_of_the_project(tmp_path):
    """Lua 根退回專案根時往上跳一層就跑到專案外面了。"""
    root = tmp_path / "proj"
    os.makedirs(root)
    assert restree.bundle_scope(str(root), str(root)) == str(root)
    assert restree.bundle_scope(str(root), None) == str(root)


def test_the_search_stops_before_it_wanders(tmp_path):
    """深度有上限：再深就開始收 build/ 與第三方目錄，那是誤報的來源。"""
    root = tmp_path / "deep"
    os.makedirs(root / "a" / "b" / "c" / "VendorResource")
    assert restree.bundle_dirs(str(root)) == []


def test_skip_dirs_are_not_searched(tmp_path):
    root = tmp_path / "skipped"
    os.makedirs(root / "build" / "TempResource")
    os.makedirs(root / ".git" / "HookResource")
    assert restree.bundle_dirs(str(root)) == []


def test_csdtree_keeps_no_project_name_of_its_own():
    """低層模組不可以再認得任何一個專案的名字。"""
    for prefix in csdtree.DEFAULT_PREFIXES + restree.DEFAULT_PREFIXES:
        assert "inanna" not in prefix and "aerith" not in prefix
