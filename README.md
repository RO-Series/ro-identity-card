# RO 的身份卡 — AstrBot 插件

从WebAPI 读取「身份配置」，根据发送者的 QQ 号匹配配置，渲染磨砂/液态玻璃风格身份卡片图片并发送。

## 支持的命令

| 命令 | 说明 |
|---|---|
| `/idcard` | 查询自己的身份卡片 |
| `/idcard <qq号>` | 查询指定 QQ 号的卡片 |
| `/idcard <qq号> frosted` | 只渲染磨砂玻璃卡片 |
| `/idcard <qq号> liquid` | 只渲染液态玻璃卡片 |
| `/idcard <qq号> both` | 渲染两种卡片（默认） |
| `/身份卡` / `/身份` / `/card` | 同上，别名指令 |
| `/绑定qq` | 官机用户扫码绑定 QQ 号（获取 openid → uin 映射） |

## 平台支持

| 平台适配器 | 说明 |
|---|---|
| `aiocqhttp` | QQ 个人号，直接使用 sender_id 作为 QQ 号 |
| `qq_official` | QQ 官方接口，sender_id 为 openid，需先 `/绑定qq` 或配置 `qq` 字段 |
| `qq_official_webhook` | QQ 官方 Webhook，同上 |

## 官机 QQ 绑定流程

1. 在 QQ 官机上发送 `/绑定qq`
2. 机器人发送 QQ 登录二维码图片
3. 用户扫码完成 QQ 登录（有效期 3 分钟）
4. 系统自动将 openid → uin 映射写入飞鸟快验「身份配置」
5. 之后发送 `/idcard` 即可查看身份卡片

## 安装

将 `ro_identity_card` 整个目录放入 AstrBot 的插件目录（通常 `data/plugins/`），安装依赖并重启：

```bash
pip install -r requirements.txt
```

