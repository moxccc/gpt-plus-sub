"""
GPT Plus Subscription Tool
通过 OpenAI Token + 代理，获取 GPT Plus 的 PayPal 直接支付链接

核心流程:
1. 通过代理 + Token 调用 OpenAI API 创建 Stripe Checkout Session (US billing → PayPal可用)
2. Playwright + Stripe.js: 设置账单地址 + 创建PayPal PM + confirm
3. 调用 OpenAI approve 端点批准支付
4. 从 Stripe poll 获取 payment_intent.next_action.redirect_to_url
5. 跟踪 Stripe redirect 302 → PayPal ba_token URL
"""

import asyncio
import json
import re
import time
import uuid
import http.server
import threading
import functools
from typing import Optional

from curl_cffi.requests import AsyncSession
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="GPT Plus Subscription Tool")

CHATGPT_BASE = "https://chatgpt.com"
# Try multiple TLS fingerprints since proxy compatibility varies
IMPERSONATE_OPTIONS = ["chrome110", "chrome120", "chrome116", "chrome99", "safari15_3"]

tasks: dict = {}


class SubmitRequest(BaseModel):
    proxy: str
    token: str
    plan: str = "chatgptplusplan"
    retry_count: int = 3
    billing_country: str = "US"
    language: str = "en-US"


def parse_proxy(proxy_str: str) -> str:
    """Parse proxy string, auto-detect protocol. Supports http/socks5/socks5h."""
    proxy_str = proxy_str.strip()
    if "://" in proxy_str:
        return proxy_str
    # Default to socks5 (most residential proxies use socks5)
    return f"socks5://{proxy_str}"


def get_proxy_variants(proxy_str: str) -> list[str]:
    """Generate proxy URL variants to try different protocols."""
    proxy_str = proxy_str.strip()
    if "://" in proxy_str:
        proto, rest = proxy_str.split("://", 1)
    else:
        proto, rest = None, proxy_str
    variants = []
    if proto:
        variants.append(proxy_str)
    for p in ["socks5", "socks5h", "http"]:
        url = f"{p}://{rest}"
        if url not in variants:
            variants.append(url)
    return variants


def extract_token(token_str: str) -> str:
    token_str = token_str.strip()
    if token_str.startswith("ey"):
        return token_str
    try:
        data = json.loads(token_str)
        return data.get("accessToken") or data.get("access_token") or token_str
    except (json.JSONDecodeError, TypeError):
        return token_str


def build_headers(token: str, device_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": CHATGPT_BASE,
        "Referer": f"{CHATGPT_BASE}/",
        "Oai-Device-Id": device_id,
        "Oai-Language": "en-US",
    }


def _serve_html(html_content: str, port: int):
    """Serve HTML on localhost for Stripe.js (needs http: origin)."""
    import tempfile, os
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "checkout.html")
    with open(path, "w") as f:
        f.write(html_content)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmpdir)
    srv = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def get_paypal_link(token: str, proxy: str, plan: str = "chatgptplusplan",
                          log_fn=None) -> Optional[str]:
    """
    Main function: get PayPal ba_token URL for GPT Plus subscription.

    Args:
        token: OpenAI access token (JWT)
        proxy: HTTP proxy URL (e.g. http://user:pass@host:port)
        plan: Plan name (default: chatgptplusplan)
        log_fn: Optional logging callback

    Returns:
        PayPal URL like https://www.paypal.com/agreements/approve?ba_token=BA-xxx
        or None on failure
    """
    if log_fn is None:
        log_fn = lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    device_id = str(uuid.uuid4())
    headers = build_headers(token, device_id)

    # --- Step 1: Create checkout session (US billing → PayPal available) ---
    log_fn("Step 1: 创建 checkout session...")
    co = None
    working_imp = None
    working_proxy = None
    proxy_variants = get_proxy_variants(proxy)
    async with AsyncSession() as sess:
        for pvar in proxy_variants:
            proto = pvar.split("://")[0]
            for imp in IMPERSONATE_OPTIONS:
                try:
                    r = await sess.post(
                        f"{CHATGPT_BASE}/backend-api/payments/checkout",
                        headers=headers,
                        json={"plan_type": plan},
                        proxy=pvar,
                        impersonate=imp,
                        timeout=30,
                    )
                    if r.status_code == 200:
                        co = r.json()
                        working_imp = imp
                        working_proxy = pvar
                        log_fn(f"  连接成功: {proto} + {imp}")
                        break
                    log_fn(f"  {proto}+{imp}: HTTP {r.status_code}")
                except Exception as e:
                    log_fn(f"  {proto}+{imp}: {str(e)[:60]}")
                    break  # Same proxy protocol fails, try next protocol
            if co is not None:
                break
        if co is None:
            log_fn("创建 checkout 失败: 所有代理协议+TLS 指纹均失败")
            return None
    proxy = working_proxy  # Use the working proxy for subsequent requests

    cs_id = co.get("checkout_session_id", "")
    pk = co.get("publishable_key", "")
    client_secret = co.get("client_secret", "")
    proc_entity = co.get("processor_entity", "openai_llc")
    billing = co.get("billing_details", {})

    if not cs_id or not pk or not client_secret:
        log_fn("checkout 响应缺少必要字段")
        return None

    log_fn(f"  CS: {cs_id[:50]}...")
    log_fn(f"  Billing: {billing.get('country')}/{billing.get('currency')}")

    # --- Step 2: Stripe.js confirm with PayPal ---
    log_fn("Step 2: Stripe.js 确认支付 (PayPal)...")

    from playwright.async_api import async_playwright

    # Build HTML page with Stripe.js
    html = f"""<!DOCTYPE html><html><body>
<script src="https://js.stripe.com/v3/"></script>
<script>
window.__ready = false;
window.__confirmed = false;
window.__error = null;
window.__paypalUrl = null;
window.__pollCount = 0;
window.__lastPollStatus = null;

(async function() {{
    try {{
        const stripe = Stripe('{pk}', {{betas: ['custom_checkout_beta_3']}});
        const co = await stripe.initCustomCheckout({{clientSecret: '{client_secret}'}});

        await co.updateBillingAddress({{
            name: 'John Smith',
            address: {{country: 'US', state: 'CA', city: 'San Francisco',
                      line1: '123 Market St', postal_code: '94105'}}
        }});
        await co.updateEmail('user@example.com');

        const {{paymentMethod, error}} = await stripe.createPaymentMethod({{
            type: 'paypal',
            billing_details: {{
                name: 'John Smith', email: 'user@example.com',
                address: {{country: 'US', state: 'CA', city: 'San Francisco',
                          line1: '123 Market St', postal_code: '94105'}}
            }}
        }});
        if (error) {{
            window.__error = 'PM: ' + error.message;
            return;
        }}

        window.__ready = true;

        // Fire confirm (runs async, polls Stripe for result)
        co.confirm({{
            paymentMethod: paymentMethod.id,
            returnUrl: 'https://chatgpt.com/#settings/subscription'
        }}).then(r => {{
            window.__confirmed = true;
            window.__confirmResult = JSON.stringify(r);
        }}).catch(e => {{
            window.__confirmed = true;
            window.__confirmError = e.message;
        }});
    }} catch(e) {{
        window.__error = 'Init: ' + e.message;
    }}
}})();
</script></body></html>"""

    port = 18900 + (hash(cs_id) % 100)
    srv = _serve_html(html, port)

    paypal_url = None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = await browser.new_page()

            stripe_confirmed = asyncio.Event()

            # Intercept Stripe poll responses to detect state changes and find redirect
            async def on_response(resp):
                nonlocal paypal_url
                if paypal_url:
                    return
                url = resp.url
                if "stripe.com" not in url or "/v1/payment_pages/" not in url:
                    return
                try:
                    body_text = await resp.text()
                    if not body_text.startswith('{'):
                        return
                    data = json.loads(body_text)

                    # Track poll status
                    po_status = data.get("payment_object_status")
                    if po_status:
                        await page.evaluate(f"window.__lastPollStatus = '{po_status}'")
                        await page.evaluate("window.__pollCount++")
                        log_fn(f"  Stripe poll: payment_object_status={po_status}")
                        if po_status in ("requires_action", "requires_confirmation", "processing"):
                            stripe_confirmed.set()

                    pi = data.get("payment_intent")
                    if pi and isinstance(pi, dict):
                        na = pi.get("next_action")
                        if na and isinstance(na, dict):
                            redir = na.get("redirect_to_url", {})
                            if isinstance(redir, dict):
                                stripe_url = redir.get("url", "")
                                if stripe_url:
                                    log_fn(f"  找到 Stripe redirect: {stripe_url[:100]}...")
                                    paypal_url = stripe_url
                except Exception:
                    pass

            page.on("response", on_response)
            await page.goto(f"http://127.0.0.1:{port}/checkout.html", timeout=60000)

            # Wait for Stripe.js to init and confirm
            for _ in range(20):
                await asyncio.sleep(1)
                ready = await page.evaluate("window.__ready || false")
                err = await page.evaluate("window.__error || null")
                if err:
                    log_fn(f"  Stripe.js 错误: {err}")
                    await browser.close()
                    srv.shutdown()
                    return None
                if ready:
                    break

            log_fn("  Stripe.js confirm 已启动")

            # Wait for Stripe to acknowledge the confirm (poll shows status change)
            log_fn("  等待 Stripe 确认...")
            try:
                await asyncio.wait_for(stripe_confirmed.wait(), timeout=30)
                log_fn("  Stripe 已确认 confirm")
            except asyncio.TimeoutError:
                log_fn("  警告: Stripe 确认超时, 尝试继续...")

            await asyncio.sleep(2)

            # --- Step 3: Approve ---
            log_fn("Step 3: 调用 approve 端点...")
            approve_result = None
            for approve_attempt in range(3):
                async with AsyncSession() as sess:
                    r = await sess.post(
                        f"{CHATGPT_BASE}/backend-api/payments/checkout/approve",
                        headers=headers,
                        json={"checkout_session_id": cs_id, "processor_entity": proc_entity},
                        proxy=proxy,
                        impersonate=working_imp or IMPERSONATE_OPTIONS[0],
                        timeout=30,
                    )
                    approve_result = r.json()
                    log_fn(f"  Approve [{approve_attempt+1}]: {approve_result}")

                if approve_result.get("result") == "approved":
                    break
                if approve_attempt < 2:
                    log_fn(f"  Approve 未成功, {3+approve_attempt*2}秒后重试...")
                    await asyncio.sleep(3 + approve_attempt * 2)

            if not approve_result or approve_result.get("result") != "approved":
                log_fn("  Approve 失败")
                await browser.close()
                srv.shutdown()
                return None

            # --- Step 4: Wait for poll to return payment_intent.next_action ---
            log_fn("Step 4: 等待 Stripe 返回 PayPal redirect...")
            for i in range(60):
                await asyncio.sleep(1)
                if paypal_url:
                    break

            await browser.close()
    finally:
        srv.shutdown()

    if not paypal_url:
        log_fn("未能从 Stripe poll 获取 redirect URL")
        return None

    # --- Step 5: Follow Stripe redirect to get PayPal ba_token ---
    log_fn("Step 5: 跟踪 redirect 获取 ba_token...")

    # Check if it's already a PayPal URL
    ba_match = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', paypal_url)
    if ba_match:
        final_url = f"https://www.paypal.com/agreements/approve?ba_token={ba_match.group(1)}"
        log_fn(f"  直接获取: {final_url}")
        return final_url

    # Follow Stripe redirect chain
    async with AsyncSession() as sess:
        current_url = paypal_url
        for hop in range(10):
            ba_match = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', current_url)
            if ba_match:
                final_url = f"https://www.paypal.com/agreements/approve?ba_token={ba_match.group(1)}"
                log_fn(f"  [跳转{hop}] 找到: {final_url}")
                return final_url

            try:
                r = await sess.get(current_url, allow_redirects=False, timeout=15)
                log_fn(f"  [跳转{hop+1}] {r.status_code}")

                if r.status_code in (301, 302, 303, 307, 308):
                    location = r.headers.get("location", "")
                    if not location:
                        break
                    current_url = location
                    continue

                # Check body for PayPal URL
                body = r.text
                m = re.search(r'https://www\.paypal\.com/[^\s"\'<>]*ba_token=[^\s"\'<>&]+', body)
                if m:
                    return m.group(0)
                break
            except Exception as e:
                log_fn(f"  [跳转{hop+1}] 错误: {e}")
                break

    log_fn("未能从 redirect chain 获取 ba_token")
    return None


# ---------- API Routes ----------

async def run_task(task_id: str, req: SubmitRequest):
    task = tasks[task_id]
    task["status"] = "running"

    def log(msg: str):
        entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
        task["logs"].append(entry)
        print(entry, flush=True)

    proxy = parse_proxy(req.proxy)
    token = extract_token(req.token)

    log(f"任务开始 (计划: {req.plan}, 国家: {req.billing_country})")

    last_error = None
    for attempt in range(1, req.retry_count + 1):
        if task.get("cancelled"):
            log("任务已取消")
            task["status"] = "cancelled"
            return

        if req.retry_count > 1:
            log(f"=== 第 {attempt}/{req.retry_count} 次尝试 ===")

        try:
            result = await get_paypal_link(token, proxy, req.plan, log)
            if result:
                log(f"成功! PayPal URL: {result}")
                ba = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', result)
                if ba:
                    log(f"BA_TOKEN: {ba.group(1)}")
                task["paypal_url"] = result
                task["status"] = "success"
                return
            last_error = "未能获取 PayPal 支付链接"
        except Exception as e:
            log(f"异常: {e}")
            last_error = str(e)

        if attempt < req.retry_count:
            log("等待 5 秒后重试...")
            await asyncio.sleep(5)

    log(f"失败: {last_error}")
    task["status"] = "failed"
    task["error"] = last_error or "未能获取 PayPal 支付链接"


@app.post("/api/submit")
async def submit_task(req: SubmitRequest):
    task_id = uuid.uuid4().hex[:8]
    tasks[task_id] = {
        "task_id": task_id,
        "status": "pending",
        "logs": [],
        "paypal_url": None,
        "error": None,
    }
    asyncio.create_task(run_task(task_id, req))
    return {"task_id": task_id, "message": "任务已提交"}


@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in tasks:
        return JSONResponse(status_code=404, content={"error": "任务不存在"})
    return tasks[task_id]


@app.post("/api/stop/{task_id}")
async def stop_task(task_id: str):
    if task_id not in tasks:
        return JSONResponse(status_code=404, content={"error": "任务不存在"})
    tasks[task_id]["cancelled"] = True
    return {"message": "已请求停止任务"}


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "static" / "index.html"
    return html_path.read_text(encoding="utf-8")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        # CLI mode: python main.py <token> <proxy> [plan]
        _token = sys.argv[1]
        _proxy = parse_proxy(sys.argv[2])
        _plan = sys.argv[3] if len(sys.argv) > 3 else "chatgptplusplan"
        result = asyncio.run(get_paypal_link(_token, _proxy, _plan))
        if result:
            print(f"\nPayPal URL: {result}")
            sys.exit(0)
        else:
            print("\nFailed to get PayPal URL")
            sys.exit(1)
    else:
        # Web server mode
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8080)
