"""把 .csd 讀成節點樹，語意跟**執行期**一致。

刻意自己解 XML 而不是叫 csdview：一次全庫掃描要碰上千個檔案，每個都開一次子行程
是幾分鐘對幾秒的差別。這裡只要節點名稱與階層，XML 直接讀就夠。

有一處跟 csdview 不同，而且是**這裡對**：`ProjectNodeObjectData` 引用另一個 .csd 時，
執行期 CSLoader 是把子檔的根節點載進來、再改名成引用處的名字，所以子檔根節點的
子節點在 Lua 端是**直接子節點**——`infoPage:getChildByName( "Bg" )` 拿得到。
csdview 為了畫面呈現保留了那一層根節點（路徑會是 `infoPage/Node/Bg`），
拿它的路徑來對 Lua 的取用鏈會整串對不上。已用 VoucherCenter 的線上程式碼驗證過。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET

from . import fileio

SKIP_DIRS = {".git", ".svn", "node_modules", "__pycache__", "build", "temp", "Temp",
             ".vs", ".idea", "obj", "bin", "out"}

# .csb 路徑在 Lua 裡有好幾種寫法，都指向同一個 cocosstudio 底下的相對路徑。
# 由長到短試，先命中的優先。
#
# 這裡只放**跟專案無關**的那幾個。專案自己的資源容器（`InannaResource/Inanna/`
# 這一類）不寫死在程式碼裡：那是專案名，換一個專案就不對了，而且不對的方式是
# 「安靜地對不到 .csd」——只會讓涵蓋率掉，不會有任何錯誤訊息。
# 由 `projects.path_prefixes()` 從實際的資源根算出來後傳進來。
DEFAULT_PREFIXES = ("res/", "cocosstudio/")

_NAME_ATTR = re.compile(r'\bName="([^"]*)"')


class CsdError(Exception):
    """讀檔或解析失敗；訊息直接回給呼叫端。"""


class Node:
    __slots__ = ("name", "type", "children", "source", "_index")

    def __init__(self, name: str, ntype: str, source: str):
        self.name = name
        self.type = ntype
        self.children: list = []
        self.source = source          # 這個節點來自哪個 .csd（巢狀展開後才分得出來）
        self._index: "dict | None" = None     # 見 name_index()

    def to_dict(self, depth: int = 99) -> dict:
        out = {"name": self.name, "type": self.type}
        if depth > 0 and self.children:
            out["children"] = [c.to_dict(depth - 1) for c in self.children]
        elif self.children:
            out["childCount"] = len(self.children)
        return out


def _object_data(path: str) -> "ET.Element | None":
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise CsdError(f"讀不了 {path}：{exc}") from exc
    # GameFile/Content/Content/ObjectData
    for content in root.iter("Content"):
        found = content.find("ObjectData")
        if found is not None:
            return found
    return None


def _ctype(element: ET.Element) -> str:
    raw = element.get("ctype") or ""
    for suffix in ("ObjectData", "Data"):
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return raw or "Node"


def _nested_path(element: ET.Element) -> "str | None":
    file_data = element.find("FileData")
    if file_data is None:
        return None
    path = (file_data.get("Path") or "").strip().replace("\\", "/")
    return path if path.lower().endswith(".csd") else None


class Project:
    """一個 cocosstudio 根目錄：負責索引、路徑解析與樹的快取。"""

    def __init__(self, root: str, prefixes: "tuple | list | None" = None):
        self.root = os.path.abspath(root)
        self.prefixes = tuple(prefixes) if prefixes else DEFAULT_PREFIXES
        self._index: "dict | None" = None
        self._trees: dict = {}        # 只放沒被循環截斷的樹，tree() 與 _expand() 共用
        self._names: "set | None" = None
        self._cycle = False           # 這一輪展開有沒有被循環引用截斷
        self.unreadable = 0           # global_names() 讀不到的 .csd 數
        self._by_folder: "dict | None" = None

    # ---------------------------------------------------------- 索引
    @property
    def index(self) -> dict:
        """相對路徑（小寫、正斜線）→ 絕對路徑。"""
        if self._index is None:
            found: dict = {}
            for dirpath, dirs, files in os.walk(self.root):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                for name in files:
                    if name.lower().endswith(".csd"):
                        full = os.path.join(dirpath, name)
                        rel = os.path.relpath(full, self.root).replace("\\", "/")
                        found[rel.lower()] = full
            self._index = found
        return self._index

    def resolve(self, csb: str) -> list:
        """把 Lua 裡寫的 .csb 路徑對到實際的 .csd，回傳所有候選（通常剛好一個）。

        先試「去掉已知前綴後的完整相對路徑」，全部落空才退到尾段比對。
        順序不能反過來：`lobby/MainView.csd` 與 `coinMonopoly/lobby/MainView.csd`
        的尾段一樣，先做尾段比對會兩個都收進來，變成union 掉檢查強度。
        """
        rel = (csb or "").strip().replace("\\", "/").lstrip("./")
        if not rel:
            return []
        if rel.lower().endswith(".csb"):
            rel = rel[:-4] + ".csd"
        elif not rel.lower().endswith(".csd"):
            rel += ".csd"

        low = rel.lower()
        tries = [low]
        for prefix in self.prefixes:
            if low.startswith(prefix):
                tries.append(low[len(prefix):])
        for candidate in tries:
            hit = self.index.get(candidate)
            if hit:
                return [hit]

        tail = tries[-1]
        needle = "/" + tail
        matches = [full for rel_low, full in self.index.items()
                   if rel_low == tail or rel_low.endswith(needle)]
        return sorted(matches)

    def global_names(self) -> set:
        """整個 cocosstudio 底下所有出現過的節點名稱。

        用途只有一個：判斷「這個名字在別的模組的 .csd 裡」。有些 Lua 檔拿到的節點
        是別的畫面建好再傳進來的（提示條掛在天bar、彈窗掛在大廳），那種檔案沒有
        自己的 .csd 可對，名字找不到不代表寫錯。名字**全庫都沒有**才是真的錯。

        全庫解析要十幾秒，所以結果存到 `~/.luadetect_cache`；簽章是每個 .csd 的
        路徑、mtime 與大小的雜湊（見 `_signature`），任何一個動過就重算。
        """
        if self._names is not None:
            return self._names
        signature = self._signature()
        cached = self._load_names_cache(signature)
        if cached is not None:
            self._names = cached
            return cached

        found: set = set()
        missed = 0
        for full in self.index.values():
            text = fileio.read_text(full)
            if text is None:
                missed += 1          # 讀不到就少一批名字，會讓本來寬容放過的變成警告
                continue
            found.update(_NAME_ATTR.findall(text))
        self._names = found
        self.unreadable = missed
        if not missed:
            # 不完整的名稱表不可以寫進快取：下一次跑會拿到一份少了東西的表，
            # 而且簽章看起來還是有效的。
            self._save_names_cache(signature, found)
        return found

    def _signature(self) -> str:
        """快取簽章：每個檔案的路徑、mtime 與大小一起雜湊。

        原本是「檔案數 + mtime 總和」，兩個檔案的 mtime 往相反方向各動一樣的量
        就會碰撞（改一個檔、又還原另一個檔的時間戳就會遇到），拿到過期的名稱表
        會讓本來存在的節點被判成不存在。逐檔雜湊沒有這個問題，成本一樣是一輪 stat。
        """
        digest = hashlib.sha1()
        for rel in sorted(self.index):
            try:
                info = os.stat(self.index[rel])
                stamp = f"{rel}\x00{info.st_mtime_ns}\x00{info.st_size}\n"
            except OSError:
                stamp = f"{rel}\x00missing\n"
            digest.update(stamp.encode("utf-8"))
        return f"{len(self.index)}:{digest.hexdigest()}"

    def _cache_path(self) -> str:
        folder = os.path.join(os.path.expanduser("~"), ".luadetect_cache")
        digest = hashlib.sha1(os.path.normcase(self.root).encode("utf-8")).hexdigest()[:16]
        return os.path.join(folder, f"names_{digest}.json")

    def _load_names_cache(self, signature: str) -> "set | None":
        try:
            with open(self._cache_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if data.get("signature") != signature:
            return None
        return set(data.get("names") or [])

    def _save_names_cache(self, signature: str, found: set) -> None:
        path = self._cache_path()
        # 暫存檔名帶 pid：同一台機器上開好幾個 agent 時，固定檔名會互相截斷
        temp = f"{path}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(temp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"root": self.root, "signature": signature,
                           "names": sorted(found)}, fh, ensure_ascii=False)
            fileio.replace_atomically(temp, path)
        except OSError:
            pass          # 快取寫不進去不該讓檢查失敗
        finally:
            if os.path.exists(temp):
                try:
                    os.remove(temp)       # replace 沒成功時不要留下垃圾
                except OSError:
                    pass

    def by_stem(self, stem: str) -> list:
        """依檔名（不含副檔名）找 .csd。`FooView.lua` 對 `FooView.csd` 這種慣例用的。"""
        needle = (stem or "").strip().lower()
        if not needle:
            return []
        target = needle + ".csd"
        return sorted(full for rel, full in self.index.items()
                      if rel.rsplit("/", 1)[-1] == target)

    @property
    def by_folder(self) -> dict:
        """資料夾（normcase）→ 底下的 .csd（已排序）。

        原本 `in_folder` 每次都掃一遍整個索引，對每一筆做 `dirname` + `normcase`。
        `module` 來源的檔案會問上百次，實測是 18 萬次路徑運算、將近 1 秒。
        建一次表就沒了。
        """
        if self._by_folder is None:
            found: dict = {}
            for full in self.index.values():
                found.setdefault(os.path.normcase(os.path.dirname(full)),
                                 []).append(full)
            for paths in found.values():
                paths.sort()
            self._by_folder = found
        return self._by_folder

    def in_folder(self, folder: str) -> list:
        """某個資料夾底下（不含子資料夾）的所有 .csd。"""
        return list(self.by_folder.get(
            os.path.normcase(os.path.abspath(folder)), ()))

    # ---------------------------------------------------------- 樹
    def tree(self, csd_path: str) -> Node:
        """讀出一份 .csd 的節點樹（結果會快取）。

        循環引用是由 `_build` / `_expand` 的 `visiting` 集合擋掉的，不靠快取佔位。
        這裡刻意**只在建好之後才寫快取**：先塞佔位的話，任何一個非 CsdError 的例外
        都會讓那個空節點永久留在快取裡，之後這份 .csd 的每個節點都會被判成不存在
        ——長駐的 MCP 伺服器會吞掉例外繼續跑，然後安靜地回報一整頁假問題。
        """
        key = os.path.normcase(os.path.abspath(csd_path))
        cached = self._trees.get(key)
        if cached is not None:
            return cached
        self._cycle = False
        built = self._build(csd_path, set())
        if not self._cycle:
            # `_trees` 裡只放**沒被循環引用截斷**的樹，因為 `_expand` 也會拿它來
            # 當巢狀展開的來源。被截斷的那份只對「以它為根」的這一次成立，
            # 存進去會讓別的上層拿到少了東西的子樹。
            self._trees[key] = built
        return built

    def _build(self, csd_path: str, visiting: set) -> Node:
        key = os.path.normcase(os.path.abspath(csd_path))
        obj = _object_data(csd_path)
        if obj is None:
            raise CsdError(f"{csd_path} 裡找不到 ObjectData，可能不是 .csd。")
        node = Node(obj.get("Name") or "Node", _ctype(obj), csd_path)
        node.children = self._children(obj, csd_path, visiting | {key})
        return node

    def _children(self, element: ET.Element, source: str, visiting: set) -> list:
        container = element.find("Children")
        if container is None:
            return []
        out: list = []
        for child in container.findall("AbstractNodeData"):
            item = Node(child.get("Name") or "", _ctype(child), source)
            nested = _nested_path(child)
            inline = self._children(child, source, visiting)
            if nested:
                # ProjectNode 引用了別的 .csd，**而且引用處自己還可以再加子節點**。
                # 執行期兩邊都在：CSLoader 先把被引用檔案的根節點載進來、改名，
                # 然後照常把這一層自己的 Children 也加上去。
                #
                # 只取被引用檔案那一邊會把引用處加的節點整批弄丟——實測
                # `BottomBarGWS.csd` 的 `n_extraBet` 就是這樣（ProjectNode，底下
                # 自己掛著 `n_extraBetHint`），於是 6 筆 error 等級的誤報說那個
                # 節點不存在，而遊戲跑得好好的。
                #
                # `_expand` 回傳的是共用的快取 list，所以這裡要**接成新的**，
                # 不可以 extend 進去，否則會污染別的父節點看到的子樹。
                item.children = self._expand(nested, visiting) + inline
            else:
                item.children = inline
            out.append(item)
        return out

    def _expand(self, nested_rel: str, visiting: set) -> list:
        """展開 ProjectNode：取被引用檔案的**根節點的子節點**，那一層根不保留。

        跟 `tree()` **共用同一份快取**。同一個 .csd 可能既被當成頂層樹讀過、又被
        別的檔案引用；兩邊各自一份快取的話就會解析兩次（實測 1296 個檔案解了
        1680 次）。公用元件被幾十處引用更是常態——`itemcell.csd` 在全庫掃描裡
        被引用 92 次。

        只有**沒被循環引用截斷**的樹才進快取：被截斷的那份是「從這個上層看過去」
        才長那樣，換一個上層就不對了。內層被截斷時連外層一起不存（`_cycle` 往上傳），
        因為外層的內容同樣依賴當時的 visiting。節點建好之後沒有人會改它，
        所以同一份子樹讓多個父節點共用是安全的。
        """
        candidates = self.resolve(nested_rel)
        if len(candidates) != 1:
            return []
        target = candidates[0]
        key = os.path.normcase(os.path.abspath(target))
        if key in visiting:
            self._cycle = True             # 循環引用，停在這裡
            return []
        cached = self._trees.get(key)
        if cached is not None:
            return cached.children

        outer, self._cycle = self._cycle, False
        try:
            built = self._build(target, visiting)
        except CsdError:
            # 讀不了或不是 .csd。不快取這個空結果：可能只是 Cocos Studio 正在存檔
            # 而暫時鎖住，把「空的」記下來會讓這個 ProjectNode 底下的每個節點
            # 在這個行程的餘生都被判成不存在。
            self._cycle = outer or self._cycle
            return []
        clean = not self._cycle
        self._cycle = outer or self._cycle
        if clean:
            self._trees[key] = built
        return built.children


# ---------------------------------------------------------------- 查詢

def walk(node: Node, path: tuple = ()):
    here = path + (node.name,)
    yield node, here
    for child in node.children:
        for item in walk(child, here):
            yield item


def name_index(node: Node) -> dict:
    """`名稱 → [節點]`，整棵樹建一次就存在節點上。

    每個取用點都重走一遍整棵樹的話，一次全庫掃描是 1350 萬次 walk() 呼叫——
    尤其 `module` 來源的候選 .csd 可以到 239 份，每個取用點都要走 239 棵樹。
    樹建好之後不會再變，所以索引可以一直用。
    """
    if node._index is None:
        found: dict = {}
        for item, _ in walk(node):
            found.setdefault(item.name, []).append(item)
        node._index = found
    return node._index


def names(node: Node) -> set:
    return set(name_index(node))


def find_chain(roots: list, chain: list) -> str:
    """檢查一串 `getChildByName` 名稱鏈在樹裡是否成立。

    回傳 "ok"（整串接得起來）、"parent"（末端名稱存在但不在那個父節點下）、
    或 "missing"（末端名稱整棵樹都找不到）。

    chain[0] 不要求是根節點的直接子節點——取用鏈的起點常常是某個先前抓好的節點，
    這裡只驗證「這一串相對關係成立」，不驗證它掛在哪。
    """
    if not chain:
        return "ok"
    starts: list = []
    for root in roots:
        starts.extend(name_index(root).get(chain[0], ()))
    if starts:
        for node in starts:
            if _descend(node, chain[1:]):
                return "ok"

    last = chain[-1]
    if any(last in name_index(root) for root in roots):
        # 起點找不到、或找到了但接不下去；末端名稱存在，是掛錯父節點
        return "parent" if starts else "missing"
    return "missing"


def _descend(node: Node, rest: list) -> bool:
    if not rest:
        return True
    for child in node.children:
        if child.name == rest[0] and _descend(child, rest[1:]):
            return True
    return False
