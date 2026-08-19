# jdchat

京东咚咚聊天记录被动采集与本地采集网关实验项目。

## Conda 环境

本项目后端开发、运行、启动、测试都必须先激活 conda 环境：

```bash
conda activate jdchat
```

首次创建环境：

```bash
conda env create -f environment.yml
```

如果环境已存在，更新依赖：

```bash
conda env update -f environment.yml --prune
```

校验当前环境：

```bash
python -V
python -m pip list
```

后续所有后端命令统一放在激活环境之后执行。

## 本机采集网关

启动服务：

```bash
eval "$(conda shell.zsh hook)"
conda activate jdchat
uvicorn jdchat_gateway.main:app --host 127.0.0.1 --port 8765 --reload
```

健康检查：

```bash
curl http://127.0.0.1:8765/health
```

运行测试：

```bash
eval "$(conda shell.zsh hook)"
conda activate jdchat
ruff check .
pytest -q
```

默认 SQLite 路径：

```text
data/jdchat.sqlite3
```

可通过环境变量覆盖：

```bash
export JDCHAT_DATABASE_PATH=/absolute/path/to/jdchat.sqlite3
```

可选本机接口 token：

```bash
export JDCHAT_API_TOKEN=local-secret
```

设置后请求需要带：

```text
Authorization: Bearer local-secret
```

## 主要接口

```text
GET  /health
POST /capture/events
GET  /conversations
GET  /conversations/{conversation_key}/messages
```

`POST /capture/events` 接收插件批量事件，后端会规范化 conversation/message，计算 `dedupe_key`，并写入 SQLite。去重优先级：

```text
1. msg.id
2. conversation_key + mid
3. conversation_key + timestamp + direction + body_type + content_hash
```
