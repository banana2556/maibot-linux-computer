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
from plugin import LinuxComputerPlugin, add_link_reminder, add_progress_reminder  # noqa: E402

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


async def main() -> None:
    check_link_reminder()
    check_progress_reminder()
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
        assert not bad["success"] and "開啟失敗" in bad["error"], bad
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
