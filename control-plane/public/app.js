const tabs = document.querySelectorAll("[data-tab]");
const panels = document.querySelectorAll("[data-panel]");
const immutableImagePattern = /^(?:[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?\/)?[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[a-f0-9]{64}$/;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

for (const tab of tabs) {
  tab.addEventListener("click", () => {
    for (const item of tabs) {
      const selected = item === tab;
      item.classList.toggle("active", selected);
      item.setAttribute("aria-selected", String(selected));
    }
    for (const panel of panels) panel.classList.toggle("active", panel.dataset.panel === tab.dataset.tab);
    const toolbar = document.querySelector(".code-toolbar span");
    toolbar.textContent = tab.dataset.tab === "agent" ? "AGENT PLAYBOOK" : "POST /api/v1/submissions";
    document.querySelector(".copy").dataset.copyTarget = document.querySelector(".code-panel.active").id || "";
  });
}

document.querySelector(".copy")?.addEventListener("click", async (event) => {
  const active = document.querySelector(".code-panel.active");
  await navigator.clipboard.writeText(active?.innerText || "");
  event.currentTarget.textContent = "Copied";
  setTimeout(() => { event.currentTarget.textContent = "Copy"; }, 1200);
});

function refreshArgvRows() {
  const rows = [...document.querySelectorAll(".argv-row")];
  rows.forEach((row, index) => {
    row.querySelector("input").setAttribute("aria-label", `Command argument ${index + 1}`);
    row.querySelector(".remove-argv").disabled = rows.length === 1;
  });
}

function addArgvRow(value = "") {
  const row = element("div", undefined, "argv-row");
  const input = document.createElement("input");
  input.type = "text";
  input.required = true;
  input.value = value;
  const remove = element("button", "×", "remove-argv");
  remove.type = "button";
  remove.setAttribute("aria-label", "Remove command argument");
  row.append(input, remove);
  document.querySelector("#argv-list").append(row);
  refreshArgvRows();
  input.focus();
}

document.querySelector("#add-argv")?.addEventListener("click", () => addArgvRow());
document.querySelector("#argv-list")?.addEventListener("click", (event) => {
  const remove = event.target.closest(".remove-argv");
  if (!remove || remove.disabled) return;
  remove.closest(".argv-row").remove();
  refreshArgvRows();
});

function resetIdempotencyKey() {
  document.querySelector("#quick-idempotency-key").value = `web-${crypto.randomUUID()}`;
}

resetIdempotencyKey();
refreshArgvRows();

document.querySelector("#quick-submit-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const image = data.get("image").trim();
  const apiKey = data.get("api_key");
  const argv = [...form.querySelectorAll(".argv-row input")].map((input) => input.value.trim());
  const status = document.querySelector("#quick-submit-status");
  const submit = form.querySelector("button[type=submit]");

  if (!immutableImagePattern.test(image)) {
    status.textContent = "Use a lowercase registry/repository@sha256 digest. Mutable tags cannot be queued.";
    status.className = "error";
    form.querySelector("[name=image]").focus();
    return;
  }
  if (argv.some((argument) => !argument)) {
    status.textContent = "Every command argument must contain a value.";
    status.className = "error";
    return;
  }

  status.textContent = "Queueing the immutable image…";
  status.className = "";
  submit.disabled = true;
  try {
    const response = await fetch("/api/v1/submissions", {
      method: "POST",
      headers: {
        authorization: `Bearer ${apiKey}`,
        "content-type": "application/json",
        "idempotency-key": data.get("idempotency_key"),
      },
      body: JSON.stringify({
        image,
        argv,
        name: data.get("name").trim(),
        model_version: data.get("model_version").trim(),
      }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "The submission could not be queued.");

    form.querySelector("[name=api_key]").value = "";
    status.textContent = `Queued ${body.id}. Opening its dedicated result page…`;
    status.className = "success";
    resetIdempotencyKey();
    location.assign(`/results/?submission=${encodeURIComponent(body.id)}`);
  } catch (error) {
    status.textContent = error.message;
    status.className = "error";
  } finally {
    submit.disabled = false;
  }
});

const legacySubmissionId = new URLSearchParams(location.hash.split("?")[1] || "").get("submission");
if (legacySubmissionId) {
  location.replace(`/results/?submission=${encodeURIComponent(legacySubmissionId)}`);
}
