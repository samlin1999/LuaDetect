"""安裝與設定：註冊 MCP、裝 git hook、建基準檔，也負責移除。

沒給參數又是在真的主控台裡跑（雙擊 `安裝.bat`）就進選單；給了參數就照參數做，
CI 與腳本用得上。兩條路走的是同一組函式，不會有「手動裝跟自動裝結果不一樣」的問題。

為什麼設定流程寫在 Python 而不是 .bat：cmd.exe 是一邊執行一邊按**位元組偏移**
重讀批次檔的，檔案裡有中文、又在中途 `chcp` 切編碼的話，解析位置會整個錯位，
接下來的 echo 文字就被當成指令去執行。所以 .bat 只留純 ASCII 的幾行，訊息一律由這裡印。

python.exe 與 run.py 的路徑由這支程式自己找，不要寫死、也不要叫使用者填。

用法：
    安裝.bat                              雙擊，進選單
    python install.py --client both       只註冊 MCP，不問
    python install.py --skill             部署 Claude Code skill
    python install.py --hook <專案資料夾>  裝 pre-commit hook
    python install.py --remove-hook <專案資料夾>  只移除這個專案的 pre-commit hook
    python install.py --baseline <專案資料夾>
    python install.py --status            看目前狀態
    python install.py --uninstall         全部移除
    python install.py --show-config       只印設定，不寫檔
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SERVER_KEY = "luadetect"
LAUNCHER = os.path.join(HERE, "run.py")
CLI = os.path.join(HERE, "run_cli.py")
SKILL_DIR = os.path.join(HERE, ".claude", "skills", SERVER_KEY)
# 基準檔名跟 CLI 用同一個常數，不要各寫一份。
# 但這支程式的工作之一就是診斷「裝壞了」，所以套件 import 不起來時要還能跑
# （--show-config、--status 都不需要套件），不能讓它在這裡就整個掛掉。
try:
    from luadetect.cli import DEFAULT_BASELINE as BASELINE_NAME   # noqa: E402
except Exception:                                  # noqa: BLE001
    BASELINE_NAME = ".luadetect-baseline.json"


# ---------------------------------------------------------------- 基礎

def use_utf8_console() -> None:
    """把主控台與自己的輸出都切到 UTF-8。

    切編碼一定要在 Python 行程裡做，不能寫在 .bat 裡：cmd.exe 是一邊執行一邊按
    位元組偏移重讀批次檔的，中途 `chcp` 會讓它的解析位置錯位。

    三件事都要做：SetConsoleOutputCP 決定主控台怎麼「解讀」收到的位元組，
    SetConsoleCP 決定它怎麼處理**輸入**（選單要讀 input()），
    reconfigure 決定 Python 怎麼產生與讀取那些位元組。只做一半就是亂碼。
    """
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:                          # noqa: BLE001
            pass
    for stream, mode in ((sys.stdout, "replace"), (sys.stderr, "replace"),
                         (sys.stdin, "replace")):
        try:
            stream.reconfigure(encoding="utf-8", errors=mode)
        except (AttributeError, OSError, ValueError):
            pass


def say(text: str = "") -> None:
    print(text)


def run(command: list, **kwargs):
    return subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kwargs)


def find_python() -> str:
    """找出要用來跑 MCP 的 python.exe。

    優先用「正在執行這支安裝程式」的解譯器——使用者打 `python install.py`
    就是這個。再從 PATH 找 `python` / `python3`，Windows 另外問 `py -3`。
    """
    candidates: list = []

    def add(path) -> None:
        if not path:
            return
        full = os.path.abspath(path)
        if not os.path.isfile(full):
            return
        if any(os.path.normcase(item) == os.path.normcase(full) for item in candidates):
            return
        candidates.append(full)

    add(sys.executable)
    for name in ("python", "python3"):
        add(shutil.which(name))
    if os.name == "nt":
        launcher = shutil.which("py") or shutil.which("py.exe")
        if launcher:
            probe = run([launcher, "-3", "-c", "import sys; print(sys.executable)"])
            if probe.returncode == 0:
                add(probe.stdout.strip())

    if not candidates:
        raise SystemExit(
            "找不到 Python。請安裝 Python 3.9+，安裝時勾選 Add Python to PATH，"
            "或用「py -3 install.py」再跑一次。")
    return candidates[0]


def find_claude() -> "str | None":
    """找 claude 指令。

    Windows 上它是 claude.cmd，而 subprocess 不透過 shell 時不會自己補副檔名，
    所以要把找到的完整路徑拿來用。
    """
    for name in ("claude.cmd", "claude.exe", "claude"):
        found = shutil.which(name)
        if found:
            return found
    return None


def cursor_mcp_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".cursor", "mcp.json")


def cursor_server_entry(python: str) -> dict:
    """Cursor 的 stdio MCP 設定。

    command / args 一律用絕對路徑：Cursor 啟動子行程時的工作目錄不一定在這裡。
    -u 與 PYTHONUNBUFFERED 避免 stdout 被緩衝，協定通道卡住時客戶端只會顯示連線中。
    """
    return {"type": "stdio", "command": python, "args": ["-u", LAUNCHER],
            "env": {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}}


def _load_cursor_config() -> tuple:
    path = cursor_mcp_path()
    if not os.path.isfile(path):
        return {"mcpServers": {}}, None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, f"讀不了 {path}：{exc}"
    if not raw.strip():
        return {"mcpServers": {}}, None
    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        return None, f"無法解析現有的 {path}：{exc}"
    if not isinstance(loaded, dict):
        return None, f"{path} 根節點不是物件，沒有改寫以免弄壞現有設定。"
    return loaded, None


def write_atomically(path: str, text: str, timeout: float = 5.0) -> str:
    """寫成暫存檔再換過去，換不過去時重試。

    暫存檔名帶 pid：兩個安裝流程同時跑不會互相截斷。
    重試是因為 Windows 的 `os.replace` 在目的檔正被別的行程開著讀的時候會
    `PermissionError`——多開 agent 時實測會發生，而且失敗的是寫的那一方。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    deadline = time.monotonic() + timeout
    delay = 0.01
    while True:
        try:
            os.replace(tmp, path)
            return path
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise OSError(f"{path} 一直被別的行程開著（{timeout:.0f} 秒內都換不過去），"
                              "沒有寫入。磁碟上還是原本那一份。") from exc
            time.sleep(delay)
            delay = min(delay * 1.5, 0.2)


def _save_cursor_config(config: dict) -> str:
    return write_atomically(
        cursor_mcp_path(),
        json.dumps(config, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 註冊

def register_claude(python: str) -> tuple:
    """重新註冊到 Claude Code。

    先 remove 再 add 是為了讓路徑變更能生效（`mcp add` 對已存在的名稱會拒絕）。
    但這中間有個窗口：add 失敗的話，原本好好的註冊已經被 remove 掉了。
    所以失敗時要把「原本那份也沒了」講出來，不然使用者以為維持原狀。
    """
    claude = find_claude()
    if not claude:
        return False, "找不到 claude 指令。要給 Claude Code 用請先安裝它。"
    removed = run([claude, "mcp", "remove", "--scope", "user", SERVER_KEY])
    had_one = removed.returncode == 0
    added = run([claude, "mcp", "add", "--scope", "user", SERVER_KEY,
                 "--", python, LAUNCHER])
    if added.returncode != 0:
        detail = (added.stderr or added.stdout or "").strip()[:500]
        if had_one:
            detail += ("\n          注意：原本的註冊已經先被移除了，現在是未註冊狀態。"
                       "排除問題後再跑一次，或用 --show-config 手動設定。")
        return False, detail
    return True, "已註冊（使用者層級，所有專案都用得到）"


def unregister_claude() -> tuple:
    claude = find_claude()
    if not claude:
        return False, "找不到 claude 指令。"
    done = run([claude, "mcp", "remove", "--scope", "user", SERVER_KEY])
    if done.returncode != 0:
        return False, (done.stderr or done.stdout or "").strip()[:300] or "移除失敗"
    return True, "已移除"


def _update_cursor_config(mutate, expected, attempts: int = 4) -> tuple:
    """讀-改-寫 ~/.cursor/mcp.json，寫完回讀確認，發現被蓋掉就重做。

    `mutate(servers)` 就地改那份 dict，回傳 None 表示不必寫；
    `expected(servers)` 檢查寫入的結果是不是我們要的。

    **這只能減輕、不能根治。** 這個檔案是共用的（csdview 之類的工具也讀-改-寫它），
    而 lost update 的成因在對方：對方讀完、我們寫入、對方才把手上那份舊的寫回去，
    我們加的那一項就消失了——實測確實會發生，而且沒有任何錯誤訊息。
    回讀確認抓得到「覆蓋已經落地」的情況（然後重做一次，兩邊的項目都留住），
    但攔不住之後才落地的那一次；除非兩邊約好同一套鎖，在 luadetect 這一側
    沒有辦法阻止別的程式寫回舊內容。

    真的撞到的話 `--status` 會顯示 Cursor 未註冊，重跑一次就好。刻意不用鎖檔：
    安裝流程崩在中間會留下卡住的鎖，那個代價比這個問題本身大。
    """
    for remaining in range(attempts - 1, -1, -1):
        config, error = _load_cursor_config()
        if error:
            return False, error
        servers = config.get("mcpServers")
        if servers is None:
            servers = {}
            config["mcpServers"] = servers
        if not isinstance(servers, dict):
            return False, f"{cursor_mcp_path()} 的 mcpServers 不是物件，沒有改寫。"
        if mutate(servers) is None:
            return True, "本來就沒有註冊"
        try:
            path = _save_cursor_config(config)
        except OSError as exc:
            return False, f"寫不進 {cursor_mcp_path()}：{exc}"

        written, error = _load_cursor_config()
        if error:
            return False, error
        if expected((written or {}).get("mcpServers") or {}):
            return True, path
        if not remaining:
            return False, (f"{cursor_mcp_path()} 一直被別的行程改寫，"
                           f"{attempts} 次都沒寫成。等一下再試，"
                           "或用 --show-config 手動貼進去。")
    return False, "沒有寫成"      # 不會走到，保險


def register_cursor(python: str) -> tuple:
    """寫進 ~/.cursor/mcp.json，既有的其他伺服器原樣保留。"""
    entry = cursor_server_entry(python)

    def mutate(servers):
        servers[SERVER_KEY] = entry
        return True

    return _update_cursor_config(mutate, lambda servers: servers.get(SERVER_KEY) == entry)


def unregister_cursor() -> tuple:
    def mutate(servers):
        if SERVER_KEY not in servers:
            return None                # 沒這一項，不必寫檔
        servers.pop(SERVER_KEY)
        return True

    return _update_cursor_config(mutate, lambda servers: SERVER_KEY not in servers)


# ---------------------------------------------------------------- skill

SKILL_SOURCE = os.path.join(SKILL_DIR, "SKILL.md")
SKILL_RULES = os.path.join(SKILL_DIR, "RULES.md")
SKILL_MARK = "<!-- luadetect: 由 install.py 部署，不要直接改這一份 -->"


def user_skill_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "skills",
                        SERVER_KEY, "SKILL.md")


def _split_front_matter(text: str) -> tuple:
    """切成 (frontmatter, 其餘)。沒有合法的 frontmatter 就回 `("", text)`。"""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[: index + 1]), "".join(lines[index + 1:])
    return "", text


def render_skill(source: str) -> str:
    """把 repo 裡的 SKILL.md 變成要部署出去的那一份。

    插在 frontmatter 之後——frontmatter 必須留在檔案最前面，Claude Code 才認得，
    插在最上面等於整份 skill 失效，而且不會有錯誤訊息，只是安靜地不被載入。
    """
    note = (f"{SKILL_MARK}\n"
            f"> 這一份是 `install.py` 從工具本體部署過來的。改它沒有用，下次部署會蓋掉；\n"
            f"> 要改就改工具本體裡的那一份，再重跑一次 `python install.py --skill`。\n"
            f">\n"
            f"> 工具本體：`{HERE}`\n"
            f"> **規則本體只有一份**，在 `{SKILL_RULES}`——\n"
            f"> 下面每一處 `RULES.md` 都是指那一份。讀不到就表示工具本體被搬走了，\n"
            f"> 到新位置重跑 `python install.py --skill`，不要憑印象補規則。\n")
    front, body = _split_front_matter(source)
    if not front:
        return f"{note}\n{source}"
    return f"{front}\n{note}{body}"


def skill_state() -> tuple:
    """(狀態, 說明)：`missing` / `foreign` / `stale` / `ok` / `broken`。

    要分得出「沒部署」與「部署過但已經過期」：過期的那一份讀起來完全正常，
    只是指回一個已經搬走的工具本體，或少了後來新增的段落——
    看起來跟正確的一模一樣，這正是最需要被講出來的一種。
    """
    path = user_skill_path()
    try:
        with open(SKILL_SOURCE, encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        return "broken", f"工具本體裡讀不到 {SKILL_SOURCE}：{exc}"
    if not os.path.isfile(path):
        return "missing", f"未部署（{path}）"
    try:
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
    except OSError as exc:
        return "broken", f"讀不到 {path}：{exc}"
    if SKILL_MARK not in existing:
        return "foreign", f"{path} 是別人的 skill，沒有動它"
    if existing != render_skill(source):
        return "stale", f"已過期（工具本體改過或搬過家）——重跑 --skill 更新"
    return "ok", f"已部署  {path}"


def install_skill() -> tuple:
    """把 skill 部署到使用者層級，讓在別的專案裡工作時也讀得到規則。

    只部署 `SKILL.md`，**不複製 `RULES.md`**：規則本體維持 repo 裡唯一的一份，
    部署出去的這份寫的是它的絕對路徑。複製過去的話，改了 repo 卻忘了重部署，
    使用者層級那份就會安靜地過期——過期的規則跟正確的規則長得一模一樣。
    """
    state, detail = skill_state()
    if state in ("broken", "foreign"):
        return False, detail
    with open(SKILL_SOURCE, encoding="utf-8") as fh:
        source = fh.read()
    if not os.path.isfile(SKILL_RULES):
        return False, f"{SKILL_RULES} 不在，工具本體不完整，沒有部署。"
    try:
        path = write_atomically(user_skill_path(), render_skill(source))
    except OSError as exc:
        return False, str(exc)
    return True, path


def remove_skill() -> tuple:
    state, detail = skill_state()
    if state == "missing":
        return True, "本來就沒部署"
    if state in ("broken", "foreign"):
        return False, detail
    path = user_skill_path()
    try:
        os.remove(path)
    except OSError as exc:
        return False, f"刪不掉 {path}：{exc}"
    folder = os.path.dirname(path)
    try:
        os.rmdir(folder)               # 空的才刪得掉，裡面有別的東西就留著
    except OSError:
        pass
    return True, "已移除"


# ---------------------------------------------------------------- git hook

HOOK_MARK = "luadetect"
# 改這段文字之前先看 hook_state()：「這個檔案整份都是我們產生的、可以整個刪掉」
# 是拿檔案跟這份樣板逐字比對判斷的。改了之後，舊版裝出去的 hook 就認不出來，
# 只會被當成「混了別的內容」而不敢刪——沒有錯誤訊息，使用者只會發現怎麼移除不掉。
# 要改就把舊的那份原樣放進 _OLD_HOOK_TEMPLATES。
HOOK_TEMPLATE = """#!/bin/sh
# luadetect pre-commit：只檢查這次要提交（已 git add）的 .lua。
# 由 luaDetect/install.py 產生；要停用就刪掉這個檔案，或跑 install.py --uninstall。
"{python}" "{cli}" scan --staged --severity error
status=$?
if [ $status -eq 1 ]; then
    echo ""
    echo "luadetect 找到錯誤等級的問題（上面那些）。"
    echo "確認過是誤報的話，用 --no-verify 略過這次檢查。"
elif [ $status -ne 0 ]; then
    echo ""
    echo "luadetect 自己出錯了（離開碼 $status），這不是檢查結果。"
    echo "先擋下這次 commit 以免漏檢；要照樣提交就用 --no-verify。"
fi
exit $status
"""
_OLD_HOOK_TEMPLATES: tuple = ()


def hooks_dir(directory: str) -> "str | None":
    """這個工作目錄**實際生效**的 hooks 資料夾，問 git 而不是自己接路徑。

    自己接 `.git/hooks` 在兩種很常見的情況下是錯的：

    - **linked worktree**（多開 agent 時每個 agent 一個 worktree）的 `.git` 是
      **檔案**不是目錄，`os.path.isdir` 會失敗，於是回報「這不像一個 git 倉庫」
      ——它是，而且它的 commit 確實會跑共用的那份 hook。
    - **設過 `core.hooksPath`** 的專案（husky 之類）根本不看 `.git/hooks`，
      寫進去等於沒裝，而安裝流程卻回報成功。

    `git rev-parse --git-path hooks` 兩種都處理好：worktree 會給共用目錄的絕對路徑，
    設過 core.hooksPath 就給那個路徑。
    """
    if not os.path.isdir(directory):
        return None
    try:
        done = run(["git", "rev-parse", "--git-path", "hooks"], cwd=directory)
    except OSError:
        return None            # git 不在 PATH 上，或路徑當不了工作目錄
    if done.returncode != 0:
        return None
    raw = (done.stdout or "").strip()
    if not raw:
        return None
    return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(directory, raw))


def hook_path(directory: str) -> "str | None":
    folder = hooks_dir(directory)
    return os.path.join(folder, "pre-commit") if folder else None


def _is_generated_hook(text: str) -> bool:
    """整份是不是 HOOK_TEMPLATE 產生的。兩個路徑欄位容許任何值：
    換過 Python、工具搬過家之後裝的那一份也要認得。"""
    text = text.replace("\r\n", "\n")
    for template in (HOOK_TEMPLATE, *_OLD_HOOK_TEMPLATES):
        pieces = re.split(r"\{(?:python|cli)\}", template)
        if re.fullmatch(r'[^"\n]*'.join(re.escape(piece) for piece in pieces), text):
            return True
    return False


def hook_state(directory: str) -> tuple:
    """(狀態, 說明)：`nodir` / `nogit` / `unreadable` / `missing` / `ours` /
    `mixed` / `foreign`。

    要分出 `ours` 與 `mixed`，因為只有前者能整個刪掉：`mixed` 是檔案裡有呼叫
    luadetect、但還有別的內容——多半是使用者照 install_hook 的提示，把那一行併進
    自己原本的 hook。整個刪掉會連他原本的 hook 一起刪，而且不會有任何提示。
    以前只看檔案裡有沒有 "luadetect" 這個字，這種檔案會被移除整個刪掉、
    被安裝整個蓋掉。
    """
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        return "nodir", f"{directory} 不存在。"
    path = hook_path(directory)
    if not path:
        return "nogit", f"{directory} 不是 git 工作目錄（git rev-parse 失敗）。"
    if not os.path.isfile(path):
        return "missing", f"未裝（{path} 不存在）"
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        return "unreadable", f"讀不到 {path}：{exc}"
    if _is_generated_hook(text):
        return "ours", f"已裝  {path}"
    if HOOK_MARK in text.lower():
        return "mixed", f"{path} 裡有呼叫 luadetect，但還混了別的內容"
    return "foreign", f"{path} 是別的 pre-commit hook，不是 luadetect 的"


HOOK_LABELS = {
    "nodir": "資料夾不在", "nogit": "不是 git 工作目錄", "unreadable": "hook 讀不到",
    "missing": "未裝", "ours": "已裝",
    "mixed": "已裝（併在別的 hook 裡）", "foreign": "未裝（有別的 pre-commit hook）",
}


def hook_label(directory: str) -> str:
    return HOOK_LABELS[hook_state(directory)[0]]


def hook_installed(directory: str) -> bool:
    """有沒有一份「整個都是我們的」hook——也就是移除時會被刪掉的那種。"""
    return hook_state(directory)[0] == "ours"


def install_hook(directory: str, python: str) -> tuple:
    """裝 pre-commit hook。

    只檢查這次要提交的檔案：全庫掃十幾秒，掛在每次 commit 上會被拔掉。
    """
    directory = os.path.abspath(directory)
    state, detail = hook_state(directory)
    if state in ("nodir", "nogit", "unreadable"):
        return False, detail
    path = hook_path(directory)
    if state in ("mixed", "foreign"):
        return False, (f"{path} 已經有別的 pre-commit hook，沒有覆蓋。"
                       "請自己把下面這行加進去：\n"
                       f'         "{python}" "{CLI}" scan --staged --severity error')
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(HOOK_TEMPLATE.format(python=python.replace("\\", "/"),
                                          cli=CLI.replace("\\", "/")))
    except OSError as exc:
        return False, f"寫不進 {path}：{exc}"
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    return True, path


def remove_hook(directory: str) -> tuple:
    """移除這一個專案的 luadetect pre-commit hook，別的專案不動。

    回傳的 ok 是「之後這個專案 commit 時不會再跑 luadetect」：沒裝、或現有的是
    別人的 hook，都算 ok。併在別人 hook 裡的那種刪不了，算失敗——commit 時它
    還是會跑，要講出來請人手動拿掉。對不到專案或 git 也算失敗：
    「查不出來」不等於「本來就沒裝」。
    """
    state, detail = hook_state(directory)
    if state == "missing":
        return True, "本來就沒裝"
    if state == "foreign":
        return True, f"本來就沒裝；{detail}，沒有動它"
    if state == "mixed":
        return False, (f"{detail}，沒有刪（整個刪掉會連原本的 hook 一起刪）。"
                       "要停用就自己打開它，把呼叫 run_cli.py 的那一行拿掉。")
    if state != "ours":
        return False, detail
    path = hook_path(directory)
    try:
        os.remove(path)
    except OSError as exc:
        return False, f"刪不掉 {path}：{exc}"
    return True, f"已移除  {path}"


# ---------------------------------------------------------------- 基準檔

def write_baseline(directory: str, python: str) -> tuple:
    done = run([python, CLI, "scan", "--write-baseline"], cwd=directory)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout or "").strip()[:400]
    lines = (done.stdout or "").strip().splitlines()
    return True, lines[-1] if lines else "完成"


# ---------------------------------------------------------------- 專案

def known_projects() -> list:
    """認得的專案，沿用 csdview 的登錄檔。"""
    try:
        from luadetect import projects
        return projects.known()
    except Exception:                              # noqa: BLE001
        return []


def pick_project(prompt: str, note=None) -> "str | None":
    """`note(資料夾)` 有給的話接在每個專案後面，例如 hook 裝了沒。"""
    entries = known_projects()
    say()
    if entries:
        say(f"  {prompt}")
        for index, item in enumerate(entries, 1):
            extra = f"   [{note(item.directory)}]" if note else ""
            say(f"    {index}) {item.name}   {item.directory}{extra}")
        say("    0) 取消（或直接貼上一個資料夾路徑）")
    else:
        say(f"  {prompt}（沒有認得的專案，請貼上資料夾路徑）")
    answer = input("  > ").strip().strip('"')
    if not answer or answer == "0":
        return None
    if answer.isdigit() and entries and 1 <= int(answer) <= len(entries):
        return entries[int(answer) - 1].directory
    if os.path.isdir(answer):
        return answer
    # 打錯號碼或路徑時要講，不然看起來跟「選了取消」一模一樣
    say(f"  [x] 沒有這個號碼：{answer}" if answer.isdigit()
        else f"  [x] 找不到這個資料夾：{answer}")
    return None


# ---------------------------------------------------------------- 狀態

def status(python: str) -> None:
    say()
    say("  目前狀態")
    say("  " + "-" * 46)
    say(f"  Python    {python}")
    say(f"  啟動檔    {LAUNCHER}")

    claude = find_claude()
    if not claude:
        say("  Claude Code  找不到 claude 指令")
    else:
        listing = run([claude, "mcp", "list"])
        line = next((l.strip() for l in (listing.stdout or "").splitlines()
                     if l.strip().startswith(SERVER_KEY + ":")), None)
        say(f"  Claude Code  {line if line else '未註冊'}")

    config, error = _load_cursor_config()
    if error:
        say(f"  Cursor       {error}")
    else:
        servers = config.get("mcpServers") or {}
        say(f"  Cursor       {'已註冊' if SERVER_KEY in servers else '未註冊'}"
            f"  （{cursor_mcp_path()}）")

    state, detail = skill_state()
    say(f"  skill        {detail}")

    entries = known_projects()
    if not entries:
        say("  專案         沒有認得的專案（用 csdview setup 登錄，或直接給路徑）")
    for item in entries:
        baseline = os.path.join(item.directory, BASELINE_NAME)
        say(f"  專案 {item.name}")
        say(f"    hook      {hook_label(item.directory)}")
        say(f"    基準檔    {'有' if os.path.isfile(baseline) else '無'}"
            f"  {baseline}")
        say(f"    cocosstudio {item.csd_root or '未設定（節點檢查會跳過）'}")
    say()


# ---------------------------------------------------------------- 動作

def do_register(python: str, client: str) -> bool:
    ok_any = False
    if client in ("both", "claude"):
        say("        Claude Code")
        ok, detail = register_claude(python)
        say(f"          {'' if ok else '[x] '}{detail}")
        ok_any = ok_any or ok
    if client in ("both", "cursor"):
        say("        Cursor")
        ok, detail = register_cursor(python)
        say(f"          {'已寫入 ' + detail if ok else '[x] ' + detail}")
        ok_any = ok_any or ok
    return ok_any


def do_deploy_skill() -> bool:
    say("        skill")
    ok, detail = install_skill()
    say(f"          {'已部署 ' + detail if ok else '[x] ' + detail}")
    return ok


def uninstall_preview() -> list:
    """解除安裝**會動到哪些東西**——先講清楚再問要不要做。

    「全部移除」四個字看不出範圍：使用者會擔心是不是連工具本體、
    連專案裡的基準檔都一起刪掉。把實際存在的項目逐條列出來就沒有這個疑慮。
    """
    items: list = []
    claude = find_claude()
    if claude:
        listing = run([claude, "mcp", "list"])
        if any(line.strip().startswith(SERVER_KEY + ":")
               for line in (listing.stdout or "").splitlines()):
            items.append("Claude Code 的 MCP 註冊（使用者層級）")
    config, error = _load_cursor_config()
    if not error and SERVER_KEY in (config.get("mcpServers") or {}):
        items.append(f"Cursor 的 MCP 註冊（{cursor_mcp_path()} 裡的 {SERVER_KEY} 這一項）")
    if skill_state()[0] in ("ok", "stale"):
        items.append(f"部署出去的 skill（{user_skill_path()}）")
    for item in known_projects():
        if hook_installed(item.directory):
            items.append(f"{item.name} 的 pre-commit hook（{hook_path(item.directory)}）")
    return items


def do_uninstall(python: str) -> None:
    say()
    say("  移除中 ...")
    ok, detail = unregister_claude()
    say(f"    Claude Code  {detail}")
    ok, detail = unregister_cursor()
    say(f"    Cursor       {detail if isinstance(detail, str) else '已移除'}")
    ok, detail = remove_skill()
    say(f"    skill        {detail}")
    for item in known_projects():
        ok, detail = remove_hook(item.directory)
        say(f"    hook {item.name}  {detail}")
    say()
    say("  基準檔（.luadetect-baseline.json）與 .luadetect.json 沒有刪，"
        "那是專案的設定，要不要留由你決定。")
    say()


def self_check(python: str) -> None:
    say("  自我檢查：跑 tests/ 底下的測試，確認解析、規則與 MCP 協定都正常。")
    say("  （只讀這個資料夾裡的東西，不會碰到你的專案。）")
    say()
    say("  執行中 ...")
    check = run([python, "-m", "pytest", "-q", os.path.join(HERE, "tests")],
                cwd=HERE, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if check.returncode == 0:
        say("        通過")
        return
    if "No module named pytest" in (check.stderr or ""):
        fallback = run([python, os.path.join(HERE, "tests", "test_protocol.py")],
                       cwd=HERE, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        say("        通過（沒有 pytest，只跑了協定測試）" if fallback.returncode == 0
            else "  [!] 協定測試沒過。")
        return
    say("  [!] 自我檢查沒過：")
    for line in (check.stdout or "").splitlines()[-6:]:
        say(f"        {line.strip()}")


# ---------------------------------------------------------------- 選單

# 每個選項都寫清楚「選了會發生什麼」。只寫「自我檢查」「全部移除」這種名詞，
# 使用者看不出範圍——尤其是移除，會擔心連工具本體跟專案設定一起刪掉。
MENU = """
  luadetect — 設定
  ================================================

    1) 註冊 MCP 到 Claude Code 與 Cursor，並部署 skill
         裝好之後就能在對話裡直接叫 scan / explain，一般選這個
    2) 只註冊 Claude Code（含 skill）
    3) 只註冊 Cursor
    4) 只部署／更新 skill
         讓在別的專案裡工作時也讀得到 RULES.md；工具本體搬過家就重跑這個

    5) 裝 pre-commit hook
         之後每次 commit 前自動檢查這次異動的 .lua，有錯就擋下來
    6) 移除 pre-commit hook
         只拿掉選定那個專案的 hook；別的專案、MCP 註冊與 skill 都不動
    7) 建立／更新基準檔
         記下專案現在既有的問題，之後只有新寫出來的才算失敗

    8) 看目前狀態
         兩個客戶端註冊了沒、skill 部署了沒、各專案的 hook 與基準檔在不在
    9) 自我檢查
         跑一次測試，確認工具本身沒壞（裝完覺得怪怪的就跑這個）
   10) 解除安裝
         取消 MCP 註冊、收回 skill、移除各專案的 pre-commit hook；不刪工具與專案設定

    0) 離開
"""


def menu(python: str) -> int:
    while True:
        say(MENU)
        try:
            choice = input("  請選擇： ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0

        if choice in ("0", "q", ""):
            return 0

        if choice in ("1", "2", "3"):
            client = {"1": "both", "2": "claude", "3": "cursor"}[choice]
            say()
            say("  註冊中 ...")
            ok = do_register(python, client)
            if client != "cursor":
                do_deploy_skill()
            say()
            if ok:
                say("  完成。Claude Code 要開一個新的對話才會載入這些工具；")
                say("  Cursor 到 Settings → MCP 確認，沒出現就重開 Cursor。")
            else:
                say("  [x] 沒有註冊成功，看上面的訊息。")

        elif choice == "4":
            say()
            do_deploy_skill()

        elif choice == "5":
            directory = pick_project("要裝 hook 到哪個專案？")
            if directory:
                ok, detail = install_hook(directory, python)
                say(f"  {'已寫入 ' + detail if ok else '[!] ' + detail}")
                if ok:
                    say("  之後每次 commit 只會檢查這次異動到的 .lua。")

        elif choice == "6":
            directory = pick_project("要移除哪個專案的 hook？", note=hook_label)
            if directory:
                ok, detail = remove_hook(directory)
                say(f"  {detail}" if ok else f"  [!] {detail}")

        elif choice == "7":
            directory = pick_project("要建基準檔的專案？")
            if directory:
                say("  掃描中（第一次要十幾秒）...")
                ok, detail = write_baseline(directory, python)
                say(f"  {detail}" if ok else f"  [x] {detail}")

        elif choice == "8":
            status(python)

        elif choice == "9":
            say()
            self_check(python)

        elif choice == "10":
            items = uninstall_preview()
            say()
            if not items:
                say("  沒有東西可以移除——MCP 沒註冊、也沒有裝過 hook。")
            else:
                say("  會移除下面這些：")
                for item in items:
                    say(f"    · {item}")
                say()
                say("  不會動到的：luaDetect 這個資料夾本身，還有各專案的")
                say("  .luadetect.json 與 .luadetect-baseline.json（那是專案的設定）。")
                confirm = input("\n  確定移除？(y/N) ").strip().lower()
                if confirm == "y":
                    do_uninstall(python)
                else:
                    say("  取消，什麼都沒動。")

        else:
            say(f"  沒有這個選項：{choice}")

        try:
            input("\n  按 Enter 回選單 ...")
        except (EOFError, KeyboardInterrupt):
            return 0


# ---------------------------------------------------------------- 進入點

def _parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="設定 luadetect：註冊 MCP、裝 git hook、建基準檔。"
                    "不給參數又在主控台裡執行時會進選單。")
    parser.add_argument("--menu", action="store_true", help="強制進選單")
    parser.add_argument("--client", choices=["both", "claude", "cursor", "none"],
                        help="要註冊到哪些客戶端")
    parser.add_argument("--skill", action="store_true",
                        help="部署／更新 Claude Code skill 到使用者層級")
    parser.add_argument("--hook", metavar="專案資料夾", help="裝 pre-commit hook")
    parser.add_argument("--remove-hook", metavar="專案資料夾",
                        help="只移除這個專案的 pre-commit hook，其他都不動")
    parser.add_argument("--baseline", metavar="專案資料夾", help="建立／更新基準檔")
    parser.add_argument("--status", action="store_true", help="只看狀態")
    parser.add_argument("--uninstall", action="store_true", help="移除註冊與 hook")
    parser.add_argument("--show-config", action="store_true",
                        help="只印出填好本機路徑的設定片段，不寫檔")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    use_utf8_console()
    argv = sys.argv[1:] if argv is None else list(argv)
    args = _parse_args(argv)
    python = find_python()

    if args.show_config:
        say(json.dumps({"mcpServers": {SERVER_KEY: cursor_server_entry(python)}},
                       indent=2, ensure_ascii=False))
        say()
        say(f"  Python : {python}")
        say(f"  啟動檔 : {LAUNCHER}")
        say(f"  寫入位置: {cursor_mcp_path()}")
        return 0

    if args.status:
        status(python)
        return 0

    if args.uninstall:
        do_uninstall(python)
        return 0

    # 雙擊 .bat（沒帶參數、而且真的有主控台）就進選單；
    # 被腳本或 CI 呼叫時 stdin 不是 tty，這時走原本的「一次裝好」流程，不會卡在等輸入。
    interactive = args.menu or (not argv and sys.stdin is not None
                                and getattr(sys.stdin, "isatty", lambda: False)())
    if interactive:
        return menu(python)

    # 有給任何一個「做什麼」的參數，就只做那些，不要掉進下面的一鍵安裝。
    # 原本用 did_something 判斷，於是 `--client none` 因為什麼都沒做，
    # 反而掉下去把兩個客戶端都註冊了——跟這個參數的意思正好相反。
    did_something = bool(args.client or args.skill or args.hook or args.remove_hook
                         or args.baseline)
    if args.client and args.client != "none":
        say()
        say("  註冊 MCP 客戶端 ...")
        do_register(python, args.client)
    elif args.client == "none":
        say()
        say("  --client none：不註冊任何客戶端。")
    failed = False
    if args.skill:
        say()
        say("  部署 skill ...")
        failed = not do_deploy_skill() or failed
    if args.hook:
        ok, detail = install_hook(args.hook, python)
        say(f"  hook：{'已寫入 ' + detail if ok else '[!] ' + detail}")
        failed = failed or not ok
    if args.remove_hook:
        ok, detail = remove_hook(args.remove_hook)
        say(f"  hook：{detail if ok else '[!] ' + detail}")
        failed = failed or not ok
    if args.baseline:
        ok, detail = write_baseline(args.baseline, python)
        say(f"  基準檔：{detail if ok else '[!] ' + detail}")
        failed = failed or not ok
    if did_something:
        # 失敗要反映在離開碼上，不然腳本與 CI 看不出來
        return 1 if failed else 0

    # 完全沒給參數、又不是互動：照舊一次裝好
    say()
    say("  luadetect — 安裝 Lua 靜態檢查（節點契約 + 生命週期）")
    say("  " + "=" * 48)
    say()
    say(f"  使用的 Python：{python}")
    say()
    self_check(python)
    say("  註冊 MCP 客戶端 ...")
    registered = do_register(python, "both")
    do_deploy_skill()
    if not registered:
        say()
        say("  [x] Claude Code 與 Cursor 都沒註冊成功。")
        return 1
    say()
    say("  完成。Claude Code 要開新對話才會載入工具；Cursor 到 Settings → MCP 確認。")
    say(f"  要裝 git hook 或建基準檔：python install.py --menu")
    say()
    return 0


if __name__ == "__main__":
    sys.exit(main())
