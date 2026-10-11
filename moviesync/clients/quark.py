"""夸克网盘 API 客户端。

用途：校验 Cookie、解析分享文件、创建分类/影视专属目录，以及执行白名单文件转存。
维护说明：分享解析限制递归深度和文件数量；转存只接收经过服务端重新解析并校验过的文件 FID。
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

from .http import ApiError, HttpClient

QUARK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://pan.quark.cn/",
    "Origin": "https://pan.quark.cn",
    "Content-Type": "application/json",
}
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".ts", ".m2ts", ".iso")


def sanitize_pwd_id(pwd_id: object) -> str:
    if not pwd_id:
        return ""
    value = str(pwd_id).strip()
    match = re.search(r"quark\.cn/s/([A-Za-z0-9]+)", value, re.IGNORECASE)
    if match:
        return match.group(1)
    if "://" in value or "/" in value or "?" in value:
        return ""
    return value if re.fullmatch(r"[A-Za-z0-9]{1,128}", value) else ""


def clean_tv_filename(raw_name: str, title: str = "") -> tuple[int | None, str]:
    """兼容旧调用方；集数判断统一委托给共享文件名规则。"""
    if not raw_name:
        return None, raw_name

    # 延迟导入以避免 clients.quark 与卡片注册初始化期间形成循环导入。
    from ..services.filename_rules import parse_tv_episode

    _season, episode = parse_tv_episode(raw_name)
    return episode, raw_name


class QuarkClient:
    def __init__(self, cookie: str):
        cookie = "".join((cookie or "").splitlines()).strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie[7:].strip()
        self.cookie = cookie
        headers = {**QUARK_HEADERS, "Cookie": cookie}
        self.http = HttpClient(headers)

    def _request_json(self, *args, **kwargs):
        """Make refreshed Quark session cookies available to subsequent API calls."""
        response, data = self.http.request_json(*args, **kwargs)
        response_cookies = getattr(response, "cookies", None)
        get_dict = getattr(response_cookies, "get_dict", None)
        if callable(get_dict):
            refreshed = get_dict()
            if isinstance(refreshed, dict):
                updates = {key: str(refreshed[key]) for key in ("__pus", "__puus") if key in refreshed}
                if updates:
                    pairs = {}
                    for part in self.cookie.split(";"):
                        name, sep, value = part.strip().partition("=")
                        if sep and name:
                            pairs[name] = value
                    pairs.update(updates)
                    self.cookie = "; ".join(f"{name}={value}" for name, value in pairs.items())
                    self.http.headers["Cookie"] = self.cookie
                    session = getattr(getattr(self.http, "_local", None), "session", None)
                    if session is not None:
                        session.headers["Cookie"] = self.cookie
        return response, data

    def get_cookie(self) -> str:
        """Return the current Cookie header, including refreshed Quark session values."""
        return self.cookie

    def check_cookie_valid(self) -> bool:
        if not self.cookie:
            return False
        try:
            response, data = self._request_json(
                "GET",
                "https://drive.quark.cn/1/clouddrive/file/sort?pr=ucpro&fr=pc&pdir_fid=0&num=1",
                timeout=6,
                retries=1,
            )
            return response.status_code == 200 and data.get("code") == 0
        except ApiError:
            return False

    def _list_children(self, parent_fid: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while page <= 20:
            url = (
                "https://drive.quark.cn/1/clouddrive/file/sort"
                f"?pr=ucpro&fr=pc&pdir_fid={quote(str(parent_fid))}&p={page}&num=200"
            )
            try:
                response, data = self._request_json("GET", url, timeout=6, retries=1)
            except ApiError:
                break
            if response.status_code != 200 or data.get("code") != 0:
                break
            batch = data.get("data", {}).get("list", []) or []
            items.extend(batch)
            if len(batch) < 200:
                break
            page += 1
        return items

    def get_or_create_subfolder(self, folder_name: str, parent_fid: str = "0") -> tuple[str | None, str | None]:
        parent_fid = str(parent_fid or "0").strip()
        folder_name = str(folder_name or "").strip()
        if not folder_name:
            return None, "文件夹名称不能为空"

        for item in self._list_children(parent_fid):
            is_dir = item.get("dir_file") is True or item.get("file_type") == 0
            if is_dir and item.get("file_name") == folder_name and item.get("fid"):
                return str(item["fid"]), None

        url = "https://drive-pc.quark.cn/1/clouddrive/file?pr=ucpro&fr=pc"
        payload = {"pdir_fid": parent_fid, "file_name": folder_name, "dir_init_lock": False, "dir_path": ""}
        error_message = "创建目录失败"
        try:
            response, data = self._request_json("POST", url, timeout=8, retries=0, json=payload)
            if response.status_code == 200 and data.get("code") == 0:
                obj = data.get("data") or {}
                fid = obj.get("fid") or obj.get("file_id") or data.get("fid")
                if fid:
                    return str(fid), None
            error_message = str(data.get("message") or f"HTTP {response.status_code}")
        except ApiError as exc:
            error_message = str(exc)

        for item in self._list_children(parent_fid):
            is_dir = item.get("dir_file") is True or item.get("file_type") == 0
            if is_dir and item.get("file_name") == folder_name and item.get("fid"):
                return str(item["fid"]), None
        return None, error_message

    def list_drive_files(self, parent_fid: object = "0") -> list[dict[str, Any]]:
        """列出本人夸克网盘目录，不读取分享链接。"""
        parent = str(parent_fid or "0").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parent):
            raise ValueError("目录 FID 格式无效")
        normalized = []
        for item in self._list_children(parent):
            fid = str(item.get("fid") or "").strip()
            name = str(item.get("file_name") or "").strip()
            if not fid or not name:
                continue
            is_dir = item.get("dir_file") is True or item.get("file_type") == 0
            is_video = item.get("category") == 1 or name.lower().endswith(VIDEO_EXTENSIONS)
            normalized.append({
                "fid": fid, "file_name": name, "is_dir": bool(is_dir),
                "is_video": bool(not is_dir and is_video),
                "size": item.get("size") or 0,
                "updated_at": item.get("updated_at") or item.get("l_updated_at") or 0,
            })
        return normalized

    def get_playback_info(self, fid: object) -> dict[str, Any]:
        """通过夸克转码播放接口解析短时视频地址，不向浏览器暴露 Cookie。"""
        from urllib.parse import urlparse

        file_id = str(fid or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", file_id):
            raise ValueError("文件 FID 格式无效")
        url = "https://drive.quark.cn/1/clouddrive/file/v2/play/project?pr=ucpro&fr=pc"
        payload = {
            "fid": file_id,
            "resolutions": "low,normal,high,super,2k,4k",
            "supports": "fmp4_av,m3u8,dolby_vision",
        }
        try:
            response, data = self._request_json("POST", url, timeout=12, retries=1, json=payload)
        except ApiError as exc:
            raise RuntimeError(f"获取夸克播放地址失败：{exc}") from exc
        if response.status_code != 200 or str(data.get("code")) != "0":
            # Quark's current PC transcode endpoint can reject valid browser-cookie
            # sessions with 14018/plf_invalid. Fall back to its authenticated
            # original-file download URL instead of failing playback outright.
            upstream_message = str(data.get("message") or data.get("msg") or "")
            upstream_message = upstream_message.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
            upstream_message = upstream_message[:160]
            is_transcode_platform_error = (
                str(data.get("code")) == "14018"
                or "plf_invalid" in upstream_message.lower()
            )
            if is_transcode_platform_error:
                download_url = (
                    "https://drive-pc.quark.cn/1/clouddrive/file/download"
                    "?pr=ucpro&fr=pc&sys=win32&ve=2.5.56&ut=&guid="
                )
                try:
                    download_response, download_data = self._request_json(
                        "POST",
                        download_url,
                        timeout=12,
                        retries=1,
                        json={"fids": [file_id]},
                    )
                except ApiError as exc:
                    raise RuntimeError(f"夸克转码不可用，获取原始视频地址失败：{exc}") from exc

                # The web-style download endpoint rejects large files with 23018.
                # Retry only that specific response through the Quark PC-client endpoint,
                # which uses the documented PC UA and client version parameters.
                if str(download_data.get("code")) == "23018":
                    pc_download_url = (
                        "https://drive-pc.quark.cn/1/clouddrive/file/download"
                        "?pr=ucpro&fr=pc&sys=win32&ve=6.9.7.761"
                    )
                    pc_headers = {
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0.0.0 Safari/537.36 QuarkPC/6.9.7.761 "
                            "QuarkCloudDrivePC/6.9.7.761 quark-cloud-drive/2.5.40"
                        ),
                        "Referer": "https://pan.quark.cn/",
                        "Origin": "https://pan.quark.cn",
                    }
                    # The PC-client flow may require a short-lived social download token.
                    now = int(time.time())
                    token_url = (
                        "https://drive-social-api.quark.cn/1/clouddrive/chat/conv/file/acquire_dl_token"
                        "?pr=ucpro&fr=pc&sys=win32&ve=6.9.7.761"
                        "&fr=win&la=zh-CN&ch=pckk%40product_guanwan"
                    )
                    download_token = ""
                    try:
                        _, token_data = self._request_json(
                            "POST",
                            token_url,
                            timeout=8,
                            retries=0,
                            headers=pc_headers,
                            json={
                                "conversation_id": f"300000{now}",
                                "conversation_type": 3,
                                "msg_id": f"{now}000",
                            },
                        )
                        token_obj = token_data.get("data")
                        if isinstance(token_obj, dict):
                            download_token = str(token_obj.get("token") or "")
                    except ApiError:
                        # Some sessions still allow PC-link retrieval without this token.
                        pass

                    try:
                        download_response, download_data = self._request_json(
                            "POST",
                            pc_download_url,
                            timeout=12,
                            retries=1,
                            headers=pc_headers,
                            json={
                                "fids": [file_id],
                                "speedup_session": "",
                                "token": download_token,
                            },
                        )
                    except ApiError as exc:
                        raise RuntimeError(
                            f"夸克网页下载接口限制文件大小，PC 下载接口请求失败：{exc}"
                        ) from exc

                download_items = download_data.get("data")
                download_item = (
                    download_items[0]
                    if isinstance(download_items, list)
                    and download_items
                    and isinstance(download_items[0], dict)
                    else {}
                )
                original_url = str(download_item.get("download_url") or "").strip()
                parsed_url = urlparse(original_url)
                if (
                    download_response.status_code == 200
                    and str(download_data.get("code")) == "0"
                    and parsed_url.scheme == "https"
                    and parsed_url.hostname
                ):
                    file_name = str(download_item.get("file_name") or "")
                    suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
                    mime_type = {
                        "m3u8": "application/vnd.apple.mpegurl",
                        "mp4": "video/mp4",
                        "m4v": "video/mp4",
                        "mov": "video/quicktime",
                        "webm": "video/webm",
                    }.get(suffix, "video/mp4")
                    return {
                        "url": original_url,
                        "file_name": file_name,
                        "resolution": "原始文件",
                        "format": suffix or "original",
                        "size": download_item.get("size") or 0,
                        "mime_type": mime_type,
                    }

                download_message = str(
                    download_data.get("message") or download_data.get("msg") or ""
                ).replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()[:160]
                details = [
                    f"HTTP {download_response.status_code}",
                    f"code={download_data.get('code', 'missing')}",
                ]
                if download_message:
                    details.append(f"message={download_message}")
                raise RuntimeError(
                    "夸克转码地址不可用，原始视频地址获取失败（"
                    + ", ".join(details)
                    + "）"
                )

            details = [f"HTTP {response.status_code}", f"code={data.get('code', 'missing')}"]
            if upstream_message:
                details.append(f"message={upstream_message}")
            raise RuntimeError("夸克播放接口拒绝请求（" + ", ".join(details) + "）")
        detail = data.get("data") if isinstance(data.get("data"), dict) else {}
        video_list = detail.get("video_list") if isinstance(detail.get("video_list"), list) else []
        candidates = []
        for item in video_list:
            if not isinstance(item, dict):
                continue
            info = item.get("video_info") if isinstance(item.get("video_info"), dict) else {}
            play_url = str(info.get("url") or "").strip()
            parsed = urlparse(play_url)
            if not play_url or parsed.scheme != "https" or not parsed.hostname:
                continue
            candidates.append({
                "url": play_url,
                "resolution": str(item.get("resolution") or info.get("resolution") or ""),
                "format": str(info.get("format") or ""),
                "size": info.get("size") or detail.get("size") or 0,
                "mime_type": "application/vnd.apple.mpegurl" if parsed.path.lower().endswith(".m3u8") else "video/mp4",
            })
        if not candidates:
            raise RuntimeError("夸克没有返回可用的 HTTPS 视频地址，请确认文件已转存且夸克账号可播放")
        selected = next(
            (item for item in candidates if not item["url"].split("?", 1)[0].lower().endswith(".m3u8")),
            candidates[0],
        )
        return {"file_name": str(detail.get("file_name") or ""), **selected}

    def get_share_files(
        self,
        pwd_id: object,
        max_depth: int = 3,
        max_files: int = 5000,
        passcode: str = "",
    ) -> tuple[
        list[dict[str, Any]] | None,
        str | None,
        str | None,
    ]:
        pwd_id = sanitize_pwd_id(pwd_id)
        if not pwd_id:
            return None, None, "分享链接 ID 无效"

        token_url = "https://drive.quark.cn/1/clouddrive/share/sharepage/token"
        try:
            response, data = self._request_json(
                "POST",
                token_url,
                timeout=8,
                retries=1,
                json={"pwd_id": pwd_id, "passcode": str(passcode or "").strip()},
            )
            if response.status_code == 404:
                return None, None, "分享链接已失效或已被删除"
            if response.status_code != 200 or data.get("code") != 0:
                return None, None, str(data.get("message") or f"获取 Token 失败（HTTP {response.status_code}）")
            stoken = (data.get("data") or {}).get("stoken")
        except ApiError as exc:
            return None, None, f"请求 Token 失败: {exc}"

        if not stoken:
            return None, None, "上游未返回 stoken"

        all_files: list[dict[str, Any]] = []
        visited: set[str] = set()
        walk_error: str | None = None

        def walk(parent_fid: str, depth: int) -> None:
            nonlocal walk_error

            if (
                walk_error
                or depth > max_depth
                or len(all_files) >= max_files
                or parent_fid in visited
            ):
                return

            visited.add(parent_fid)
            page = 1

            while (
                page <= 20
                and len(all_files) < max_files
                and not walk_error
            ):
                url = (
                    "https://drive.quark.cn/1/clouddrive/share/sharepage/detail"
                    f"?pr=ucpro&fr=pc&pwd_id={quote(pwd_id)}"
                    f"&stoken={quote(stoken, safe='')}"
                    f"&pdir_fid={quote(str(parent_fid))}&p={page}&num=200"
                )

                try:
                    response, payload = self._request_json(
                        "GET",
                        url,
                        timeout=8,
                        retries=1,
                    )
                except ApiError as exc:
                    walk_error = f"读取分享文件列表失败: {exc}"
                    return

                if response.status_code != 200 or payload.get("code") != 0:
                    walk_error = str(
                        payload.get("message")
                        or f"读取分享文件列表失败（HTTP {response.status_code}）"
                    )
                    return

                batch = (
                    payload.get("data", {}).get("list", [])
                    or []
                )

                for item in batch:
                    if (
                        item.get("dir_file") is True
                        or item.get("file_type") == 0
                    ):
                        fid = item.get("fid")
                        if fid:
                            walk(str(fid), depth + 1)
                    else:
                        all_files.append(item)
                        if len(all_files) >= max_files:
                            return

                if len(batch) < 200:
                    break

                page += 1

        walk("0", 0)

        if walk_error:
            return None, None, walk_error

        return all_files, str(stoken), None

    def save_files(
        self,
        pwd_id: object,
        files_to_save: list[dict[str, Any]],
        stoken: str | None,
        target_fid: object = "0",
    ) -> tuple[bool, str]:
        pwd_id = sanitize_pwd_id(pwd_id)
        if not pwd_id:
            return False, "分享链接 ID 无效"
        if not stoken:
            return False, "缺少分享 Token"
        fid_list: list[str] = []
        seen: set[str] = set()

        for item in (files_to_save or [])[:200]:
            if not isinstance(item, dict):
                continue

            fid = str(item.get("fid") or "").strip()

            if (
                fid
                and len(fid) <= 128
                and re.fullmatch(r"[A-Za-z0-9_-]+", fid)
                and fid not in seen
            ):
                seen.add(fid)
                fid_list.append(fid)

        if not fid_list:
            return False, "没有可转存的有效文件"

        target_fid = str(target_fid or "0").strip()
        if (
            len(target_fid) > 128
            or not re.fullmatch(r"[A-Za-z0-9_-]+", target_fid)
        ):
            return False, "目标目录 FID 格式无效"

        url = "https://drive.quark.cn/1/clouddrive/share/sharepage/save?pr=ucpro&fr=pc"
        payload = {
            "pwd_id": pwd_id,
            "stoken": stoken,
            "fid_list": fid_list,
            "to_pdir_fid": target_fid,
        }
        try:
            response, data = self._request_json("POST", url, timeout=10, retries=0, json=payload)
            if response.status_code == 200 and data.get("code") == 0:
                return True, "转存成功"
            return False, str(data.get("message") or f"转存失败（HTTP {response.status_code}）")
        except ApiError as exc:
            return False, (
                "转存结果不确定：请求可能已到达网盘，请检查目标目录后再决定是否重试。"
                f"详情：{exc}"
            )
