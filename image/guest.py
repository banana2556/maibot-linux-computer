"""容器内执行器：一次 docker exec 执行一批桌面动作，结果 JSON 写到 stdout。

用法：
    guest.py run <op_id>      从 stdin 读取 {"actions": [...], "screenshot": bool, "max_edge": int}
    guest.py cancel <op_id>   终止该操作的整个进程组（含 shell 子进程），已完成的动作不回滚
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import base64
import fcntl
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

OPS_DIR = Path("/tmp/mai-ops")
CDP = "http://127.0.0.1:9222"
# 麦麦用 open 打开的标签页 id，按打开顺序
TABS_FILE = "/tmp/mai-tabs"
# 一次读取的正文字数和链接数上限（多页时平分）：结果会整段留在 Planner 上下文里，后续每轮都重复携带
PAGE_TEXT_LIMIT = 12000
MAX_LINKS = 40
# 读取页面正文和可见链接（去重，最多 MAX_LINKS 个），搜索结果等页面可以顺着链接继续读
PAGE_SCRIPT = """(() => {
  const seen = new Set(), links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const text = a.innerText.replace(/\\s+/g, ' ').trim();
    if (!text || !/^https?:/.test(a.href) || seen.has(a.href) || !a.getClientRects().length) continue;
    seen.add(a.href);
    // DuckDuckGo 结果链接是跳转地址，真实网址在 uddg 参数里
    links.push({text: text.slice(0, 100), url: new URL(a.href).searchParams.get('uddg') || a.href});
    if (links.length >= MAX_LINKS) break;
  }
  const status = (performance.getEntriesByType('navigation')[0] || {}).responseStatus || 0;
  return {url: location.href, title: document.title, text: document.body ? document.body.innerText : '', links, status};
})()"""
# 验证或拦截页的标题/短正文特征；长文章里提到 captcha 不算
BLOCKED_PAGE = re.compile(r"just a moment|attention required|access denied|verify you are human|checking your browser|"
                          r"captcha|are you a robot|unusual traffic|請稍候|人机验证|人機驗證|安全验证|安全驗證", re.I)
# 读不到正文时提示改用一直可见的 linux_desktop 看实际画面
READ_FALLBACK = "有網頁讀取失敗或被阻擋：改用 linux_desktop 開啟這些網址看實際畫面。"
OUTPUT_LIMIT = 20000
BUTTONS = {"left": 1, "middle": 2, "right": 3}
MODIFIERS = ("ctrl", "alt", "shift", "super")
KEY_ALIASES = {
    "control": "ctrl", "win": "super", "meta": "super", "cmd": "super",
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "backspace": "BackSpace",
    "tab": "Tab", "space": "space", "delete": "Delete", "del": "Delete", "insert": "Insert",
    "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
}


class Cancelled(Exception):
    pass


def on_term(signum, frame):
    raise Cancelled("Operation cancelled; completed actions are not rolled back.")


def xdo(*args) -> str:
    return subprocess.run(["xdotool", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


def key_name(key: str) -> str:
    lower = key.lower()
    if lower in MODIFIERS:
        return lower
    if lower in KEY_ALIASES:
        return KEY_ALIASES[lower]
    if re.fullmatch(r"f\d{1,2}", lower):
        return lower.upper()
    return key


def truncate(text: str) -> str:
    if len(text) <= OUTPUT_LIMIT:
        return text
    return text[:OUTPUT_LIMIT // 2] + f"\n...[省略 {len(text) - OUTPUT_LIMIT} 字元]...\n" + text[-OUTPUT_LIMIT // 2:]


class Desktop:
    def __init__(self, max_edge: int, max_tabs: int) -> None:
        self.max_tabs = max_tabs
        # 桌面刚启动时 Xvfb 可能尚未就绪
        deadline = time.monotonic() + 15
        while True:
            try:
                self.width, self.height = map(int, xdo("getdisplaygeometry").split())
                break
            except subprocess.CalledProcessError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        self.scale = min(1.0, max_edge / max(self.width, self.height)) if max_edge > 0 else 1.0

    def point(self, x: float, y: float) -> tuple:
        """模型坐标按最近截图给出，换算回原始屏幕坐标。"""
        return round(x / self.scale), round(y / self.scale)

    def frame(self) -> dict:
        from PIL import ImageGrab

        image = ImageGrab.grab()
        if self.scale < 1:
            image = image.resize((int(self.width * self.scale), int(self.height * self.scale)))
        stream = io.BytesIO()
        image.convert("RGB").save(stream, "JPEG", quality=85)
        try:
            title = xdo("getactivewindow", "getwindowname")
        except subprocess.CalledProcessError:
            title = ""
        return {
            "image": base64.b64encode(stream.getvalue()).decode(), "mime_type": "image/jpeg",
            "width": image.width, "height": image.height,
            "native_width": self.width, "native_height": self.height, "active_window": title,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    def release(self) -> None:
        xdo("keyup", *MODIFIERS)
        for button in BUTTONS.values():
            xdo("mouseup", button)

    def run(self, action: dict) -> dict:
        kind = action["action"]
        button = BUTTONS[action.get("button", "left")]
        if kind in ("observe",):
            return {}
        if kind == "release":
            self.release()
        elif kind == "move":
            xdo("mousemove", *self.point(action["x"], action["y"]))
        elif kind in ("click", "double_click"):
            xdo("mousemove", *self.point(action["x"], action["y"]))
            xdo("click", "--repeat", 2 if kind == "double_click" else 1, button)
        elif kind == "drag":
            start, end = self.point(action["x"], action["y"]), self.point(action["end_x"], action["end_y"])
            steps = max(1, int(action.get("seconds", 0.5) * 30))
            xdo("mousemove", *start)
            xdo("mousedown", button)
            for step in range(1, steps + 1):
                xdo("mousemove", *(round(a + (b - a) * step / steps) for a, b in zip(start, end)))
                time.sleep(action.get("seconds", 0.5) / steps)
            xdo("mouseup", button)
        elif kind == "mouse_down":
            xdo("mousedown", button)
        elif kind == "mouse_up":
            xdo("mouseup", button)
        elif kind == "scroll":
            # 与 Windows 滚轮一致：正数向上
            delta = int(action["delta"])
            if delta:
                xdo("click", "--repeat", abs(delta), 4 if delta > 0 else 5)
        elif kind == "keys":
            keys = [key_name(key) for key in action["keys"]]
            if action.get("seconds"):
                xdo("keydown", *keys)
                time.sleep(action["seconds"])
                xdo("keyup", *reversed(keys))
            else:
                xdo("key", "+".join(keys))
        elif kind == "key_down":
            xdo("keydown", *map(key_name, action["keys"]))
        elif kind == "key_up":
            xdo("keyup", *map(key_name, action["keys"]))
        elif kind == "type":
            xdo("type", "--delay", 12, "--", action["text"])
        elif kind == "open":
            if re.match(r"https?://", action["path"]):
                return open_tab(action["path"], self.max_tabs)
            return open_program(action["path"], action.get("arguments", []))
        elif kind == "shell":
            return shell(action["command"])
        elif kind == "wait":
            time.sleep(action["seconds"])
        elif kind == "wait_window":
            wait_until(lambda: window_exists(action["title"]), action["seconds"], f"視窗 {action['title']}")
        elif kind == "wait_process":
            wait_until(lambda: not Path(f"/proc/{action['pid']}").exists(), action["seconds"], f"程序 {action['pid']} 結束")
        elif kind == "browser":
            return browser(action["method"], action.get("target", ""), action.get("params", {}))
        elif kind == "read":
            if action.get("urls"):
                return read_pages(action["urls"], action.get("seconds", 20))
            return read_page("", action.get("target", ""), action.get("seconds", 20))
        else:
            raise ValueError(f"不支援的桌面動作：{kind}")
        return {}


def open_program(path: str, arguments: list) -> dict:
    # 新会话启动，取消当前操作时不会连带关闭打开的应用
    process = subprocess.Popen([path, *arguments], cwd="/workspace", start_new_session=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"pid": process.pid}


def shell(command: str) -> dict:
    process = subprocess.Popen(["bash", "-lc", command], cwd="/workspace", stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()
    return {"exit_code": process.returncode,
            "stdout": truncate(stdout.decode(errors="replace")), "stderr": truncate(stderr.decode(errors="replace"))}


def window_exists(title: str) -> bool:
    return subprocess.run(["xdotool", "search", "--onlyvisible", "--name", title], capture_output=True).returncode == 0


def wait_until(condition, seconds: float, label: str) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"等待{label}逾時")
        time.sleep(0.2)


def cdp_http(path: str, method: str = "GET") -> str:
    request = urllib.request.Request(CDP + path, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode()


def cdp_ready() -> bool:
    try:
        cdp_http("/json/version")
        return True
    except OSError:
        return False


def cdp_pages() -> list:
    """列出标签页；浏览器没开时先启动，登录状态保存在 /workspace/.browser。"""
    if not cdp_ready():
        open_program("browser", [])
        wait_until(cdp_ready, 20, "瀏覽器啟動")
    return [item for item in json.loads(cdp_http("/json/list")) if item["type"] == "page"]


def cdp_send(page: dict, method: str, params: dict) -> dict:
    import websocket

    socket = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
    try:
        socket.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            message = json.loads(socket.recv())
            if message.get("id") == 1:
                break
    finally:
        socket.close()
    if "error" in message:
        raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
    return message["result"]


def cdp_eval(page: dict, expression: str):
    return cdp_send(page, "Runtime.evaluate", {"expression": expression, "returnByValue": True})["result"].get("value")


def browser(method: str, target: str, params: dict) -> dict:
    pages = cdp_pages()
    if method == "targets":
        return {"targets": pages}
    pages = [item for item in pages if not target or item["id"] == target]
    if not pages:
        raise RuntimeError("沒有符合的 CDP 頁面")
    return {"target": pages[0]["id"], "result": cdp_send(pages[0], method, params)}


def new_tab(url: str) -> dict:
    """在新标签页打开网址；打不开时关掉这个标签页并报错。"""
    cdp_pages()
    page = json.loads(cdp_http("/json/new", "PUT"))
    error = cdp_send(page, "Page.navigate", {"url": url}).get("errorText")
    if error:
        cdp_http(f"/json/close/{page['id']}")
        raise RuntimeError(f"開啟失敗：{error}")
    return page


def open_tab(url: str, max_tabs: int) -> dict:
    """打开并切到前台；麦麦开的标签页超过上限时关掉最早的，手动打开的标签页不在记录里，不受影响。"""
    page = new_tab(url)
    cdp_http(f"/json/activate/{page['id']}")
    with open(TABS_FILE, "a+") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        file.seek(0)
        alive = {item["id"] for item in cdp_pages()}
        tabs = [tab for tab in file.read().split() if tab in alive] + [page["id"]]
        for stale in tabs[:-max_tabs] if max_tabs > 0 else []:
            cdp_http(f"/json/close/{stale}")
        file.seek(0)
        file.truncate()
        file.write("\n".join(tabs[-max_tabs:] if max_tabs > 0 else tabs))
    return {"target": page["id"]}


def read_pages(urls: list, seconds: float) -> dict:
    """并行读取多个网址，一个打不开不影响其他，全部失败才算失败；正文字数和链接数按页数平分，总量与读一页相当。"""
    def one(url: str) -> dict:
        try:
            return read_page(url, "", seconds, PAGE_TEXT_LIMIT // len(urls), MAX_LINKS // len(urls))
        except Exception as exc:
            return {"url": url, "error": f"{type(exc).__name__}: {exc}"}

    # ponytail: 取消时线程随进程组一起结束，正在读的标签页不会关闭；重开浏览器或 docker restart 可清理
    with ThreadPoolExecutor(len(urls)) as pool:
        pages = list(pool.map(one, urls))
    if all("error" in page for page in pages):
        raise RuntimeError("；".join(page["error"] for page in pages) + "\n" + READ_FALLBACK)
    if any("error" in page or "problem" in page for page in pages):
        return {"pages": pages, "hint": READ_FALLBACK}
    return {"pages": pages}


def page_problem(result: dict, status: int) -> str:
    """读到的不是正常内容时说明原因：HTTP 错误、验证或拦截页、没有文字；正常页面返回空字符串。"""
    if status >= 400:
        return f"HTTP {status}"
    if BLOCKED_PAGE.search(result["title"]) or (len(result["text"]) < 1500 and BLOCKED_PAGE.search(result["text"])):
        return "像是驗證或阻擋頁面"
    if not result["text"].strip():
        return "頁面沒有文字內容"
    return ""


def read_page(url: str, target: str, seconds: float, text_limit: int = PAGE_TEXT_LIMIT, link_limit: int = MAX_LINKS) -> dict:
    """给 url 时在新标签页打开、读取正文后关闭，并发读取互不干扰；给 target 时读取已打开的页面，不导航也不关闭。"""
    if not url and not target:
        raise ValueError("read 需要 urls 或 target")
    if url:
        page = new_tab(url)
    else:
        page = next((item for item in cdp_pages() if item["id"] == target), None)
        if page is None:
            raise RuntimeError(f"沒有這個分頁：{target}")

    def loaded() -> bool:
        try:
            return bool(cdp_eval(page, "location.href !== 'about:blank' && document.readyState === 'complete'"))
        except RuntimeError:  # 导航中执行上下文被替换
            return False

    try:
        result = {}
        try:
            wait_until(loaded, seconds, "頁面載入")
            # ponytail: 加载完成后固定再等 1 秒给前端渲染；更慢的动态页面可用 target 再读一次
            time.sleep(1)
        except TimeoutError:
            result["note"] = f"{seconds:g} 秒內沒有載入完成，以下是當時已顯示的內容"
        result.update(cdp_eval(page, PAGE_SCRIPT.replace("MAX_LINKS", str(link_limit))))
    finally:
        if url:
            cdp_http(f"/json/close/{page['id']}")
    problem = page_problem(result, result.pop("status"))
    if problem:
        result["problem"] = problem
    if len(result["text"]) > text_limit:
        result["text"] = result["text"][:text_limit] + f"\n...[後面還有 {len(result['text']) - text_limit} 字元未顯示]"
    return result


def cancel(op_id: str) -> dict:
    try:
        pgid = int((OPS_DIR / f"{op_id}.pid").read_text())
    except FileNotFoundError:
        return {"cancelled": False}
    os.killpg(pgid, signal.SIGTERM)
    # 给 shell 子进程一点收尾时间，仍未退出的强制结束
    for _ in range(30):
        time.sleep(0.1)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return {"cancelled": True}
    os.killpg(pgid, signal.SIGKILL)
    return {"cancelled": True}


def run(op_id: str, request: dict) -> dict:
    try:
        os.setsid()
    except PermissionError:
        pass  # 已是进程组组长
    OPS_DIR.mkdir(exist_ok=True)
    pid_file = OPS_DIR / f"{op_id}.pid"
    pid_file.write_text(str(os.getpgrp()))
    signal.signal(signal.SIGTERM, on_term)
    desktop = Desktop(int(request.get("max_edge", 0)), int(request.get("max_tabs", 5)))
    outputs = []
    result = {"success": True, "status": "completed"}
    try:
        for action in request["actions"]:
            output = desktop.run(action)
            outputs.append({"action": action["action"], "result": output})
            if output.get("exit_code", 0) != 0:
                raise RuntimeError(f"Shell exited with code {output['exit_code']}")
    except Cancelled as exc:
        result = {"success": False, "status": "cancelled", "error": str(exc)}
        desktop.release()
    except Exception as exc:
        detail = f"\n{exc.stderr}" if isinstance(exc, subprocess.CalledProcessError) else ""
        result = {"success": False, "status": "failed", "error": f"{type(exc).__name__}: {exc}{detail}"}
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pid_file.unlink(missing_ok=True)
    result["results"] = outputs
    if request.get("screenshot", True):
        # 失败也返回现场截图，便于模型修正
        try:
            result.update(desktop.frame())
        except Exception as exc:
            result["capture_error"] = f"{type(exc).__name__}: {exc}"
    return result


def main() -> None:
    command, op_id = sys.argv[1], sys.argv[2]
    if not re.fullmatch(r"[0-9a-f]{32}", op_id):
        raise SystemExit("invalid op_id")
    result = cancel(op_id) if command == "cancel" else run(op_id, json.load(sys.stdin))
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
