# 项目指令

@/Users/qiaoyuan/.codex/RTK.md

## 规格驱动开发（SDD）

所有开发任务都依据规格（spec）实施。开始写代码前，先找到对应功能的 spec。已有规格覆盖本次需求时直接复用；新增功能或改变行为时，先在 `docs/` 新建或更新对应规格，写清适用范围、输入、预期输出、异常行为和可验证的验收条件。修复缺陷时，以规格中的预期行为作为回归依据。

按 spec 实现，在对应 skill 的测试目录验证验收条件。开发中发现需求与 spec 不一致时，先澄清差异、更新规格，再继续实现。验收后按下文规则把已实现的行为同步到 skill。

## 网页抓取设计：逐页询问

新增或调整抓取流程时，按用户的实际操作顺序设计。每次只讨论当前页面的当前步骤，提出一个具体问题，等用户回答后，把已确认的规则记入 spec，再询问下一步。用户已经明确的步骤直接记录。

例如设计“售出列表 → 订单详情 → 聊天”时，先确认列表上哪一笔订单应进入详情；这一步确定后，再确认详情页要读取哪些字段；最后确认聊天范围及什么变化需要输出。当前页面的入口、读取范围和进入下一页的条件都明确后，再推进到下一页。按已确认的 spec 执行既有采集流程时，直接使用其中记录的步骤。

## 项目 skill

修改或排查数据库任务爬虫、配置、SQL、分片、加强、通知、启动和部署时，先读取 [.kiro/skills/g2g-crawl-dev/SKILL.md](.kiro/skills/g2g-crawl-dev/SKILL.md)。这是本项目生产爬虫规范的唯一维护源。

修改独立抓取、登录、会话和导出工具时，另读 [.codebuddy/skills/g2g-scraper/SKILL.md](.codebuddy/skills/g2g-scraper/SKILL.md)。其独立工具约定不能覆盖数据库生产爬虫的约定。

按指定订单号或详情链接手动采集单笔 Eldorado 订单及其聊天时，使用 [Eldorado 单笔订单采集规范](.agents/skills/eldorado-manual-order/SKILL.md)。修改该工具时同步此规范。

## 模块与测试目录

业务实现按职责归入 `g2g/crawl/`（数据库生产爬虫）、`g2g/scraper/`（独立工具）和 `g2g/eldorado/`。Eldorado 的单笔采集、持续监控及两者共用的页面读取逻辑分别放在 `manual/`、`monitor/`、`shared/`。命令入口保留在 `tools/`。服务器监控脚本支持单文件部署。

所有测试脚本放在 `test/<skill 名称>/`，目录名将 skill 名称中的连字符换成下划线，例如 `test/eldorado_manual_order/`。每个测试文件按负责该行为的 skill 归档，共享行为的测试只保留一份。运行全量测试用 `venv/bin/python -m unittest discover -s test`。

## 每次改动后同步 skill

完成 `g2g/**`、`tools/**`、`test/**`、`sql/**`、`main.py`、启动/部署脚本、依赖或环境配置的相关修改后，在回复用户之前检查本次 diff 和最终源码，直接同步受影响 skill。

- 更新实际改变的配置默认值、字段、关联、分类分流、抓取范围、加强选择、价格来源、通知顺序、失败行为、锁、日志和执行方式。
- 替换过时段落，保留仍成立的规则；删除文件时检查相关引用。仅维护已确认行为，不把计划或未经验证的服务器状态写成事实，不追加改动流水账。
- 纯格式、注释或现有规范仍准确时，只需核对，不强制改写。
- 不把账号、密码、令牌、会话、真实环境凭据或业务记录复制到 skill。
- 改动涉及 PHP 后台接口或字段时，核对关联后台项目的规范和调用方；不要将旧手工 CrawlService 与本项目生产爬虫混为一谈。
- 用户明确要求只读或限制文档修改时遵守范围，并说明需要后续同步的规范。

## 自动维护 hook

[.kiro/hooks/sync-crawl-skills.json](.kiro/hooks/sync-crawl-skills.json) 按 Kiro IDE 1.x / CLI V3 的 hook 格式配置 `PostFileCreate`、`PostFileSave`、`PostFileDelete`，在代理改动相关源码后提示同步 skill。匹配范围排除 skill、hook、AGENTS.md、日志、会话及虚拟环境，避免递归。

这是 Kiro 的会话事件配置，不是后台文件监视器，也不是 CodeBuddy 的原生 hook。项目另有 `.codex/hooks.json`，在 Codex 收到用户请求时提醒核对并同步 skill；钩子需要 Codex 启用并信任后才运行，提醒不能替代最终源码检查。人工编辑、外部脚本或其他 IDE 的改动不能假定触发这些 hook，后续任务应结合 diff 补齐规范。
