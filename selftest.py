"""端到端自检：在独立测试容器里跑一遍全部工具，结束后删除容器和数据卷。

需要本机可用的 docker；在 MaiBot 根目录运行：uv run python plugins/linux_computer/selftest.py
"""

from datetime import datetime
from pathlib import Path

import asyncio
import json
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plugin import (READ_FALLBACK_MARK, LinuxComputerPlugin, add_link_reminder, add_progress_reminder,  # noqa: E402
                    add_screenshot_reminder, apply_tool_catalog)

NAME = "maibot-linux-computer-selftest"


def check_link_reminder() -> None:
    """链接提醒：只针对聊天消息里没打开过的链接，紧跟在最新的那条之后。"""

    def item(kind: str, item_id: str, **fields: object) -> dict:
        return {"item_type": kind, "meta": {"item_id": item_id, "logical_turn_id": None,
                                            "timestamp": "2026-11-02T12:00:00"}, **fields}

    def text(kind: str, item_id: str, value: str) -> dict:
        return item(kind, item_id, parts=[{"type": "text", "text": value}])

    items = [
        text("SystemMessageItem", "s", "system"),
        text("UserMessageItem", "a", '<message msg_id="101" time="12:00:01" user="小明">\n看看这个 https://example.com/a?x=1。'),
        text("UserMessageItem", "b", '<message msg_id="102" time="12:00:05" user="小红">\n看过了 https://opened.example/page'),
        text("UserMessageItem", "m", '<message msg_id="103" time="12:00:06" user="麦麦" is_self_message="true">\n'
                                     "https://self.example"),
        text("UserMessageItem", "n", '<message msg_id="104" time="12:00:07" user="小刚">\nhttps://native.example'),
        item("ProviderActivityItem", "p", provider_type="x", call_id="2", status="completed", action_type="open_page",
             display_summary="打开网页", details=["https://native.example"], source_count=1),
        item("FunctionCallItem", "c", tool_call={"call_id": "1", "func_name": "x",
                                                 "args": {"url": "https://opened.example/page"}, "extra_content": None}),
        item("FunctionCallOutputItem", "d", call_id="1", output="ok", success=True, tool_name="x"),
        text("UserMessageItem", "e", "子代理结论 https://internal.example"),
    ]
    result = add_link_reminder(items)
    assert [i["meta"]["item_id"] for i in result] == ["s", "a", "link-reminder-a", "b", "m", "n", "p", "c", "d", "e"], result
    reminder = result[2]["parts"][0]["text"]
    assert "- https://example.com/a?x=1\n" in reminder, reminder
    assert not any(word in reminder for word in ("opened", "self", "native", "internal")), reminder
    assert add_link_reminder(items[:1] + items[2:]) is None


def check_progress_reminder() -> None:
    """进度提醒：上次说话后连续操作超过阈值才提醒，插在最近的工具结果和截图之后。"""

    def item(kind: str, item_id: str, second: int, **fields: object) -> dict:
        return {"item_type": kind, "meta": {"item_id": item_id, "logical_turn_id": "t" if "Function" in kind else None,
                                            "timestamp": f"2026-11-02T12:00:{second:02d}"}, **fields}

    def call(item_id: str, second: int, name: str) -> list:
        return [item("FunctionCallItem", item_id, second, tool_call={"call_id": item_id, "func_name": name, "args": {},
                                                                      "extra_content": None}),
                item("FunctionCallOutputItem", f"{item_id}-out", second, call_id=item_id, output="ok", success=True,
                     tool_name=name)]

    items = [
        item("UserMessageItem", "self", 0, parts=[{"type": "text", "text":
             '<message msg_id="1" time="12:00:00" user="麦麦" is_self_message="true">\n好，我去弄'}]),
        *call("a", 1, "linux_shell"), *call("b", 2, "linux_desktop"),
        item("UserMessageItem", "shot", 2, parts=[{"type": "text", "text": "[图片]"},
                                                  {"type": "image", "image_format": "jpeg", "image_base64": ""}]),
        item("UserMessageItem", "tail", 3, parts=[{"type": "text", "text": "<system-reminder>tool_search</system-reminder>"}]),
    ]
    late = datetime.fromisoformat("2026-11-02T12:01:30")
    result = add_progress_reminder(items, 60, now=late)
    assert [i["meta"]["item_id"] for i in result][-2:] == ["progress-reminder-shot", "tail"], result
    assert "2 步" in result[-2]["parts"][0]["text"], result[-2]
    assert add_progress_reminder(items, 60, now=datetime.fromisoformat("2026-11-02T12:00:30")) is None
    assert add_progress_reminder(items, 0, now=late) is None
    assert add_progress_reminder([*items, *call("r", 4, "reply")], 60, now=late) is None


def check_screenshot_reminder() -> None:
    """截图提醒：上次说话后用过 linux_desktop 才提醒，指向最近一次带截图的结果，插在它的截图之后。"""

    def item(kind: str, item_id: str, **fields: object) -> dict:
        return {"item_type": kind, "meta": {"item_id": item_id, "logical_turn_id": "t" if "Function" in kind else None,
                                            "timestamp": "2026-11-04T12:00:00"}, **fields}

    def call(item_id: str, name: str, output: str = "ok") -> list:
        return [item("FunctionCallItem", item_id, tool_call={"call_id": item_id, "func_name": name, "args": {},
                                                             "extra_content": None}),
                item("FunctionCallOutputItem", f"{item_id}-out", call_id=item_id, output=output, success=True, tool_name=name)]

    def desktop(item_id: str) -> list:  # 主程序拆出截图时的结果文字和媒体消息
        media = f'ok\n\n<tool_result_media_list>\n  <media msg_id="tool_result:{item_id}:1" type="image" />\n</tool_result_media_list>'
        return [*call(item_id, "linux_desktop", media),
                item("UserMessageItem", f"{item_id}-shot", parts=[{"type": "image", "image_format": "jpeg", "image_base64": ""}])]

    said = item("UserMessageItem", "self", parts=[{"type": "text", "text":
                '<message msg_id="1" time="12:00:00" user="麦麦" is_self_message="true">\n好，我去弄'}])
    items = [said, *desktop("a"), *desktop("b"), *call("c", "linux_desktop"), *call("d", "linux_shell")]
    result = add_screenshot_reminder(items)
    ids = [i["meta"]["item_id"] for i in result]
    assert ids[ids.index("b-shot") + 1] == "screenshot-reminder-b-shot", ids  # c 没有截图，指向 b
    assert "media_index=tool_result:b:1" in result[ids.index("b-shot") + 1]["parts"][0]["text"], result
    # 同一轮并行调用：先是两个结果，再是截图；提醒要放在整批之后
    shot_b, shell_d = desktop("b"), call("d", "linux_shell")
    batch = [said, shot_b[0], shell_d[0], shot_b[1], shell_d[1], shot_b[2]]
    assert [i["meta"]["item_id"] for i in add_screenshot_reminder(batch)][-2:] == ["b-shot", "screenshot-reminder-b-shot"]
    assert add_screenshot_reminder([*items, *call("r", "reply")]) is None
    assert add_screenshot_reminder([*desktop("a"), said, *call("d", "linux_shell")]) is None


def check_tool_catalog() -> None:
    """工具目录：类别目录进系统提示词、整组载入、会话内保持、tools 顺序不变、没搜到时列出类别。"""

    def item(kind: str, item_id: str, **fields: object) -> dict:
        return {"item_type": kind, "meta": {"item_id": item_id, "logical_turn_id": "t" if "Function" in kind else None,
                                            "timestamp": "2026-11-03T12:00:00"}, **fields}

    def tool(name: str) -> dict:
        return {"type": "function", "function": {"name": name, "description": name, "parameters": {}}}

    def reminder(*names: str) -> dict:  # 主程序 build_deferred_tools_reminder 的格式
        lines = "\n".join(f"{index}. {name}: {name} 的描述" for index, name in enumerate(names, 1))
        return item("UserMessageItem", "z", parts=[{"type": "text", "text": "<system-reminder>\n以下工具当前未直接暴露给你，"
                    f"但可以通过 tool_search 工具发现并在后续轮次中使用：\n{lines}\n\n如需其中某个工具…\n</system-reminder>"}])

    def search(call_id: str, output: str) -> list:
        return [item("FunctionCallItem", call_id, tool_call={"call_id": call_id, "func_name": "tool_search",
                                                              "args": {"query": "x"}, "extra_content": None}),
                item("FunctionCallOutputItem", f"{call_id}-out", call_id=call_id, output=output, success=True,
                     tool_name="tool_search")]

    system = item("SystemMessageItem", "s", parts=[{"type": "text", "text": "system"}])
    loaded: dict = {}
    pool: dict = {}
    names = lambda tools: [definition["function"]["name"] for definition in tools]  # noqa: E731

    items, tools = apply_tool_catalog([system, reminder("linux_desktop", "linux_cancel", "web_fetch")],
                                      [tool("reply"), tool("tool_search"), tool("linux_shell")], loaded, pool, {})
    catalog = items[0]["parts"][0]["text"]
    assert [i["meta"]["item_id"] for i in items] == ["s"], items  # 主程序的 deferred 提示被拿掉
    assert "- linux：linux_desktop、linux_cancel\n- web：web_fetch\n" in catalog and "描述" not in catalog, catalog
    assert names(tools) == ["reply", "tool_search", "linux_shell"], tools
    assert tools[1]["function"]["parameters"]["properties"]["limit"]["maximum"] == 5, tools[1]

    # 搜到 linux_desktop：同类别的 linux_cancel 一起载入，追加在末尾
    found = search("1", "已找到 1 个 deferred tools，它们会在后续轮次中加入可用工具列表：\n- linux_desktop（本次新发现）")
    items, tools = apply_tool_catalog([system, *found, reminder("linux_cancel", "web_fetch")],
                                      [tool("reply"), tool("tool_search"), tool("linux_shell"), tool("linux_desktop")],
                                      loaded, pool, {"linux_cancel": tool("linux_cancel")})
    assert names(tools) == ["reply", "tool_search", "linux_shell", "linux_desktop", "linux_cancel"], tools
    assert items[0]["parts"][0]["text"] == catalog, "目录不能变"
    assert items[2]["output"].startswith("已載入：linux_desktop\n同類別一起載入：linux_cancel\n"), items[2]

    # 搜索记录滑出上下文、主程序把工具折回 deferred 并换了顺序：仍保持载入、顺序不变；没搜到时列出类别
    missed = search("2", "未找到匹配的 deferred tools，请尝试更完整的工具名、前缀或其他关键词。")
    items, tools = apply_tool_catalog([system, *missed, reminder("linux_desktop", "linux_cancel", "web_fetch")],
                                      [tool("linux_shell"), tool("reply"), tool("tool_search")], loaded, pool, {})
    assert names(tools) == ["reply", "tool_search", "linux_shell", "linux_desktop", "linux_cancel"], tools
    assert items[2]["output"] == "沒有找到符合的工具。可以改用這些類別名稱搜尋：linux、web。", items[2]

    # 工具下线（不在本轮工具里、也不在 deferred 池里）才移除
    _, tools = apply_tool_catalog([system, reminder("linux_desktop", "linux_cancel", "web_fetch")],
                                  [tool("reply"), tool("tool_search")], loaded, pool, {})
    assert names(tools) == ["reply", "tool_search", "linux_desktop", "linux_cancel"], tools

    # 网页读取失败：不经搜索直接整组载入 linux_desktop
    _, tools = apply_tool_catalog([system, reminder("linux_desktop", "linux_cancel")], [tool("reply"), tool("tool_search")],
                                  {}, {}, {"linux_desktop": tool("linux_desktop"), "linux_cancel": tool("linux_cancel")},
                                  ["linux_desktop"])
    assert names(tools) == ["reply", "tool_search", "linux_desktop", "linux_cancel"], tools


async def main() -> None:
    check_link_reminder()
    check_progress_reminder()
    check_screenshot_reminder()
    check_tool_catalog()
    plugin = LinuxComputerPlugin()
    plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"},
                              "computer": {"container_name": NAME, "vnc_port": 0, "allowed_chats": ["owner"], "max_tabs": 2}})
    # 子代理要求只加在有 subagent 工具的 Planner 请求上；子代理自己的请求没有 reply / subagent
    system = [{"item_type": "SystemMessageItem", "meta": {"item_id": "s", "logical_turn_id": None,
               "timestamp": "2026-11-02T12:00:00"}, "parts": [{"type": "text", "text": "system"}]}]
    tools = [{"type": "function", "function": {"name": name}} for name in ("reply", "subagent")]
    hooked = await plugin.handle_planner_request(items=system, tool_definitions=tools)
    assert "subagent" in hooked["modified_kwargs"]["items"][0]["parts"][0]["text"], hooked
    assert await plugin.handle_planner_request(items=system, tool_definitions=tools[:1]) is None
    try:
        denied = await plugin.handle_shell("id", stream_id="group")
        assert not denied["success"] and "權限" in denied["error"], denied

        shell = await plugin.handle_shell("echo hi && pwd", stream_id="owner")
        out = json.loads(shell["content"])["results"][0]["result"]
        assert shell["success"] and out["stdout"] == "hi\n/workspace\n", shell

        failed = await plugin.handle_shell("exit 3", stream_id="owner")
        assert not failed["success"] and "code 3" in failed["error"], failed

        desktop = await plugin.handle_desktop(
            [{"action": "open", "path": "https://example.com"}, {"action": "wait_window", "title": "Example", "seconds": 30},
             {"action": "click", "x": 100, "y": 100}, {"action": "keys", "keys": ["ctrl", "l"]}, {"action": "observe"}],
            stream_id="owner")
        assert desktop["success"], desktop
        assert desktop["content_items"][0]["mime_type"] == "image/jpeg", desktop.keys()
        title = json.loads(desktop["content"])["active_window"]
        assert "Example" in title, title
        target = json.loads(desktop["content"])["results"][0]["result"]["target"]

        cdp = await plugin.handle_desktop(
            [{"action": "browser", "method": "Runtime.evaluate", "target": target, "params": {"expression": "document.title"}}],
            stream_id="owner")
        assert json.loads(cdp["content"])["results"][0]["result"]["result"]["result"]["value"] == "Example Domain", cdp

        async def tab_ids() -> list:
            listed = await plugin.handle_desktop([{"action": "browser", "method": "targets"}], stream_id="owner")
            return [tab["id"] for tab in json.loads(listed["content"])["results"][0]["result"]["targets"]]

        async def tab_count() -> int:
            return len(await tab_ids())

        tabs = await tab_count()
        page = await plugin.handle_read_page(url="https://example.com", stream_id="owner")  # 旧版单个 url 参数
        read = json.loads(page["content"])["results"][0]["result"]["pages"][0]
        assert page["success"] and "content_items" not in page, page
        assert read["title"] == "Example Domain" and "domain" in read["text"], read
        assert any("iana.org" in link["url"] for link in read["links"]), read["links"]
        # 一次并行读多个：打不开的只在自己那一项报错
        mixed = await plugin.handle_read_page(["https://example.com", "https://nonexistent.invalid"], stream_id="owner")
        pages = json.loads(mixed["content"])["results"][0]["result"]["pages"]
        assert mixed["success"] and pages[0]["title"] == "Example Domain" and "開啟失敗" in pages[1]["error"], pages
        bad = await plugin.handle_read_page(["https://nonexistent.invalid"], stream_id="owner")
        assert not bad["success"] and "開啟失敗" in bad["error"] and READ_FALLBACK_MARK in bad["error"], bad
        # 被拦截或 HTTP 错误：标出原因，附上改用桌面的提示；正常页面不标
        await plugin.handle_shell("mkdir -p /tmp/www && echo '<title>Just a moment...</title>' > /tmp/www/block.html && "
                                  "(setsid python3 -m http.server 8765 --bind 127.0.0.1 --directory /tmp/www "
                                  ">/dev/null 2>&1 &) && sleep 1", stream_id="owner")
        blocked = await plugin.handle_read_page(["http://127.0.0.1:8765/block.html", "http://127.0.0.1:8765/missing",
                                                 "https://example.com"], stream_id="owner")
        result = json.loads(blocked["content"])["results"][0]["result"]
        assert [page.get("problem") for page in result["pages"]] == ["像是驗證或阻擋頁面", "HTTP 404", None], result
        assert READ_FALLBACK_MARK in blocked["content"], result
        assert await tab_count() == tabs, "read tabs must be closed"

        # max_tabs=2：再开两个网页后，最早由 open 打开的 example.com 标签页被自动关闭
        more = await plugin.handle_desktop([{"action": "open", "path": "https://example.com/?2"},
                                            {"action": "open", "path": "https://example.com/?3"}], stream_id="owner")
        newest = [step["result"]["target"] for step in json.loads(more["content"])["results"]]
        ids = await tab_ids()
        assert target not in ids and all(tab in ids for tab in newest) and len(ids) == tabs + 1, (target, newest, ids)

        running = await plugin.handle_shell("sleep 30; echo done", timeout=1, stream_id="owner")
        operation_id = json.loads(running["content"])["operation_id"]
        assert json.loads(running["content"])["status"] == "running", running
        cancelled = await plugin.handle_cancel(operation_id, stream_id="owner")
        assert json.loads(cancelled["content"])["status"] == "cancelled", cancelled

        polled = await plugin.handle_poll(operation_id, stream_id="owner")
        assert json.loads(polled["content"])["status"] == "cancelled", polled
        print("selftest ok")
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
        subprocess.run(["docker", "volume", "rm", f"{NAME}-workspace"], capture_output=True)


if __name__ == "__main__":
    asyncio.run(main())
