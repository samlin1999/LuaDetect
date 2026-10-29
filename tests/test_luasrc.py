"""Lua 解析的測試。

重點放在**位移要對得上原檔**與**引數解析**：這兩件事錯了，回報的行號會指到別的地方，
或是把正確的程式碼判成問題——兩種都會讓人不再相信這個工具。
"""
from luadetect import luasrc


def test_comments_blanked_but_offsets_preserved():
    src = 'local a = 1 -- 註解裡的 getChildByName( "x" )\nlocal b = 2\n'
    code, _ = luasrc.scan(src)
    assert len(code) == len(src)
    assert "getChildByName" not in code
    assert luasrc.line_of(code, code.index("local b")) == 2


def test_block_comment_and_strings_survive():
    src = 'local s = "keep me"\n--[[ 這段\n整個不算 ]]\nlocal t = 1\n'
    code, spans = luasrc.scan(src)
    assert "keep me" in code
    assert "整個不算" not in code
    assert any(value == "keep me" for _, _, value in spans)


def test_string_containing_double_dash_is_not_a_comment():
    src = 'local url = "http://a--b/c"\nlocal after = 1\n'
    code, _ = luasrc.scan(src)
    assert "http://a--b/c" in code
    assert "after" in code


def test_constants_table_and_alias():
    src = ('local NodeNames = {\n'
           '    BG = "bg",\n'
           '    BTN = "btn_ok",\n'
           '}\n'
           'local M = {}\n'
           'M.NodeNames = NodeNames\n')
    code, _ = luasrc.scan(src)
    consts = luasrc.constants(code)
    assert consts["tables"]["NodeNames"]["BG"] == "bg"
    assert luasrc.resolve_literal("NodeNames.BTN", consts) == "btn_ok"
    assert luasrc.resolve_literal('NodeNames["BG"]', consts) == "bg"
    assert luasrc.resolve_literal("self.NodeNames.BG", consts) == "bg"
    assert luasrc.resolve_literal('"literal"', consts) == "literal"
    assert luasrc.resolve_literal("string.format( '%d', i )", consts) is None


def test_expr_before_receiver():
    code = 'self.m_bg:getChildByName( X )'
    index = code.index(":getChildByName")
    assert luasrc.expr_before(code, index) == "self.m_bg"

    code = 'list[ i ]:getChildByName( X )'
    assert luasrc.expr_before(code, code.index(":getChildByName")) == "list[ i ]"

    code = 'self:GetView():getChildByName( X )'
    assert luasrc.expr_before(code, code.rindex(":getChildByName")) == "self:GetView()"


def test_call_args_and_split():
    code = 'f( a, g( b, c ), "x, y" )'
    inner = luasrc.call_args(code, code.index("("))
    parts = luasrc.split_args(inner)
    assert parts == ["a", "g( b, c )", '"x, y"']


def test_assign_target_reaches_across_a_long_receiver():
    # 這正是排程 handle 抓不到的那個情境：等號離呼叫很遠
    code = "self.m_entry = cc.Director:getInstance():getScheduler():scheduleScriptFunc("
    index = code.index("scheduleScriptFunc")
    assert luasrc.assign_target(code, index) == "self.m_entry"


def test_assign_target_ignores_nested_call():
    code = 'local a = foo( bar:getChildByName( "x" ) )'
    index = code.index(":getChildByName") + 1
    assert luasrc.assign_target(code, index) is None


def test_enclosing_function():
    code = ('function View:OnEnter()\n'
            '    doThing()\n'
            'end\n')
    assert luasrc.enclosing_function(code, code.index("doThing")) == "View:OnEnter"
