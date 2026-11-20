import { App } from "@modelcontextprotocol/ext-apps";

const META = "tbank/confirmation";
const app = new App({ name: "tbank confirmations", version: "1.0.0" });
const $ = (id) => document.getElementById(id);
let current = null;
let busy = false;
let timer;
const inputs = new Map();
const usedTokens = new Set();

function enable(enabled) {
  $("confirm").disabled = !enabled;
  $("cancel").disabled = !enabled;
}

function textResult(result) {
  return (result?.content ?? []).filter((item) => item.type === "text").map((item) => item.text).join("\n");
}

function render(result) {
  clearInterval(timer);
  const card = result?._meta?.[META];
  if (result?.isError) {
    enable(false);
    $("status").textContent = textResult(result) || "Не удалось обработать подтверждение.";
    return;
  }
  if (card?.status !== "pending") {
    current = null;
    $("form").hidden = true;
    $("title").textContent = card?.action === "decline" || card?.action === "cancel"
      ? "Операция отменена" : "Результат операции";
    $("message").textContent = "";
    $("status").textContent = "";
    $("result").hidden = false;
    $("result").textContent = textResult(result);
    // Only public result text is shared with the model, never hidden metadata.
    app.updateModelContext({ content: [{ type: "text", text: textResult(result) }] }).catch(() => {});
    return;
  }
  if (typeof card.token !== "string" || typeof card.message !== "string") return;
  if (usedTokens.has(card.token)) return;
  current = card;
  busy = false;
  $("title").textContent = "Подтверждение операции";
  $("message").textContent = card.message;
  $("result").hidden = true;
  $("fields").replaceChildren();
  inputs.clear();
  for (const [name, schema] of Object.entries(card.schema?.properties ?? {})) {
    const label = document.createElement("label");
    const caption = document.createElement("span");
    caption.textContent = schema.description || schema.title || name;
    label.append(caption);
    let input;
    if (Array.isArray(schema.enum)) {
      input = document.createElement("select");
      const placeholder = new Option("Выберите…", "");
      placeholder.disabled = true;
      placeholder.selected = true;
      input.add(placeholder);
      schema.enum.forEach((value) => input.add(new Option(String(value), String(value))));
      input.required = true;
    } else {
      input = document.createElement("input");
      input.type = schema.type === "boolean" ? "checkbox"
        : schema.type === "number" || schema.type === "integer" ? "number" : "text";
      if (input.type === "checkbox") input.checked = schema.default === true;
      else {
        input.required = (card.schema.required ?? []).includes(name);
        if (schema.default !== undefined) input.value = String(schema.default);
        if (input.type === "number") input.step = schema.type === "integer" ? "1" : "any";
      }
    }
    label.append(input);
    $("fields").append(label);
    inputs.set(name, { input, type: schema.type });
  }
  $("form").hidden = false;
  function tick() {
    const remaining = Math.max(0, Math.ceil((card.expiresAt - Date.now()) / 1000));
    enable(!busy && remaining > 0);
    $("status").textContent = remaining > 0 ? `Подтвердите в течение ${remaining} с.`
      : "Срок подтверждения истёк. Запросите операцию заново.";
  }
  tick();
  timer = setInterval(tick, 1000);
}

async function submit(action) {
  if (!current || busy || Date.now() >= current.expiresAt) return;
  if (action === "accept" && !$("form").reportValidity()) return;
  const card = current;
  const data = {};
  for (const [name, { input, type }] of inputs) {
    data[name] = type === "boolean" ? input.checked
      : type === "integer" || type === "number" ? Number(input.value) : input.value;
  }
  busy = true;
  usedTokens.add(card.token);
  enable(false);
  clearInterval(timer);
  $("status").textContent = "Ожидаем ответ сервера…";
  try {
    const result = await app.callServerTool({ name: "confirmation_action",
      arguments: { token: card.token, action, ...(action === "accept" ? { data } : {}) } });
    render(result);
  } catch {
    // Never auto-retry a financial action after an ambiguous response.
    current = null;
    $("status").textContent = "Ответ не получен. Не повторяйте оплату: сначала проверьте её статус или операции по счёту.";
  }
}

$("form").addEventListener("submit", (event) => { event.preventDefault(); submit("accept"); });
// Sandboxed MCP Apps need not have allow-forms. Never use native submission.
$("confirm").addEventListener("click", () => submit("accept"));
$("form").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.tagName === "INPUT") {
    event.preventDefault();
    submit("accept");
  }
});
$("cancel").addEventListener("click", () => submit("decline"));
app.ontoolresult = render;
app.ontoolcancelled = () => {
  current = null;
  clearInterval(timer);
  enable(false);
  $("status").textContent = "Вызов отменён. Новое подтверждение можно запросить в чате.";
};
app.connect().catch(() => { $("status").textContent = "Не удалось подключить карточку. Обновите приложение в ChatGPT."; });
setTimeout(() => {
  if (!current && $("message").textContent === "Загрузка карточки…") {
    $("status").textContent = "Карточка ещё не получила результат инструмента.";
  }
}, 10000);
