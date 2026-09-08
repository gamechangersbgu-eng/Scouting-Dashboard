(() => {
  const nativeFetch = window.fetch.bind(window);

  // Add the selected league to player-search requests without changing the existing
  // dashboard search code.
  window.fetch = (input, init) => {
    let url = typeof input === "string" ? input : input.url;
    if (url.includes("/api/players")) {
      const league = document.getElementById("league")?.value;
      if (league) {
        const parsed = new URL(url, window.location.origin);
        parsed.searchParams.set("league", league);
        input = parsed.toString();
      }
    }
    return nativeFetch(input, init);
  };

  async function populateLeagues() {
    const select = document.getElementById("league");
    if (!select) return;

    try {
      const response = await nativeFetch("/api/summary");
      if (!response.ok) throw new Error(`summary -> ${response.status}`);
      const summary = await response.json();
      for (const league of summary.leagues || []) {
        const option = document.createElement("option");
        option.value = league;
        option.textContent = league;
        select.append(option);
      }
    } catch (error) {
      console.error("Failed to load league filter", error);
    }

    select.addEventListener("change", () => {
      if (typeof window.loadResults === "function") {
        window.loadResults();
      }
    });
  }

  populateLeagues();
})();
