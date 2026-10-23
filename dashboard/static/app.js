"use strict";

// Marker radii in pixels. The earliest club gets the largest pin, so size reads as
// "how far back in the career this was".
const PIN_MAX = 30;
const PIN_MIN = 14;
const PIN_SINGLE = 22;

const el = (id) => document.getElementById(id);
const num = (value) => (value === null || value === undefined ? "—" : Number(value).toLocaleString("he-IL"));

const state = {
  players: [],
  selected: null,
  player: null,
  favorites: new Map(),
  map: null,
  layer: null,
  primed: false,
};

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

/* ------------------------------ data loading ------------------------------ */

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} -> ${response.status}`);
  return response.json();
}

async function sendJSON(method, url, body) {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    // 503 means favorites.csv is locked, usually because it is open in Excel.
    const reason = response.status === 503 ? "הקובץ favorites.csv פתוח בתוכנה אחרת" : `שגיאה ${response.status}`;
    throw new Error(reason);
  }
  return response.json();
}

async function loadSummary() {
  const summary = await getJSON("/api/summary");
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

    const sub = document.createElement("div");
    sub.className = "result-sub";
    const born = player.birth_year ? `נולד ${player.birth_year}` : "שנת לידה לא ידועה";
    sub.textContent = `${born} · ${player.current_team || "—"}`;

    item.append(name, sub);
    item.addEventListener("click", () => selectPlayer(player.player_id));
    list.append(item);
  }
}

/* -------------------------------- favourites -------------------------------- */

async function loadFavorites() {
  const favorites = await getJSON("/api/favorites");
  state.favorites = new Map(favorites.map((favorite) => [favorite.player_id, favorite]));
  renderFavorites();
}

function renderFavorites() {
  const list = el("favorites");
  list.replaceChildren();
  const count = state.favorites.size;
  el("favorites-count").textContent = count ? `(${count})` : "";
  el("favorites-meta").textContent = count === 1 ? "שחקן מועדף אחד" : count ? `${count} שחקנים מועדפים` : "";

  if (!count) {
    const note = document.createElement("li");
    note.className = "empty-note";
    note.textContent = "עדיין אין מועדפים. כדי להוסיף, לחצו על \"☆ הוספה למועדפים\" בכרטיס של שחקן.";
    list.append(note);
    return;
  }

  // The Map keeps the server's newest-first order, with fresh additions put in front.
  for (const favorite of state.favorites.values()) {
    const item = document.createElement("li");
    if (favorite.player_id === state.selected) item.classList.add("active");

    const name = document.createElement("div");
    name.className = "result-name";
    const label = document.createElement("span");
    label.className = "fav-name";
    label.textContent = favorite.player_name || `שחקן #${favorite.player_id}`;
    name.append(label);
    if (favorite.notes.trim()) {
      const note = document.createElement("span");
      note.className = "fav-note";
      note.textContent = favorite.notes.trim().split("\n")[0];
      note.title = favorite.notes;
      name.append(note);
    }

    const sub = document.createElement("div");
    sub.className = "result-sub";
    const born = favorite.birth_year ? `נולד ${favorite.birth_year}` : "שנת לידה לא ידועה";
    sub.textContent = `${favorite.current_team || "—"} · ${born}`;

    item.append(name, sub);
    item.addEventListener("click", () => selectPlayer(favorite.player_id));
    list.append(item);
  }
}

// The two header tabs swap the sidebar between search and favourites; the player card
// on the right stays as it is, so a scout can flip views without losing their place.
function showSidebarView(view) {
  for (const [name, tab, panel] of [
    ["search", el("tab-search"), el("search-view")],
    ["favorites", el("tab-favorites"), el("favorites-view")],
  ]) {
    tab.setAttribute("aria-selected", String(name === view));
    panel.hidden = name !== view;
  }
}

function renderFavoriteControls(player) {
  const favorite = state.favorites.get(player.player_id);
  const button = el("favorite-toggle");
  button.setAttribute("aria-pressed", String(Boolean(favorite)));
  button.classList.toggle("is-favorite", Boolean(favorite));
  button.querySelector(".star-icon").textContent = favorite ? "★" : "☆";
  button.querySelector(".star-label").textContent = favorite ? "במועדפים" : "הוספה למועדפים";
  button.title = favorite ? "הסרה מהמועדפים" : "הוספה למועדפים";

  el("favorite-notes").hidden = !favorite;
  el("notes-input").value = favorite ? favorite.notes : "";
  setNotesStatus("");
}

function setNotesStatus(text, kind = "") {
  const status = el("notes-status");
  status.textContent = text;
  status.className = `notes-status ${kind}`.trim();
}

async function toggleFavorite() {
  const player = state.player;
  if (!player) return;
  const favorite = state.favorites.get(player.player_id);
  try {
    if (favorite) {
      if (favorite.notes.trim() && !confirm("להסיר את השחקן מהמועדפים? ההערות שלו יימחקו.")) return;
      await sendJSON("DELETE", `/api/favorites/${player.player_id}`);
      state.favorites.delete(player.player_id);
    } else {
      const saved = await sendJSON("PUT", `/api/favorites/${player.player_id}`, {});
      state.favorites = new Map([[saved.player_id, saved], ...state.favorites]);
    }
  } catch (error) {
    alert(`השמירה נכשלה: ${error.message}`);
    return;
  }
  renderFavorites();
  renderFavoriteControls(player);
  renderMatches(player);
}

async function saveNotes() {
  const player = state.player;
  if (!player || !state.favorites.has(player.player_id)) return;
  const button = el("notes-save");
  button.disabled = true;
  try {
    const saved = await sendJSON("PUT", `/api/favorites/${player.player_id}`, {
      notes: el("notes-input").value,
    });
    state.favorites.set(saved.player_id, saved);
    renderFavorites();
    setNotesStatus("נשמר ✓", "ok");
  } catch (error) {
    setNotesStatus(`השמירה נכשלה: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------ recent matches ------------------------------ */

function formatDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  return match ? `${match[3]}/${match[2]}/${match[1]}` : value || "—";
}

async function renderMatches(player) {
  const panel = el("matches-panel");
  const body = el("matches-body");
  if (!state.favorites.has(player.player_id)) {
    panel.hidden = true;
    body.replaceChildren();
    return;
  }
  panel.hidden = false;

  const result = await getJSON(`/api/player/${player.player_id}/matches`);
  // A slow response must not paint over a player selected in the meantime.
  if (state.selected !== player.player_id) return;
  body.replaceChildren();

  if (!result.available || !result.matches.length) {
    const note = document.createElement("p");
    note.className = "matches-empty";
    note.textContent = result.available
      ? "לא נמצאו משחקים לשחקן הזה בקובץ המשחקים."
      : "נתוני משחקים יתעדכנו אוטומטית בעדכון השבועי";
    body.append(note);
    return;
  }

  const table = document.createElement("table");
  table.className = "seasons matches";
  table.innerHTML =
    "<thead><tr><th>תאריך</th><th>קבוצה</th><th>יריבה</th><th>תחרות</th>" +
    "<th>דק׳</th><th>הרכב</th><th>שערים</th><th>כרטיסים</th></tr></thead>";
  const rows = document.createElement("tbody");

  for (const match of result.matches) {
    const row = document.createElement("tr");
    const cells = [
      { text: formatDate(match.match_date), className: "season-cell" },
      { text: match.team_name || `קבוצה #${match.team_id}` },
      { text: match.opponent_name || (match.opponent_team_id ? `קבוצה #${match.opponent_team_id}` : "—") },
      { text: match.competition_name || (match.competition_id ? `תחרות #${match.competition_id}` : "—") },
      { text: num(match.minutes_played), className: "num" },
      { text: match.is_starter === null ? "—" : match.is_starter ? "פותח" : "מחליף" },
      { text: num(match.goals), className: "num" },
    ];
    for (const cell of cells) {
      const td = document.createElement("td");
      td.textContent = cell.text;
      if (cell.className) td.className = cell.className;
      row.append(td);
    }

    const cards = document.createElement("td");
    if (match.yellow_cards) {
      const pill = document.createElement("span");
      pill.className = "card-pill card-yellow";
      pill.textContent = match.yellow_cards;
      cards.append(pill);
    }
    if (match.red_cards) {
      const pill = document.createElement("span");
      pill.className = "card-pill card-red";
      pill.textContent = match.red_cards;
      cards.append(pill);
    }
    if (!match.yellow_cards && !match.red_cards) cards.textContent = "—";
    row.append(cards);
    rows.append(row);
  }

  table.append(rows);
  body.append(table);
}

/* ------------------------------ player view ------------------------------ */

async function selectPlayer(playerId) {
  state.selected = playerId;
  renderResults();
  renderFavorites();
  const player = await getJSON(`/api/player/${playerId}`);
  if (state.selected !== playerId) return;
  state.player = player;
  el("player").hidden = false;
  renderHeader(player);
  renderFavoriteControls(player);
  renderTiles(player);
  renderSeasons(player);
  renderMap(player);
  renderMatches(player).catch((error) => {
    el("matches-body").textContent = `שגיאה בטעינת המשחקים: ${error.message}`;
  });
}

function renderHeader(player) {
  el("player-name").textContent = player.player_name;

  const photo = el("player-photo");
  if (player.image_url) {
    photo.src = player.image_url;
    photo.alt = player.player_name;
    photo.hidden = false;
  } else {
    photo.hidden = true;
    photo.removeAttribute("src");
  }

  // One uniform label/value list instead of a grey meta line plus a row of pills.
  const facts = [
    { label: "קבוצה נוכחית", value: player.current_team || "—" },
    ageGroupFact(player),
    { label: "שנת לידה", value: player.birth_year || "לא ידועה" },
  ];

  // The earliest registered club is factual registration history; it is kept apart
  // from the inferred origin area below.
  if (player.first_club) {
    const season = player.first_registered_season ? ` (${player.first_registered_season})` : "";
    facts.push({
      label: "מועדון ראשון",
      value: `${player.first_club}${season}`,
      title: "המועדון המוקדם ביותר שנמצא בהיסטוריית הרישום הזמינה של ההתאחדות.",
    });
  }

  const origin = originFact(player);
  if (origin) facts.push(origin);

  if (player.above_age_history) {
    facts.push({ label: "היסטוריה מעל הגיל", value: player.above_age_history, className: "fact-up" });
  }
  facts.push({ label: "מס׳ שחקן", value: player.player_id });

  const list = el("player-facts");
  list.replaceChildren();
  for (const fact of facts) {
    const item = document.createElement("div");
    item.className = "fact";
    if (fact.title) item.title = fact.title;
    const term = document.createElement("dt");
    term.textContent = fact.label;
    const value = document.createElement("dd");
    value.textContent = fact.value;
    if (fact.className) value.className = fact.className;
    item.append(term, value);
    list.append(item);
  }
}

function ageGroupFact(player) {
  const label = "קבוצת גיל נוכחית";
  const group = player.current_age_group || "—";
  if (player.plays_above_age) {
    // The left-to-right mark keeps the "+" in front of the number on an RTL page.
    return { label, value: `${group} · ‎+${player.age_groups_above} מעל גילו`, className: "fact-up" };
  }
  return { label, value: `${group} · בקבוצת הגיל שלו` };
}

function originFact(player) {
  const label = "אזור מוצא משוער";
  if (player.likely_origin_city) {
    const confidence = player.likely_origin_confidence === "medium" ? "בינוני" : "נמוך";
    const basis = player.likely_origin_basis || {};
    const clubs = Array.isArray(basis.clubs) ? basis.clubs.join(" / ") : "";
    const age = Number.isInteger(basis.approx_age) ? `, גיל משוער ${basis.approx_age}` : "";
    return {
      label,
      value: `${player.likely_origin_city} (ביטחון ${confidence})`,
      title: `הערכה לפי המועדון הראשון הרשום${clubs ? `: ${clubs}` : ""}${basis.season ? ` (${basis.season}${age})` : ""}. אינה כתובת מגורים מאומתת.`,
    };
  }
  if (player.likely_origin_confidence === "ambiguous" && player.likely_origin_candidates?.length) {
    return {
      label,
      value: `לא חד-משמעי: ${player.likely_origin_candidates.join(" / ")}`,
      title: "בשנת הרישום הראשונה נמצאו מועדונים ביותר מעיר אחת, ולכן לא נבחרה עיר יחידה.",
    };
  }
  return null;
}

const pct = (value) => (value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`);
const rate = (value) => (value === null || value === undefined ? "—" : value.toFixed(2));

function trendTile(m) {
  const label = "מגמה מול עונה קודמת";
  const previous = m.previous;
  if (!previous) return { label, value: "—", note: "אין עונה קודמת להשוואה" };
  if (m.goals_per_match === null || previous.goals_per_match === null) {
    return { label, value: "—", note: "אין נתוני שערים להשוואה" };
  }
  const delta = m.goals_per_match - previous.goals_per_match;
  // Rounded first, so a tiny change that displays as 0.00 is not coloured as a trend.
  const shown = Math.round(delta * 100) / 100;
  return {
    label,
    // The left-to-right mark keeps the sign in front of the number on an RTL page.
    value: `‎${shown > 0 ? "+" : ""}${shown.toFixed(2)}`,
    className: shown > 0 ? "trend-up" : shown < 0 ? "trend-down" : "",
    note: `בשערים למשחק, לעומת ${previous.season}`,
  };
}

function minutesShareNote(m) {
  const minutes = `${num(m.minutes)} דקות`;
  // The season-on-season minutes comparison lives here, beside the metric it is about.
  if (!m.previous || m.previous.minutes_share === null) return minutes;
  return `${minutes} · לעומת ${pct(m.previous.minutes_share)} ב-${m.previous.season}`;
}

function metricTiles(m) {
  const peers = m.goals_percentile === null
    ? `פחות מ-${m.min_minutes_for_rank} דקות, אין השוואה`
    : m.goals_percentile === 100
      ? `הגבוה ביותר ב${m.age_group}`
      : `גבוה מ-${m.goals_percentile}% מהשחקנים ב${m.age_group}`;
  return [
    { label: "שערים למשחק מלא", value: rate(m.goals_per_match), note: `${num(m.goals)} שערים · ${peers}` },
    { label: "חלק משערי הקבוצה", value: pct(m.team_goal_share), note: `${num(m.goals)} מתוך ${num(m.team_goals)} שערים` },
    { label: "חלק מהדקות האפשריות", value: pct(m.minutes_share), note: minutesShareNote(m) },
    { label: "אחוז פתיחות", value: pct(m.start_rate), note: `${num(m.starts)} פתיחות ב-${num(m.games)} משחקים` },
    { label: "משחקים מלאים", value: num(m.full_matches_played), note: `פתח וסיים, מתוך ${num(m.starts)} פתיחות` },
    trendTile(m),
  ];
}

function renderTiles(player) {
  const m = player.metrics;
  const caption = el("tiles-caption");
  if (m) {
    const why = m.is_reference_season ? "" : " · העונה האחרונה שבה שיחק";
    caption.textContent = `מדדי עונה ${m.season} · ${m.age_group}${why}`;
  } else {
    caption.textContent = "אין לשחקן משחקים בעונות שיש עליהן סטטיסטיקה";
  }

  const tiles = m
    ? metricTiles(m)
    : ["שערים למשחק מלא", "חלק משערי הקבוצה", "חלק מהדקות האפשריות", "אחוז פתיחות", "משחקים מלאים", "מגמה מול עונה קודמת"]
        .map((label) => ({ label, value: "—", note: "" }));

  const container = el("tiles");
  container.replaceChildren();
  for (const tile of tiles) {
    const box = document.createElement("div");
    box.className = "tile";
    const label = document.createElement("div");
    label.className = "tile-label";
    label.textContent = tile.label;
    const value = document.createElement("div");
    value.className = `tile-value ${tile.className || ""}`.trim();
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
    if (!season.has_stats) {
      const historyOnly = document.createElement("span");
      historyOnly.className = "league-note history-only";
      historyOnly.textContent = "היסטוריה בלבד · אין סטטיסטיקה מפורטת";
      teamCell.append(historyOnly);
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
    const yellows = season.yellow_cards_league_cup + season.yellow_cards_toto;
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
    for (const value of [season.games, season.goals]) {
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
el("tab-search").addEventListener("click", () => showSidebarView("search"));
el("tab-favorites").addEventListener("click", () => showSidebarView("favorites"));
el("favorite-toggle").addEventListener("click", toggleFavorite);
el("notes-save").addEventListener("click", saveNotes);
el("notes-input").addEventListener("input", () => setNotesStatus("יש שינויים שלא נשמרו", "dirty"));

// Favourites load first so the opening player's star is already right.
loadSummary().then(loadFavorites).then(loadResults).catch((error) => {
  // The dataset line is hidden normally and only surfaces to report a load failure.
  el("dataset").textContent = `שגיאה בטעינת הנתונים: ${error.message}`;
  el("dataset").hidden = false;
});
