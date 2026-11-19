const byId = (id) => document.getElementById(id);
const panel = byId("processing-panel");
const filter = byId("result-filter");
const refresh = byId("refresh-history");
const more = byId("more-processing");
const notice = byId("admin-notice");
const rows = byId("processing-rows");
const docs = byId("api-docs");
const parentWindow = window.opener;
const dateFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" });
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
let requestId = 0;
let nextBefore = null;
let appending = false;
let timeout;

function setNotice(message = "", error = false) {
  notice.textContent = message;
  notice.hidden = !message;
  notice.classList.toggle("error", error);
}

function setBusy(busy) {
  refresh.disabled = more.disabled = filter.disabled = busy;
}

function loadHistory(append = false) {
  if (!parentWindow || parentWindow.closed) return;
  appending = append;
  setBusy(true);
  setNotice("Загружаем результаты…");
  parentWindow.postMessage({
    type: "eduflow-admin-history",
    requestId: ++requestId,
    result: filter.value,
    before: append ? nextBefore : null,
  }, location.origin);
  clearTimeout(timeout);
  timeout = setTimeout(() => {
    setBusy(false);
    setNotice("LectureAI не ответил. Попробуй обновить журнал или открыть окно заново.", true);
  }, 20000);
}

function renderRow(item) {
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

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin || event.source !== parentWindow) return;
  const data = event.data;
  if (data?.type !== "eduflow-admin-page" || data.requestId !== requestId) return;
  clearTimeout(timeout);
  setBusy(false);
  if (!data.page) {
    if (data.error === 401 || data.error === 403) {
      panel.hidden = true;
      docs.hidden = true;
      rows.replaceChildren();
      setNotice("Войди в аккаунт администратора в основном окне LectureAI.", true);
    } else {
      setNotice("Не удалось загрузить журнал. Попробуй обновить его.", true);
    }
    return;
  }
  const page = data.page;
  if (!appending) rows.replaceChildren();
  rows.append(...page.items.map(renderRow));
  byId("processing-counts").textContent = `Успешных: ${page.successful_count} · Неуспешных: ${page.failed_count}`;
  nextBefore = page.next_before;
  more.hidden = nextBefore === null;
  panel.hidden = false;
  docs.hidden = false;
  setNotice(rows.children.length ? "" : "По этому фильтру пока нет обработок.");
});

filter.addEventListener("change", () => loadHistory());
refresh.addEventListener("click", () => loadHistory());
more.addEventListener("click", () => loadHistory(true));
if (parentWindow) {
  panel.hidden = false;
  loadHistory();
}
