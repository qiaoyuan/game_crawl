"""将 DD373 商品列表短码映射到官方行情接口的游戏、商品与区服参数。"""

import re
from urllib.parse import urlsplit


CATALOG = "https://game.dd373.com"
LIST_PATH = re.compile(r"/s-([a-z0-9]+)-c-([a-z0-9]+)-([a-z0-9]+(?:-[a-z0-9]+)*)\.html", re.ASCII)


def parse_dd373_url(url: str):
    parts = urlsplit(url)
    match = LIST_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.netloc.lower() != "www.dd373.com"
            or parts.query or parts.fragment or not match):
        raise ValueError("请使用含商品类型和区服的 DD373 普通商品列表原始链接")
    game, goods, region = match.groups()
    return game, goods, region.split("-")


def dd373_result(payload):
    """两个业务状态均成功才有有效数据，包括成功的空列表。"""
    if isinstance(payload, dict) and str(payload.get("StatusCode")) == "0":
        status = payload.get("StatusData")
        if isinstance(status, dict) and str(status.get("ResultCode")) == "0" and "ResultData" in status:
            return status["ResultData"]
    raise ValueError("DD373 接口返回失败或缺少 ResultData")


def _entry(rows, code, key, parent=None):
    if not isinstance(rows, list):
        raise ValueError("DD373 游戏目录格式异常")
    matches = [row for row in rows if isinstance(row, dict) and row.get(key) == code
               and (parent is None or row.get("ParentId") == parent)]
    if len(matches) != 1 or not matches[0].get("Id") or matches[0].get("IsClose"):
        raise ValueError(f"无法在 DD373 对应目录唯一匹配短码 {code}")
    return matches[0]


async def _region_children(get_json, parent):
    group = dd373_result(await get_json(CATALOG + "/Api/GameOther/List", {"parentId": parent}))
    if not isinstance(group, dict) or not isinstance(group.get("GameOther"), list):
        raise ValueError("DD373 区服目录格式异常")
    return group["GameOther"]


async def resolve_dd373_route(url, get_json):
    """逐级核对目录归属。返回 ApiGetShopList 参数及后台区服展示名称。"""
    game_code, goods_code, region_codes = parse_dd373_url(url)
    games = dd373_result(await get_json(CATALOG + "/api/game/list", {}))
    game_id = _entry(games, game_code, "Identify")["Id"]
    types = dd373_result(await get_json(CATALOG + "/Api/GameGoodsType/List", {"parentId": game_id}))
    goods = _entry(types.get("GameGoodsType") if isinstance(types, dict) else None,
                   goods_code, "Identtify", game_id)
    if goods.get("Property") != "游戏币":
        raise ValueError("DD373 收货报价仅支持游戏币商品")

    parent = game_id
    ids, names = [], []
    for code in region_codes:
        row = _entry(await _region_children(get_json, parent), code, "Identify", parent)
        name = row.get("Name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("DD373 区服目录缺少名称")
        parent = row["Id"]
        ids.append(parent)
        names.append(name.strip())
    if await _region_children(get_json, parent):
        raise ValueError("请在 DD373 列表选择具体服务器后保存链接")
    return {"GameId": game_id, "GameShopTypeId": goods["Id"], "GameOtherId": "_".join(ids)}, "/".join(names)
