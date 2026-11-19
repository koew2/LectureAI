const byId = (id) => document.getElementById(id);
const apiRoot = "/api/v1";
const pageSize = 30;
const states = { pending: "В очереди", processing: "Обрабатывается", completed: "Готово", failed: "Ошибка" };
const errors = {
  400: "Проверь введённые данные или выбранный файл.", 401: "Войди в аккаунт ещё раз.",
  404: "Лекция не найдена.", 409: "Запрос сейчас недоступен.",
  413: "Файл слишком большой.", 415: "Выбери файл MP3, WAV или M4A.",
  422: "Проверь данные в форме.", 429: "Слишком много запросов. Попробуй чуть позже.",
  503: "Сервис временно недоступен. Попробуй ещё раз.",
};
const tabs = [...document.querySelectorAll("[data-tab]")];
const authDialog = byId("auth-dialog");
const authForm = byId("auth-form");
const verifyForm = byId("verify-form");
const username = byId("username");
const password = byId("password");
const confirmation = byId("password-confirmation");
const emailCode = byId("email-code");
const fileInput = byId("audio-file");
const chatByLecture = new Map();

if ("IntersectionObserver" in window && matchMedia("(prefers-reduced-motion: no-preference)").matches) {
  const sections = new IntersectionObserver((entries, observer) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add("is-entering");
      observer.unobserve(entry.target);
    }
  });
  for (const section of document.querySelectorAll("main > .section, .site-footer")) {
    sections.observe(section);
  }
}
let registration = false;
let pendingRegistration = null;
let authBusy = false;
let session = null;
let adminWindow = null;
let refreshInFlight = null;
let loggingOut = false;
let lectures = [];
let libraryRequest = 0;
let libraryOffset = 0;
let hasMore = false;
let activeId = null;
let activeLecture = null;
let detailRequest = 0;
let pollTimer;
let preferredTab = "summary";
let cardIndex = 0;

function notice(id, message = "", isError = false) {
  const el = byId(id);
  el.textContent = message;
  el.hidden = !message;
  el.classList.toggle("error", isError);
}

function element(tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (className) node.className = className;
  return node;
}

function json(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

async function request(path, options = {}, token) {
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let response;
  try {
    response = await fetch(`${apiRoot}${path}`, { ...options, headers, redirect: "error" });
  } catch {
    throw new Error("Не удалось связаться с LectureAI. Проверь подключение.");
  }
  if (!response.ok) {
    const error = new Error(errors[response.status] || "Не удалось выполнить запрос. Попробуй ещё раз.");
    error.status = response.status;
    if (response.headers.get("content-type")?.includes("application/json")) {
      error.detail = (await response.json().catch(() => null))?.detail;
    }
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

async function renew(current, access) {
  if (session !== current || loggingOut) throw new Error(errors[401]);
  if (current.access_token !== access) return;
  if (!refreshInFlight) {
    refreshInFlight = request("/auth/refresh", json({ refresh_token: current.refresh_token }))
      .then((pair) => {
        if (session !== current) return;
        Object.assign(current, pair);
        updateAdminControls(pair.is_admin);
      })
      .catch((error) => {
        if (error.status === 401 && session === current) setSession(null);
        throw error;
      })
      .finally(() => { refreshInFlight = null; });
  }
  await refreshInFlight;
  if (session !== current || loggingOut) throw new Error(errors[401]);
}

async function authorized(path, options) {
  const current = session;
  if (!current || loggingOut) throw new Error(errors[401]);
  const access = current.access_token;
  try {
    return await request(path, options, access);
  } catch (error) {
    if (error.status !== 401) throw error;
    await renew(current, access);
    return request(path, options, current.access_token);
  }
}

function updateAdminControls(isAdmin) {
  byId("open-admin").hidden = byId("api-docs-footer").hidden = !isAdmin;
  if (!isAdmin) closeAdminWindow();
}

function setSession(value) {
  session = value;
  updateAdminControls(value?.is_admin);
  byId("guest-panel").hidden = !!value;
  byId("study-panel").hidden = !value;
  byId("account").textContent = value ? "Выйти ↗" : "Войти ↗";
  lectures = [];
  libraryOffset = 0;
  hasMore = false;
  activeId = null;
  activeLecture = null;
  clearTimeout(pollTimer);
  chatByLecture.clear();
  byId("lecture-result").hidden = true;
  byId("lecture-content").hidden = true;
  for (const id of ["lecture-title", "summary-content", "key-points", "transcript-content", "card-question", "card-answer", "chat-log"]) {
    byId(id).replaceChildren();
  }
  notice("lecture-notice");
  notice("chat-notice");
  notice("library-notice");
  byId("upload-form").reset();
  byId("chat-form").reset();
  updateChatControls();
  notice("upload-notice");
  renderLibrary();
  if (value) void loadLibrary();
}

function authMode(create) {
  registration = create;
  pendingRegistration = null;
  authForm.hidden = false;
  verifyForm.hidden = true;
  verifyForm.reset();
  byId("auth-title").textContent = create ? "Создать аккаунт" : "Войти";
  byId("auth-submit").textContent = create ? "Получить код" : "Войти";
  byId("switch-auth").textContent = create ? "Уже есть аккаунт? Войти" : "Создать аккаунт";
  password.autocomplete = create ? "new-password" : "current-password";
  password.minLength = create ? 10 : 1;
  byId("password-hint").hidden = !create;
  byId("confirmation-field").hidden = confirmation.disabled = !create;
  byId("username-field").hidden = username.disabled = !create;
  confirmation.value = "";
  resetPasswordVisibility();
  notice("auth-notice");
  notice("verify-notice");
}

function setPasswordVisibility(input, visible) {
  input.type = visible ? "text" : "password";
  const button = document.querySelector(`[data-password="${input.id}"]`);
  const name = input === password ? "пароль" : "подтверждение пароля";
  button.textContent = visible ? "Скрыть" : "Показать";
  button.setAttribute("aria-label", `${button.textContent} ${name}`);
}

function resetPasswordVisibility() {
  setPasswordVisibility(password, false);
  setPasswordVisibility(confirmation, false);
}

for (const input of [password, confirmation]) {
  document.querySelector(`[data-password="${input.id}"]`).addEventListener("click", () => {
    setPasswordVisibility(input, input.type === "password");
  });
}

function setAuthBusy(busy) {
  authBusy = busy;
  for (const control of authDialog.querySelectorAll("button, input")) {
    control.disabled = busy || (!registration && (control === confirmation || control === username));
  }
}

function showVerification(email) {
  authForm.hidden = true;
  verifyForm.hidden = false;
  byId("auth-title").textContent = "Подтвердить почту";
  byId("switch-auth").textContent = "Изменить данные регистрации";
  byId("code-destination").textContent = `Код отправлен на ${email}. Он действует 10 минут.`;
  verifyForm.reset();
  notice("verify-notice");
}

async function login(email, secret) {
  const pair = await request("/auth/login", {
    method: "POST", body: new URLSearchParams({ username: email, password: secret }),
  });
  setSession(pair);
  authDialog.close();
  location.hash = "workspace";
  fileInput.focus({ preventScroll: true });
}

function showAuth() {
  authMode(false);
  authDialog.showModal();
}

async function onAccount() {
  if (loggingOut) return;
  if (!session) {
    showAuth();
    return;
  }
  const current = session;
  const button = byId("account");
  loggingOut = true;
  button.disabled = true;
  try {
    await refreshInFlight?.catch(() => {});
    await request("/auth/logout", json({ refresh_token: current.refresh_token }));
    if (session === current) setSession(null);
  } catch (error) {
    if (error.status === 401 && session === current) setSession(null);
    else if (session === current) notice("library-notice", error.message, true);
  } finally {
    loggingOut = false;
    button.disabled = false;
  }
}

byId("account").addEventListener("click", onAccount);

function closeAdminWindow() {
  adminWindow?.close();
  adminWindow = null;
}

window.addEventListener("pagehide", closeAdminWindow);
byId("open-admin").addEventListener("click", () => {
  if (!session?.is_admin) return;
  if (adminWindow && !adminWindow.closed) {
    adminWindow.focus();
    return;
  }
  adminWindow = window.open("/admin", "eduflow-admin", "popup,width=1100,height=760");
  if (!adminWindow) {
    byId("workspace").scrollIntoView();
    notice("library-notice", "Разреши всплывающее окно для LectureAI и нажми кнопку ещё раз.", true);
  }
});

window.addEventListener("message", async (event) => {
  if (event.origin !== location.origin || !adminWindow || event.source !== adminWindow) return;
  const { type, requestId, result, before } = event.data || {};
  if (type !== "eduflow-admin-history" || !Number.isInteger(requestId)) return;
  if (!["all", "completed", "failed"].includes(result)) return;
  if (before != null && (!Number.isSafeInteger(before) || before < 1)) return;
  const target = adminWindow;
  const current = session;
  try {
    if (!current?.is_admin) throw new Error(errors[401]);
    const query = new URLSearchParams({ result });
    if (before != null) query.set("before", before);
    const page = await authorized(`/admin/processing?${query}`);
    if (session !== current || adminWindow !== target) return;
    target.postMessage({ type: "eduflow-admin-page", requestId, page }, location.origin);
  } catch (error) {
    if (session !== current || adminWindow !== target || target.closed) return;
    target.postMessage({ type: "eduflow-admin-page", requestId, error: error.status || 0 }, location.origin);
  }
});

byId("workspace-login").addEventListener("click", showAuth);
byId("start-lecture").addEventListener("click", (event) => {
  if (session || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  showAuth();
});
byId("switch-auth").addEventListener("click", () => authMode(!!pendingRegistration || !registration));
byId("close-auth").addEventListener("click", () => authDialog.close());
authDialog.addEventListener("cancel", (event) => {
  if (authBusy) event.preventDefault();
});
authDialog.addEventListener("close", () => {
  pendingRegistration = null;
  password.value = confirmation.value = emailCode.value = "";
  username.value = "";
  resetPasswordVisibility();
});
authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (authBusy) return;
  const email = byId("email").value.trim();
  const secret = password.value;
  const creating = registration;
  const displayName = username.value.trim();
  if (creating && !displayName) {
    notice("auth-notice", "Укажи имя пользователя.", true);
    username.focus();
    return;
  }
  if (creating && secret !== confirmation.value) {
    notice("auth-notice", "Пароли не совпадают.", true);
    confirmation.focus();
    return;
  }
  setAuthBusy(true);
  notice("auth-notice", creating ? "Отправляем код…" : "Входим…");
  try {
    if (creating) {
      const details = { email, username: displayName, password: secret, password_confirmation: confirmation.value };
      await request("/auth/register", json(details));
      pendingRegistration = details;
      password.value = confirmation.value = "";
      resetPasswordVisibility();
      showVerification(email);
    } else {
      await login(email, secret);
    }
  } catch (error) {
    let message = error.message;
    if (error.status === 401) message = "Проверь почту и пароль.";
    if (creating && error.status === 409) message = "Аккаунт с этой почтой уже существует. Войди в него.";
    if (creating && error.status === 503) message = "Не удалось отправить письмо. Попробуй ещё раз позже.";
    notice("auth-notice", message, true);
  } finally {
    setAuthBusy(false);
    if (pendingRegistration) emailCode.focus();
  }
});

verifyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (authBusy || !pendingRegistration) return;
  const details = pendingRegistration;
  const code = emailCode.value.trim();
  setAuthBusy(true);
  notice("verify-notice", "Проверяем код…");
  try {
    await request("/auth/register/confirm", json({ ...details, code }));
    authMode(false);
    try {
      await login(details.email, details.password);
    } catch {
      notice("auth-notice", "Почта подтверждена. Теперь можно войти.");
    }
  } catch (error) {
    const message = error.status === 400 ? "Код неверный, истёк или исчерпаны попытки. Проверь письмо или запроси новый код." : error.message;
    notice("verify-notice", message, true);
  } finally {
    setAuthBusy(false);
  }
});

byId("resend-code").addEventListener("click", async () => {
  if (authBusy || !pendingRegistration) return;
  setAuthBusy(true);
  notice("verify-notice", "Отправляем код…");
  try {
    await request("/auth/register", json(pendingRegistration));
    emailCode.value = "";
    notice("verify-notice", "Отправили новый код. Используй последнее письмо.");
  } catch (error) {
    notice("verify-notice", error.status === 429 ? "Подожди минуту между запросами кода." : error.message, true);
  } finally {
    setAuthBusy(false);
    emailCode.focus();
  }
});

function lectureName(lecture) {
  return lecture.filename || `Лекция ${lecture.id.slice(0, 8)}`;
}

function renderLibrary() {
  const list = byId("lecture-list");
  list.replaceChildren();
  for (const lecture of lectures) {
    const row = element("button", undefined, "lecture-row");
    row.type = "button";
    if (lecture.id === activeId) row.setAttribute("aria-current", "true");
    const title = element("span");
    title.append(element("strong", lectureName(lecture)), element("small", new Date(lecture.created_at).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })));
    row.append(title, element("span", states[lecture.status], `status ${lecture.status}`));
    row.addEventListener("click", () => openLecture(lecture.id));
    const item = element("li");
    item.append(row);
    list.append(item);
  }
  byId("more-lectures").hidden = !hasMore;
}

async function loadLibrary(append = false) {
  const current = session;
  const run = ++libraryRequest;
  const controls = [byId("reload-list"), byId("more-lectures")];
  controls.forEach((control) => { control.disabled = true; });
  notice("library-notice", "Загружаем список…");
  try {
    const offset = append ? libraryOffset : 0;
    const items = await authorized(`/lectures?offset=${offset}&limit=${pageSize}`);
    if (session !== current || run !== libraryRequest) return;
    lectures = append ? lectures.concat(items.filter((item) => !lectures.some(({ id }) => id === item.id))) : items;
    libraryOffset = offset + items.length;
    hasMore = items.length === pageSize;
    renderLibrary();
    notice("library-notice", lectures.length ? "" : "Здесь появятся загруженные лекции.");
  } catch (error) {
    if (session === current && run === libraryRequest) notice("library-notice", error.message, true);
  } finally {
    if (run === libraryRequest) controls.forEach((control) => { control.disabled = false; });
  }
}

byId("reload-list").addEventListener("click", () => loadLibrary());
byId("more-lectures").addEventListener("click", () => loadLibrary(true));

const dropZone = byId("drop-zone");
dropZone.addEventListener("dragover", (event) => { event.preventDefault(); dropZone.classList.add("is-dragging"); });
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("is-dragging"));
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("is-dragging");
  if (fileInput.disabled || !event.dataTransfer.files.length) return;
  const selection = new DataTransfer();
  selection.items.add(event.dataTransfer.files[0]);
  fileInput.files = selection.files;
  notice("upload-notice");
});
fileInput.addEventListener("change", () => notice("upload-notice"));
byId("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const current = session;
  const file = fileInput.files[0];
  if (!file || !/\.(mp3|wav|m4a)$/i.test(file.name) || !file.size) {
    notice("upload-notice", "Выбери непустой файл MP3, WAV или M4A.", true);
    return;
  }
  const data = new FormData();
  data.set("file", file);
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = fileInput.disabled = true;
  notice("upload-notice", "Отправляем запись…");
  try {
    const result = await authorized("/lectures/upload", { method: "POST", body: data });
    if (session !== current) return;
    fileInput.value = "";
    notice("upload-notice", "Запись добавлена в библиотеку.");
    void loadLibrary();
    openLecture(result.id, file.name);
  } catch (error) {
    if (session !== current) return;
    const failedLectureId = error.detail?.lecture_id;
    if (error.status === 503 && typeof failedLectureId === "string") {
      notice("upload-notice", "Очередь обработки недоступна. Попробуй загрузить запись позже.", true);
      void loadLibrary();
      openLecture(failedLectureId, file.name);
    } else {
      notice("upload-notice", error.message, true);
    }
  } finally {
    submit.disabled = fileInput.disabled = false;
  }
});

function openLecture(id, filename) {
  activeId = id;
  activeLecture = null;
  cardIndex = 0;
  byId("chat-form").reset();
  byId("question").value = chatByLecture.get(id)?.question || "";
  notice("chat-notice");
  byId("lecture-result").hidden = false;
  byId("lecture-content").hidden = true;
  byId("lecture-title").textContent = filename || lectureName(lectures.find((item) => item.id === id) || { id });
  notice("lecture-notice", "Загружаем лекцию…");
  renderLibrary();
  byId("lecture-result").scrollIntoView();
  byId("lecture-title").focus({ preventScroll: true });
  void loadLecture();
}

function isProcessing(lecture) {
  return lecture?.status === "pending" || lecture?.status === "processing";
}

async function loadLecture() {
  clearTimeout(pollTimer);
  if (!activeId || !session) return;
  if (loggingOut) {
    pollTimer = setTimeout(loadLecture, 3500);
    return;
  }
  const id = activeId;
  const current = session;
  const run = ++detailRequest;
  const refresh = byId("refresh-lecture");
  refresh.disabled = true;
  try {
    const data = await authorized(`/lectures/${id}`);
    if (session !== current || run !== detailRequest) return;
    const previousStatus = activeLecture?.status;
    activeLecture = data;
    lectures = lectures.map((item) => item.id === id ? data : item);
    renderLibrary();
    byId("lecture-title").textContent = lectureName(data);
    notice("lecture-notice", statusText(data), data.status === "failed");
    if (data.status === "completed" && previousStatus !== "completed") renderResults(data);
    if (isProcessing(data)) pollTimer = setTimeout(loadLecture, 3500);
  } catch (error) {
    if (session !== current || run !== detailRequest) return;
    if (loggingOut) {
      pollTimer = setTimeout(loadLecture, 3500);
      return;
    }
    const temporary = error.status == null || error.status === 429 || error.status >= 500;
    if (temporary && (!activeLecture || isProcessing(activeLecture))) {
      notice("lecture-notice", "Не удалось обновить статус. Повторим проверку автоматически.", true);
      pollTimer = setTimeout(loadLecture, 7000);
    } else {
      notice("lecture-notice", error.message, true);
    }
  } finally {
    if (run === detailRequest) refresh.disabled = false;
  }
}

function statusText(lecture) {
  if (lecture.status === "pending") return "В очереди. Статус обновляется автоматически.";
  if (lecture.status === "processing") return "Запись обрабатывается. Статус обновляется автоматически.";
  if (lecture.status === "failed") {
    if (lecture.error_message?.startsWith("AI output limit")) return "Расшифровка прошла, но ответ AI для конспекта оборвался. Попробуй загрузить запись ещё раз.";
    if (lecture.error_message?.startsWith("AI processing")) return "Расшифровка прошла, но AI не смог создать конспект. Попробуй загрузить запись ещё раз.";
    if (lecture.error_message?.startsWith("Task queue")) return "Очередь обработки недоступна. Попробуй загрузить запись позже.";
    if (lecture.error_message === "No speech was detected in the recording") return "В записи не обнаружена речь. Проверь звук и попробуй ещё раз.";
    if (lecture.error_message === "Audio duration exceeds the configured limit or is invalid") return "Длительность записи превышает лимит или её не удалось определить.";
    if (["Unable to decode the audio file", "File contents do not match a supported audio format"].includes(lecture.error_message)) return "Не удалось прочитать запись. Проверь файл и попробуй ещё раз.";
    return "Не удалось обработать запись. Попробуй загрузить её ещё раз позже.";
  }
  return lecture.duration == null ? "Готово" : `Готово · ${clock(lecture.duration)}`;
}

byId("refresh-lecture").addEventListener("click", loadLecture);

function selectTab(name) {
  preferredTab = name;
  for (const button of tabs) {
    const selected = button.dataset.tab === name;
    button.tabIndex = selected ? 0 : -1;
    button.setAttribute("aria-selected", String(selected));
    byId(`view-${button.dataset.tab}`).hidden = !selected;
  }
}

for (const [index, tab] of tabs.entries()) {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  tab.addEventListener("keydown", (event) => {
    const keys = { ArrowLeft: (index + tabs.length - 1) % tabs.length, ArrowRight: (index + 1) % tabs.length, Home: 0, End: tabs.length - 1 };
    if (!Object.hasOwn(keys, event.key)) return;
    event.preventDefault();
    const next = tabs[keys[event.key]];
    selectTab(next.dataset.tab);
    next.focus();
  });
}
for (const link of document.querySelectorAll("[data-result-view]")) {
  link.addEventListener("click", (event) => {
    selectTab(link.dataset.resultView);
    if (!activeLecture || activeLecture.status !== "completed") return;
    event.preventDefault();
    location.hash = "lecture-result";
    byId(`tab-${preferredTab}`).focus({ preventScroll: true });
  });
}

function clock(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function renderMarkdown(target, source) {
  target.replaceChildren();
  let list = null;
  for (const line of source.split("\n")) {
    const heading = /^(#{1,6})\s+(.+)$/.exec(line.trim());
    const bullet = /^[-*+]\s+(.+)$/.exec(line.trim());
    const numbered = /^\d+[.)]\s+(.+)$/.exec(line.trim());
    const text = (heading?.[2] || bullet?.[1] || numbered?.[1] || line).trim();
    if (bullet || numbered) {
      const tag = bullet ? "UL" : "OL";
      if (list?.tagName !== tag) {
        list = element(tag.toLowerCase());
        target.append(list);
      }
      addFormattedText(list.appendChild(element("li")), text);
    } else {
      list = null;
      if (text) addFormattedText(target.appendChild(element(heading ? `h${Math.min(6, heading[1].length + 3)}` : "p")), text);
    }
  }
}

function addFormattedText(node, text) {
  for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)) {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) node.append(element("strong", part.slice(2, -2)));
    else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) node.append(element("code", part.slice(1, -1)));
    else node.append(document.createTextNode(part));
  }
}

function renderResults(lecture) {
  const summary = lecture.summary;
  renderMarkdown(byId("summary-content"), summary?.structured_markdown || "Конспект пока отсутствует.");
  byId("key-points").replaceChildren(...(summary?.key_points_json || []).map((point) => element("li", point)));
  const transcript = lecture.transcription;
  const segments = (transcript?.timestamps_json || []).map((part) => {
    const row = element("div", undefined, "segment");
    row.append(element("time", clock(part.start)), element("p", part.text));
    return row;
  });
  byId("transcript-content").replaceChildren(...(segments.length ? segments : [element("p", transcript?.clean_text || "Расшифровка пока отсутствует.")]));
  byId("download-transcript").disabled = !transcript?.clean_text;
  byId("chat-form").hidden = !transcript;
  renderCard();
  renderChat();
  selectTab(preferredTab);
  byId("lecture-content").hidden = false;
}

byId("download-transcript").addEventListener("click", () => {
  if (!activeLecture?.transcription) return;
  const url = URL.createObjectURL(new Blob([activeLecture.transcription.clean_text], { type: "text/plain;charset=utf-8" }));
  const link = element("a");
  link.href = url;
  link.download = `${lectureName(activeLecture).replace(/\.[^.]+$/, "")}.txt`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

function renderCard() {
  const cards = activeLecture?.summary?.flashcards || [];
  const card = cards[cardIndex];
  byId("card-count").textContent = card ? `Карточка ${cardIndex + 1} из ${cards.length}` : "";
  byId("card-question").textContent = card?.question || "Карточки пока отсутствуют.";
  byId("card-answer").textContent = card?.answer || "";
  byId("card-answer").hidden = true;
  const reveal = byId("reveal-card");
  reveal.hidden = !card;
  reveal.textContent = "Показать ответ";
  reveal.setAttribute("aria-expanded", "false");
  byId("previous-card").disabled = cardIndex === 0;
  byId("next-card").disabled = cardIndex + 1 >= cards.length;
}
byId("reveal-card").addEventListener("click", () => {
  const answer = byId("card-answer");
  answer.hidden = !answer.hidden;
  const reveal = byId("reveal-card");
  reveal.textContent = answer.hidden ? "Показать ответ" : "Скрыть ответ";
  reveal.setAttribute("aria-expanded", String(!answer.hidden));
});
byId("previous-card").addEventListener("click", () => { cardIndex--; renderCard(); });
byId("next-card").addEventListener("click", () => { cardIndex++; renderCard(); });

function updateChatControls() {
  const chat = chatByLecture.get(activeId);
  const busy = chat?.pending === true;
  byId("question").disabled = byId("chat-submit").disabled = busy;
  notice("chat-notice", busy ? "Тьютор готовит ответ…" : chat?.error, !!chat?.error);
}

function renderChat() {
  const log = byId("chat-log");
  const messages = chatByLecture.get(activeId)?.messages || [];
  log.replaceChildren(...messages.map(({ author, text }) => {
    const bubble = element("div", undefined, `chat-bubble ${author === "Ты" ? "user" : "tutor"}`);
    bubble.append(element("strong", author), element("p", text));
    return bubble;
  }));
  log.scrollTop = log.scrollHeight;
  updateChatControls();
}

byId("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const current = session;
  const id = activeId;
  if (!id || activeLecture?.status !== "completed") return;
  const input = byId("question");
  const question = input.value.trim();
  if (!question) { notice("chat-notice", "Напиши вопрос по лекции.", true); return; }
  const chat = chatByLecture.get(id) || { messages: [], pending: false, error: "" };
  if (chat.pending) return;
  chat.pending = true;
  chat.question = question;
  chat.error = "";
  chatByLecture.set(id, chat);
  updateChatControls();
  try {
    const response = await authorized(`/lectures/${id}/chat`, json({ question }));
    if (session !== current) return;
    chat.messages.push({ author: "Ты", text: question }, { author: "Тьютор", text: response.answer });
    chat.question = "";
    if (id === activeId) input.value = "";
  } catch (error) {
    chat.error = error.message;
  } finally {
    chat.pending = false;
    if (session === current && id === activeId) renderChat();
  }
});
