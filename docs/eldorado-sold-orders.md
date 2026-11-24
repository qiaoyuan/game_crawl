# Eldorado 的 Aion 2 新售出订单

在开启远程调试的 Chrome 中登录 Eldorado，打开[售出订单列表](https://www.eldorado.gg/dashboard/orders/sold)，将排序设为 **Recent**。在项目根目录运行以下命令，首次连接时按 Chrome 提示允许。

```bash
venv/bin/python -m tools.monitor_eldorado --sold-orders --chrome-auto-connect
```

程序立即读取列表，随后默认每 30 秒刷新一次。首次成功读取建立起点，当时已有的订单不会写入结果。看到“已写入一笔新订单”时，可在 `downloads/eldorado_new_orders.jsonl` 中查看记录。每笔订单占一行，按 Ctrl+C 可停止监控。

## 新订单判定与验收

| 场景 | 预期行为 |
| --- | --- |
| 首次看到订单或明确的空列表 | 保存当前起点。若列表为空，下一次出现的 Aion 2 订单即可采集。 |
| 后续刷新 | 只判断 Recent 排序下的第一笔。用该笔下单时间推进起点，只有它是新的 Aion 2 或 Aion2 订单才入队，匹配忽略大小写。 |
| 多笔订单同秒下单 | 除比较下单时间，还记录该秒已见的订单号。之后出现的同秒新订单号仍可入队，同一订单号只写一行。 |
| 详情加载较慢或暂时缺字段 | 列表仍按周期刷新。逐笔在临时标签页读取详情，核对订单号和游戏；全部必需字段可见后写入。未成功读取的订单保留在队列中，下一轮继续尝试。 |
| 排序、下单时间或页面加载异常 | 本次列表读取失败时保留先前的起点。登录失效后，在同一浏览器会话登录并返回售出订单列表，后续刷新继续采集。 |
| 停止后使用同一输出路径重启 | 从已保存的起点和待处理队列继续。写文件中途退出时，能恢复和补齐已登记的最后一行。 |

如果监控使用的列表标签页进入订单详情，程序会在同一会话另开售出列表继续刷新，方便查看原详情。每轮至多发现一笔新单。两次刷新之间出现多笔订单时，只判断刷新时排在第一笔的订单。

## 输出与恢复

查看汇总请打开 `downloads/eldorado_new_orders.json`。该文件在启动恢复和每次录入后更新，包含所有已录入订单的 JSON 数组。自定义输出以 `.jsonl` 结尾时，汇总换成 `.json` 后缀，否则追加 `.json`。

每行包含 `order_id`（订单号）、`ordered_at`（Ordered 下单时间，按浏览器时区解析后转换为 UTC）、`game`、`region`、`faction`、`server`、`quantity`、`buyer`、`order_price`、`commission` 和 `you_receive`。数量、订单金额、佣金和实收金额保留详情页显示的值及单位。

状态库 `downloads/eldorado_new_orders.jsonl.sqlite3` 记录起点、待处理订单和已写位置。恢复监控时保留 JSONL 与对应的状态库；备份这两个文件前先停止监控。使用 `--output` 指定其他文件时，状态库名称为同一路径加 `.sqlite3`。同一个输出文件仅允许一个写进程。

## 读取聊天记录

要监控当前列表页所有 Aion 2 订单的聊天，使用以下命令。两种列表模式都可用 `--interval` 调整刷新间隔。

```bash
mkdir -p downloads
venv/bin/python -m tools.monitor_eldorado --sold-chats --chrome-auto-connect --output downloads/eldorado_aion2_chats.jsonl
```

程序逐笔读取 TalkJS 中可加载的历史消息，在首次读取或聊天发生变化时输出该订单的对话快照。快照包含订单详情链接、UTC 采集时间 `captured_at`、消息正文及可辨认的时间、方向和附件名。聊天模式的去重范围是本次运行，重启后会重新输出首份快照。

要观察手动打开的单笔订单详情，可省略两种列表模式参数，默认每 3 秒检查一次该页面。
