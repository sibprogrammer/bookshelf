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
