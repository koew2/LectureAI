const byId = (id) => document.getElementById(id);
const workspace = byId("admin-workspace");
const tabs = [...document.querySelectorAll("[data-admin-view]")];
const views = {
  processing: {
    title: "Обработка лекций", panel: byId("processing-panel"), filter: byId("result-filter"),
    refresh: byId("refresh-history"), more: byId("more-processing"), rows: byId("processing-rows"), nextBefore: null,
  },
  events: {
    title: "Логи", panel: byId("events-panel"), filter: byId("event-level"),
    refresh: byId("refresh-events"), more: byId("more-events"), rows: byId("events-rows"), nextBefore: null,
  },
};
const notice = byId("admin-notice");
const docs = byId("api-docs");
const parentWindow = window.opener;
const dateFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "medium" });
const failures = {
  "Unable to decode the audio file": "Не удалось прочитать аудиофайл",
  "No speech was detected in the recording": "В записи не обнаружена речь",
  "Audio duration exceeds the configured limit or is invalid": "Недопустимая длительность аудио",
  "File contents do not match a supported audio format": "Файл не соответствует формату аудио",
  "AI output limit reached while generating study notes.": "Ответ AI достиг лимита длины",
  "AI processing failed. Check provider configuration or limits.": "AI не смог подготовить конспект",
  "Task queue is unavailable. Upload the file again later.": "Очередь обработки недоступна",
  "Lecture processing failed. Contact the project maintainer.": "Внутренняя ошибка обработки",
};
const eventNames = {
  api_error: "Внутренняя ошибка API",
  registration_mail_failed: "Не удалось отправить код регистрации",
  user_registered: "Пользователь зарегистрирован",
  login_failed: "Ошибка входа",
  login_succeeded: "Успешный вход",
  upload_rejected: "Загрузка отклонена",
  upload_failed: "Ошибка сохранения загрузки",
  lecture_uploaded: "Лекция загружена",
  queue_failed: "Ошибка очереди обработки",
  lecture_failed: "Ошибка обработки лекции",
  lecture_completed: "Лекция обработана",
  chat_failed: "Ошибка AI-тьютора",
  lecture_mail_failed: "Не удалось отправить результат на почту",
};
let activeView = "processing";
let requestId = 0;
let appending = false;
let timeout;

function setNotice(message = "", error = false) {
  notice.textContent = message;
  notice.hidden = !message;
  notice.classList.toggle("error", error);
}

function setBusy(busy) {
  const view = views[activeView];
  view.refresh.disabled = view.more.disabled = view.filter.disabled = busy;
}

function loadHistory(append = false) {
  if (!parentWindow || parentWindow.closed) {
    setNotice("Открой админ-панель заново с главной страницы LectureAI.", true);
    return;
  }
  const view = views[activeView];
  appending = append;
  setBusy(true);
  setNotice("Загружаем результаты…");
  parentWindow.postMessage({
    type: "eduflow-admin-history",
    requestId: ++requestId,
    view: activeView,
    ...(activeView === "events" ? { level: view.filter.value } : { result: view.filter.value }),
    before: append ? view.nextBefore : null,
  }, location.origin);
  clearTimeout(timeout);
  timeout = setTimeout(() => {
    setBusy(false);
    setNotice("LectureAI не ответил. Попробуй обновить журнал или открыть окно заново.", true);
  }, 20000);
}

function renderProcessingRow(item) {
  const row = document.createElement("tr");
  row.insertCell().textContent = item.filename || `Лекция ${item.lecture_id.slice(0, 8)}`;
  row.insertCell().textContent = item.username || item.user_email;
  const status = document.createElement("span");
  status.className = `status ${item.successful ? "completed" : "failed"}`;
  status.textContent = item.successful ? "Успешно" : "Ошибка";
  row.insertCell().append(status);
  const timestamp = item.finished_at || item.uploaded_at;
  const date = row.insertCell();
  date.textContent = dateFormat.format(new Date(timestamp));
  if (!item.finished_at) date.append(" (загрузка)");
  row.insertCell().textContent = item.error_message ? (failures[item.error_message] || item.error_message) : "—";
  return row;
}

function renderEventRow(item) {
  const row = document.createElement("tr");
  row.insertCell().textContent = dateFormat.format(new Date(item.created_at));
  const user = row.insertCell();
  user.textContent = item.username || item.user_email || "—";
  if (item.username && item.user_email) {
    const email = document.createElement("span");
    email.className = "admin-user-email";
    email.textContent = item.user_email;
    user.append(email);
  }
  const level = document.createElement("span");
  level.className = `status ${item.level === "error" ? "failed" : "completed"}`;
  level.textContent = item.level === "error" ? "Ошибка" : "Информация";
  row.insertCell().append(level);
  const event = row.insertCell();
  event.textContent = eventNames[item.event] || item.event;
  if (item.status_code != null) {
    const status = document.createElement("small");
    status.className = "admin-user-email";
    status.textContent = `HTTP ${item.status_code}`;
    event.append(status);
  }
  row.insertCell().textContent = item.lecture_id || "—";
  row.insertCell().textContent = item.error_type || "—";
  return row;
}

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin || event.source !== parentWindow) return;
  const data = event.data;
  if (data?.type !== "eduflow-admin-page" || data.requestId !== requestId || (data.view || "processing") !== activeView) return;
  clearTimeout(timeout);
  setBusy(false);
  if (!data.page) {
    if (data.error === 401 || data.error === 403) {
      workspace.hidden = true;
      docs.hidden = true;
      for (const view of Object.values(views)) view.rows.replaceChildren();
      setNotice("Войди в аккаунт администратора в основном окне LectureAI.", true);
    } else {
      setNotice("Не удалось загрузить журнал. Попробуй обновить его.", true);
    }
    return;
  }
  const view = views[activeView];
  const page = data.page;
  if (!appending) view.rows.replaceChildren();
  view.rows.append(...page.items.map(activeView === "events" ? renderEventRow : renderProcessingRow));
  if (activeView === "processing") byId("processing-counts").textContent = `Успешных: ${page.successful_count} · Неуспешных: ${page.failed_count}`;
  view.nextBefore = page.next_before;
  view.more.hidden = view.nextBefore === null;
  workspace.hidden = false;
  view.panel.hidden = false;
  docs.hidden = false;
  setNotice(view.rows.children.length ? "" : "По этому фильтру пока нет записей.");
});

function selectView(name) {
  activeView = name;
  byId("admin-view-title").textContent = views[name].title;
  for (const tab of tabs) {
    const selected = tab.dataset.adminView === name;
    tab.tabIndex = selected ? 0 : -1;
    tab.setAttribute("aria-selected", String(selected));
    views[tab.dataset.adminView].panel.hidden = !selected;
  }
  loadHistory();
}

for (const [index, tab] of tabs.entries()) {
  tab.addEventListener("click", () => selectView(tab.dataset.adminView));
  tab.addEventListener("keydown", (event) => {
    const keys = { ArrowLeft: (index + tabs.length - 1) % tabs.length, ArrowRight: (index + 1) % tabs.length, Home: 0, End: tabs.length - 1 };
    if (!Object.hasOwn(keys, event.key)) return;
    event.preventDefault();
    const next = tabs[keys[event.key]];
    selectView(next.dataset.adminView);
    next.focus();
  });
}
for (const view of Object.values(views)) {
  view.filter.addEventListener("change", () => loadHistory());
  view.refresh.addEventListener("click", () => loadHistory());
  view.more.addEventListener("click", () => loadHistory(true));
}
if (parentWindow) {
  workspace.hidden = false;
  selectView(activeView);
}
