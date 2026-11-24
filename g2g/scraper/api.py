"""G2G API 调用"""

import json
import requests
from g2g import config


def bulk_export(token: str, payload: dict = None) -> dict:
    """调用 bulk_export API 导出 offers"""
    resp = requests.post(
        config.BULK_EXPORT_URL,
        headers=config.api_headers(token),
        json=payload or config.EXPORT_PAYLOAD,
        timeout=30,
    )
    return resp.json()


def download_file(url: str, filename: str = "offers_export.zip") -> str | None:
    """下载文件到 downloads 目录"""
    import os
    resp = requests.get(url, timeout=60)
    if resp.status_code == 200:
        filepath = os.path.join(config.DOWNLOAD_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(resp.content)
        print(f"[✓] 下载完成: {filepath} ({len(resp.content):,} bytes)")
        return filepath
    else:
        print(f"[!] 下载失败: {resp.status_code}")
        return None


def get_seller_offers(token: str, seller_id: str = None, page: int = 1, page_size: int = 20) -> dict:
    """获取卖家 offers 列表"""
    sid = seller_id or config.SELLER_ID
    resp = requests.get(
        f"{config.API_BASE}/offer/seller/{sid}",
        headers=config.api_headers(token),
        params={"page": page, "page_size": page_size},
        timeout=30,
    )
    return resp.json()


def get_seller_info(token: str, seller_id: str = None) -> dict:
    """获取卖家信息"""
    sid = seller_id or config.SELLER_ID
    resp = requests.get(
        f"{config.API_BASE}/user/seller/{sid}",
        headers=config.api_headers(token),
        timeout=30,
    )
    return resp.json()


def api_get(token: str, path: str, params: dict = None) -> dict:
    """通用 GET 请求"""
    resp = requests.get(
        f"{config.API_BASE}{path}",
        headers=config.api_headers(token),
        params=params,
        timeout=30,
    )
    return resp.json()


def api_post(token: str, path: str, data: dict = None) -> dict:
    """通用 POST 请求"""
    resp = requests.post(
        f"{config.API_BASE}{path}",
        headers=config.api_headers(token),
        json=data,
        timeout=30,
    )
    return resp.json()
