"""数据库操作"""

import pymysql
from datetime import datetime
from g2g import config


def get_connection():
    """获取数据库连接"""
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_pending_targets() -> list:
    """获取待爬取的目标 URL 列表（status=1 且未删除）"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM crawl_target WHERE status = 1 AND deleted_at IS NULL"
            )
            return cur.fetchall()
    finally:
        conn.close()


def update_last_crawl(target_id: int):
    """更新 crawl_target 的最后爬取时间"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crawl_target SET last_crawl_at = NOW() WHERE id = %s",
                (target_id,),
            )
        conn.commit()
    finally:
        conn.close()


def increment_version(target_id: int) -> int:
    """将 crawl_target.version +1，返回更新后的新版本号"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crawl_target
                SET version = COALESCE(version, 0) + 1
                WHERE id = %s
                """,
                (target_id,),
            )
            cur.execute(
                "SELECT version FROM crawl_target WHERE id = %s",
                (target_id,),
            )
            row = cur.fetchone()
        conn.commit()
        return int(row["version"]) if row else 1
    finally:
        conn.close()


def save_crawl_data(
    target_id: int,
    platform: str,
    items: list,
    game_product_id: int = 0,
    version: int = 0,
) -> int:
    """批量插入爬取数据，每条记录携带本次的 version，返回插入条数。"""
    if not items:
        return 0

    conn = get_connection()
    inserted = 0
    try:
        with conn.cursor() as cur:
            sql = """
                INSERT INTO crawl_data (
                    target_id, game_product_id, platform, version,
                    seller_id, seller_name, seller_level, seller_url, is_online,
                    product_title, offer_url,
                    sold_count, sold_count_num,
                    stock, stock_num,
                    price, currency,
                    min_order, delivery_time,
                    raw_data, crawled_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s
                )
            """
            now = datetime.now()
            for item in items:
                import json
                row = (
                    target_id,
                    game_product_id,
                    platform,
                    version,
                    item.get("seller_id"),
                    item.get("seller_name"),
                    item.get("seller_level"),
                    item.get("seller_url"),
                    1 if item.get("is_online") else 0,
                    item.get("product_title"),
                    item.get("offer_url"),
                    item.get("sold_count"),
                    parse_sold_count(item.get("sold_count")),
                    item.get("stock"),
                    parse_stock(item.get("stock")),
                    parse_price(item.get("price")),
                    item.get("currency"),
                    item.get("min_order"),
                    item.get("delivery_time"),
                    json.dumps(item, ensure_ascii=False),
                    now,
                )
                cur.execute(sql, row)
                inserted += 1
        conn.commit()
        return inserted
    finally:
        conn.close()


def parse_sold_count(text: str | None) -> int | None:
    """解析已售数量: '1 Sold' -> 1, '17,010 Sold' -> 17010"""
    if not text:
        return None
    import re
    m = re.search(r"([\d,]+)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def parse_stock(text: str | None) -> int | None:
    """解析库存: '119k' -> 119000, '5.1M' -> 5100000"""
    if not text:
        return None
    text = text.strip().replace(",", "")
    import re
    m = re.match(r"^([\d.]+)([kKmM]?)$", text)
    if not m:
        return None
    num = float(m.group(1))
    suffix = m.group(2).lower()
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    return int(num)


def parse_price(text: str | None) -> float | None:
    """解析价格: '0.00073' -> 0.00073"""
    if not text:
        return None
    try:
        return float(text.replace(",", ""))
    except (ValueError, AttributeError):
        return None


# ==================== 爬取完成通知 ====================
# 架构：Python 只负责爬取 + 发信号。爬完一个目标写一条 crawl_notify(status=0)，
#       PHP 侧 `php think price:strategy:consume` 消费通知后执行改价策略并在 PHP 侧改价。
#       改价不在 Python 做。


def insert_crawl_notify(target_id: int, crawled_count: int):
    """爬完一个目标写一条通知，交给 PHP 消费执行改价策略"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO crawl_notify
                    (crawl_target_id, crawled_count, status, crawled_at, created_at, updated_at)
                VALUES (%s, %s, 0, NOW(), NOW(), NOW())
                """,
                (target_id, crawled_count),
            )
        conn.commit()
    finally:
        conn.close()
