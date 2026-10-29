"""安裝流程的測試——重點是 hook 到底裝到哪。

自己接 `.git/hooks` 在兩種常見情況下是錯的：linked worktree（多開 agent 時每個
agent 一個 worktree）的 `.git` 是檔案不是目錄，而設過 `core.hooksPath` 的專案
根本不看 `.git/hooks`。兩種都會裝到不會被執行的地方，或誤判成「不是 git 倉庫」。
"""
import os
import subprocess
import sys

import pytest

import install


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _init_repo(place):
    os.makedirs(place)
    _git(place, "init", "-q")
    _git(place, "config", "user.email", "t@example.com")
    _git(place, "config", "user.name", "t")
    with open(place / "x.txt", "w", encoding="utf-8") as fh:
        fh.write("1\n")
    _git(place, "add", ".")
    _git(place, "commit", "-qm", "init")
    return place


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path / "repo")


def test_hooks_dir_in_a_plain_repo(repo):
    assert (os.path.normcase(install.hooks_dir(str(repo)))
            == os.path.normcase(os.path.join(str(repo), ".git", "hooks")))


def test_hooks_dir_follows_core_hookspath(repo):
    _git(repo, "config", "core.hooksPath", ".husky")
    assert (os.path.normcase(install.hooks_dir(str(repo)))
            == os.path.normcase(os.path.join(str(repo), ".husky")))


def test_a_linked_worktree_resolves_to_the_shared_hooks_dir(repo, tmp_path):
    """worktree 的 .git 是檔案；hook 住在共用的 git dir，而且確實會被執行。"""
    tree = tmp_path / "wt"
    assert _git(repo, "worktree", "add", "-q", str(tree), "-b", "feat").returncode == 0
    assert os.path.isfile(os.path.join(str(tree), ".git"))     # 是檔案，不是目錄
    assert (os.path.normcase(install.hooks_dir(str(tree)))
            == os.path.normcase(os.path.join(str(repo), ".git", "hooks")))


def test_installing_from_inside_a_worktree_works_and_is_visible(repo, tmp_path):
    tree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(tree), "-b", "feat")
    ok, detail = install.install_hook(str(tree), sys.executable)
    assert ok, detail
    # 從 worktree 裝的、從主 repo 也看得到（是同一份），status 與解除安裝才不會漏
    assert install.hook_installed(str(tree))
    assert install.hook_installed(str(repo))
    assert install.remove_hook(str(tree))[0]
    assert not install.hook_installed(str(repo))


def test_a_foreign_pre_commit_hook_is_not_overwritten(repo):
    path = install.hook_path(str(repo))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho someone elses hook\n")
    ok, detail = install.install_hook(str(repo), sys.executable)
    assert not ok and "沒有覆蓋" in detail
    with open(path, encoding="utf-8") as fh:
        assert "someone elses hook" in fh.read()


def test_a_non_repo_is_reported_clearly(tmp_path):
    plain = tmp_path / "plain"
    os.makedirs(plain)
    ok, detail = install.install_hook(str(plain), sys.executable)
    assert not ok and "git" in detail


def test_client_none_registers_nothing(monkeypatch):
    """原本 `--client none` 因為「什麼都沒做」而掉進一鍵安裝，把兩個都註冊了。"""
    calls = []
    monkeypatch.setattr(install, "do_register",
                        lambda python, client: calls.append(client) or True)
    monkeypatch.setattr(install, "self_check", lambda python: None)
    monkeypatch.setattr(install, "say", lambda *a, **k: None)
    assert install.main(["--client", "none"]) == 0
    assert calls == []


def test_a_failed_hook_install_shows_up_in_the_exit_code(monkeypatch, tmp_path):
    monkeypatch.setattr(install, "say", lambda *a, **k: None)
    assert install.main(["--hook", str(tmp_path / "nope")]) == 1


# ------------------------------------------------------------------ 移除 hook

def _write_hook(repo, text, newline="\n"):
    path = install.hook_path(str(repo))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return path


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_removing_a_hook_only_touches_the_chosen_project(repo, tmp_path):
    """選單 6 / --remove-hook 是「只拿掉這一個」，跟解除安裝的「全部」分開。"""
    other = _init_repo(tmp_path / "other")
    assert install.install_hook(str(repo), sys.executable)[0]
    assert install.install_hook(str(other), sys.executable)[0]
    ok, detail = install.remove_hook(str(repo))
    assert ok and "已移除" in detail
    assert not os.path.exists(install.hook_path(str(repo)))
    assert install.hook_installed(str(other))


def test_a_hook_written_by_another_python_or_location_is_still_ours(repo):
    """換過 Python、工具搬過家，或檔案被編輯器存成 CRLF，都還是我們產生的那份。"""
    _write_hook(repo, install.HOOK_TEMPLATE.format(
        python="D:/Other Python/python.exe", cli="E:/moved/luaDetect/run_cli.py"),
        newline="\r\n")
    assert install.hook_state(str(repo))[0] == "ours"
    assert install.remove_hook(str(repo))[0]
    assert not os.path.exists(install.hook_path(str(repo)))


def test_a_hook_merged_into_someone_elses_is_not_deleted(repo):
    """照 install_hook 的提示把那一行併進自己的 hook 之後，整個刪掉會連原本的一起刪。

    以前只看檔案裡有沒有 "luadetect"，這種檔案移除時整個被刪、安裝時整個被蓋。
    提示給的路徑是 `luaDetect/run_cli.py`（大寫 D），比對要不分大小寫才認得。
    """
    mine = ('#!/bin/sh\nnpm run lint || exit 1\n'
            '"C:/Python/python.exe" "C:/tools/luaDetect/run_cli.py" scan --staged\n')
    path = _write_hook(repo, mine)
    assert install.hook_state(str(repo))[0] == "mixed"
    ok, detail = install.remove_hook(str(repo))
    assert not ok and "那一行" in detail
    assert not install.install_hook(str(repo), sys.executable)[0]
    assert _read(path) == mine


def test_a_generated_hook_with_lines_added_by_hand_is_not_deleted(repo):
    """樣板之外多了一行，就不是「整份都是我們的」。"""
    text = install.HOOK_TEMPLATE.format(python="py", cli="cli") + "echo 我自己加的\n"
    path = _write_hook(repo, text)
    assert not install.remove_hook(str(repo))[0]
    assert _read(path) == text


def test_removing_leaves_someone_elses_hook_alone(repo):
    path = _write_hook(repo, "#!/bin/sh\necho someone elses hook\n")
    ok, detail = install.remove_hook(str(repo))
    assert ok and "沒有動它" in detail          # luadetect 本來就不會跑，所以算 ok
    assert "someone elses hook" in _read(path)


def test_removing_from_a_non_repo_is_not_reported_as_done(tmp_path):
    """對不到 git 不等於「本來就沒裝」——講成沒裝，使用者會以為已經處理好了。"""
    plain = tmp_path / "plain"
    os.makedirs(plain)
    ok, detail = install.remove_hook(str(plain))
    assert not ok and "git" in detail
    ok, detail = install.remove_hook(str(tmp_path / "nope"))
    assert not ok and "不存在" in detail


def test_remove_hook_flag_and_its_exit_code(repo, monkeypatch, tmp_path):
    monkeypatch.setattr(install, "say", lambda *a, **k: None)
    install.install_hook(str(repo), sys.executable)
    assert install.main(["--remove-hook", str(repo)]) == 0
    assert not install.hook_installed(str(repo))
    assert install.main(["--remove-hook", str(repo)]) == 0       # 再跑一次：本來就沒裝
    assert install.main(["--remove-hook", str(tmp_path / "nope")]) == 1


def test_menu_option_6_removes_the_hook_of_the_pasted_project(repo, monkeypatch):
    """選單重新編號過；接錯的話選 6 會跑去建基準檔，而且沒有任何錯誤訊息。"""
    install.install_hook(str(repo), sys.executable)
    answers = iter(["6", str(repo), "", "0"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    printed = []
    monkeypatch.setattr(install, "say", lambda text="": printed.append(text))
    assert install.menu(sys.executable) == 0
    assert not install.hook_installed(str(repo))
    assert any("已移除" in line for line in printed)


# ------------------------------------------------------------------ skill

@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    """把使用者層級的 skill 目錄搬到 tmp，不要動到真的 ~/.claude。"""
    home = tmp_path / "home"
    os.makedirs(home)
    monkeypatch.setattr(install, "user_skill_path",
                        lambda: str(home / ".claude" / "skills" / "luadetect" / "SKILL.md"))
    return home


def _deployed(home):
    path = home / ".claude" / "skills" / "luadetect" / "SKILL.md"
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_deployed_skill_points_back_at_the_repo(elsewhere):
    """部署出去的那份要能把讀的人帶回工具本體，不然搬過家就找不到規則。"""
    ok, detail = install.install_skill()
    assert ok, detail
    text = _deployed(elsewhere)
    assert install.SKILL_RULES in text
    assert install.HERE in text


def test_rules_md_is_never_copied_out(elsewhere):
    """規則本體只能有一份。複製出去的那份會安靜過期，而過期跟正確長得一樣。"""
    install.install_skill()
    folder = os.path.dirname(elsewhere / ".claude" / "skills" / "luadetect" / "SKILL.md")
    assert os.listdir(folder) == ["SKILL.md"]


def test_the_front_matter_stays_at_the_top(elsewhere):
    """frontmatter 不在第一行 Claude Code 就不載入這個 skill，而且不會報錯。"""
    install.install_skill()
    lines = _deployed(elsewhere).splitlines()
    assert lines[0] == "---"
    assert "name: luadetect" in lines[1:6]
    assert lines.index(install.SKILL_MARK) > lines.index("---", 1)


def test_a_moved_tool_shows_up_as_stale_not_as_ok(elsewhere, monkeypatch):
    """搬過家的部署讀起來完全正常，只是指向不存在的位置——狀態要講出來。"""
    install.install_skill()
    assert install.skill_state()[0] == "ok"
    monkeypatch.setattr(install, "HERE", str(elsewhere / "moved"))
    state, detail = install.skill_state()
    assert state == "stale" and "重跑" in detail


def test_someone_elses_skill_is_not_overwritten(elsewhere):
    path = install.user_skill_path()
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("---\nname: luadetect\n---\n\n別人的 skill\n")
    ok, detail = install.install_skill()
    assert not ok and "別人的" in detail
    with open(path, encoding="utf-8") as fh:
        assert "別人的 skill" in fh.read()
    # 解除安裝也不可以把它刪掉
    assert not install.remove_skill()[0]
    assert os.path.isfile(path)


def test_removing_a_skill_that_was_never_deployed_is_not_a_failure(elsewhere):
    ok, detail = install.remove_skill()
    assert ok and "本來就沒" in detail


def test_deploy_is_idempotent(elsewhere):
    install.install_skill()
    first = _deployed(elsewhere)
    assert install.install_skill()[0]
    assert _deployed(elsewhere) == first
    assert install.skill_state()[0] == "ok"


def test_render_without_front_matter_still_carries_the_pointer():
    """SKILL.md 哪天沒了 frontmatter 也不能默默丟掉指標。"""
    rendered = install.render_skill("# 標題\n\n內文\n")
    assert install.SKILL_MARK in rendered and install.SKILL_RULES in rendered
