"""命令列的啟動點。

跟 run.py 同一個道理：用絕對路徑呼叫這個檔案就能跑，不必先安裝套件、
也不必管工作目錄在哪。git hook 與 CI 用的就是這個。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from luadetect.cli import main   # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
