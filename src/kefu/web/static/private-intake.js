import { api, navigate, url } from "./client.js";

const key = "kefu_private_intake_token";
const path = window.location.pathname;
const basePath = new URL(document.querySelector("base").href).pathname.replace(/\/$/, "");
const localPath = path.startsWith(basePath) ? path.slice(basePath.length) : path;
const status = document.getElementById("private-intake-status");

if (localPath === "/issues/private-intake") {
  const token = window.location.hash.slice(1);
  if (token) sessionStorage.setItem(key, token);
  else sessionStorage.removeItem(key);
  history.replaceState(null, "", window.location.pathname + window.location.search);
  navigate("/issues/private-intake/continue");
} else {
  const token = sessionStorage.getItem(key);
  if (!token) {
    navigate("/issues/new");
  } else {
    try {
      await api("/api/issues/private-intake/open", {
        method: "POST",
        body: { token },
      });
      navigate("/issues/new?private_intake=1");
    } catch (_) {
      if (status) status.textContent = "交接链接已过期或账号不匹配，请重新从机器人私聊打开卡片。";
    }
  }
}
