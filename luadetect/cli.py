"""命令列介面——給 pre-commit hook、CI 與想直接跑一次的人用。

離開碼刻意跟 csdview 一致：`0` 通過、`1` 有錯誤等級的問題、`2` 是工具自己出錯。
CI 才分得出「檢查失敗」與「檢查跑不起來」——這兩件事的處理方式完全不同。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from . import bind, projects, report, scan, verify
from .csdtree import CsdError
from .projects import ProjectError
from .verify import VerifyError
from .rules import ALL_RULES, GROUPS, resolve_groups

DEFAULT_BASELINE = ".luadetect-baseline.json"


def _use_utf8() -> None:
    """把主控台切到 UTF-8。

    Windows 的主控台預設是 cp950，報告裡的中文會整片變成問號。兩件事都要做：
    SetConsoleOutputCP 決定主控台怎麼「解讀」位元組，reconfigure 決定 Python
    怎麼「產生」它們，只做一半照樣是亂碼。
    """
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:                          # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _dump(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _workspace(args):
    return projects.resolve(getattr(args, "project", None),
                            getattr(args, "path", None))


# ---------------------------------------------------------------- 子指令

def cmd_scan(args) -> int:
    workspace = _workspace(args)
    groups = resolve_groups(args.rules)

    if args.changed or args.staged:
        files = scan.changed_files(workspace, args.base, staged=args.staged)
        if not files:
            message = "沒有異動到的 .lua，不用檢查。"
            _dump({"findings": [], "note": message}) if args.json else print(message)
            return 0
    else:
        files = projects.lua_files(workspace, args.path) if args.path else None

    # 建基準時一律收到 info：基準要是被 --severity 篩過的，之後在較低等級跑
    # 會把所有 warn/info 當成新問題，而且跟 MCP 的 write_baseline 產生的內容不一樣。
    severity = report.INFO if args.write_baseline else args.severity
    if args.staged:
        # 檢查的必須是這次要提交的內容，不是工作目錄那份
        with scan.index_reader(workspace.directory) as read:
            result = scan.scan(workspace, files, groups, severity, args.explain, read)
    else:
        result = scan.scan(workspace, files, groups, severity, args.explain)
    findings, stats = result["findings"], result["stats"]

    baseline_path = args.baseline or os.path.join(workspace.directory, DEFAULT_BASELINE)
    if args.write_baseline:
        info = report.save_baseline(baseline_path, findings, workspace.lua_root)
        print(f"基準檔已寫入 {info['baseline']}（{info['count']} 筆，含所有等級）。"
              "之後只有新出現的問題會讓檢查失敗。")
        return 0

    known = None
    if not args.no_baseline and os.path.isfile(baseline_path):
        findings, known = report.split_baseline(
            findings, report.load_baseline(baseline_path), workspace.lua_root)

    if args.json:
        _dump(report.to_json(findings, workspace.lua_root, stats, known))
    else:
        print(report.to_text(findings, workspace.lua_root, stats, known))
    return report.exit_code(findings)


def cmd_explain(args) -> int:
    workspace = _workspace(args)
    payload = scan.explain_file(workspace, args.path)
    if args.json:
        _dump(payload)
        return 0
    print(f"檔案      {payload['file']}")
    print(f"對到的 csb {'、'.join(payload['csb']) or '（無）'}")
    print(f"對到的 csd {'、'.join(payload['csd']) or '（無）'}")
    print(f"依據      {payload['origin'] or '找不到'}（把握 {payload['confidence']}）")
    if payload.get("note"):
        print(f"說明      {payload['note']}")
    print(f"排程      Safe {payload['scheduleSafe']} 次、原生 {payload['scheduleRaw']} 次")
    if not payload["findings"]:
        print("\n沒有發現問題。")
        return 0
    print("")
    for item in payload["findings"]:
        print("  L%-5d %-10s %-22s %s" % (item["line"], item["severity"],
                                          item["rule"], item["message"]))
    return 1 if any(f["severity"] == report.ERROR for f in payload["findings"]) else 0


def cmd_bind(args) -> int:
    """從 .csd 產生 Properties（UIManager）綁定。

    檢查是「Lua 寫的名字，csd 裡有沒有」；這裡是同一條證據鏈反過來走。
    產出來的東西天生就通得過 `nodes` 那組規則——實測抽 80 份 .csd、
    2139 個綁定，用規則驗全部 0 findings。
    """
    workspace = _workspace(args)
    project = workspace.csd
    if project is None:
        raise ProjectError("這個專案沒有設定 cocosstudio 根目錄。")
    try:
        result = bind.build(project, args.csd, args.name or "",
                            args.module or "", args.include_default)
    except CsdError as exc:
        raise ProjectError(str(exc)) from exc

    if args.json:
        _dump({k: v for k, v in result.items()})
        return 0

    if args.out:
        # 不覆蓋既有檔案：這是產生器，不是遷移工具。
        # 用 `"x"`（獨佔建立）而不是「先 exists 再 open("w")」——後者中間有空隙，
        # 兩個 agent 同時跑就會兩邊都通過檢查、然後互相蓋掉，產出半截的 Lua。
        # `"x"` 的存在性判斷與建立是同一個系統呼叫，沒有那個空隙。
        try:
            with open(args.out, "x", encoding="utf-8", newline="\n") as fh:
                fh.write(result["lua"])
        except FileExistsError as exc:
            raise ProjectError(
                f"{args.out} 已經存在，不覆蓋。改個檔名或先移開。") from exc
        print(f"寫入 {args.out}（{result['bindings']} 個綁定）")
    else:
        print(result["lua"])

    notes = []
    if result["skipped"]:
        notes.append(f"跳過 {len(result['skipped'])} 個沒改過名字的節點"
                     "（Panel_1 這種，沒人會拿名字取它）；要的話加 --include-default")
    for name, was, now in result["renamed"]:
        notes.append(f"節點 '{name}' 撞名，欄位改成 {now}（原本會是 {was}）——"
                     "建議手動改成看得懂的名字")
    for note in notes:
        print("  提醒：" + note, file=sys.stderr)
    return 0


def cmd_tree(args) -> int:
    workspace = _workspace(args)
    project = workspace.csd
    if project is None:
        raise ProjectError("這個專案沒有設定 cocosstudio 根目錄。")
    matches = ([os.path.abspath(args.csd)] if os.path.isfile(args.csd)
               else project.resolve(args.csd) or project.by_stem(
                   os.path.splitext(os.path.basename(args.csd))[0]))
    if not matches:
        raise ProjectError(f"找不到 {args.csd}。")
    if len(matches) > 1:
        listed = "\n".join("  " + m for m in matches[:10])
        raise ProjectError(f"'{args.csd}' 對到 {len(matches)} 個檔案，請給完整路徑：\n{listed}")
    tree = project.tree(matches[0])
    if args.json:
        _dump({"file": matches[0], "tree": tree.to_dict(args.depth)})
        return 0

    def show(node, indent=0, depth=args.depth):
        print("  " * indent + f"{node.name}  [{node.type}]")
        if depth <= 0:
            return
        for child in node.children:
            show(child, indent + 1, depth - 1)
    show(tree)
    return 0


def cmd_verify(args) -> int:
    """拿 csdview 當對照，檢查這裡的 .csd 解析有沒有語意缺口。

    不放進 scan 是因為 csdview 一份要 0.5 秒、全庫要 11 分鐘。但獨立實作會漂移
    ——ProjectNode 的 inline children 就漏過一次，造成 9 筆 error 等級的誤報。
    這個指令把「人工讀 XML 抓出來」變成可以重複跑的檢查。
    """
    workspace = _workspace(args)
    project = workspace.csd
    if project is None:
        raise ProjectError("這個專案沒有設定 cocosstudio 根目錄，沒有 .csd 可比對。")

    if args.csd:
        paths = ([os.path.abspath(args.csd)] if os.path.isfile(args.csd)
                 else project.resolve(args.csd)
                 or project.by_stem(os.path.splitext(os.path.basename(args.csd))[0]))
        if not paths:
            raise ProjectError(f"找不到 {args.csd}。")
    else:
        import random
        every = sorted(project.index.values())
        # 固定 seed：同一份專案每次抽到同一批，比對結果才可重現
        random.Random(args.seed).shuffle(every)
        paths = every[:args.sample]

    result = verify.compare(project, paths)
    if args.json:
        _dump(result)
        return 1 if result["gaps"] else 0

    print(f"比對 {result['checked']} 份 .csd"
          + (f"（{result['failed']} 份讀不了）" if result["failed"] else ""))
    for message in result["errors"][:5]:
        print(f"  ! {message}")
    if not result["gaps"]:
        print("解析結果與 csdview 一致"
              "（扣掉刻意不保留的那層被引用檔案根節點）。")
        return 0
    print(f"\n有 {len(result['gaps'])} 份對不上"
          "——這是解析器的缺口，不是專案的問題：")
    for gap in result["gaps"]:
        print(f"  {gap['file']}")
        if gap["missing"]:
            print(f"     csdview 有、我們沒有: {', '.join(gap['missing'][:8])}")
        if gap["extra"]:
            print(f"     我們有、csdview 沒有: {', '.join(gap['extra'][:8])}")
    return 1


def cmd_rules(args) -> int:
    if args.json:
        _dump({"groups": {k: {"title": v["title"], "doc": v["doc"]}
                          for k, v in GROUPS.items()},
               "rules": ALL_RULES})
        return 0
    for key, group in GROUPS.items():
        print(f"\n{key}  {group['title']}——{group['doc']}")
        for rule, info in sorted(ALL_RULES.items()):
            if info["group"] == key:
                print(f"    {rule:<22} {info['doc']}")
    return 0


def cmd_projects(args) -> int:
    entries = [w.to_dict() for w in projects.known()]
    if args.json:
        _dump({"projects": entries})
        return 0
    if not entries:
        print("沒有認得的專案。在專案根目錄執行，或用 csdview setup 登錄一個。")
        return 0
    for entry in entries:
        print(f"\n{entry['name']}  （來源：{entry['source']}）")
        print(f"  Lua      {entry['luaRoot']}")
        print(f"  cocosstudio {entry['csdRoot'] or '（未設定，節點檢查會跳過）'}")
        if not entry["csdAvailable"] and entry["csdRoot"]:
            print("  ⚠ 這個 cocosstudio 目錄現在不存在")
    return 0


# ---------------------------------------------------------------- 進入點

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="luadetect",
        description="Cocos2d-x Lua 專案的靜態檢查：節點契約與生命週期對稱性。")
    subs = parser.add_subparsers(dest="command")

    def common(sub):
        sub.add_argument("--project", help="專案名稱或資料夾（省略時自動判斷）")
        sub.add_argument("--json", action="store_true", help="輸出 JSON")
        return sub

    scan_cmd = common(subs.add_parser("scan", help="掃描（預設指令）"))
    scan_cmd.add_argument("path", nargs="?", help="檔案、資料夾或模組名稱；省略則全掃")
    scan_cmd.add_argument("--rules", help=f"要跑的規則或群組，逗號分隔（{'、'.join(GROUPS)}）")
    scan_cmd.add_argument("--severity", default=report.INFO,
                          choices=[report.ERROR, report.WARN, report.INFO],
                          help="只回報這個等級以上的問題")
    scan_cmd.add_argument("--changed", action="store_true",
                          help="只檢查 git 上有異動的檔案（工作目錄 + 已暫存）")
    # --staged 與 --base 是兩種不同的「異動」定義，同時給只會有一個生效，
    # 讓 argparse 當場擋掉比安靜忽略一個好。
    picked = scan_cmd.add_mutually_exclusive_group()
    picked.add_argument("--staged", action="store_true",
                        help="只檢查這次要提交的檔案（pre-commit hook 用）")
    picked.add_argument("--base", help="與哪個分支比對異動（配合 --changed）")
    scan_cmd.add_argument("--baseline", help=f"基準檔路徑（預設 {DEFAULT_BASELINE}）")
    scan_cmd.add_argument("--write-baseline", action="store_true",
                          help="把這次的結果寫成基準，之後只報新問題")
    scan_cmd.add_argument("--no-baseline", action="store_true", help="忽略基準檔")
    scan_cmd.add_argument("--explain", action="store_true",
                          help="連「查不出對應 .csb 而跳過」的檔案也列出來")
    scan_cmd.set_defaults(func=cmd_scan)

    explain_cmd = common(subs.add_parser(
        "explain", help="單一檔案的診斷：對到哪個 .csd、為什麼"))
    explain_cmd.add_argument("path", help="要診斷的 .lua")
    explain_cmd.set_defaults(func=cmd_explain)

    tree_cmd = common(subs.add_parser("tree", help="印出 .csd 的節點樹（含巢狀展開）"))
    tree_cmd.add_argument("csd", help=".csd 路徑、相對路徑或檔名")
    tree_cmd.add_argument("--depth", type=int, default=99, help="只印到第幾層")
    tree_cmd.set_defaults(func=cmd_tree)

    verify_cmd = common(subs.add_parser(
        "verify", help="拿 csdview 當對照，檢查 .csd 解析有沒有語意缺口"))
    verify_cmd.add_argument("csd", nargs="?",
                            help=".csd 路徑或檔名；省略則隨機抽樣")
    verify_cmd.add_argument("--sample", type=int, default=40,
                            help="省略 csd 時抽幾份（預設 40，一份約 0.5 秒）")
    verify_cmd.add_argument("--seed", type=int, default=0,
                            help="抽樣的隨機種子（固定才可重現）")
    verify_cmd.set_defaults(func=cmd_verify)

    bind_cmd = common(subs.add_parser(
        "bind", help="從 .csd 產生 Properties（UIManager）綁定程式碼"))
    bind_cmd.add_argument("csd", help=".csd 路徑或檔名")
    bind_cmd.add_argument("--name", help="類別名（省略時用 .csd 的檔名）")
    bind_cmd.add_argument("--module", default="", help="模組名，放進 IClass 與 ---@class")
    bind_cmd.add_argument("--include-default", action="store_true",
                          help="連 Panel_1 這種沒改過名字的節點也綁")
    bind_cmd.add_argument("--out", help="寫進這個檔案（已存在就不覆蓋）")
    bind_cmd.set_defaults(func=cmd_bind)

    rules_cmd = common(subs.add_parser("rules", help="列出所有規則"))
    rules_cmd.set_defaults(func=cmd_rules)

    projects_cmd = common(subs.add_parser("projects", help="列出認得的專案"))
    projects_cmd.set_defaults(func=cmd_projects)
    return parser


def main(argv: "list | None" = None) -> int:
    _use_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    # 沒給子指令時當成 scan：`luadetect Modules/Lobby` 直接就能跑
    if argv and argv[0] not in ("scan", "explain", "tree", "rules", "projects",
                                "verify", "bind", "-h", "--help"):
        argv.insert(0, "scan")
    args = build_parser().parse_args(argv or ["scan"])
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 0
    try:
        return args.func(args)
    except (ProjectError, CsdError, VerifyError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 2
    except Exception:                              # noqa: BLE001
        # 沒接住的話 Python 自己會用離開碼 1 結束，而 1 的意思是「找到錯誤等級的
        # 問題」——CI 會把工具當機讀成檢查失敗，兩件事的處理方式完全不同。
        # traceback 照印（要能查），但離開碼一定要是 2。
        traceback.print_exc()
        print("\n錯誤：luadetect 自己出錯了（上面是 traceback），這不是檢查結果。",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
