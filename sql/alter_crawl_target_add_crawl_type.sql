-- 先执行迁移，再部署后台和 Python 爬虫。历史目标保持 0（默认），按需切换 1（店铺加强）。
ALTER TABLE `crawl_target`
  ADD COLUMN `crawl_type` tinyint unsigned NOT NULL DEFAULT 0
  COMMENT '爬虫类型 0-默认 1-绑定店铺详情加强' AFTER `category`;
