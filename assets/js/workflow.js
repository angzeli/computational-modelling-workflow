(function () {
  "use strict";

  document.documentElement.classList.add("js");

  const board = document.querySelector("[data-workflow-board]");
  if (!board) return;

  const branches = Array.from(board.querySelectorAll("[data-workflow-branch]"));
  const controls = Array.from(board.querySelectorAll("[data-view]"));
  const status = board.querySelector("[data-view-status]");
  const narrowLayout = window.matchMedia("(max-width: 820px)");
  let selectedView = "both";

  function updateView() {
    const filterIsActive = narrowLayout.matches && selectedView !== "both";

    branches.forEach(function (branch) {
      branch.hidden = filterIsActive && branch.dataset.workflowBranch !== selectedView;
    });

    controls.forEach(function (control) {
      const isSelected = control.dataset.view === selectedView;
      control.classList.toggle("is-active", isSelected);
      control.setAttribute("aria-pressed", String(isSelected));
    });

    board.dataset.activeView = narrowLayout.matches ? selectedView : "both";

    if (status) {
      status.textContent = narrowLayout.matches
        ? selectedView === "both"
          ? "Both molecular and periodic workflows are visible."
          : selectedView.charAt(0).toUpperCase() + selectedView.slice(1) + " workflow is visible."
        : "Both molecular and periodic workflows are visible side by side.";
    }
  }

  controls.forEach(function (control) {
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
