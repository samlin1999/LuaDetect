"""MCP stdio 伺服器。

跟 csd MCP 一樣刻意不依賴官方 SDK：這裡只需要 initialize / tools/list / tools/call
三個方法，自己實作就少一個要裝的套件，在沒有對外網路的公司機器上比較不會卡住。

stdout 是協定通道，**只能出現 JSON-RPC 訊息**。任何診斷訊息一律寫 stderr，
不然會把協定弄壞、客戶端只會回報一句看不出原因的解析錯誤。
"""
from __future__ import annotations

import json
import sys
import traceback

from . import tools
from . import __version__

NAME = "luadetect"
# 版本號只有 luadetect/__init__.py 一處，這裡不要再寫一份
VERSION = __version__

KNOWN_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_VERSION = "2025-06-18"

INSTRUCTIONS = (
    "檢查 Lua 三件事：getChildByName 取的節點在對應的 .csd 裡存不存在、"
    "排程與事件註冊了有沒有解除、引用的共用資源檔在不在。"
    "改完模組用 scan 確認。"
    "**scan 回傳的 summaryTable 要原樣呈現給使用者**——那是一份 markdown 統整表，"
    "含規則×嚴重度、涵蓋率、以及每一筆為什麼不是 error。"
    "涵蓋率那一段特別不能省：有些檔案是「根本沒檢查」而不是「檢查過沒問題」，"
    "不講出來就會被讀成「都對」。"
    "看到「節點不存在」先用 explain 確認它對到的是正確的 .csd，對錯了就是解析問題、"
    "不要去動程式碼；還是懷疑就用 verify_parser 對照 csdview。"
    "要知道某個節點該怎麼取用 node_tree 看階層。"
)


def _log(message: str) -> None:
    print(f"[{NAME}] {message}", file=sys.stderr, flush=True)


def _send(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(line + b"\n")
    sys.stdout.buffer.flush()


def _result(request_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id,
           "error": {"code": code, "message": message}})


def _text(payload) -> dict:
    """把回傳值包成 MCP 的文字內容。

    刻意不排版：這段字是給模型讀的，縮排與換行要花 token 而不帶任何資訊。
    寫到磁碟的基準檔照舊保留 indent，那是給人看的、也不佔 token。
    """
    body = (payload if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False,
                            separators=(",", ":"), default=str))
    return {"type": "text", "text": body}


def _handle_call(params: dict) -> dict:
    name = params.get("name") or ""
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        # 回 isError 而不是協定層錯誤：模型看得到內容，才有機會自己改對重試。
        return {"content": [_text(
            f"錯誤：arguments 必須是物件（一組參數名對值），收到 "
            f"{type(args).__name__}。請寫成 {{\"path\": \"…\"}} 這種形式。")],
            "isError": True}
    try:
        payload = tools.call(name, args)
    except tools.USER_ERRORS as exc:
        # 這幾種都是「使用者做得到的修正」，訊息本身就寫成了怎麼解決，直接回給模型
        return {"content": [_text(f"錯誤：{exc}")], "isError": True}
    except Exception as exc:                     # noqa: BLE001 - 不能讓伺服器整個掛掉
        _log("未預期的錯誤:\n" + traceback.format_exc())
        return {"content": [_text(f"未預期的錯誤：{type(exc).__name__}: {exc}")],
                "isError": True}
    return {"content": [_text(payload)], "isError": False}


def handle(message: dict) -> None:
    """處理一則 JSON-RPC 訊息。通知（沒有 id）不回覆。"""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        if request_id is not None:
            _error(request_id, -32602,
                   f"params 必須是物件，收到 {type(params).__name__}。")
        return

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in KNOWN_VERSIONS else DEFAULT_VERSION
        _result(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": INSTRUCTIONS,
        })
        return

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return

    if method == "ping":
        _result(request_id, {})
        return

    if method == "tools/list":
        _result(request_id, {"tools": tools.TOOLS})
        return

    if method == "tools/call":
        _result(request_id, _handle_call(params))
        return

    # 有些客戶端不看 capabilities 就問，回空清單比回錯誤乾淨
    if method == "resources/list":
        _result(request_id, {"resources": []})
        return
    if method == "resources/templates/list":
        _result(request_id, {"resourceTemplates": []})
        return
    if method == "prompts/list":
        _result(request_id, {"prompts": []})
        return
    if method == "logging/setLevel":
        _result(request_id, {})
        return

    if request_id is not None:
        _error(request_id, -32601, f"不支援的方法: {method}")


def main() -> int:
    _log(f"啟動（{VERSION}）")
    stream = sys.stdin.buffer
    while True:
        line = stream.readline()
        if not line:
            break
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text.decode("utf-8"))
        except ValueError as exc:
            _log(f"收到不是 JSON 的一行，略過：{exc}")
            continue
        # JSON-RPC 的訊息一定是物件。收到陣列（批次請求，這裡不支援）或純量時要在這裡
        # 擋掉：往下丟的話 message.get 會 AttributeError，而下面那個 except 自己也呼叫
        # message.get——連 catch-all 一起爆掉，整個迴圈就結束了。
        if not isinstance(message, dict):
            _log(f"收到不是物件的一則訊息（{type(message).__name__}），略過。"
                 "批次請求（JSON 陣列）目前不支援。")
            continue
        try:
            handle(message)
        except Exception:                        # noqa: BLE001
            _log("處理訊息時發生未預期的錯誤:\n" + traceback.format_exc())
            request_id = message.get("id")
            if request_id is not None:
                _error(request_id, -32603, "伺服器內部錯誤，詳情看 stderr。")
    _log("結束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
