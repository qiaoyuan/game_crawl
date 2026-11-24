---
name: eldorado-manual-order
description: 手动查询 Eldorado 最新一笔或指定的 Aion 2 售出订单，记录交付与结算字段，或单独采集已记录订单的聊天。
---

# 手动采集一笔 Eldorado 订单

在项目根目录运行命令。将 `ORDER_ID_OR_URL` 替换为用户指定的完整 UUID 或对应的 Eldorado 订单详情链接。使用已登录卖家账户并开启远程调试的 Chrome。若浏览器弹出连接提示，请用户在 Chrome 中允许连接。

## 记录订单

查询最新订单时直接运行：

```bash
venv/bin/python -m tools.capture_eldorado_order latest --chrome-auto-connect
```

程序优先复用已打开的默认售出列表，刷新一次，在 Recent 排序的当前页取第一笔 Aion 2 订单。已保存的订单直接返回本地文件，首次发现才进入详情页。指定订单时使用：

```bash
venv/bin/python -m tools.capture_eldorado_order order 'ORDER_ID_OR_URL' --chrome-auto-connect
```

此步骤只读取详情中的十个字段，并核对页面显示的订单号、详情链接及 Aion 2 游戏名。

| 页面字段 | 输出字段 |
| --- | --- |
| Order ID、Game、Region、Faction、Server | `order_id`、`game`、`region`、`faction`、`server` |
| Quantity、Buyer | `quantity`、`buyer` |
| Order price、Commission、You receive | `order_price`、`commission`、`you_receive` |

数量和金额保留页面显示的单位、货币符号与正负号。成功后在 `downloads/eldorado_manual/<UUID>/order.jsonl` 保存一条记录。同一订单再次执行时保留首次记录。用户只要求记录订单时，到此结束并告知文件位置。

## 读取这笔订单的聊天

用户明确要求聊天后，使用同一个订单号或详情链接。此步骤要求该单已通过 `order` 步骤记录。

```bash
venv/bin/python -m tools.capture_eldorado_order chat 'ORDER_ID_OR_URL' --chrome-auto-connect
```

读取这笔订单的 TalkJS 聊天，将页面可加载的消息按从早到晚的顺序以 UTF-8 原文保存到同一目录的 `chat.jsonl`。文件只保留最近一次成功读取的完整快照。两份结果可通过目录中的订单号对应。
