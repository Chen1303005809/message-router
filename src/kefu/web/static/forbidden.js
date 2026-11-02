const message = document.getElementById("forbidden-message");
const body = document.body;
const detail = message?.dataset.message || "当前身份无权执行此操作。";
const shouldReturn = body.dataset.shouldReturn === "true";
const target = body.dataset.target || body.dataset.issueList || "/issues";
window.alert(detail);

if (shouldReturn) {
  window.location.replace(target);
} else {
  const actions = document.getElementById("forbidden-actions");
  const back = document.createElement("button");
  back.type = "button";
  back.textContent = "返回上一页";
  back.addEventListener("click", () => window.history.back());
  const list = document.createElement("a");
  list.href = body.dataset.issueList || "/issues";
  list.textContent = "打开问题列表";
  actions?.append(back, list);
}
