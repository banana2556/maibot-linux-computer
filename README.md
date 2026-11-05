# Linux 沙盒电脑插件

让麦麦的 Planner **直接**拥有一台 Linux 电脑：终端、图形桌面和 Chromium 浏览器。
不经过外接 Agent 转述——聊天的那个麦麦自己决定、自己动手；耗时任务交给它的后台子代理，聊天不中断。

电脑是一个长期运行的 Docker 容器（Debian + Xvfb + openbox + Chromium），不是宿主机本身：
群聊里任何人诱导麦麦乱跑命令，最坏只影响容器。

## 工具

| 工具 | 用途 |
|---|---|
| `linux_shell` | 执行 bash（root），返回 stdout/stderr/exit_code，不截图 |
| `linux_read_page` | 像 Claude Code 的 WebFetch：用 Chromium 打开网址（一次最多 5 个，并行），原文交给小模型按必填的 `prompt` 摘取，只返回摘要和相关链接；每个网址单独标签页、读完关闭 |
| `linux_desktop` | 批量桌面动作（点击、输入、按键、拖拽、打开程序/网址、CDP 调用；`read` 读当前分页的完整原文，`elements` 列出可点元素和坐标），批次里有 `observe` 才返回截图 |
| `linux_poll` | 继续等待超时返回 `running` 的操作，不重复执行 |
| `linux_cancel` | 终止运行中的操作及其子进程 |

麦麦是模拟真人聊天的 bot，看网页远比装软件常见，工具按这个分层：`linux_read_page`、`linux_desktop` 一直可见
（两个合计约 2900 字，每轮都会带上）；`linux_shell`、`linux_poll`、`linux_cancel` 是 deferred，第一次要用时先 `tool_search`，三个会一起载入。操作超时返回 running 时，插件直接把这一组载入，下一轮就能 `linux_poll`。
参照 Claude Code 的工具写法：`linux_desktop` 的参数摊平成一个 `action` 选项加选填字段，每个动作要填什么只在 `action` 的说明里写一次，缺字段时执行结果会指出是第几个动作、哪个字段；工具说明只写什么时候用、什么时候不用。只有 deferred 的三个工具在说明里列了使用者会说的动词和同义词（安裝、等待、停止、install、kill……），供 `tool_search` 检索。

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

参照 Claude Code 的 WebFetch / 浏览器工具分工：

- `linux_read_page`：只想知道网页里的某件事。每页原文（最多 10 万字，同 WebFetch）和链接交给 `extract_task` 指定的模型（默认 `utils`），
  按 `prompt` 摘出相关内容、列出要继续看的链接，只有摘要进 Planner 上下文。摘取失败时退回原文前 3000 字。
  每页多一次小模型调用，会慢几秒；超时返回 running 时，`linux_poll` 拿到的也是摘好的结果。
  原文超过上限时结果标出 `truncated`（后面还有多少字没读到）。
  文字读取看不到图片：页面里有内文图片（不算 100px 以下的图示）时，结果标出 `images`（几张图，要看图用桌面截图）。
- 要对网页给感想、评论或看图时，工具说明引导直接用 `linux_desktop`：`open` 后 `read` 读全文、`observe` 看画面；
  `linux_read_page` 的 800 字摘要是用来查事实的，不适合当感想的素材。
- 同一聊天 15 分钟内（同 WebFetch 的缓存时间）每个网址只能用 `linux_read_page` 读一次：重读直接拒绝、不开浏览器，
  结果说明重读不会多出内容，要更多就用 `linux_desktop` 的 `open` 加 `read`，要往下卷才载入的内容先 `scroll` 或 `observe` 看画面。
  一批里混着读过和没读过的网址时，只读没读过的，读过的列在 `skipped`。避免 Planner 换个问法反复读同一页，该看画面时却不用桌面。
- `linux_desktop` 的 `read`：要完整原文时用，例如 `[open 网址, read]`；不填 target 就读当前显示的分页。
- 截图是 `observe` 动作，批次里有它才截，只读文字的批次不附图。
- `elements` 列出当前分页里可以点的链接、按钮、输入框和坐标（和截图同比例，可直接 `click`），
  不在画面里的只标 `where`（above/below），滚动后再取；Planner 看不了图时也能照文字清单操作。

用浏览器搜索时，工具说明引导麦麦读取 DuckDuckGo 的 HTML 版（Google 会对自动访问弹出验证），
`prompt` 写要找什么，摘要里会列出相关结果的真实网址，麦麦可以挑几个继续读。

网页读取失败或被拦截时（打不开、HTTP 4xx/5xx、Cloudflare/Google 之类的验证页、页面没有文字），
对应那页带 `problem` 说明原因，结果附上改用 `linux_desktop` 打开看实际画面的提示，
`linux_desktop` 一直可见，下一轮就能直接用。

为了少跑轮数，工具说明要求：几个网址一次放进 `urls` 并行读；通常挑 2～3 个最相关的结果，信息够了就停。

每条规则只写在一个地方：工具说明只写工具之间怎么选（查事实用 `linux_read_page`，给感想、看图、操作用 `linux_desktop`）；
自己做还是交给子代理、先回一句、交给 Replyer 这三条，按设置组合成系统提示词末尾的一段「使用 Linux 電腦的方式」，每轮内容相同。
分流规则是：搜索、读好几个网页、比对信息交给后台子代理（需开启 `planner_subagent` 和 `advanced_sub_agent`）；
只看一个网页（查一件事、给感想、看图）自己做，因为子代理截的图只留在子代理自己的历史里，传不到回复里。
`linux_desktop` 的 `read` 返回的原文合计约 1.2 万字、链接 40 个，因为会整段留在 Planner 上下文里，更长的页面会截断。

工具说明、提醒和返回给模型的提示都用繁体中文；WebUI 配置说明和本文档仍是简体。

## 链接提醒

插件挂在 `maisaka.planner.before_request`：最近 10 条聊天消息里有还没打开过的 http(s) 链接时，
在含链接的最新那条消息后面插入一条提醒，要求先打开查看实际内容再回应。

- 不点名任何工具，用浏览器、网页读取还是子代理由麦麦自己决定。
- 提醒紧跟在消息后面，不放进请求末尾的 `tool_search` 工具提示里。
- 链接出现在任何工具调用的参数或结果里，或出现在模型自带联网的记录里，就视为已经打开过，不再提醒；麦麦自己发的消息不提醒。
- QQ 分享卡片（B 站、小程序等）的网址在主程序解析时就被丢弃，插件看不到，这类卡片不会触发提醒。

## 先回一句（`brief_reply_first`，默认开启）

读网页、操作桌面要花一段时间，对方会以为麦麦没反应。系统提示词末尾要求：有人问了之后还没回过话，
第一次用电脑时在同一轮也用 `reply` 简短说一句在看什么（例如「我看一下」），查完再正式回复，中间不回报进度。
漏了的话（有人说话后，麦麦没回应就先调用了 linux 工具），同一个 hook 在最近一次工具结果后补一条提醒：
已经能回答就直接正式回复，还要继续查就先简短回一句、和下一步同一轮发出。麦麦一说话就不再提醒。

## 交给 Replyer（`reply_handoff`，默认开启）

麦麦的回复由 Replyer 另外生成，它只收到真实聊天消息和 Planner 填的 `reply_reference`，看不到工具结果和截图
（`attach_pic` 附图时也只收到「工具返回媒体…」这句说明）。所以：

- 系统提示词末尾要求 Planner 用 `reply` 时，把用电脑读到的具体内容（名称、日期、数字、重点）写进 `reply_reference`。
- 插件挂在 `maisaka.replyer.before_model_request`：Replyer 这次选中的模型有视觉（`visual = true`）时，
  把 Planner 上次说话后工具返回的截图（最新 3 张）加进 Replyer 请求，放在 `reply_reference` 前面，
  并说明这是麦麦自己截的画面、不是对方传的。Replyer 模型没视觉时不加。
  截图取自 Planner 最近一次请求，所以和 Planner 决定回复时看到的一致；
  一则回复生成完（`maisaka.reply.before_post_process`）就清掉，不会带到之后不经过 Planner 的回复，Replyer 重试时仍用同一批。

麦麦上次说话后在桌面上操作过（点击、输入、按键、拖拽；只是 `observe` 截图、`scroll`、`move` 看画面，
或用 `open`、`read`、`elements` 读网页都不算，看公告给感想不必把截图发回聊天）时
（`desktop_reply_screenshot`，默认开启），同一个 hook 在那批工具结果和截图之后插入提醒：回复时一定要附上最新截图。
操作后有截图就直接给出 `reply` 的 `attach_pic` 要填的 `media_index=tool_result:<call_id>:1`，还没有就要求先 `observe` 截一张。

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
| `computer.brief_reply_first` | 有人说话后要用电脑时，要求先简短回一句再查，查完再正式回复；默认开启 |
| `computer.reply_handoff` | 要求 Planner 把读到的内容写进 `reply_reference`；Replyer 有视觉时把最新 3 张截图一起交给它；默认开启 |
| `computer.desktop_reply_screenshot` | 上次说话后在桌面上操作过时，要求回复附上最新截图，默认开启 |
| `computer.extract_task` | `linux_read_page` 按 `prompt` 摘取网页用的模型任务（`model_config` 里的任务名），默认 `utils`，建议用快的小模型 |
| `computer.prefer_subagent` | 在 Planner 系统提示词末尾要求搜索、读好几个网页、比对信息时交给子代理，派出后等它回报、不自己重做；只看一个网页（查一件事、给感想、看图）自己做，因为子代理截的图传不到回复里；默认开启，主程序没开 `planner_subagent` 时不生效 |
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
