"""
RO 的身份卡 — AstrBot 插件
========================
功能：
  从飞鸟快验 WebAPI 读取「身份配置」公共变量（INI 格式），
  根据发送者的 QQ 号匹配配置，渲染磨砂/液态玻璃风格身份卡片图片并发送。

平台支持：
  - aiocqhttp（QQ 个人号）：直接读取 QQ 号
  - qq_official / qq_official_webhook（QQ 官机）：通过 api.czcn.xyz 扫码绑定 openid → uin

命令：
  /idcard              查询自己的身份卡片
  /idcard <qq号>       查询指定 QQ 号的卡片
  /idcard <qq号> frosted   只渲染磨砂玻璃卡片
  /idcard <qq号> liquid    只渲染液态玻璃卡片
  /idcard <qq号> both      渲染两种卡片（默认）
  /绑定qq              官机用户使用，生成 QQ 扫码绑定二维码

INI 配置格式（飞鸟快验「身份配置」公共变量）：
  [2182344375]
  nick=白轩
  acc=BaiXuan|作者
  from=ROTeam|WhiteChestnut
  time=2026-7-29
"""

import io
import os
import re
import base64
import asyncio
import tempfile
from typing import Optional

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# ============================================================
# 硬编码配置
# ============================================================
_FEINIAO_URL = "https://auth.leatiny.icu"
_FEINIAO_TOKEN = "LTGUPRG7TJBIQ2SHPUTD3OQ64CTGEXZV"
_FEINIAO_CONFIG_NAME = "身份配置"

_QQ_AVATAR_URL = "https://q.qlogo.cn/headimg_dl?dst_uin={uin}&spec=640&img_type=jpg"
_QQ_USERINFO_URL = "https://uapis.cn/api/v1/social/qq/userinfo?qq={qq}"

# 点击获取QQ — 生成登录二维码（openid 绑定用）
_QQ_LOGIN_QR_API = "https://api.czcn.xyz/api/qqgjbd?action=get_qrcode"
# 点击获取QQ — 检查登录状态（轮询用）
_QQ_LOGIN_CHECK_API = "https://api.czcn.xyz/api/qqgjbd?action=check_login&code={code}"

_CARD_WIDTH = 520
_CARD_VIEWPORT_HEIGHT = 800

# 官机扫码绑定超时（秒）
_QR_LOGIN_TIMEOUT = 180
# 轮询间隔（秒）
_POLL_INTERVAL = 3

# ============================================================
# INI 解析
# ============================================================
def parse_feiniao_ini(ini_text: str) -> dict:
    """
    解析飞鸟快验「身份配置」INI 文本。
    格式：
      [2182344375]
      nick=白轩
      acc=BaiXuan|作者
      from=ROTeam|WhiteChestnut
      time=2026-7-29
    返回：{qq_str: {nick, acc, from, time, qq}}
    """
    result: dict[str, dict] = {}
    current_key: Optional[str] = None

    for raw_line in ini_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or line.startswith(';'):
            continue

        m = re.match(r'^\[(.+)\]$', line)
        if m:
            current_key = m.group(1).strip()
            if current_key not in result:
                result[current_key] = {}
            continue

        if '=' in line and current_key is not None:
            key, _, value = line.partition('=')
            key = key.strip().lower()
            value = value.strip()
            result[current_key][key] = value

    return result


def get_user_profile(ini_data: dict, identifier: str) -> dict:
    """查找用户档案，优先精确匹配，回退到 default。
    若 identifier 节中含 qq 字段，则合并对应 QQ 号节的档案数据。
    """
    profile = ini_data.get(identifier, {})
    if not profile:
        profile = ini_data.get("default", {})

    # 若当前节含 qq 字段，尝试合并 QQ 号节中的完整档案
    qq_val = profile.get("qq", "")
    if qq_val and str(qq_val).isdigit() and str(qq_val) in ini_data:
        qq_profile = ini_data[str(qq_val)]
        merged = dict(qq_profile)
        merged.update(profile)  # identifier 节的字段优先（如 qq 映射）
        return merged

    return profile


# ============================================================
# 网络请求
# ============================================================
async def fetch_feiniao_data(session: aiohttp.ClientSession) -> Optional[str]:
    """获取飞鸟快验公共变量（INI 文本）"""
    try:
        req_url = f"{_FEINIAO_URL.rstrip('/')}/WebApi/GetPublicData"
        headers = {
            "Content-Type": "application/json",
            "Token": _FEINIAO_TOKEN,
            "Referer": _FEINIAO_URL + "/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64)"
        }
        async with session.post(
            req_url, json={"Name": _FEINIAO_CONFIG_NAME},
            headers=headers, ssl=False,  # 飞鸟快验部分环境可能存在证书问题，暂跳过验证
            timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            data = await resp.json()
            if data.get("code") == 10000:
                return data.get("data")
            msg = data.get("msg", "未知错误")
            code = data.get("code")
            logger.warning(f"飞鸟快验获取失败 (code={code}): {msg}")
            # 返回 None 区分 API 错误与无配置
            return None
    except Exception as e:
        logger.warning(f"飞鸟快验请求异常: {e}")
    return None


async def fetch_qq_avatar_b64(session: aiohttp.ClientSession, uin: str) -> str:
    """获取 QQ 头像并转为 base64 data URI"""
    try:
        async with session.get(
            _QQ_AVATAR_URL.format(uin=uin), ssl=False,  # 腾讯 CDN 部分环境证书异常
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64)"}
        ) as resp:
            if resp.status == 200:
                img_bytes = await resp.read()
                if img_bytes:
                    b64 = base64.b64encode(img_bytes).decode()
                    return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        logger.warning(f"获取 QQ 头像失败: {e}")
    return ""


async def fetch_qq_nickname(session: aiohttp.ClientSession, qq: str) -> str:
    """获取 QQ 昵称（API 失败时返回原始字符串）"""
    try:
        async with session.get(
            _QQ_USERINFO_URL.format(qq=qq), ssl=False,  # uapis.cn 部分环境证书异常
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("nickname") or data.get("nick") or qq
    except Exception:
        pass
    return qq


async def get_qr_login_code(session: aiohttp.ClientSession) -> Optional[dict]:
    """调用 api.czcn.xyz 获取登录二维码 code 和 qr_url"""
    try:
        async with session.get(
            _QQ_LOGIN_QR_API, ssl=False,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 200:
                    return data.get("data", {})
    except Exception as e:
        logger.warning(f"获取 QQ 登录码失败: {e}")
    return None


async def poll_qr_login(session: aiohttp.ClientSession, code: str) -> Optional[int]:
    """轮询 check_login 接口，等待用户扫码，返回 uin 或 None"""
    async def _check_once() -> Optional[int]:
        try:
            async with session.get(
                _QQ_LOGIN_CHECK_API.format(code=code), ssl=False,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 200:
                        d = data.get("data", {})
                        # 兼容多种返回格式：data.uin / data.qq / data
                        uin = d.get("uin") or d.get("qq") if isinstance(d, dict) else d
                        if uin:
                            return int(uin)
        except Exception:
            pass
        return None

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _QR_LOGIN_TIMEOUT
    last_poll_time = 0.0

    while loop.time() < deadline:
        if loop.time() - last_poll_time >= _POLL_INTERVAL:
            last_poll_time = loop.time()
            uin = await _check_once()
            if uin is not None:
                return uin

        await asyncio.sleep(0.5)

    return None


# ============================================================
# QR 码生成
# ============================================================
def generate_qr_image(url: str, size: int = 280) -> bytes:
    """将 URL 生成 QR 码图片，返回 PNG bytes"""
    try:
        import qrcode
        from PIL import Image as PILImage
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        img = img.resize((size, size), resample=PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return _make_placeholder_qr(size)
    except Exception as e:
        logger.warning(f"QR 码生成失败: {e}")
        return _make_placeholder_qr(size)


def _make_placeholder_qr(size: int = 280) -> bytes:
    """生成最小占位 PNG（白底），用于无 qrcode 库时的降级"""
    try:
        from PIL import Image as PILImage
        img = PILImage.new("RGB", (size, size), color="white")
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        cx, cy = size // 2, size // 2
        s = size // 4
        draw.rectangle([cx-s, cy-s, cx+s, cy+s], fill="#cccccc")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        import struct, zlib
        sig = b'\x89PNG\r\n\x1a\n'
        ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
        ihdr_crc = zlib.crc32(b'IHDR' + ihdr) & 0xffffffff
        ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr + struct.pack('>I', ihdr_crc)
        raw = zlib.compress(b'\x00\xff\xff\xff')
        idat_crc = zlib.crc32(b'IDAT' + raw) & 0xffffffff
        idat = struct.pack('>I', len(raw)) + b'IDAT' + raw + struct.pack('>I', idat_crc)
        iend_crc = zlib.crc32(b'IEND') & 0xffffffff
        return sig + ihdr + idat + struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)


# ============================================================
# HTML 卡片模板
# ============================================================
HTML_CARD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500;700&family=Noto+Color+Emoji&family=Noto+Sans+Symbols+2&display=swap');
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Noto Sans SC', 'Noto Sans Symbols 2', 'Microsoft YaHei', 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', sans-serif;
    background: url('https://t.alcy.cc/pc') center/cover no-repeat scroll,
      linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
    min-height: {{ viewport_height }}px;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 50px; padding: 50px 20px;
    -webkit-font-smoothing: antialiased;
  }
  .card-section { position: relative; z-index: 1; display: flex; flex-direction: column; align-items: center; }
  .card-label { color: rgba(255,255,255,0.6); font-size: 14px; font-weight: 300;
    letter-spacing: 2px; text-align: center; margin-bottom: 18px;
    text-transform: uppercase; text-shadow: 0 1px 4px rgba(0,0,0,0.5); }
  .frosted-card, .liquid-card {
    width: {{ card_width }}px; min-height: 300px; padding: 35px;
    display: flex; flex-direction: column; position: relative; overflow: hidden;
  }
  .frosted-card {
    border-radius: 20px;
    backdrop-filter: blur(20px) saturate(180%);
    -webkit-backdrop-filter: blur(20px) saturate(180%);
    background: rgba(15, 15, 35, 0.35);
    border: 1px solid rgba(255,255,255,0.25);
    box-shadow: 0 8px 32px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.3);
  }
  .frosted-card::before {
    content: ''; position: absolute; top: 0; left: 10%;
    width: 80%; height: 2px;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.6), transparent);
  }
  .liquid-card {
    border-radius: 32px;
    backdrop-filter: blur(30px) saturate(200%) brightness(1.1);
    -webkit-backdrop-filter: blur(30px) saturate(200%) brightness(1.1);
    background: linear-gradient(135deg,
      rgba(255,255,255,0.22) 0%, rgba(15,15,35,0.25) 50%, rgba(255,255,255,0.12) 100%);
    border: 1.5px solid rgba(255,255,255,0.35);
    box-shadow: 0 20px 60px rgba(0,0,0,0.35), 0 0 80px rgba(100,150,255,0.15),
      inset 0 1px 1px rgba(255,255,255,0.5), inset 0 -1px 1px rgba(255,255,255,0.1),
      inset 0 0 30px rgba(150,200,255,0.08);
  }
  .liquid-card::before {
    content: ''; position: absolute; top: -50%; left: -10%;
    width: 120%; height: 100%;
    background: radial-gradient(ellipse at center top, rgba(255,255,255,0.2) 0%, transparent 60%);
    pointer-events: none;
  }
  .liquid-card::after {
    content: ''; position: absolute; bottom: -30%; left: 20%;
    width: 60%; height: 80%;
    background: radial-gradient(ellipse at center bottom, rgba(150,200,255,0.12) 0%, transparent 50%);
    pointer-events: none;
  }
  .liquid-card .dispersion-border {
    position: absolute; inset: 0; border-radius: 32px; padding: 2px;
    background: linear-gradient(135deg,
      rgba(255,100,100,0.3), rgba(255,200,100,0.2),
      rgba(100,255,150,0.3), rgba(100,180,255,0.3), rgba(180,100,255,0.2));
    -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude; pointer-events: none;
  }
  .card-header { display: flex; align-items: center; gap: 25px; margin-bottom: 20px; }
  .avatar {
    width: 80px; height: 80px; border-radius: 50%; overflow: hidden;
    border: 2px solid rgba(255,255,255,0.4);
    box-shadow: 0 4px 15px rgba(0,0,0,0.2);
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0; background: linear-gradient(135deg, #667eea, #764ba2);
  }
  .avatar-img { width: 100%; height: 100%; object-fit: cover; }
  .avatar-placeholder { width: 45px; height: 45px; fill: rgba(255,255,255,0.85); }
  .liquid-card .avatar {
    border: 2px solid rgba(255,255,255,0.5);
    box-shadow: 0 4px 20px rgba(168,85,247,0.4), 0 0 30px rgba(100,150,255,0.2), inset 0 1px 2px rgba(255,255,255,0.4);
  }
  .name-block { display: flex; flex-direction: column; gap: 6px; flex: 1; min-width: 0; }
  .name { font-size: 26px; font-weight: 700; color: #fff; text-shadow: 0 1px 4px rgba(0,0,0,0.6); word-break: break-all; }
  .liquid-card .name { text-shadow: 0 1px 4px rgba(0,0,0,0.6), 0 0 20px rgba(150,200,255,0.3); }
  .tags-row { display: flex; flex-wrap: wrap; gap: 6px; }
  .tags-row.small .tag { font-size: 13px; padding: 2px 10px; }
  .tag {
    display: inline-block; font-size: 14px; font-weight: 400;
    color: rgba(255,255,255,0.95); background: rgba(255,255,255,0.15);
    border: 1px solid rgba(255,255,255,0.2); border-radius: 12px;
    padding: 3px 12px; text-shadow: 0 1px 3px rgba(0,0,0,0.4);
  }
  .divider { height: 1px; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.3), transparent); margin: 8px 0 20px; }
  .liquid-card .divider { background: linear-gradient(90deg, transparent, rgba(200,220,255,0.4), rgba(255,255,255,0.3), rgba(200,220,255,0.4), transparent); }
  .info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px 30px; flex: 1; }
  .info-item { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .info-label { font-size: 12px; color: rgba(255,255,255,0.65); letter-spacing: 1px; text-shadow: 0 1px 3px rgba(0,0,0,0.5); }
  .info-value { font-size: 15px; color: rgba(255,255,255,0.98); font-weight: 500; text-shadow: 0 1px 4px rgba(0,0,0,0.5); word-break: break-all; }
</style>
</head>
<body>
{% for section in card_sections %}
<div class="card-section">
  <div class="card-label">{{ section.label }}</div>
  {% if section.card_type == "frosted" %}
  <div class="frosted-card">
  {% elif section.card_type == "liquid" %}
  <div class="liquid-card">
    <div class="dispersion-border"></div>
  {% endif %}
    <div class="card-header">
      <div class="avatar">
        {% if avatar_data_uri %}
        <img class="avatar-img" src="{{ avatar_data_uri }}" alt="avatar">
        {% else %}
        <svg class="avatar-placeholder" viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
        {% endif %}
      </div>
      <div class="name-block">
        <div class="name">{{ nickname }}</div>
        <div class="tags-row">{{ acc_tags }}</div>
      </div>
    </div>
    <div class="divider"></div>
    <div class="info-grid">
      <div class="info-item"><div class="info-label">账号</div><div class="info-value">{{ qq }}</div></div>
      <div class="info-item"><div class="info-label">归属</div><div class="tags-row small">{{ from_tags }}</div></div>
      <div class="info-item"><div class="info-label">身份</div><div class="info-value">{{ acc_display }}</div></div>
      <div class="info-item"><div class="info-label">加入时间</div><div class="info-value">{{ join_time }}</div></div>
    </div>
  </div>
</div>
{% endfor %}
</body>
</html>"""


# ============================================================
# AstrBot 插件
# ============================================================
@register("ro_identity_card", "ROTeam",
          "飞鸟快验身份配置查询，自动生成磨砂/液态玻璃风格身份卡片",
          "1.2.0", "https://github.com/ROTeam/ro-identity-card")
class IdentityCardPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.feiniao_url = _FEINIAO_URL
        self.feiniao_token = _FEINIAO_TOKEN
        self.config_name = _FEINIAO_CONFIG_NAME
        self.card_width = _CARD_WIDTH
        self.card_type_default = "liquid"

        self._ini_data: Optional[dict] = None
        self._http_session: Optional[aiohttp.ClientSession] = None

        logger.info(f"RO的身份卡插件已加载 v1.2.0")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _load_ini(self, session: aiohttp.ClientSession) -> dict:
        """从飞鸟快验加载 INI 配置，每次请求都刷新"""
        raw = await fetch_feiniao_data(session)
        if raw is not None:
            self._ini_data = parse_feiniao_ini(raw)
            logger.info(f"身份配置已刷新，共 {len(self._ini_data)} 条记录")
        else:
            if self._ini_data is None:
                self._ini_data = {}
            logger.warning("飞鸟快验身份配置刷新失败（可能 Token 无效或网络问题），使用上次缓存")
        return self._ini_data

    async def terminate(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        logger.info("RO的身份卡插件已卸载")

    # ----------------------------------------------------------
    # /idcard
    # ----------------------------------------------------------
    @filter.command("idcard", alias={"身份卡", "身份", "card"})
    async def idcard(self, event: AstrMessageEvent,
                     target: str = None,
                     card_type: str = None):
        """生成身份卡片

        /idcard                    查询自己的卡片
        /idcard <qq号>             查指定 QQ 号卡片
        /idcard <qq号> frosted/liquid/both
        """
        # 参数校验：card_type 不能是目标 QQ 号
        ct = card_type or self.card_type_default
        if ct in ("frosted", "liquid", "both"):
            # 没有 target 但有合法 card_type → target 默认为发送者
            if target is None or target not in ("frosted", "liquid", "both"):
                pass  # target 正常
            else:
                # target 实际是 card_type，card_type 参数没有传
                ct = target
                target = None

        identifier = target or self._get_sender_id(event)
        if ct not in ("frosted", "liquid", "both"):
            ct = "both"

        logger.info(f"/idcard: identifier={identifier} type={ct}")
        session = await self._get_session()

        # 加载配置
        ini_data = await self._load_ini(session)
        if not ini_data:
            yield event.plain_result("⚠️ 身份配置为空，请检查飞鸟快验公共变量「身份配置」是否正确配置。")
            event.stop_event()
            return

        # 查找用户档案
        profile = get_user_profile(ini_data, identifier)
        if not profile:
            hint = ""
            if not identifier.isdigit():
                hint = ("\n   提示：此平台返回的是 openid（非 QQ 号），请先发送 /绑定qq 完成 QQ 绑定，\n"
                        "   或在飞鸟快验「身份配置」中添加 [openid节] 并填入对应 qq 号。")
            yield event.plain_result("⚠️ 未找到标识符「{}」的身份配置。{}".format(identifier, hint))
            event.stop_event()
            return

        # 解析字段
        nickname = profile.get("nick", identifier)
        acc_raw = profile.get("acc", "")
        from_raw = profile.get("from", "")
        join_time = profile.get("time", "未知")

        acc_list = [a.strip() for a in acc_raw.split("|") if a.strip()] if acc_raw else []
        from_list = [f.strip() for f in from_raw.split("|") if f.strip()] if from_raw else []

        def _escape_html(text: str) -> str:
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

        acc_tags = "".join(f'<span class="tag">{_escape_html(a)}</span>' for a in acc_list)
        from_tags = "".join(f'<span class="tag">{_escape_html(f)}</span>' for f in from_list)
        acc_display = " | ".join(acc_list) if acc_list else "未知"

        # 确定 QQ 号（用于头像和昵称）
        if identifier.isdigit():
            qq = int(identifier)
        else:
            qq_raw = profile.get("qq")
            if qq_raw and str(qq_raw).isdigit():
                qq = int(qq_raw)
            else:
                qq = None

        # 获取昵称和头像（仅在有有效 QQ 号时）
        avatar_b64 = ""
        if qq is not None:
            nickname = await fetch_qq_nickname(session, str(qq))
            avatar_b64 = await fetch_qq_avatar_b64(session, str(qq))

        # 构建卡片 sections
        sections = []
        if ct in ("frosted", "both"):
            sections.append({"label": "磨砂玻璃 · Frosted Glass", "card_type": "frosted"})
        if ct in ("liquid", "both"):
            sections.append({"label": "液态玻璃 · Liquid Glass", "card_type": "liquid"})

        # 渲染（对所有用户可控文本进行 HTML 转义，防止 XSS / 模板注入）
        qq_display = str(qq) if qq is not None else identifier
        tpl_data = {
            "card_sections": sections,
            "card_width": self.card_width,
            "viewport_height": _CARD_VIEWPORT_HEIGHT,
            "nickname": _escape_html(str(nickname)),
            "qq": _escape_html(qq_display),
            "acc_tags": acc_tags,
            "from_tags": from_tags,
            "acc_display": _escape_html(acc_display),
            "join_time": _escape_html(str(join_time)),
            "avatar_data_uri": avatar_b64,
        }

        try:
            img_url = await self.html_render(
                HTML_CARD_TEMPLATE, tpl_data,
                options={"type": "png", "full_page": True, "scale": "device"}
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"卡片渲染失败: {e}")
            yield event.plain_result(f"❌ 卡片渲染失败: {e}")
        event.stop_event()

    # ----------------------------------------------------------
    # /绑定qq（官机用）
    # ----------------------------------------------------------
    @filter.command("绑定qq", alias={"绑定", "bindqq", "bind_qq", "绑定QQ", "BindQQ", "绑定QQ号", "bindqq号"})
    async def bind_qq(self, event: AstrMessageEvent):
        """官机用户扫码绑定 QQ 号

        发送此指令后，机器人将发送 QQ 登录二维码，
        扫码成功后即可将当前 openid 绑定至你的 QQ 号。
        """
        # 仅官机平台需要绑定
        platform = event.get_platform_name()
        if platform not in ("qq_official", "qq_official_webhook"):
            yield event.plain_result("ℹ️ 此命令仅用于 QQ 官方平台（官机）的 openid 绑定。\n当前平台无需绑定，直接使用 /idcard 即可。")
            event.stop_event()
            return

        session = await self._get_session()

        # 获取登录码
        qr_data = await get_qr_login_code(session)
        if not qr_data:
            yield event.plain_result("❌ 获取 QQ 登录二维码失败，请稍后重试。")
            event.stop_event()
            return

        qr_url = qr_data.get("qr_url", "")
        qr_code = qr_data.get("code", "")

        if not qr_url or not qr_code or len(qr_code) < 8:
            logger.warning(f"QQ 登录码无效: code={qr_code!r} url={qr_url!r}")
            yield event.plain_result("❌ 获取 QQ 登录链接失败，请稍后重试。")
            event.stop_event()
            return

        # 生成 QR 码图片
        tmp_path = None
        try:
            qr_bytes = generate_qr_image(qr_url, size=280)
            tmp_dir = tempfile.gettempdir()
            tmp_path = os.path.join(tmp_dir, f"ro_qr_{qr_code[:12]}.png")
            with open(tmp_path, "wb") as f:
                f.write(qr_bytes)
        except Exception as e:
            logger.warning(f"QR 码图片生成失败: {e}")

        # 提示用户扫码
        yield event.plain_result("📱 请扫描以下二维码登录 QQ，完成后我将自动绑定你的身份信息（有效期 3 分钟）：")

        if tmp_path:
            try:
                yield event.image_result(tmp_path)
            except Exception as e:
                logger.warning(f"发送 QR 码图片失败: {e}")

        # 轮询获取 uin
        logger.info(f"等待用户扫码绑定 QQ，code={qr_code[:8]}...")
        uin = await poll_qr_login(session, qr_code)

        # 清理临时文件
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        if uin is None:
            logger.info("用户未在规定时间内扫码，绑定超时")
            yield event.plain_result("⏰ 扫码超时，请重新发送 /绑定qq 再次尝试。")
            event.stop_event()
            return

        logger.info(f"用户扫码成功，uin={uin}")

        # 更新本地缓存中的 ini_data（追加 qq 字段）
        ini_data = await self._load_ini(session)
        sender_id = self._get_sender_id(event)
        if sender_id not in ini_data:
            ini_data[sender_id] = {}
        ini_data[sender_id]["qq"] = str(uin)

        # 先同步到飞鸟快验，成功后再告知用户（确保配置持久化）
        save_ok = await self._save_feiniao_config(session, ini_data)

        # 更新本地缓存
        self._ini_data = ini_data

        if save_ok:
            yield event.plain_result(f"✅ QQ 绑定成功！你的 QQ 号是 {uin}，现在可以使用 /idcard 查看身份卡片。")
        else:
            yield event.plain_result(
                f"✅ QQ 绑定成功！你的 QQ 号是 {uin}\n"
                f"⚠️ 配置同步失败（飞鸟快验写入异常），\n"
                f"   请在飞鸟快验中手动添加 [openid节] 并填入 qq={uin}。"
            )

        event.stop_event()

    # ----------------------------------------------------------
    # 辅助方法
    # ----------------------------------------------------------
    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        """获取发送者唯一标识（QQ 号 或 openid）"""
        platform = event.get_platform_name()
        sender_id = event.get_sender_id()

        if platform in ("qq_official", "qq_official_webhook"):
            # 优先从 raw_message 提取 openid
            raw = getattr(event.message_obj, 'raw_message', None)
            if raw:
                # 尝试直接属性
                openid = getattr(raw, 'openid', None)
                if openid:
                    return str(openid)
                # 尝试嵌套 sender 对象
                sender = getattr(raw, 'sender', None)
                if sender:
                    openid = getattr(sender, 'openid', None)
                    if not openid and isinstance(sender, dict):
                        openid = sender.get('openid')
                    if openid:
                        return str(openid)
            # 回退：sender_id 在官机适配器中通常就是 openid
            return str(sender_id)

        return str(sender_id)

    async def _save_feiniao_config(self, session: aiohttp.ClientSession,
                                    ini_data: dict) -> bool:
        """
        将 INI 数据写回飞鸟快验公共变量。
        通过重新拉取原始 INI → 合并修改 → 写回，避免丢失其他字段。
        返回是否写入成功。
        """
        # 重新拉取原始 INI 文本，保留所有已有字段
        original_text = await fetch_feiniao_data(session)
        if original_text is None:
            logger.warning("无法获取原始飞鸟快验配置，跳过写入")
            return False

        # 解析原始 INI 并合并修改
        merged = parse_feiniao_ini(original_text)
        # 将新的 ini_data 合并进去（新数据覆盖同名节）
        for section, fields in ini_data.items():
            if section not in merged:
                merged[section] = {}
            merged[section].update(fields)

        # 重新序列化为 INI 文本
        lines = []
        for section, fields in merged.items():
            lines.append(f"[{section}]")
            for k, v in fields.items():
                lines.append(f"{k}={v}")
            lines.append("")
        ini_text = "\n".join(lines)

        # 带重试写入
        for attempt in range(3):
            try:
                req_url = f"{_FEINIAO_URL.rstrip('/')}/WebApi/SetPublicData"
                headers = {
                    "Content-Type": "application/json",
                    "Token": _FEINIAO_TOKEN,
                    "Referer": _FEINIAO_URL + "/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64)"
                }
                async with session.post(
                    req_url, json={"Name": _FEINIAO_CONFIG_NAME, "Value": ini_text},
                    headers=headers, ssl=False,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    data = await resp.json()
                    if data.get("code") == 10000:
                        logger.info("飞鸟快验配置已更新")
                        return True
                    logger.warning(f"飞鸟快验写入失败 (attempt={attempt+1}): {data.get('msg')}")
            except Exception as e:
                logger.warning(f"飞鸟快验写入异常 (attempt={attempt+1}): {e}")
            if attempt < 2:
                await asyncio.sleep(1)

        return False
