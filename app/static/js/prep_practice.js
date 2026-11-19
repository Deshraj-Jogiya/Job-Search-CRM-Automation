// Interview prep pages: Parsons drill, pattern drill, random pick, and (in the public demo only) practice kept in the
// visitor's own browser with the same review gaps the server uses (prep_practice_service.INTERVAL_DAYS).
(function () {
    "use strict";
    var INTERVALS = [1, 3, 7, 21, 45];
    var STORE = "cp-prep-practice";

    function load() {
        try { return JSON.parse(window.localStorage.getItem(STORE) || "{}"); } catch (e) { return {}; }
    }
    function save(data) {
        try { window.localStorage.setItem(STORE, JSON.stringify(data)); } catch (e) { /* private mode: keep going */ }
    }
    function shuffle(list) {
        var a = list.slice();
        for (var i = a.length - 1; i > 0; i--) {
            var j = Math.floor(Math.random() * (i + 1));
            var t = a[i]; a[i] = a[j]; a[j] = t;
        }
        return a;
    }
    function fmt(ts) {
        return new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    }

    // ---- a link to a card or question (#key) opens it, along with any section it sits in
    function openHash() {
        var id = decodeURIComponent((window.location.hash || "").slice(1));
        var el = id && document.getElementById(id);
        if (!el) return;
        for (var n = el; n; n = n.parentElement) { if (n.tagName === "DETAILS") n.open = true; }
        el.scrollIntoView();
    }
    openHash();
    window.addEventListener("load", function () { window.requestAnimationFrame(openHash); });
    window.addEventListener("hashchange", openHash);

    // ---- practice in the browser (demo only: the forms carry data-local)
    document.querySelectorAll("form.prep-rate[data-local]").forEach(function (form) {
        var key = form.getAttribute("data-key");
        var status = form.querySelector(".prep-rate-status");
        var row = load()[key];
        if (row && status) status.textContent = "Practised " + row.reviews + "x, next review " + fmt(row.due);
        form.addEventListener("submit", function (ev) {
            ev.preventDefault();
            var btn = ev.submitter || document.activeElement;
            var rating = parseInt(btn && btn.value, 10);
            if (!rating) return;
            var data = load();
            var r = data[key] || { box: 0, reviews: 0 };
            r.box = rating === 1 ? 0 : rating === 3 ? Math.min(r.box + 1, INTERVALS.length - 1) : r.box;
            r.last = rating;
            r.reviews += 1;
            r.due = Date.now() + INTERVALS[r.box] * 86400000;
            data[key] = r;
            save(data);
            if (status) status.textContent = "Saved in this browser. Next review in " + INTERVALS[r.box] + " day" + (INTERVALS[r.box] === 1 ? "" : "s") + ".";
        });
    });

    // ---- practice page: due list and counts from the browser (demo)
    var localDue = document.querySelector("[data-local-due]");
    var randomBox = document.querySelector(".prep-random");
    var items = randomBox ? JSON.parse(randomBox.getAttribute("data-items") || "[]") : [];
    if (localDue) {
        var data = load();
        var now = Date.now();
        var due = items.filter(function (it) { return data[it.key] && data[it.key].due <= now; });
        due.sort(function (a, b) { return data[a.key].last - data[b.key].last; });
        if (due.length) {
            due.forEach(function (it) {
                var li = document.createElement("li");
                var a = document.createElement("a");
                a.href = it.href; a.textContent = it.label;
                if (it.external) { a.target = "_blank"; a.rel = "noopener"; }
                li.textContent = it.group + ": ";
                li.appendChild(a);
                localDue.appendChild(li);
            });
        } else {
            var li = document.createElement("li");
            li.textContent = "Nothing due in this browser yet. Rate a few items on the other tabs.";
            localDue.appendChild(li);
        }
        localDue.hidden = false;
        document.querySelectorAll(".prep-summary[data-prefix]").forEach(function (box) {
            var prefix = box.getAttribute("data-prefix");
            var n = { practised: 0, solid: 0, due: 0 };
            items.forEach(function (it) {
                var r = data[it.key];
                if (!r || it.key.indexOf(prefix) !== 0) return;
                n.practised += 1;
                if (r.box >= 3) n.solid += 1;
                if (r.due <= now) n.due += 1;
            });
            Object.keys(n).forEach(function (k) { var el = box.querySelector('[data-n="' + k + '"]'); if (el) el.textContent = n[k]; });
        });
    }

    // ---- random pick
    if (randomBox) {
        var out = randomBox.querySelector("[data-random-out]");
        randomBox.querySelector("[data-random-next]").addEventListener("click", function () {
            if (!items.length) return;
            var it = items[Math.floor(Math.random() * items.length)];
            out.textContent = it.group + ": ";
            var a = document.createElement("a");
            a.href = it.href; a.textContent = it.label;
            if (it.external) { a.target = "_blank"; a.rel = "noopener"; }
            out.appendChild(a);
        });
    }

    // ---- Parsons drill: put the lines of the worked example in order
    document.querySelectorAll(".prep-parsons").forEach(function (box) {
        var lines = JSON.parse(box.getAttribute("data-lines") || "[]");
        var list = box.querySelector("[data-parsons-list]");
        var result = box.querySelector("[data-parsons-result]");

        function move(li, dir) {
            var other = dir < 0 ? li.previousElementSibling : li.nextElementSibling;
            if (!other) return;
            if (dir < 0) list.insertBefore(li, other); else list.insertBefore(other, li);
            li.querySelector(dir < 0 ? "[data-up]" : "[data-down]").focus();
        }
        function render() {
            list.innerHTML = "";
            result.textContent = "";
            var order = shuffle(lines);
            if (lines.length > 1 && order.join("\n") === lines.join("\n")) order.push(order.shift());
            order.forEach(function (text) {
                var li = document.createElement("li");
                li.className = "prep-parsons-line";
                var code = document.createElement("code");
                code.textContent = text;
                var up = document.createElement("button");
                up.type = "button"; up.className = "btn-secondary"; up.textContent = "↑"; up.setAttribute("data-up", "");
                up.setAttribute("aria-label", "Move up: " + text.trim());
                var down = document.createElement("button");
                down.type = "button"; down.className = "btn-secondary"; down.textContent = "↓"; down.setAttribute("data-down", "");
                down.setAttribute("aria-label", "Move down: " + text.trim());
                up.addEventListener("click", function () { move(li, -1); });
                down.addEventListener("click", function () { move(li, 1); });
                li.appendChild(code); li.appendChild(up); li.appendChild(down);
                list.appendChild(li);
            });
        }
        render();
        if (window.Sortable) new window.Sortable(list, { animation: 120, handle: "code" });
        box.querySelector("[data-parsons-shuffle]").addEventListener("click", render);
        box.querySelector("[data-parsons-check]").addEventListener("click", function () {
            var now = Array.prototype.map.call(list.querySelectorAll("code"), function (c) { return c.textContent; });
            var right = 0;
            now.forEach(function (t, i) { if (t === lines[i]) right += 1; });
            result.textContent = right === lines.length
                ? "All " + lines.length + " lines in order. Now write it from a blank file."
                : right + " of " + lines.length + " lines are in the right place. Think about what each step needs before it.";
        });
    });

    // ---- pattern drill: name the pattern for a random problem
    var drill = document.getElementById("pattern-drill");
    if (drill) {
        var problems = JSON.parse(drill.getAttribute("data-problems") || "[]");
        var names = JSON.parse(drill.getAttribute("data-patterns") || "[]");
        var card = drill.querySelector("[data-drill-card]");
        var statement = drill.querySelector("[data-drill-statement]");
        var choice = drill.querySelector("[data-drill-choice]");
        var res = drill.querySelector("[data-drill-result]");
        var current = null;
        names.forEach(function (n) { var o = document.createElement("option"); o.value = n; o.textContent = n; choice.appendChild(o); });
        drill.querySelector("[data-drill-next]").addEventListener("click", function () {
            current = problems[Math.floor(Math.random() * problems.length)];
            statement.textContent = current.statement;
            res.textContent = "";
            card.hidden = false;
            choice.focus();
        });
        drill.querySelector("[data-drill-check]").addEventListener("click", function () {
            if (!current) return;
            var ok = current.patterns.map(function (p) { return p.name; });
            res.textContent = (ok.indexOf(choice.value) !== -1 ? "Yes. " : "Not quite. ") + current.title + ": " + ok.join(" or ") + ".";
        });
    }
})();
