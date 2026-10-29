# luaDetect

靜態檢查工具，掃 Cocos2d-x Lua 專案裡的程式碼與 `.csd` 對應關係。
使用說明看 `README.md`。

主要用在 Inanna，但**專案名不寫死在程式碼裡**：Lua 根、資源根、路徑前綴都是照
命名慣例找出來的（`*Lua`、`*Resource(s)`，前綴從實際找到的資源根反推）。要加
Inanna 專屬的東西之前先想清楚——這三件事猜錯都**不會有錯誤訊息**，只會讓涵蓋率
安靜地掉下去。`tests/test_portability.py` 用一個叫 `Aerith` 的假專案守著這條線。

**動手前先讀 `.claude/skills/luadetect/RULES.md`**——那裡是這個工具開發過程中
實際踩過的每一個坑，包含 `error` 只能用在「會炸」的情況、回報一定要附涵蓋率表、
以及幾個不會有錯誤訊息的正則／快取陷阱。規則只寫在那一份，這裡不重複。

那份規則**跟著這個 repo 走**，不在 `~/.claude/skills/` 裡。使用者層級那一份是
`python install.py --skill` 部署出去的指標，指回這裡；工具本體搬過家就重跑一次。

改完跑 `python -m pytest -q tests`，要全過。
（不寫項數——寫死的數字每次加測試就過期，而過期的樣子跟壞掉一樣。）
