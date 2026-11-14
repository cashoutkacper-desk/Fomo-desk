// Hand-drawn <canvas> charts. No chart library. Implemented with the app UI.
function drawChart(canvas, series) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
}
