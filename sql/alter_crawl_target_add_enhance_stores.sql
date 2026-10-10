-- 先执行迁移，再发布后台和 Python 爬虫。空名单不读取详情，前10条全部入库。
ALTER TABLE `crawl_target`
  ADD COLUMN `enhance_stores` varchar(2048) NOT NULL DEFAULT ''
  COMMENT '加强店铺名，多行或逗号分隔，仅crawl_type=1生效' AFTER `crawl_type`;
