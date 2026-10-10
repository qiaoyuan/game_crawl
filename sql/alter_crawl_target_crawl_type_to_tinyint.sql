-- 仅用于已添加 varchar crawl_type 的数据库；未添加字段时使用 add_crawl_type 迁移。
-- 暂停相关写入并在维护窗口执行，保留已有 default/top3 选择。
UPDATE `crawl_target`
SET `crawl_type` = CASE
  WHEN `crawl_type` IN ('top3', '1') THEN '1'
  ELSE '0'
END;

ALTER TABLE `crawl_target`
  MODIFY COLUMN `crawl_type` tinyint unsigned NOT NULL DEFAULT 0
  COMMENT '爬虫类型 0-默认 1-按策略过滤后最低三条' AFTER `category`;
