"""MCP 協定的測試——也能直接執行（install.py 在沒有 pytest 時會這樣叫它）。

測的是協定層的行為，不是規則：伺服器不能因為一則爛訊息就整個停掉回應，
那種故障在客戶端只會顯示「連線中」，完全查不出原因。
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luadetect import server, tools    # noqa: E402


class _Capture(io.BytesIO):
    def flush(self):
        pass


def _talk(messages: list) -> list:
    """把幾則訊息餵給伺服器，收回它寫到 stdout 的東西。"""
    stdin = io.BytesIO("\n".join(json.dumps(m) for m in messages).encode("utf-8"))
    out = _Capture()

    class Fake:
        buffer = out
    real_in, real_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = type("S", (), {"buffer": stdin})(), Fake()
    try:
        server.main()
    finally:
        sys.stdin, sys.stdout = real_in, real_out
    return [json.loads(line) for line in
            out.getvalue().decode("utf-8").splitlines() if line.strip()]


def test_initialize_echoes_known_version():
    replies = _talk([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05"}}])
    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"
    assert replies[0]["result"]["serverInfo"]["name"] == "luadetect"


def test_initialize_falls_back_for_unknown_version():
    replies = _talk([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "1999-01-01"}}])
    assert replies[0]["result"]["protocolVersion"] == server.DEFAULT_VERSION


def test_tools_list_matches_the_dispatch_table():
    replies = _talk([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    listed = {t["name"] for t in replies[0]["result"]["tools"]}
    assert listed == {"list_projects", "rules", "scan", "explain",
                      "node_tree", "bind_properties", "verify_parser",
                      "write_baseline"}
    for tool in replies[0]["result"]["tools"]:
        assert tool["description"] and tool["inputSchema"]["type"] == "object"


def test_unknown_tool_is_an_isError_not_a_protocol_error():
    replies = _talk([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "nope", "arguments": {}}}])
    assert replies[0]["result"]["isError"] is True
    assert "nope" in replies[0]["result"]["content"][0]["text"]


def test_bad_arguments_type_is_reported_to_the_model():
    replies = _talk([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "rules", "arguments": ["path"]}}])
    assert replies[0]["result"]["isError"] is True
    assert "arguments" in replies[0]["result"]["content"][0]["text"]


def test_non_object_message_does_not_stop_the_loop():
    replies = _talk([[1, 2, 3],
                     {"jsonrpc": "2.0", "id": 7, "method": "ping"}])
    assert replies and replies[-1]["id"] == 7


def test_notifications_get_no_reply():
    replies = _talk([{"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
    assert len(replies) == 1 and replies[0]["id"] == 2


def test_rules_tool_returns_every_rule():
    payload = tools.call("rules", {})
    assert "NODE_MISSING" in payload["rules"]
    assert "SCHED_LEAK" in payload["rules"]


if __name__ == "__main__":
    failed = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"  ok   {name}")
            except Exception as exc:                     # noqa: BLE001
                failed += 1
                print(f"  FAIL {name}: {exc}")
    print("全部通過。" if not failed else f"{failed} 項失敗。")
    sys.exit(1 if failed else 0)


def test_scan_hands_the_agent_a_table_to_present():
    """agent 不該自己從 summary 重組表格——重組要花 token 而且數字抄錯沒人看得出來。"""
    import os
    import tempfile

    from luadetect import projects, scan as scan_module

    base = tempfile.mkdtemp()
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, "InannaLua"))
    with open(os.path.join(repo, "InannaLua", "a.lua"), "w", encoding="utf-8") as fh:
        fh.write("local a = 1\n")
    payload = tools.call("scan", {"project": repo, "severity": "info"})
    assert "summaryTable" in payload and payload["summaryTable"].startswith("###")
    assert "| 規則 |" in payload["summaryTable"]
    assert "涵蓋率" in payload["summaryTable"]
    assert "present" in payload and "summaryTable" in payload["present"]
    assert "findingsTable" in payload


def test_the_server_instructions_tell_the_agent_to_show_the_table():
    replies = _talk([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18"}}])
    instructions = replies[0]["result"]["instructions"]
    assert "summaryTable" in instructions
    assert "涵蓋率" in instructions


def test_the_scan_tool_description_mentions_presenting_it():
    listed = {t["name"]: t["description"] for t in tools.TOOLS}
    assert "summaryTable" in listed["scan"]
    assert "原樣呈現" in listed["scan"]


def test_the_explain_table_stays_a_valid_gfm_table(tmp_path):
    """訊息若跨行，markdown 表格的欄數會對不齊、整張垮掉（實測發生過）。

    表格分兩個區塊（診斷 3 欄、逐筆問題 5 欄），所以要**分區塊**檢查。
    """
    import json
    import os

    from luadetect import projects

    csd_root = tmp_path / "cocosstudio" / "ui"
    os.makedirs(csd_root)
    with open(csd_root / "V.csd", "w", encoding="utf-8") as fh:
        fh.write('<GameFile><Content ctype="GameProjectContent"><Content>'
                 '<ObjectData Name="Layer" ctype="GameLayerObjectData"><Children>'
                 '<AbstractNodeData Name="bg" ctype="ImageViewObjectData"/>'
                 '</Children></ObjectData></Content></Content></GameFile>')
    repo = tmp_path / "repo"
    module = repo / "InannaLua" / "Modules" / "Demo"
    os.makedirs(module)
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        json.dump({"csdRoot": str(tmp_path / "cocosstudio")}, fh)
    with open(module / "V.lua", "w", encoding="utf-8") as fh:
        fh.write('local CSB = "ui/V.csb"\n'
                 'function V:Bind( v )\n'
                 '    self.m_bg = v:getChildByName( "bg" )\n'
                 '    self.m_x = self.m_bg:getChildByName( "gone" )\n'
                 'end\n'
                 'function V:Show()\n'
                 '    self.m_x:setVisible( true )\n'
                 'end\n'
                 'function V:Hook()\n'
                 '    dispatcher:addEventListener( function( s, e )\n'
                 '        act()\n'
                 '    end, self )\n'
                 'end\n')

    payload = tools.call("explain", {"project": str(repo),
                                     "path": "Modules/Demo/V.lua"})
    blocks, current = [], []
    for line in payload["summaryTable"].splitlines():
        if line.startswith("|"):
            current.append(line.count("|"))
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    assert blocks, payload["summaryTable"]
    for block in blocks:
        assert len(set(block)) == 1, (block, payload["summaryTable"])
    assert "\n" not in "".join(
        item["message"] for item in payload["findings"])
