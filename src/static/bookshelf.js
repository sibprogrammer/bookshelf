"use strict";

const bookPreview = document.querySelector("[data-book-preview]");
if (bookPreview) {
  const previousButtons = document.querySelectorAll("[data-preview-previous]");
  const nextButtons = document.querySelectorAll("[data-preview-next]");
  const statuses = document.querySelectorAll("[data-preview-status]");
  const expand = bookPreview.querySelector("[data-preview-expand]");
  const modalElement = document.querySelector("#book-preview-modal");
  const modalImageContainer = modalElement.querySelector("[data-preview-modal-image]");
  const frames = [bookPreview, modalElement.querySelector("[data-preview-modal-page]")];
  const modal = bootstrap.Modal.getOrCreateInstance(modalElement);
  const firstPageUrl = new URL(bookPreview.dataset.bookPreview, window.location.href);
  const firstPageNumber = Number(bookPreview.dataset.previewStart ?? 1);
  let pageNumber = null;
  let pageCount = 0;
  let loading = false;
  let modalOpen = false;
  let largePage = false;
  let currentImageUrl;

  function setStatus(text) {
    statuses.forEach((status) => { status.textContent = text; });
  }

  function pageLabel() {
    return pageNumber === 0 ? "Cover" : `Page ${pageNumber} of ${pageCount}`;
  }

  function updateButtons() {
    previousButtons.forEach((button) => {
      button.disabled = loading || pageNumber === null || pageNumber <= firstPageNumber;
    });
    nextButtons.forEach((button) => {
      button.disabled = loading || !pageCount || pageNumber >= pageCount;
    });
  }

  async function showPage(number) {
    if (loading || number < firstPageNumber || (pageCount && number > pageCount)) return;
    loading = true;
    updateButtons();
    frames.forEach((frame) => frame.setAttribute("aria-busy", "true"));
    const large = modalOpen;
    let loaded = false;
    let imageUrl;

    try {
      const url = new URL(String(number), firstPageUrl);
      if (large) url.searchParams.set("size", "large");
      const response = await fetch(url);
      if (!response.ok) throw new Error("Book preview unavailable");
      const count = Number(response.headers.get("X-Page-Count"));
      if (!Number.isSafeInteger(count) || count < Math.max(number, 1)) {
        throw new Error("Invalid book page count");
      }
      imageUrl = URL.createObjectURL(await response.blob());
      const image = new Image(333, 500);
      image.className = "book-cover book-preview-image img-fluid rounded shadow-sm";
      image.alt = number === 0
        ? `Cover of ${bookPreview.dataset.bookTitle}`
        : `Page ${number} of ${count} of ${bookPreview.dataset.bookTitle}`;
      image.src = imageUrl;
      await image.decode();
      bookPreview.querySelector(".book-cover, .book-cover-placeholder").replaceWith(image);
      const enlargedImage = image.cloneNode();
      enlargedImage.className = "book-preview-modal-image";
      modalImageContainer.replaceChildren(enlargedImage);
      if (currentImageUrl) URL.revokeObjectURL(currentImageUrl);
      currentImageUrl = imageUrl;
      imageUrl = undefined;
      pageNumber = number;
      pageCount = count;
      largePage = large;
      loaded = true;
      expand.disabled = false;
      [...previousButtons, ...nextButtons].forEach((button) => { button.hidden = false; });
      setStatus(pageLabel());
    } catch {
      setStatus(pageNumber !== null
        ? `${pageLabel()}. Unable to load ${number === 0 ? "cover" : `page ${number}`}. Try again.`
        : "Book preview unavailable.");
    } finally {
      if (imageUrl) URL.revokeObjectURL(imageUrl);
      loading = false;
      updateButtons();
      frames.forEach((frame) => frame.removeAttribute("aria-busy"));
      // Opening the modal during a page turn should upgrade the arriving page too.
      if (loaded && modalOpen && !large) showPage(pageNumber);
    }
  }

  previousButtons.forEach((button) => {
    button.addEventListener("click", () => showPage(pageNumber - 1));
  });
  nextButtons.forEach((button) => {
    button.addEventListener("click", () => showPage(pageNumber + 1));
  });
  function handlePageKey(event) {
    if (pageNumber === null) return;
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      showPage(pageNumber + (event.key === "ArrowLeft" ? -1 : 1));
    }
  }
  bookPreview.addEventListener("keydown", handlePageKey);
  modalElement.addEventListener("keydown", handlePageKey);
  expand.addEventListener("click", () => modal.show(expand));
  modalElement.addEventListener("show.bs.modal", () => {
    modalOpen = true;
    if (!largePage) showPage(pageNumber);
  });
  modalElement.addEventListener("hide.bs.modal", () => { modalOpen = false; });
  modalElement.addEventListener("hidden.bs.modal", () => { expand.focus(); });
  showPage(firstPageNumber);
}

const indexForm = document.querySelector("#index-form");
const indexModal = document.querySelector("#index-modal");
if (indexForm) {
  indexForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = indexForm.querySelector("button");
    if (button.disabled) return;
    const status = indexModal.querySelector("[role='status']");
    const progress = indexModal.querySelector("[role='progressbar']");
    const bar = progress.firstElementChild;
    const footer = indexModal.querySelector(".modal-footer");
    status.textContent = "Finding books…";
    progress.hidden = false;
    progress.removeAttribute("aria-valuenow");
    bar.style.width = "100%";
    bar.classList.add("progress-bar-striped", "progress-bar-animated");
    footer.hidden = true;
    bootstrap.Modal.getOrCreateInstance(indexModal).show();
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.querySelector(".spinner-border").classList.remove("d-none");
    button.querySelector("[data-button-label]").textContent = "Indexing…";
    let active = true;
    async function poll() {
      try {
        const response = await fetch(`${indexForm.action}/progress`, { cache: "no-store" });
        const { phase, completed, total } = await response.json();
        if (active && response.ok && phase) {
          const percent = total ? Math.round(completed / total * 100) : null;
          status.textContent = total ? `${phase}: ${completed} of ${total} (${percent}%)` : `${phase}…`;
          bar.classList.remove("w-100");
          bar.style.width = `${percent ?? 100}%`;
          bar.classList.toggle("progress-bar-striped", percent === null);
          bar.classList.toggle("progress-bar-animated", percent === null);
          if (percent === null) progress.removeAttribute("aria-valuenow");
          else progress.setAttribute("aria-valuenow", percent);
        }
      } catch { /* The index request handles failures. */ }
      if (active) timer = setTimeout(poll, 400);
    }
    let timer = setTimeout(poll, 400);
    try {
      const response = await fetch(indexForm.action, { method: "POST" });
      if (!response.ok) {
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        throw new Error(page.querySelector(".alert-danger")?.textContent || "Reindexing failed.");
      }
      window.location.assign(response.url);
    } catch (error) {
      status.textContent = error instanceof TypeError
        ? "Connection lost. Reindexing may still be running. Reload to check your library."
        : error.message;
      progress.hidden = true;
      footer.hidden = false;
      footer.querySelector("button").focus();
    } finally {
      active = false;
      clearTimeout(timer);
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.querySelector(".spinner-border").classList.add("d-none");
      button.querySelector("[data-button-label]").textContent = "Reindex";
    }
  });
  indexModal.addEventListener("hidden.bs.modal", () => indexForm.querySelector("button").focus());
}

window.addEventListener("pageshow", () => {
  if (indexForm) {
    bootstrap.Modal.getInstance(indexModal)?.hide();
    const button = indexForm.querySelector("button");
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.querySelector(".spinner-border").classList.add("d-none");
    button.querySelector("[data-button-label]").textContent = "Reindex";
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
