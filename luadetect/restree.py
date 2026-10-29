"""資源根的索引：`資料夾 → 檔名集合`，以及「這個路徑算不算缺檔」的判斷。

跟 `csdtree` 是同一個道理，只是 ground truth 從 `.csd` 換成了圖檔、plist、spine、
音檔這些**共用資源**。Lua 裡寫的是字串路徑，寫錯或資源被刪掉編譯期一樣沒有保護，
要到玩家開到那個畫面才發現圖沒出來、或是 `create()` 回 nil 當場炸掉。

**判斷方式刻意保守：只在「所在資料夾解析得到、檔案卻不在裡面」時才算缺檔。**
資料夾整個對不到就回 `unresolved`，什麼都不報。這一條是這個規則能不能用的關鍵：
專案裡的字面路徑有一大半是相對於執行期的資源搜尋路徑（或屬於別的資源包，
那些根本不在這台機器上），拿「檔案找不到」直接當問題的話實測有 8.67% 誤報；
加上這個前提之後降到 1.18%，而且剩下的每一筆都驗證過是真的缺檔。
"""
from __future__ import annotations

import os

# Lua 裡會出現的資源副檔名。刻意列舉而不是「有副檔名就算」——
# `.lua`、`.txt`、`.json`（設定檔）這些不是共用資源，判斷基準也不一樣。
#
# **`.csb` 刻意不在裡面**，兩個理由：它是 Cocos Studio 從 `.csd` 編出來的產物，
# 原始碼樹裡本來就不一定有（沒發佈過就沒有），拿「檔案不在」當問題會變成
# 「你還沒發佈」的雜訊；而且節點規則已經在驗 `.csb`了——它會對到 `.csd`，
# 對不到就報 `NODE_CSB_UNRESOLVED`。同一件事不要兩組規則各報一次。
EXTENSIONS = (".png", ".jpg", ".jpeg", ".plist", ".mp3", ".wav", ".ogg",
              ".m4a", ".atlas", ".skel", ".fnt", ".ttf", ".pvr", ".pvr.ccz")

# 跟 csdtree 一樣的前綴：同一份資源在 Lua 裡有好幾種寫法。
# 這裡只放跟專案無關的；專案自己的資源容器由 `projects.path_prefixes()` 算出來傳進來。
DEFAULT_PREFIXES = ("res/", "cocosstudio/")

SKIP_DIRS = {".git", ".svn", "node_modules", "__pycache__", "build", "temp", "Temp",
             ".vs", ".idea", "obj", "bin", "out"}

# 資源容器的**名字結尾**，不是完整名字。寫死 `InannaResource` 換個專案就找不到，
# 而找不到資源根的表現是「resources 規則整組跳過」——不是錯誤，是安靜地不檢查。
_BUNDLE_SUFFIXES = ("resource", "resources")


def dirs_matching(directory: str, accept, depth: int = 2) -> list:
    """從專案根往下找名字符合 `accept()` 的資料夾，最多 `depth` 層，淺的排前面。

    **不能只看頂層。** 實測 ManganDahen 用的是一模一樣的命名慣例，只是多包了
    一層：`ClientCocos/MDLua`、`ClientCocos/MDResource`。只看頂層的話 Lua 根會
    退回整個倉庫（連 `Gt2Server`、`ClientFlash` 一起掃）、資源根整個找不到——
    而**這兩件事都不會報錯**，只表現成涵蓋率低，掃出來看起來像通過。

    深度停在 2：再深就開始把 `build/`、第三方目錄那種同名資料夾也收進來，
    而收錯資源根的代價是誤報，比涵蓋率低糟（A3）。真的更深就在
    `.luadetect.json` 明講。

    **往下鑽的時候不進入另一個 repo。** 沒有這條界線，`Mangan_git`（底下放了
    十幾個 repo 的那種資料夾）會因為兩層下有 `Inanna/InannaLua` 而被當成一個
    專案根，`_find_upwards` 從任何地方往上走都會撞到它，於是解到一個
    lua_root = 別人整個倉庫的假專案——實測就是這樣壞的。`ClientCocos` 不是
    repo，照樣鑽得進去。

    排序固定（同一層照名字排）：`os.listdir` 的順序一變，資源根的順序、由它算出
    的前綴順序都跟著飄，而飄掉的方式一樣是安靜換一個答案。
    """
    out: list = []
    level = [""]
    for _ in range(max(depth, 1)):
        children: list = []
        for parent in level:
            full = os.path.join(directory, *parent.split("/")) if parent else directory
            try:
                names = sorted(os.listdir(full))
            except OSError:
                continue
            for name in names:
                if name in SKIP_DIRS or name.startswith("."):
                    continue
                child = os.path.join(full, name)
                if not os.path.isdir(child):
                    continue
                rel = f"{parent}/{name}" if parent else name
                if accept(name):
                    out.append(rel)
                elif not _is_own_project(child):
                    children.append(rel)
        level = children
        if not level:
            break
    return out


def _is_own_project(path: str) -> bool:
    """這個資料夾自己就是一個專案根嗎（有 `.git` 或 `.luadetect.json`）。

    `.git` 在主 repo 是目錄、在 linked worktree 是檔案，所以用 `exists` 不是 `isdir`。
    """
    return (os.path.exists(os.path.join(path, ".git"))
            or os.path.isfile(os.path.join(path, ".luadetect.json")))


def bundle_dirs(directory: str, depth: int = 2) -> list:
    """看起來像資源容器（`*Resource(s)`）的資料夾，相對於專案根。

    比對結尾而不是完整名字：`InannaResource`、`MDResource`、`Resources`
    都要收得到——寫死 `InannaResource` 換個專案就找不到。
    """
    return dirs_matching(directory,
                         lambda name: name.lower().endswith(_BUNDLE_SUFFIXES),
                         depth)


def bundle_scope(directory: str, lua_root: "str | None") -> str:
    """從哪一層開始找資源容器：Lua 根的上一層，而不是整個倉庫。

    一個倉庫裡不只一個客戶端。ManganDahen 的 `ClientFlash/resource` 是 **Flash**
    那一版的資源，跟 Cocos 的 Lua 無關；整個倉庫找的話會把它一起掛成搜尋路徑，
    而多掛一棵不相干的資源樹**只會製造誤報**——資料夾突然存在了，於是本來
    `unresolved`（安靜跳過）的路徑變成 `missing`（報出來）。誤報比漏報糟（A3）。

    Lua 根是「沒找到而退回專案根」的時候不能往上跳，那會跑出專案外面。
    """
    if not lua_root:
        return directory
    parent = os.path.dirname(os.path.abspath(lua_root.rstrip("\\/")))
    root = os.path.abspath(directory)
    if os.path.normcase(parent) == os.path.normcase(root):
        return directory
    try:
        inside = not os.path.relpath(parent, root).startswith("..")
    except ValueError:                      # 跨磁碟
        inside = False
    return parent if inside else directory


def default_roots(directory: str, csd_root: "str | None",
                  lua_root: "str | None" = None) -> list:
    """自動猜資源根。

    - `<專案>/*Resource(s)`（`InannaResource`、`AerithResource`、`Resources` …）：
      以及**它底下的每一個子目錄**，見下
    - `<cocosstudio 的兄弟>/res`：Cocos Studio 專案匯出的資源樹
    - `<cocosstudio>` 本身

    **`*Resource` 這種資料夾要連它的子目錄一起收。** 執行期是用 `addSearchPath`
    把 `InannaResource/Inanna`、`InannaResource/Sound` 這些各自掛成搜尋路徑，
    所以 Lua 寫 `grandwheel/vfx/x.skel` 指的是 `InannaResource/Inanna/grandwheel/...`
    ——中間那層在路徑裡是看不到的。只收 `InannaResource` 當根的話，那一批全部
    會被判成「資料夾在（因為 res/ 底下也有 grandwheel/vfx）、檔案不在」，
    實測是 30 筆誤報，而檔案其實好好地在另一個根底下。

    `res` 與 `cocosstudio` **不做**這個展開：它們自己就是搜尋路徑，底下那 80 個
    子目錄是模組資料夾，展開會讓 `vfx/x.png` 對到任何模組的 vfx，太鬆。

    猜不到就回空的，規則會整組跳過（跟「查不出對應 .csb」一樣的處理）。
    """
    found: list = []

    def add(path: "str | None") -> None:
        if path and os.path.isdir(path):
            key = os.path.normcase(os.path.abspath(path))
            if all(os.path.normcase(os.path.abspath(x)) != key for x in found):
                found.append(os.path.abspath(path))

    def add_bundle(path: str) -> None:
        """`*Resource` 容器：它本身與它底下的每個 bundle 都是搜尋路徑。"""
        if not os.path.isdir(path):
            return
        add(path)
        try:
            for name in sorted(os.listdir(path)):
                child = os.path.join(path, name)
                if os.path.isdir(child) and name not in SKIP_DIRS \
                        and not name.startswith("."):
                    add(child)
        except OSError:
            pass

    scope = bundle_scope(directory, lua_root)
    for name in bundle_dirs(scope):
        add_bundle(os.path.join(scope, name))
    add(os.path.join(scope, "res"))
    if os.path.normcase(os.path.abspath(scope)) != os.path.normcase(os.path.abspath(directory)):
        add(os.path.join(directory, "res"))
    if csd_root:
        add(os.path.join(os.path.dirname(csd_root), "res"))
        add(csd_root)
    return found


class Bundle:
    """一組資源根。索引是走訪出來的，第一次用到才建（實測 0.36 秒 / 2.8 萬個檔案）。

    刻意不做磁碟快取：0.36 秒可以接受，而快取就要處理失效，那一整個面向的複雜度
    不值得——名稱快取那邊已經有過簽章碰撞與寫壞的問題了。
    """

    def __init__(self, roots: list, prefixes: "tuple | list | None" = None):
        self.roots = list(roots)
        self.prefixes = tuple(prefixes) if prefixes else DEFAULT_PREFIXES
        self._folders: "dict | None" = None

    @property
    def folders(self) -> dict:
        """資料夾相對路徑（小寫、正斜線；根目錄是 `""`）→ 該層的檔名集合（小寫）。"""
        if self._folders is None:
            found: dict = {}
            for root in self.roots:
                for dirpath, dirs, files in os.walk(root):
                    dirs[:] = [d for d in dirs
                               if d not in SKIP_DIRS and not d.startswith(".")]
                    rel = os.path.relpath(dirpath, root).replace("\\", "/").lower()
                    bucket = found.setdefault("" if rel == "." else rel, set())
                    bucket.update(name.lower() for name in files)
            self._folders = found
        return self._folders

    def candidates(self, path: str) -> list:
        """一個 Lua 路徑可能對應的相對路徑寫法（原樣，加上剝掉已知前綴的）。"""
        low = path.replace("\\", "/").lstrip("./").lower()
        out = [low]
        for prefix in self.prefixes:
            if low.startswith(prefix):
                out.append(low[len(prefix):])
        return out

    def verdict(self, path: str) -> str:
        """`ok`（找到了）、`missing`（資料夾在、檔案不在）、`unresolved`（資料夾都對不到）。

        候選一定要**全部試完**才能說 missing：任何一個候選的資料夾裡有這個檔案就算
        找到。試到第一個存在的資料夾就回答會把 `InannaResource/Inanna/GameAllPic.plist`
        判成缺檔——剝掉 `inannaresource/inanna/` 之後落在索引根，那一層當然沒有它。
        """
        folders = self.folders
        seen = False
        for candidate in self.candidates(path):
            folder, _, name = candidate.rpartition("/")
            bucket = folders.get(folder)
            if bucket is None:
                continue
            seen = True
            if name in bucket:
                return "ok"
        return "missing" if seen else "unresolved"
