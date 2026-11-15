"use strict";

const pdfPreview = document.querySelector("[data-pdf-preview]");
if (pdfPreview) {
  const previous = pdfPreview.querySelector("[data-preview-previous]");
  const next = pdfPreview.querySelector("[data-preview-next]");
  const status = pdfPreview.parentElement.querySelector("[data-preview-status]");
  const firstPageUrl = new URL(pdfPreview.dataset.pdfPreview, window.location.href);
  let pageNumber = 0;
  let pageCount = 0;
  let loading = false;

  async function showPage(number) {
    if (loading || number < 1 || (pageCount && number > pageCount)) return;
    loading = true;
    previous.disabled = true;
    next.disabled = true;
    pdfPreview.setAttribute("aria-busy", "true");
    let imageUrl;

    try {
      const response = await fetch(new URL(String(number), firstPageUrl));
      if (!response.ok) throw new Error("PDF preview unavailable");
      const count = Number(response.headers.get("X-Page-Count"));
      if (!Number.isSafeInteger(count) || count < number) {
        throw new Error("Invalid PDF page count");
      }
      imageUrl = URL.createObjectURL(await response.blob());
      const image = new Image(333, 500);
      image.className = "book-cover book-preview-image img-fluid rounded shadow-sm";
      image.alt = `Page ${number} of ${count} of ${pdfPreview.dataset.bookTitle}`;
      image.src = imageUrl;
      await image.decode();
      pdfPreview.querySelector(".book-cover, .book-cover-placeholder").replaceWith(image);
      pageNumber = number;
      pageCount = count;
      previous.hidden = false;
      next.hidden = false;
      status.textContent = `Page ${pageNumber} of ${pageCount}`;
    } catch {
      status.textContent = pageNumber
        ? `Page ${pageNumber} of ${pageCount}. Unable to load page ${number}. Try again.`
        : "PDF preview unavailable.";
    } finally {
      if (imageUrl) URL.revokeObjectURL(imageUrl);
      loading = false;
      previous.disabled = pageNumber <= 1;
      next.disabled = !pageCount || pageNumber >= pageCount;
      pdfPreview.removeAttribute("aria-busy");
    }
  }

  previous.addEventListener("click", () => showPage(pageNumber - 1));
  next.addEventListener("click", () => showPage(pageNumber + 1));
  pdfPreview.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      showPage(pageNumber + (event.key === "ArrowLeft" ? -1 : 1));
    }
  });
  showPage(1);
}

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
