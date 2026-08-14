-- 爬取数据表
CREATE TABLE IF NOT EXISTS `game_platform`.`crawl_data` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `target_id` BIGINT UNSIGNED NOT NULL COMMENT '关联 crawl_target.id',
  `game_product_id` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '关联游戏产品ID',
  `platform` VARCHAR(32) NOT NULL DEFAULT 'g2g' COMMENT '平台: g2g/eldorado',

  -- 卖家信息
  `seller_id` VARCHAR(128) DEFAULT NULL COMMENT '店铺唯一标识',
  `seller_name` VARCHAR(128) DEFAULT NULL COMMENT '店铺显示名',
  `seller_level` VARCHAR(32) DEFAULT NULL COMMENT '卖家等级',
  `seller_url` VARCHAR(512) DEFAULT NULL COMMENT '店铺链接',
  `is_online` TINYINT(1) DEFAULT 0 COMMENT '是否在线',

  -- 商品信息
  `product_title` VARCHAR(512) DEFAULT NULL COMMENT '产品标题',
  `offer_url` VARCHAR(512) DEFAULT NULL COMMENT '产品链接',

  -- 核心数据
  `sold_count` VARCHAR(64) DEFAULT NULL COMMENT '已售数量（原始文本）',
  `sold_count_num` INT DEFAULT NULL COMMENT '已售数量（数字）',
  `stock` VARCHAR(32) DEFAULT NULL COMMENT '库存（原始文本 119k/5.1M）',
  `stock_num` BIGINT DEFAULT NULL COMMENT '库存（数字）',
  `price` DECIMAL(20,8) DEFAULT NULL COMMENT '单价',
  `currency` VARCHAR(16) DEFAULT NULL COMMENT '货币单位',
  `min_order` VARCHAR(64) DEFAULT NULL COMMENT '最低起订（原始文本）',
  `delivery_time` VARCHAR(64) DEFAULT NULL COMMENT '交货时间',

  -- 原始数据
  `raw_data` JSON DEFAULT NULL COMMENT '完整原始数据',

  -- 时间
  `crawled_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '爬取时间',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',

  PRIMARY KEY (`id`),
  KEY `idx_target_id` (`target_id`),
  KEY `idx_game_product_id` (`game_product_id`),
  UNIQUE KEY `uk_target_game_product_seller` (`target_id`, `game_product_id`, `seller_id`),
  KEY `idx_platform_seller` (`platform`, `seller_id`),
  KEY `idx_crawled_at` (`crawled_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='爬取数据表';
