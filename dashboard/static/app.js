"use strict";

// Marker radii in pixels. The earliest club gets the largest pin, so size reads as
// "how far back in the career this was".
const PIN_MAX = 30;
const PIN_MIN = 14;
const PIN_SINGLE = 22;

const el = (id) => document.getElementById(id);
const num = (value) => (value === null || value === undefined ? "—" : Number(value).toLocaleString("he-IL"));

const state = { players: [], selected: null, map: null, layer: null, primed: false };

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

    const sub = document.createElement("div");
    sub.className = "result-sub";
    const born = player.birth_year ? `נולד ${player.birth_year}` : "שנת לידה לא ידועה";
    sub.textContent = `${born} · ${player.current_team || "—"} · ${player.goals_total} שערים`;

    item.append(name, sub);
    item.addEventListener("click", () => selectPlayer(player.player_id));
    list.append(item);
  }
}

/* ------------------------------ player view ------------------------------ */

async function selectPlayer(playerId) {
  state.selected = playerId;
  renderResults();
  const player = await getJSON(`/api/player/${playerId}`);
  el("player").hidden = false;
  renderHeader(player);
  renderTiles(player);
  renderSeasons(player);
  renderMap(player);
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

  // Always show the earliest registered club when the backend can identify one.
  // This is factual registration history; it is deliberately kept separate from the
  // inferred origin-area badge below.
  if (player.first_club) {
    const firstClubBadge = document.createElement("span");
    firstClubBadge.className = "badge badge-first-club";
    const firstSeason = player.first_registered_season
      ? ` · ${player.first_registered_season}`
      : "";
    firstClubBadge.textContent = `מועדון ראשון: ${player.first_club}${firstSeason}`;
    firstClubBadge.title = "המועדון המוקדם ביותר שנמצא בהיסטוריית הרישום הזמינה של ההתאחדות.";
    badges.append(firstClubBadge);
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

loadSummary().then(loadResults).catch((error) => {
  el("dataset").textContent = `שגיאה בטעינת הנתונים: ${error.message}`;
});
