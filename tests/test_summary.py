"""檢測後的統整表。

對齊是這裡最容易錯的地方：欄位名稱與說明幾乎都是中文，而中文在終端機是雙寬字元，
用 `len()` 去補空白會整張表歪掉。
"""
from luadetect import report
from luadetect.report import ERROR, INFO, WARN, Finding


def _finding(rule, severity, detail=None, line=1):
    return Finding(rule, severity, "a.lua", line, "m", key=f"{rule}{line}",
                   detail=detail or {})


def test_display_width_counts_cjk_as_two_columns():
    assert report.display_width("abc") == 3
    assert report.display_width("節點") == 4
    assert report.display_width("節點 x") == 6
    assert report.display_width("") == 0


def test_table_rows_all_have_the_same_display_width():
    lines = report.table(["規則", "數量"], [["NODE_MISSING", 58], ["提醒", 3]],
                         right={1})
    widths = {report.display_width(line.rstrip()) for line in lines}
    # 每一行的可見寬度不一定相同（尾端空白被 rstrip 掉），但欄位起點要一致
    starts = [line.index("5") if "5" in line else None for line in lines]
    assert lines[1].startswith("----")            # 分隔線
    assert len(widths) >= 1
    # 用一個固定寬度的欄位驗證對齊：數字欄靠右，所以每一行的行尾對齊
    numeric = [line for line in lines[2:]]
    assert report.display_width(numeric[0]) == report.display_width(numeric[1])


def test_table_pads_cjk_correctly():
    lines = report.table(["項目", "數量"], [["掃描的 .lua", 1304], ["讀不到", 0]],
                         right={1})
    body = lines[2:]
    assert report.display_width(body[0]) == report.display_width(body[1])


def test_summarise_counts_by_rule_and_severity():
    findings = [_finding("NODE_MISSING", WARN, {"rooted": False}, 1),
                _finding("NODE_MISSING", ERROR, {"rooted": True,
                                                 "dereferenced": True}, 2),
                _finding("EVENT_LEAK", INFO, line=3),
                _finding("EVENT_LEAK", INFO, line=4)]
    summary = report.summarise(findings, {"files": 10})
    assert summary["byRule"]["NODE_MISSING"] == {ERROR: 1, WARN: 1, INFO: 0}
    assert summary["byRule"]["EVENT_LEAK"][INFO] == 2
    assert summary["totals"] == {ERROR: 1, WARN: 1, INFO: 2}
    assert summary["total"] == 4
    assert summary["exitCode"] == 1


def test_summarise_explains_why_things_are_not_errors():
    findings = [
        _finding("NODE_MISSING", WARN, {"rooted": True, "existsElsewhere": True}, 1),
        _finding("NODE_MISSING", WARN, {"rooted": False}, 2),
        _finding("NODE_MISSING", WARN, {"rooted": True, "dereferenced": False}, 3),
        _finding("NODE_MISSING", WARN, {"rooted": True, "nilGuarded": True}, 4),
        # error 的那筆不該進降級統計
        _finding("NODE_MISSING", ERROR, {"rooted": True, "dereferenced": True}, 5),
    ]
    summary = report.summarise(findings, {})
    assert summary["downgrades"] == {"existsElsewhere": 1, "notRooted": 1,
                                     "notDereferenced": 1, "nilGuarded": 1}


STATS = {"files": 1304, "unreadable": 2, "nodeFiles": 614, "checkedNodes": 582,
         "unresolvedCsb": 32, "resourcePaths": 2541, "checkedResources": 1180,
         "unresolvedResources": 214, "dynamicResources": 1147,
         "templateResources": 156, "fragmentResources": 991}


def test_coverage_is_always_present_so_silence_is_not_read_as_clean():
    """只給「幾筆問題」的話，「沒有回報」會被讀成「都對」。"""
    summary = report.summarise([], STATS)
    for key, value in STATS.items():
        assert summary["coverage"][key] == value, key
    text = "\n".join(report.summary_lines(summary))
    for number in ("1304", "582", "32", "1180", "214"):
        assert number in text
    assert "沒有回報" in text and "都對" in text


def test_every_coverage_row_shows_its_denominator():
    """只給「資源路徑判斷 1180」的話，看的人沒辦法分辨這是「1180 條全驗過」
    還是「1180 條驗過、另外 1361 條根本沒交代」——而這張表存在的唯一理由
    就是回答那個問題。

    所以每一列都要 `檢查了 + 沒檢查 == 總數`，而且總數要印出來。
    """
    rows = report.coverage_rows(STATS)
    assert len(report.COVERAGE_HEADER) == 5
    for label, done, undone, total, why in rows:
        assert done + undone == total, (label, done, undone, total)
        assert why, label

    labels = {row[0]: row for row in rows}
    assert labels["資源路徑"][1:4] == [1180, 1361, 2541]
    # 沒檢查的 1361 條要交代清楚是哪幾種，不能只給一個總數——
    # 只寫「動態組出來的 1147」會讓人推論成「檔名都是 string.format 拼的」，
    # 而實測 `Item` 模組 690 條裡**只有 1 條**是格式樣板，其餘是寫死的檔名。
    why = labels["資源路徑"][4]
    assert "要接前綴的檔名片段 991" in why, why
    assert "格式樣板 156" in why, why
    assert "對不到資源根 214" in why, why
    assert "動態" not in why, why
    assert labels["有節點取用的檔案"][1:4] == [582, 32, 614]

    text = "\n".join(report.summary_lines(report.summarise([], STATS)))
    for number in ("2541", "1361", "614"):
        assert number in text, number


def test_coverage_totals_fall_back_when_a_denominator_is_missing():
    """舊的 stats（沒有 nodeFiles／resourcePaths）不能讓表算出 0 當分母。"""
    rows = report.coverage_rows({"files": 10, "checkedNodes": 4,
                                 "unresolvedCsb": 1, "checkedResources": 7,
                                 "unresolvedResources": 2})
    labels = {row[0]: row for row in rows}
    assert labels["有節點取用的檔案"][3] == 5
    assert labels["資源路徑"][3] == 9


def test_to_text_ends_with_the_summary_table():
    findings = [_finding("NODE_MISSING", WARN, {"rooted": False})]
    text = report.to_text(findings, "/root", {"files": 1})
    assert "統整" in text
    assert "規則" in text and "合計" in text
    assert "離開碼 0" in text


def test_to_json_carries_the_summary_of_everything_not_just_the_listed_page():
    """MCP 只列最嚴重的幾筆，統整要算全部——不然模型會以為只有那幾筆。"""
    findings = ([_finding("NODE_MISSING", WARN, {"rooted": False}, i)
                 for i in range(1, 6)]
                + [_finding("EVENT_LEAK", INFO, line=9)])
    payload = report.to_json(findings[:2], "/root", {"files": 3}, counted=findings)
    assert len(payload["findings"]) == 2
    assert payload["summary"]["total"] == 6
    assert payload["summary"]["byRule"]["NODE_MISSING"][WARN] == 5


def test_exit_code_zero_is_stated_plainly():
    text = "\n".join(report.summary_lines(report.summarise([], {"files": 1})))
    assert "離開碼 0（通過，沒有錯誤等級的問題）" in text


# ---------------------------------------------------------------- 給 agent 的表

def test_summary_markdown_is_a_valid_gfm_table():
    """agent 的回答是 markdown 呈現的，所以給它 markdown 表格而不是對齊的 ASCII。"""
    summary = report.summarise(
        [_finding("NODE_MISSING", WARN, {"rooted": False}),
         _finding("EVENT_LEAK", INFO, line=2)],
        {"files": 7, "checkedNodes": 3, "unresolvedCsb": 1})
    text = report.summary_markdown(summary)
    rows = [line for line in text.splitlines() if line.startswith("|")]
    assert rows, text
    # 每一區塊的表格欄數要一致（GFM 的硬要求）
    blocks, current = [], []
    for line in text.splitlines():
        if line.startswith("|"):
            current.append(line.count("|"))
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    for block in blocks:
        assert len(set(block)) == 1, block
    assert "### 檢測統整" in text
    assert "涵蓋率" in text and "沒有回報" in text
    assert "離開碼 `0`" in text


def test_summary_markdown_keeps_the_coverage_numbers():
    summary = report.summarise([], {"files": 1304, "checkedNodes": 574,
                                    "unresolvedCsb": 32,
                                    "checkedResources": 1180,
                                    "unresolvedResources": 214})
    text = report.summary_markdown(summary)
    for number in ("1304", "574", "32", "1180", "214"):
        assert f"| {number} |" in text, number


def test_why_line_covers_every_rule():
    """表格的「為什麼是這個等級」欄不該出現「見訊息」這種沒說明的字。"""
    cases = [
        _finding("NODE_MISSING", ERROR, {"rooted": True, "dereferenced": True}),
        _finding("NODE_MISSING", WARN, {"rooted": True, "existsElsewhere": True}),
        _finding("NODE_MISSING", WARN, {"rooted": True, "dereferenced": False}),
        _finding("NODE_MISSING", WARN, {"rooted": True, "nilGuarded": True}),
        _finding("NODE_WRONG_PARENT", WARN),
        _finding("SCHED_LEAK", ERROR, {"context": "enter"}),
        _finding("SCHED_LEAK", WARN, {"ownerProtected": True}),
        _finding("SCHED_NO_HANDLE", WARN, {"context": "once"}),
        _finding("EVENT_LEAK", INFO, {"context": "once"}),
        _finding("RES_MISSING", WARN),
        _finding("NODE_CSB_UNRESOLVED", INFO),
    ]
    for finding in cases:
        why = report.why_line(finding)
        assert why and why != "見訊息", (finding.rule, finding.detail)


def test_why_from_dict_matches_why_line():
    """explain 回的是序列化後的 dict，兩邊的說法必須一致。"""
    finding = _finding("NODE_MISSING", WARN, {"rooted": True, "nilGuarded": True})
    assert report.why_from_dict(finding.to_dict()) == report.why_line(finding)


def test_findings_markdown_says_why_for_each_row():
    findings = [_finding("NODE_MISSING", WARN, {"rooted": False}, 5),
                _finding("RES_MISSING", WARN, line=9)]
    text = report.findings_markdown(findings, "/root")
    assert "為什麼是這個等級" in text
    assert "取用鏈起點不確定" in text
    assert "少畫一個元素" in text
    assert text.count("\n|") >= 3


def test_findings_markdown_caps_the_list_and_says_so():
    findings = [_finding("NODE_MISSING", WARN, {"rooted": False}, i)
                for i in range(1, 41)]
    text = report.findings_markdown(findings, "/root", limit=5)
    assert "共 40 筆，只列前 5 筆" in text
    assert text.count("`NODE_MISSING`") == 5


def test_findings_markdown_on_a_clean_result():
    assert report.findings_markdown([], "/root") == "沒有發現問題。"
