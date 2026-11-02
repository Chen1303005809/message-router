const baseElement = document.querySelector("base");
const basePath = baseElement
  ? new URL(baseElement.href, window.location.href).pathname.replace(/\/$/, "")
  : "";

export function url(path) {
  if (/^https?:\/\//i.test(path)) return path;
  if (!path.startsWith("/")) path = `/${path}`;
  if (!basePath || path === basePath || path.startsWith(`${basePath}/`)) return path;
  return `${basePath}${path}`;
}

window.kefuUrl = url;

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

export function formatDate(value, options = {}) {
  if (!value) return "未填写";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return escapeHtml(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    ...options,
  }).format(date);
}

export function localDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date).reduce((result, part) => {
    result[part.type] = part.value;
    return result;
  }, {});
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  let body = options.body;
  if (body && !(body instanceof FormData) && typeof body === "object") {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  headers.set("Accept", "application/json");
  let response;
  try {
    response = await fetch(url(path), {
      ...options,
      body,
      headers,
      credentials: "same-origin",
    });
  } catch (error) {
    throw new Error(error?.message || "网络连接失败，请检查网络后重试。");
  }

  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.assign(`${url("/auth/login")}?next=${encodeURIComponent(next)}`);
    throw new Error("登录状态已失效，正在重新登录。");
  }
  if (!response.ok) {
    const message = typeof payload.detail === "string" && payload.detail.trim()
      ? payload.detail
      : `请求失败（${response.status}）`;
    if (response.status === 403) window.alert(message);
    throw new Error(message);
  }
  return payload;
}

export function navigate(path) {
  window.location.assign(url(path));
}

export function showFlash(message, { error = false, timeout = 5000 } = {}) {
  let container = document.querySelector(".flash-container");
  if (!container) {
    container = document.createElement("div");
    container.className = "flash-container";
    container.setAttribute("aria-live", "polite");
    document.body.append(container);
  }
  const notice = document.createElement("div");
  notice.className = "flash-notice";
  notice.setAttribute("role", error ? "alert" : "status");
  notice.textContent = message;
  container.append(notice);
  window.setTimeout(() => notice.remove(), timeout);
}

export function formValues(form) {
  const values = {};
  for (const element of form.elements) {
    if (!element.name || element.disabled) continue;
    if (element.type === "checkbox") {
      values[element.name] = element.checked && element.value !== "on"
        ? element.value
        : element.checked;
    } else if (element.type !== "file") {
      values[element.name] = element.value;
    }
  }
  return values;
}

export function setPage(title, bodyClass) {
  document.title = title;
  if (bodyClass) document.body.className = bodyClass;
}

export function bindImageUploader(root = document) {
  root.querySelectorAll('[data-image-uploader="true"]').forEach((uploader) => {
    if (uploader.dataset.bound === "true") return;
    uploader.dataset.bound = "true";
    const input = uploader.querySelector('[data-image-input="true"]');
    const list = uploader.querySelector('[data-image-list="true"]');
    const error = uploader.querySelector('[data-image-error="true"]');
    const form = input?.form;
    const dropZone = form?.querySelector('[data-image-drop-zone="true"]')
      || uploader.querySelector('[data-image-drop-zone="true"]');
    if (!input || !list || !error || !form) return;

    const maxBytes = Number(uploader.dataset.maxImageBytes || 20971520);
    let selected = Array.from(input.files || []);
    let canSynchronize = typeof DataTransfer !== "undefined";
    const keyFor = (file) => [file.name, file.size, file.lastModified, file.type].join(":");
    const sync = () => {
      if (!canSynchronize) return;
      try {
        const transfer = new DataTransfer();
        selected.forEach((file) => transfer.items.add(file));
        input.files = transfer.files;
      } catch (_) {
        canSynchronize = false;
      }
    };
    const render = () => {
      list.replaceChildren();
      selected.forEach((file, index) => {
        const item = document.createElement("li");
        item.className = "image-upload-item";
        const name = document.createElement("span");
        name.textContent = file.name || `图片 ${index + 1}`;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "image-upload-remove";
        remove.textContent = "移除";
        remove.addEventListener("click", () => {
          selected.splice(index, 1);
          sync();
          render();
        });
        item.append(name, remove);
        list.append(item);
      });
    };
    const add = (files) => {
      const existing = new Set(selected.map(keyFor));
      const rejected = [];
      for (const file of files) {
        if (!file || !file.type.startsWith("image/")) {
          rejected.push(`${file?.name || "文件"} 不是图片`);
        } else if (file.size > maxBytes) {
          rejected.push(`${file.name || "图片"} 超过 20 MiB`);
        } else if (!existing.has(keyFor(file))) {
          existing.add(keyFor(file));
          selected.push(file);
        }
      }
      sync();
      render();
      error.textContent = rejected.join("；");
      error.hidden = !rejected.length;
    };
    input.addEventListener("change", () => {
      const picked = Array.from(input.files || []);
      if (!picked.length) return;
      if (canSynchronize) input.value = "";
      add(picked);
    });
    if (dropZone) {
      ["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => {
        event.preventDefault();
        dropZone.classList.add("drag-active");
      }));
      ["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => {
        event.preventDefault();
        dropZone.classList.remove("drag-active");
      }));
      dropZone.addEventListener("drop", (event) => add(Array.from(event.dataTransfer?.files || [])));
    }
    form.addEventListener("paste", (event) => {
      const files = Array.from(event.clipboardData?.items || [])
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter(Boolean);
      if (files.length) {
        event.preventDefault();
        add(files);
      }
    });
    form.addEventListener("submit", sync);
  });
}
