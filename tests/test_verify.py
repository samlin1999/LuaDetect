"""跟 csdview 交叉比對的測試。

用一個假的 csdview（真的子行程、真的 JSON）來測，而不是把 `_csdview_names` 換掉
——那樣就沒測到 JSON 解析與離開碼處理，而那兩件事才是會出錯的地方。
"""
import json
import os
import stat
import sys

import pytest

from luadetect import verify
from luadetect.csdtree import Project
from tests.test_csdtree import CSD, NODE, _write


FAKE = '''\
# -*- coding: utf-8 -*-
"""假的 csdview：把 TREE 這份固定的 JSON 印出來。"""
import json, sys
TREE = {tree!r}
if "--json" not in sys.argv:
    sys.exit("這個假的只支援 --json")
sys.stdout.reconfigure(encoding="utf-8")
print(json.dumps({{"file": sys.argv[2], "tree": TREE}}, ensure_ascii=False))
'''


def _fake_csdview(tmp_path, tree, exit_code=0, garbage=False):
    """做一個會印出指定 JSON 的假 csdview，回傳它的路徑。"""
    script = tmp_path / "fake_csdview.py"
    body = FAKE.format(tree=tree)
    if garbage:
        body = ("import sys\n"
                "sys.stdout.write('this is not json')\n")
    if exit_code:
        body = f"import sys\nsys.exit({exit_code})\n"
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(body)
    launcher = tmp_path / ("run_fake.cmd" if os.name == "nt" else "run_fake.sh")
    with open(launcher, "w", encoding="utf-8", newline="\n") as fh:
        if os.name == "nt":
            fh.write(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n')
        else:
            fh.write(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IEXEC)
    return str(launcher)


@pytest.fixture
def project(tmp_path):
    """一份 .csd：`Layer/bg/btn`，另外有一個 ProjectNode 引用的子檔。"""
    root = str(tmp_path / "cocosstudio")
    os.makedirs(root)
    _write(root, "sub/Panel.csd",
           NODE.format(name="inner", ctype="ImageViewObjectData", inner=""))
    _write(root, "main/View.csd",
           NODE.format(name="bg", ctype="ImageViewObjectData",
                       inner="<Children>"
                             + NODE.format(name="btn", ctype="ButtonObjectData",
                                           inner="")
                             + "</Children>")
           + NODE.format(name="holder", ctype="ProjectNodeObjectData",
                         inner='<FileData Type="Normal" Path="sub/Panel.csd" />'))
    return Project(root), os.path.join(root, "main", "View.csd")


def _node(name, children=()):
    return {"name": name, "children": [dict(c) for c in children]}


def test_no_gap_when_both_agree(project, tmp_path):
    proj, path = project
    # csdview 保留被引用檔案的根節點那一層（Layer），這裡刻意不保留——不算缺口
    tree = _node("Layer", [
        _node("bg", [_node("btn")]),
        _node("holder", [_node("Layer", [_node("inner")])]),
    ])
    result = verify.compare(proj, [path], _fake_csdview(tmp_path, tree))
    assert result == {"checked": 1, "failed": 0, "gaps": [], "errors": []}


def test_a_node_csdview_has_and_we_miss_is_a_gap(project, tmp_path):
    """這個方向才是會害人的：我們少一個節點 → 誤報「節點不存在」。

    ProjectNode 的 inline children 就是這樣漏掉的，造成 9 筆 error 等級的誤報。
    """
    proj, path = project
    tree = _node("Layer", [
        _node("bg", [_node("btn"), _node("added_at_reference_site")]),
        _node("holder", [_node("Layer", [_node("inner")])]),
    ])
    result = verify.compare(proj, [path], _fake_csdview(tmp_path, tree))
    assert result["checked"] == 1
    assert [g["missing"] for g in result["gaps"]] == [["added_at_reference_site"]]
    assert result["gaps"][0]["extra"] == []


def test_a_node_only_we_have_is_also_a_gap(project, tmp_path):
    proj, path = project
    tree = _node("Layer", [_node("bg", [_node("btn")])])       # 少了 holder 那一支
    result = verify.compare(proj, [path], _fake_csdview(tmp_path, tree))
    assert set(result["gaps"][0]["extra"]) == {"holder", "inner"}


def test_a_broken_csdview_run_does_not_stop_the_batch(project, tmp_path):
    proj, path = project
    result = verify.compare(proj, [path], _fake_csdview(tmp_path, {}, exit_code=3))
    assert result["checked"] == 0 and result["failed"] == 1
    assert "csdview 讀不了" in result["errors"][0]


def test_non_json_output_is_reported_not_swallowed(project, tmp_path):
    proj, path = project
    result = verify.compare(proj, [path], _fake_csdview(tmp_path, {}, garbage=True))
    assert result["failed"] == 1
    assert "不是 JSON" in result["errors"][0]


def test_a_missing_csdview_says_so_and_does_not_break_scan(project, monkeypatch):
    proj, path = project
    monkeypatch.setattr(verify.shutil, "which", lambda name: None)
    assert verify.available() is None
    with pytest.raises(verify.VerifyError) as exc:
        verify.compare(proj, [path])
    assert "不影響 scan" in str(exc.value)


def test_the_dropped_root_level_is_computed_from_the_tree(project):
    """要扣掉的是「這棵樹用到的來源檔案」各自的根節點名，不是去掃全庫。"""
    proj, path = project
    tree = proj.tree(path)
    assert verify._dropped_root_names(proj, tree) == {"Layer"}
