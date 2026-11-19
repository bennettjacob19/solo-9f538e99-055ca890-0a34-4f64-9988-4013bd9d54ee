// Confirmation dialogs for destructive actions. Names are read from data
// attributes (already HTML-escaped by the template), never interpolated into
// inline JavaScript.
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) {
    return;
  }
  var name = form.dataset.name || "";
  if (!window.confirm(form.dataset.confirm.replace("{name}", name))) {
    event.preventDefault();
  }
});

// Receipt lookup by request id: navigate to /receipts/<id> using a path prefix
// declared in the form's data attributes, so no values are baked into inline JS.
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (!(form instanceof HTMLFormElement) || form.id !== "rid-jump") {
    return;
  }
  event.preventDefault();
  var input = form.querySelector("input[name='rid']");
  var rid = input ? input.value.trim() : "";
  if (!rid) {
    return;
  }
  window.location.pathname = (form.dataset.jumpPrefix || "/receipts/") + encodeURIComponent(rid);
});
