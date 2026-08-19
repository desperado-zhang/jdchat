# 插件 MVP 说明

## 1. 当前范围

插件位于 `extension/`，用于验证咚咚工作站聊天记录的只读采集链路。

当前只实现 MVP：

```text
注入 MAIN world 脚本
读取 window.session.customer
读取 window.session.messages()
默认读取页面已有前端状态和已渲染 DOM 消息
监听当前会话 #t-chat-scroll DOM 增量
通过插件弹窗显式开启 Network 被动监听
把事件发到 background 队列
批量 POST 到 http://127.0.0.1:8765/capture/events
```

当前不实现：

```text
主动请求京东接口
抓取或复用 token/cookie
批量拉历史
自动切换客户
自动输入
自动点击发送
自动已读
自动回复
```

## 2. 文件结构

```text
extension/
├── manifest.json
├── injected-main.js
├── content-isolated.js
├── background.js
├── options.html
└── options.js
```

职责：

```text
injected-main.js      页面 MAIN world，只读观察 window.session
content-isolated.js   插件 isolated world，监听 DOM 和转发 page postMessage
background.js         插件 service worker，本地队列和上报本机 Python 服务
options.html/js       插件弹窗，配置总开关、DOM、session、Network、网关地址
manifest.json         Chrome MV3 配置
```

## 3. 使用方式

先启动本机采集网关：

```bash
eval "$(conda shell.zsh hook)"
conda activate jdchat
uvicorn jdchat_gateway.main:app --host 127.0.0.1 --port 8765 --reload
```

Chrome 加载插件：

```text
chrome://extensions
打开 Developer mode
点击 Load unpacked
选择 /Users/leo/project/jdchat/extension
```

打开咚咚工作站：

```text
https://dongdong.jd.com/
```

手动打开一个客户会话，插件会被动采集当前会话已渲染消息和页面前端状态。

如果要启用 Network + DOM 双通道，在插件弹窗中打开：

```text
Network 被动监听
fetch
XHR
```

然后刷新咚咚页面生效。`WebSocket` 开关默认关闭，只在需要验证实时链路且确认页面兼容时再手动打开。

Network 监听只做页面内被动观察：

```text
不主动请求京东接口
不复用 token/cookie/sign
不保存完整响应体
只从页面自然收到的响应/帧中提取疑似聊天消息
事件只写入本机 http://127.0.0.1:8765/capture/events
```

历史对话需要先由客服手动点击左侧顶部的“历史咨询”时钟 tab。实测结构：

```text
.c_tabs-tab[title="历史咨询"]        历史咨询入口
.list-compatible.recent-user-w      历史咨询列表容器
.alluser-item                       单个历史会话行
```

插件默认不自动点击“历史咨询”，也不自动遍历 `.alluser-item`。客服手动进入历史 tab 并手动点开某个历史会话后，插件再读取右侧已经渲染出的会话消息。

检查后端：

```bash
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/conversations
curl "http://127.0.0.1:8765/capture/events/recent?limit=10"
```

历史会话验证时，`/capture/events/recent` 应出现：

```text
active_sidebar_tab = history
history_list_visible = true
message_node_count > 0
```

## 4. 安全边界

插件代码中没有：

```text
.click()
键盘事件模拟
输入框写入
window.session.send
window.session.read
window.session.setStatus
自动点击历史咨询 tab
自动遍历历史会话行
主动 fetch 京东接口
主动创建额外 WebSocket
```

`injected-main.js` 默认不包装 WebSocket / fetch / XHR，避免影响咚咚工作台初始化。Network hook 通过插件弹窗显式打开后才注入；fetch/XHR 与 WebSocket 是独立开关，WebSocket 默认关闭。

`background.js` 只向本机服务发送：

```text
http://127.0.0.1:8765/capture/events
```

## 5. 当前限制

```text
DOM 来源消息会生成 dom-* id，可能无法与结构化 session 消息完全合并
Network hook 只能观察启用后页面自然收到的数据，不能回溯 DevTools 里已经存在的历史请求响应
background 队列使用 chrome.storage.local，适合 MVP，不适合大规模长期缓存
```

后续应增加：

```text
本机接口 token 配置
队列状态展示
真实咚咚页面联调报告
DOM 与结构化消息合并策略优化
```
