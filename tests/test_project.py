"""全庫不變量：拿真正的 Inanna 跑一次，把「現在報什麼」釘住。

這一份是唯一刻意**不密閉**的測試，專案不在這台機器上就自己跳過。
它存在的理由是：之前這個檢查放在 `%TEMP%` 的手工腳本裡，寫死「全庫 0 筆 error」，
加了一條新規則就過期，而過期的方式是「看起來像工具壞了」——每次都要重新判斷
到底是期望值舊了還是真的壞了。釘住**具體是哪幾筆**就不會有這個問題：
多一筆、少一筆、跑到別的檔案去，diff 都會直接指出來。

`error` 在這個工具裡代表「會炸」。目前全庫**一筆都沒有**。

曾經有 5 筆 `LISTENER_LEAK`，逐一開原始碼查過之後全部是誤報：
`cc.EventListenerCustom` 靠 `removeCustomEventListeners( 事件名 )` 解除，
不需要留著 listener 的參考，而規則漏掉了那個寫法。
（印證 A2：跑得起來的專案裡出現 error，預設它是誤報。）

要往這個集合加東西之前，先確認那一筆真的是「會炸」，不是「寫得不好」，
而且**解除的所有寫法都查過了**——不是只查了規則現在認得的那幾種。
"""
from __future__ import annotations

import os

import pytest

import conftest

from luadetect import projects, report, scan
from luadetect.rules import nodes as node_rules

# (規則, 相對於 lua_root 的路徑, 行號)
EXPECTED_ERRORS: set = set()


@pytest.fixture(scope="module")
def _real_scan():
    """真專案掃一次，整個 module 共用（全庫一次約 7 秒）。

    `monkeypatch` 是 function scope，接不到 module scope 的 fixture，所以這裡
    自己動 `os.environ`——`conftest` 的隔離要在這一份裡被還原掉才找得到真專案。
    """
    saved = {name: os.environ.get(name) for name in conftest.REAL_ENVIRONMENT}
    for name, value in conftest.REAL_ENVIRONMENT.items():
        os.environ.pop(name, None) if value is None else os.environ.update({name: value})
    try:
        try:
            workspace = projects.resolve("Inanna")
        except Exception as exc:                               # noqa: BLE001
            pytest.skip(f"這台機器上沒有 Inanna：{exc}")
        node_rules.reset_caches()
        yield workspace, scan.scan(workspace, projects.lua_files(workspace))
    finally:
        for name, value in saved.items():
            os.environ.pop(name, None) if value is None else os.environ.update({name: value})
        node_rules.reset_caches()


def test_every_project_wide_error_is_a_verified_one(_real_scan):
    workspace, result = _real_scan
    actual = {(f.rule, report.relative(f.file, workspace.lua_root).replace("\\", "/"),
               f.line)
              for f in result["findings"] if f.severity == report.ERROR}
    assert actual == EXPECTED_ERRORS, (
        "全庫 error 清單變了。新增的每一筆都要先開原始碼確認是「會炸」"
        f"才可以加進 EXPECTED_ERRORS。\n多了：{sorted(actual - EXPECTED_ERRORS)}"
        f"\n少了：{sorted(EXPECTED_ERRORS - actual)}")


def test_coverage_is_reported_so_quiet_is_not_read_as_clean(_real_scan):
    """「沒有回報」不等於「都對」——涵蓋率必須有實際數字。"""
    stats = _real_scan[1]["stats"]
    assert stats["files"] > 1000
    for name in ("checkedNodes", "checkedResources"):
        assert stats[name] > 0, name


def test_scanning_twice_gives_the_same_answer(_real_scan):
    """快取跨次掃描不可以改變結果（`reset_caches` 的迴歸）。"""
    workspace, first = _real_scan
    node_rules.reset_caches()
    second = scan.scan(workspace, projects.lua_files(workspace))
    key = lambda found: sorted((f.rule, f.file, f.line, f.key) for f in found)
    assert key(second["findings"]) == key(first["findings"])
