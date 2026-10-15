(() => {
  "use strict";

  const REFRESH_INTERVAL_MS = 30_000;
  const RECORDING_NAME = /^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:__[A-Za-z0-9][A-Za-z0-9_.-]{0,63})?\.mp4$/;
  const STARTED_AT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/;

  const elements = {
    healthDot: document.getElementById("healthDot"),
    serverStatus: document.getElementById("serverStatus"),
    archiveStatus: document.getElementById("archiveStatus"),
    refreshButton: document.getElementById("refreshButton"),
    refreshLabel: document.getElementById("refreshLabel"),
    totalCount: document.getElementById("totalCount"),
    resultCount: document.getElementById("resultCount"),
    searchInput: document.getElementById("searchInput"),
    dateInput: document.getElementById("dateInput"),
    clearFilters: document.getElementById("clearFilters"),
    retryButton: document.getElementById("retryButton"),
    listError: document.getElementById("listError"),
    listErrorText: document.getElementById("listErrorText"),
    loadingMessage: document.getElementById("loadingMessage"),
    emptyState: document.getElementById("emptyState"),
    emptyTitle: document.getElementById("emptyTitle"),
    emptyDescription: document.getElementById("emptyDescription"),
    emptyReset: document.getElementById("emptyReset"),
    recordingsList: document.getElementById("recordingsList"),
    playerPanel: document.getElementById("playerPanel"),
    playerFileName: document.getElementById("playerFileName"),
    videoPlayer: document.getElementById("videoPlayer"),
    playerError: document.getElementById("playerError"),
    closePlayer: document.getElementById("closePlayer"),
    liveControls: document.getElementById("liveControls"),
    liveSourceSelect: document.getElementById("liveSourceSelect"),
    liveStart: document.getElementById("liveStart"),
    liveStop: document.getElementById("liveStop"),
    liveStatus: document.getElementById("liveStatus"),
    liveFrame: document.getElementById("liveFrame"),
    liveImage: document.getElementById("liveImage"),
  };

  let recordings = [];
  let hasLoadedRecordings = false;
  let currentName = null;
  let refreshPromise = null;
  let activeController = null;
  let refreshTimer = null;
  let pageIsLeaving = false;
  let liveSources = [];
  let liveRequestActive = false;
  let liveConnectTimer = null;

  function makeElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  async function getJson(path, signal) {
    const response = await fetch(path, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
    if (!response.ok) {
      let detail = "";
      try {
        const body = await response.json();
        if (typeof body.error === "string") detail = `: ${body.error}`;
      } catch (_) {
        // The status code remains useful when an upstream error is not JSON.
      }
      throw new Error(`HTTP ${response.status}${detail}`);
    }
    return response.json();
  }

  function archiveStateLabel(state) {
    const labels = {
      running: "Запись активна",
      degraded: "Источник переподключается",
      stopped: "Запись остановлена",
      disabled: "Источник не настроен",
    };
    return labels[state] || "Состояние архива неизвестно";
  }

  function archiveStatusText(archive) {
    const cameras = archive && Array.isArray(archive.cameras) ? archive.cameras : [];
    if (cameras.length < 2) return archiveStateLabel(archive && archive.state);
    const issueCount = cameras.filter((camera) => camera.state !== "running").length;
    if (issueCount) return `Камер: ${cameras.length} · сбоев: ${issueCount}`;
    return `Камер: ${cameras.length} · запись активна`;
  }

  function readableLoadError(error) {
    if (error && error.message && error.message.startsWith("HTTP ")) {
      return `Сервер отклонил запрос (${error.message}).`;
    }
    return "Не удалось связаться с сервером и загрузить записи. Проверьте соединение и повторите попытку.";
  }

  async function refreshHealth(signal) {
    try {
      const data = await getJson("/api/health", signal);
      if (!data || data.status !== "ok") throw new Error("Сервер не подтвердил готовность API");
      elements.healthDot.dataset.state = "online";
      elements.serverStatus.textContent = "Сервер доступен";
      elements.archiveStatus.textContent = data.archive && typeof data.archive.state === "string"
        ? archiveStatusText(data.archive)
        : "Состояние архива не получено";
      const cameras = data.archive && Array.isArray(data.archive.cameras) ? data.archive.cameras : [];
      elements.archiveStatus.title = cameras.length > 1
        ? cameras.map((camera) => {
          const label = camera.id || "Камера без ID";
          const detail = camera.last_error ? `: ${camera.last_error}` : "";
          return `${label} — ${archiveStateLabel(camera.state)}${detail}`;
        }).join("; ")
        : "";
    } catch (error) {
      if (error.name === "AbortError") return;
      elements.healthDot.dataset.state = "offline";
      elements.serverStatus.textContent = "Сервер недоступен";
      elements.archiveStatus.textContent = "Не удалось проверить API";
    }
  }

  function setLiveStatus(message, state) {
    elements.liveStatus.textContent = message;
    elements.liveStatus.dataset.state = state || "";
  }

  function stopLivePreview(message) {
    liveRequestActive = false;
    if (liveConnectTimer !== null) {
      window.clearTimeout(liveConnectTimer);
      liveConnectTimer = null;
    }
    elements.liveImage.removeAttribute("src");
    elements.liveImage.hidden = true;
    elements.liveFrame.hidden = true;
    elements.liveStart.disabled = false;
    elements.liveStop.hidden = true;
    if (message) setLiveStatus(message);
  }

  function renderLiveSources(sources) {
    const previousId = elements.liveSourceSelect.value;
    const nextSources = Array.isArray(sources)
      ? sources.filter((item) => item && typeof item.id === "string" && typeof item.label === "string")
      : [];
    const previousStillExists = nextSources.some((item) => item.id === previousId);
    const currentId = elements.liveSourceSelect.value;
    if (liveRequestActive && !nextSources.some((item) => item.id === currentId)) {
      stopLivePreview("Выбранный источник больше не доступен.");
    }
    liveSources = nextSources;
    elements.liveSourceSelect.replaceChildren();
    for (const source of liveSources) {
      const option = document.createElement("option");
      option.value = source.id;
      option.textContent = source.label;
      elements.liveSourceSelect.append(option);
    }

    if (liveSources.length === 0) {
      stopLivePreview();
      elements.liveControls.hidden = true;
      setLiveStatus("Источник не настроен. Подключите RTSP/HTTP-камеру или запустите синтетический источник для демонстрации.");
      return;
    }

    elements.liveControls.hidden = false;
    elements.liveSourceSelect.value = previousStillExists ? previousId : liveSources[0].id;
    if (!liveRequestActive) {
      setLiveStatus("Выберите источник и нажмите «Открыть эфир».");
    }
  }

  async function refreshLiveSources(signal) {
    try {
      const data = await getJson("/api/live/sources", signal);
      renderLiveSources(data && data.sources);
    } catch (error) {
      if (error.name === "AbortError") return;
      stopLivePreview();
      elements.liveControls.hidden = true;
      setLiveStatus("Не удалось получить список источников для прямого просмотра.", "error");
    }
  }

  function startLivePreview() {
    const sourceId = elements.liveSourceSelect.value;
    if (!sourceId || !liveSources.some((source) => source.id === sourceId)) return;
    stopLivePreview();
    liveRequestActive = true;
    elements.liveImage.alt = `Прямой видеопоток: ${elements.liveSourceSelect.selectedOptions[0].textContent}`;
    elements.liveImage.hidden = false;
    elements.liveFrame.hidden = false;
    elements.liveStart.disabled = true;
    elements.liveStop.hidden = false;
    setLiveStatus("Подключаемся к потоку…");
    const query = new URLSearchParams({ source_id: sourceId, t: String(Date.now()) });
    elements.liveImage.src = `/api/live.mjpeg?${query.toString()}`;
    const deadline = Date.now() + 15_000;
    const checkForFirstFrame = () => {
      liveConnectTimer = null;
      if (!liveRequestActive) return;
      if (elements.liveImage.naturalWidth > 0 && elements.liveImage.naturalHeight > 0) {
        markLivePreviewConnected();
      } else if (Date.now() >= deadline) {
        elements.liveStart.disabled = false;
        setLiveStatus("Поток ещё не появился. Проверьте доступность камеры и её адрес.", "error");
      } else {
        liveConnectTimer = window.setTimeout(checkForFirstFrame, 250);
      }
    };
    liveConnectTimer = window.setTimeout(checkForFirstFrame, 250);
  }

  function markLivePreviewConnected() {
    if (!liveRequestActive) return;
    if (liveConnectTimer !== null) {
      window.clearTimeout(liveConnectTimer);
      liveConnectTimer = null;
    }
    elements.liveStart.disabled = false;
    setLiveStatus("Прямой эфир подключён.", "online");
  }

  function markLivePreviewFailed() {
    if (!liveRequestActive) return;
    if (liveConnectTimer !== null) {
      window.clearTimeout(liveConnectTimer);
      liveConnectTimer = null;
    }
    elements.liveStart.disabled = false;
    setLiveStatus("Не удалось получить изображение. Проверьте состояние источника и повторите попытку.", "error");
  }

  function isRecording(item) {
    return Boolean(
      item &&
      typeof item.name === "string" && RECORDING_NAME.test(item.name) &&
      typeof item.started_at === "string" && STARTED_AT.test(item.started_at)
    );
  }

  async function refreshRecordings(signal) {
    if (!hasLoadedRecordings) elements.loadingMessage.hidden = false;
    try {
      const data = await getJson("/api/videos", signal);
      if (!data || !Array.isArray(data.videos)) throw new Error("Ответ API не содержит список записей");
      recordings = data.videos.filter(isRecording).slice().sort((a, b) =>
        b.started_at.localeCompare(a.started_at)
      );
      hasLoadedRecordings = true;
      elements.listError.hidden = true;
      elements.loadingMessage.hidden = true;
      renderRecordings();
    } catch (error) {
      if (error.name === "AbortError") return;
      elements.loadingMessage.hidden = true;
      elements.listErrorText.textContent = readableLoadError(error);
      elements.listError.hidden = false;
      elements.emptyState.hidden = true;
      if (!hasLoadedRecordings) {
        elements.resultCount.textContent = "Список недоступен";
        elements.emptyState.hidden = true;
      }
    }
  }

  function refreshAll() {
    if (refreshPromise) return refreshPromise;
    if (pageIsLeaving) return Promise.resolve();

    elements.refreshButton.disabled = true;
    elements.refreshLabel.textContent = "Обновляем…";
    activeController = new AbortController();
    const signal = activeController.signal;
    refreshPromise = Promise.all([
      refreshHealth(signal),
      refreshRecordings(signal),
      refreshLiveSources(signal),
    ]).finally(() => {
      refreshPromise = null;
      activeController = null;
      elements.refreshButton.disabled = false;
      elements.refreshLabel.textContent = "Обновить";
    });
    return refreshPromise;
  }

  function filteredRecordings() {
    const query = elements.searchInput.value.trim().toLocaleLowerCase("ru-RU");
    const selectedDate = elements.dateInput.value;
    return recordings.filter((item) => {
      const matchesName = !query || item.name.toLocaleLowerCase("ru-RU").includes(query);
      const matchesDate = !selectedDate || item.started_at.slice(0, 10) === selectedDate;
      return matchesName && matchesDate;
    });
  }

  function formatDate(value) {
    const parts = value.split("-").map(Number);
    const date = new Date(parts[0], parts[1] - 1, parts[2]);
    return new Intl.DateTimeFormat("ru-RU", {
      day: "numeric", month: "short", year: "numeric",
    }).format(date);
  }

  function formatDuration(value) {
    if (value === null || value === undefined || value === "") return "—";
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remaining = total % 60;
    const pad = (part) => String(part).padStart(2, "0");
    return hours ? `${pad(hours)}:${pad(minutes)}:${pad(remaining)}` : `${pad(minutes)}:${pad(remaining)}`;
  }

  function formatSize(value) {
    if (value === null || value === undefined || value === "") return "—";
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "—";
    if (bytes < 1024) return `${bytes} Б`;
    const units = ["КБ", "МБ", "ГБ", "ТБ"];
    let amount = bytes / 1024;
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) {
      amount /= 1024;
      unit += 1;
    }
    return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(amount)} ${units[unit]}`;
  }

  function addMeta(card, label, value, extraClass) {
    const block = makeElement("div", `recording-meta${extraClass ? ` ${extraClass}` : ""}`);
    block.append(makeElement("span", "", label), makeElement("strong", "", value));
    card.append(block);
  }

  function createRecordingCard(item) {
    const card = makeElement("li", `recording-card${item.name === currentName ? " is-selected" : ""}`);
    const main = makeElement("div", "recording-main");
    main.append(makeElement("span", "file-badge", "MP4"));
    const fileCopy = makeElement("div", "file-copy");
    const filename = makeElement("p", "recording-file", item.name);
    filename.title = item.name;
    fileCopy.append(
      filename,
      makeElement("p", "recording-date", formatDate(item.started_at.slice(0, 10)))
    );
    main.append(fileCopy);
    card.append(main);
    addMeta(card, "Время начала", item.started_at.slice(11, 19));
    addMeta(card, "Длительность", formatDuration(item.duration_seconds));
    addMeta(card, "Размер файла", formatSize(item.size_bytes), "size-meta");

    const watch = makeElement("button", "button watch-button");
    watch.type = "button";
    watch.setAttribute("aria-label", `Смотреть ${item.name}`);
    watch.append(document.createElementNS("http://www.w3.org/2000/svg", "svg"));
    const icon = watch.firstElementChild;
    icon.setAttribute("viewBox", "0 0 20 20");
    icon.setAttribute("fill", "none");
    icon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "m7 4.8 8 5.2-8 5.2V4.8Z");
    path.setAttribute("fill", "currentColor");
    icon.append(path);
    watch.append(makeElement("span", "", "Смотреть"));
    watch.addEventListener("click", () => selectRecording(item));
    card.append(watch);
    return card;
  }

  function recordingCountLabel(count) {
    const lastTwo = count % 100;
    const last = count % 10;
    const noun = lastTwo >= 11 && lastTwo <= 14
      ? "записей"
      : last === 1 ? "запись" : [2, 3, 4].includes(last) ? "записи" : "записей";
    return `${count} ${noun}`;
  }

  function renderRecordings() {
    const visible = filteredRecordings();
    const hasFilters = Boolean(elements.searchInput.value.trim() || elements.dateInput.value);
    elements.totalCount.textContent = String(recordings.length);
    elements.resultCount.textContent = hasFilters
      ? `Найдено ${visible.length} из ${recordings.length}`
      : `Показано ${visible.length}`;
    elements.clearFilters.disabled = !hasFilters;
    elements.recordingsList.replaceChildren();

    const shouldShowEmpty = hasLoadedRecordings && visible.length === 0;
    elements.emptyState.hidden = !shouldShowEmpty;
    if (shouldShowEmpty) {
      if (recordings.length === 0 && !hasFilters) {
        elements.emptyTitle.textContent = "В архиве пока нет записей";
        elements.emptyDescription.textContent = "Новые файлы появятся здесь после сохранения на сервере.";
        elements.emptyReset.hidden = true;
      } else {
        elements.emptyTitle.textContent = "Записи не найдены";
        elements.emptyDescription.textContent = "Измените запрос или сбросьте фильтры, чтобы увидеть другие записи.";
        elements.emptyReset.hidden = !hasFilters;
      }
    }

    const groups = new Map();
    for (const item of visible) {
      const cameraId = typeof item.camera_id === "string" && item.camera_id.trim()
        ? item.camera_id
        : null;
      if (!groups.has(cameraId)) groups.set(cameraId, []);
      groups.get(cameraId).push(item);
    }

    for (const [cameraId, items] of groups) {
      const group = makeElement("li", "source-group");
      const heading = makeElement("div", "source-group-heading");
      const label = cameraId ? `Камера ${cameraId}` : "Источник без идентификатора";
      heading.append(
        makeElement("h3", "source-group-title", label),
        makeElement("span", "source-group-count", recordingCountLabel(items.length))
      );
      const list = makeElement("ul", "source-recordings");
      list.setAttribute("aria-label", label);
      for (const item of items) list.append(createRecordingCard(item));
      group.append(heading, list);
      elements.recordingsList.append(group);
    }
  }

  async function selectRecording(item) {
    currentName = item.name;
    elements.playerFileName.textContent = item.name;
    elements.playerError.hidden = true;
    elements.playerPanel.hidden = false;
    elements.videoPlayer.pause();
    elements.videoPlayer.src = `/api/videos/${encodeURIComponent(item.name)}`;
    elements.videoPlayer.load();
    renderRecordings();
    elements.playerPanel.scrollIntoView({ block: "nearest" });
    try {
      await elements.videoPlayer.play();
    } catch (error) {
      if (error.name !== "AbortError") elements.playerError.hidden = false;
    }
  }

  function closePlayer() {
    currentName = null;
    elements.videoPlayer.pause();
    elements.videoPlayer.removeAttribute("src");
    elements.videoPlayer.load();
    elements.playerError.hidden = true;
    elements.playerPanel.hidden = true;
    renderRecordings();
  }

  function stopAutoRefresh(abortRequest) {
    if (refreshTimer !== null) {
      window.clearInterval(refreshTimer);
      refreshTimer = null;
    }
    if (abortRequest && activeController) activeController.abort();
  }

  function startAutoRefresh() {
    if (refreshTimer === null && !document.hidden && !pageIsLeaving) {
      refreshTimer = window.setInterval(() => refreshAll(), REFRESH_INTERVAL_MS);
    }
  }

  elements.refreshButton.addEventListener("click", () => refreshAll());
  elements.retryButton.addEventListener("click", () => refreshAll());
  elements.searchInput.addEventListener("input", renderRecordings);
  elements.dateInput.addEventListener("change", renderRecordings);
  elements.clearFilters.addEventListener("click", () => {
    elements.searchInput.value = "";
    elements.dateInput.value = "";
    renderRecordings();
  });
  elements.emptyReset.addEventListener("click", () => elements.clearFilters.click());
  elements.closePlayer.addEventListener("click", closePlayer);
  elements.liveStart.addEventListener("click", startLivePreview);
  elements.liveStop.addEventListener("click", () => stopLivePreview("Прямой просмотр остановлен."));
  elements.liveSourceSelect.addEventListener("change", () => {
    if (liveRequestActive) stopLivePreview("Источник изменён. Нажмите «Открыть эфир».");
  });
  elements.liveImage.addEventListener("load", markLivePreviewConnected);
  elements.liveImage.addEventListener("error", markLivePreviewFailed);
  elements.videoPlayer.addEventListener("error", () => {
    if (currentName) elements.playerError.hidden = false;
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopAutoRefresh(true);
      if (liveRequestActive) stopLivePreview("Просмотр остановлен, вкладка скрыта.");
    } else {
      refreshAll();
      startAutoRefresh();
    }
  });
  window.addEventListener("pagehide", () => {
    pageIsLeaving = true;
    stopAutoRefresh(true);
    stopLivePreview();
  });
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const editing = target instanceof HTMLElement && (
      target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)
    );
    if (event.key === "/" && !editing && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      elements.searchInput.focus();
    }
  });

  refreshAll();
  startAutoRefresh();
})();
