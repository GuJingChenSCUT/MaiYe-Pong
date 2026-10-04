"use strict";
(() => {
  const root = document.documentElement;
  const system = matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";
  try { preference = localStorage.getItem("maiyebang-theme") || "system"; } catch {}
  if (!["system", "light", "dark"].includes(preference)) preference = "system";
  function apply() {
    const theme = preference === "system" ? (system.matches ? "dark" : "light") : preference;
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#10252d" : "#145d68";
  }
  apply();
  system.addEventListener("change", apply);
  document.addEventListener("DOMContentLoaded", () => {
    const control = document.getElementById("theme-select");
    control.value = preference;
    control.addEventListener("change", () => {
      preference = control.value;
      try { localStorage.setItem("maiyebang-theme", preference); } catch {}
      apply();
    });
  });
})();
