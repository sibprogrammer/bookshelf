"use strict";

const indexForm = document.querySelector("#index-form");
if (indexForm) {
  indexForm.addEventListener("submit", () => {
    const button = indexForm.querySelector("button");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.querySelector(".spinner-border").classList.remove("d-none");
    button.querySelector("[data-button-label]").textContent = "Indexing…";
  });
}

window.addEventListener("pageshow", () => {
  if (indexForm) {
    const button = indexForm.querySelector("button");
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.querySelector(".spinner-border").classList.add("d-none");
    button.querySelector("[data-button-label]").textContent = "Index / reindex books";
  }
});

const searchInput = document.querySelector("#book-search");
if (searchInput) {
  const searchForm = searchInput.form;
  const main = document.querySelector("#main-content");
  const status = document.querySelector("#search-status");
  let timer;
  let controller;
  let revision = 0;
  let composing = false;
  let searched = false;

  function cancelSearch() {
    clearTimeout(timer);
    controller?.abort();
    revision += 1;
    main.removeAttribute("aria-busy");
    status.classList.add("visually-hidden");
    status.textContent = "";
  }

  async function search() {
    const currentRevision = revision;
    const url = new URL(searchForm.action);
    url.searchParams.set("q", searchInput.value.trim());
    controller = new AbortController();
    main.setAttribute("aria-busy", "true");
    status.textContent = "Searching…";

    try {
      const response = await fetch(url, { signal: controller.signal });
      if (!response.ok) throw new Error("Search failed");
      const html = await response.text();
      if (currentRevision !== revision) return;
      const page = new DOMParser().parseFromString(html, "text/html");
      const content = page.querySelector("#main-content");
      if (!content) throw new Error("Missing search results");

      main.replaceChildren(...content.childNodes);
      document.title = page.title;
      document.querySelectorAll("nav [aria-current='page']").forEach((link) => {
        link.classList.remove("active");
        link.removeAttribute("aria-current");
      });
      if (window.location.pathname !== url.pathname) {
        window.history.pushState(null, "", url);
      } else {
        window.history.replaceState(null, "", url);
      }
      searched = true;
      status.textContent = main.querySelector(".badge")?.textContent
        || main.querySelector("p")?.textContent || "Search updated.";
    } catch (error) {
      if (currentRevision !== revision || error.name === "AbortError") return;
      status.classList.remove("visually-hidden");
      status.textContent = "Unable to update search. Press Enter or Search to try again.";
    } finally {
      if (currentRevision === revision) main.removeAttribute("aria-busy");
    }
  }

  searchInput.addEventListener("input", (event) => {
    cancelSearch();
    if (!composing && !event.isComposing) timer = setTimeout(search, 250);
  });
  searchInput.addEventListener("compositionstart", () => {
    composing = true;
    cancelSearch();
  });
  searchInput.addEventListener("compositionend", () => {
    composing = false;
    cancelSearch();
    timer = setTimeout(search, 250);
  });
  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    cancelSearch();
    if (!composing) search();
  });
  window.addEventListener("popstate", () => {
    cancelSearch();
    if (searched) window.location.reload();
  });
}
