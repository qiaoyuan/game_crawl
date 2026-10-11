# 项目指令

@/Users/qiaoyuan/.codex/RTK.md

## 项目 skill

修改或排查数据库任务爬虫、配置、SQL、分片、加强、通知、启动和部署时，先读取 [.kiro/skills/g2g-crawl-dev/SKILL.md](.kiro/skills/g2g-crawl-dev/SKILL.md)。这是本项目生产爬虫规范的唯一维护源。

修改独立抓取、登录、会话和导出工具时，另读 [.codebuddy/skills/g2g-scraper/SKILL.md](.codebuddy/skills/g2g-scraper/SKILL.md)。其独立工具约定不能覆盖数据库生产爬虫的约定。

## 每次改动后同步 skill

完成 `g2g/**`、`tools/**`、`tests/**`、`sql/**`、`main.py`、启动/部署脚本、依赖或环境配置的相关修改后，在回复用户之前检查本次 diff 和最终源码，直接同步受影响 skill。

- 更新实际改变的配置默认值、字段、关联、分类分流、抓取范围、加强选择、价格来源、通知顺序、失败行为、锁、日志和执行方式。
- 替换过时段落，保留仍成立的规则；删除文件时检查相关引用。仅维护已确认行为，不把计划或未经验证的服务器状态写成事实，不追加改动流水账。
- 纯格式、注释或现有规范仍准确时，只需核对，不强制改写。
- 不把账号、密码、令牌、会话、真实环境凭据或业务记录复制到 skill。
- 改动涉及 PHP 后台接口或字段时，核对关联后台项目的规范和调用方；不要将旧手工 CrawlService 与本项目生产爬虫混为一谈。
- 用户明确要求只读或限制文档修改时遵守范围，并说明需要后续同步的规范。

## 自动维护 hook

[.kiro/hooks/sync-crawl-skills.json](.kiro/hooks/sync-crawl-skills.json) 按 Kiro IDE 1.x / CLI V3 的 hook 格式配置 `PostFileCreate`、`PostFileSave`、`PostFileDelete`，在代理改动相关源码后提示同步 skill。匹配范围排除 skill、hook、AGENTS.md、日志、会话及虚拟环境，避免递归。

这是 Kiro 的会话事件配置，不是后台文件监视器，也不是 Codex 或 CodeBuddy 的原生 hook。Codex 在本项目依照上面的“每次改动后同步 skill”规则维护；人工编辑、外部脚本或其他 IDE 的改动不能假定触发 Kiro hook，后续任务应结合 diff 补齐规范。
