"""每一條都對應一個實際找到並修掉的 bug。

留著的理由跟 test_rules.py 一樣：這些不是假想的邊界，是真的踩過的。
名字寫得長一點，測試掛掉的時候看名字就知道退回了哪個行為。
"""
import builtins
import json
import os
import subprocess
import sys

import pytest

from luadetect import csdtree, luasrc, projects, report, scan
from luadetect.csdtree import Project
from luadetect.report import ERROR, INFO, WARN, Finding
from luadetect.rules import lifecycle

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------- luasrc

def test_handle_inside_a_table_constructor_is_not_a_handle():
    """`self.m_pack = { timer = ...schedule() }` 指派給 self.m_pack 的是那張表。

    原本只數圓括號，於是把 self.m_pack 當成 handle，解除寫 self.m_pack.timer
    也對不上，變成一筆假的「排程沒解除」。
    """
    code = "self.m_pack = { timer = s:scheduleScriptFunc( f, 1, false ) }"
    assert luasrc.assign_target(code, code.index("scheduleScriptFunc")) is None


def test_assign_target_still_reaches_across_a_long_receiver():
    code = "self.m_entry = cc.Director:getInstance():getScheduler():scheduleScriptFunc("
    assert luasrc.assign_target(code, code.index("scheduleScriptFunc")) == "self.m_entry"


def test_enclosing_function_with_a_prebuilt_index_matches_the_slow_path():
    code = "\n".join(f"function View:F{i}()\n  doThing()\nend" for i in range(20))
    table = luasrc.function_index(code)
    for offset in range(0, len(code), 7):
        assert (luasrc.enclosing_function(code, offset, table)
                == luasrc.enclosing_function(code, offset))


def test_enclosing_function_handles_offset_zero_and_empty_files():
    assert luasrc.enclosing_function("", 0) is None
    assert luasrc.enclosing_function("function A:B()\nx()\nend", 0) is None


# ---------------------------------------------------------------- csdtree

CSD = ('<GameFile><Content ctype="GameProjectContent"><Content>'
       '<ObjectData Name="{name}" ctype="GameLayerObjectData"><Children>{children}'
       '</Children></ObjectData></Content></Content></GameFile>')
NODE = '<AbstractNodeData Name="{name}" ctype="{ctype}">{inner}</AbstractNodeData>'


def _node(name, inner="", ctype="ImageViewObjectData"):
    return NODE.format(name=name, ctype=ctype, inner=inner)


def _project_node(name, path):
    return _node(name, f'<FileData Type="Normal" Path="{path}" />',
                 "ProjectNodeObjectData")


def _write_csd(root, rel, name, children):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(CSD.format(name=name, children=children))
    return path


def test_a_failed_build_does_not_poison_the_tree_cache(tmp_path, monkeypatch):
    """非 CsdError 的例外原本會讓 `<loading>` 空節點永久留在快取裡。

    之後這份 .csd 的每個節點都會被判成不存在——長駐的 MCP 伺服器會吞掉例外繼續跑，
    然後安靜地回報一整頁假問題。
    """
    path = _write_csd(tmp_path, "A.csd", "A", _node("bg"))
    project = Project(str(tmp_path))

    def boom(self, csd_path, visiting):
        raise RuntimeError("simulated failure inside _build")

    monkeypatch.setattr(Project, "_build", boom)
    with pytest.raises(RuntimeError):
        project.tree(path)
    monkeypatch.undo()

    tree = project.tree(path)
    assert tree.name == "A"
    assert [c.name for c in tree.children] == ["bg"]


def test_nested_expansion_is_parsed_once_and_shared(tmp_path):
    """公用元件被多處引用時只解析一次（實測原本 itemcell.csd 被解析 92 次）。"""
    _write_csd(tmp_path, "Leaf.csd", "Leaf", _node("leaf_bg"))
    _write_csd(tmp_path, "Mid.csd", "Mid", _project_node("leafA", "Leaf.csd"))
    _write_csd(tmp_path, "Top.csd", "Top",
               _project_node("m1", "Mid.csd") + _project_node("m2", "Mid.csd")
               + _project_node("l", "Leaf.csd"))
    project = Project(str(tmp_path))

    parsed = []
    real = csdtree._object_data
    csdtree._object_data = lambda p: (parsed.append(os.path.basename(p)), real(p))[1]
    try:
        tree = project.tree(os.path.join(str(tmp_path), "Top.csd"))
    finally:
        csdtree._object_data = real

    assert parsed.count("Leaf.csd") == 1
    assert parsed.count("Mid.csd") == 1
    for chain in (["m1", "leafA", "leaf_bg"], ["m2", "leafA", "leaf_bg"],
                  ["l", "leaf_bg"]):
        assert csdtree.find_chain([tree], chain) == "ok"


@pytest.mark.parametrize("order", [("A.csd", "C.csd"), ("C.csd", "A.csd")])
def test_a_cycle_truncated_subtree_is_never_reused(tmp_path, order):
    """A→B→A 時 B 之下的展開被截斷，那份截斷的子樹不能被 C→B 重用。

    兩種順序都要得到同樣的答案，否則就是快取洩漏了「從某個上層看過去」的結果。
    """
    _write_csd(tmp_path, "A.csd", "A", _node("a_bg") + _project_node("toB", "B.csd"))
    _write_csd(tmp_path, "B.csd", "B", _node("b_bg") + _project_node("toA", "A.csd"))
    _write_csd(tmp_path, "C.csd", "C", _project_node("useB", "B.csd"))
    project = Project(str(tmp_path))

    trees = {name: project.tree(os.path.join(str(tmp_path), name)) for name in order}

    def children_of(tree, name):
        found = csdtree.name_index(tree).get(name) or []
        assert len(found) == 1, name
        return {child.name for child in found[0].children}

    # C 走進 B 再走進 A，這一路沒有循環，A 的內容必須完整展開
    assert children_of(trees["C.csd"], "toA") == {"a_bg", "toB"}
    assert csdtree.find_chain([trees["C.csd"]], ["useB", "toA", "a_bg"]) == "ok"
    # A 走進 B 又要走回 A，停在 toA，底下是空的
    assert children_of(trees["A.csd"], "toA") == set()
    # 兩種建立順序要得到同一個答案，否則就是快取洩漏了被截斷的那份
    assert csdtree.find_chain([trees["A.csd"]], ["toB", "toA", "a_bg"]) == "parent"


def test_signature_notices_an_offsetting_pair_of_mtime_changes(tmp_path):
    """原本簽章是「檔案數 + mtime 總和」，兩個檔案往相反方向各動一樣的量會碰撞。"""
    first = _write_csd(tmp_path, "One.csd", "One", _node("a"))
    second = _write_csd(tmp_path, "Two.csd", "Two", _node("b"))
    project = Project(str(tmp_path))
    before = project._signature()
    os.utime(first, (1_000_000, 1_000_000))
    os.utime(second, (3_000_000, 3_000_000))
    after = project._signature()
    # 總和相同（2_000_000 + 2_000_000 == 1_000_000 + 3_000_000），簽章仍要不同
    os.utime(first, (2_000_000, 2_000_000))
    os.utime(second, (2_000_000, 2_000_000))
    assert project._signature() != after != before


def test_find_chain_reports_missing_when_the_chain_start_is_unknown(tmp_path):
    _write_csd(tmp_path, "V.csd", "V", _node("bg", f"<Children>{_node('btn')}</Children>"))
    project = Project(str(tmp_path))
    tree = project.tree(os.path.join(str(tmp_path), "V.csd"))
    assert csdtree.find_chain([tree], ["bg", "btn"]) == "ok"
    assert csdtree.find_chain([tree], ["bg", "nope"]) == "missing"
    # 末端存在但父節點不對 → parent
    assert csdtree.find_chain([tree], ["V", "btn"]) == "parent"
    # 起點整棵樹都沒有 → missing，即使末端名稱存在
    assert csdtree.find_chain([tree], ["ghost", "btn"]) == "missing"


# ---------------------------------------------------------------- lifecycle

class _Workspace:
    lua_root = "."
    csd_root = None
    config: dict = {}


def _lifecycle(source):
    return lifecycle.check("Timer.lua", source, _Workspace())


def test_a_file_using_only_the_safe_wrapper_is_still_checked():
    """`"scheduleScriptFunc" in code` 大小寫敏感，對 SafeScheduleScriptFunc 不成立。

    於是只用專案建議寫法的檔案，整條排程規則被靜默跳過。
    """
    found = _lifecycle("function View:OnEnter()\n"
                       "    self.m_t = SafeScheduleScriptFunc( nil, f, 1, false )\n"
                       "end\n")
    assert [f.rule for f in found] == ["SCHED_LEAK"]
    assert found[0].severity == ERROR          # owner 是 nil，沒有自動解除保護


def test_a_safe_schedule_bound_to_an_owner_is_a_warning_not_an_error():
    """綁了 owner 的話 wrapper 會在 owner 失效時自己解除，不是野指標。"""
    found = _lifecycle("function View:OnEnter()\n"
                       "    self.m_t = SafeScheduleScriptFunc( self, f, 1, false )\n"
                       "end\n")
    assert [(f.rule, f.severity) for f in found] == [("SCHED_LEAK", WARN)]
    assert found[0].detail["ownerProtected"] is True


def test_a_released_safe_schedule_is_not_reported():
    assert _lifecycle("function View:OnEnter()\n"
                      "    self.m_t = SafeScheduleScriptFunc( self, f, 1, false )\n"
                      "end\n"
                      "function View:OnExit()\n"
                      "    SafeUnschedule( self.m_t )\n"
                      "end\n") == []


def test_two_timer_tables_with_the_same_key_do_not_cancel_each_other_out():
    """原本 handle 只留索引部分，`self.m_a[k]` 與 `self.m_b[k]` 變成同一個。"""
    found = _lifecycle(
        "function View:OnEnter()\n"
        "    self.m_tickTimer[ k ] = s:scheduleScriptFunc( f, 1, false )\n"
        "    self.m_flyTimer[ k ] = s:scheduleScriptFunc( f, 1, false )\n"
        "end\n"
        "function View:OnExit()\n"
        "    local t = self.m_tickTimer\n"
        "    s:unscheduleScriptEntry( t[ k ] )\n"
        "end\n")
    assert [f.detail["handle"] for f in found] == ["self.m_flyTimer[ k ]"]


def test_the_table_alias_case_still_passes():
    """註冊寫 self.m_timers[k]、解除時先取別名再寫 t[k]——正確的程式碼。"""
    assert _lifecycle("function Timer:Start( key )\n"
                      "    self.m_timers[ key ] = s:scheduleScriptFunc( f, 1, false )\n"
                      "end\n"
                      "function Timer:Stop( key )\n"
                      "    local timers = self.m_timers\n"
                      "    s:unscheduleScriptEntry( timers[ key ] )\n"
                      "end\n") == []


def test_schedule_stats_do_not_subtract_the_safe_calls_twice():
    numbers = lifecycle.stats(
        "local a = s:scheduleScriptFunc( f, 1, false )\n"
        "local b = SafeScheduleScriptFunc( self, f, 1, false )\n"
        "local c = SafeScheduleScriptFunc( self, f, 1, false )\n"
        "local d = SafeScheduleScriptFunc( self, f, 1, false )\n")
    assert numbers == {"scheduleRaw": 1, "scheduleSafe": 3}


# ---------------------------------------------------------------- report

def test_save_baseline_is_atomic(tmp_path):
    """半截的基準檔會被讀成「沒有基準」，既有問題全部變成新問題。"""
    path = str(tmp_path / "b.json")
    findings = [Finding("NODE_MISSING", ERROR, f"f{i}.lua", i, "m", key=f"k{i}")
                for i in range(500)]
    report.save_baseline(path, findings, str(tmp_path))
    first = report.load_baseline(path)
    assert len(first) == 500
    # 再寫一次；寫入過程中的任何時刻，磁碟上的那份都必須是完整的舊版或完整的新版
    report.save_baseline(path, findings[:100], str(tmp_path))
    assert len(report.load_baseline(path)) == 100
    assert not [n for n in os.listdir(str(tmp_path)) if n.endswith(".tmp")]


def test_a_missing_baseline_is_empty_but_an_unreadable_one_is_an_error(tmp_path):
    """兩件事不能混：回空集合等於「既存問題全部變成新問題」。"""
    missing = str(tmp_path / "nope.json")
    assert report.load_baseline(missing) == set()

    broken = tmp_path / "broken.json"
    with open(broken, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    with pytest.raises(ValueError) as exc:
        report.load_baseline(str(broken))
    assert "--no-baseline" in str(exc.value)          # 訊息要寫得能照著解決

    wrong_shape = tmp_path / "list.json"
    with open(wrong_shape, "w", encoding="utf-8") as fh:
        fh.write("[1, 2, 3]")
    with pytest.raises(ValueError):
        report.load_baseline(str(wrong_shape))


def test_a_locked_baseline_is_retried_not_treated_as_absent(tmp_path, monkeypatch):
    """Windows 上另一個行程 os.replace 時，讀取端的 open 會拿到 sharing violation。"""
    path = tmp_path / "b.json"
    report.save_baseline(str(path), [Finding("R", ERROR, "a.lua", 1, "m", key="k")],
                         str(tmp_path))
    real_open = builtins.open
    attempts = {"n": 0}

    def flaky_open(*args, **kwargs):
        if args and str(args[0]) == str(path):
            attempts["n"] += 1
            if attempts["n"] <= 3:
                raise PermissionError(13, "simulated sharing violation")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)
    got = report.load_baseline(str(path))
    monkeypatch.undo()
    assert len(got) == 1                    # 重試之後讀到了，不是回空集合
    assert attempts["n"] == 4


def test_a_baseline_write_never_leaves_a_partial_file(tmp_path):
    """並發讀寫：讀到的永遠是完整的舊版或完整的新版，寫入端也不能炸掉。"""
    import threading
    import time as _time

    path = str(tmp_path / "b.json")
    findings = [Finding("R", ERROR, f"f{i}.lua", i, "m", key=f"k{i}")
                for i in range(400)]
    report.save_baseline(path, findings, str(tmp_path))

    problems: list = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            try:
                report.save_baseline(path, findings, str(tmp_path))
            except Exception as exc:                       # noqa: BLE001
                problems.append(f"寫入端: {exc!r}")
                return
            _time.sleep(0.005)

    def reader():
        while not stop.is_set():
            try:
                if len(report.load_baseline(path)) != 400:
                    problems.append("讀到不完整的基準")
                    return
            except Exception as exc:                       # noqa: BLE001
                problems.append(f"讀取端: {exc!r}")
                return

    threads = [threading.Thread(target=writer), threading.Thread(target=reader),
               threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    _time.sleep(1.5)
    stop.set()
    for thread in threads:
        thread.join()
    assert not problems, problems[:3]
    assert not [n for n in os.listdir(str(tmp_path)) if n.endswith(".tmp")]


def test_relative_survives_a_cross_drive_path():
    """Windows 上跨磁碟的 relpath 會拋 ValueError，指紋算不出來等於基準失效。"""
    got = report.relative(r"D:\elsewhere\a.lua", r"C:\project\lua")
    assert got == "D:/elsewhere/a.lua"
    finding = Finding("NODE_MISSING", ERROR, r"D:\elsewhere\a.lua", 1, "m", key="k")
    assert finding.fingerprint(r"C:\project\lua").endswith("|NODE_MISSING|k")


def test_counts_cover_every_finding_not_just_the_listed_ones():
    """MCP 只列最嚴重的 50 筆，計數卻要算全部，不然模型會以為沒有警告。"""
    findings = ([Finding("A", ERROR, "a.lua", 1, "m")] * 2
                + [Finding("B", WARN, "b.lua", 1, "m")] * 3
                + [Finding("C", INFO, "c.lua", 1, "m")] * 4)
    payload = report.to_json(findings[:2], "/root", {}, counted=findings)
    assert payload["counts"] == {"error": 2, "warn": 3, "info": 4}
    assert len(payload["findings"]) == 2


# ---------------------------------------------------------------- scan / git

def _git(repo, *args):
    # 一定要指定 encoding：hook 的輸出是 UTF-8，用 locale（cp950）解會炸
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    lua = repo / "InannaLua"
    os.makedirs(lua)
    _git(str(repo), "init", "-q")
    _git(str(repo), "config", "user.email", "t@example.com")
    _git(str(repo), "config", "user.name", "t")
    with open(lua / "committed.lua", "w", encoding="utf-8") as fh:
        fh.write("local a = 1\n")
    _git(str(repo), "add", ".")
    _git(str(repo), "commit", "-qm", "init")
    return projects.Workspace(str(repo), str(lua), None, "test", {})


def test_a_missing_base_ref_raises_instead_of_reporting_no_changes(git_repo):
    """回空清單的話 CLI 會印「沒有異動」然後 exit 0——CI 從第一天起永遠綠燈。

    GitHub Actions 預設是淺層 clone，`origin/main` 根本不存在，正好踩到這條。
    """
    with pytest.raises(projects.ProjectError) as exc:
        scan.changed_files(git_repo, "origin/definitely-not-a-ref")
    assert "fetch" in str(exc.value)          # 訊息要寫得能照著解決


def test_staged_mode_ignores_unstaged_edits(git_repo):
    """pre-commit 該看的是這次要提交的內容，不是工作目錄裡還沒 add 的改動。"""
    lua = os.path.join(git_repo.directory, "InannaLua")
    with open(os.path.join(lua, "staged.lua"), "w", encoding="utf-8") as fh:
        fh.write("local b = 2\n")
    _git(git_repo.directory, "add", "InannaLua/staged.lua")
    with open(os.path.join(lua, "unstaged.lua"), "w", encoding="utf-8") as fh:
        fh.write("local c = 3\n")
    _git(git_repo.directory, "add", "-N", "InannaLua/unstaged.lua")

    staged = [os.path.basename(p) for p in scan.changed_files(git_repo, staged=True)]
    both = [os.path.basename(p) for p in scan.changed_files(git_repo)]
    assert staged == ["staged.lua"]
    assert "unstaged.lua" in both


def test_git_missing_is_an_error_not_a_pass(git_repo, monkeypatch):
    def no_git(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", no_git)
    with pytest.raises(projects.ProjectError):
        scan.changed_files(git_repo)


# ---------------------------------------------------------------- CLI 離開碼

def test_an_unexpected_exception_exits_two_not_one():
    """1 的意思是「找到錯誤等級的問題」。工具當機用 1 會被 CI 讀成檢查失敗。"""
    stub = (
        "import sys\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "from luadetect import scan, cli\n"
        "def boom(*a, **k): raise KeyError('simulated internal failure')\n"
        "scan.scan = boom\n"
        "sys.exit(cli.main(['scan', '--project', '.']))\n")
    done = subprocess.run([sys.executable, "-c", stub], capture_output=True,
                          text=True, errors="replace", cwd=ROOT)
    assert done.returncode == 2, done.stderr[-400:]


def test_exit_code_one_still_means_findings(tmp_path):
    findings = [Finding("NODE_MISSING", ERROR, "a.lua", 1, "m")]
    assert report.exit_code(findings) == 1
    assert report.exit_code([Finding("X", WARN, "a.lua", 1, "m")]) == 0


# ---------------------------------------------------------------- 快取失效

def test_module_dir_never_climbs_out_of_the_lua_root(tmp_path):
    """放在 lua_root 正下方的檔案原本會讓 _module_dir 一路爬到磁碟根目錄。

    接著 `_module_facts` 會 `os.walk("C:\\")` 把整顆磁碟走一遍。
    """
    from luadetect.rules.nodes import _module_dir

    lua_root = tmp_path / "InannaLua"
    deep = lua_root / "Modules" / "Lobby" / "sub"
    os.makedirs(deep)
    assert _module_dir(str(lua_root / "GlobalFunc.lua"), str(lua_root)) == str(lua_root)
    assert (_module_dir(str(deep / "A.lua"), str(lua_root))
            == str(lua_root / "Modules" / "Lobby"))
    assert (_module_dir(str(lua_root / "GameCommon" / "H.lua"), str(lua_root))
            == str(lua_root / "GameCommon"))
    # lua_root 之外的檔案以它自己的資料夾為界，不往上爬
    outside = tmp_path / "elsewhere" / "X.lua"
    assert _module_dir(str(outside), str(lua_root)) == str(tmp_path / "elsewhere")


def test_a_file_directly_in_the_lua_root_does_not_walk_the_whole_disk(tmp_path):
    """回歸的實測版：掃一個放在 lua_root 正下方、又對得到 .csd 的檔案。"""
    csd_root = tmp_path / "cocosstudio"
    _write_csd(csd_root, "ui/Solo.csd", "Solo", _node("bg"))
    repo = tmp_path / "repo"
    lua_root = repo / "InannaLua"
    os.makedirs(lua_root)
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        json.dump({"csdRoot": str(csd_root)}, fh)
    solo = lua_root / "Solo.lua"
    with open(solo, "w", encoding="utf-8") as fh:
        fh.write('local CSB = "ui/Solo.csb"\n'
                 'function M:Bind( v )\n'
                 '    self.m_bg = v:getChildByName( "bg" )\n'
                 '    self.m_no = v:getChildByName( "nope" )\n'
                 'end\n')
    workspace = projects.resolve(project=str(repo))
    found = scan.scan(workspace, [str(solo)])["findings"]
    assert [f.detail.get("node") for f in found] == ["nope"]


def test_module_caches_are_dropped_at_the_start_of_every_scan(tmp_path):
    """MCP 是長駐行程：改完 Lua 再 scan 一次必須看到新內容。

    原本 `_runtime_cache` 沒有任何失效條件，補上 setName 之後那筆 NODE_MISSING
    還是照報，要重開伺服器才會消失——而「改完用 scan 確認」正是 MCP 的主要用法。
    """
    csd_root = tmp_path / "cocosstudio"
    _write_csd(csd_root, "ui/View.csd", "View", _node("bg"))
    repo = tmp_path / "repo"
    module = repo / "InannaLua" / "Modules" / "Demo"
    os.makedirs(module)
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        json.dump({"csdRoot": str(csd_root)}, fh)
    view = module / "View.lua"
    with open(view, "w", encoding="utf-8") as fh:
        fh.write('local CSB = "ui/View.csb"\n'
                 'function View:Bind( v )\n'
                 '    self.m_bg = v:getChildByName( "bg" )\n'
                 '    self.m_dyn = v:getChildByName( "dyn_btn" )\n'
                 'end\n')

    workspace = projects.resolve(project=str(repo))
    first = scan.scan(workspace, [str(view)])["findings"]
    assert [f.detail.get("node") for f in first] == ["dyn_btn"]

    # 另一個檔案在執行期把 dyn_btn 建出來——同一個行程再掃就該乾淨了
    with open(module / "Builder.lua", "w", encoding="utf-8") as fh:
        fh.write('local b = cc.Node:create()\nb:setName( "dyn_btn" )\n')
    again = scan.scan(projects.resolve(project=str(repo)), [str(view)])["findings"]
    assert again == []


def test_explain_accepts_the_same_relative_path_in_cli_and_mcp(tmp_path, monkeypatch):
    """MCP 會把相對路徑對到 lua_root，CLI 原本只以 cwd 為準——同一個路徑一邊行一邊不行。"""
    from luadetect import tools

    csd_root = tmp_path / "cocosstudio"
    _write_csd(csd_root, "ui/V.csd", "V", _node("bg"))
    repo = tmp_path / "repo"
    module = repo / "InannaLua" / "Modules" / "Demo"
    os.makedirs(module)
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        json.dump({"csdRoot": str(csd_root)}, fh)
    with open(module / "V.lua", "w", encoding="utf-8") as fh:
        fh.write('local CSB = "ui/V.csb"\n'
                 'function V:Bind( v )\n    self.b = v:getChildByName( "bg" )\nend\n')

    rel = "Modules/Demo/V.lua"
    workspace = projects.resolve(project=str(repo))
    monkeypatch.chdir(tmp_path)                  # cwd 刻意不在專案裡
    from_cli = scan.explain_file(workspace, rel)
    from_mcp = tools.call("explain", {"project": str(repo), "path": rel})
    assert from_cli["file"] == from_mcp["file"] == rel

    with pytest.raises(projects.ProjectError) as exc:
        scan.explain_file(workspace, "Modules/Demo/Nope.lua")
    assert "試過" in str(exc.value)              # 訊息要列出試過哪些位置


# ---------------------------------------------------------------- 設定檔

def test_a_broken_project_config_is_an_error_not_silently_ignored(tmp_path):
    """`.luadetect.json` 裡的 csdRoot 決定節點檢查對得到哪些 .csd。

    安靜忽略的話一個逗號打錯就讓整組節點規則停止工作，而報告只會說
    「這個專案沒有設定 cocosstudio 根目錄」——完全查不到是設定檔壞了。
    """
    repo = tmp_path / "repo"
    os.makedirs(repo / "InannaLua")
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        fh.write('{ "csdRoot": "C:/x",, }')          # 多一個逗號

    with pytest.raises(projects.ProjectError) as exc:
        projects.load_config(str(repo))
    assert ".luadetect.json" in str(exc.value)

    # 列清單還是要能列（一個壞專案不該讓 list_projects 整個掛掉）
    listed = [w for w in projects.known() if projects._norm(w.directory)
              == projects._norm(str(repo))]
    # 選到它的時候才報錯
    with pytest.raises(projects.ProjectError):
        projects.resolve(project=str(repo))
    for workspace in listed:
        assert workspace.to_dict()["configError"]


def test_a_config_that_is_not_an_object_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    os.makedirs(repo / "InannaLua")
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        fh.write('["luaRoot"]')
    with pytest.raises(projects.ProjectError):
        projects.load_config(str(repo))


def test_a_broken_registry_says_so_instead_of_no_projects_found(tmp_path, monkeypatch):
    """登錄檔壞掉時原本的訊息是「請在專案根目錄執行」——完全指錯方向。"""
    registry = tmp_path / "bad.json"
    with open(registry, "w", encoding="utf-8") as fh:
        fh.write("{ not a list")
    monkeypatch.setenv("CSDVIEW_PROJECTS", str(registry))
    monkeypatch.chdir(tmp_path)                       # 往上也找不到任何專案
    with pytest.raises(projects.ProjectError) as exc:
        projects.resolve()
    assert "bad.json" in str(exc.value)


def test_an_unreadable_csd_leaves_a_trace_instead_of_silence(tmp_path):
    """對到的 .csd 讀不了時原本直接 return []——報告裡一個字都沒有。"""
    csd_root = tmp_path / "cocosstudio"
    broken = csd_root / "ui"
    os.makedirs(broken)
    with open(broken / "V.csd", "w", encoding="utf-8") as fh:
        fh.write("<GameFile><Content>這不是完整的 XML")
    repo = tmp_path / "repo"
    module = repo / "InannaLua" / "Modules" / "Demo"
    os.makedirs(module)
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        json.dump({"csdRoot": str(csd_root)}, fh)
    with open(module / "V.lua", "w", encoding="utf-8") as fh:
        fh.write('local CSB = "ui/V.csb"\n'
                 'function V:Bind( v )\n    self.b = v:getChildByName( "bg" )\nend\n')

    workspace = projects.resolve(project=str(repo))
    result = scan.scan(workspace, [str(module / "V.lua")], explain=True)
    rules = [f.rule for f in result["findings"]]
    assert rules == ["NODE_CSB_UNRESOLVED"]
    assert result["stats"]["unresolvedCsb"] == 1
    assert "讀不了" in result["findings"][0].message


def test_an_unreadable_lua_is_counted_not_silently_skipped(tmp_path, monkeypatch):
    """沒被檢查到的檔案在報告上原本跟乾淨的檔案長得一模一樣。"""
    repo = tmp_path / "repo"
    lua_root = repo / "InannaLua"
    os.makedirs(lua_root)
    target = lua_root / "A.lua"
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("local a = 1\n")

    real_open = builtins.open

    def locked(*args, **kwargs):
        if args and str(args[0]) == str(target):
            raise PermissionError(13, "locked by another process")
        return real_open(*args, **kwargs)

    workspace = projects.resolve(project=str(repo))
    monkeypatch.setattr(builtins, "open", locked)
    result = scan.scan(workspace, [str(target)])
    monkeypatch.undo()
    assert result["stats"]["unreadable"] == 1
    assert result["stats"]["files"] == 0
    assert "讀不到" in report.to_text([], str(lua_root), result["stats"])


# ---------------------------------------------------------------- pre-commit

def _staged_repo(tmp_path):
    """建一個 repo：索引裡是壞的取用鏈，工作目錄是好的。"""
    csd_root = tmp_path / "cocosstudio"
    _write_csd(csd_root, "ui/V.csd", "V",
               _node("bg", "<Children>"
                     + _node("btn", ctype="ButtonObjectData") + "</Children>"))
    repo = tmp_path / "repo"
    module = repo / "InannaLua" / "Modules" / "Demo"
    os.makedirs(module)
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        json.dump({"csdRoot": str(csd_root)}, fh)

    lua = module / "V.lua"

    def write(node):
        with open(lua, "w", encoding="utf-8", newline="\n") as fh:
            fh.write('local CSB = "ui/V.csb"\n'
                     'function V:Bind( v )\n'
                     '    self.m_bg = v:getChildByName( "bg" )\n'
                     f'    self.m_btn = self.m_bg:getChildByName( "{node}" )\n'
                     'end\n'
                     # 取到之後真的用它，這樣才是 error 等級（沒人讀的話只算
                     # warn，見 test_a_missing_node_nobody_reads_is_only_a_warning）
                     'function V:Show()\n'
                     '    self.m_btn:setVisible( true )\n'
                     'end\n')

    _git(str(repo), "init", "-q")
    _git(str(repo), "config", "user.email", "t@example.com")
    _git(str(repo), "config", "user.name", "t")
    write("btn")
    _git(str(repo), "add", ".")
    _git(str(repo), "commit", "-qm", "init")
    write("typo_btn")
    _git(str(repo), "add", "--", str(lua))     # 暫存壞的
    write("btn")                                # 工作目錄修好
    return repo, lua


def test_staged_mode_reads_the_content_from_the_index(tmp_path):
    """pre-commit 要檢查的是這次要提交的內容，不是工作目錄那份。

    `git add -p` 只暫存一半時，讀工作目錄會放過已經暫存的問題。
    """
    repo, lua = _staged_repo(tmp_path)
    workspace = projects.resolve(project=str(repo))

    with scan.index_reader(str(repo)) as read:
        assert 'typo_btn' in (read(str(lua)) or "")     # 索引裡是壞的
        staged = scan.scan(workspace, [str(lua)], read=read)["findings"]
    assert [(f.severity, f.detail.get("node")) for f in staged] == [("error", "typo_btn")]

    # 工作目錄那份是好的，所以不用 index_reader 就抓不到——正是原本的漏洞
    from_disk = scan.scan(workspace, [str(lua)])["findings"]
    assert from_disk == []


def test_the_index_reader_falls_back_when_a_path_is_not_in_the_index(tmp_path):
    repo, lua = _staged_repo(tmp_path)
    with scan.index_reader(str(repo)) as read:
        assert read(str(tmp_path / "outside.lua")) is None
        assert read(str(repo / "InannaLua" / "never-added.lua")) is None
        assert read(str(lua)) is not None                # 同一個行程還活著


def test_the_pre_commit_hook_blocks_a_commit_in_a_repo_it_was_installed_into(tmp_path):
    """hook 不帶 --project（同一份 hook 由主 repo 與所有 worktree 共用），

    所以 resolve() 沒給參數時必須以 cwd 為準。原本回登錄檔第一個專案，於是在別的
    倉庫 commit 時 hook 去檢查了別人的專案、回報「沒有異動」然後放過這次 commit。
    """
    import install

    repo, _ = _staged_repo(tmp_path)
    ok, detail = install.install_hook(str(repo), sys.executable)
    assert ok, detail
    done = _git(str(repo), "commit", "-m", "should be blocked")
    assert done.returncode != 0, "hook 沒有擋下這次 commit"
    assert "typo_btn" in (done.stdout + done.stderr)


def test_resolve_prefers_the_project_the_cwd_is_in(tmp_path, monkeypatch):
    other = tmp_path / "other"
    os.makedirs(other / "InannaLua")
    mine = tmp_path / "mine"
    os.makedirs(mine / "InannaLua")
    registry = tmp_path / "reg.json"
    with open(registry, "w", encoding="utf-8") as fh:
        json.dump([{"directory": str(other)}, {"directory": str(mine)}], fh)
    monkeypatch.setenv("CSDVIEW_PROJECTS", str(registry))

    monkeypatch.chdir(mine)
    assert projects._norm(projects.resolve().directory) == projects._norm(str(mine))
    monkeypatch.chdir(other)
    assert projects._norm(projects.resolve().directory) == projects._norm(str(other))


def test_find_upwards_recognises_a_worktree_dot_git_file(tmp_path):
    repo = tmp_path / "repo"
    os.makedirs(repo)
    _git(str(repo), "init", "-q")
    _git(str(repo), "config", "user.email", "t@example.com")
    _git(str(repo), "config", "user.name", "t")
    with open(repo / "x.txt", "w", encoding="utf-8") as fh:
        fh.write("1\n")
    _git(str(repo), "add", ".")
    _git(str(repo), "commit", "-qm", "init")
    tree = tmp_path / "wt"
    _git(str(repo), "worktree", "add", "-q", str(tree), "-b", "feat")
    assert os.path.isfile(tree / ".git")            # 是檔案，不是目錄
    assert (projects._norm(projects._find_upwards(str(tree)))
            == projects._norm(str(tree)))


# ---------------------------------------------------------------- 字串內誤匹配

def test_constants_ignore_matches_inside_string_literals():
    """字串不抹白（節點名藏在裡面），所以結構性正則會匹配到字串內容。

    實測全庫：`local X = {` 在字串裡命中 7 次、`X = "..."` 125 次。錯誤的常數對照
    會被 resolve_literal 變成錯誤的節點名，那就是誤報。
    """
    src = ('local sql = "UPDATE t SET NodeNames = { BG = \\"wrong_bg\\" }"\n'
           'local NodeNames = { BG = "right_bg" }\n')
    code, spans = luasrc.scan(src)
    consts = luasrc.constants(code, spans)
    assert consts["tables"]["NodeNames"]["BG"] == "right_bg"
    assert luasrc.resolve_literal("NodeNames.BG", consts) == "right_bg"


def test_function_index_ignores_declarations_inside_strings():
    src = ('local snippet = "function Fake:Inner() end"\n'
           'function Real:Method()\n'
           '    doThing()\n'
           'end\n')
    code, spans = luasrc.scan(src)
    table = luasrc.function_index(code, spans)
    assert luasrc.enclosing_function(code, code.index("doThing"), table) == "Real:Method"
    assert "Fake:Inner" not in table[1]


def test_a_schedule_call_inside_a_string_is_not_a_registration():
    found = _lifecycle('local doc = "self.t = s:scheduleScriptFunc( f, 1, false )"\n'
                       'function V:OnEnter()\n    doNothing()\nend\n')
    assert found == []


def test_in_string_is_correct_at_span_boundaries():
    code, spans = luasrc.scan('a = "xy" .. \'z\'\n')
    first = spans[0]
    assert not luasrc.in_string(spans, first[0] - 1)
    assert luasrc.in_string(spans, first[0])          # 開頭的引號算在裡面
    assert luasrc.in_string(spans, first[1] - 1)
    assert not luasrc.in_string(spans, first[1])      # 結尾之後不算
    # 跟原本的線性版本必須完全一致
    for index in range(len(code) + 1):
        linear = any(s <= index < e for s, e, _ in spans)
        assert luasrc.in_string(spans, index) is linear, index


def test_a_locked_lua_in_the_module_downgrades_instead_of_false_accusing(tmp_path,
                                                                         monkeypatch):
    """讀不到的 .lua 裡可能有 setName——少收一個就冤枉一行正確的程式碼。

    所以模組裡有讀不到的檔案時不給 error，並在訊息裡講出原因。
    """
    csd_root = tmp_path / "cocosstudio"
    _write_csd(csd_root, "ui/V.csd", "V",
               _node("bg", "<Children>" + _node("btn", ctype="ButtonObjectData")
                     + "</Children>"))
    repo = tmp_path / "repo"
    module = repo / "InannaLua" / "Modules" / "Demo"
    os.makedirs(module)
    with open(repo / ".luadetect.json", "w", encoding="utf-8") as fh:
        json.dump({"csdRoot": str(csd_root)}, fh)
    view = module / "V.lua"
    with open(view, "w", encoding="utf-8") as fh:
        fh.write('local CSB = "ui/V.csb"\n'
                 'function V:Bind( v )\n'
                 '    self.m_bg = v:getChildByName( "bg" )\n'
                 '    self.m_x = self.m_bg:getChildByName( "made_at_runtime" )\n'
                 'end\n')
    builder = module / "Builder.lua"
    with open(builder, "w", encoding="utf-8") as fh:
        fh.write('local n = cc.Node:create()\nn:setName( "made_at_runtime" )\n')

    workspace = projects.resolve(project=str(repo))
    assert scan.scan(workspace, [str(view)])["findings"] == []   # 看得到 setName

    real_open = builtins.open

    def locked(*args, **kwargs):
        if args and str(args[0]) == str(builder):
            raise PermissionError(13, "locked")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", locked)
    found = scan.scan(workspace, [str(view)])["findings"]
    monkeypatch.undo()

    assert len(found) == 1
    assert found[0].severity == WARN            # 不是 error：可能是誤報
    assert found[0].confidence == "medium"
    assert "讀不到" in found[0].message
    assert found[0].detail["unreadableInModule"] == 1


def test_an_incomplete_global_name_table_is_not_cached(tmp_path):
    """名稱表少一批卻存進快取的話，下一次跑會拿到一份殘缺、簽章卻有效的表。

    **這裡不可以用 `monkeypatch.undo()`。** `monkeypatch` 是 function scope，
    `conftest` 的 `_isolated_environment` 跟這個測試拿到的是**同一個實例**，
    `undo()` 會把它設的 `HOME`／`USERPROFILE` 一起還原掉——於是下面那行
    `fresh.global_names()` 就寫進使用者真正的 `~/.luadetect_cache`。
    測試照樣全綠，只是每跑一次在人家家目錄留一個檔（實測累積了 178 個）。
    用 `context()` 只還原自己這一段。
    """
    root = tmp_path / "cocosstudio"
    first = _write_csd(root, "a.csd", "A", _node("only_in_a"))
    _write_csd(root, "b.csd", "B", _node("only_in_b"))
    project = Project(str(root))

    real_open = builtins.open

    def locked(*args, **kwargs):
        if args and str(args[0]) == str(first):
            raise PermissionError(13, "locked")
        return real_open(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(builtins, "open", locked)
        names = project.global_names()
    assert "only_in_b" in names and "only_in_a" not in names
    assert project.unreadable == 1
    assert not os.path.isfile(project._cache_path())     # 殘缺的不寫快取

    fresh = Project(str(root))
    assert "only_in_a" in fresh.global_names()           # 重新讀就完整了


def test_a_git_repo_above_the_cwd_is_not_mistaken_for_a_lua_project(tmp_path,
                                                                    monkeypatch):
    """從 cwd 往上找只要看到 `.git` 就停，而 `_lua_root` 又會退回那一層本身。

    於是「我上面某一層剛好是 git 倉庫」會變成一個 lua_root = 整棵上層目錄的假專案，
    而且會蓋掉使用者本來要用的專案。
    """
    outer = tmp_path / "monorepo"
    deep = outer / "tools" / "somewhere"
    real = tmp_path / "TheProject"
    os.makedirs(deep)
    os.makedirs(real / "InannaLua")
    _git(str(outer), "init", "-q")
    registry = tmp_path / "reg.json"
    with open(registry, "w", encoding="utf-8") as fh:
        json.dump([{"directory": str(real)}], fh)
    monkeypatch.setenv("CSDVIEW_PROJECTS", str(registry))

    monkeypatch.chdir(deep)
    assert [w.name for w in projects.known()] == ["TheProject"]
    assert projects._norm(projects.resolve().directory) == projects._norm(str(real))

    # 反面：cwd 真的在一個看得出是 Lua 專案的目錄裡，就要選它
    mine = outer / "sub" / "MyGame"
    os.makedirs(mine / "InannaLua")
    monkeypatch.chdir(mine)
    assert projects._norm(projects.resolve().directory) == projects._norm(str(mine))


def test_the_index_reader_normalises_crlf_like_the_disk_reader(tmp_path):
    repo = tmp_path / "repo"
    lua_root = repo / "InannaLua"
    os.makedirs(lua_root)
    _git(str(repo), "init", "-q")
    _git(str(repo), "config", "user.email", "t@example.com")
    _git(str(repo), "config", "user.name", "t")
    _git(str(repo), "config", "core.autocrlf", "false")
    target = lua_root / "A.lua"
    with open(target, "wb") as fh:
        fh.write(b"local a = 1\r\nlocal b = 2\r\n")     # 刻意存 CRLF
    _git(str(repo), "add", ".")

    with scan.index_reader(str(repo)) as read:
        from_index = read(str(target))
    with open(target, encoding="utf-8") as fh:
        from_disk = fh.read()
    assert from_index == from_disk == "local a = 1\nlocal b = 2\n"


def test_the_index_reader_ignores_directories(tmp_path):
    repo = tmp_path / "repo"
    os.makedirs(repo / "InannaLua" / "Modules")
    _git(str(repo), "init", "-q")
    _git(str(repo), "config", "user.email", "t@example.com")
    _git(str(repo), "config", "user.name", "t")
    with open(repo / "InannaLua" / "Modules" / "A.lua", "w", encoding="utf-8") as fh:
        fh.write("local a = 1\n")
    _git(str(repo), "add", ".")
    with scan.index_reader(str(repo)) as read:
        assert read(str(repo / "InannaLua" / "Modules")) is None   # 目錄不是 blob
        assert read(str(repo / "InannaLua" / "Modules" / "A.lua")) is not None


# ---------------------------------------------------------------- 效能改寫的等價性

_BS = chr(92)


@pytest.mark.parametrize("src", [
    "", "a", "-", "-x", "--", "--\n", "--x\n--y\n",
    "[", "[[", "[[x", "[=", "[==", "[=x", "[=[x]=]", "[==[ ]] ]==]",
    "]]", "x[1]", "x[[y]]", "t[ [[s]] ]", " [ ",
    "--[[\nx\n]]", "--[[ no close", "--[=[a]=]b", "--\n--\n[[a]]",
    "[[a]] [[b]]", "x = [[", "'a", '"a\n b"',
    "a = 'it" + _BS + "'s'", "a = 'x" + _BS, 'b = "' + _BS + 'n"',
    "c = '" + _BS + _BS + "'", "s = '' .. ''",
    "::l::", "a--b", '"http://a--b"', "\n\n\n", "'a' .. \"b\"",
])
def test_scan_keeps_offsets_and_handles_every_bracket_shape(src):
    """剝註解改成「正則跳位 + 片段拼回」之後的等價性與不變式。

    位移對得上原檔是這個模組的核心前提——行號、字串區間、取用鏈全部靠它。
    """
    code, spans = luasrc.scan(src)
    assert len(code) == len(src)                  # 抹白不能改變長度
    for start, end, _ in spans:
        assert 0 <= start < end <= len(src)
        assert code[start:end] == src[start:end]  # 字串原樣保留
    # 註解裡的東西一定被抹掉
    for start, end, _ in spans:
        assert "--" not in code[start:end] or src[start:end] == code[start:end]
    # spans 不重疊且遞增（in_string / outside_strings 的前提）
    for earlier, later in zip(spans, spans[1:]):
        assert earlier[1] <= later[0]


def test_scan_blanks_comments_but_keeps_newlines():
    src = "a = 1 --[[ 兩行\n註解 ]] b = 2\n"
    code, _ = luasrc.scan(src)
    assert len(code) == len(src)
    assert code.count("\n") == src.count("\n")     # 行號不能跑掉
    assert "註解" not in code and "a = 1" in code and "b = 2" in code


def test_outside_strings_matches_the_bisect_reference():
    """前進掃描與逐個二分必須給出完全相同的結果。"""
    src = ("local sql = \"a = { K = 'v' }\"\n"
           "local T = { K = \"real\" }\n"
           "function F() end\n"
           "local s = 'x'\n")
    code, spans = luasrc.scan(src)
    for pattern in (luasrc._CONST_DECL, luasrc._FUNC_DECL):
        fast = [m.start() for m in luasrc.outside_strings(pattern, code, spans)]
        slow = [m.start() for m in pattern.finditer(code)
                if not luasrc.in_string(spans, m.start())]
        assert fast == slow


def test_a_concatenated_string_is_not_recorded_as_a_literal():
    """`local f = "%0" .. n` 的值是組出來的，只記第一段會變成假的字面值。"""
    code, spans = luasrc.scan('local f = "%0" .. n .. "d"\nlocal g = "plain"\n')
    consts = luasrc.constants(code, spans)
    assert "f" not in consts["strings"]
    assert consts["strings"]["g"] == "plain"


def test_an_unterminated_quote_does_not_swallow_later_declarations():
    """短字串不能跨行。允許跨行的話一個沒收尾的引號會吃掉後面幾十行的宣告。"""
    src = ("local broken = 'oops\n"
           "local NodeNames = { BG = \"bg\" }\n"
           "local after = \"kept\"\n")
    code, spans = luasrc.scan(src)
    consts = luasrc.constants(code, spans)
    assert consts["tables"]["NodeNames"]["BG"] == "bg"
    assert consts["strings"]["after"] == "kept"


# ---------------------------------------------------------------- 訊息與指紋

def test_a_message_never_spans_lines(tmp_path):
    """規則會把原始碼片段放進訊息，而那片段可能是一整個 inline function。

    實測壞了三個地方：文字報告一筆佔六行、markdown 表格欄數對不齊（GFM 整張垮）、
    指紋含了函式內容（callback 裡改一個空白就變成「新問題」）。
    """
    long_body = ("function( sender, eventType )\n"
                 "    if eventType == 1 then\n        act()\n    end\nend")
    finding = Finding("EVENT_LEAK", INFO, "a.lua", 8,
                      f"addEventListener( {long_body} ) 找不到解除",
                      key=f"addEventListener:{long_body}")
    assert "\n" not in finding.message
    assert "\n" not in finding.key
    assert "\n" not in finding.fingerprint("/root")
    assert len(finding.key) <= 160


def test_one_line_collapses_and_truncates():
    assert report.one_line("a\n  b\tc") == "a b c"
    assert report.one_line("") == ""
    long_text = "x" * 300
    trimmed = report.one_line(long_text, 50)
    assert len(trimmed) == 50 and trimmed.endswith("…")


def _lifecycle_repo(tmp_path, body):
    repo = tmp_path / "repo"
    module = repo / "InannaLua" / "Modules" / "Demo"
    os.makedirs(module)
    with open(module / "V.lua", "w", encoding="utf-8") as fh:
        fh.write(body)
    workspace = projects.resolve(project=str(repo))
    return scan.scan(workspace, [str(module / "V.lua")], ["lifecycle"])["findings"]


def test_a_widget_listener_is_not_a_leak(tmp_path):
    """`widget:addEventListener( cb )` 是 ccui.Widget——Cocos 的 Lua 綁定沒有
    widget 版的 removeEventListener，callback 跟著節點消滅，沒有東西要解除。

    專案裡 33 處 `addEventListener(` 全是這一種，不分開就是 33 個誤報。
    """
    found = _lifecycle_repo(tmp_path,
                            'function V:Show()\n'
                            '    self.m_page:addEventListener( function( s, e )\n'
                            '        act()\n'
                            '    end )\n'
                            '    self.m_cb:addEventListener( onTouched )\n'
                            'end\n')
    assert [f for f in found if f.rule == "EVENT_LEAK"] == []


def test_a_dispatcher_listener_still_needs_releasing(tmp_path):
    found = _lifecycle_repo(tmp_path,
                            'function V:Show()\n'
                            '    dispatcher:addEventListener( self.m_listener, node )\n'
                            'end\n')
    assert [(f.rule, f.detail["event"]) for f in found] \
        == [("EVENT_LEAK", "self.m_listener")]


@pytest.mark.parametrize("receiver", [
    "dispatcher", "eventDispatcher", "pcsEventDispatcher",
    "cc.Director:getInstance():getEventDispatcher()", "self:getEventDispatcher()"])
def test_every_dispatcher_spelling_is_recognised(tmp_path, receiver):
    """`expr_before` 回的接收者帶著結尾的 `:`，忘了這件事就會把 dispatcher
    也當成 widget 放過——26 個誤報換成 24 個漏報。"""
    found = _lifecycle_repo(tmp_path,
                            "function V:Show()\n"
                            f"    {receiver}:addEventListener( self.m_l, node )\n"
                            "end\n")
    assert len([f for f in found if f.rule == "EVENT_LEAK"]) == 1, receiver


def test_a_dispatcher_listener_with_a_matching_release_is_quiet(tmp_path):
    found = _lifecycle_repo(tmp_path,
                            'function V:Show()\n'
                            '    dispatcher:addEventListener( self.m_l, node )\n'
                            'end\n'
                            'function V:Hide()\n'
                            '    dispatcher:removeEventListener( self.m_l )\n'
                            'end\n')
    assert [f for f in found if f.rule == "EVENT_LEAK"] == []


# ------------------------------------------- EventDispatcher（FixedPriority）

def test_scene_graph_priority_is_not_a_leak(tmp_path):
    """綁在節點上，節點銷毀時 EventDispatcher 自己會移除。

    專案裡 25 處，其中 16 處沒有解除——但那是對的。
    """
    found = _lifecycle_repo(tmp_path,
                            'function V:Show()\n'
                            '    local listener = cc.EventListenerCustom:create( E, h )\n'
                            '    dispatcher:addEventListenerWithSceneGraphPriority('
                            ' listener, self )\n'
                            'end\n')
    assert [f for f in found if f.rule == "LISTENER_LEAK"] == []


def test_fixed_priority_without_a_release_is_an_error(tmp_path):
    """沒有節點綁定，不手動解除就會一直收事件。

    這一塊本來從來沒被檢查過：`addEventListenerWithFixedPriority` 與
    `addEventListener` 前綴相同，而規則的正則要求 `addEventListener` 後面緊接 `(`。
    """
    found = [f for f in _lifecycle_repo(
        tmp_path,
        'function V:Bind()\n'
        '    self.m_l = cc.EventListenerTouchOneByOne:create()\n'
        '    dispatcher:addEventListenerWithFixedPriority( self.m_l, 10 )\n'
        'end\n') if f.rule == "LISTENER_LEAK"]
    assert [(f.severity, f.detail["listener"]) for f in found] \
        == [(ERROR, "self.m_l")]
    assert "找不到對應的 removeEventListener" in found[0].message


def test_a_custom_listener_released_by_event_name_is_not_reported(tmp_path):
    """`cc.EventListenerCustom` 是靠**事件名**解除的，不需要 listener 的參考。

        local listener = cc.EventListenerCustom:create( inn.EVENT.A, h )
        dispatcher:addEventListenerWithFixedPriority( listener, 10 )
        ...
        dispatcher:removeCustomEventListeners( inn.EVENT.A )   -- 這樣就對了

    漏掉這個寫法的代價：全庫 5 筆 error 全是誤報，訊息還斷言「已經不可能解除」
    ——把完全正確的程式碼指成必須重寫。`BS_ContentType3.lua`、
    `ModelLocator.lua` 都是這個形狀。
    """
    found = [f for f in _lifecycle_repo(
        tmp_path,
        'function V:OnEnter()\n'
        '    local listener = cc.EventListenerCustom:create( inn.EVENT.A, h )\n'
        '    dispatcher:addEventListenerWithFixedPriority( listener, 10 )\n'
        'end\n'
        'function V:OnExit()\n'
        '    dispatcher:removeCustomEventListeners( inn.EVENT.A )\n'
        'end\n') if f.rule == "LISTENER_LEAK"]
    assert found == []


def test_a_custom_listener_with_no_release_is_still_caught(tmp_path):
    """B1：修完誤報之後要確認真 bug 還抓得到。「0 筆」有兩種可能。"""
    found = [f for f in _lifecycle_repo(
        tmp_path,
        'function V:OnEnter()\n'
        '    local listener = cc.EventListenerCustom:create( inn.EVENT.A, h )\n'
        '    dispatcher:addEventListenerWithFixedPriority( listener, 10 )\n'
        'end\n') if f.rule == "LISTENER_LEAK"]
    assert len(found) == 1 and found[0].severity == ERROR
    assert found[0].detail["customEvent"] == "inn.EVENT.A"
    # 訊息要指出正確的修法，不能再說「不可能解除」
    assert "removeCustomEventListeners" in found[0].message
    assert "不可能" not in found[0].message


def test_releasing_a_different_event_name_does_not_count(tmp_path):
    found = [f for f in _lifecycle_repo(
        tmp_path,
        'function V:OnEnter()\n'
        '    local listener = cc.EventListenerCustom:create( inn.EVENT.A, h )\n'
        '    dispatcher:addEventListenerWithFixedPriority( listener, 10 )\n'
        'end\n'
        'function V:OnExit()\n'
        '    dispatcher:removeCustomEventListeners( inn.EVENT.B )\n'
        'end\n') if f.rule == "LISTENER_LEAK"]
    assert len(found) == 1


def test_tostring_wrapping_does_not_break_the_event_match(tmp_path):
    """`ModelLocator` 註冊寫 `tostring(eventName)`、解除有時寫 `eventName`。"""
    found = [f for f in _lifecycle_repo(
        tmp_path,
        'function M:register( eventName, h )\n'
        '    local listener = cc.EventListenerCustom:create('
        ' tostring(eventName), h )\n'
        '    dispatcher:addEventListenerWithFixedPriority( listener, 10 )\n'
        'end\n'
        'function M:unregisterAll( names )\n'
        '    for i, eventName in pairs( names ) do\n'
        '        dispatcher:removeCustomEventListeners( eventName )\n'
        '    end\n'
        'end\n') if f.rule == "LISTENER_LEAK"]
    assert found == []


def test_removing_a_whole_listener_type_counts_as_releasing(tmp_path):
    """`removeEventListenersForType` 一次清掉整個型別。

    靜態比不出型別，一律當成全部解除——誤報比漏報糟。
    """
    found = [f for f in _lifecycle_repo(
        tmp_path,
        'function V:OnEnter()\n'
        '    local listener = cc.EventListenerCustom:create( inn.EVENT.A, h )\n'
        '    dispatcher:addEventListenerWithFixedPriority( listener, 10 )\n'
        'end\n'
        'function V:OnExit()\n'
        '    dispatcher:removeEventListenersForType( cc.EventListener.CUSTOM )\n'
        'end\n') if f.rule == "LISTENER_LEAK"]
    assert found == []


def test_a_non_custom_listener_is_not_excused_by_a_custom_release(tmp_path):
    """`removeCustomEventListeners` 只解除自訂事件，別的 listener 不算。"""
    found = [f for f in _lifecycle_repo(
        tmp_path,
        'function V:OnEnter()\n'
        '    local listener = cc.EventListenerTouchOneByOne:create()\n'
        '    dispatcher:addEventListenerWithFixedPriority( listener, -1 )\n'
        'end\n'
        'function V:OnExit()\n'
        '    dispatcher:removeCustomEventListeners( inn.EVENT.A )\n'
        'end\n') if f.rule == "LISTENER_LEAK"]
    assert len(found) == 1


def test_a_listener_stored_in_a_field_and_removed_there_is_quiet(tmp_path):
    """註冊寫 local、解除寫欄位，中間靠「欄位 = local」連起來。

    這是 PlayInfo.lua 的寫法，完全正確。純文字比對會誤報，所以要雙向的等價。
    """
    found = _lifecycle_repo(tmp_path,
                            'function V:Bind()\n'
                            '    local touchListener = cc.EventListenerTouchOneByOne'
                            ':create()\n'
                            '    dispatcher:addEventListenerWithFixedPriority('
                            ' touchListener, -1 )\n'
                            '    self.m_touch = touchListener\n'
                            'end\n'
                            'function V:OnClose()\n'
                            '    if self.m_touch then\n'
                            '        dispatcher:removeEventListener( self.m_touch )\n'
                            '    end\n'
                            'end\n')
    assert [f for f in found if f.rule == "LISTENER_LEAK"] == []


def test_remove_all_event_listeners_counts_as_releasing_everything(tmp_path):
    found = _lifecycle_repo(tmp_path,
                            'function V:Bind()\n'
                            '    self.m_l = cc.EventListenerCustom:create( E, h )\n'
                            '    dispatcher:addEventListenerWithFixedPriority('
                            ' self.m_l, 10 )\n'
                            'end\n'
                            'function V:OnExit()\n'
                            '    dispatcher:removeAllEventListeners()\n'
                            'end\n')
    assert [f for f in found if f.rule == "LISTENER_LEAK"] == []


def test_equivalence_is_bidirectional_and_transitive():
    code = luasrc.scan("local a = b\nself.m_c = a\nlocal d = self.m_c\n")[0]
    groups = lifecycle._equivalence(code)
    reps = {groups[name] for name in ("a", "b", "self.m_c", "d")}
    assert len(reps) == 1, groups


def test_clearing_with_nil_does_not_merge_unrelated_listeners(tmp_path):
    """`X = nil` 是清除，不是「X 跟 nil 是同一個物件」。

    當成等價的話，同一個檔案裡每個被 `= nil` 清掉的東西會全部併成一類，
    解除其中一個就等於解除全部——實測全庫有 529 個檔案會這樣併。
    這裡兩個監聽都被 `= nil` 清掉，只解除了 a，b 必須照樣被報出來。
    """
    found = [f for f in _lifecycle_repo(
        tmp_path,
        'function V:Bind()\n'
        '    local a = cc.EventListenerCustom:create( E1, h )\n'
        '    local b = cc.EventListenerCustom:create( E2, h )\n'
        '    dispatcher:addEventListenerWithFixedPriority( a, 10 )\n'
        '    dispatcher:addEventListenerWithFixedPriority( b, 10 )\n'
        '    self.m_a = a\n'
        '    self.m_b = b\n'
        'end\n'
        'function V:OnExit()\n'
        '    dispatcher:removeEventListener( self.m_a )\n'
        '    self.m_a = nil\n'
        '    self.m_b = nil\n'
        'end\n') if f.rule == "LISTENER_LEAK"]
    assert [f.detail["listener"] for f in found] == ["b"], \
        "只解除了 a，b 應該還是要報；報成 0 筆代表 nil 又把兩個併在一起了"


def test_literals_never_enter_the_equivalence_relation():
    """PlayInfo.lua 的實際案例：`touchBeganPos`、`touchListener`、
    `self.m_pageViewLoopTouchListener` 曾經因為都碰到 `= nil` 而併成同一類。
    """
    code = luasrc.scan("local pos = nil\n"
                       "self.m_listener = listener\n"
                       "self.m_listener = nil\n"
                       "local flag = false\n")[0]
    groups = lifecycle._equivalence(code)
    assert "nil" not in groups and "false" not in groups
    assert "pos" not in groups and "flag" not in groups
    assert groups["listener"] == groups["self.m_listener"]   # 真的別名還要留著


def test_table_aliases_also_ignore_nil(tmp_path):
    """`_aliases` 是排程 handle 收斂用的，同樣不能讓 `local t = nil` 把
    handle 的表名解析成 `nil`——那會讓不同表的 handle 撞成同一個。
    """
    code = luasrc.scan("local t = nil\nlocal u = self.m_timers\n")[0]
    aliases = lifecycle._aliases(code)
    assert "t" not in aliases and aliases["u"] == "self.m_timers"
    assert lifecycle._normalise_handle("t[ k ]", aliases) == "t[k]"


# --------------------------------------------------- 明確指定的專案必須被驗證

def _fake_project(tmp_path):
    """一個能被 `path` 自己推斷出來的專案（有 InannaLua 這一層就夠）。"""
    module = tmp_path / "InannaLua" / "Modules" / "M"
    module.mkdir(parents=True)
    (module / "A.lua").write_text("local x = 1\n", encoding="utf-8")
    return module


def test_an_unknown_project_name_is_never_silently_ignored(tmp_path):
    """打錯專案名不可以被 `path` 的優先序蓋過去。

    實測過的失敗方式：在 Inanna 目錄下跑
    `luadetect voucherCenter --project NoSuchProj`，`path` 分支先解析成功，
    於是拿 cwd 的專案掃完並回報「通過、離開碼 0」。CI 裡一個 typo 就變成
    「檢查過了」——對一個檢查工具來說這是最不能有的失敗方式。
    """
    module = _fake_project(tmp_path)

    # 省略 project 時，path 自己推斷得到（這條路不能被下面的修正弄壞）
    assert projects.resolve(None, str(module)).lua_root

    with pytest.raises(projects.ProjectError) as err:
        projects.resolve("NoSuchProjXYZ", str(module))
    assert "NoSuchProjXYZ" in str(err.value)
    assert "已知的專案" in str(err.value)


def test_an_unknown_project_name_fails_even_without_a_path(tmp_path):
    with pytest.raises(projects.ProjectError):
        projects.resolve("NoSuchProjXYZ", None)


def test_a_directory_given_as_the_project_still_works(tmp_path):
    """`--project <資料夾>` 這條路不能被驗證擋掉。"""
    _fake_project(tmp_path)
    workspace = projects.resolve(str(tmp_path), None)
    assert workspace.lua_root.endswith("InannaLua")


def test_a_skipped_file_says_which_csd_had_the_names(tmp_path):
    """「只吻合 0%」讀起來像「這些名字哪裡都沒有」，但常見的是名字全都在、
    只有取用鏈的根對不上——helper 收外面傳進來的節點就是這樣。

    實際案例：`VoucherCenter/TipHelper.lua` 的 6 個名字有 5 個在
    `VoucherCenterTip.csd` 裡，鏈卻一條都對不上。只講 0% 的話，這個「跳過」
    等於叫人自己重查一遍。
    """
    repo = tmp_path / "repo"
    module = repo / "InannaLua" / "Modules" / "Tip"
    studio = tmp_path / "cocosstudio"
    module.mkdir(parents=True)
    (repo / ".luadetect.json").write_text(
        json.dumps({"csdRoot": str(studio)}), encoding="utf-8")

    # 這個模組的版面：名字都在，但掛在 holder 底下，不是 Lua 用的那個根
    _write_csd(studio, "tip/TipPanel.csd", "Layer",
               _node("holder", "<Children>"
                     + _node("tip_bg") + _node("arrow")
                     + _node("txt_tip") + _node("amount")
                     + "</Children>", "PanelObjectData"))
    # 同模組另一份完全不相關的版面，用來確認挑的是名字最齊的那一份
    _write_csd(studio, "tip/Other.csd", "Layer", _node("nothing_here"))

    (module / "TipHelper.lua").write_text(
        'local CSB = "tip/TipPanel.csb"\n'
        'local OTHER = "tip/Other.csb"\n'
        "function TipHelper.Bind( root )\n"
        '    local node = root:getChildByName( "outside_tip" )\n'
        '    local bg = node:getChildByName( "tip_bg" )\n'
        '    local arrow = bg:getChildByName( "arrow" )\n'
        '    local txt = bg:getChildByName( "txt_tip" )\n'
        '    local num = bg:getChildByName( "amount" )\n'
        "    return { bg, arrow, txt, num }\n"
        "end\n", encoding="utf-8")

    workspace = projects.resolve(project=str(repo))
    found = scan.scan(workspace, projects.lua_files(workspace),
                      explain=True)["findings"]
    skipped = [f for f in found if f.rule == "NODE_CSB_UNRESOLVED"]
    assert len(skipped) == 1, [(f.rule, f.message) for f in found]

    message, detail = skipped[0].message, skipped[0].detail
    assert "取用鏈" in message, message
    assert detail["bestNameMatch"] == "TipPanel.csd", detail
    assert detail["namesFound"] == 4 and detail["namesUsed"] == 5, detail
    assert "4/5" in message and "TipPanel.csd" in message, message
    # 對不上的是鏈的根，訊息要講出來，不然「跳過」沒有可行動的線索
    assert "鏈的根" in message, message
