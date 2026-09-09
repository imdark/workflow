import json
import os
import time
import asyncio
from pathlib import Path
from aiohttp import web

BROWSER_DAEMON_DIR = Path.home() / ".wf" / "browser"
STATE_FILE = BROWSER_DAEMON_DIR / "state"
PID_FILE = BROWSER_DAEMON_DIR / "daemon.pid"
PORT = int(os.environ.get("BROWSER_DAEMON_PORT", 18765))

browser = None
current_page = None


async def discover_ws_endpoint(debug_port: int = 9222) -> str:
    """Ask Chrome's own /json/version endpoint for the full CDP websocket URL.

    pyppeteer.connect requires the exact URL including the browser id, not
    just the host:port prefix -- and Chrome only reliably answers this over
    IPv6/localhost, not always 127.0.0.1, so we resolve it at runtime rather
    than hardcoding either.
    """
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://localhost:{debug_port}/json/version", timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            return data["webSocketDebuggerUrl"]


async def get_browser():
    global browser
    if browser is None:
        import pyppeteer
        ws_endpoint = os.environ.get("CHROME_WS_ENDPOINT")
        if not ws_endpoint:
            debug_port = int(os.environ.get("CHROME_DEBUG_PORT", 9222))
            ws_endpoint = await discover_ws_endpoint(debug_port)
        browser = await pyppeteer.connect(
            browserWSEndpoint=ws_endpoint,
            defaultViewport=None
        )
    return browser


async def navigate(request):
    global current_page
    try:
        data = await request.json()
        url = data.get("url", "")
        
        b = await get_browser()
        current_page = await b.newPage()
        
        response = await current_page.goto(url, timeout=60000, waitUntil='networkidle0')
        if response and response.status >= 400:
            return web.json_response({"status": "error", "error": f"HTTP {response.status}"}, status=500)
        return web.json_response({"status": "ok", "url": url})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def screenshot(request):
    global current_page
    data = await request.json()
    output = data.get("output", "screenshot.png")
    
    b = await get_browser()
    if current_page is None:
        pages = await b.pages()
        if pages:
            current_page = pages[0]
        else:
            current_page = await b.newPage()
    
    if current_page is None:
        current_page = await b.newPage()
    
    await current_page.screenshot({'path': output})
    return web.json_response({"status": "ok", "saved_path": output})


async def snapshot(request):
    global current_page
    b = await get_browser()
    if current_page is None:
        current_page = await b.newPage()
    
    content = await current_page.content()
    return web.json_response({"status": "ok", "content": content})


async def list_pages_handler(request):
    b = await get_browser()
    pages = await b.pages()
    page_list = []
    for page in pages:
        try:
            title = await page.title() if page else "Unknown"
        except:
            title = "Unknown"
        page_list.append({
            "url": page.url if page else "Unknown",
            "title": title
        })
    return web.json_response({"status": "ok", "pages": page_list})


async def select_page_handler(request):
    global current_page
    data = await request.json()
    url_contains = data.get("url_contains", "")

    b = await get_browser()
    pages = await b.pages()
    for page in pages:
        if url_contains in (page.url or ""):
            current_page = page
            return web.json_response({"status": "ok", "url": page.url})
    return web.json_response({"status": "error", "error": "no matching page"}, status=404)


async def hover_handler(request):
    global current_page
    data = await request.json()
    x = data.get("x")
    y = data.get("y")

    b = await get_browser()
    if current_page is None:
        pages = await b.pages()
        current_page = pages[0] if pages else await b.newPage()

    await current_page._client.send("Input.dispatchMouseEvent", {
        "type": "mouseMoved",
        "x": x,
        "y": y,
    })
    return web.json_response({"status": "ok"})


async def real_click_handler(request):
    global current_page
    data = await request.json()
    x = data.get("x")
    y = data.get("y")

    b = await get_browser()
    if current_page is None:
        pages = await b.pages()
        current_page = pages[0] if pages else await b.newPage()

    for mtype in ("mouseMoved", "mousePressed", "mouseReleased"):
        await current_page._client.send("Input.dispatchMouseEvent", {
            "type": mtype,
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1,
        })
    return web.json_response({"status": "ok"})


async def scroll_handler(request):
    global current_page
    data = await request.json()
    delta_y = data.get("deltaY", 1000)
    x = data.get("x", 640)
    y = data.get("y", 400)

    b = await get_browser()
    if current_page is None:
        pages = await b.pages()
        current_page = pages[0] if pages else await b.newPage()

    await current_page._client.send("Input.dispatchMouseEvent", {
        "type": "mouseWheel",
        "x": x,
        "y": y,
        "deltaX": 0,
        "deltaY": delta_y,
    })
    return web.json_response({"status": "ok"})


async def click_handler(request):
    global current_page
    data = await request.json()
    uid = data.get("uid", "")
    
    b = await get_browser()
    if current_page is None:
        pages = await b.pages()
        current_page = pages[0] if pages else await b.newPage()
    
    await current_page.click(uid)
    return web.json_response({"status": "ok"})


async def press_handler(request):
    global current_page
    try:
        data = await request.json()
        key = data.get("key", "")
        
        b = await get_browser()
        if current_page is None:
            pages = await b.pages()
            current_page = pages[0] if pages else await b.newPage()
        
        if "+" in key:
            parts = key.split("+")
            for modifier in parts[:-1]:
                await current_page.keyboard.down(modifier)
            await current_page.keyboard.press(parts[-1])
            for modifier in reversed(parts[:-1]):
                await current_page.keyboard.up(modifier)
        else:
            await current_page.keyboard.press(key)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def fill_handler(request):
    global current_page
    data = await request.json()
    uid = data.get("uid", "")
    value = data.get("value", "")
    
    b = await get_browser()
    if current_page is None:
        pages = await b.pages()
        current_page = pages[0] if pages else await b.newPage()
    
    await current_page.type(uid, value)
    return web.json_response({"status": "ok"})


async def eval_handler(request):
    global current_page
    data = await request.json()
    script = data.get("script", "")
    
    try:
        b = await get_browser()
        if current_page is None:
            pages = await b.pages()
            current_page = pages[0] if pages else await b.newPage()
        
        if current_page is None:
            return web.json_response({"status": "error", "error": "No page available. Use 'wf browser open' first."}, status=400)
        
        result = await current_page.evaluate(script)
        return web.json_response({"status": "ok", "result": str(result)})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def wait_handler(request):
    global current_page
    data = await request.json()
    text = data.get("text", "")
    timeout = data.get("timeout", 30000)
    
    b = await get_browser()
    if current_page is None:
        pages = await b.pages()
        current_page = pages[0] if pages else await b.newPage()
    
    await current_page.waitForSelector(text, timeout=timeout)
    return web.json_response({"status": "ok"})


async def cookies_handler(request):
    global current_page
    b = await get_browser()
    if current_page is None:
        pages = await b.pages()
        current_page = pages[0] if pages else await b.newPage()
    
    if current_page is None:
        return web.json_response({"status": "error", "error": "No page available"}, status=400)
    
    cookies = await current_page.cookies()
    return web.json_response({"status": "ok", "cookies": cookies})


async def health(request):
    return web.json_response({"status": "ok"})


def get_status():
    """Get daemon status"""
    state = load_daemon_state()
    if state and state.get("running"):
        return {"running": True, "port": state.get("port", PORT)}
    return {"running": False}


def is_daemon_running() -> bool:
    """Check if daemon is running"""
    return get_status().get("running", False)


def start_daemon():
    """Start the daemon (blocking)"""
    import asyncio
    asyncio.run(main())


def save_daemon_state(pid: int, port: int):
    BROWSER_DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    state = {"pid": pid, "port": port, "running": True}
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def load_daemon_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


async def start_server():
    app = web.Application()
    app.router.add_get('/health', health)
    app.router.add_post('/screenshot', screenshot)
    app.router.add_post('/snapshot', snapshot)
    app.router.add_post('/pages', list_pages_handler)
    app.router.add_put('/open', navigate)
    app.router.add_post('/click', click_handler)
    app.router.add_post('/press', press_handler)
    app.router.add_post('/fill', fill_handler)
    app.router.add_post('/eval', eval_handler)
    app.router.add_post('/wait', wait_handler)
    app.router.add_post('/cookies', cookies_handler)
    app.router.add_post('/scroll', scroll_handler)
    app.router.add_post('/select_page', select_page_handler)
    app.router.add_post('/hover', hover_handler)
    app.router.add_post('/real_click', real_click_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', PORT)
    await site.start()
    return runner


async def main():
    save_daemon_state(os.getpid(), PORT)
    print(f"Browser daemon started on port {PORT}")
    await start_server()
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
