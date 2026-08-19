# 插件 MVP 说明

## 1. 当前范围

插件位于 `extension/`，用于验证咚咚工作站聊天记录的只读采集链路。

当前只实现 MVP：

```text
注入 MAIN world 脚本
读取 window.session.customer
读取 window.session.messages()
被动观察页面已有 WebSocket / fetch / XHR 响应
监听当前会话 #t-chat-scroll DOM 增量
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
└── background.js
```

职责：

```text
injected-main.js      页面 MAIN world，只读观察 window.session 和页面网络事件
content-isolated.js   插件 isolated world，监听 DOM 和转发 page postMessage
background.js         插件 service worker，本地队列和上报本机 Python 服务
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

检查后端：

```bash
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/conversations
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
主动 fetch 京东接口
主动创建额外 WebSocket
```

`injected-main.js` 的网络 hook 只读取页面自身已经收到的响应 clone，不修改请求和响应，也不阻断页面逻辑。

`background.js` 只向本机服务发送：

```text
http://127.0.0.1:8765/capture/events
```

## 5. 当前限制

```text
DOM 来源消息会生成 dom-* id，可能无法与结构化 session 消息完全合并
WebSocket/XHR/fetch 消息解析是通用递归提取，需要用真实页面继续校验
background 队列使用 chrome.storage.local，适合 MVP，不适合大规模长期缓存
插件还没有配置 UI，网关地址固定为 127.0.0.1:8765
```

后续应增加：

```text
插件开关 UI
网关地址配置
本机接口 token 配置
队列状态展示
真实咚咚页面联调报告
DOM 与结构化消息合并策略优化
```
