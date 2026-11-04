# Linux 沙盒电脑插件

让麦麦的 Planner **直接**拥有一台 Linux 电脑：终端、图形桌面和 Chromium 浏览器。
不经过外接 Agent 转述——聊天的那个麦麦自己决定、自己动手；耗时任务交给它的后台子代理，聊天不中断。

电脑是一个长期运行的 Docker 容器（Debian + Xvfb + openbox + Chromium），不是宿主机本身：
群聊里任何人诱导麦麦乱跑命令，最坏只影响容器。

## 工具

| 工具 | 用途 |
|---|---|
| `linux_shell` | 执行 bash（root），返回 stdout/stderr/exit_code，不截图 |
| `linux_read_page` | 用 Chromium 打开网址（一次最多 5 个，并行读取），返回最终网址、标题、正文文字和页面上的链接，不截图；每个网址单独标签页、读完关闭，多个聊天同时读互不干扰 |
| `linux_desktop` | 批量桌面动作（点击、输入、按键、拖拽、打开程序/网址、CDP 调用、`read` 读正文……），返回截图 |
| `linux_poll` | 继续等待超时返回 `running` 的操作，不重复执行 |
| `linux_cancel` | 终止运行中的操作及其子进程 |

工具按「第一轮就可能要用」分层：`linux_read_page`、`linux_shell`、`linux_poll` 一直可见；
`linux_desktop`（参数约 5000 字，占全部工具的七成）和 `linux_cancel` 是 deferred，第一次要用时先 `tool_search`，两个会一起载入。
工具说明里列了使用者会说的动词和同义词（截圖、點擊、安裝、搜尋、screenshot、click……），供 `tool_search` 检索。

## 工具目录（`tool_catalog`）

开启时（默认）插件接管 Planner 的 deferred tools，对所有插件和内置工具生效，主程序不用改：

- 主程序每轮列出全部 deferred 工具描述的提示，换成系统提示词末尾的类别目录：只列类别（工具名第一个 `_` 前的部分）和工具名，不放参数。
- `tool_search` 搜到一个工具时，同类别的插件工具一起载入；载入后整个会话保持，搜索记录滑出上下文也不会折回去。
- tools 数组按首次出现的顺序只追加、不重排，系统提示词内容固定，不破坏提示词缓存。
- `tool_search` 每次最多 5 个；没搜到时结果改为列出可搜的类别（例如用简体「截图」搜不到繁体描述时，模型会看到可以搜 `linux`）。

限制：主程序自带的 deferred 工具（`web_fetch` 等）插件拿不到参数定义，不能整组带出，只能靠搜索各自载入；
主程序用 OpenAI 格式发送工具，不支持 Anthropic 的 `tool_reference`，新载入的工具只能追加到 tools 末尾。

看网页走浏览器：页面真实渲染、执行 JavaScript，并带着在这台电脑上登录过的状态（例如在 noVNC 里手动登录过的网站）。
主程序的 `web_search` / `web_fetch` 可以关掉，对话模型自带联网时搜索交给模型本身。

用浏览器搜索时，工具说明引导麦麦读取 DuckDuckGo 的 HTML 版（Google 会对自动访问弹出验证），
结果链接会还原成真实网址放在 `links` 里，麦麦可以挑相关结果继续打开正文。

网页读取失败或被拦截时（打不开、HTTP 4xx/5xx、Cloudflare/Google 之类的验证页、页面没有文字），
对应那页带 `problem` 说明原因，结果附上改用 `linux_desktop` 打开看实际画面的提示，
同时插件把 `linux_desktop` 直接载入 Planner（需开启 `tool_catalog`），下一轮就能用，不必先搜索。

为了少跑轮数，工具说明要求：几个网址一次放进 `urls` 并行读；通常挑 2～3 个最相关的结果，信息够了就停；
需要搜索加读好几页的查询优先交给后台子代理（需开启 `planner_subagent` 和 `advanced_sub_agent`）。
一次读取的正文合计约 1.2 万字、链接合计 40 个（多页时平分），因为结果会整段留在 Planner 上下文里，后续每轮都重复携带；
更长的页面会截断。想进一步省上下文可以在 `bot_config.toml` 开启 `tool_result_condense`，长结果会先整理成结论。

工具说明、提醒和返回给模型的提示都用繁体中文；WebUI 配置说明和本文档仍是简体。

## 链接提醒

插件挂在 `maisaka.planner.before_request`：最近 10 条聊天消息里有还没打开过的 http(s) 链接时，
在含链接的最新那条消息后面插入一条提醒，要求先打开查看实际内容再回应。

- 不点名任何工具，用浏览器、网页读取还是子代理由麦麦自己决定。
- 提醒紧跟在消息后面，不放进请求末尾的 `tool_search` 工具提示里。
- 链接出现在任何工具调用的参数或结果里，或出现在模型自带联网的记录里，就视为已经打开过，不再提醒；麦麦自己发的消息不提醒。
- QQ 分享卡片（B 站、小程序等）的网址在主程序解析时就被丢弃，插件看不到，这类卡片不会触发提醒。

## 进度提醒

麦麦上次在聊天里说话之后，已经连续操作电脑超过 `progress_reminder_seconds`（默认 60 秒）时，
同一个 hook 会在最近一次工具结果后插入提醒：用一两句话说明在做什么、做到哪一步，画面有参考价值时附上截图，
并且和下一步操作放在同一轮一起发出，不单独占一轮。
说话后重新计时，所以长任务里大约每分钟会汇报一次；设为 0 关闭。

麦麦上次说话后用过 `linux_desktop` 时（`desktop_reply_screenshot`，默认开启），同一个 hook 在那批工具结果和截图之后插入提醒：
回复时一定要附上最新截图，并直接给出 `reply` 的 `attach_pic` 要填的 `media_index=tool_result:<call_id>:1`。

只对 Planner 自己操作有效。后台子代理不能在聊天里说话，它工作时可以问麦麦「进度如何」，
麦麦能随时截一张当前画面（桌面是共用的，看得到子代理正在做什么）；想实时看就用下面的 noVNC。

## 标签页

`open` 打开的网页在新标签页中打开并返回 `target`。麦麦打开的标签页最多保留 `max_tabs` 个（默认 5），
超出时自动关闭最早的；你在 noVNC 里手动打开的标签页不受影响。`linux_read_page` 读完就关，不计入上限。

## 安装（VPS）

```bash
# 1. Docker，并允许运行麦麦的用户使用（docker 组等同 root 权限）
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # 重新登录生效

# 2. 插件放到 MaiBot/plugins/linux_computer，启动麦麦后在 WebUI 插件设置里启用
```

启用后插件会自动构建镜像（首次约 5 分钟、约 1.8 GB 磁盘）并启动容器 `maibot-linux-computer`。

**Planner 模型必须支持看图，并在 `model_config` 里把 planner 任务用到的每个模型都标记 `visual = true`。**
否则 Planner 以纯文本模式运行，截图只会在后台转成文字描述，桌面点击基本无法使用（`linux_shell` 不受影响）；
子代理也用 planner 模型，且会直接收到截图。

建议同时在 `bot_config.toml` 打开后台子代理，让长任务不卡聊天：

```toml
[capabilities]
planner_subagent = true
advanced_sub_agent = true
```

## 配置

| 项 | 说明 |
|---|---|
| `computer.allowed_chats` | 允许使用电脑的聊天流 session_id，留空为所有聊天 |
| `computer.networking` / `memory_mb` / `screen` / `vnc_port` | 创建容器时生效；改完需删除容器重建 |
| `computer.screenshot_max_edge` | 截图最长边，越小越省 token |
| `computer.max_tabs` | 麦麦打开的网页标签页上限，默认 5，0 为不限制 |
| `computer.progress_reminder_seconds` | 连续操作多少秒没说话就提醒汇报进度，默认 60，0 为关闭 |
| `computer.desktop_reply_screenshot` | 上次说话后用过 `linux_desktop` 时，要求回复附上最新截图，默认开启 |
| `computer.prefer_subagent` | 在 Planner 系统提示词末尾要求查资料（搜索、读网页、比对信息）尽量交给子代理，只有打开单个链接这种一步完成的事自己做；默认开启，主程序没开 `planner_subagent` 时不生效 |
| `computer.tool_catalog` | 接管 Planner 的 deferred tools（类别目录、整组载入、会话内保持、顺序固定），默认开启；关闭后交回主程序原本的 `tool_search` |

## 看它在做什么 / 手动接管

容器把 noVNC 映射到宿主机 `127.0.0.1:6080`。在你的电脑上：

```bash
ssh -L 6080:127.0.0.1:6080 你的VPS
```

浏览器打开 <http://127.0.0.1:6080/vnc.html> 即可观看或手动操作（例如帮它登录网站、过验证码）。

## 重置

| 操作 | 结果 |
|---|---|
| `docker restart maibot-linux-computer` | 重启桌面，已装软件保留 |
| `docker rm -f maibot-linux-computer` | 删除容器，下次使用时重建，已装软件丢失，`/workspace` 保留 |
| `docker volume rm maibot-linux-computer-workspace` | 删除工作目录（含浏览器登录状态） |

## 自检

```bash
uv run python plugins/linux_computer/selftest.py
```

在独立测试容器里跑一遍全部工具，结束后自动删除。

## 限制

- 插件重启后，之前返回 `running` 的操作无法再用 `linux_poll` 查询（容器里的进程仍在跑）。
- 多个聊天同时操作同一桌面时输入可能交错；需要隔离时用 `allowed_chats` 限定。
- 麦麦本身若跑在 Docker 里，需要挂载 `/var/run/docker.sock` 并安装 docker CLI。
