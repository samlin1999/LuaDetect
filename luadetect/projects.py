"""要檢查哪個專案的哪些 Lua——路徑解析全部集中在這裡。

不寫死任何專案路徑。一個「專案」對這個工具而言是兩個根目錄的配對：

- **Lua 根**：`getChildByName` 那些程式碼在哪
- **cocosstudio 根**：對應的 `.csd` 在哪

這個配對 csdview 的登錄檔 `~/.csdview_projects` 早就記了（`directory` 是程式碼倉庫、
`csdRoot` 是 cocosstudio），所以直接共用同一份，不另立一套要使用者再設定一次。

來源優先序：

1. 呼叫時直接給的路徑
2. 專案根目錄下的 `.luadetect.json`（要覆寫 Lua 根或加例外時才需要）
3. 環境變數 `LUADETECT_ROOTS`（用 `os.pathsep` 分隔的程式碼倉庫路徑）
4. csdview 的登錄檔
5. 當前工作目錄往上找
"""
from __future__ import annotations

import json
import os

from . import csdtree
from . import fileio
from . import report
from . import restree
from .csdtree import Project as CsdProject

_ENV_ROOTS = "LUADETECT_ROOTS"
CONFIG_NAME = ".luadetect.json"

# Lua 根目錄的候選名字，由確定到寬鬆。
# 另外還會收專案根底下叫做 `*Lua` 的資料夾（`InannaLua`、`AerithLua` …）——
# 那是這個團隊的命名慣例，但**專案名不寫死在程式碼裡**：寫死換個專案就找不到，
# 而找不到的表現是「退回專案根整個掃」，不是錯誤訊息。
_LUA_DIRS = ("src/lua", "lua", "scripts/lua")
_LUA_SUFFIX = "lua"


def lua_dir_candidates(directory: str, depth: int = 2) -> list:
    """這個專案根底下，看起來像 Lua 根的相對路徑，由確定到寬鬆。

    先試固定清單，再收任何 `*Lua` 資料夾（`InannaLua`、`MDLua` …）。
    後者預設往下找兩層——ManganDahen 的是 `ClientCocos/MDLua`，只看頂層就會退回
    整個倉庫，而「退回」不會有任何訊息，只是默默多掃了幾個不相干的子系統。

    **`depth=1` 是給「這一層是不是專案根」用的**，見 `_find_upwards`：那個問題
    往下看兩層會反過來咬人——只要底下擺著幾個專案資料夾，那個**容器**就會被
    當成專案根（實測 `pytest-239/` 底下一堆 `test_x/InannaLua` 就會這樣）。
    已經確定是專案根之後再問「Lua 在哪」，才可以往下找。
    """
    out = [name for name in _LUA_DIRS
           if os.path.isdir(os.path.join(directory, *name.split("/")))]
    for rel in restree.dirs_matching(
            directory,
            lambda name: name.lower().endswith(_LUA_SUFFIX)
            and name.lower() != _LUA_SUFFIX,
            depth):
        if rel not in out:
            out.append(rel)
    return out


def path_prefixes(directory: str, csd_root: "str | None",
                  resource_roots: "list | None" = None,
                  config: "dict | None" = None) -> tuple:
    """Lua 裡的資源／`.csb` 路徑可以剝掉哪些前綴，由長到短。

    以前這份清單是寫死的 `("inannaresource/inanna/", "inannaresource/", ...)`。
    換一個專案那兩項就永遠對不到，而**對不到不會有錯誤訊息**——只是 `.csb` 解析
    不到 `.csd`、資源路徑判成 `unresolved`，兩者都被算進「沒檢查」而安靜跳過。
    所以改成從實際找到的資源根反推：`<專案>/InannaResource/Inanna` 就會產出
    `inannaresource/inanna/`，同一套邏輯在任何專案都成立。

    **由長到短排序是必要的，不只是好看。** `csdtree.resolve()` 的尾段比對用的是
    `tries[-1]`，也就是「最短的那個前綴剝完剩下的東西」；順序一亂，尾段比對的
    對象就變了，而它的失敗方式一樣是安靜地對到別份 .csd。
    """
    found = set(csdtree.DEFAULT_PREFIXES)
    for root in list(resource_roots or []) + ([csd_root] if csd_root else []):
        rel = fileio.relative(root, directory).lower()
        if rel and not rel.startswith("../") and rel != ".":
            found.add(rel.rstrip("/") + "/")
    for item in (config or {}).get("pathPrefixes") or []:
        if isinstance(item, str) and item.strip():
            found.add(item.strip().replace("\\", "/").lstrip("./").lower().rstrip("/") + "/")
    return tuple(sorted(found, key=lambda p: (-len(p), p)))


class ProjectError(Exception):
    """路徑解析失敗；訊息會直接回給呼叫端，所以要寫得能照著解決。"""


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def registry_path() -> str:
    """csdview 登錄檔的位置。

    每次呼叫都重讀環境變數，不在 import 時定案：長駐的 MCP 行程與測試都需要能換掉
    它，而 import 時就固定的話，改了環境變數也沒有效果。
    """
    return (os.environ.get("CSDVIEW_PROJECTS")
            or os.path.join(os.path.expanduser("~"), ".csdview_projects"))


_registry_problem: "str | None" = None


def _load_registry() -> list:
    """讀 csdview 的登錄檔。壞掉時記下原因，好讓錯誤訊息講得出真正的問題。

    原本壞檔與沒檔都回空清單，於是登錄檔壞掉時使用者看到的是
    「找不到任何專案，請在專案根目錄執行」——完全指錯方向。
    """
    global _registry_problem
    _registry_problem = None
    path = registry_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        _registry_problem = f"讀不了 {path}：{exc}"
        return []
    except ValueError as exc:
        _registry_problem = f"{path} 不是有效的 JSON：{exc}"
        return []
    if not isinstance(data, list):
        _registry_problem = f"{path} 的內容不是陣列。"
        return []
    return [item for item in data if isinstance(item, dict) and item.get("directory")]


def load_config(directory: str) -> dict:
    """讀專案根目錄下的 `.luadetect.json`；沒有就回空的。

    **內容壞掉一定要報錯，不能當成沒有設定。** 這份設定裡的 `csdRoot` 決定節點檢查
    對得到哪些 .csd，安靜忽略的話一個逗號打錯就會讓整組節點規則停止工作，而報告
    只會說「這個專案沒有設定 cocosstudio 根目錄」——根本查不到是設定檔壞了。
    """
    path = os.path.join(directory, CONFIG_NAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ProjectError(f"讀不了 {path}：{exc}") from exc
    except ValueError as exc:
        raise ProjectError(f"{path} 不是有效的 JSON：{exc}。"
                           "修好它，或先改名把它移開。") from exc
    if not isinstance(data, dict):
        raise ProjectError(f"{path} 的根節點必須是物件（{{...}}）。")
    return data


def _lua_root(directory: str, config: dict) -> "str | None":
    """這個專案的 Lua 根。找不到已知的子目錄時退回專案根本身。

    退回是必要的：有些倉庫的 Lua 就放在最上層（登錄檔裡的 TMDC_Aerith 就是）。
    但「退回」與「真的找到」要分得出來，見 `_lua_root_is_certain`。
    """
    override = config.get("luaRoot")
    if override:
        full = override if os.path.isabs(override) else os.path.join(directory, override)
        return full if os.path.isdir(full) else None
    for name in lua_dir_candidates(directory):
        return os.path.join(directory, *name.split("/"))
    return directory if os.path.isdir(directory) else None


def load_config_safely(directory: str) -> dict:
    """讀設定，壞掉時回空的——只用在「這一層算不算專案」這種探索性的判斷上。

    真的要用那個專案時走 `load_config`，壞掉就報錯。
    """
    try:
        return load_config(directory)
    except ProjectError:
        return {}


def _lua_root_is_certain(directory: str, config: dict) -> bool:
    """這個 Lua 根是「找到的」還是「沒得選才退回專案根」？

    差別在自動探索的時候會咬人：從 cwd 往上找只要看到 `.git` 就會停下來，而
    `_lua_root` 又會退回那一層本身，於是「我上面某一層剛好是個 git 倉庫」就變成
    一個 lua_root = 整棵上層目錄的假專案。真的去掃它會走遍整個倉庫，而且可能蓋掉
    使用者本來要用的專案。人工指定或登錄檔裡的專案不受影響——那是刻意的。
    """
    if config.get("luaRoot"):
        return True
    return bool(lua_dir_candidates(directory))


def _find_upwards(start: str) -> "str | None":
    """從某個位置往上找專案根（有 .luadetect.json、.git，或看得到 Lua 根的那一層）。"""
    current = os.path.abspath(start)
    if os.path.isfile(current):
        current = os.path.dirname(current)
    for _ in range(16):
        if os.path.isfile(os.path.join(current, CONFIG_NAME)):
            return current
        # `.git` 在主 repo 是目錄、在 linked worktree 是檔案（內容是 gitdir: ...）。
        # 只認目錄的話會從 worktree 往上爬出去，多開 agent 時每個 agent 一個
        # worktree，那就會解到別的專案。
        if os.path.exists(os.path.join(current, ".git")):
            return current
        # 只看一層：往下兩層會把「底下放了幾個專案的資料夾」也認成專案根，
        # 而從任何地方往上走都會撞到那種容器。上面兩個標記才是真正的根。
        if lua_dir_candidates(current, depth=1):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


class Workspace:
    """一組配對好的 Lua 根與 cocosstudio 根，外加專案自己的設定。"""

    def __init__(self, directory: str, lua_root: str, csd_root: "str | None",
                 source: str, config: "dict | None" = None,
                 config_error: "str | None" = None):
        self.directory = directory
        self.lua_root = lua_root
        self.csd_root = csd_root
        self.source = source
        self.config = config or {}
        self.config_error = config_error      # .luadetect.json 壞掉時的原因
        self._csd: "CsdProject | None" = None
        self._resources = None
        self._resources_ready = False
        self._resource_roots: list = []

    @property
    def name(self) -> str:
        return os.path.basename(self.directory.rstrip("\\/")) or self.directory

    @property
    def prefixes(self) -> tuple:
        """Lua 路徑可以剝掉的前綴，從這個專案實際的資源根算出來。

        先碰 `resources` 是必要的：資源根是在那裡解析的，沒解析過就只剩通用前綴，
        於是 `.csb` 對不到 `.csd`——而那是安靜地掉涵蓋率，不會有錯誤訊息。
        """
        self.resources                                     # noqa: B018 逼它解析資源根
        return path_prefixes(self.directory, self.csd_root,
                             self._resource_roots, self.config)

    @property
    def csd(self) -> "CsdProject | None":
        if self.csd_root and self._csd is None:
            self._csd = CsdProject(self.csd_root, self.prefixes)
        return self._csd

    @property
    def resources(self):
        """共用資源的索引；猜不到資源根就回 None（那一組規則整組跳過）。

        `.luadetect.json` 的 `resourceRoots` 可以覆寫，值可以是絕對路徑或相對於
        專案根。沒設就自動找——`*Resource(s)/`、cocosstudio 旁邊的 `res/`。
        """
        if not self._resources_ready:
            self._resources_ready = True
            given = self.config.get("resourceRoots")
            if given:
                roots = [item if os.path.isabs(item)
                         else os.path.join(self.directory, item)
                         for item in (given if isinstance(given, list) else [given])]
                roots = [r for r in roots if os.path.isdir(r)]
            else:
                roots = restree.default_roots(self.directory, self.csd_root,
                                              self.lua_root)
            self._resource_roots = roots
            prefixes = path_prefixes(self.directory, self.csd_root, roots, self.config)
            self._resources = restree.Bundle(roots, prefixes) if roots else None
        return self._resources

    def to_dict(self) -> dict:
        out = {"name": self.name, "directory": self.directory,
               "luaRoot": self.lua_root, "csdRoot": self.csd_root,
               "source": self.source,
               "hasConfig": bool(self.config),
               "csdAvailable": bool(self.csd_root and os.path.isdir(self.csd_root)),
               "resourceRoots": [r for r in (self.resources.roots
                                             if self.resources else [])]}
        if self.config_error:
            out["configError"] = self.config_error
        return out


def known() -> list:
    """列出所有認得的專案。"""
    out: list = []
    seen = set()

    def add(directory: str, csd_root: "str | None", source: str) -> None:
        directory = os.path.abspath(directory)
        if not os.path.isdir(directory):
            return
        key = _norm(directory)
        if key in seen:
            return
        # 一個專案的設定壞掉不該讓整份清單列不出來，但選到它時要當場報錯，
        # 所以把問題記在 Workspace 上，由 resolve() 決定要不要拋。
        problem = None
        try:
            config = load_config(directory)
        except ProjectError as exc:
            config, problem = {}, str(exc)
        lua_root = _lua_root(directory, config)
        if not lua_root:
            return
        seen.add(key)
        root = config.get("csdRoot") or csd_root
        out.append(Workspace(directory, lua_root, root, source, config, problem))

    for raw in (os.environ.get(_ENV_ROOTS) or "").split(os.pathsep):
        raw = raw.strip()
        if raw:
            add(raw, None, _ENV_ROOTS)

    for entry in _load_registry():
        add(entry["directory"], entry.get("csdRoot"), "csdview 登錄檔")

    here = _find_upwards(os.getcwd())
    # 自動探索出來的那一層，只有在「看得出是 Lua 專案」時才算——光是上面有個
    # .git 不代表什麼，收進來會變成一個 lua_root = 整棵上層目錄的假專案。
    if here and (os.path.isfile(os.path.join(here, CONFIG_NAME))
                 or _lua_root_is_certain(here, load_config_safely(here))):
        add(here, None, "工作目錄")
    return out


def _containing(entries: list, target: str) -> list:
    """`target` 落在哪些專案裡面（可能有巢狀的，所以回傳全部）。"""
    low = _norm(target)
    return [w for w in entries
            if low == _norm(w.lua_root)
            or low.startswith(_norm(w.lua_root) + os.sep)
            or low == _norm(w.directory)
            or low.startswith(_norm(w.directory) + os.sep)]


def _match_project(project: str, entries: list, picked) -> Workspace:
    """把明確指定的專案名或資料夾對到一個 Workspace；對不到就報錯。

    刻意不回 `None`——「對不到」在這裡永遠是使用者的輸入錯了，
    悄悄退回別的專案比直接失敗糟得多。
    """
    needle = project.strip().lower()
    chosen = [w for w in entries
              if w.name.lower() == needle
              or _norm(w.directory) == _norm(project)
              or _norm(w.lua_root) == _norm(project)]
    if chosen:
        return picked(chosen[0])
    if os.path.isdir(project):
        config = load_config(project)
        lua_root = _lua_root(project, config)
        if lua_root:
            return Workspace(os.path.abspath(project), lua_root,
                             config.get("csdRoot"), "直接指定", config)
    names = "、".join(w.name for w in entries) or "（沒有任何已登錄專案）"
    raise ProjectError(
        f"找不到專案 '{project}'。已知的專案：{names}。"
        "用 list_projects 看完整清單，或直接給資料夾路徑。")


def resolve(project: "str | None" = None, path: "str | None" = None) -> Workspace:
    """挑出要用的專案。

    優先序：給的 `path` → 給的 `project` → **當前工作目錄所在的專案** → 登錄檔第一個。

    工作目錄那一層不是可有可無的。pre-commit hook 不能把專案路徑寫死（同一份 hook
    由主 repo 與所有 linked worktree 共用），所以它是靠 cwd 判斷自己在哪個倉庫；
    原本沒給參數時直接回 `entries[0]`，於是在**別的**倉庫 commit 時，hook 會去檢查
    登錄檔裡的第一個專案——那邊沒有暫存的異動，於是回報「沒有異動」並放過這次
    commit。實測過：hook 裝好了、commit 有問題的內容照樣過。
    """
    entries = known()

    def picked(workspace: Workspace) -> Workspace:
        """選定一個專案；它的設定壞掉的話現在就報錯，不要帶著半套設定往下跑。"""
        if workspace.config_error:
            raise ProjectError(workspace.config_error)
        return workspace

    # 明確給了 `project` 就**先驗證它存在**，即使 `path` 的優先序比較高。
    #
    # 不先驗證的話，打錯專案名會被靜靜忽略：實測在 Inanna 目錄下跑
    # `luadetect voucherCenter --project NoSuchProj`，`path` 分支先解析成功，
    # 於是拿 cwd 的專案掃完並回報「通過、離開碼 0」——CI 裡一個 typo
    # 就會變成「檢查過了」，這是這個工具最不能出現的失敗方式。
    if project:
        _match_project(project, entries, picked)

    if path:
        inside = _containing(entries, path)
        if inside:
            # 有多個時取 Lua 根最長的那個（最貼近的巢狀專案）
            return picked(max(inside, key=lambda w: len(_norm(w.lua_root))))
        found = _find_upwards(path)
        if found:
            config = load_config(found)
            lua_root = _lua_root(found, config)
            if lua_root:
                return Workspace(found, lua_root, config.get("csdRoot"), "路徑推斷", config)

    if project:
        return _match_project(project, entries, picked)

    if not entries:
        raise ProjectError(
            "找不到任何專案。請在專案根目錄執行，或用 project 指定資料夾；"
            f"也可以在專案根目錄放一份 {CONFIG_NAME} 標明 luaRoot 與 csdRoot。"
            + (f"\n  另外：{_registry_problem}" if _registry_problem else ""))

    # 什麼都沒給：以「我現在站在哪個專案裡」為準，而不是登錄檔的順序
    here = _containing(entries, os.getcwd())
    if here:
        return picked(max(here, key=lambda w: len(_norm(w.lua_root))))
    return picked(entries[0])


def lua_files(workspace: Workspace, subpath: "str | None" = None) -> list:
    """列出要掃的 .lua，套用設定裡的排除規則。"""
    base = workspace.lua_root
    if subpath:
        candidate = subpath if os.path.isabs(subpath) else os.path.join(base, subpath)
        if os.path.isfile(candidate):
            return [os.path.abspath(candidate)]
        if os.path.isdir(candidate):
            base = candidate
        else:
            # 也接受相對於專案根、或模組名稱
            for guess in (os.path.join(workspace.directory, subpath),
                          os.path.join(workspace.lua_root, "Modules", subpath)):
                if os.path.isfile(guess):
                    return [os.path.abspath(guess)]
                if os.path.isdir(guess):
                    base = guess
                    break
            else:
                raise ProjectError(f"找不到 '{subpath}'（試過 {workspace.lua_root} 與模組名稱）。")

    excludes = [str(e).replace("\\", "/").lower()
                for e in (workspace.config.get("exclude") or [])]
    out: list = []
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for name in files:
            if not name.lower().endswith(".lua"):
                continue
            full = os.path.join(dirpath, name)
            rel = report.relative(full, workspace.lua_root).lower()
            if any(pattern in rel for pattern in excludes):
                continue
            out.append(os.path.abspath(full))
    return sorted(out)
