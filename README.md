# Telegram 批量注册机（单文件 Web 版）

手机号、Socks5 代理、API 密钥、验证码全部在网页上填写。验证码为网页人工填码。

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python app.py
```

浏览器打开 `http://127.0.0.1:8000`。

## 使用

1. 到 <https://my.telegram.org/apps> 申请 `api_id` / `api_hash`。
2. 网页上填写：
   - API ID / API Hash
   - 手机号（每行一个，国际格式 `+8613800138000`）
   - Socks5 代理（每行 `host:port:user:pass`，无认证写 `host:port`，留空直连）
3. 点「开始注册」。脚本逐个触发短信，页面上弹出该号验证码输入框，人工填码后自动继续。
4. 注册成功的 session 文件可在页面「Session 下载」区下载复用。

## 环境变量

- `HOST`：监听地址，默认 `127.0.0.1`
- `PORT`：监听端口，默认 `8000`
