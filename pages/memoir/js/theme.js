// Appearance is local to this browser; it never modifies AstrBot settings.
export function initTheme() {
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "auto";
  let hostDark = null;
  try {
    const saved = localStorage.getItem("memoir.theme");
    if (["auto", "light", "dark"].includes(saved)) preference = saved;
  } catch {
    // Embedded browsers may disable storage; switching still works in memory.
  }

  const apply = () => {
    const dark = preference === "auto" ? hostDark ?? system.matches : preference === "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.querySelectorAll('input[name="theme"]').forEach((input) => {
      input.checked = input.value === preference;
    });
    document.getElementById("theme-status").textContent = preference === "auto"
      ? `跟随${hostDark === null ? "系统" : "AstrBot"} · ${dark ? "深色" : "浅色"}`
      : "已手动选择";
    window.__fxRecolor?.();
  };

  document.querySelector(".theme-picker").addEventListener("change", (event) => {
    if (!event.target.matches('input[name="theme"]')) return;
    preference = event.target.value;
    try {
      localStorage.setItem("memoir.theme", preference);
    } catch {
      // Keep the user's choice for the current page when persistence is denied.
    }
    apply();
  });
  system.addEventListener("change", apply);
  window.addEventListener("storage", (event) => {
    if (event.key !== "memoir.theme" && event.key !== null) return;
    preference = ["light", "dark"].includes(event.newValue) ? event.newValue : "auto";
    apply();
  });
  apply();

  return (context) => {
    if (typeof context?.isDark === "boolean") {
      hostDark = context.isDark;
      apply();
    }
  };
}
