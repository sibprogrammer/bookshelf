"use strict";

const pdfPreview = document.querySelector("[data-pdf-preview]");
if (pdfPreview) {
  const previousButtons = document.querySelectorAll("[data-preview-previous]");
  const nextButtons = document.querySelectorAll("[data-preview-next]");
  const statuses = document.querySelectorAll("[data-preview-status]");
  const expand = pdfPreview.querySelector("[data-preview-expand]");
  const modalElement = document.querySelector("#pdf-preview-modal");
  const modalImageContainer = modalElement.querySelector("[data-preview-modal-image]");
  const frames = [pdfPreview, modalElement.querySelector("[data-preview-modal-page]")];
  const modal = bootstrap.Modal.getOrCreateInstance(modalElement);
  const firstPageUrl = new URL(pdfPreview.dataset.pdfPreview, window.location.href);
  let pageNumber = 0;
  let pageCount = 0;
  let loading = false;
  let modalOpen = false;
  let largePage = false;
  let currentImageUrl;

  function setStatus(text) {
    statuses.forEach((status) => { status.textContent = text; });
  }

  function updateButtons() {
    previousButtons.forEach((button) => { button.disabled = loading || pageNumber <= 1; });
    nextButtons.forEach((button) => {
      button.disabled = loading || !pageCount || pageNumber >= pageCount;
    });
  }

  async function showPage(number) {
    if (loading || number < 1 || (pageCount && number > pageCount)) return;
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
      setStatus(`Page ${pageNumber} of ${pageCount}`);
    } catch {
      setStatus(pageNumber
        ? `Page ${pageNumber} of ${pageCount}. Unable to load page ${number}. Try again.`
        : "PDF preview unavailable.");
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
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      showPage(pageNumber + (event.key === "ArrowLeft" ? -1 : 1));
    }
  }
  pdfPreview.addEventListener("keydown", handlePageKey);
  modalElement.addEventListener("keydown", handlePageKey);
  expand.addEventListener("click", () => modal.show(expand));
  modalElement.addEventListener("show.bs.modal", () => {
    modalOpen = true;
    if (!largePage) showPage(pageNumber);
  });
  modalElement.addEventListener("hide.bs.modal", () => { modalOpen = false; });
  modalElement.addEventListener("hidden.bs.modal", () => { expand.focus(); });
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
