"""Real Chromium + MCP Apps postMessage host; offline, no bank requests."""
import json
import time
from playwright.sync_api import sync_playwright, expect
from tbank_mcp.mcp_apps import ASSET, META

HOST = """<iframe id='widget' sandbox='allow-scripts'></iframe><script>
window.calls = []; window.contexts = [];
window.sendResult = (result) => document.querySelector('iframe').contentWindow.postMessage(
  {jsonrpc:'2.0', method:'ui/notifications/tool-result', params:result}, '*');
window.reply = (index, result) => document.querySelector('iframe').contentWindow.postMessage(
  {jsonrpc:'2.0', id:window.calls[index].id, result}, '*');
addEventListener('message', (e) => {
  const m=e.data;
  if (m?.jsonrpc !== '2.0') return;
  const reply=(result)=>e.source.postMessage({jsonrpc:'2.0',id:m.id,result}, '*');
  if(m.method==='ui/initialize') reply({protocolVersion:m.params.protocolVersion,
    hostInfo:{name:'offline-test',version:'1'},hostCapabilities:{serverTools:{},updateModelContext:{}},
    hostContext:{theme:'light',displayMode:'inline'}});
  if(m.method==='ui/notifications/initialized') {
    e.source.postMessage({jsonrpc:'2.0',method:'ui/notifications/tool-input',params:{arguments:{}}},'*');
    window.widgetReady=true;
  }
  if(m.method==='tools/call') window.calls.push(m);
  if(m.method==='ui/update-model-context') { window.contexts.push(m.params);reply({}); }
});
</script>"""


def card(token, schema=None, message="Перевести 1 000 ₽ → Получатель?"):
    return {"content": [{"type": "text", "text": "Ожидается подтверждение"}],
            "_meta": {META: {"status": "pending", "token": token, "message": message,
                              "expiresAt": int(time.time() * 1000) + 60000,
                              "schema": schema or {"type": "object", "properties": {}}}}}


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.set_content(HOST)
    page.eval_on_selector("#widget", "(el, html) => el.srcdoc=html", ASSET.read_text())
    page.wait_for_function("window.widgetReady === true")
    frame = page.frame_locator("#widget")
    page.evaluate("window.sendResult", card("hidden-token-first"))
    frame.locator("#confirm").wait_for(state="visible")
    assert page.evaluate("window.calls.length") == 0, "rendering submitted an action"
    assert "hidden-token-first" not in frame.locator("body").inner_text()
    assert "1 000" in frame.locator("#message").inner_text()
    frame.locator("#confirm").click()
    page.wait_for_function("window.calls.length===1")
    call = page.evaluate("window.calls[0]")
    assert call["params"]["name"] == "confirmation_action"
    assert call["params"]["arguments"] == {"token": "hidden-token-first", "action": "accept", "data": {}}
    expect(frame.locator("#confirm")).to_be_disabled()
    # Redelivering the first result during a click cannot re-enable its handle.
    page.evaluate("window.sendResult", card("hidden-token-first"))
    assert frame.locator("#confirm").is_disabled()

    # A second form arrives on the same original operation (account/bank picker).
    selection = {"type": "object", "properties": {"choice": {
        "type": "string", "enum": ["Счёт A", "Счёт B"]}}, "required": ["choice"]}
    page.evaluate("r => window.reply(0,r)", card("hidden-token-second", selection, "Выберите счёт"))
    frame.locator("select").wait_for()
    frame.locator("select").select_option("Счёт B")
    frame.locator("#confirm").click()
    page.wait_for_function("window.calls.length===2")
    assert page.evaluate("window.calls[1].params.arguments.data") == {"choice": "Счёт B"}
    page.evaluate("r => window.reply(1,r)", {"content": [{"type": "text", "text": "Готово"}],
                  "_meta": {META: {"status": "complete", "action": "accept"}}})
    frame.locator("#result").wait_for(state="visible")
    assert frame.locator("#result").inner_text() == "Готово"
    assert frame.locator("#form").is_hidden()
    assert "hidden-token" not in json.dumps(page.evaluate("window.contexts"))

    # Expired cards, cancel buttons and untrusted text never auto-confirm or run HTML.
    expired = card("hidden-expired")
    expired["_meta"][META]["expiresAt"] = 1
    page.evaluate("window.sendResult", expired)
    expect(frame.locator("#confirm")).to_be_disabled()
    page.evaluate("window.sendResult", card("hidden-cancel", message='<img src=x onerror="alert(1)">'))
    expect(frame.locator("#message")).to_contain_text("<img")
    assert frame.locator("#message img").count() == 0
    assert "<img" in frame.locator("#message").inner_text()
    frame.locator("#cancel").click()
    page.wait_for_function("window.calls.length===3")
    assert page.evaluate("window.calls[2].params.arguments") == {"token": "hidden-cancel", "action": "decline"}
    page.evaluate("r => window.reply(2,r)", {"content": [{"type": "text", "text": "Отменено"}],
                  "_meta": {META: {"status": "complete", "action": "decline"}}})
    expect(frame.locator("#title")).to_have_text("Операция отменена")
    browser.close()
print("MCP Apps UI: handshake, explicit click, no duplicate click, chained picker, private handles, expiry, cancel and text escaping OK")
