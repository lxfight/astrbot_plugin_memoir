import { toast } from "./utils.js";

export const bridge = window.AstrBotPluginPage;

// Unified API error wrapper: toast + console on failure, returns null so caller renders error panel
export async function safe(label, fn, { silent = false } = {}) {
  try {
    return await fn();
  } catch (err) {
    const msg = err?.message || String(err);
    console.error(`[Memoir] ${label}:`, err);
    if (!silent) toast(`${label}失败：${msg}`, "err");
    return null;
  }
}
