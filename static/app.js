"use strict";

let csrf = "";
let busy = false;
const byId = (name) => document.getElementById(name);
const sections = ["unlock", "phone", "code", "password", "account"];

function show(section) {
  sections.forEach((name) => { byId(`${name}-section`).hidden = name !== section; });
  byId("session-controls").hidden = section === "unlock";
  byId("status-pill").textContent = section === "account" ? "Аккаунт подключён" :
    section === "unlock" ? "Панель закрыта" : "Подключение";
  byId("status-pill").classList.toggle("connected", section === "account");
}

function notice(message, error = false) {
  byId("notice").textContent = message;
  byId("notice").hidden = !message;
  byId("notice").classList.toggle("error", error);
}

async function request(path, data) {
  const options = { credentials: "same-origin", cache: "no-store" };
  if (data !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf };
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  let result;
  try { result = await response.json(); }
  catch { throw new Error("Сервис ещё запускается или временно недоступен. Попробуй позже."); }
  if (!response.ok) {
    if (response.status === 401) { show("unlock"); csrf = ""; }
    const error = new Error(typeof result.detail === "string" ? result.detail : "Не удалось выполнить запрос.");
    error.status = response.status;
    throw error;
  }
  return result;
}

async function refresh() {
  const state = await request("/api/status");
  csrf = state.csrf;
  if (!state.configured) { show("phone"); notice(state.message, true); return; }
  if (state.authorized) {
    byId("account-name").textContent = state.account.name;
    byId("account-detail").textContent = state.account.username ? `@${state.account.username}` : "Вход в Telegram выполнен.";
    byId("save-required").hidden = state.saved;
    byId("session-saved").hidden = !state.saved;
    show("account");
  } else if (state.needs_password) { show("password"); }
  else if (state.pending_code) { show("code"); }
  else { show("phone"); }
}

async function act(action) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  notice("");
  try { await action(); }
  catch (error) {
    if (error.status === 409) { try { await refresh(); } catch { /* Keep the original error. */ } }
    notice(error.message || "Соединение прервалось. Попробуй позже.", true);
  }
  finally {
    busy = false;
    document.querySelectorAll("button").forEach((button) => { button.disabled = false; });
  }
}

byId("unlock-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void act(async () => {
    const password = byId("panel-password").value;
    byId("panel-password").value = "";
    const result = await request("/api/unlock", { password });
    csrf = result.csrf;
    await refresh();
  });
});

byId("phone-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void act(async () => {
    const result = await request("/api/login/start", { phone: byId("phone").value });
    if (result.state === "authorized") await refresh();
    else { show("code"); byId("code").focus(); notice("Запрос кода отправлен в Telegram."); }
  });
});

byId("code-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void act(async () => {
    const code = byId("code").value;
    byId("code").value = "";
    const result = await request("/api/login/code", { code });
    if (result.state === "password_needed") { show("password"); byId("telegram-password").focus(); }
    else await refresh();
  });
});

byId("password-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void act(async () => {
    const password = byId("telegram-password").value;
    byId("telegram-password").value = "";
    await request("/api/login/password", { password });
    await refresh();
  });
});

byId("export-session").addEventListener("click", () => {
  void act(async () => {
    const result = await request("/api/session/export", {});
    byId("encrypted-session").value = result.encrypted_session;
    byId("session-export").hidden = false;
    notice("Скопируй сохранение в Environment на Render.");
  });
});

byId("copy-session").addEventListener("click", () => {
  void act(async () => {
    try {
      await navigator.clipboard.writeText(byId("encrypted-session").value);
      notice("Скопировано. Добавь значение в TG_SESSION_ENCRYPTED на Render.");
    } catch {
      byId("encrypted-session").select();
      notice("Скопируй выделенный текст вручную.");
    }
  });
});

byId("change-phone").addEventListener("click", () => { show("phone"); notice(""); });
byId("refresh").addEventListener("click", () => { void act(refresh); });
byId("lock").addEventListener("click", () => {
  void act(async () => {
    await request("/api/lock", {});
    csrf = "";
    byId("phone").value = "";
    byId("encrypted-session").value = "";
    byId("session-export").hidden = true;
    show("unlock");
  });
});

void refresh().catch((error) => {
  show("unlock");
  if (error.message !== "Введи пароль панели.") notice(error.message, true);
});
