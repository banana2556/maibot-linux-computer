"""Linux 沙盒电脑插件：让麦麦的 Planner 直接操作 Docker 容器里的桌面、浏览器和终端。

容器长期保留，装过的软件在停止/重启后仍在；工作目录是独立数据卷，重建容器也不会丢失。
每次工具调用通过 docker exec 运行容器内的 guest.py，超时未完成的操作可用 linux_poll 继续等待。
"""

from pathlib import Path
from typing import Any, Awaitable, Dict, List, Optional
from uuid import uuid4

import asyncio
import json
import re
import time

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool

IMAGE_DIR = Path(__file__).resolve().parent / "image"
GUEST_PATH = "/opt/mai/guest.py"
# 单次调用最多等待的秒数；超出后返回 running，由 linux_poll 继续等待
MAX_WAIT_SECONDS = 300
TOOL_RPC_TIMEOUT_MS = (MAX_WAIT_SECONDS + 60) * 1000
# 保留最近的操作结果，供重复查询
KEPT_OPERATIONS = 50

URL_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
# Planner 里的真实聊天消息以 <message … user="…"> 开头，借此排除内部提示和工具结果；麦麦自己发的不提醒
CHAT_HEADER = re.compile(r'<message (?![^>]*is_self_message="true")[^>]*\buser="')
SELF_HEADER = re.compile(r'<message [^>]*is_self_message="true"')
# 只提醒最近这么多条聊天消息里的链接，更早的视为已经过去的话题
RECENT_IM_MESSAGES = 10
# Planner 系统提示词末尾的「使用 Linux 电脑的方式」：下面三条按设置组合成一段固定内容，见 computer_rules。
# 每条规则只写在一个地方：自己做还是交给子代理只写在这里，工具说明只写工具之间怎么选。
# 分流：子代理截的图只留在子代理自己的历史里，Replyer 看不到，所以给感想、看图要自己做
SUBAGENT_PREFERENCE = (
    "要搜尋、讀好幾個網頁或比對資訊時，交給 subagent 並授權它需要的工具，派出後等它回報，不要自己再查同一件事；"
    "只看一個網頁（查一件事、給感想、看圖）自己做，subagent 截的圖傳不到你的回覆裡。"
)
# 第一次用电脑时就在同一轮简短回一句；漏了由 add_brief_reply_reminder 补提醒
BRIEF_REPLY_FIRST = (
    "用 linux_read_page、linux_desktop 查東西要花一段時間：有人問了之後你還沒回過話，"
    "第一次呼叫時在同一輪也用 reply 簡短說一句你在看什麼（例如「我看一下」），查完再正式回覆，中間不用回報進度。"
)
# Replyer 只收到真实聊天消息和 reply_reference：工具的文字结果要 Planner 写进 reply_reference，
# 截图由插件在 Replyer 请求前直接加进去（Replyer 模型有视觉时）
REPLY_HANDOFF = ("用 reply 時把用電腦讀到的具體內容（名稱、日期、數字、重點）寫進 reply_reference，"
                 "Replyer 看不到工具的文字結果；你截過的畫面會一起交給 Replyer 看。")
REPLYER_SCREENS = 3  # 交给 Replyer 的截图最多几张，取最新的
REPLYER_SCREENS_NOTE = "以下是你剛才在 Linux 電腦上看到的畫面（越後面越新），回覆時可以依畫面內容說；這些是你自己截的圖，不是對方傳來的。"
# 主程序 deferred tools 提示（runtime.build_deferred_tools_reminder）的开头和每行「N. 名称: 描述」
DEFERRED_REMINDER = "以下工具当前未直接暴露给你"
DEFERRED_LINE = re.compile(r"^\d+\. ([^\s:]+)", re.M)
# 算「在桌面上操作过」、回复要附截图的 linux_desktop 动作：会改变画面的点击、输入、按键、拖拽。
# 只是看的 observe、scroll、move 和读网页的 open、read、elements 不算，看公告给感想不必把截图发回聊天
GUI_ACTIONS = {"click", "double_click", "drag", "mouse_down", "mouse_up", "keys", "key_down", "key_up", "type"}
# 操作超时返回 running 时的说明；hook 看到它就直接载入 deferred 的 linux_poll（连同整组 linux 工具）
RUNNING_MESSAGE = "操作仍在執行，用 linux_poll 繼續等待；沒有重複執行。"
# 主程序 tool_search 结果里命中的每行「- 名称（本次新发现）」
SEARCH_HIT = re.compile(r"^- ([^\s（]+)", re.M)
# ponytail: 整个替换主程序 tool_search 的说明和参数；主程序以后给它加参数时要跟着改
TOOL_SEARCH_FUNCTION = {
    "name": "tool_search",
    "description": "載入 deferred tools：用系統提示詞工具目錄裡的類別名稱（例如 linux）、工具名或想做的事搜尋，"
    "命中的工具連同同類別的其他工具一起載入，下一輪起整個對話都能直接呼叫。只負責載入，不執行工具。",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "類別名稱、工具名，或想做的事的動詞關鍵字"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 5, "description": "最多回傳幾個，預設 5"},
    }, "required": ["query"]},
}


def add_link_reminder(items: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """在含有未打开链接的最新聊天消息后插入独立提醒，不提任何工具；没有需要提醒的链接时返回 None。

    出现在任何工具调用参数或结果、或模型自带联网记录里的链接视为已经打开过。提醒的 id 和内容由锚点消息决定，
    每轮请求都相同，不破坏提示词缓存。
    """
    handled = "\n".join(
        json.dumps(item["tool_call"].get("args"), ensure_ascii=False) if item["item_type"] == "FunctionCallItem"
        else str(item.get("output", "")) if item["item_type"] == "FunctionCallOutputItem"
        else json.dumps([item.get("display_summary"), item.get("details")], ensure_ascii=False)
        for item in items
        if item.get("item_type") in ("FunctionCallItem", "FunctionCallOutputItem", "ProviderActivityItem")
    )
    chat = [
        (index, texts) for index, item in enumerate(items) if item.get("item_type") == "UserMessageItem"
        and (texts := [part["text"] for part in item.get("parts", []) if part.get("type") == "text"])
        and CHAT_HEADER.match(texts[0])
    ][-RECENT_IM_MESSAGES:]
    urls: List[str] = []
    anchor = -1
    for index, texts in chat:
        for url in URL_PATTERN.findall("\n".join(texts)):
            url = url.rstrip(".,;:!?'\")]}")
            if url.rstrip("/") not in handled and url not in urls:
                urls.append(url)
                anchor = index
    if not urls:
        return None
    meta = items[anchor]["meta"]
    reminder = {
        "item_type": "UserMessageItem",
        "meta": {"item_id": f"link-reminder-{meta['item_id']}", "logical_turn_id": None, "timestamp": meta["timestamp"]},
        "parts": [{"type": "text", "text": "<system-reminder>\n聊天裡有還沒開啟過的連結：\n"
                   + "\n".join(f"- {url}" for url in urls)
                   + "\n涉及這些連結時，先打開查看實際內容再回應，不要只憑網址或標題猜測。\n</system-reminder>"}],
    }
    return [*items[:anchor + 1], reminder, *items[anchor + 1:]]


def add_brief_reply_reminder(items: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """有人说话后，麦麦还没回应就开始用电脑时，提醒先简短回一句再继续；否则返回 None。

    读网页、操作画面要花一段时间，对方不知道麦麦在做事。先回了「我看一下」再用电脑、或和电脑操作同一轮回覆的不提醒；
    麦麦一说话就不再提醒，中途也不定时回报。提醒插在最近一次工具结果（及其截图）之后。
    """
    spoke = max((index for index, item in enumerate(items) if _is_own_message(item)), default=-1)
    first = next((index for index, item in enumerate(items) if index > spoke and item.get("item_type") == "FunctionCallItem"
                  and item["tool_call"]["func_name"].startswith("linux_")), None)
    outputs = [index for index, item in enumerate(items) if item.get("item_type") == "FunctionCallOutputItem"]
    if first is None or not outputs or not any(_is_chat_message(item) for item in items[spoke + 1:first]):
        return None
    end = _after_tool_results(items, outputs[-1])
    reminder = {
        "item_type": "UserMessageItem",
        "meta": {"item_id": f"brief-reply-reminder-{items[end]['meta']['item_id']}", "logical_turn_id": None,
                 "timestamp": items[end]["meta"]["timestamp"]},
        "parts": [{"type": "text", "text": "<system-reminder>\n對方還沒收到你任何回應，你就開始用電腦了。"
                   "已經能回答就直接正式回覆；還要繼續查，就先用 reply 簡短說一句你在看什麼（例如「我看一下公告」），"
                   "和下一步操作放在同一輪發出，查完再正式回覆，中間不用回報進度。\n</system-reminder>"}],
    }
    return [*items[:end + 1], reminder, *items[end + 1:]]


def computer_rules(subagent: bool, brief: bool, handoff: bool) -> str:
    """开启的规则组合成系统提示词末尾的一段，内容只随设置变化；一条都没开时返回空字符串。"""
    lines = [text for on, text in ((subagent, SUBAGENT_PREFERENCE), (brief, BRIEF_REPLY_FIRST),
                                   (handoff, REPLY_HANDOFF)) if on]
    return "\n\n使用 Linux 電腦的方式：\n" + "\n".join(f"- {line}" for line in lines) if lines else ""


def append_to_system(items: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """在系统提示词末尾追加固定内容，每轮相同，不破坏提示词缓存；没有系统提示词时原样返回。"""
    index = next((index for index, item in enumerate(items) if item.get("item_type") == "SystemMessageItem"), None)
    if index is None:
        return items
    parts = items[index]["parts"]
    system = {**items[index], "parts": [*parts[:-1], {**parts[-1], "text": parts[-1]["text"] + text}]}
    return [*items[:index], system, *items[index + 1:]]


def tool_name(definition: Dict[str, Any]) -> str:
    return str(definition.get("function", {}).get("name") or "")


def namespace(name: str) -> str:
    """工具名第一个 _ 之前的部分就是类别，例如 linux_desktop → linux。"""
    return name.split("_", 1)[0]


def apply_tool_catalog(
    items: List[Dict[str, Any]], tools: List[Dict[str, Any]], loaded: Dict[str, Dict[str, Any]],
    pool: Dict[str, None], schemas: Dict[str, Dict[str, Any]], wanted: Optional[List[str]] = None,
) -> tuple:
    """接管 Planner 的 deferred tools，返回新的 (items, tools)。

    - 主程序每轮列出全部 deferred 工具描述的提示换成系统提示词里的类别目录，只列类别和名称，不放参数。
    - 搜到的工具连同同类别的其他工具一起载入，整个会话保持载入；主程序在搜索记录滑出上下文后会把它折回去，这里补回。
    - tools 数组按首次出现的顺序只追加，不重排；tool_search 没搜到时改为列出可搜的类别。

    loaded：本会话已载入的工具（名称→定义，按载入顺序）；pool：本会话见过的 deferred 工具名。两者都原地更新、只追加。
    schemas：插件工具的完整定义，用来整组载入；主程序自带的工具拿不到定义，只能靠搜索载入。
    wanted：不经搜索直接整组载入的工具，例如操作返回 running 后的 linux_poll。
    """
    reminder = next((index for index, item in enumerate(items) if item.get("item_type") == "UserMessageItem"
                     and any(DEFERRED_REMINDER in part.get("text", "") for part in item.get("parts", []))), None)
    deferred = [] if reminder is None else DEFERRED_LINE.findall(
        "\n".join(part.get("text", "") for part in items[reminder]["parts"]))
    if reminder is not None:
        items = [*items[:reminder], *items[reminder + 1:]]
    current = {tool_name(definition): definition for definition in tools}
    wanted = wanted or []
    discovered = [name for name in [*current, *wanted] if name not in loaded and (name in pool or name in wanted)]
    pool.update(dict.fromkeys(deferred))
    for name in list(loaded):
        if name in current:
            loaded[name] = current[name]  # 原位换成最新定义
        elif name not in deferred:
            del loaded[name]  # 既不在本轮工具里、也不在 deferred 池里：已经下线
    for name, definition in current.items():
        loaded.setdefault(name, definition)
    for name in discovered:
        for other in deferred:
            if namespace(other) == namespace(name) and other in schemas:
                loaded.setdefault(other, schemas[other])

    groups: Dict[str, List[str]] = {}
    for name in pool:
        groups.setdefault(namespace(name), []).append(name)
    if groups:
        items = append_to_system(items, "\n\n<deferred_tools>\n以下是 deferred tools 的類別目錄，只列名稱；"
                                 "要用時先用 tool_search 載入，用類別名稱搜尋會一次載入整組，載入後整個對話都能用。\n"
                                 + "\n".join(f"- {group}：{'、'.join(names)}" for group, names in groups.items())
                                 + "\n</deferred_tools>")

    def search_output(item: Dict[str, Any]) -> Dict[str, Any]:
        output = str(item.get("output", ""))
        if item.get("item_type") != "FunctionCallOutputItem" or item.get("tool_name") != "tool_search":
            return item
        if output.startswith("未找到匹配"):
            text = f"沒有找到符合的工具。可以改用這些類別名稱搜尋：{'、'.join(groups)}。" if groups else "目前沒有可以載入的工具。"
        elif output.startswith("已找到"):
            hits = SEARCH_HIT.findall(output)
            together = [name for name in loaded if name in pool and name not in hits
                        and namespace(name) in {namespace(hit) for hit in hits}]
            text = "已載入：" + "、".join(hits) + (f"\n同類別一起載入：{'、'.join(together)}" if together else "") \
                + "\n下一輪起整個對話都能直接呼叫。"
        else:
            return item
        return {**item, "output": text}

    tools = [{**definition, "function": TOOL_SEARCH_FUNCTION} if name == "tool_search" else definition
             for name, definition in loaded.items()]
    return [search_output(item) for item in items], tools


def add_screenshot_reminder(items: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """麦麦上次说话后在桌面上操作过（点击、输入、按键、拖拽，见 GUI_ACTIONS）时，要求回复附上最新截图；否则返回 None。

    只是看（observe、scroll）和读网页（open、read、elements）不算操作，不必每次看网页都附图。操作之后有截图就给出 reply 的 attach_pic
    要填的 media_index，还没有就要求先 observe 截一张。提醒插在最近一次 linux_desktop 那批工具结果（及其截图）之后。
    """
    spoke = max((index for index, item in enumerate(items) if _is_own_message(item)), default=-1)
    desktop = [(index, item["tool_call"]) for index, item in enumerate(items) if index > spoke
               and item.get("item_type") == "FunctionCallItem" and item["tool_call"]["func_name"] == "linux_desktop"]
    operated = [index for index, call in desktop if any(
        isinstance(action, dict) and action.get("action") in GUI_ACTIONS for action in _call_args(call).get("actions", []))]
    if not operated:
        return None
    later = {call["call_id"] for index, call in desktop if index >= operated[-1]}
    outputs = [index for index, item in enumerate(items)
               if item.get("item_type") == "FunctionCallOutputItem" and item.get("call_id") in later]
    if not outputs:
        return None
    # 主程序把工具返回的图片拆成媒体消息，并在结果文字里附 <tool_result_media_list>；截图是第 1 个媒体
    shots = [index for index in outputs if "<tool_result_media_list>" in str(items[index].get("output", ""))]
    if shots:
        text = (f"你這段時間在桌面上操作過。回覆對方時一定要附上最新的截圖：reply 的 attach_pic 填 "
                f"media_index=tool_result:{items[shots[-1]]['call_id']}:1，讓對方看到實際畫面。")
    else:
        text = "你這段時間在桌面上操作過，但操作後還沒有截圖。回覆對方前先用 linux_desktop 執行 observe 截一張目前的畫面，再用 reply 的 attach_pic 附上。"
    end = _after_tool_results(items, outputs[-1])
    reminder = {
        "item_type": "UserMessageItem",
        "meta": {"item_id": f"screenshot-reminder-{items[end]['meta']['item_id']}", "logical_turn_id": None,
                 "timestamp": items[end]["meta"]["timestamp"]},
        "parts": [{"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}],
    }
    return [*items[:end + 1], reminder, *items[end + 1:]]


def _call_args(call: Dict[str, Any]) -> Dict[str, Any]:
    """工具调用参数；有的模型给的是 JSON 字符串。"""
    args = call.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {}
    return args if isinstance(args, dict) else {}


def _after_tool_results(items: List[Dict[str, Any]], index: int) -> int:
    """跳过紧跟在后面的同批工具结果和拆出来的工具媒体消息：提醒不能插进一批工具结果中间。

    主程序先写完一轮所有工具结果，再追加以 <tool_result_media …/> 开头的媒体消息（视觉模式下带图片）。
    """
    def is_media(item: Dict[str, Any]) -> bool:
        parts = item.get("parts", [])
        return item.get("item_type") == "UserMessageItem" and bool(parts) and (
            str(parts[0].get("text", "")).startswith("<tool_result_media") or any(p.get("type") == "image" for p in parts))

    while index + 1 < len(items) and (items[index + 1].get("item_type") == "FunctionCallOutputItem"
                                      or is_media(items[index + 1])):
        index += 1
    return index


def recent_tool_images(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """麦麦上次说话后工具返回的图片（最新的 REPLYER_SCREENS 张），也就是 Planner 这次回复前看过的画面。

    Planner 看不了图时主程序已把图片换成文字描述，这里就是空的。
    """
    spoke = max((index for index, item in enumerate(items) if _is_own_message(item)), default=-1)
    images = [part for item in items[spoke + 1:] if item.get("item_type") == "UserMessageItem" and item.get("parts")
              and str(item["parts"][0].get("text", "")).startswith("<tool_result_media")
              for part in item["parts"] if part.get("type") == "image" and part.get("image_base64")]
    return images[-REPLYER_SCREENS:]


def add_replyer_images(
    items: List[Dict[str, Any]], images: List[Dict[str, Any]], reference: str,
) -> Optional[List[Dict[str, Any]]]:
    """把截图加进 Replyer 请求，放在 reply_reference 那条前面（找不到就放在最后一条 user 消息前面）；否则返回 None。"""
    users = [index for index, item in enumerate(items) if item.get("item_type") == "UserMessageItem"]
    if not images or not users:
        return None
    anchor = next((index for index in users if reference.strip() and "".join(
        part.get("text", "") for part in items[index].get("parts", [])).strip() == reference.strip()), users[-1])
    meta = items[anchor]["meta"]
    screens = {"item_type": "UserMessageItem",
               "meta": {"item_id": f"replyer-screens-{meta['item_id']}", "logical_turn_id": None,
                        "timestamp": meta["timestamp"]},
               "parts": [{"type": "text", "text": REPLYER_SCREENS_NOTE}, *images]}
    return [*items[:anchor], screens, *items[anchor:]]


def _is_chat_message(item: Dict[str, Any]) -> bool:
    """别人在聊天里说的话。"""
    texts = [part["text"] for part in item.get("parts", []) if part.get("type") == "text"]
    return item.get("item_type") == "UserMessageItem" and bool(texts) and bool(CHAT_HEADER.match(texts[0]))


def _is_own_message(item: Dict[str, Any]) -> bool:
    """麦麦在聊天里说过的话：reply 调用，或历史里带 is_self_message 的消息。"""
    if item.get("item_type") == "FunctionCallItem":
        return item["tool_call"]["func_name"] == "reply"
    texts = [part["text"] for part in item.get("parts", []) if part.get("type") == "text"]
    return item.get("item_type") == "UserMessageItem" and bool(texts) and bool(SELF_HEADER.match(texts[0]))


# 桌面动作参数摊平成一个对象：每个动作要填哪些字段只在 action 的说明里写一次，缺字段时 guest.py 报出来
DESKTOP_ACTION = {"type": "object", "properties": {
    "action": {"type": "string", "enum": [
        "observe", "move", "click", "double_click", "drag", "mouse_down", "mouse_up", "release", "scroll",
        "keys", "key_down", "key_up", "type", "open", "shell", "wait", "wait_window", "wait_process", "browser", "read",
        "elements"],
        "description": "observe：批次結束時回傳截圖；批次裡沒有 observe 就不截圖\n"
        "move、click、double_click：x、y，可加 button\n"
        "drag：從 x、y 拖到 end_x、end_y，可加 seconds、button\n"
        "mouse_down、mouse_up：可加 button；release：放開所有按鍵和滑鼠\n"
        "scroll：delta，正數向上\n"
        "keys：keys，例如 [\"ctrl\", \"l\"]，可加 seconds 持續按住；key_down、key_up：keys\n"
        "type：text\n"
        "open：path 是程式或 http(s) 網址，可加 arguments；網址在新分頁開啟並回傳 target，分頁超過上限會自動關閉最早的\n"
        "shell：command\n"
        "wait：seconds；wait_window：title、seconds；wait_process：pid、seconds\n"
        "browser：method 呼叫 Chromium CDP（targets 列出分頁），可加 target、params\n"
        "read：分頁的完整原文和連結；不填 target 就讀目前顯示的分頁，例如先 open 網址再 read\n"
        "elements：目前分頁可以點的連結、按鈕、輸入框和座標 x、y（和截圖同比例，可直接 click）；"
        "不在畫面裡的只標 where（above、below），scroll 後再取一次；可加 target"},
    "x": {"type": "number"}, "y": {"type": "number"}, "end_x": {"type": "number"}, "end_y": {"type": "number"},
    "button": {"type": "string", "enum": ["left", "right", "middle"]},
    "delta": {"type": "integer"},
    "keys": {"type": "array", "items": {"type": "string"}},
    "seconds": {"type": "number", "minimum": 0},
    "text": {"type": "string"}, "path": {"type": "string"}, "arguments": {"type": "array", "items": {"type": "string"}},
    "command": {"type": "string"}, "title": {"type": "string"}, "pid": {"type": "integer"},
    "method": {"type": "string"}, "target": {"type": "string"}, "params": {"type": "object"},
}, "required": ["action"], "additionalProperties": False}


URLS_SCHEMA = {"type": "array", "items": {"type": "string", "pattern": "^https?://"}, "minItems": 1, "maxItems": 5,
               "description": "要讀取的網址，最多 5 個，同時開啟並行讀取"}
TIMEOUT_SCHEMA = {"type": "number", "minimum": 0, "maximum": MAX_WAIT_SECONDS,
                  "description": f"最多等待秒數，預設 60，上限 {MAX_WAIT_SECONDS}；逾時回傳 running 和 operation_id"}
# linux_read_page 像 Claude Code 的 WebFetch：原文交给小模型按 prompt 摘取，只有摘要进 Planner 上下文
EXTRACT_TEXT_LIMIT = 100000  # 每页交给小模型的正文上限，同 WebFetch
# 同一聊天 15 分钟内（同 WebFetch 的缓存时间）每个网址只给 linux_read_page 读一次，重读不会多出内容；
# Planner 换个问法反复读同一页时，直接把它导向 linux_desktop
REREAD_SECONDS = 900
REREAD_ERROR = ("{urls} 剛才已經用 linux_read_page 讀過，結果在前面，重讀不會多出內容。"
                "還需要更多就改用 linux_desktop：open 網址後 read 讀全文；內容要往下捲才會載入時先 scroll 再 read，或 observe 看畫面。")
EXTRACT_LINKS = 40
EXTRACT_TIMEOUT_MS = 120000
PAGE_TEXT_FALLBACK = 3000  # 摘取失败时退回的原文字数
EXTRACT_PROMPT = """你負責從網頁內容裡找出和問題相關的資訊，結果會交給另一個助理回覆使用者。
問題：{prompt}
網址：{url}
標題：{title}

規則：
- 只寫和問題相關的內容，用繁體中文，越精簡越好，最多 800 字。
- 保留具體的數字、日期、版本和名稱；重要的句子可以短短引用原文。
- 問題需要繼續查的話，從下面的頁面連結裡列出最相關的幾個，格式「標題：網址」，網址照原樣寫。
- 這頁和問題無關或找不到答案，就直接說「這頁沒有相關資訊」，不要編造。

網頁內文：
{text}

頁面連結：
{links}"""


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ComputerSectionConfig(PluginConfigBase):
    """沙盒电脑配置。"""

    __ui_label__ = "电脑"
    __ui_icon__ = "monitor"
    __ui_order__ = 1

    allowed_chats: List[str] = Field(default_factory=list, description="允许使用电脑的聊天流 session_id；留空表示所有聊天都可使用")
    container_name: str = Field(default="maibot-linux-computer", description="Docker 容器名称；已有同名容器会被复用")
    image: str = Field(default="maibot-linux-computer:latest", description="镜像名称；不存在时用插件 image 目录自动构建")
    networking: bool = Field(default=True, description="允许容器联网；删除容器重建后生效")
    memory_mb: int = Field(default=4096, description="容器内存上限（MB）；删除容器重建后生效")
    screen: str = Field(default="1280x800", description="虚拟屏幕分辨率；删除容器重建后生效")
    screenshot_max_edge: int = Field(default=1280, description="截图最长边（像素），0 为原始分辨率；坐标始终按返回的截图计算")
    max_tabs: int = Field(default=5, description="麦麦用 open 打开的网页标签页最多保留几个，超出时自动关闭最早的；0 为不限制")
    brief_reply_first: bool = Field(
        default=True, description="有人说话后麦麦要用电脑（读网页、操作桌面）时，要求先简短回一句（例如「我看一下」），"
        "查完再正式回复；中途不定时回报进度")
    reply_handoff: bool = Field(
        default=True, description="要求 Planner 用 reply 时把读到的内容写进 reply_reference（Replyer 看不到工具结果）；"
        "Replyer 模型有视觉时，把 Planner 上次说话后截的画面（最新 3 张）一起交给 Replyer")
    desktop_reply_screenshot: bool = Field(
        default=True, description="上次说话后在桌面上操作过（点击、输入、按键、拖拽；只是截图、滚动看不算）时，要求麦麦回复时附上最新的截图")
    extract_task: str = Field(
        default="utils", description="linux_read_page 依 prompt 摘取网页重点用的模型任务（model_config 里的任务名），建议用快的小模型")
    prefer_subagent: bool = Field(
        default=True, description="在 Planner 提示词里要求搜索、读好几个网页、比对信息时交给子代理，只看一个网页（查一件事、"
        "给感想、看图）自己做；主程序开启 planner_subagent 时才生效")
    tool_catalog: bool = Field(
        default=True, description="接管 Planner 的 deferred tools：系统提示词只放类别目录，搜到的工具整组载入并在会话内保持，"
        "tools 顺序固定；对所有插件和内置工具生效")
    vnc_port: int = Field(default=6080, description="noVNC 映射到宿主机 127.0.0.1 的端口，0 为不映射；删除容器重建后生效")
    docker_command: str = Field(default="docker", description="docker 可执行程序")


class LinuxComputerConfig(PluginConfigBase):
    """Linux 沙盒电脑插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    computer: ComputerSectionConfig = Field(default_factory=ComputerSectionConfig)


class LinuxComputerPlugin(MaiBotPlugin):
    """Linux 沙盒电脑插件。"""

    config_model = LinuxComputerConfig

    def __init__(self) -> None:
        super().__init__()
        self._ready = False
        self._ensure_lock = asyncio.Lock()
        self._operations: Dict[str, asyncio.Task] = {}
        # (stream_id, url) → 用 linux_read_page 读成功的时间，见 REREAD_SECONDS
        self._read_urls: Dict[tuple, float] = {}
        self._warmup: Optional[asyncio.Task] = None
        # session_id → (已载入的工具, 见过的 deferred 工具)，见 apply_tool_catalog
        self._tool_sessions: Dict[str, tuple] = {}
        # session_id → Planner 最近一次请求里、上次说话后看过的截图，Replyer 请求时加进去
        self._screens: Dict[str, List[Dict[str, Any]]] = {}

    async def on_load(self) -> None:
        """启用时预先构建镜像并启动容器，首次构建需要几分钟。"""
        if self.config.plugin.enabled:
            self._warmup = asyncio.create_task(self._ensure())
            self._warmup.add_done_callback(self._log_warmup)

    async def on_unload(self) -> None:
        """容器独立于麦麦常驻，卸载插件不停止容器。"""

    def _log_warmup(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.ctx.logger.error(f"Linux 电脑预热失败，首次使用时会重试：{task.exception()}")

    # ===== 工具 =====

    @Tool(
        "linux_shell",
        description="在你的 Linux 沙盒電腦（Debian 容器，root）裡執行 bash 指令，回傳 stdout/stderr/exit_code，"
        "工作目錄 /workspace。可用 apt-get、pip、curl 等安裝軟體，裝過的軟體會保留。"
        "長時間指令逾時回傳 running，用 linux_poll 等待結果，不要重複執行。"
        "用於：執行指令、跑程式或腳本、安裝軟體、下載檔案、查看檔案、終端機（terminal, command, install, download）。",
        parameters={"type": "object", "properties": {"command": {"type": "string"}, "timeout": TIMEOUT_SCHEMA},
                    "required": ["command"], "additionalProperties": False},
        visibility="deferred",
        timeout_ms=TOOL_RPC_TIMEOUT_MS,
    )
    async def handle_shell(self, command: str, timeout: float = 60, stream_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """执行 shell，不截图。"""
        del kwargs
        return await self._safe(self._submit([{"action": "shell", "command": command}], timeout, stream_id, screenshot=False))

    @Tool(
        "linux_desktop",
        description="在 Linux 沙盒電腦的圖形桌面上依序執行 actions。需要看畫面或圖片、對網頁給感想或評論（open 後 read 讀全文、"
        "observe 看畫面）、點擊、輸入、登入，或 linux_read_page 讀取失敗、被阻擋時用它；只想查網頁裡的某件事用 linux_read_page。"
        "批次裡有 observe 才回傳截圖；看不到圖時用 elements 取得可以點的元素和座標。"
        "逾時回傳 running，用 linux_poll 等待，不要重複操作。",
        parameters={"type": "object", "properties": {
            "actions": {"type": "array", "items": DESKTOP_ACTION, "minItems": 1},
            "timeout": TIMEOUT_SCHEMA,
        }, "required": ["actions"], "additionalProperties": False},
        visibility="visible",
        timeout_ms=TOOL_RPC_TIMEOUT_MS,
    )
    async def handle_desktop(
        self, actions: List[Dict[str, Any]], timeout: float = 60, stream_id: str = "", **kwargs: Any,
    ) -> Dict[str, Any]:
        """执行桌面动作；批次里有 observe 才截图。"""
        del kwargs
        screenshot = any(action.get("action") == "observe" for action in actions)
        return await self._safe(self._submit(actions, timeout, stream_id, screenshot=screenshot))

    @Tool(
        "linux_read_page",
        description="用你 Linux 電腦裡的 Chromium 開啟網址（會執行 JavaScript，帶著這台電腦上的登入狀態），"
        "讀完由小模型依 prompt 擷取相關內容，只回傳摘要和相關連結，不回傳原文。"
        "要看幾個網址就一次全放進 urls 並行讀取，不要一輪讀一個。"
        "搜尋時讀 https://html.duckduckgo.com/html/?q=關鍵字（Google 會攔截自動存取），prompt 寫要找什麼，"
        "例如「列出 FFXIV 7.4 官方更新說明的連結」；摘要不夠再挑 2～3 個結果一次讀，資訊足夠回答就停。"
        "同一網址只能讀一次；要對網頁給感想或評論、看圖、更多內容、完整原文、點擊、登入，或讀取失敗、被阻擋時改用 linux_desktop。",
        parameters={"type": "object", "properties": {
            "urls": URLS_SCHEMA,
            "prompt": {"type": "string", "description": "想從這些網頁知道什麼，例如「7.4 版有哪些新內容」；只會回傳和它相關的部分"},
            "timeout": TIMEOUT_SCHEMA,
        }, "required": ["urls", "prompt"], "additionalProperties": False},
        visibility="visible",
        timeout_ms=TOOL_RPC_TIMEOUT_MS,
    )
    async def handle_read_page(
        self, urls: Optional[List[str]] = None, prompt: str = "", timeout: float = 60, stream_id: str = "", **kwargs: Any,
    ) -> Dict[str, Any]:
        """并行读取网页，交给小模型按 prompt 摘取；兼容旧版单个 url 参数。"""
        if not urls and kwargs.get("url"):
            urls = [kwargs["url"]]
        if not urls:
            return {"success": False, "error": "需要至少一個網址（urls）。"}
        if not prompt.strip():
            return {"success": False, "error": "需要 prompt：寫想從這些網頁知道什麼。要完整原文請用 linux_desktop 的 open 加 read。"}
        urls = list(dict.fromkeys(urls[:5]))
        now = time.monotonic()
        self._read_urls = {key: at for key, at in self._read_urls.items() if now - at < REREAD_SECONDS}
        repeated = [url for url in urls if (stream_id, url) in self._read_urls]
        fresh = [url for url in urls if url not in repeated]
        if not fresh:
            return {"success": False, "error": REREAD_ERROR.format(urls="、".join(repeated))}
        action = {"action": "read", "urls": fresh, "text_limit": EXTRACT_TEXT_LIMIT, "link_limit": EXTRACT_LINKS}
        return await self._safe(self._submit([action], timeout, stream_id, screenshot=False,
                                             post=lambda result: self._extract(result, prompt, fresh, repeated, stream_id)))

    async def _extract(self, result: Dict[str, Any], prompt: str, urls: List[str], repeated: List[str],
                       stream_id: str) -> Dict[str, Any]:
        """记下读成功的网址；每页原文分别交给小模型按 prompt 摘取，读取失败或被拦截的页面原样保留原因。"""
        if not result.get("success"):
            return result
        step = result["results"][0]
        for url, page in zip(urls, step["result"]["pages"]):
            if "error" not in page and "problem" not in page:
                self._read_urls[(stream_id, url)] = time.monotonic()
        pages = await asyncio.gather(*(self._extract_page(page, prompt) for page in step["result"]["pages"]))
        read = {**step["result"], "pages": list(pages)}
        if repeated:
            read["skipped"] = REREAD_ERROR.format(urls="、".join(repeated))
        return {**result, "results": [{**step, "result": read}]}

    async def _extract_page(self, page: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        brief = {key: page[key] for key in ("url", "title", "note", "truncated", "error", "problem")
                 if key in page}
        if page.get("images"):
            brief["images"] = f"這頁有 {page['images']} 張圖片，摘要不包含圖片內容；要看圖用 linux_desktop open 後 observe。"
        if "error" in page or "problem" in page:
            return brief
        links = "\n".join(f"- {link['text']}：{link['url']}" for link in page.get("links", [])) or "（沒有）"
        answer = await self._ask_model(EXTRACT_PROMPT.format(
            prompt=prompt, url=page["url"], title=page["title"], text=page["text"], links=links))
        if answer:
            return {**brief, "answer": answer}
        # 摘取失败时退回原文前段，至少不让这次读取白费
        return {**brief, "answer_error": "擷取失敗，以下是原文前段", "text": page["text"][:PAGE_TEXT_FALLBACK]}

    async def _ask_model(self, text: str) -> str:
        """用 extract_task 指定的模型回答；失败时返回空字符串。"""
        try:
            reply = await self.ctx.call_capability("llm.generate", timeout_ms=EXTRACT_TIMEOUT_MS, prompt=text,
                                                   model="", task_name=self.config.computer.extract_task, max_tokens=1200)
        except Exception as exc:  # 能力调用失败不影响读取结果
            self.ctx.logger.warning(f"网页摘取失败：{exc}")
            return ""
        if isinstance(reply, dict) and reply.get("success") and str(reply.get("response") or "").strip():
            return str(reply["response"]).strip()
        self.ctx.logger.warning(f"网页摘取没有结果：{reply.get('error') if isinstance(reply, dict) else reply}")
        return ""

    @Tool(
        "linux_poll",
        description="繼續等待 linux_shell / linux_desktop / linux_read_page 回傳 running 的操作，不會重複執行。"
        "用於：等待、查看進度、拿結果（wait, status, progress, result）。",
        parameters={"type": "object", "properties": {"operation_id": {"type": "string"}, "timeout": TIMEOUT_SCHEMA},
                    "required": ["operation_id"], "additionalProperties": False},
        visibility="deferred",
        timeout_ms=TOOL_RPC_TIMEOUT_MS,
    )
    async def handle_poll(self, operation_id: str, timeout: float = 60, stream_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """查询操作结果。"""
        del kwargs
        return await self._safe(self._poll(operation_id, timeout, stream_id))

    @Tool(
        "linux_cancel",
        description="終止仍在執行的 Linux 電腦操作（連同它啟動的指令），已完成的動作不會復原。"
        "用於：停止、中止、取消、強制結束卡住的指令或操作（stop, kill, abort, cancel）。",
        parameters={"type": "object", "properties": {"operation_id": {"type": "string"}},
                    "required": ["operation_id"], "additionalProperties": False},
        visibility="deferred",
        timeout_ms=TOOL_RPC_TIMEOUT_MS,
    )
    async def handle_cancel(self, operation_id: str, stream_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """取消操作。"""
        del kwargs
        return await self._safe(self._cancel(operation_id, stream_id))

    # ===== Hook =====

    @HookHandler("maisaka.replyer.before_model_request",
                 description="Replyer 模型有视觉时，把 Planner 上次说话后在电脑上截的画面一起交给 Replyer")
    async def handle_replyer_request(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Replyer 只收到真实聊天消息，看不到工具返回的截图；这里补上 Planner 回复前看过的最新几张。"""
        if not self.config.computer.reply_handoff or not kwargs.get("selected_model_visual"):
            return None
        reference = str((kwargs.get("reply_tool_args") or {}).get("reply_reference") or "")
        items = add_replyer_images(kwargs.get("items") or [], self._screens.get(str(kwargs.get("session_id") or ""), []),
                                   reference)
        return None if items is None else {"action": "continue", "modified_kwargs": {"items": items}}

    @HookHandler("maisaka.reply.before_post_process",
                 description="一则回复生成完就清掉交给 Replyer 的截图，不会带到之后不经过 Planner 的回复")
    async def handle_reply_done(self, **kwargs: Any) -> None:
        """Replyer 重试时还要用同一批截图，所以等整则回复生成完（每则回复只触发一次）才清掉。"""
        self._screens.pop(str(kwargs.get("session_id") or ""), None)

    @HookHandler("maisaka.planner.before_request",
                 description="提醒 Planner 打开聊天里的链接、用电脑前先简短回一句，可要求查资料交给子代理，接管 deferred tools")
    async def handle_planner_request(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """提醒都插在对应内容之后，不放进请求末尾的工具提示里。

        子代理的请求也经过这个 hook：它没有 reply、subagent 和 tool_search，所以不会收到先回一句的提醒、子代理要求，
        工具也不被接管。
        """
        original = kwargs.get("items") or []
        definitions = kwargs.get("tool_definitions") or []
        tools = {tool_name(definition) for definition in definitions}
        items = add_link_reminder(original) or original
        cfg, reply = self.config.computer, "reply" in tools
        rules = computer_rules(cfg.prefer_subagent and "subagent" in tools, reply and cfg.brief_reply_first,
                               reply and cfg.reply_handoff)
        if rules:
            items = append_to_system(items, rules)
        if reply:
            if cfg.brief_reply_first:
                items = add_brief_reply_reminder(items) or items
            if cfg.reply_handoff:
                self._screens[str(kwargs.get("session_id") or "")] = recent_tool_images(original)
            if cfg.desktop_reply_screenshot:
                items = add_screenshot_reminder(items) or items
        result = definitions
        if self.config.computer.tool_catalog and "tool_search" in tools:
            loaded, pool = self._tool_sessions.setdefault(str(kwargs.get("session_id") or ""), ({}, {}))
            wanted = ["linux_poll"] if any(item.get("item_type") == "FunctionCallOutputItem"
                                           and RUNNING_MESSAGE in str(item.get("output", "")) for item in items) else []
            discovering = any(name not in loaded and (name in pool or name in wanted) for name in [*tools, *wanted])
            items, result = apply_tool_catalog(items, definitions, loaded, pool,
                                               await self._plugin_tool_schemas() if discovering else {}, wanted)
        if items is original and result is definitions:
            return None
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": items, "tool_definitions": result}}

    async def _plugin_tool_schemas(self) -> Dict[str, Dict[str, Any]]:
        """所有插件工具的完整定义，用于整组载入；拿不到时只载入搜到的那个。"""
        try:
            tools = await self.ctx.tool.get_definitions()
        except Exception as exc:  # 能力调用失败不影响本轮请求
            self.ctx.logger.warning(f"读取插件工具定义失败，本次不整组载入：{exc}")
            return {}
        return {
            tool["name"]: {"type": "function", "function": {
                "name": tool["name"], "description": tool["definition"].get("description", ""),
                "parameters": tool["definition"].get("parameters_schema")
                or {"type": "object", "properties": {}, "required": []},
            }} for tool in tools or [] if isinstance(tool, dict) and tool.get("name")
        }

    # ===== 操作 =====

    async def _safe(self, work: Awaitable[Dict[str, Any]]) -> Dict[str, Any]:
        """权限、参数和 docker 错误作为失败结果交给模型，不当作插件异常。"""
        try:
            return await work
        except (OSError, RuntimeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    async def _poll(self, operation_id: str, timeout: float, stream_id: str) -> Dict[str, Any]:
        self._check_chat(stream_id)
        return await self._collect(operation_id, timeout)

    async def _cancel(self, operation_id: str, stream_id: str) -> Dict[str, Any]:
        self._check_chat(stream_id)
        self._get_operation(operation_id)
        await self._guest("cancel", operation_id)
        return await self._collect(operation_id, 10)

    def _check_chat(self, stream_id: str) -> None:
        allowed = self.config.computer.allowed_chats
        if allowed and stream_id not in allowed:
            raise PermissionError("目前的聊天沒有使用 Linux 電腦的權限。")

    def _get_operation(self, operation_id: str) -> asyncio.Task:
        if operation_id not in self._operations:
            raise ValueError(f"未知或已過期的操作：{operation_id}")
        return self._operations[operation_id]

    async def _submit(
        self, actions: List[Dict[str, Any]], timeout: float, stream_id: str, *, screenshot: bool,
        post: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """post 在同一个操作里处理执行器结果（例如摘取网页），超时后用 linux_poll 拿到的也是处理过的结果。"""
        self._check_chat(stream_id)
        await self._ensure()
        operation_id = uuid4().hex
        request = {"actions": actions, "screenshot": screenshot, "max_edge": self.config.computer.screenshot_max_edge,
                   "max_tabs": self.config.computer.max_tabs}

        async def work() -> Dict[str, Any]:
            result = await self._guest("run", operation_id, request)
            return await post(result) if post else result

        self._operations[operation_id] = asyncio.create_task(work())
        # ponytail: 只保留最近的操作，插件重启后旧操作无法再查询；容器里的进程不受影响
        for stale in list(self._operations)[:-KEPT_OPERATIONS]:
            if self._operations[stale].done():
                del self._operations[stale]
        return await self._collect(operation_id, timeout)

    async def _collect(self, operation_id: str, timeout: float) -> Dict[str, Any]:
        task = self._get_operation(operation_id)
        done, _ = await asyncio.wait({task}, timeout=min(max(timeout, 0), MAX_WAIT_SECONDS))
        if not done:
            return {"success": True, "content": json.dumps(
                {"status": "running", "operation_id": operation_id,
                 "message": RUNNING_MESSAGE}, ensure_ascii=False)}
        result = dict(task.result())
        result["operation_id"] = operation_id
        image = result.pop("image", None)
        mime_type = result.pop("mime_type", "")
        reply: Dict[str, Any] = {"success": result["success"], "content": json.dumps(result, ensure_ascii=False)}
        if not result["success"]:
            reply["error"] = "\n".join(result[key] for key in ("error", "capture_error") if key in result)
        if image:
            reply["content_items"] = [{"content_type": "image", "data": image, "mime_type": mime_type}]
        return reply

    async def _guest(self, command: str, operation_id: str, request: Dict[str, Any] | None = None) -> Dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            self.config.computer.docker_command, "exec", "-i", self.config.computer.container_name,
            "python3", GUEST_PATH, command, operation_id,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(json.dumps(request).encode() if request is not None else b"")
        if stdout.strip():
            return json.loads(stdout)
        # 容器被删除或停止时下次调用重新准备
        self._ready = False
        return {"success": False, "status": "failed",
                "error": f"執行器沒有回傳結果（exit={process.returncode}）：{stderr.decode(errors='replace').strip()}"}

    # ===== 容器 =====

    async def _docker(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            self.config.computer.docker_command, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        output = (await process.communicate())[0].decode(errors="replace")
        if process.returncode != 0:
            raise RuntimeError(f"docker {args[0]} 失敗：{output.strip()[-2000:]}")
        return output.strip()

    async def _ensure(self) -> None:
        """容器不存在时构建镜像并创建，已停止时启动；执行器脚本每次都同步为插件内的版本。"""
        async with self._ensure_lock:
            if self._ready:
                return
            cfg = self.config.computer
            if not re.fullmatch(r"\d+x\d+", cfg.screen):
                raise ValueError("screen 必須是 1280x800 這種格式")
            try:
                running = await self._docker("inspect", "-f", "{{.State.Running}}", cfg.container_name)
            except RuntimeError:
                running = None
            if running is None:
                try:
                    await self._docker("image", "inspect", cfg.image)
                except RuntimeError:
                    await self._docker("build", "-t", cfg.image, str(IMAGE_DIR))
                args = ["run", "-d", "--init", "--name", cfg.container_name, "--restart", "unless-stopped",
                        "--shm-size", "1g", "--memory", f"{cfg.memory_mb}m", "-e", f"SCREEN={cfg.screen}",
                        "-v", f"{cfg.container_name}-workspace:/workspace"]
                if not cfg.networking:
                    args += ["--network", "none"]
                if cfg.vnc_port:
                    args += ["-p", f"127.0.0.1:{cfg.vnc_port}:6080"]
                await self._docker(*args, cfg.image)
            elif running != "true":
                await self._docker("start", cfg.container_name)
            await self._docker("cp", str(IMAGE_DIR / "guest.py"), f"{cfg.container_name}:{GUEST_PATH}")
            self._ready = True


def create_plugin() -> LinuxComputerPlugin:
    """创建 Linux 沙盒电脑插件实例。"""

    return LinuxComputerPlugin()
