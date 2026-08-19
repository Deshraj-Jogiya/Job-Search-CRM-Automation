// Light/dark theme toggle. The initial theme class is already applied
// to <html> by the inline script in _base.html's <head> (before paint,
// to avoid a flash of the wrong theme) -- this file just owns the
// click-to-toggle interaction, persistence, and keeping the icon in
// sync with the current state.
(function () {
    var toggleBtn = document.getElementById("theme-toggle");
    if (!toggleBtn) return;

    var icon = toggleBtn.querySelector(".theme-icon");

    function isLight() {
        return document.documentElement.classList.contains("light-theme");
    }

    function syncIcon() {
        if (icon) icon.textContent = isLight() ? "☀️" : "🌙";
    }

    syncIcon();

    toggleBtn.addEventListener("click", function () {
        document.documentElement.classList.toggle("light-theme");
        try {
            localStorage.setItem("theme", isLight() ? "light" : "dark");
        } catch (e) { /* localStorage unavailable -- theme just won't persist across loads */ }
        syncIcon();
    });
})();
