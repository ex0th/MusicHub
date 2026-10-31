// Setzt das Farbschema vor dem ersten Rendern (verhindert Aufblitzen). Standard: dunkel.
(function () {
  var theme = "dark";
  try {
    theme = localStorage.getItem("musichub.theme") || "dark";
  } catch (e) {}
  if (theme === "system") {
    theme = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  document.documentElement.dataset.theme = theme;
})();
