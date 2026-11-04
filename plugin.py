"""Linux 沙盒电脑插件：让麦麦的 Planner 直接操作 Docker 容器里的桌面、浏览器和终端。

容器长期保留，装过的软件在停止/重启后仍在；工作目录是独立数据卷，重建容器也不会丢失。
每次工具调用通过 docker exec 运行容器内的 guest.py，超时未完成的操作可用 linux_poll 继续等待。
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Optional
from uuid import uuid4

import asyncio
import json
import re

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
SUBAGENT_PREFERENCE = (
    "\n\n查資料（搜尋、讀網頁、比對資訊）時，盡量交給 subagent 去做，並授權它需要的工具；"
    "派出時可以在同一輪用一句話告訴對方你在查，拿到結論後再回覆。"
    "只有開啟聊天裡的單一連結這種一步就能完成的事，才自己直接做。"
)
# 主程序 deferred tools 提示（runtime.build_deferred_tools_reminder）的开头和每行「N. 名称: 描述」
DEFERRED_REMINDER = "以下工具当前未直接暴露给你"
DEFERRED_LINE = re.compile(r"^\d+\. ([^\s:]+)", re.M)
# guest.py 的 READ_FALLBACK：linux_read_page 读取失败或被拦截时结果里带这句，看到就直接载入 linux_desktop
READ_FALLBACK_MARK = "改用 linux_desktop 開啟這些網址"
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


def add_progress_reminder(
    items: List[Dict[str, Any]], seconds: int, now: Optional[datetime] = None,
) -> Optional[List[Dict[str, Any]]]:
    """麦麦上次在聊天里说话之后，已经连续操作电脑超过 seconds 秒时，提醒先说一下进展再继续；否则返回 None。

    提醒插在最近一次工具结果（及其截图）之后。说话后计时重新开始，所以长任务里大约每 seconds 秒提醒一次。
    """
    if seconds <= 0:
        return None
    spoke = max((index for index, item in enumerate(items) if _is_own_message(item)), default=-1)
    calls = [item for item in items[spoke + 1:] if item.get("item_type") == "FunctionCallItem"
             and item["tool_call"]["func_name"].startswith("linux_")]
    if not calls:
        return None
    started = datetime.fromisoformat(calls[0]["meta"]["timestamp"])
    if ((now or datetime.now(started.tzinfo)) - started).total_seconds() < seconds:
        return None
    end = _after_tool_results(items, max(
        index for index, item in enumerate(items) if item.get("item_type") == "FunctionCallOutputItem"))
    reminder = {
        "item_type": "UserMessageItem",
        "meta": {"item_id": f"progress-reminder-{items[end]['meta']['item_id']}", "logical_turn_id": None,
                 "timestamp": items[end]["meta"]["timestamp"]},
        "parts": [{"type": "text", "text": f"<system-reminder>\n你已經在電腦上連續操作了 {len(calls)} 步，"
                   "這段時間對方沒收到你的任何訊息，不知道進度。用一兩句話告訴對方你在做什麼、做到哪一步，"
                   "畫面有參考價值時可以附上截圖；這句話和下一步操作放在同一輪一起發出，不要為此單獨佔一輪。"
                   "\n</system-reminder>"}],
    }
    return [*items[:end + 1], reminder, *items[end + 1:]]


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
    wanted：不经搜索直接整组载入的工具，例如网页读取失败后的 linux_desktop。
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
    """麦麦上次说话后用过 linux_desktop 时，要求回复附上最新截图；没有带截图的桌面操作时返回 None。

    提醒插在最近一次带截图的 linux_desktop 那批工具结果（及其截图）之后，给出 reply 的 attach_pic 要填的 media_index。
    """
    spoke = max((index for index, item in enumerate(items) if _is_own_message(item)), default=-1)
    calls = {item["tool_call"]["call_id"] for item in items[spoke + 1:] if item.get("item_type") == "FunctionCallItem"
             and item["tool_call"]["func_name"] == "linux_desktop"}
    # 主程序把工具返回的图片拆成媒体消息，并在结果文字里附 <tool_result_media_list>；截图是第 1 个媒体
    shots = [index for index, item in enumerate(items) if item.get("item_type") == "FunctionCallOutputItem"
             and item.get("call_id") in calls and "<tool_result_media_list>" in str(item.get("output", ""))]
    if not shots:
        return None
    call_id = items[shots[-1]]["call_id"]
    end = _after_tool_results(items, shots[-1])
    reminder = {
        "item_type": "UserMessageItem",
        "meta": {"item_id": f"screenshot-reminder-{items[end]['meta']['item_id']}", "logical_turn_id": None,
                 "timestamp": items[end]["meta"]["timestamp"]},
        "parts": [{"type": "text", "text": "<system-reminder>\n你這段時間在桌面上操作過。回覆對方時一定要附上最新的截圖："
                   f"reply 的 attach_pic 填 media_index=tool_result:{call_id}:1，讓對方看到實際畫面。\n</system-reminder>"}],
    }
    return [*items[:end + 1], reminder, *items[end + 1:]]


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


def _is_own_message(item: Dict[str, Any]) -> bool:
    """麦麦在聊天里说过的话：reply 调用，或历史里带 is_self_message 的消息。"""
    if item.get("item_type") == "FunctionCallItem":
        return item["tool_call"]["func_name"] == "reply"
    texts = [part["text"] for part in item.get("parts", []) if part.get("type") == "text"]
    return item.get("item_type") == "UserMessageItem" and bool(texts) and bool(SELF_HEADER.match(texts[0]))


def action_schemas() -> List[Dict[str, Any]]:
    number = {"type": "number", "minimum": 0}
    seconds = {"type": "number", "minimum": 0}
    text = {"type": "string"}
    keys = {"type": "array", "items": text, "minItems": 1}
    button = {"type": "string", "enum": ["left", "right", "middle"]}
    definitions = [
        ("observe", {}, []),
        ("release", {}, []),
        ("move", {"x": number, "y": number}, ["x", "y"]),
        ("click", {"x": number, "y": number, "button": button}, ["x", "y"]),
        ("double_click", {"x": number, "y": number, "button": button}, ["x", "y"]),
        ("drag", {"x": number, "y": number, "end_x": number, "end_y": number,
                  "seconds": seconds, "button": button}, ["x", "y", "end_x", "end_y"]),
        ("mouse_down", {"button": button}, []), ("mouse_up", {"button": button}, []),
        ("scroll", {"delta": {"type": "integer"}}, ["delta"]),
        ("keys", {"keys": keys, "seconds": seconds}, ["keys"]),
        ("key_down", {"keys": keys}, ["keys"]), ("key_up", {"keys": keys}, ["keys"]),
        ("type", {"text": text}, ["text"]),
        ("open", {"path": text, "arguments": {"type": "array", "items": text}}, ["path"]),
        ("shell", {"command": text}, ["command"]),
        ("wait", {"seconds": seconds}, ["seconds"]),
        ("wait_window", {"title": text, "seconds": seconds}, ["title", "seconds"]),
        ("wait_process", {"pid": {"type": "integer", "minimum": 1}, "seconds": seconds}, ["pid", "seconds"]),
        ("browser", {"method": text, "target": text, "params": {"type": "object"}}, ["method"]),
        ("read", {"urls": URLS_SCHEMA, "target": text, "seconds": seconds}, []),
    ]
    return [{"type": "object", "properties": {"action": {"type": "string", "const": name}, **fields},
             "required": ["action", *required], "additionalProperties": False} for name, fields, required in definitions]


URLS_SCHEMA = {"type": "array", "items": {"type": "string", "pattern": "^https?://"}, "minItems": 1, "maxItems": 5,
               "description": "要讀取的網址，最多 5 個，同時開啟並行讀取"}
TIMEOUT_SCHEMA = {"type": "number", "minimum": 0, "maximum": MAX_WAIT_SECONDS,
                  "description": f"最多等待秒數，預設 60，上限 {MAX_WAIT_SECONDS}；逾時回傳 running 和 operation_id"}


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
    progress_reminder_seconds: int = Field(
        default=60, description="连续操作电脑超过这么多秒、期间没在聊天里说过话时，提醒麦麦先说一下进展；0 为关闭")
    desktop_reply_screenshot: bool = Field(
        default=True, description="上次说话后用过 linux_desktop 时，要求麦麦回复时附上最新的截图")
    prefer_subagent: bool = Field(
        default=True, description="在 Planner 提示词里要求查资料时尽量交给子代理；主程序开启 planner_subagent 时才生效")
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
        self._warmup: Optional[asyncio.Task] = None
        # session_id → (已载入的工具, 见过的 deferred 工具)，见 apply_tool_catalog
        self._tool_sessions: Dict[str, tuple] = {}

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
        visibility="visible",
        timeout_ms=TOOL_RPC_TIMEOUT_MS,
    )
    async def handle_shell(self, command: str, timeout: float = 60, stream_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """执行 shell，不截图。"""
        del kwargs
        return await self._safe(self._submit([{"action": "shell", "command": command}], timeout, stream_id, screenshot=False))

    @Tool(
        "linux_desktop",
        description="在 Linux 沙盒電腦的圖形桌面上批次操作並回傳截圖。先 observe，座標依最新截圖。"
        "動作：observe；move/click/double_click(x,y,button)；drag(x,y,end_x,end_y,seconds,button)；mouse_down/mouse_up(button)；"
        "scroll(delta，正數向上)；keys/key_down/key_up(keys 陣列，如 ctrl、enter、a，keys 可加 seconds 持續按住)；type(text)；"
        "open(path,arguments，程式或 http(s) 網址；網址在 Chromium 新分頁開啟並回傳 target，"
        "你開的分頁超過上限會自動關閉最早的，不用自己關)；shell(command)；wait(seconds)；"
        "wait_window(title,seconds)、wait_process(pid,seconds)；browser(method,target,params) 呼叫 Chromium CDP，method=targets 列出頁面；"
        "read(urls 或 target,seconds) 讀取網頁內文。"
        "只看網頁內容用 linux_read_page，純命令列任務用 linux_shell。逾時回傳 running，用 linux_poll 查詢，不要重複輸入。"
        "用於：看畫面、截圖、點擊、輸入文字、拖曳、開啟程式或網頁、登入網站、操作圖形介面（screenshot, click, type, GUI, desktop, browser）。",
        parameters={"type": "object", "properties": {
            "actions": {"type": "array", "items": {"oneOf": action_schemas()}, "minItems": 1},
            "timeout": TIMEOUT_SCHEMA,
        }, "required": ["actions"], "additionalProperties": False},
        visibility="deferred",
        timeout_ms=TOOL_RPC_TIMEOUT_MS,
    )
    async def handle_desktop(
        self, actions: List[Dict[str, Any]], timeout: float = 60, stream_id: str = "", **kwargs: Any,
    ) -> Dict[str, Any]:
        """执行桌面动作并截图。"""
        del kwargs
        return await self._safe(self._submit(actions, timeout, stream_id, screenshot=True))

    @Tool(
        "linux_read_page",
        description="用你 Linux 電腦裡的 Chromium 開啟網址並讀取頁面，回傳最終網址、標題、內文和頁面上的連結（links），不截圖。"
        "看連結或網頁內容時用它：頁面真實渲染、會執行 JavaScript，並帶著在這台電腦上登入過的狀態。"
        "要看幾個網址就一次全放進 urls 並行讀取，不要一輪讀一個。"
        "搜尋時讀取 https://html.duckduckgo.com/html/?q=關鍵字（Google 會攔截自動存取），"
        "摘要不夠回答時，從 links 裡挑 2～3 個最相關的結果一次開啟；資訊足夠回答就停，不要為了更完整一直讀下去。"
        "需要搜尋加讀好幾頁的資料查詢，優先交給 subagent 並授權本工具，你繼續聊天。"
        "每次在新分頁開啟、讀完關閉；讀取失敗、被阻擋，或需要看畫面、點擊、登入時改用 linux_desktop。"
        "用於：搜尋、查資料、上網、Google、看連結、讀網頁、開網址（search, browse, web, url）。",
        parameters={"type": "object", "properties": {"urls": URLS_SCHEMA, "timeout": TIMEOUT_SCHEMA},
                    "required": ["urls"], "additionalProperties": False},
        visibility="visible",
        timeout_ms=TOOL_RPC_TIMEOUT_MS,
    )
    async def handle_read_page(
        self, urls: Optional[List[str]] = None, timeout: float = 60, stream_id: str = "", **kwargs: Any,
    ) -> Dict[str, Any]:
        """并行读取网页文字，不截图；兼容旧版单个 url 参数。"""
        if not urls and kwargs.get("url"):
            urls = [kwargs["url"]]
        if not urls:
            return {"success": False, "error": "需要至少一個網址（urls）。"}
        return await self._safe(self._submit([{"action": "read", "urls": urls[:5]}], timeout, stream_id, screenshot=False))

    @Tool(
        "linux_poll",
        description="繼續等待 linux_shell / linux_desktop / linux_read_page 回傳 running 的操作，不會重複執行。"
        "用於：等待、查看進度、拿結果（wait, status, progress, result）。",
        parameters={"type": "object", "properties": {"operation_id": {"type": "string"}, "timeout": TIMEOUT_SCHEMA},
                    "required": ["operation_id"], "additionalProperties": False},
        visibility="visible",
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

    @HookHandler("maisaka.planner.before_request",
                 description="提醒 Planner 打开聊天里的链接、长时间操作电脑时先说一下进展，可要求查资料交给子代理，接管 deferred tools")
    async def handle_planner_request(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """提醒都插在对应内容之后，不放进请求末尾的工具提示里。

        子代理的请求也经过这个 hook：它没有 reply、subagent 和 tool_search，所以不会收到进度提醒、子代理要求，
        工具也不被接管。
        """
        original = kwargs.get("items") or []
        definitions = kwargs.get("tool_definitions") or []
        tools = {tool_name(definition) for definition in definitions}
        items = add_link_reminder(original) or original
        if "reply" in tools:
            items = add_progress_reminder(items, self.config.computer.progress_reminder_seconds) or items
            if self.config.computer.desktop_reply_screenshot:
                items = add_screenshot_reminder(items) or items
        if self.config.computer.prefer_subagent and "subagent" in tools:
            items = append_to_system(items, SUBAGENT_PREFERENCE)
        result = definitions
        if self.config.computer.tool_catalog and "tool_search" in tools:
            loaded, pool = self._tool_sessions.setdefault(str(kwargs.get("session_id") or ""), ({}, {}))
            wanted = ["linux_desktop"] if any(item.get("item_type") == "FunctionCallOutputItem"
                                              and READ_FALLBACK_MARK in str(item.get("output", "")) for item in items) else []
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
    ) -> Dict[str, Any]:
        self._check_chat(stream_id)
        await self._ensure()
        operation_id = uuid4().hex
        request = {"actions": actions, "screenshot": screenshot, "max_edge": self.config.computer.screenshot_max_edge,
                   "max_tabs": self.config.computer.max_tabs}
        self._operations[operation_id] = asyncio.create_task(self._guest("run", operation_id, request))
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
                 "message": "操作仍在執行，用 linux_poll 繼續等待；沒有重複執行。"}, ensure_ascii=False)}
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
