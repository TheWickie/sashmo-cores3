/* Sashmo Command Centre. All personal values enter the page through textContent. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const sections = {};
  let versions = {};
  let retryMs = 8000;
  let timer = null;
  let firstNotifications = true;
  let knownNotifications = new Set();
  let toastTimer = null;
  const priorities = {low: 0, normal: 1, high: 2, critical: 3};

  function element(tag, className = "", value = null) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== null && value !== undefined) node.textContent = String(value);
    return node;
  }
  function empty(parent, message) { parent.replaceChildren(element("p", "empty-state", message)); }
  function note(parent, message, warning = false) { parent.append(element("p", warning ? "warning-note" : "section-note", message)); }
  function shortDate(value) {
    if (!value) return "Not reported";
    const date = new Date(value);
    return isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, {day:"numeric", month:"short", hour:"numeric", minute:"2-digit"}).format(date);
  }
  function timeAgo(value) {
    if (!value) return "Not yet";
    const elapsed = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
    if (!Number.isFinite(elapsed)) return "Not reported";
    if (elapsed < 60) return `${elapsed}s ago`;
    if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m ago`;
    return shortDate(value);
  }
  function duration(minutes) {
    if (!Number.isInteger(minutes) || minutes <= 0) return "Reported window";
    if (minutes % 10080 === 0) return `${minutes / 10080} week`;
    if (minutes % 1440 === 0) return `${minutes / 1440} day`;
    if (minutes % 60 === 0) return `${minutes / 60} hour`;
    return `${minutes} min`;
  }
  function line(parent, title, detail) {
    const row = element("li");
    row.append(element("span", "item-title", title), element("small", "", detail || ""));
    parent.append(row);
  }
  function stateLabel(value) {
    return ({in_progress:"In progress", completed:"Completed", error:"Error",
      interrupted:"Interrupted", unknown:"Status unavailable"})[value] || "Status unavailable";
  }
  function card(parent, className, title, subtitle) {
    const node = element("div", className);
    node.append(element("span", "item-title", title));
    if (subtitle) node.append(element("p", "", subtitle));
    parent.append(node);
    return node;
  }
  function setConnection(online, label) {
    $("connection-pill").classList.toggle("offline", !online);
    $("connection-text").textContent = label;
  }
  function showToast(message) {
    const toast = $("toast");
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {toast.hidden = true;}, 6500);
  }
  function applyNightMode() {
    const prefs = sections.notifications?.preferences;
    if (!prefs?.night_dim) {document.body.classList.remove("night-dim"); return;}
    const now = new Date();
    const value = now.getHours() * 60 + now.getMinutes();
    const [sh, sm] = (prefs.night_start || "22:00").split(":").map(Number);
    const [eh, em] = (prefs.night_end || "07:00").split(":").map(Number);
    const start = sh * 60 + sm, end = eh * 60 + em;
    document.body.classList.toggle("night-dim", start < end ? value >= start && value < end : value >= start || value < end);
  }

  function renderCodex(data) {
    const root = $("quota-content"), sessionsRoot = $("codex-sessions");
    root.replaceChildren(); sessionsRoot.replaceChildren();
    $("codex-tag").textContent = data.connected ? "Connected" : "Unavailable";
    $("codex-tag").classList.toggle("warn", !data.connected);
    if (data.error) note(root, data.error, true);
    const buckets = Array.isArray(data.buckets) ? data.buckets : [];
    const windows = buckets.flatMap(bucket => (bucket.windows || []).map(window => ({...window, bucket: bucket.name || bucket.id || "Codex"})));
    if (!windows.length) {
      note(root, "No usage window is currently reported by Codex. Figures are never estimated.");
    } else {
      const hero = element("div", "quota-hero");
      const primary = windows.find(window => String(window.bucket).toLowerCase().includes("codex")) || windows[0];
      const value = element("div", "quota-primary", Number.isFinite(primary.remaining_percent) ? `${primary.remaining_percent}%` : "—");
      value.append(element("small", "", `${primary.bucket} remaining`));
      hero.append(value);
      const cards = element("div", "quota-windows");
      windows.forEach(window => {
        const box = element("div", "window-card");
        const head = element("div", "window-head");
        head.append(element("span", "", `${window.bucket} · ${duration(window.duration_mins)}`),
                    element("strong", "", Number.isFinite(window.remaining_percent) ? `${window.remaining_percent}%` : "—"));
        box.append(head);
        const meter = element("div", "meter");
        const fill = element("span");
        fill.style.width = `${Math.max(0, Math.min(100, Number(window.remaining_percent) || 0))}%`;
        meter.append(fill); box.append(meter);
        box.append(element("p", "", `Resets ${shortDate(window.resets_at_local)}`));
        cards.append(box);
      });
      hero.append(cards); root.append(hero);
    }
    const credits = data.reset_credits_available;
    note(root, `Reported plan: ${data.plan_reported || "unavailable"} · Reset credits: ${Number.isInteger(credits) ? credits : "not reported"} · Updated ${timeAgo(data.last_updated)}`);
    sessionsRoot.append(element("p", "small-label", "Sessions & agent activity"));
    if (data.sessions_error) note(sessionsRoot, `Session activity unavailable: ${data.sessions_error}`);
    else if (!data.sessions?.length) note(sessionsRoot, "No recent session state reported.");
    else {
      const list = element("ul", "compact-list");
      data.sessions.slice(0, 5).forEach(session => line(list, session.label || "Codex task", stateLabel(session.state)));
      sessionsRoot.append(list);
    }
    sessionsRoot.append(element("p", "small-label", "Recent completed work"));
    if (data.sessions_error) note(sessionsRoot, "Completed task status is unavailable.");
    else if (!data.completed_sessions?.length) note(sessionsRoot, "No recently completed task is reported.");
    else {
      const list = element("ul", "compact-list");
      data.completed_sessions.slice(0, 4).forEach(session => line(list, session.label || "Codex task", "Completed"));
      sessionsRoot.append(list);
    }
  }

  function renderChatGPT(data) {
    const root = $("chat-content"); root.replaceChildren();
    const available = !data.reminders_error;
    $("chat-tag").textContent = available ? "Scheduled connected" : "Unavailable";
    $("chat-tag").classList.toggle("warn", !available);
    if (!available) note(root, `Scheduled tasks unavailable: ${data.reminders_error || "not connected"}`, true);
    else if (!data.reminders?.length) note(root, "No active scheduled tasks returned by the connected reminder chat.");
    else data.reminders.forEach(task => card(root, "event-card", task.title || "Scheduled task", task.schedule || "Schedule not provided"));
    note(root, data.notifications_note);
    note(root, data.account_note);
    note(root, `Last scheduled-task check: ${timeAgo(data.checked_at)}`);
  }

  function renderDevelopment(data) {
    const root = $("development-content"); root.replaceChildren();
    const projects = data.named_projects || [];
    if (!projects.length) note(root, "Featured projects have not been selected on the Windows PC.");
    projects.forEach(entry => {
      const project = entry.status;
      const message = !project ? "Not registered for Sashmo's read-only project status." :
        project.state !== "ok" ? `Status unavailable: ${project.state}` :
        `${project.branch || "Branch unavailable"} · ${project.dirty ? "Uncommitted changes" : "Clean working tree"}`;
      card(root, "project-card", entry.name, message);
    });
    root.append(element("p", "small-label", "Active development"));
    if (!data.active_sessions?.length) note(root, "No active Codex session is currently reported.");
    else {
      const list = element("ul", "compact-list");
      data.active_sessions.slice(0, 5).forEach(item => line(list, item.label || "Codex task", stateLabel(item.state)));
      root.append(list);
    }
    root.append(element("p", "small-label", "Recent important work"));
    if (!data.recent_work?.length) note(root, "No recent Codex work available.");
    else {
      const list = element("ul", "compact-list");
      data.recent_work.slice(0, 5).forEach(item => line(list, item.title || "Untitled task", item.source || "Codex"));
      root.append(list);
    }
  }

  function renderCommunications(data) {
    const root = $("communications-content"); root.replaceChildren();
    const columns = element("div", "split-list");
    const calendar = element("div"), mail = element("div");
    calendar.append(element("p", "small-label", "Upcoming calendar"));
    if (!data.calendar?.connected) note(calendar, `Calendar unavailable: ${data.calendar?.error || "not connected"}`, true);
    else if (!data.calendar.events?.length) note(calendar, "No event in the connected 48-hour window.");
    else data.calendar.events.forEach(event => card(calendar, "event-card", event.title || "Event", `${shortDate(event.start)}${event.all_day ? " · all day" : ""}`));
    mail.append(element("p", "small-label", "Unread email"));
    if (!data.gmail?.connected) note(mail, `Gmail unavailable: ${data.gmail?.error || "not connected"}`, true);
    else if (!data.gmail.messages?.length) note(mail, "No unread message in the latest metadata sample.");
    else data.gmail.messages.forEach(message => card(mail, "message-card", message.subject || "(No subject)", `${message.from || "Unknown sender"} · ${message.date || "Date unavailable"}`));
    if (data.gmail?.connected) note(mail, `${data.gmail.unread_count ?? "—"} unread reported. Message bodies are never loaded.`);
    columns.append(calendar, mail); root.append(columns);
  }

  function renderSystem(data) {
    const root = $("system-content"); root.replaceChildren();
    const metrics = element("div", "system-grid");
    const device = data.device || {};
    const entries = [
      ["Backend", "Connected"],
      ["CoreS3 last seen", timeAgo(data.device_last_seen)],
      ["Battery", Number.isInteger(device.battery_percent) ? `${device.battery_percent}%${device.charging ? " · charging" : ""}` : "Not reported"],
      ["Power", device.usb_powered === true ? "USB" : device.usb_powered === false ? "Battery" : "Not reported"],
      ["Wi-Fi signal", Number.isInteger(device.wifi_rssi) ? `${device.wifi_rssi} dBm` : "Not reported"],
      ["Voice", data.voice_state || "Unavailable"],
    ];
    entries.forEach(([label, value]) => {const box = element("div", "metric");box.append(element("small", "", label), element("strong", "", value));metrics.append(box);});
    root.append(metrics);
    note(root, "Safari: Share → Add to Home Screen for a standalone dashboard. Background push needs HTTPS and a separate opt-in setup.");
  }

  function renderNotifications(data) {
    const root = $("updates-list"); root.replaceChildren();
    const items = data.items || [];
    const unread = items.filter(item => !item.read).length;
    $("updates-count").textContent = `${unread} unread`;
    $("unread-badge").textContent = unread;
    $("unread-badge").hidden = !unread;
    if (!items.length) {empty(root, "No updates have been recorded yet. Disconnected sources appear here as connection notices.");return;}
    const prefs = data.preferences || {};
    const fresh = items.filter(item => !knownNotifications.has(item.id));
    if (!firstNotifications && prefs.live_alerts) {
      const important = fresh.find(item => !item.read && priorities[item.priority] >= priorities[prefs.minimum_priority || "normal"]);
      if (important) showToast(`${important.source}: ${important.title}`);
    }
    firstNotifications = false;
    knownNotifications = new Set(items.map(item => item.id));
    items.forEach(item => {
      const row = element("article", `update-item ${item.priority || "normal"}${item.read ? "" : " unread"}`);
      const top = element("div", "update-top");
      top.append(element("span", "update-source", `${item.source || "UPDATE"} · ${(item.priority || "normal").toUpperCase()}`));
      const date = element("time", "", shortDate(item.timestamp));date.dateTime = item.timestamp || "";top.append(date);
      row.append(top, element("h3", "", item.title || "Update"), element("p", "", item.summary || ""));
      const button = element("button", "", item.read ? "Mark unread" : "Mark read");
      button.type = "button";
      button.addEventListener("click", () => markRead(item.id, !item.read));
      row.append(button);root.append(row);
    });
    applyNightMode();
  }

  async function markRead(id, read) {
    try {
      const response = await fetch(`/ipad/api/notifications/${encodeURIComponent(id)}/read`, {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json"}, body:JSON.stringify({read})});
      if (!response.ok) throw new Error("Could not update notification");
      versions.notifications = "";
      schedule(0);
    } catch (error) {showToast(error.message);}
  }
  function applySections(payload) {
    Object.assign(versions, payload.versions || {});
    Object.assign(sections, payload.sections || {});
    const renderers = {codex:renderCodex, chatgpt:renderChatGPT, development:renderDevelopment,
      communications:renderCommunications, system:renderSystem, notifications:renderNotifications};
    Object.entries(payload.sections || {}).forEach(([key, value]) => renderers[key]?.(value));
  }
  function schedule(delay) {clearTimeout(timer);timer = setTimeout(poll, delay);}
  async function poll() {
    if (document.hidden) {schedule(20000);return;}
    const query = new URLSearchParams();
    Object.entries(versions).forEach(([key, value]) => query.set(`v_${key}`, value));
    try {
      const response = await fetch(`/ipad/api/updates?${query}`, {credentials:"same-origin", cache:"no-store"});
      if (response.status === 401) {showPairing();return;}
      if (response.status !== 204) {
        if (!response.ok) throw new Error(`Service returned ${response.status}`);
        applySections(await response.json());
      }
      setConnection(true, "Live");
      $("last-sync").textContent = `Checked ${new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"})}`;
      retryMs = 8000;
      schedule(8000);
    } catch (_error) {
      setConnection(false, "Reconnecting");
      $("last-sync").textContent = "Connection lost · retrying";
      schedule(retryMs);
      retryMs = Math.min(60000, Math.round(retryMs * 1.7));
    }
  }
  function showPairing() {
    setConnection(false, "Pairing needed");
    if (!$("pair-dialog").open) $("pair-dialog").showModal();
  }
  async function bootstrap() {
    try {
      const response = await fetch("/ipad/api/bootstrap", {credentials:"same-origin",cache:"no-store"});
      if (!response.ok) throw new Error("Unavailable");
      const data = await response.json();
      if (data.paired) schedule(0); else showPairing();
    } catch (_error) {setConnection(false, "Reconnecting");schedule(5000);}
  }
  $("pair-form").addEventListener("submit", async event => {
    event.preventDefault();
    $("pair-error").textContent = "";
    try {
      const response = await fetch("/ipad/api/pair", {method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:$("pair-code").value.trim()})});
      if (!response.ok) throw new Error(response.status === 429 ? "Code incorrect or expired. Check the Windows dashboard." : "Could not pair this iPad.");
      $("pair-code").value = "";
      $("pair-dialog").close();
      versions = {};
      schedule(0);
    } catch (error) {$("pair-error").textContent = error.message;}
  });
  $("settings-button").addEventListener("click", () => {
    const prefs = sections.notifications?.preferences || {};
    $("live-alerts").checked = !!prefs.live_alerts;
    $("minimum-priority").value = prefs.minimum_priority || "normal";
    $("night-dim").checked = !!prefs.night_dim;
    $("night-start").value = prefs.night_start || "22:00";
    $("night-end").value = prefs.night_end || "07:00";
    $("settings-message").textContent = "";
    $("settings-dialog").showModal();
  });
  $("close-settings").addEventListener("click", () => $("settings-dialog").close());
  $("settings-form").addEventListener("submit", async event => {
    event.preventDefault();
    const body = {live_alerts:$("live-alerts").checked, minimum_priority:$("minimum-priority").value,
      night_dim:$("night-dim").checked, night_start:$("night-start").value, night_end:$("night-end").value};
    try {
      const response = await fetch("/ipad/api/preferences", {method:"POST", credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      if (!response.ok) throw new Error("Could not save preferences");
      $("settings-message").textContent = "Saved";
      versions.notifications = "";
      schedule(0);
      setTimeout(() => $("settings-dialog").close(), 500);
    } catch (error) {$("settings-message").textContent = error.message;}
  });
  function tickClock() {
    $("clock").textContent = new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
    applyNightMode();
  }
  window.addEventListener("online", () => schedule(0));
  window.addEventListener("offline", () => setConnection(false, "Offline"));
  document.addEventListener("visibilitychange", () => {if (!document.hidden) {versions = {};schedule(0);}});
  tickClock();setInterval(tickClock,30000);bootstrap();
  if (window.isSecureContext && "serviceWorker" in navigator) {
    navigator.serviceWorker.register("/ipad/sw.js", {scope:"/ipad/"}).catch(() => {});
  }
})();
