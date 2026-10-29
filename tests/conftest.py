"""測試的共用設定。

**測試必須是密閉的。** 原本 `projects.known()` 會去讀開發者真實的
`~/.csdview_projects`，還會從 `os.getcwd()` 往上找專案根——tmp_path 剛好落在某個
已登錄專案底下時就會解到錯的 workspace，而且結果因機器而異。這裡把兩個來源都指到
空的地方，測試只看自己造出來的專案。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# 真實環境的值，在被隔離覆蓋之前先留一份。`test_project.py` 要拿它跑真專案，
# 那是唯一刻意不密閉的測試（也因此會在專案不在這台機器上時自己跳過）。
REAL_ENVIRONMENT = {name: os.environ.get(name)
                    for name in ("CSDVIEW_PROJECTS", "LUADETECT_ROOTS",
                                 "HOME", "USERPROFILE")}


@pytest.fixture
def real_environment(monkeypatch):
    """把 `_isolated_environment` 的覆蓋還原回去。"""
    for name, value in REAL_ENVIRONMENT.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


_REAL_HOME = (REAL_ENVIRONMENT.get("USERPROFILE") or REAL_ENVIRONMENT.get("HOME")
              or os.path.expanduser("~"))
_REAL_CACHE = os.path.join(_REAL_HOME, ".luadetect_cache")


def _cache_entries() -> set:
    try:
        return set(os.listdir(_REAL_CACHE))
    except OSError:
        return set()


@pytest.fixture(autouse=True)
def _nothing_leaks_into_the_real_home(tmp_path_factory):
    """測試不可以在使用者真正的家目錄裡留東西。

    這條網子是補回來的。`monkeypatch` 是 function scope，上面那個隔離 fixture 跟
    測試本身拿到的是**同一個實例**——測試裡呼叫 `monkeypatch.undo()` 會把 `HOME`
    的隔離一起還原掉，於是名稱快取寫進真正的 `~/.luadetect_cache`。
    測試照樣全綠，只是每跑一次留一個檔（實測累積了 178 個才被發現）。

    只認「root 指向這次 pytest 暫存目錄」的那些：`test_project.py` 是刻意不密閉的，
    它掃真專案、動到真專案的快取檔是正常的。
    """
    before = _cache_entries()
    yield
    base = os.path.normcase(str(tmp_path_factory.getbasetemp()))
    leaked = []
    for name in sorted(_cache_entries() - before):
        path = os.path.join(_REAL_CACHE, name)
        try:
            with open(path, encoding="utf-8") as fh:
                root = json.load(fh).get("root") or ""
        except (OSError, ValueError):
            continue
        if os.path.normcase(root).startswith(base):
            leaked.append(f"{name} → {root}")
            try:
                os.remove(path)          # 順手收掉，不要留給下一次跑
            except OSError:
                pass
    assert not leaked, (
        f"測試寫進了使用者真正的 {_REAL_CACHE}：{leaked}。"
        "多半是 monkeypatch.undo() 把這裡的 HOME 隔離一起還原了——"
        "改用 pytest.MonkeyPatch.context()，只還原自己那一段。")


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path_factory, monkeypatch):
    empty = tmp_path_factory.mktemp("registry") / "none.json"
    monkeypatch.setenv("CSDVIEW_PROJECTS", str(empty))
    monkeypatch.delenv("LUADETECT_ROOTS", raising=False)
    # 名稱快取寫到 HOME 底下，別碰使用者真正的那一份
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
