// Medição de fluidez (só com ROBIN_DEBUG=1, pela palette): roda as animações principais enquanto
// amostra o intervalo entre quadros com requestAnimationFrame. Meta: 60 fps (quadros de ~16,7ms).

import { call } from "./bridge.js";
import { wait } from "./overlay.js";
import { toast } from "./toast.js";

const DROPPED_MS = 25; // um quadro acima disso já é perceptível a 60 Hz

function sampleFrames() {
  const deltas = [];
  let last = 0;
  let running = true;
  const tick = (now) => {
    if (last) deltas.push(now - last);
    last = now;
    if (running) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return () => {
    running = false;
    return deltas;
  };
}

function summarize(deltas) {
  const sorted = [...deltas].sort((a, b) => a - b);
  const average = deltas.reduce((sum, delta) => sum + delta, 0) / Math.max(1, deltas.length);
  return {
    frames: deltas.length,
    fps: Math.round(1000 / average),
    p95: Number((sorted[Math.floor(sorted.length * 0.95)] ?? 0).toFixed(1)),
    worst: Number((sorted.at(-1) ?? 0).toFixed(1)),
    dropped: deltas.filter((delta) => delta > DROPPED_MS).length,
  };
}

/** Roda o roteiro e mostra o resultado num toast (e no console). */
export async function measureSmoothness({ sidebar, palette, models, chat, mock }) {
  const results = {};
  const measure = async (name, run) => {
    const stop = sampleFrames();
    await run();
    results[name] = summarize(stop());
  };

  await measure("sidebar", async () => {
    for (let round = 0; round < 2; round += 1) {
      sidebar.toggle();
      await wait(450);
      sidebar.toggle();
      await wait(450);
    }
  });
  await measure("palette", async () => {
    palette.open();
    await wait(350);
    palette.toggle();
    await wait(300);
  });
  await measure("popover", async () => {
    await models.open();
    await wait(350);
    document.getElementById("model-trigger").click();
    await wait(300);
  });
  await measure("toasts", async () => {
    for (let index = 0; index < 3; index += 1) {
      toast(`Teste de fluidez ${index + 1}`, { iconName: "check" });
      await wait(120);
    }
    await wait(900);
  });
  if (mock) {
    await measure("streaming", async () => {
      chat.send("longo");
      await wait(5000);
      chat.stop();
      await wait(600);
    });
  }

  const lines = Object.entries(results).map(
    ([name, r]) => `${name}: ${r.fps} fps, p95 ${r.p95}ms, pior ${r.worst}ms, ${r.dropped}/${r.frames} quadros > ${DROPPED_MS}ms`,
  );
  console.table(results);
  for (const line of lines) call("log_debug", `fluidez ${line}`); // relatório completo no terminal do app
  const totalDropped = Object.values(results).reduce((sum, r) => sum + r.dropped, 0);
  toast(`Fluidez medida: ${totalDropped} quadros perdidos (detalhes no terminal)`, { iconName: "cpu" });
  return results;
}
