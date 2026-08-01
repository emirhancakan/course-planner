(function () {
  "use strict";

  const DAY_ORDER = ["M", "T", "W", "Th", "F", "S"];
  const DAY_LABELS = { M: "Mon", T: "Tue", W: "Wed", Th: "Thu", F: "Fri", S: "Sat" };
  const MAX_RESULTS = 100;

  // Spread hues far apart so consecutive additions read as clearly distinct colors.
  const COURSE_HUES = [210, 350, 150, 35, 275, 95, 190, 320, 60, 250, 130, 5];

  function hueForKey(key) {
    let hash = 0;
    for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
    return COURSE_HUES[hash % COURSE_HUES.length];
  }

  function courseColor(key) {
    const hue = hueForKey(key);
    return {
      bg: `hsl(${hue} 75% 50% / 0.24)`,
      border: `hsl(${hue} 70% 45%)`,
      solid: `hsl(${hue} 70% 45%)`,
    };
  }

  let currentSemester = null;
  let currentData = null;      // { semester, slotTimes, sections }
  let bySectionKey = {};       // "CODE.SECTION" -> section
  let selections = {};         // "CODE.SECTION" -> section (subset of bySectionKey)

  const el = {
    semesterSelect: document.getElementById("semesterSelect"),
    searchInput: document.getElementById("searchInput"),
    results: document.getElementById("results"),
    grid: document.getElementById("grid"),
    conflicts: document.getElementById("conflicts"),
    selectedList: document.getElementById("selectedList"),
    summary: document.getElementById("summary"),
  };

  function sectionKey(s) { return s.code + "." + s.section; }
  function storageKey(sem) { return "plannerSelections::" + sem; }

  function loadSelectedKeys(sem) {
    try {
      const raw = localStorage.getItem(storageKey(sem));
      return raw ? JSON.parse(raw) : [];
    } catch (e) { return []; }
  }

  function saveSelectedKeys() {
    localStorage.setItem(storageKey(currentSemester), JSON.stringify(Object.keys(selections)));
  }

  function ensureDataset(semester) {
    if (window.COURSE_DATASETS && window.COURSE_DATASETS[semester]) {
      return Promise.resolve(window.COURSE_DATASETS[semester]);
    }
    return new Promise((resolve, reject) => {
      const filename = "courses-" + semester.split("/").join("-") + ".js";
      const script = document.createElement("script");
      script.src = "data/" + filename;
      script.onload = () => resolve(window.COURSE_DATASETS[semester]);
      script.onerror = () => reject(new Error("Failed to load dataset for " + semester));
      document.body.appendChild(script);
    });
  }

  function formatMeeting(m) {
    const times = currentData.slotTimes[String(m.slot)];
    const timeStr = times ? `${times.start}-${times.end}` : `slot ${m.slot}`;
    return `${DAY_LABELS[m.day] || m.day} ${timeStr} · ${m.room || "TBA"} (${m.type}${m.instructor ? ", " + m.instructor : ""})`;
  }

  function matchesQuery(section, tokens) {
    const haystack = section.__search;
    return tokens.every(t => haystack.indexOf(t) !== -1);
  }

  function renderResults() {
    const query = el.searchInput.value.trim().toLowerCase();
    el.results.innerHTML = "";

    if (!query) {
      el.results.innerHTML = `<div class="empty-note">Type to search over ${currentData.sections.length.toLocaleString()} sections by course code or name.</div>`;
      return;
    }

    const tokens = query.split(/\s+/).filter(Boolean);
    const matches = [];
    for (const s of currentData.sections) {
      if (matchesQuery(s, tokens)) {
        matches.push(s);
        if (matches.length >= MAX_RESULTS) break;
      }
    }

    if (matches.length === 0) {
      el.results.innerHTML = `<div class="empty-note">No matches.</div>`;
      return;
    }

    const frag = document.createDocumentFragment();
    for (const s of matches) {
      const key = sectionKey(s);
      const isSelected = !!selections[key];

      const card = document.createElement("div");
      card.className = "result-card";
      card.innerHTML = `
        <div class="rc-top">
          <div>
            <div class="rc-code">${s.code}.${s.section}</div>
            <div class="rc-name">${escapeHtml(s.name)} · ${escapeHtml(s.department)}</div>
          </div>
          <button class="add-btn" data-key="${key}">${isSelected ? "Added" : "Add"}</button>
        </div>
        <div class="rc-meta">${s.meetings.map(formatMeeting).map(escapeHtml).join("<br>") || "No fixed meeting time"}</div>
      `;
      card.querySelector(".add-btn").disabled = isSelected;
      card.querySelector(".add-btn").addEventListener("click", () => toggleSelect(s));
      frag.appendChild(card);
    }
    el.results.appendChild(frag);

    if (matches.length >= MAX_RESULTS) {
      const note = document.createElement("div");
      note.className = "empty-note";
      note.textContent = `Showing first ${MAX_RESULTS} matches — refine your search for more.`;
      el.results.appendChild(note);
    }
  }

  function toggleSelect(section) {
    const key = sectionKey(section);
    if (selections[key]) {
      delete selections[key];
    } else {
      selections[key] = section;
    }
    saveSelectedKeys();
    renderAll();
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  function renderGrid() {
    const slots = Object.keys(currentData.slotTimes).map(Number).sort((a, b) => a - b);

    // occupancy[day][slot] = array of {section, meeting}
    const occupancy = {};
    for (const d of DAY_ORDER) occupancy[d] = {};

    for (const key in selections) {
      const s = selections[key];
      for (const m of s.meetings) {
        if (!occupancy[m.day]) occupancy[m.day] = {};
        if (!occupancy[m.day][m.slot]) occupancy[m.day][m.slot] = [];
        occupancy[m.day][m.slot].push({ section: s, meeting: m });
      }
    }

    let html = "<thead><tr><th></th>" + DAY_ORDER.map(d => `<th>${DAY_LABELS[d]}</th>`).join("") + "</tr></thead><tbody>";

    for (const slot of slots) {
      const t = currentData.slotTimes[String(slot)];
      html += `<tr><td class="time-col">${t.start}</td>`;
      for (const day of DAY_ORDER) {
        const entries = (occupancy[day] && occupancy[day][slot]) || [];
        if (entries.length === 0) {
          html += "<td></td>";
        } else {
          const conflict = entries.length > 1;
          const blocks = entries.map(({ section, meeting }) => {
            const color = courseColor(sectionKey(section));
            return `
            <div class="cell-block ${conflict ? "conflict" : ""}" style="background:${color.bg}; border-left-color:${color.border};">
              <div class="cb-code">${section.code}.${section.section}</div>
              <div class="cb-room">${escapeHtml(meeting.room || "TBA")}</div>
            </div>`;
          }).join("");
          html += `<td><div class="cell-stack">${blocks}</div></td>`;
        }
      }
      html += "</tr>";
    }
    html += "</tbody>";
    el.grid.innerHTML = html;

    renderConflictSummary(occupancy, slots);
  }

  function renderConflictSummary(occupancy, slots) {
    const lines = [];
    for (const day of DAY_ORDER) {
      for (const slot of slots) {
        const entries = (occupancy[day] && occupancy[day][slot]) || [];
        if (entries.length > 1) {
          const t = currentData.slotTimes[String(slot)];
          const names = entries.map(e => `${e.section.code}.${e.section.section}`).join(" vs ");
          lines.push(`${DAY_LABELS[day]} ${t.start}-${t.end}: ${names}`);
        }
      }
    }
    el.conflicts.innerHTML = lines.length
      ? lines.map(l => `<div class="conflict-line">⚠ Conflict — ${escapeHtml(l)}</div>`).join("")
      : "";
  }

  function renderSelectedList() {
    const keys = Object.keys(selections);
    if (keys.length === 0) {
      el.selectedList.innerHTML = `<div class="empty-note">No courses added yet.</div>`;
      return;
    }
    el.selectedList.innerHTML = keys.sort().map(key => {
      const s = selections[key];
      const color = courseColor(key);
      return `
        <div class="selected-item">
          <span class="si-swatch" style="background:${color.solid}"></span>
          <div class="si-info">
            <div class="si-code">${s.code}.${s.section}</div>
            <div class="si-name">${escapeHtml(s.name)}</div>
          </div>
          <button class="remove-btn" data-key="${key}">Remove</button>
        </div>`;
    }).join("");

    el.selectedList.querySelectorAll(".remove-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const s = bySectionKey[btn.dataset.key];
        if (s) toggleSelect(s);
      });
    });
  }

  function renderSummary() {
    const keys = Object.keys(selections);
    const totalCredit = keys.reduce((sum, k) => sum + (Number(selections[k].credit) || 0), 0);
    const totalEcts = keys.reduce((sum, k) => sum + (Number(selections[k].ects) || 0), 0);
    el.summary.innerHTML = `
      <span><strong>${keys.length}</strong> courses</span>
      <span><strong>${totalCredit}</strong> credits</span>
      <span><strong>${totalEcts}</strong> ECTS</span>
    `;
  }

  function renderAll() {
    renderGrid();
    renderSelectedList();
    renderSummary();
    renderResults();
  }

  function populateSemesterDropdown() {
    const semesters = window.AVAILABLE_SEMESTERS || [];
    el.semesterSelect.innerHTML = semesters.map(s => `<option value="${s}">${s}</option>`).join("");
  }

  function switchSemester(semester) {
    currentSemester = semester;
    localStorage.setItem("plannerLastSemester", semester);

    ensureDataset(semester).then(data => {
      currentData = data;
      bySectionKey = {};
      for (const s of data.sections) {
        // Match on course code and name only - including department or
        // instructor made queries like "engineering" match every section of
        // every *_ENGINEERING department.
        // Codes are stored with the registrar's irregular spacing ("CE  101"),
        // so index a compact form too and "ce101" still finds it.
        const compact = s.code.replace(/\s+/g, "");
        s.__search = [
          s.code,
          compact,
          s.code + "." + s.section,
          compact + "." + s.section,
          s.name,
        ].join(" ").toLowerCase();
        bySectionKey[sectionKey(s)] = s;
      }

      selections = {};
      for (const key of loadSelectedKeys(semester)) {
        if (bySectionKey[key]) selections[key] = bySectionKey[key];
      }

      renderAll();
    }).catch(err => {
      el.results.innerHTML = `<div class="empty-note">${escapeHtml(err.message)}</div>`;
    });
  }

  el.searchInput.addEventListener("input", renderResults);
  el.semesterSelect.addEventListener("change", () => switchSemester(el.semesterSelect.value));

  populateSemesterDropdown();
  const semesters = window.AVAILABLE_SEMESTERS || [];
  const lastUsed = localStorage.getItem("plannerLastSemester");
  const initial = (lastUsed && semesters.includes(lastUsed)) ? lastUsed : semesters[0];
  if (initial) {
    el.semesterSelect.value = initial;
    switchSemester(initial);
  } else {
    el.results.innerHTML = `<div class="empty-note">No course data found. Run the scraper first.</div>`;
  }
})();
