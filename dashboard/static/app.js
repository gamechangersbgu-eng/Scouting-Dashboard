"use strict";

// Marker radii in pixels. The earliest club gets the largest pin, so size reads as
// "how far back in the career this was".
const PIN_MAX = 30;
const PIN_MIN = 14;
const PIN_SINGLE = 22;

const el = (id) => document.getElementById(id);
const num = (value) => (value === null || value === undefined ? "—" : Number(value).toLocaleString("he-IL"));

// One shared membership set drives both result-row and detail-header stars.
// It is loaded in one bounded request, never once per displayed player.
const state = {
  players: [], selected: null, map: null, layer: null, primed: false, movements: null,
  shortlists: [], favoriteIds: new Set(), activeShortlist: null,
};

function addLogoutButton() {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "logout";
  button.textContent = "התנתקות";
  button.addEventListener("click", async () => {
    const token = await getJSON("/api/auth/csrf");
    const response = await fetch("/logout", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ csrf_token: token.csrf_token }),
    });
    if (response.redirected) window.location.assign(response.url);
  });
  document.querySelector(".brand").append(button);
}

function addLocationFilters() {
  const filters = document.querySelector(".filters");
  const location = document.createElement("label");
  location.className = "field select-field";
  location.innerHTML =
    '<span class="sr-only">\u05d0\u05d6\u05d5\u05e8 \u05dc\u05d7\u05d9\u05e4\u05d5\u05e9</span>' +
    '<select id="location"><option value="">\u05db\u05dc \u05d4\u05d0\u05e8\u05e5</option></select>';

  const radius = document.createElement("label");
  radius.className = "field select-field";
  radius.innerHTML =
    '<span class="sr-only">\u05e8\u05d3\u05d9\u05d5\u05e1 \u05d7\u05d9\u05e4\u05d5\u05e9</span>' +
    '<select id="radius-km">' +
    '<option value="10">\u05e2\u05d3 10 \u05e7\"\u05de</option>' +
    '<option value="25">\u05e2\u05d3 25 \u05e7\"\u05de</option>' +
    '<option value="50" selected>\u05e2\u05d3 50 \u05e7\"\u05de</option>' +
    '<option value="100">\u05e2\u05d3 100 \u05e7\"\u05de</option>' +
    '</select>';

  const hint = document.createElement("p");
  hint.className = "filter-hint";
  hint.textContent =
    "\u05d0\u05d6\u05d5\u05e8 \u05d4\u05d7\u05d9\u05e4\u05d5\u05e9 \u05e0\u05de\u05d3\u05d3 \u05de\u05de\u05d2\u05e8\u05e9 \u05d4\u05d1\u05d9\u05ea \u05e9\u05dc \u05d4\u05e7\u05d1\u05d5\u05e6\u05d4 \u05d4\u05e0\u05d5\u05db\u05d7\u05d9\u05ea.";
  filters.append(location, radius);
  filters.after(hint);
}

addLocationFilters();
addLogoutButton();

/* ------------------------------ data loading ------------------------------ */

async function getJSON(url) {
  const response = await fetch(url);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${url} -> ${response.status}`);
  return payload;
}

async function csrfToken() {
  return (await getJSON("/api/auth/csrf")).csrf_token;
}

async function apiJSON(url, method, body) {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": await csrfToken() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${url} -> ${response.status}`);
  return payload;
}

async function loadShortlistState(playerId) {
  const suffix = playerId === undefined ? "" : `?player_id=${encodeURIComponent(playerId)}`;
  const payload = await getJSON(`/api/shortlists${suffix}`);
  state.shortlists = payload.shortlists || [];
  state.favoriteIds = new Set(payload.favorite_player_ids || []);
  return payload;
}

function showShortlistError(error) {
  const message = error instanceof Error ? error.message : String(error);
  // Errors cannot leave an optimistic star silently wrong. The visible alert is
  // intentionally simple and works without introducing another notification framework.
  window.alert(`לא ניתן לעדכן רשימה: ${message}`);
}

function isFavorite(playerId) {
  return state.favoriteIds.has(String(playerId));
}

function updateFavoriteControl(button, playerId) {
  const favorite = isFavorite(playerId);
  button.textContent = favorite ? "★" : "☆";
  button.classList.toggle("is-favorite", favorite);
  button.setAttribute("aria-label", favorite ? "הסרה ממועדפים" : "הוספה למועדפים");
  button.title = button.getAttribute("aria-label");
}

async function toggleFavorite(playerId) {
  playerId = String(playerId);
  const wasFavorite = isFavorite(playerId);
  if (wasFavorite) state.favoriteIds.delete(playerId); else state.favoriteIds.add(playerId);
  renderResults();
  const headerFavorite = el("header-favorite");
  if (state.selected === playerId && headerFavorite) updateFavoriteControl(headerFavorite, playerId);
  try {
    await apiJSON(`/api/favorites/${encodeURIComponent(playerId)}`, wasFavorite ? "DELETE" : "PUT");
  } catch (error) {
    if (wasFavorite) state.favoriteIds.add(playerId); else state.favoriteIds.delete(playerId);
    renderResults();
    if (state.selected === playerId && headerFavorite) updateFavoriteControl(headerFavorite, playerId);
    showShortlistError(error);
  }
}

async function loadSummary() {
  const summary = await getJSON("/api/summary");
  const historyText = summary.history_seasons?.length
    ? ` · היסטוריה ${summary.history_seasons.at(-1)}–${summary.history_seasons[0]}`
    : "";
  el("dataset").textContent =
    `${num(summary.players)} שחקנים · ${num(summary.teams_located)}/${num(summary.teams)} קבוצות מאותרות · ` +
    `${num(summary.above_age)} משחקים מעל הגיל · סטטיסטיקה ${summary.seasons.join(", ")}${historyText}`;

  fillSelect(el("birth-year"), summary.birth_years, (year) => `נולדו ${year}`);
  fillSelect(el("current-team"), summary.current_teams);
  fillLocations(summary.locations || []);
}

function fillSelect(select, values, label = (value) => value) {
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label(value);
    select.append(option);
  }
}

function fillLocations(locations) {
  const select = el("location");
  for (const location of locations) {
    const option = document.createElement("option");
    option.value = location.city;
    option.textContent = location.city;
    select.append(option);
  }
}

async function loadResults() {
  const params = new URLSearchParams({ q: el("query").value.trim(), limit: "60" });
  if (el("above-age").checked) params.set("above_age", "1");
  if (el("birth-year").value) params.set("birth_year", el("birth-year").value);
  if (el("current-team").value) params.set("current_team", el("current-team").value);
  if (el("location").value) {
    params.set("location", el("location").value);
    params.set("radius_km", el("radius-km").value);
  }

  state.players = await getJSON(`/api/players?${params}`);
  renderResults();

  // Open on a real player rather than an empty prompt. Only on the first load, so a
  // later search never yanks the panel away from whoever is being looked at.
  if (!state.primed && state.players.length) {
    state.primed = true;
    await selectPlayer(state.players[0].player_id);
  }
}

/* ------------------------------ results list ------------------------------ */

function renderResults() {
  const list = el("results");
  list.replaceChildren();

  const query = el("query").value.trim();
  el("results-meta").textContent = state.players.length
    ? `${state.players.length} תוצאות${query ? "" : " · המובילים בשערים"}`
    : "";

  if (!state.players.length) {
    const note = document.createElement("li");
    note.className = "empty-note";
    note.textContent = "לא נמצאו שחקנים מתאימים";
    list.append(note);
    return;
  }

  for (const player of state.players) {
    const item = document.createElement("li");
    item.dataset.playerId = player.player_id;
    if (player.player_id === state.selected) item.classList.add("active");

    const name = document.createElement("div");
    name.className = "result-name";
    name.append(document.createTextNode(player.player_name));
    if (player.plays_above_age) {
      const chip = document.createElement("span");
      chip.className = "up-chip";
      chip.textContent = `+${player.age_groups_above} מעל הגיל`;
      name.append(chip);
    }
    if (player.former_hapoel_player) {
      const chip = document.createElement("span");
      chip.className = "ex-chip";
      chip.textContent = 'EX הפועל ב"ש';
      name.append(chip);
    }

    const sub = document.createElement("div");
    sub.className = "result-sub";
    const born = player.birth_year ? `נולד ${player.birth_year}` : "שנת לידה לא ידועה";
    sub.textContent = `${born} · ${player.current_team || "—"} · ${player.goals_total} שערים`;

    const actions = document.createElement("div");
    actions.className = "result-shortlist-actions";
    const favorite = document.createElement("button");
    favorite.type = "button";
    favorite.className = "favorite-button";
    updateFavoriteControl(favorite, player.player_id);
    favorite.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleFavorite(player.player_id);
    });
    const addToList = document.createElement("button");
    addToList.type = "button";
    addToList.className = "shortlist-button";
    addToList.textContent = "הוסף לרשימה";
    addToList.addEventListener("click", (event) => {
      event.stopPropagation();
      openMembershipMenu(player.player_id, addToList);
    });
    actions.append(favorite, addToList);
    const top = document.createElement("div");
    top.className = "result-top";
    top.append(name, actions);

    item.append(top, sub);
    item.addEventListener("click", () => selectPlayer(player.player_id));
    list.append(item);
  }
}

/* ------------------------------ player view ------------------------------ */

async function selectPlayer(playerId) {
  state.selected = String(playerId);
  el("movements").hidden = true;
  el("movements-nav").classList.remove("active");
  el("shortlists").hidden = true;
  el("shortlists-nav").classList.remove("active");
  renderResults();
  const player = await getJSON(`/api/player/${playerId}`);
  el("player").hidden = false;
  renderHeader(player);
  renderTiles(player);
  renderSeasons(player);
  renderMap(player);
}

/* --------------------------- player movements --------------------------- */

function movementTeams(teams) {
  return teams?.map((team) => team.team_name).filter(Boolean).join(" / ") || "—";
}

function movementStatus(status) {
  // The source has season-level registrations, so this is deliberately a
  // warning rather than a guessed transfer direction when clubs overlap.
  if (status === "same_season_ambiguous") return "מספר קבוצות באותה עונה — סדר המעבר אינו ידוע";
  if (status === "no_previous_history") return "לא ידוע מהנתונים";
  return "ידוע מהנתונים";
}

function movementCell(row, text, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = text ?? "—";
  row.append(cell);
}

function movementPlayerCell(row, player) {
  const cell = document.createElement("td");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "movement-player";
  button.textContent = player.player_name;
  button.title = `פתיחת כרטיס שחקן #${player.player_id}`;
  button.addEventListener("click", () => selectPlayer(player.player_id));
  cell.append(button);
  row.append(cell);
}

function renderMovementRows(body, players, kind) {
  body.replaceChildren();
  for (const player of players) {
    const row = document.createElement("tr");
    movementPlayerCell(row, player);
    movementCell(row, player.birth_year ?? "—", "num");
    if (kind === "former") {
      movementCell(row, player.last_hapoel_season);
      movementCell(row, movementTeams(player.last_hapoel_teams));
      movementCell(row, movementTeams(player.current_clubs));
      movementCell(row, player.latest_season);
    } else {
      movementCell(row, movementTeams(player.current_hapoel_teams));
      movementCell(row, player.current_spell_start_season);
      movementCell(
        row,
        player.status === "same_season_ambiguous"
          ? "לא ניתן לקבוע מהנתונים"
          : player.status === "no_previous_history"
            ? "לא ידוע מהנתונים"
            : movementTeams(player.previous_clubs)
      );
      movementCell(row, player.previous_observed_season ?? "—");
    }
    movementCell(row, movementStatus(player.status), player.status === "same_season_ambiguous" ? "movement-warning" : "");
    body.append(row);
  }
}

function renderMovements(report) {
  const summary = report.summary;
  el("movement-summary").replaceChildren(
    movementSummaryChip(`${num(summary.former_players)} שחקנים שהיו בהפועל באר שבע`),
    movementSummaryChip(`${num(summary.current_players)} שחקנים בהפועל באר שבע כיום`),
    movementSummaryChip(`${num(summary.known_previous_club)} מקור קודם ידוע`),
    movementSummaryChip(`${num(summary.no_previous_history)} ללא היסטוריה קודמת`),
    movementSummaryChip(`${num(summary.ambiguous)} מקרים לא חד־משמעיים`)
  );
  renderMovementRows(el("former-body"), report.former_players, "former");
  renderMovementRows(el("current-body"), report.current_players, "current");
  el("former-section").hidden = !report.former_players.length;
  el("current-section").hidden = !report.current_players.length;
  const message = !report.former_players.length && !report.current_players.length
    ? "לא נמצאו מעברי שחקנים להפועל באר שבע בנתונים הזמינים."
    : "שימו לב: במקרה של מספר קבוצות באותה עונה, סדר המעבר אינו ידוע.";
  el("movement-state").textContent = message;
  el("movement-state").classList.toggle("movement-state-warning", report.summary.ambiguous > 0);
}

function movementSummaryChip(text) {
  const chip = document.createElement("span");
  chip.className = "movement-summary-chip";
  chip.textContent = text;
  return chip;
}

async function showMovements() {
  // The report is a true toggle.  Its DOM remains intact while hidden, so a
  // second click restores the exact selected player view without rebuilding
  // the report or fetching it again.
  if (!el("movements").hidden) {
    el("movements").hidden = true;
    el("movements-nav").classList.remove("active");
    if (state.selected) el("player").hidden = false;
    return;
  }
  el("shortlists").hidden = true;
  el("shortlists-nav").classList.remove("active");
  el("player").hidden = true;
  el("movements").hidden = false;
  el("movements-nav").classList.add("active");
  if (state.movements) return;
  el("movement-state").classList.remove("movement-state-error", "movement-state-warning");
  el("movement-state").textContent = "טוען נתוני מעברים…";
  try {
    state.movements = await getJSON("/api/player-movements");
    renderMovements(state.movements);
  } catch (error) {
    el("movement-state").textContent = `שגיאה בטעינת נתוני המעברים: ${error.message}`;
    el("movement-state").classList.add("movement-state-error");
  }
}

/* ---------------------------- personal shortlists ---------------------------- */

function closeMembershipMenu() {
  document.querySelector(".shortlist-popover")?.remove();
}

async function openMembershipMenu(playerId, anchor) {
  closeMembershipMenu();
  try {
    const payload = await loadShortlistState(playerId);
    const selectedIds = new Set(payload.member_shortlist_ids || []);
    const menu = document.createElement("div");
    menu.className = "shortlist-popover";
    menu.setAttribute("role", "dialog");
    const title = document.createElement("p");
    title.className = "shortlist-popover-title";
    title.textContent = "שמירה ברשימות";
    menu.append(title);
    for (const shortlist of state.shortlists) {
      const label = document.createElement("label");
      label.className = "shortlist-option";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = selectedIds.has(shortlist.id);
      const text = document.createElement("span");
      text.textContent = shortlist.name;
      checkbox.addEventListener("change", async () => {
        checkbox.disabled = true;
        const previous = !checkbox.checked;
        try {
          if (shortlist.is_default) {
            await apiJSON(`/api/favorites/${encodeURIComponent(playerId)}`, checkbox.checked ? "PUT" : "DELETE");
            if (checkbox.checked) state.favoriteIds.add(String(playerId)); else state.favoriteIds.delete(String(playerId));
            renderResults();
            if (state.selected === String(playerId)) updateFavoriteControl(el("header-favorite"), playerId);
          } else {
            const url = `/api/shortlists/${shortlist.id}/players/${encodeURIComponent(playerId)}`;
            await apiJSON(url, checkbox.checked ? "POST" : "DELETE");
          }
        } catch (error) {
          checkbox.checked = previous;
          showShortlistError(error);
        } finally {
          checkbox.disabled = false;
        }
      });
      label.append(checkbox, text);
      menu.append(label);
    }
    document.body.append(menu);
    const box = anchor.getBoundingClientRect();
    menu.style.top = `${Math.min(window.innerHeight - menu.offsetHeight - 8, box.bottom + 5)}px`;
    menu.style.left = `${Math.max(8, Math.min(window.innerWidth - menu.offsetWidth - 8, box.left))}px`;
  } catch (error) {
    showShortlistError(error);
  }
}

function setShortlistsState(message = "", error = false) {
  el("shortlists-state").textContent = message;
  el("shortlists-state").classList.toggle("error", error);
}

function renderShortlistCards() {
  const cards = el("shortlist-cards");
  cards.replaceChildren();
  el("shortlist-players").hidden = true;
  el("shortlists-back").hidden = true;
  el("shortlists-title").textContent = "הרשימות שלי";
  el("shortlists-description").textContent = "רשימות אישיות נשמרות לחשבון שלך גם לאחר רענון נתוני הסקאוטינג.";
  for (const shortlist of state.shortlists) {
    const card = document.createElement("section");
    card.className = "shortlist-card";
    const heading = document.createElement("h3");
    heading.textContent = shortlist.is_default ? `★ ${shortlist.name}` : shortlist.name;
    const count = document.createElement("p");
    count.className = "shortlist-card-count";
    count.textContent = `${shortlist.player_count} שחקנים`;
    const actions = document.createElement("div");
    actions.className = "shortlist-card-actions";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "shortlist-card-open";
    open.textContent = "פתיחה";
    open.addEventListener("click", () => openShortlist(shortlist.id));
    actions.append(open);
    if (!shortlist.is_default) {
      const rename = document.createElement("button");
      rename.type = "button";
      rename.textContent = "שינוי שם";
      rename.addEventListener("click", () => renameShortlist(shortlist));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "מחיקה";
      remove.addEventListener("click", () => deleteShortlist(shortlist));
      actions.append(rename, remove);
    }
    card.append(heading, count, actions);
    cards.append(card);
  }
  setShortlistsState(state.shortlists.length ? "" : "עדיין אין רשימות.");
}

async function showShortlists() {
  closeMembershipMenu();
  if (!el("shortlists").hidden && state.activeShortlist === null) {
    el("shortlists").hidden = true;
    el("shortlists-nav").classList.remove("active");
    if (state.selected) el("player").hidden = false;
    return;
  }
  state.activeShortlist = null;
  el("movements").hidden = true;
  el("movements-nav").classList.remove("active");
  el("player").hidden = true;
  el("shortlists").hidden = false;
  el("shortlists-nav").classList.add("active");
  setShortlistsState("טוען רשימות…");
  try {
    await loadShortlistState();
    renderShortlistCards();
  } catch (error) {
    setShortlistsState(`שגיאה בטעינת הרשימות: ${error.message}`, true);
  }
}

async function openShortlist(shortlistId) {
  state.activeShortlist = shortlistId;
  setShortlistsState("טוען שחקנים…");
  try {
    const payload = await getJSON(`/api/shortlists/${shortlistId}`);
    const shortlist = payload.shortlist;
    const players = el("shortlist-players");
    el("shortlist-cards").replaceChildren();
    players.replaceChildren();
    players.hidden = false;
    el("shortlists-back").hidden = false;
    el("shortlists-title").textContent = shortlist.is_default ? `★ ${shortlist.name}` : shortlist.name;
    el("shortlists-description").textContent = `${payload.players.length} שחקנים ברשימה`;
    setShortlistsState("");
    if (!payload.players.length) setShortlistsState("אין עדיין שחקנים ברשימה.");
    for (const player of payload.players) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "shortlist-player";
      const name = document.createElement("div");
      name.className = "shortlist-player-name";
      name.textContent = player.player_name;
      const meta = document.createElement("div");
      meta.className = "shortlist-player-meta";
      const birth = player.birth_year ? `נולד ${player.birth_year}` : "שנת לידה לא ידועה";
      const team = player.available_in_current_catalog ? (player.current_team || "—") : (player.last_known_team || "—");
      meta.textContent = `${birth} · ${team}`;
      button.append(name, meta);
      if (player.plays_above_age) {
        const badge = document.createElement("span");
        badge.className = "up-chip";
        badge.textContent = `+${player.age_groups_above} מעל הגיל`;
        button.append(badge);
      }
      if (player.former_hapoel_player) {
        const badge = document.createElement("span");
        badge.className = "ex-chip";
        badge.textContent = 'EX הפועל ב"ש';
        button.append(badge);
      }
      if (!player.available_in_current_catalog) {
        const missing = document.createElement("div");
        missing.className = "shortlist-missing";
        missing.textContent = "לא נמצא בקטלוג הנוכחי · מוצג מידע שנשמר בעת ההוספה";
        button.append(missing);
      } else {
        button.addEventListener("click", () => selectPlayer(player.player_id));
      }
      players.append(button);
    }
  } catch (error) {
    setShortlistsState(`שגיאה בטעינת הרשימה: ${error.message}`, true);
  }
}

async function createShortlist() {
  const name = window.prompt("שם הרשימה החדשה:");
  if (name === null) return;
  try {
    await apiJSON("/api/shortlists", "POST", { name });
    await loadShortlistState();
    renderShortlistCards();
  } catch (error) { showShortlistError(error); }
}

async function renameShortlist(shortlist) {
  const name = window.prompt("שם הרשימה:", shortlist.name);
  if (name === null) return;
  try {
    await apiJSON(`/api/shortlists/${shortlist.id}`, "PATCH", { name });
    await loadShortlistState();
    renderShortlistCards();
  } catch (error) { showShortlistError(error); }
}

async function deleteShortlist(shortlist) {
  if (!window.confirm(`למחוק את הרשימה „${shortlist.name}”? השחקנים עצמם לא יימחקו.`)) return;
  try {
    await apiJSON(`/api/shortlists/${shortlist.id}`, "DELETE");
    await loadShortlistState();
    renderShortlistCards();
  } catch (error) { showShortlistError(error); }
}

function renderHeader(player) {
  el("player-name").textContent = player.player_name;
  updateFavoriteControl(el("header-favorite"), player.player_id);

  const photo = el("player-photo");
  if (player.image_url) {
    photo.src = player.image_url;
    photo.alt = player.player_name;
    photo.hidden = false;
  } else {
    photo.hidden = true;
    photo.removeAttribute("src");
  }

  const born = player.birth_year ? `נולד ${player.birth_year}` : "שנת לידה לא ידועה";
  el("player-meta").textContent =
    `#${player.player_id} · ${born} · ${player.current_team || "—"} · ` +
    `${player.num_teams} קבוצות · עונות ${player.seasons_played}`;

  const badges = el("player-badges");
  badges.replaceChildren();

  const ageBadge = document.createElement("span");
  if (player.plays_above_age) {
    ageBadge.className = "badge badge-up";
    ageBadge.textContent = `משחק ${player.age_groups_above} קבוצות גיל מעל גילו`;
  } else {
    ageBadge.className = "badge badge-ok";
    ageBadge.textContent = "משחק בקבוצת הגיל שלו";
  }
  badges.append(ageBadge);

  if (player.former_hapoel_player) {
    const formerHapoelBadge = document.createElement("span");
    formerHapoelBadge.className = "badge badge-ex";
    formerHapoelBadge.textContent = 'EX הפועל ב"ש';
    formerHapoelBadge.title = "שיחק בעבר בהפועל באר שבע, אך אינו בה בעונה האחרונה שנצפתה";
    badges.append(formerHapoelBadge);
  }

  const originBadge = document.createElement("span");
  if (player.likely_origin_city) {
    const confidence = player.likely_origin_confidence === "medium" ? "בינוני" : "נמוך";
    originBadge.className = "badge badge-origin";
    originBadge.textContent = `אזור מוצא משוער: ${player.likely_origin_city} · ביטחון ${confidence}`;
    const basis = player.likely_origin_basis || {};
    const clubs = Array.isArray(basis.clubs) ? basis.clubs.join(" / ") : "";
    const age = Number.isInteger(basis.approx_age) ? `, גיל משוער ${basis.approx_age}` : "";
    originBadge.title = `הערכה לפי המועדון הראשון הרשום${clubs ? `: ${clubs}` : ""}${basis.season ? ` (${basis.season}${age})` : ""}. אינה כתובת מגורים מאומתת.`;
    badges.append(originBadge);
  } else if (player.likely_origin_confidence === "ambiguous" && player.likely_origin_candidates?.length) {
    originBadge.className = "badge badge-origin badge-origin-ambiguous";
    originBadge.textContent = `אזור מוצא לא חד-משמעי: ${player.likely_origin_candidates.join(" / ")}`;
    originBadge.title = "בשנת הרישום הראשונה נמצאו מועדונים ביותר מעיר אחת, ולכן לא נבחרה עיר יחידה.";
    badges.append(originBadge);
  } else {
    originBadge.className = "badge badge-origin badge-origin-ambiguous";
    originBadge.textContent = "אזור מוצא משוער: לא ידוע";
    originBadge.title = "לא נמצאה עיר אמינה עבור המועדון המוקדם ביותר ברישום הזמין. זהו אינו מידע על כתובת מגורים.";
    badges.append(originBadge);
  }

  for (const group of player.age_groups.split(", ").filter(Boolean)) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = group;
    badges.append(badge);
  }

  if (player.above_age_history) {
    const badge = document.createElement("span");
    badge.className = "badge badge-up";
    badge.textContent = `היסטוריה: ${player.above_age_history}`;
    badges.append(badge);
  }
}

function renderTiles(player) {
  const t = player.totals;
  const tiles = [
    { label: "שערים", value: num(t.goals_total), note: `ליגה ${num(t.goals_league)} · גביע ${num(t.goals_cup)}` },
    { label: "משחקים", value: num(t.games_total), note: `פותח ${num(t.starts)} · מחליף ${num(t.sub_on)}` },
    { label: "דקות משחק", value: num(t.minutes_total), note: `${num(t.avg_minutes_per_game)} בממוצע למשחק` },
    { label: "כרטיסים צהובים", value: num(t.yellow_cards_total), note: "ליגה, גביע וטוטו" },
    { label: "כרטיסים אדומים", value: num(t.red_cards), note: "" },
    { label: "קבוצות", value: num(player.num_teams), note: "בכל העונות" },
  ];

  const container = el("tiles");
  container.replaceChildren();
  for (const tile of tiles) {
    const box = document.createElement("div");
    box.className = "tile";
    const label = document.createElement("div");
    label.className = "tile-label";
    label.textContent = tile.label;
    const value = document.createElement("div");
    value.className = "tile-value";
    value.textContent = tile.value;
    box.append(label, value);
    if (tile.note) {
      const note = document.createElement("div");
      note.className = "tile-note";
      note.textContent = tile.note;
      box.append(note);
    }
    container.append(box);
  }
}

function renderSeasons(player) {
  const body = el("seasons-body");
  body.replaceChildren();

  for (const season of player.seasons) {
    const row = document.createElement("tr");

    const seasonCell = document.createElement("td");
    seasonCell.className = "season-cell";
    seasonCell.textContent = season.season;

    const teamCell = document.createElement("td");
    teamCell.className = "team-cell";
    teamCell.append(document.createTextNode(season.team_name));
    const league = document.createElement("span");
    league.className = "league-note";
    league.textContent = season.league_name;
    teamCell.append(league);
    if (!season.stats_available) {
      const historyOnly = document.createElement("span");
      historyOnly.className = "league-note history-only";
      historyOnly.textContent = "רישום היסטורי · סטטיסטיקה מספרית אינה זמינה בהתאחדות";
      teamCell.append(historyOnly);
    } else if (season.registered_no_games) {
      const noGames = document.createElement("span");
      noGames.className = "league-note history-only";
      noGames.textContent = "רשום בסגל · 0 הופעות";
      teamCell.append(noGames);
    }

    const ageCell = document.createElement("td");
    ageCell.append(document.createTextNode(season.age_group));
    if (season.above_age_steps > 0) {
      const chip = document.createElement("span");
      chip.className = "up-chip";
      chip.textContent = `+${season.above_age_steps}`;
      ageCell.append(document.createTextNode(" "), chip);
    }

    const cards = document.createElement("td");
    const yellows = season.stats_available
      ? (season.yellow_cards_league_cup ?? 0) + (season.yellow_cards_toto ?? 0)
      : null;
    if (yellows) {
      const pill = document.createElement("span");
      pill.className = "card-pill card-yellow";
      pill.textContent = yellows;
      cards.append(pill);
    }
    if (season.red_cards) {
      const pill = document.createElement("span");
      pill.className = "card-pill card-red";
      pill.textContent = season.red_cards;
      cards.append(pill);
    }
    if (!yellows && !season.red_cards) cards.textContent = "—";

    row.append(seasonCell, teamCell, ageCell);
    for (const value of [season.games, season.goals, season.minutes]) {
      const cell = document.createElement("td");
      cell.className = "num";
      cell.textContent = num(value);
      row.append(cell);
    }
    row.append(cards);
    body.append(row);
  }
}

/* --------------------------------- map --------------------------------- */

function pinRadius(order, total) {
  if (total <= 1) return PIN_SINGLE;
  return PIN_MAX - (order / (total - 1)) * (PIN_MAX - PIN_MIN);
}

function pinColour(venue) {
  if (venue.above_age_steps > 0) return { fill: "#d29922", stroke: "#f0c860" };
  if (venue.is_current) return { fill: "#3fb950", stroke: "#7ee787" };
  return { fill: "#58a6ff", stroke: "#a5d6ff" };
}

function ensureMap() {
  if (state.map) return state.map;
  state.map = L.map("map", { scrollWheelZoom: true, zoomControl: true });
  // Plain OpenStreetMap tiles: no key, no watermark, and place names in Hebrew.
  // CARTO's free basemaps now burn an "API KEY REQUIRED" watermark into the image.
  // The dark look comes from a CSS filter on the tile pane instead of the provider.
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19,
  }).addTo(state.map);
  state.layer = L.layerGroup().addTo(state.map);
  return state.map;
}

function escapeHTML(text) {
  const box = document.createElement("div");
  box.textContent = text;
  return box.innerHTML;
}

function popupHTML(venue, total) {
  const lines = [
    `<b>${escapeHTML(venue.teams.join(" · "))}</b>`,
    `<span class="popup-meta">מקום ${venue.order + 1} מתוך ${total} · ${escapeHTML(venue.seasons)}</span>`,
  ];
  if (venue.has_detailed_stats) {
    const suffix = venue.stats_complete ? "" : " · לעונות המכוסות בלבד";
    lines.push(
      `<span class="popup-meta">${venue.games} מש׳ · ${venue.goals} שערים · ${venue.minutes} דק׳${suffix}</span>`
    );
  } else {
    lines.push('<span class="popup-meta">היסטוריית מועדון · ללא סטטיסטיקה מפורטת</span>');
  }
  const where = [venue.field_name, venue.city].filter(Boolean).map(escapeHTML).join(" · ");
  if (where) lines.push(`<span class="popup-meta">מגרש: ${where}</span>`);
  if (venue.above_age_steps > 0) {
    lines.push(`<span class="popup-up">שיחק כאן ${venue.above_age_steps} קבוצות גיל מעל גילו</span>`);
  }
  return lines.join("<br>");
}

function renderMap(player) {
  const map = ensureMap();
  state.layer.clearLayers();

  const venues = player.venues;
  const total = venues.length;

  // The path is drawn first so pins sit on top of it.
  if (total > 1) {
    L.polyline(
      venues.map((venue) => [venue.lat, venue.lon]),
      { color: "#8d99a8", weight: 1.5, opacity: 0.6, dashArray: "5,6" }
    ).addTo(state.layer);
  }

  for (const venue of venues) {
    const size = pinRadius(venue.order, total);
    const colour = pinColour(venue);
    const marker = L.marker([venue.lat, venue.lon], {
      icon: L.divIcon({
        className: "",
        html:
          `<div class="pin" style="width:${size}px;height:${size}px;` +
          `background:${colour.fill};border-color:${colour.stroke};` +
          `font-size:${Math.max(9, size * 0.42)}px">${venue.order + 1}</div>`,
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
      }),
      // Earliest places are largest, so keep them behind the smaller recent ones.
      zIndexOffset: venue.order * 10,
    });
    marker.bindPopup(popupHTML(venue, total));
    marker.bindTooltip(`${venue.order + 1}. ${escapeHTML(venue.teams[0])}`, {
      direction: "top",
    });
    marker.addTo(state.layer);
  }

  if (total > 1) {
    map.fitBounds(
      venues.map((venue) => [venue.lat, venue.lon]),
      { padding: [40, 40], maxZoom: 13 }
    );
  } else if (total === 1) {
    map.setView([venues[0].lat, venues[0].lon], 12);
  } else {
    // Nothing to show: fall back to a view of the whole country.
    map.setView([31.7, 34.9], 7);
  }
  // The panel is hidden until the first player arrives, and it stretches to fill the
  // column, so Leaflet may have measured the container before it had its real size.
  setTimeout(() => map.invalidateSize(), 0);

  const missing = player.teams.filter((team) => team.lat === null || team.lon === null);
  const note = el("unlocated");
  if (missing.length) {
    note.hidden = false;
    note.textContent = `ללא מיקום על המפה: ${missing.map((t) => t.team_name).join(", ")}`;
  } else {
    note.hidden = true;
  }
}

/* --------------------------------- wiring --------------------------------- */

function debounce(fn, delay) {
  let handle;
  return (...args) => {
    clearTimeout(handle);
    handle = setTimeout(() => fn(...args), delay);
  };
}

const debouncedSearch = debounce(loadResults, 180);
el("query").addEventListener("input", debouncedSearch);
el("above-age").addEventListener("change", loadResults);
el("birth-year").addEventListener("change", loadResults);
el("current-team").addEventListener("change", loadResults);
el("location").addEventListener("change", loadResults);
el("radius-km").addEventListener("change", loadResults);
el("movements-nav").addEventListener("click", showMovements);
el("shortlists-nav").addEventListener("click", showShortlists);
el("new-shortlist").addEventListener("click", createShortlist);
el("shortlists-back").addEventListener("click", async () => {
  state.activeShortlist = null;
  await loadShortlistState();
  renderShortlistCards();
});
el("header-favorite").addEventListener("click", () => {
  if (state.selected) toggleFavorite(state.selected);
});
el("header-add-shortlist").addEventListener("click", (event) => {
  if (state.selected) openMembershipMenu(state.selected, event.currentTarget);
});
document.addEventListener("click", (event) => {
  const menu = document.querySelector(".shortlist-popover");
  if (menu && !menu.contains(event.target) && !event.target.closest(".shortlist-button")) closeMembershipMenu();
});

Promise.all([loadSummary(), loadShortlistState()]).then(loadResults).catch((error) => {
  el("dataset").textContent = `שגיאה בטעינת הנתונים: ${error.message}`;
});
