// Prevent the browser default of Space activating focused buttons,
// which would re-trigger "Simulate Ride" / "Run Simulation" unexpectedly.
document.addEventListener("keydown", function (e) {
  if (e.code === "Space" && e.target.tagName === "BUTTON") {
    e.preventDefault();
  }
});
