---
name: luadetect
description: Use whenever running luaDetect — the static checker for Cocos2d-x Lua projects, Inanna above all (luadetect.bat / run_cli.py / the luadetect MCP tools scan, explain, node_tree, verify_parser, write_baseline) — or when reading, fixing, or adding rules to the tool itself at Mangan_git/luaDetect. Read RULES.md FIRST: it holds what "error" is allowed to mean, why a clean result must never be reported without its coverage table, and the traps that have already produced wrong answers (same-prefix API names, expr_before separators, cache keys, unreadable-vs-empty, stale results). Also use when the user asks to review or consolidate these rules.
---

# luaDetect 使用與開發規則

用途只有一個：**同一個錯不要犯第二次。**

| 檔案 | 內容 |
| --- | --- |
| `RULES.md` | 規則本體。**動手前先讀。** |
| 這一份 | 怎麼跑、怎麼呈現、什麼時候補規則 |

這兩份就住在工具本體的 `.claude/skills/luadetect/` 底下，跟著 repo 走；
`install.py` 只是把「指得回這裡」的一份 `SKILL.md` 部署到使用者層級，
**`RULES.md` 從頭到尾只有這一份**，不會有第二份跟著過期。

## 怎麼跑

```
luadetect.bat <模組或路徑>              # 最常用，預設 scan
python run_cli.py scan --project <專案> --severity info
python run_cli.py explain <某個.lua>    # 單檔診斷：對到哪份 .csd、依據是什麼
python run_cli.py bind <某個.csd> --name X --module Y   # 反推 Properties 綁定
python run_cli.py verify --project <專案>   # 拿 csdview 對照 .csd 解析
python -m pytest -q tests               # 改過工具就要全過
```

（不寫測試項數——寫死的數字每次加測試就過期，而過期的樣子跟壞掉一樣。）

MCP 工具的回傳裡有 `summaryTable`、`findingsTable`、`present`。

## 回報結果時（這一段是使用者反覆要求過的）

1. **一定要貼統整表。** 把 `summaryTable` 原樣貼出來，不要改寫成散文，
   也不要只講「找到 N 筆」。**涵蓋率那一段要一起貼**——它是
   「沒有回報 ≠ 都對」的唯一憑據。
2. **每一筆 `error` 都要開原始碼確認過**才可以回報成「問題」。
3. **不要把上一次的輸出當成這一次的。** 指令沒跑成就說沒跑成。
4. **要寫 `XxxProperties.lua` 的綁定時用 `bind`／`bind_properties`，不要看著
   `node_tree` 手打。** 節點名直接取自 `.csd`、取用鏈照樹算，產出來的東西
   天生通得過 `nodes` 規則（實測 80 份 .csd、2139 個綁定、0 findings）；
   手打會有打字錯誤，而且 `getChildByName` 只找直接子層這件事很容易寫錯。
   產出後把欄位名改成好讀的名字是正常流程。

## 什麼時候往 RULES.md 補一條

觸發條件是「**已經查清楚原因了**」，不是「出錯了」。只在符合任一條時才寫：

- 這個錯**下次還會犯**（是認知落差，不是手滑）
- 錯誤本身**沒有直接說出**該怎麼做（最糟的一種：完全沒有錯誤訊息，
  只是靜靜地一筆都對不到）
- 正確做法**不是從程式碼或說明看得出來**的

不要寫的：一次性的環境問題、單一檔案的個案、已經被測試蓋住的東西
（測試會自己擋，規則寫了只是變長）。

**RULES.md 長到你會想跳過的時候，就是該精簡它了。**
規則要能被每次都讀完才有用。
