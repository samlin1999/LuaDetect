# luadetect

Cocos2d-x Lua 專案的靜態檢查。**送出前就找出那些要等玩家開到才會炸的東西。**

長在 Inanna 上，下面的數字也都是拿 Inanna 量的，但**沒有把 Inanna 寫死在程式碼裡**：
Lua 根、資源根、路徑前綴都是照命名慣例找出來的，換一個專案照樣跑（見
「專案設定」）。

三組規則，一個入口。共通點是**證據都一樣**：程式碼裡的字面字串，對上磁碟上的
ground truth：

| 規則群組 | 抓什麼 |
| --- | --- |
| `nodes` | `getChildByName( "字串" )` 取的節點，在對應的 `.csd` 裡到底存不存在 |
| `lifecycle` | 排程、事件、EventDispatcher 監聽註冊了卻沒有解除——回呼在節點釋放後觸發就是野指標 |
| `resources` | Lua 引用的圖檔／plist／spine／音檔，在資源根裡到底存不存在（含 `string.format` 樣板的形狀檢查） |

CLI 給 pre-commit 與 CI 用，MCP 給 Claude Code / Cursor 用，兩邊跑的是同一份程式碼。

---

## 為什麼是這幾件事

專案裡有 **8259 處** `getChildByName`，全部是字串。美術在 Cocos Studio 把節點改名或
搬一層，Lua 這邊拿到的是 `nil`，編譯期沒有任何保護，要到有人開到那個畫面才炸。
排程沒解除也是同一類：靜態就看得出來，卻只會在執行期以 `EXC_BAD_ACCESS` 現身。

全庫 1304 個 Lua 檔，整條 CLI 約 7.5 秒。但每天真正在跑的是增量：
`--changed` 0.5 秒、單一檔案 0.3 秒、一個模組不到 1 秒。

**嚴重度是拿「這份程式碼目前是可運作的」校出來的。** 例如維修頁 `MaintainPage.lua`
取的 `patch_bg_1`、`txt_time`、`update_btn` 在 `MaintainPage.csd` 裡一個都不存在
（那份 csd 有的是 `txt_update`）——節點確實沒了，但那三個欄位整個模組沒有任何一處
讀它們，所以維修頁照樣開，只是少了那幾個元素。這是 `warn`，不是 `error`。
`error` 留給真的會讓玩家看到當機的那一種，見下面的判準。

---

## 安裝與設定

雙擊 `安裝.bat` 進設定選單：

```
    1) 註冊 MCP 到 Claude Code 與 Cursor，並部署 skill
         裝好之後就能在對話裡直接叫 scan / explain，一般選這個
    2) 只註冊 Claude Code（含 skill）
    3) 只註冊 Cursor
    4) 只部署／更新 skill
         讓在別的專案裡工作時也讀得到 RULES.md；工具本體搬過家就重跑這個

    5) 裝 pre-commit hook
         之後每次 commit 前自動檢查這次要提交的 .lua，有錯就擋下來
         （hook 位置問 git 拿，worktree 與 core.hooksPath 都裝得對）
    6) 移除 pre-commit hook
         只拿掉選定那個專案的 hook；別的專案、MCP 註冊與 skill 都不動
    7) 建立／更新基準檔
         記下專案現在既有的問題，之後只有新寫出來的才算失敗

    8) 看目前狀態
         兩個客戶端註冊了沒、skill 部署了沒、各專案的 hook 與基準檔在不在
    9) 自我檢查
         跑一次測試，確認工具本身沒壞
   10) 解除安裝
         取消 MCP 註冊、收回 skill、移除各專案的 pre-commit hook；不刪工具與專案設定
```

選 5、6、7 時會把認得的專案列出來讓你選號碼，也可以直接貼路徑；
選 6 時每個專案後面會標 hook 裝了沒。
選 10 會**先列出實際會被移除的項目**再問你要不要做。

移除 hook（選 6 或 `--remove-hook`）只刪**整份都是 luadetect 產生的**那個檔案。
如果你照安裝時的提示，把 luadetect 那一行併進了自己原本的 pre-commit，
它不會整個刪掉（那會連你原本的 hook 一起刪），而是告訴你去手動拿掉那一行；
別人的 hook（例如 husky 的）完全不碰。

要手動貼設定到別的客戶端，用 `python install.py --show-config`。

給參數就照參數做，不進選單——CI 與腳本用這個：

```bat
python install.py --client both              :: 只註冊 MCP
python install.py --skill                    :: 只部署 skill
python install.py --hook C:\path\to\Inanna   :: 裝 git hook
python install.py --remove-hook C:\path\to\Inanna  :: 只移除這個專案的 hook
python install.py --baseline C:\path\to\Inanna
python install.py --status                   :: 看狀態
python install.py --uninstall                :: 移除註冊、skill 與 hook
python install.py --show-config              :: 只印設定，不寫檔
```

### skill 為什麼不放在 `~/.claude/skills/`

給 agent 看的那份規則（`RULES.md`）住在 **repo 裡**的
`.claude/skills/luadetect/`，跟著工具走。`--skill` 部署到使用者層級的只有一份
指回這裡的 `SKILL.md`，**`RULES.md` 不複製出去**——複製了就會有第二份，
改了 repo 卻忘記重新部署時它會安靜地過期，而過期的規則跟正確的規則長得一模一樣。
`--status` 會把「已部署 / 已過期 / 是別人的 skill」分開講。

專案路徑**沿用 csdview 的登錄檔** `~/.csdview_projects`——那裡面已經把程式碼倉庫
（`directory`）與 cocosstudio 根目錄（`csdRoot`）配好了，不必再設定一次。

---

## 用法

```bash
luadetect                          # 全掃（在專案裡執行）
luadetect Modules/Lobby            # 只掃一個模組；也可以直接寫 Lobby
luadetect --changed                # 只掃 git 上有異動的（工作目錄 + 已暫存）
luadetect --staged                 # 只掃這次要提交的（pre-commit hook 用這個）
luadetect --severity error         # 只看錯誤等級
luadetect explain <某個.lua>       # 這個檔案被判定對到哪份 .csd、為什麼
luadetect tree <某個.csd>          # 節點階層（執行期語意）
luadetect bind <某個.csd>          # 反過來：產生 Properties 綁定程式碼
luadetect verify                  # 拿 csdview 對照，檢查 .csd 解析有沒有缺口
luadetect rules                    # 所有規則的說明
```

沒給子指令時預設就是 `scan`，所以 `luadetect Modules/Lobby` 直接可用。

### 掃完會給一張統整表

逐筆清單之後是三張表。第二張**一定要看**：

```
  規則               錯誤  警告  提醒  合計
  -----------------  ----  ----  ----  ----
  EVENT_LEAK            0    22    78   100
  NODE_MISSING          0    58     0    58
  NODE_WRONG_PARENT     0    19     0    19
  RES_MISSING           0     8     0     8
  SCHED_NO_HANDLE       0     2     0     2
  合計                  0   109    78   187

  涵蓋率——「沒有回報」不等於「都對」：

  項目              檢查了  沒檢查  總數  沒檢查的是什麼
  ----------------  ------  ------  ----  -----------------------------------
  .lua 檔案           1304       0  1304  （全部讀得到）
  有節點取用的檔案     582      32   614  查不出對應的 .csb，不猜
  資源路徑            1296    1245  2541  要接前綴的檔名片段 991、格式樣板 40、對不到資源根 214

  NODE_MISSING 為什麼不是 error（可同時成立）：

  原因                                          筆數
  --------------------------------------------  ----
  這個名字在別的 .csd 裡有（可能對到錯的版面）    39
  存進欄位卻沒有人讀它（不會當機）                29
  取用鏈的起點不確定（節點可能是外面傳進來的）    27
  有 nil 防護（取不到就自己建）                   13

  離開碼 0（通過，沒有錯誤等級的問題）
```

- **第一張**：規則 × 嚴重度。只有「錯誤」那一欄會影響離開碼。
- **第二張**：涵蓋率。**每一列都有分母，而且「檢查了 + 沒檢查 = 總數」**。
  只給「資源路徑判斷 1180」的話，看的人分不出這是「1180 條全驗過」還是
  「1180 條驗過、另外 1361 條根本沒交代」——而這張表存在的唯一理由就是
  回答那個問題。沒檢查的那些是**根本沒檢查**，不是檢查過沒問題。

  一個模組的表長這樣，一眼就看得出有沒有缺口：

  ```
  項目              檢查了  沒檢查  總數  沒檢查的是什麼
  ----------------  ------  ------  ----  --------------
  .lua 檔案             10       0    10  （全部讀得到）
  有節點取用的檔案       4       0     4  （全部對得到 .csd）
  資源路徑               7       0     7  （全部判斷過）
  ```
- **第三張**：`NODE_MISSING` 為什麼被降級。這一張是可以拿去動手的清單：
  「存進欄位卻沒有人讀它」那 29 筆其實可以直接把那幾行刪掉。

`--json` 的輸出也有同一份統整（`summary` 欄），而且算的是**截斷前的全部**——
MCP 預設只列最嚴重的 50 筆，統整仍然是 187 筆的統整。

離開碼跟 csdview 一致：`0` 通過、`1` 有錯誤等級的問題、`2` 是工具自己出錯。
CI 才分得出「檢查失敗」與「檢查跑不起來」。

「跑不起來」包含 git 叫不動、或 `--base` 指到這個 clone 裡不存在的 ref。這兩種
情況**不會**回報「沒有異動、通過」——那會讓 CI 從接進去的第一天起永遠綠燈。

### 反過來用：從 .csd 產生綁定程式碼

```bash
luadetect bind MainView_V --name MainView --module VoucherCenter
luadetect bind MainView_V --name MainView --module VoucherCenter --out MainViewProperties.lua
```

檢查是「Lua 寫的名字，csd 裡有沒有」；`bind` 是**同一條證據鏈反過來走**——
csd 裡有什麼，就把 Properties（UIManager）的綁定寫出來：

```lua
local NodeNames = {
    BG = "bg",
        BTN_INFO = "btn_info",
        LIMIT_PANEL = "limit_panel",
            LIMIT_DAY = "limit_day",
}

---@class inn.VoucherCenter.MainViewProperties
local MainViewProperties = IClass( "VoucherCenterMainViewProperties" )
MainViewProperties.NodeNames = NodeNames

function MainViewProperties:ctor( view )
    self.m_bg          = view:getChildByName( NodeNames.BG )
    self.m_btn_info    = self.m_bg:getChildByName( NodeNames.BTN_INFO )
    self.m_limit_panel = self.m_bg:getChildByName( NodeNames.LIMIT_PANEL )
    self.m_limit_day   = self.m_limit_panel:getChildByName( NodeNames.LIMIT_DAY )
end

return MainViewProperties
```

格式與命名都是**從專案現有的 73 個 `*Properties.lua` 量出來的**，不是自己發明：

| 慣例 | 實測 |
| --- | --- |
| 節點名放常數表，不寫字面字串 | 73/73 個檔案都這樣，`getChildByName( "字串" )` **一處都沒有** |
| 欄位名 = `m_` + 節點名 | 738 處綁定裡 89.7% 完全相同、2.2% 只差大小寫 |
| 常數鍵 = 節點名轉大寫 | 1946 個鍵裡 90.7% 是這樣 |

剩下不到 10% 是人工改過的（`Node_PlatRoot` → `m_plat_root`、`labelWin` →
`m_txt_win`）。**產出來之後改名是正常的**——這東西是把 90% 的機械勞動與打字錯誤
拿掉，不是取代判斷。

驗收標準不是「長得像」，是**產出來的程式碼要通得過 `nodes` 規則**：抽 80 份 `.csd`、
產出 2139 個綁定，用規則驗 **0 findings**。

第一版沒通過。跳過 `Panel_1` 這種沒改名的節點時，我把它的子節點接到了祖父層，
但 `getChildByName` **只找直接子層**——規則立刻回報 17 筆 `NODE_WRONG_PARENT`，
工具驗出了自己的生成器。現在的規則是：預設名的節點底下還有取過名字的後代時，
它自己照樣要綁；只有預設名的**葉節點**才跳過。

同名節點掛在不同父層下是合法的，但欄位名不能重複——會自動加尾碼並在 stderr
提醒你去改成看得懂的名字。

### 第一次接進 CI 一定要先建基準

```bash
luadetect scan --write-baseline
```

39 萬行的既有程式碼一定會掃出一批既存問題。第一天就讓 CI 全紅的話，結論一定是
把檢查關掉。基準檔（`.luadetect-baseline.json`）記下建立當下的問題，之後**只有新
出現的**才算失敗；既存的照樣列在報告裡，慢慢還。

指紋刻意不含行號——在檔案上方插幾行不該讓下面每一筆都變成新問題。

---

## MCP 工具

| 工具 | 用途 |
| --- | --- |
| `scan` | 掃描，預設只回錯誤等級、最多 50 筆 |
| `explain` | 單一檔案的診斷：對到哪份 `.csd`、依據是什麼 |
| `node_tree` | `.csd` 的節點階層（**執行期**語意，見下） |
| `bind_properties` | 反過來：從 `.csd` 產生 Properties（UIManager）綁定程式碼 |
| `verify_parser` | 拿 csdview 對照，檢查 `.csd` 解析本身有沒有語意缺口 |
| `list_projects` | 認得哪些專案、各自的兩個根目錄 |
| `rules` | 所有規則與說明 |
| `write_baseline` | 建立／收緊基準檔 |

### agent 回答時會把統整表呈現出來

`scan` 與 `explain` 的回傳都帶一份**已經排好的 markdown 表格**（`summaryTable`），
外加一句 `present` 指示。伺服器的 instructions 與工具說明也都寫著要原樣呈現。

不讓 agent 自己從 `summary` 那份 JSON 重組表格，有兩個理由：重組要花 token，
而且**數字抄錯了沒人看得出來**。給它一段可以直接貼出去的表格，它只需要轉述。

用 markdown 而不是對齊過的 ASCII，因為 agent 的回答本身是以 markdown 呈現的
——ASCII 表格貼進 markdown 會散掉。

| 回傳欄位 | 內容 |
| --- | --- |
| `summaryTable` | 規則×嚴重度、涵蓋率、降級原因（`scan`）／診斷與逐筆問題（`explain`） |
| `findingsTable` | 逐筆表格，最後一欄已經寫好「為什麼是這個等級」 |
| `summary` | 同一份統整的結構化版本，要自己算的時候用 |
| `present` | 給 agent 的呈現指示 |

**看到「節點不存在」時，先用 `explain` 確認它對到的是正確的 `.csd`。** 對錯了的話
問題出在解析，不是程式碼——那種時候去改程式碼會把本來對的東西改壞。
還是覺得不對就用 `verify_parser`：`explain` 說的是「對到哪份 `.csd`」，
`verify_parser` 說的是「那份 `.csd` 讀得對不對」。

---

## 這個工具在哪些事情上可以相信

| 可以相信（回報即是問題） | 只能參考 |
| --- | --- |
| `NODE_MISSING` 是 `error`：下面那五個條件全部成立才會這樣標 | `NODE_MISSING` 是 `warn`：訊息會寫出是哪個條件沒過 |
| 原生 `scheduleScriptFunc` 存進 handle 卻整檔找不到解除 → `error` | `SafeScheduleScriptFunc` 綁了 owner 卻沒解除 → `warn`（見下） |
| `RES_MISSING`：資料夾在、檔案不在，7/7 逐一驗證都是真的缺檔 | `origin` 是 `module` 的推測 → 一律 `warn` |
|  | 事件註冊在 `ctor` / `Init` 裡（多半是刻意的常駐）→ `info` |
|  | `NODE_WRONG_PARENT`：同名節點來源不只一份時會誤判 |

嚴重度是算出來的，不是形容詞。**`error` 的意思是「這會當機」，不是「這不對」**
——這兩件事差很多，而且分不清楚的話 CI 會被一堆不會當機的東西擋住。

`NODE_MISSING` 要同時滿足**全部**條件才是 `error`：

| 條件 | 為什麼 |
| --- | --- |
| 取用鏈的起點確認在這份 `.csd` 裡（且來源是 `self`/`paired`/`stem`/`config`） | 起點不在就代表節點是外面傳進來的，對不上很可能是我們找錯版面 |
| 取到的結果**真的被讀** | `getChildByName` 回 nil 本身不會當機，對那個 nil 呼叫方法才會。產生出來的 `XxxProperties.lua` 裡常常留著一批賦值 nil、卻沒人讀的欄位 |
| **沒有 nil 防護** | `if x == nil then x = ...create() end` 是「取不到就自己建」，完全正確 |
| 這個名字**全庫的 `.csd` 都找不到** | 名字在別的 `.csd` 裡有，最可能的解釋是這支 Lua 的版面其實是那一份，不是節點被刪了 |
| 這個模組的 `.lua` 全部讀得到 | 讀不到的檔案裡可能有 `setName` 建出這個節點 |

任一條不成立就降成 `warn`，訊息會寫出是哪一條。`info` 是提醒，不影響離開碼。

這套判準是拿「Inanna 目前是可運作的」當測試對照校出來的：原本 50 筆 `error`
**全部**是不會當機的東西——26 筆是沒人讀的死綁定、13 筆有 nil 防護、其餘是對到了
錯的版面。反過來，在真實檔案裡注入一個寫實的新 bug（取用鏈成立、結果被使用、
沒有防護、名字全庫不存在）照樣回報 `error`。

排程那邊的分級依據是**這段程式碼會不會真的踩到野指標**：

| 寫法 | 沒解除的話 | 等級 |
| --- | --- | --- |
| `scheduleScriptFunc( fn, ... )` | 回呼在節點釋放後照樣觸發 → `EXC_BAD_ACCESS` | `error` |
| `SafeScheduleScriptFunc( nil, fn, ... )` | 一樣，wrapper 沒有 owner 可檢查 | `error` |
| `SafeScheduleScriptFunc( self, fn, ... )` | owner 失效時 wrapper 會自己解除，不會炸；但在那之前排程照跑 | `warn` |

第三種是「少了雙保險」而不是當機來源，所以不擋 CI。靜態看不出 `owner` 是 C++
節點還是純 Lua table（後者會被 wrapper 擋掉、退回無保護模式並 `LogError`），
一律當成有保護——寧可少報一級。

事件監聽（`LISTENER_LEAK`）的依據是**這個監聽會不會自己消失**：

| 寫法 | 沒解除的話 | 等級 |
| --- | --- | --- |
| `dispatcher:addEventListenerWithFixedPriority( l, 10 )` | 沒有節點綁定，會一直收事件 | `error`（`ctor`/`Init` 裡 → `warn`） |
| `dispatcher:addEventListenerWithSceneGraphPriority( l, node )` | 監聽綁在 `node` 上，節點銷毀時 EventDispatcher 自己會移除 | 不報 |

「有沒有解除」要認得 `cc.EventDispatcher` 的**全部五種** remove，因為不是每一種
都拿 listener 物件：

| 寫法 | 比對的依據 |
| --- | --- |
| `removeEventListener( listener )` | listener 物件（會走雙向別名） |
| `removeCustomEventListeners( 事件名 )` | **事件名**——`cc.EventListenerCustom` 的正規解法，不需要留著 listener |
| `removeEventListenersForTarget( node )` | 節點 |
| `removeAllEventListeners()` / `removeEventListenersForType( t )` | 一次全解除 |

第二種漏掉的代價實測過：全庫 5 筆 error **全部是誤報**，而且訊息還斷言
「已經不可能解除」，把 `BS_ContentType3.lua`、`ModelLocator.lua` 這些完全正確的
程式碼指成必須重寫。註冊寫 `tostring(eventName)`、解除寫 `eventName` 也算配對。

這一塊本來**從來沒被檢查過**：`addEventListenerWithFixedPriority` 與
`addEventListener` 前綴相同，而舊規則的正則要求 `addEventListener` 後面緊接 `(`，
所以一處都對不到。實測 Inanna：FixedPriority 8 處，逐一查證後**全部都有正確解除**（5 處走 removeCustomEventListeners）。

### 為了不冤枉正確的程式碼，這些情況會直接跳過

每一條都對應到一個實際踩過的誤報，測試裡都留著案例：

- **`setName` 建出來的節點**——大廳的推薦遊戲按鈕就是這樣掛上去的，不會出現在任何 `.csd`。
  引數是常數（`setName( UIName.IMG_FISH )`）或用 `x.name = "..."` 寫的也認得
- **對到的 `.csd` 與取用的節點幾乎沒有交集**——吻合率低於 50% 就判定「這不是這個檔案的
  版面」，整份跳過。有些檔案只是順手載了幾個裝飾用的 `.csb`，真正的版面是外面傳進來的
- **名稱是動態組出來的**（`string.format` 之類）
- **查不出對應 `.csb` 的檔案**——不猜。猜錯會叫人去改本來正確的程式碼，比漏報糟
- **存進表格的排程 handle**——註冊寫 `self.m_timers[k]`、解除時先取別名再寫 `t[k]`，
  是完全正確的程式碼
- **`addEventListenerWithSceneGraphPriority`**——監聽綁在那個節點上，節點銷毀時
  EventDispatcher 自己會移除。專案裡 25 處，16 處沒有手動解除，但那是對的
- **註冊寫 `local`、解除寫欄位**——中間靠 `self.m_x = listener` 連起來（`PlayInfo.lua`
  就是這個寫法）。名字比對要是**雙向**的等價類，單向別名解析會對不到而誤報
- **`widget:addEventListener( callback )`**——那是 `ccui.Widget`，Cocos 的 Lua 綁定
  **沒有** widget 版的 `removeEventListener`；callback 存在 widget 上，節點銷毀就
  跟著走。專案裡 33 處 `addEventListener(` 全是這一種。只有接收者是
  `cc.EventDispatcher`（`dispatcher` / `getEventDispatcher()`）時才需要解除
- **模組裡有 `.lua` 讀不到時不給 `error`**——讀不到的檔案裡可能有 `setName` 建出這個
  節點，少收一個就會冤枉一行正確的程式碼。降級成 `warn` 並在訊息裡說明原因

### 格式樣板也查得到——只是查的不是「這個檔案在不在」

`string.format( "mail/Img/Avatar/Avatar_%02d.png", n )` 的檔名要執行期才知道，
但**不必知道 `%02d` 會變成什麼，仍然有兩件事是確定的**：

1. **目錄那一段是靜態的** → 資料夾在不在
2. **那個資料夾裡有沒有任何檔案符合這個形狀**（`Avatar_%02d.png` → `avatar_.*\.png`）

一個都不符合就是 `RES_PATTERN_EMPTY`（`warn`）：資料夾被搬走、或檔案整批改了命名。

實測全庫 155 條樣板：119 條目錄是靜態的（現在都檢查了），36 條連目錄都是變數
（`%s/%s.png`）真的查不了。**今天一筆問題都抓不到**——這條規則的價值是迴歸守門，
不是找現有 bug。願意加它的前提是**誤報率 0**。

兩道守門是必要的，少任何一道都會爆出誤報：

- **格式符之前一定要有目錄。** `coin_%d.png`、`%s.atlas` 是要接前綴的尾巴，
  不是路徑；少了這道守門它們會落到索引根，實測直接產生 30 筆誤報
- **常數前綴要先接上。** `DIR_PATH .. "/%s.skel"` 單看後半段目錄是 `/`，
  形狀當然對不上——那會變成 2 筆假問題

### 共用資源這一組為什麼只報一種情況

只在**「所在資料夾解析得到、檔案卻不在裡面」**時才報。這個前提是它能不能被留著用
的關鍵：專案裡的字面資源路徑有一大半相對於執行期的搜尋路徑，或屬於根本不在這台
機器上的別的資源包（`VolcanoResource/`、`BonusBingoResource/`）。

| 判斷方式 | 誤報率（實測 Inanna 全庫） |
| --- | --- |
| 「檔案找不到就報」 | 8.67%（235 / 2712）——不能用 |
| 「資料夾在、檔案不在才報」 | 0%（7 / 7 逐一驗證都是真的缺檔） |

資料夾對不到任何資源根就跳過，數字記在 `stats.unresolvedResources` 裡——目前
Inanna 是判斷 1180 處、跳過 214 處。**覆蓋率不寫出來的話，「沒有回報」會被當成
「資源都對」。** 要納入更多就在 `.luadetect.json` 補 `resourceRoots`。

另外三類「共用資源」的問題**刻意不做**，因為實測誤報會壓過價值：

- **`addSpriteFrames` / `retain()` 沒有對應的釋放**——32 + 10 個檔案看起來不對稱，
  但每一處都包在 `isSpriteFramesWithFileLoaded` 或 `if` 裡、或存進 `inn.*` 全域表，
  都是刻意的「載入一次就常駐」。而且共用資源提早釋放才會當機，不釋放常常是對的。
- **`getSpriteFrame( "字串" )` 的鍵**——ground truth 有（223 份 plist、4016 個名稱），
  但全庫只有 6 處是字面呼叫，其餘都是動態組的。價值密度跟 `getChildByName`
  的 8205 處差三個數量級。
- **`.csb` 存不存在**——那是 Cocos Studio 編出來的產物，原始碼樹裡不一定有；
  而且節點規則已經在驗它了（對不到就報 `NODE_CSB_UNRESOLVED`）。

### 訊息與指紋一定是單行的

規則會把原始碼片段放進訊息（節點名、handle、事件的鍵），而那片段可能是一整個
inline function。整段塞進去會壞掉三個地方，實測全部發生過：文字報告一筆佔六行、
markdown 表格的欄數對不齊（GFM 會整張垮掉）、以及基準檔的指紋含了那段函式內容
——callback 裡改一個空白就變成「新問題」，正好違反「指紋不含行號」這個設計。

所以 `Finding` 在建構時就把訊息與鍵壓成一行並截斷，不是叫每條規則自己記得
（漏一條就壞一次）。

跳過的檔案數會出現在 `stats.unresolvedCsb`，加 `--explain` 可以列出是哪些。
讀不到的 `.lua`（被別的程式鎖住、權限不足）算在 `stats.unreadable`，報告最後會講出來——完全沒被檢查到的檔案不該跟乾淨的檔案長得一樣。

---

## 為什麼不直接叫 csdview

一份 `.csd` 叫一次 `csdview tree` 要 **0.5 秒**，全庫 1295 份就是 11 分鐘；自己解 XML
是 5 秒多。掃描一天要跑很多次，那個差距不能接受。

**但獨立實作會漂移，所以有 `luadetect verify` 這道交叉檢查。** 這不是理論問題：
`ProjectNodeObjectData` 引用別的 `.csd` 時，引用處自己**還可以再加子節點**，執行期
兩邊都在——這裡漏了 inline 那一批，於是回報了 9 筆「節點不存在」的 `error`，
而 csdview 一直是對的。那個 bug 是人工讀 XML 才發現的，現在變成一個指令：

```bash
luadetect verify                  # 隨機抽 40 份比對（約 20 秒）
luadetect verify BottomBarGWS     # 只比對一份
luadetect verify --sample 300     # 大一點的樣本（約 2 分鐘）
```

修好之後抽 300 份雙向比對：**扣掉下面那個刻意的差異，節點名稱完全一致。**
兩邊各有一半是對的——

| | ProjectNode 引用處自己加的子節點 | 被引用檔案的根節點那一層 |
| --- | --- | --- |
| csdview | 正確 | 保留（`n_extraBet/Node/Bet_Icon`），拿去對 Lua 取用鏈會整串對不上 |
| luadetect | 正確（修好之後） | 不保留，與執行期一致 |

沒裝 csdview 也完全不影響 `scan`，只是少了這道檢查。

---

## 一個跟 csdview 不同、而且是這裡對的地方

`ProjectNodeObjectData` 引用另一個 `.csd` 時，執行期 `CSLoader` 是把子檔的根節點載進來、
再改名成引用處的名字。所以子檔根節點的**子節點在 Lua 端是直接子節點**：

```lua
local page = view:getChildByName( "infoPage" )   -- ProjectNode，引用 InfoPage_H.csd
local bg   = page:getChildByName( "Bg" )         -- 直接就拿得到
```

csdview 為了畫面呈現保留了那一層根節點（路徑是 `infoPage/Node/Bg`）。拿它的路徑來對
Lua 的取用鏈會整串對不上，所以這裡自己解 XML。已用 VoucherCenter 的線上程式碼驗證過。

---

## 專案設定（選用）

多數情況不需要。要覆寫時在專案根目錄放 `.luadetect.json`：

```json
{
  "luaRoot": "InannaLua",
  "csdRoot": "C:/.../CocoStudioProject/Inanna/cocosstudio",
  "exclude": ["Tools/poco/", "LuaPanda.lua"],
  "resourceRoots": ["InannaResource", "InannaResource/Inanna"],
  "pathPrefixes": ["legacy/"],
  "csbMap": {
    "Modules/Foo/BarView.lua": ["foo/Bar.csb"]
  }
}
```

| 欄位 | 用途 |
| --- | --- |
| `luaRoot` | Lua 根目錄（預設自動找 `src/lua`、`lua`、`scripts/lua`，或叫 `*Lua` 的資料夾） |
| `resourceRoots` | 資源根（預設自動找 `*Resource(s)/` 與它底下的 bundle、`res/`） |
| `csdRoot` | cocosstudio 根目錄（預設沿用 csdview 登錄檔） |
| `pathPrefixes` | Lua 路徑要多剝掉哪些前綴（**加在**自動推導出來的那些之上） |
| `exclude` | 路徑含這些字串的檔案不掃 |
| `csbMap` | 手動指定某個 Lua 檔對應的 `.csb`——自動判斷失敗時用 |

### 換一個專案要不要改程式碼

不用。三個「Inanna 的形狀」都是照命名慣例找的，不是寫死的名字：

| 東西 | 怎麼找 |
| --- | --- |
| Lua 根 | `src/lua` / `lua` / `scripts/lua`，或任何 `*Lua` 資料夾（**往下找兩層**） |
| 資源根 | **Lua 根同一層**底下的 `*Resource(s)`（連它底下的每個 bundle）、`res/` |
| 路徑前綴 | 從**實際找到的資源根**反推：`Foo/BarResource/Bar` → `barresource/bar/` |

兩層是因為 ManganDahen 的是 `ClientCocos/MDLua` 與 `ClientCocos/MDResource`
——同一套慣例，只是多包一層。兩條界線：**不鑽進另一個 repo**（有 `.git` 或
`.luadetect.json` 的子目錄就停），**資源根不跨出 Lua 根那一層**（ManganDahen 的
`ClientFlash/resource` 是 Flash 版的，多掛一棵不相干的資源樹只會製造誤報）。

這幾件事猜錯的共同特徵是**不會有錯誤訊息**：Lua 根退回整個倉庫、資源規則整組
跳過、`.csb` 對不到 `.csd`，全部只表現成涵蓋率變低。所以掃新專案的第一件事是
看涵蓋率那張表——它會直接告訴你哪一類根本沒檢查。對不上就用上面的欄位補。

實測（`ManganDahen`，登錄檔裡的另一個專案，一行程式碼都沒改）：

```
  項目              檢查了  沒檢查  總數  沒檢查的是什麼
  ----------------  ------  ------  ----  ----------------------------
  .lua 檔案             32       0    32  （全部讀得到）
  有節點取用的檔案      21       3    24  查不出對應的 .csb，不猜
  資源路徑               1       2     3  要接前綴的檔名片段 1、對不到資源根 1
```

---

## 接進 CI

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0          # 淺層 clone 沒有 origin/main，--base 會失敗（exit 2）
- name: luadetect
  run: python luaDetect/run_cli.py scan --changed --base origin/main --severity error
```

**`fetch-depth: 0` 不能省。** actions/checkout 預設只抓一個 commit，`origin/main`
在那個 clone 裡不存在，`--base` 就會失敗——工具會 exit 2 明講這件事，而不是假裝
沒有異動然後通過。

`--changed` 只看這個 PR 動過的 `.lua`。全庫掃 7.5 秒還好，但只掃異動檔是
**0.5 秒**（含 Python 啟動），掛在 pre-commit 上不會有人想拔掉。

pre-commit hook 用的是 `--staged`：**檔名與內容都從 git 索引取**，檢查的就是這次
真的要提交的東西。`git add -p` 只暫存一半時，讀工作目錄會放過已經暫存的問題
（也會為沒要提交的改動擋下 commit）。

被檢查檔案的周邊——成對的 `XxxProperties.lua`、模組裡的 `setName`——還是讀工作目錄。
那份在實務上等於或新於索引（先改檔再 `git add`），所以不會漏掉問題。

---

## 效能

量法要講清楚，不然數字沒有意義：

| 情境 | 時間 |
| --- | --- |
| 整條 CLI 全庫掃（含 Python 啟動、`.csd` 索引與樹的冷解析） | **7.5s** |
| 行程內全庫掃、已暖機（取三次最小值） | **5.6s** |
| `--changed`（pre-commit 走這條） | **0.5s** |
| 單一檔案 | **0.3s** |

同一台機器上原本是 15 秒。全庫那 5.6 秒裡約 2.2 秒是嚴重度校準要做的
「這個欄位有沒有被讀／有沒有 nil 防護」分析。

**量的時候一定要暖機**：第一組跑的規則會付掉整個 `.csd` 索引與名稱快取的建置
成本，沒暖機時 `nodes` 量出來是 13.7 秒，實際是 4.4 秒。
省下來的是重複做的事，不是檢查強度——每一步的結果都逐筆對過，findings 完全相同：

| | 原本 | 現在 |
| --- | --- | --- |
| 剝註解（`luasrc.scan`） | 每檔 4.9 次 | 1.5 次（兩組規則共用） |
| 剝註解本身 | `list(text)` 逐字改 | 正則跳位 + 片段拼回，快 5 倍 |
| `.csd` 解析 | 每份 3.0 次 | **1.0 次**（`tree()` 與巢狀展開共用快取） |
| 節點鏈比對 | 每個取用點重走整棵樹 | 建 `name → [節點]` 索引 |
| `enclosing_function` | 每個呼叫點重掃前綴 O(n·m) | 建表 + 二分，快 366 倍 |
| 常數表 | 三個相似的正則各掃全文 | 合成一個掃一遍 |
| 常數解析 | 每個檔案 2.6 次（三組規則各算一次） | 一次、三組共用，而且**只有用得到的檔案才算**（1304 個裡有 530 個兩者都不需要） |
| `_usage` 的四條防護正則 | 每個名字編譯四條、各掃一遍 | 併成一條交替式，掃一遍 |

## 檔案

```
luaDetect/
  .claude/skills/luadetect/
    SKILL.md        怎麼跑、怎麼呈現結果（`--skill` 部署一份指標到使用者層級）
    RULES.md        開發規則：每一條對應一次實際踩過的坑。只有這一份
  run.py            MCP 啟動點（給 MCP 客戶端用絕對路徑呼叫）
  run_cli.py        CLI 啟動點（給 git hook 與 CI）
  install.py        安裝與註冊
  luadetect/
    luasrc.py       Lua 輕量解析：剝註解、常數表、取用鏈、指派目標
    csdtree.py      .csd → 節點樹（執行期語意的巢狀展開）、索引、全庫名稱快取
    restree.py      資源根的索引（資料夾 → 檔名）與「算不算缺檔」的判斷
    fileio.py       檔案讀寫的韌性（被鎖住時重試、原子寫入、跨磁碟路徑）
    verify.py       跟 csdview 交叉比對，抓自己解析器的語意缺口
    projects.py     Lua 根與 cocosstudio 根的配對與解析
    scan.py         掃描主流程，CLI 與 MCP 共用
    report.py       結果表示、文字/JSON 輸出、基準檔
    rules/
      nodes.py      節點契約
      lifecycle.py  生命週期對稱
      resources.py  共用資源契約
    cli.py          命令列
    server.py       MCP stdio 伺服器（不依賴官方 SDK）
    tools.py        MCP 工具定義與 dispatch
  tests/
    test_luasrc.py     Lua 解析
    test_csdtree.py    .csd 解析與巢狀展開
    test_rules.py      三組規則與嚴重度判準
    test_protocol.py   MCP 協定（沒有 pytest 也能直接執行）
    test_regressions.py 每一條對應一個修掉的 bug
    test_install.py    hook 裝到哪（worktree / core.hooksPath）、移除時不誤刪別人的 hook
    test_resources.py  共用資源契約與它的跳過條件
    test_verify.py     跟 csdview 的交叉比對（用假的 csdview，不依賴本機有裝）
    test_summary.py    統整表（CLI 的對齊、給 agent 的 markdown）
```
