"""檔案讀寫的韌性——集中在這裡，因為每一條都對應一個實際踩過的失敗。

這個工具跑的時候，同一批檔案常常有別人正在動：Cocos Studio 在存 `.csd`、編輯器
壓著 `.lua`、另一個 agent 在寫基準檔。Windows 上這些都會變成 `PermissionError`，
而「讀不到」被當成「內容是空的」的話，結果是**誤報**——不是漏報：

- 讀不到 `.lua` → 收不到裡面的 `setName`，那些執行期建出來的節點就被冤枉成不存在
- 讀不到 `.csd` → 全庫名稱表少一批，本來寬容放過的名字變成警告

所以這裡的原則是：短暫重試（鎖住幾乎都是瞬時的），真的讀不到就**讓呼叫端知道**，
不要安靜地回一個空值。
"""
from __future__ import annotations

import os
import time


def read_text(path: str, timeout: float = 0.5) -> "str | None":
    """讀成文字。被鎖住時短暫重試；真的讀不到回 None（而不是空字串）。

    `errors="replace"` 是刻意的：既有程式碼裡有少數非 UTF-8 的檔案，整份解碼失敗
    比替換掉幾個字元糟糕得多。
    """
    deadline = time.monotonic() + timeout
    delay = 0.01
    while True:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            if not os.path.isfile(path) or time.monotonic() >= deadline:
                return None
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def replace_atomically(temp: str, target: str, timeout: float = 5.0) -> None:
    """`os.replace(temp, target)`，Windows 上被讀取鎖住時重試。

    Windows 的 `os.replace` 在目的檔正被另一個行程開著讀的時候會 `PermissionError`
    （WinError 5）——Python 的 `open()` 不帶 FILE_SHARE_DELETE。多開 agent 時這是
    實測會發生的：一邊在 `write_baseline`，另一邊的 pre-commit hook 正在讀那份基準檔。
    原子性本身沒問題（讀的人永遠看到完整的舊版或完整的新版），但寫的人會整個炸掉。

    讀一份 20KB 的 JSON 只要幾百微秒，所以重試幾乎一定在第一次就成功。真的撐過
    整個 timeout 才往外拋，而且訊息要講清楚——安靜跳過會讓使用者以為基準更新了，
    其實磁碟上還是舊的那份。
    """
    deadline = time.monotonic() + timeout
    delay = 0.01
    while True:
        try:
            os.replace(temp, target)
            return
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise OSError(
                    f"{target} 一直被別的行程開著（{timeout:.0f} 秒內都換不過去），"
                    "沒有寫入。磁碟上還是原本那一份。"
                    "如果有另一個 agent 或編輯器壓著這個檔案，關掉再試一次。") from exc
            time.sleep(delay)
            delay = min(delay * 1.5, 0.2)


def relative(path: str, root: str) -> str:
    """相對路徑，跨磁碟時退回絕對路徑。

    Windows 上 `os.path.relpath` 對不同磁碟的兩個路徑會拋 ValueError。指紋算不出來
    就等於整個基準檔失效，所以這裡一定要擋——路徑本身當成指紋也還是穩定的。
    """
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")
