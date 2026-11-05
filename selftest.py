"""端到端自检：在独立测试容器里跑一遍全部工具，结束后删除容器和数据卷。

需要本机可用的 docker；在 MaiBot 根目录运行：uv run python plugins/linux_computer/selftest.py
"""

from pathlib import Path

import asyncio
import json
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plugin import (BRIEF_REPLY_FIRST, RUNNING_MESSAGE, LinuxComputerPlugin, add_brief_reply_reminder,  # noqa: E402
                    add_link_reminder, add_screenshot_reminder, apply_tool_catalog, recent_tool_images)

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


def check_brief_reply_reminder() -> None:
    """先回一句：有人说话后麦麦没回应就用电脑才提醒，插在最近的工具结果和截图之后；回过话或同一轮回覆就不提醒。"""

    def item(kind: str, item_id: str, **fields: object) -> dict:
        return {"item_type": kind, "meta": {"item_id": item_id, "logical_turn_id": "t" if "Function" in kind else None,
                                            "timestamp": "2026-11-05T12:00:00"}, **fields}

    def text(item_id: str, value: str) -> dict:
        return item("UserMessageItem", item_id, parts=[{"type": "text", "text": value}])

    def call(item_id: str, name: str) -> list:
        return [item("FunctionCallItem", item_id, tool_call={"call_id": item_id, "func_name": name, "args": {},
                                                             "extra_content": None}),
                item("FunctionCallOutputItem", f"{item_id}-out", call_id=item_id, output="ok", success=True, tool_name=name)]

    said = text("self", '<message msg_id="1" time="12:00:00" user="麦麦" is_self_message="true">\n好')
    asked = text("ask", '<message msg_id="2" time="12:00:01" user="小明">\n看一下这篇公告 https://x.notion.site/a')
    shot = item("UserMessageItem", "shot", parts=[{"type": "image", "image_format": "jpeg", "image_base64": ""}])
    tail = text("tail", "<system-reminder>tool_search</system-reminder>")
    # 问了之后没回话就读网页、开桌面：提醒放在最后的结果和截图之后
    items = [said, asked, *call("a", "linux_read_page"), *call("b", "linux_desktop"), shot, tail]
    result = add_brief_reply_reminder(items)
    assert [i["meta"]["item_id"] for i in result][-2:] == ["brief-reply-reminder-shot", "tail"], result
    assert "簡短說一句" in result[-2]["parts"][0]["text"], result[-2]
    # 同一轮 reply 和读网页一起发、先回了再查、回覆之后、没人说话、还没用电脑：都不提醒
    reply, read = call("r", "reply"), call("a", "linux_read_page")
    assert add_brief_reply_reminder([said, asked, reply[0], read[0], reply[1], read[1]]) is None
    assert add_brief_reply_reminder([said, asked, *reply, *read]) is None
    assert add_brief_reply_reminder([*items, *reply]) is None
    assert add_brief_reply_reminder([said, *read]) is None
    assert add_brief_reply_reminder([said, asked]) is None


async def check_replyer_images() -> None:
    """Replyer 截图：Planner 上次说话后工具返回的图片（最新 3 张）放在 reply_reference 前面；模型没视觉就不加。"""

    def item(item_id: str, *parts: dict) -> dict:
        return {"item_type": "UserMessageItem", "meta": {"item_id": item_id, "logical_turn_id": None,
                                                         "timestamp": "2026-11-05T12:00:00"}, "parts": list(parts)}

    def text(value: str) -> dict:
        return {"type": "text", "text": value}

    def image(name: str) -> dict:
        return {"type": "image", "image_format": "jpeg", "image_base64": name}

    def media(item_id: str) -> dict:
        return item(item_id, text(f'<tool_result_media msg_id="tool_result:{item_id}:1" />'), image(item_id))

    said = item("self", text('<message msg_id="1" time="12:00:00" user="麦麦" is_self_message="true">\n我看一下'))
    chat = item("chat", text('<message msg_id="2" time="12:00:01" user="小明">\n你看这张'), image("chat"))
    planner = [media("old"), said, chat, *(media(f"s{n}") for n in range(4))]
    assert [part["image_base64"] for part in recent_tool_images(planner)] == ["s1", "s2", "s3"]
    assert recent_tool_images([*planner, said]) == []

    system = {"item_type": "SystemMessageItem", "meta": {"item_id": "sys", "logical_turn_id": None,
                                                         "timestamp": "2026-11-05T12:00:00"}, "parts": [text("system")]}
    replyer = [system, chat, item("ref", text("公告重點：11 月 19 日主線第六章")), item("final", text("請回覆"))]
    plugin = LinuxComputerPlugin()
    plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"}, "computer": {}})
    tools = [{"type": "function", "function": {"name": "reply"}}]
    await plugin.handle_planner_request(items=[system, *planner], tool_definitions=tools, session_id="s")
    args = {"items": replyer, "session_id": "s", "reply_tool_args": {"reply_reference": "公告重點：11 月 19 日主線第六章"}}
    hooked = await plugin.handle_replyer_request(**args, selected_model_visual=True)
    items = hooked["modified_kwargs"]["items"]
    assert [i["meta"]["item_id"] for i in items] == ["sys", "chat", "replyer-screens-ref", "ref", "final"], items
    assert [part.get("image_base64") for part in items[2]["parts"][1:]] == ["s1", "s2", "s3"], items[2]
    assert await plugin.handle_replyer_request(**args, selected_model_visual=False) is None
    assert await plugin.handle_replyer_request(**{**args, "session_id": "other"}, selected_model_visual=True) is None
    # Replyer 重试仍用同一批；整则回复生成完就清掉，之后不经过 Planner 的回复不会带到旧截图
    assert await plugin.handle_replyer_request(**args, selected_model_visual=True) is not None
    await plugin.handle_reply_done(session_id="s", response="好", reply_tool_args={})
    assert await plugin.handle_replyer_request(**args, selected_model_visual=True) is None


def check_screenshot_reminder() -> None:
    """截图提醒：上次说话后在桌面上操作过才提醒；有截图就指向最近那张，没有就要求先 observe。"""

    def item(kind: str, item_id: str, **fields: object) -> dict:
        return {"item_type": kind, "meta": {"item_id": item_id, "logical_turn_id": "t" if "Function" in kind else None,
                                            "timestamp": "2026-11-04T12:00:00"}, **fields}

    def call(item_id: str, name: str, output: str = "ok", *actions: str) -> list:
        args = {"actions": [{"action": action} for action in actions]} if actions else {}
        return [item("FunctionCallItem", item_id, tool_call={"call_id": item_id, "func_name": name, "args": args,
                                                             "extra_content": None}),
                item("FunctionCallOutputItem", f"{item_id}-out", call_id=item_id, output=output, success=True, tool_name=name)]

    def desktop(item_id: str) -> list:  # click 加 observe；主程序拆出截图时的结果文字和媒体消息
        media = f'ok\n\n<tool_result_media_list>\n  <media msg_id="tool_result:{item_id}:1" type="image" />\n</tool_result_media_list>'
        return [*call(item_id, "linux_desktop", media, "click", "observe"),
                item("UserMessageItem", f"{item_id}-shot", parts=[{"type": "image", "image_format": "jpeg", "image_base64": ""}])]

    def text(result: list, after: str) -> str:
        ids = [i["meta"]["item_id"] for i in result]
        assert ids[ids.index(after) + 1].startswith("screenshot-reminder-"), ids
        return result[ids.index(after) + 1]["parts"][0]["text"]

    said = item("UserMessageItem", "self", parts=[{"type": "text", "text":
                '<message msg_id="1" time="12:00:00" user="麦麦" is_self_message="true">\n好，我去弄'}])
    # c 只读网页不算操作；截图指向最近一次操作后的 b，提醒放在最后一次桌面结果 c 之后
    items = [said, *desktop("a"), *desktop("b"), *call("c", "linux_desktop", "ok", "open", "read"), *call("d", "linux_shell")]
    assert "media_index=tool_result:b:1" in text(add_screenshot_reminder(items), "c-out")
    # 点击后还没截图：要求先 observe
    clicked = [said, *call("e", "linux_desktop", "ok", "click")]
    assert "observe" in text(add_screenshot_reminder(clicked), "e-out")
    # 只读网页、说话之后没操作、操作在说话之前：都不提醒
    assert add_screenshot_reminder([said, *call("c", "linux_desktop", "ok", "open", "read", "elements")]) is None
    # 只是看画面（截图、滚动、移动鼠标）也不算，看公告给感想不必把截图发回聊天
    assert add_screenshot_reminder([said, *call("v", "linux_desktop", "ok", "open", "scroll", "move", "observe")]) is None
    assert add_screenshot_reminder([*items, *call("r", "reply")]) is None
    assert add_screenshot_reminder([*desktop("a"), said, *call("d", "linux_shell")]) is None
    # 同一轮并行调用：先是两个结果，再是截图；提醒要放在整批之后
    shot_b, shell_d = desktop("b"), call("d", "linux_shell")
    batch = [said, shot_b[0], shell_d[0], shot_b[1], shell_d[1], shot_b[2]]
    assert [i["meta"]["item_id"] for i in add_screenshot_reminder(batch)][-2:] == ["b-shot", "screenshot-reminder-b-shot"]


async def check_tool_catalog() -> None:
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

    # 操作返回 running：hook 不经搜索直接载入 deferred 的 linux_poll，同组的 linux_shell、linux_cancel 一起
    plugin = LinuxComputerPlugin()
    plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"}, "computer": {}})

    async def schemas() -> dict:
        return {name: tool(name) for name in ("linux_shell", "linux_poll", "linux_cancel")}

    plugin._plugin_tool_schemas = schemas
    running = item("FunctionCallOutputItem", "r-out", call_id="r", tool_name="linux_read_page", success=True,
                   output=json.dumps({"status": "running", "message": RUNNING_MESSAGE}, ensure_ascii=False))
    hooked = await plugin.handle_planner_request(
        items=[system, running, reminder("linux_shell", "linux_poll", "linux_cancel")], session_id="poll",
        tool_definitions=[tool("reply"), tool("tool_search"), tool("linux_read_page"), tool("linux_desktop")])
    assert names(hooked["modified_kwargs"]["tool_definitions"]) == [
        "reply", "tool_search", "linux_read_page", "linux_desktop", "linux_shell", "linux_poll", "linux_cancel"], hooked
    # 有 reply 的 Planner 请求：系统提示词末尾要求第一次用电脑时同一轮先简短回一句
    assert BRIEF_REPLY_FIRST in hooked["modified_kwargs"]["items"][0]["parts"][-1]["text"], hooked


async def main() -> None:
    check_link_reminder()
    check_brief_reply_reminder()
    await check_replyer_images()
    check_screenshot_reminder()
    await check_tool_catalog()
    plugin = LinuxComputerPlugin()
    plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"},
                              "computer": {"container_name": NAME, "vnc_port": 0, "allowed_chats": ["owner"], "max_tabs": 2}})
    # 子代理要求只加在有 subagent 工具的 Planner 请求上；子代理自己的请求没有 reply / subagent
    system = [{"item_type": "SystemMessageItem", "meta": {"item_id": "s", "logical_turn_id": None,
               "timestamp": "2026-11-02T12:00:00"}, "parts": [{"type": "text", "text": "system"}]}]
    tools = [{"type": "function", "function": {"name": name}} for name in ("reply", "subagent")]
    hooked = await plugin.handle_planner_request(items=system, tool_definitions=tools)
    # 三条规则组合成系统提示词末尾的一段，分流只写在这里
    rules = hooked["modified_kwargs"]["items"][0]["parts"][0]["text"]
    assert rules.count("使用 Linux 電腦的方式") == 1 and rules.count("\n- ") == 3 and "subagent" in rules, rules
    only_reply = await plugin.handle_planner_request(items=system, tool_definitions=tools[:1])
    prompt = only_reply["modified_kwargs"]["items"][0]["parts"][0]["text"]
    assert BRIEF_REPLY_FIRST in prompt and "subagent" not in prompt, prompt
    reader = [{"type": "function", "function": {"name": "linux_read_page"}}]  # 子代理自己的请求：不动
    assert await plugin.handle_planner_request(items=system, tool_definitions=reader) is None
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
        # 参数是扁平的，缺字段在执行时报出是哪个动作、哪个字段
        missing = await plugin.handle_desktop([{"action": "observe"}, {"action": "click", "y": 10}], stream_id="owner")
        assert not missing["success"] and "第 2 個動作缺少或填錯欄位：'x'" in missing["error"], missing
        assert "content_items" not in cdp, "批次里没有 observe 就不截图"
        # read 不填 target 就读目前显示的分页，也就是刚 open 的 example.com，回传原文
        opened = await plugin.handle_desktop([{"action": "read"}], stream_id="owner")
        raw = json.loads(opened["content"])["results"][0]["result"]
        assert raw["title"] == "Example Domain" and "domain" in raw["text"] and raw["links"], raw
        # elements：example.com 唯一的链接在视口下方，只标 where 不给坐标
        listed = await plugin.handle_desktop([{"action": "elements"}], stream_id="owner")
        found = json.loads(listed["content"])["results"][0]["result"]["elements"]
        assert any(e["kind"] == "a" and e.get("where") == "below" and "x" not in e for e in found), found
        # 本地页面（下面的拦截测试也用这个服务）：视口里的链接给坐标，直接 click 会跳过去
        await plugin.handle_shell("mkdir -p /tmp/www && echo '<title>Just a moment...</title>' > /tmp/www/block.html && "
                                  "echo '<a href=\"/block.html\" style=\"font-size:40px\">go</a>' > /tmp/www/link.html && "
                                  "python3 -c \"print('<p>' + 'word ' * 30000)\" > /tmp/www/long.html && "
                                  "echo '<p>公告</p><img src=\"/a.png\" width=\"300\" height=\"200\"><img src=\"/i.png\" "
                                  "width=\"16\" height=\"16\">' > /tmp/www/pic.html && "
                                  "(setsid python3 -m http.server 8765 --bind 127.0.0.1 --directory /tmp/www "
                                  ">/dev/null 2>&1 &) && sleep 1", stream_id="owner")
        local = await plugin.handle_desktop([{"action": "open", "path": "http://127.0.0.1:8765/link.html"},
                                             {"action": "wait", "seconds": 2}, {"action": "elements"}], stream_id="owner")
        steps = json.loads(local["content"])["results"]
        link = next(e for e in steps[2]["result"]["elements"] if e["kind"] == "a")
        await plugin.handle_desktop([{"action": "click", "x": link["x"], "y": link["y"]}, {"action": "wait", "seconds": 2}],
                                    stream_id="owner")
        jumped = await plugin.handle_desktop([{"action": "browser", "method": "Runtime.evaluate", "target": steps[0]["result"]["target"],
                                               "params": {"expression": "location.href"}}], stream_id="owner")
        assert json.loads(jumped["content"])["results"][0]["result"]["result"]["result"]["value"].endswith("/block.html"), link

        async def tab_ids() -> list:
            listed = await plugin.handle_desktop([{"action": "browser", "method": "targets"}], stream_id="owner")
            return [tab["id"] for tab in json.loads(listed["content"])["results"][0]["result"]["targets"]]

        async def tab_count() -> int:
            return len(await tab_ids())

        # linux_read_page 像 WebFetch：原文和连结交给小模型按 prompt 摘取，只回传摘要。这里用假模型代替
        asked = []

        async def fake_model(text: str) -> str:
            asked.append(text)
            return "摘要"

        plugin._ask_model = fake_model
        tabs = await tab_count()
        page = await plugin.handle_read_page(url="https://example.com", prompt="這頁在講什麼", stream_id="owner")  # 旧版 url 参数
        read = json.loads(page["content"])["results"][0]["result"]["pages"][0]
        assert page["success"] and "content_items" not in page, page
        assert read["title"] == "Example Domain" and read["answer"] == "摘要" and "text" not in read, read
        assert "問題：這頁在講什麼" in asked[0] and "domain" in asked[0] and "iana.org" in asked[0], asked
        nop = await plugin.handle_read_page(["https://example.com"], stream_id="owner")
        assert not nop["success"] and "prompt" in nop["error"], nop
        # 摘取失败时退回原文前段
        async def broken_model(text: str) -> str:
            return ""

        plugin._ask_model = broken_model
        fallback = await plugin.handle_read_page(["https://example.com/?fallback"], prompt="x", stream_id="owner")
        fallback_page = json.loads(fallback["content"])["results"][0]["result"]["pages"][0]
        assert fallback_page["answer_error"] and "domain" in fallback_page["text"], fallback_page
        plugin._ask_model = fake_model
        # 同一聊天 15 分钟内同一网址只能读一次：换个问法重读直接拒绝，不进容器，导向 linux_desktop
        guest_calls = []
        real_guest = plugin._guest
        plugin._guest = lambda *args: (guest_calls.append(args), real_guest(*args))[1]
        again = await plugin.handle_read_page(["https://example.com"], prompt="換個問法", stream_id="owner")
        plugin._guest = real_guest
        assert not again["success"] and "linux_desktop" in again["error"] and not guest_calls, (again, guest_calls)
        # 超过 EXTRACT_TEXT_LIMIT 的原文截断，结果里标出还有多少没读到
        long = await plugin.handle_read_page(["http://127.0.0.1:8765/long.html"], prompt="x", stream_id="owner")
        long_page = json.loads(long["content"])["results"][0]["result"]["pages"][0]
        assert "沒有讀到" in long_page["truncated"] and "後面還有" in asked[-1], long_page
        # 文字读取看不到图：标出内文图片张数（16px 的图示不算），引导用桌面截图看
        pic = await plugin.handle_read_page(["http://127.0.0.1:8765/pic.html"], prompt="x", stream_id="owner")
        pic_page = json.loads(pic["content"])["results"][0]["result"]["pages"][0]
        assert pic_page["images"].startswith("這頁有 1 張圖片") and "observe" in pic_page["images"], pic_page
        # 一次并行读多个：打不开的只在自己那一项报错；读过的网址跳过并说明
        mixed = await plugin.handle_read_page(["https://example.com", "https://example.com/?mixed",
                                               "https://nonexistent.invalid"], prompt="x", stream_id="owner")
        result = json.loads(mixed["content"])["results"][0]["result"]
        pages = result["pages"]
        assert mixed["success"] and pages[0]["answer"] == "摘要" and "開啟失敗" in pages[1]["error"], pages
        assert len(pages) == 2 and result["skipped"].startswith("https://example.com 剛才"), result
        bad = await plugin.handle_read_page(["https://nonexistent.invalid"], prompt="x", stream_id="owner")
        assert not bad["success"] and "開啟失敗" in bad["error"] and "linux_desktop" in bad["error"], bad
        # 被拦截或 HTTP 错误：标出原因，附上改用桌面的提示；正常页面不标
        blocked = await plugin.handle_read_page(["http://127.0.0.1:8765/block.html", "http://127.0.0.1:8765/missing",
                                                 "https://example.com/?blocked"], prompt="x", stream_id="owner")
        result = json.loads(blocked["content"])["results"][0]["result"]
        assert [page.get("problem") for page in result["pages"]] == ["像是驗證或阻擋頁面", "HTTP 404", None], result
        assert "linux_desktop" in result["hint"], result
        assert await tab_count() == tabs, "read tabs must be closed"

        # max_tabs=2：再开两个网页后，之前用 open 打开的 example.com 和本地页面两个分页被自动关闭
        more = await plugin.handle_desktop([{"action": "open", "path": "https://example.com/?2"},
                                            {"action": "open", "path": "https://example.com/?3"}], stream_id="owner")
        newest = [step["result"]["target"] for step in json.loads(more["content"])["results"]]
        ids = await tab_ids()
        closed = [target, steps[0]["result"]["target"]]
        assert not set(closed) & set(ids) and all(tab in ids for tab in newest) and len(ids) == tabs, (closed, newest, ids)

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
