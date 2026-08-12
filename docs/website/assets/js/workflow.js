(function () {
  "use strict";

  const root = document.documentElement;
  const themeStorageKey = "cmw-theme";
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  const themeControls = Array.from(document.querySelectorAll("[data-theme-choice]"));
  const themeColor = document.querySelector('meta[name="theme-color"]');

  root.classList.add("js");

  function resolvedTheme() {
    return root.dataset.theme || (systemDark.matches ? "dark" : "light");
  }

  function syncThemeControls() {
    const activeTheme = resolvedTheme();

    themeControls.forEach(function (control) {
      const isActive = control.dataset.themeChoice === activeTheme;
      control.classList.toggle("is-active", isActive);
      control.setAttribute("aria-pressed", String(isActive));
    });

    if (themeColor) {
      themeColor.content = activeTheme === "dark" ? "#11181d" : "#f2efe8";
    }
  }

  themeControls.forEach(function (control) {
    control.addEventListener("click", function () {
      const nextTheme = control.dataset.themeChoice;
      root.dataset.theme = nextTheme;

      try {
        localStorage.setItem(themeStorageKey, nextTheme);
      } catch (error) {
        // The selected theme still applies for this page view.
      }

      syncThemeControls();
    });
  });

  if (typeof systemDark.addEventListener === "function") {
    systemDark.addEventListener("change", function () {
      if (!root.dataset.theme) syncThemeControls();
    });
  } else {
    systemDark.addListener(function () {
      if (!root.dataset.theme) syncThemeControls();
    });
  }

  syncThemeControls();

  const board = document.querySelector("[data-workflow-board]");
  if (!board) return;

  const branchElements = Array.from(board.querySelectorAll("[data-branch]"));
  const viewControls = Array.from(board.querySelectorAll("[data-view]"));
  const status = board.querySelector("[data-view-status]");
  const narrowLayout = window.matchMedia("(max-width: 820px)");
  const viewLabels = { molecular: "Finite", periodic: "Periodic" };
  let selectedView = "both";

  function updateView() {
    const filterIsActive = narrowLayout.matches && selectedView !== "both";

    branchElements.forEach(function (element) {
      element.hidden = filterIsActive && element.dataset.branch !== selectedView;
    });

    viewControls.forEach(function (control) {
      const isSelected = control.dataset.view === selectedView;
      control.classList.toggle("is-active", isSelected);
      control.setAttribute("aria-pressed", String(isSelected));
    });

    board.dataset.activeView = narrowLayout.matches ? selectedView : "both";

    if (status) {
      status.textContent = narrowLayout.matches
        ? selectedView === "both"
          ? "Both finite and periodic workflow stages are visible."
          : viewLabels[selectedView] + " workflow stages are visible."
        : "Finite and periodic workflow stages are visible in aligned columns.";
    }
  }

  viewControls.forEach(function (control) {
    control.addEventListener("click", function () {
      selectedView = control.dataset.view;
      updateView();
    });
  });

  if (typeof narrowLayout.addEventListener === "function") {
    narrowLayout.addEventListener("change", updateView);
  } else {
    narrowLayout.addListener(updateView);
  }

  updateView();
})();
