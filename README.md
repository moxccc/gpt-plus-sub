# GPT Plus 订阅工具

通过 OpenAI Token + 代理，自动提取 GPT Plus 的 PayPal 直接支付链接。

生成格式: `https://www.paypal.com/agreements/approve?ba_token=BA-xxxxxxxxxx`

## 核心流程

```
checkout → Stripe.js confirm(PayPal) → approve → poll PaymentIntent → redirect → ba_token
```

1. 通过代理调用 OpenAI API 创建 Stripe Checkout Session (US billing → PayPal 可用)
2. Playwright + Stripe.js: 设置账单地址 + 创建 PayPal 支付方式 + confirm
3. 调用 OpenAI approve 端点批准支付
4. 从 Stripe poll 获取 `payment_intent.next_action.redirect_to_url`
5. 跟踪 Stripe redirect 302 → PayPal ba_token URL

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 运行

**Web 模式** (推荐):

```bash
python main.py
# 访问 http://localhost:8080
```

**CLI 模式**:

```bash
python main.py <token> <proxy>
# 例: python main.py eyJhbG... http://user:pass@host:port
```

### Docker 部署

```bash
docker build -t gpt-plus-sub .
docker run -p 8080:8080 gpt-plus-sub
```

## 使用说明

### 获取 Token

1. 浏览器登录 [chatgpt.com](https://chatgpt.com)
2. 访问 `https://chatgpt.com/api/auth/session`
3. 复制 `accessToken` 字段 (以 `ey` 开头的 JWT)

### 代理要求

- 支持 HTTP/HTTPS 代理
- 格式: `user:pass@host:port` 或 `http://user:pass@host:port`
- 建议使用美国 IP 代理 (PayPal 支付需要 US billing)

## 功能特性

- 深色主题 Web UI
- 代理本地保存 (localStorage)
- 失败自动重试 (可配置次数)
- 账单国家切换 (US = PayPal, JP = Card)
- 订阅计划选择 (Plus / Team / Pro)
- 实时日志输出
- 成功后一键复制 PayPal 链接
- 历史记录

## 项目结构

```
├── main.py              # FastAPI 后端 + 核心逻辑 + CLI
├── static/index.html    # Web 前端 UI
├── requirements.txt     # Python 依赖
└── Dockerfile           # Docker 部署配置
```

## 技术栈

- **后端**: FastAPI + curl_cffi (TLS 指纹模拟)
- **浏览器自动化**: Playwright (Stripe.js 交互)
- **前端**: 原生 HTML/CSS/JS
