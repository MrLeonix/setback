// Setback console -- vanilla-JS client for the docket board and case page.
//
// Deliberately dependency-free: this is a small, judge-readable script
// wiring four things against console/app.py's JSON API --
//   1. the create-case flow on the docket board (POST /api/cases) --
//      console/app.py's server-rendered board has no case-creation UI of
//      its own (only the JSON route existed), so this script builds the
//      form itself; see `initCreateCaseForm` below.
//   2. the interview chat (GET/POST /api/cases/{id}/interview)
//   3. photo/document upload (POST /api/cases/{id}/documents)
//   4. the SSE event stream (GET /api/cases/{id}/events), which reloads
//      the page's server-rendered sections whenever a new event lands, so
//      reviewer opinions / gate decisions / the submission appear live as
//      the tribunal job progresses, with no client-side re-implementation
//      of the rendering setback.console.app already does server-side.
//
// Wave 5 UI revamp (Package C -- static/app.js only, per UI-SPEC.md):
//   - Bubble-asymmetry transcript rendering (Sec 2.1) + quick-reply chips
//     (Sec 2.2), both funnelled through one `submitAnswer(text)` path.
//   - A humanised stage stepper (Sec 2.3).
//   - Citation-chip behaviour (Sec 2.5): clause popover / doc+page scroll /
//     image-region scroll-and-highlight, docking onto the wave-4 overlay.
//   - The tribunal "courtroom sitting" timeline (Sec 2.7/2.8/2.10, Sec 3.5)
//     rendered live from the existing `review_verdict` / `adjudication_
//     decision` / `gate_decision` SSE events -- no new backend events.
//   - Graceful-degrade "Change" links on the check-answers summary list
//     (per this wave's design-judgment notes): the interview state
//     machine cannot reopen an arbitrary past stage this wave, so a
//     "Change" click focuses the typed-answer input for a correction turn
//     rather than attempting a stage jump that doesn't exist.
//
// Cross-lane data contract this file assumes from `console/app.py`
// (Package B) -- documented here since this file cannot touch app.py
// itself; see this work package's notesForOrchestrator for the same list:
//   - `suggested_replies: string[] | null` on the interview turn JSON
//     (both GET's auto-started turn and POST's response), for CONFIRMING/
//     ASK_MORE stages only.
//   - `data-ground-id="<id>"` on each rendered ground element, so this
//     script can resolve a ground's human claim text for the tribunal
//     timeline instead of falling back to its category label.
//   - `data-doc-id`/`data-page` on the annotated-overlay `<img>` (or its
//     wrapping element), so a `--doc`/`--region` citation chip can find
//     and scroll to the right document viewer.
//   - `data-run-cost-usd="0.0238"` on `<body>` (alongside the existing
//     `data-case-id`/`data-last-sequence`), the case's ledger total, for
//     the tribunal timeline's "This run: $0.02" cost chip.
// Every feature that reads one of these degrades to a plain, correct
// fallback when the attribute is absent (documented inline at each use),
// so this file is fully functional against today's app.py and upgrades
// automatically once Package B ships the attributes above.
(function () {
  "use strict";

  // ===========================================================================
  // Wave 9 (LEO-FEEDBACK-UIUX.md): theme toggle, present in every page's
  // header (`_THEME_TOGGLE_BUTTON`, console/app.py). Runs unconditionally,
  // before the case-page-only early return below, since the landing page
  // and docket board have no `data-case-id`.
  // ===========================================================================

  (function initThemeToggle() {
    const THEME_KEY = "setback:theme";

    // An explicit `?theme=` on THIS load always wins (filming consistency,
    // console/app.py's `force_theme`) -- only fall back to a remembered
    // preference when the server didn't already stamp one on `<html>`.
    try {
      if (!document.documentElement.getAttribute("data-theme")) {
        const stored = window.localStorage.getItem(THEME_KEY);
        if (stored === "light" || stored === "dark") {
          document.documentElement.setAttribute("data-theme", stored);
        }
      }
    } catch (err) {
      // localStorage unavailable (private browsing, locked-down browser):
      // the system/query default stands, exactly as if nothing were stored.
    }

    const toggleBtn = document.getElementById("theme-toggle");
    if (!toggleBtn) return;
    toggleBtn.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme");
      const systemPrefersDark =
        window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      const isDark = current ? current === "dark" : systemPrefersDark;
      const next = isDark ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try {
        window.localStorage.setItem(THEME_KEY, next);
      } catch (err) {
        // Preference just won't survive a reload -- the toggle still works
        // for this page view, which is the important part.
      }
    });
  })();

  const RESIDENT_SESSION_KEY = "setback:resident-session";

  // A stable per-browser identifier so revisiting the same DA number from
  // the same browser resumes the same case (`state.firestore.case_id_for`
  // is a deterministic hash of application_number + resident_session) --
  // a different browser/profile correctly starts a fresh case. Wrapped in
  // try/catch: private-browsing or a locked-down browser can make
  // `localStorage` throw on access, and losing session continuity there is
  // far better than the create-case flow crashing outright.
  function getResidentSessionId() {
    try {
      let sessionId = window.localStorage.getItem(RESIDENT_SESSION_KEY);
      if (!sessionId) {
        sessionId =
          (window.crypto && window.crypto.randomUUID && window.crypto.randomUUID()) ||
          `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        window.localStorage.setItem(RESIDENT_SESSION_KEY, sessionId);
      }
      return sessionId;
    } catch (err) {
      return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }
  }

  function initCreateCaseForm() {
    const docketList = document.querySelector(".docket-list");
    const main = document.querySelector("main.container");
    if (!docketList || !main) return; // not the docket board

    const card = document.createElement("section");
    card.className = "card create-case-card";
    card.innerHTML = `
      <h3>Start a new objection</h3>
      <form class="create-case-form" id="create-case-form">
        <input
          id="application-number-input"
          type="text"
          placeholder="DA number, e.g. DA2026/0359 or PAN-661190"
          autocomplete="off"
          required
        >
        <button type="submit">Start</button>
      </form>
      <p class="create-case-hint">
        Enter the development application number for the property near you --
        Setback will walk you through raising an objection in a few minutes.
        Revisiting the same DA number from this browser resumes your case.
      </p>
    `;
    // Prepended above the "Docket board" heading itself: the call to
    // action ("start a new objection") reads before the list of existing
    // cases, not buried beside it.
    main.insertBefore(card, main.firstChild);

    const form = card.querySelector("#create-case-form");
    const input = card.querySelector("#application-number-input");

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const applicationNumber = input.value.trim();
      if (!applicationNumber) return;

      const existingError = card.querySelector(".create-case-error");
      if (existingError) existingError.remove();

      const submitButton = form.querySelector("button");
      submitButton.disabled = true;
      submitButton.textContent = "Starting...";

      try {
        const response = await fetch("/api/cases", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            application_number: applicationNumber,
            resident_session: getResidentSessionId(),
          }),
        });
        if (response.ok) {
          const body = await response.json();
          window.location.href = `/cases/${body.case_id}`;
          return;
        }
        let detail = "Could not start a case for that application number. Please try again.";
        try {
          const errorBody = await response.json();
          if (errorBody && errorBody.detail) detail = errorBody.detail;
        } catch (err) {
          // Non-JSON error body: keep the generic message above.
        }
        const errorEl = document.createElement("p");
        errorEl.className = "create-case-error";
        errorEl.textContent = detail;
        card.appendChild(errorEl);
      } finally {
        submitButton.disabled = false;
        submitButton.textContent = "Start";
      }
    });
  }

  initCreateCaseForm();

  // ===========================================================================
  // Wave 9: the public landing page (`/`) -- one DA-number input that starts
  // a new objection, plus a "your previous cases" list read entirely from
  // this browser's own localStorage (LEO-FEEDBACK-UIUX.md §1). Nothing
  // server-side: a different browser/profile simply sees an empty list.
  // ===========================================================================

  const PREVIOUS_CASES_KEY = "setback:previous-cases";
  const MAX_REMEMBERED_CASES = 20;

  function getPreviousCases() {
    try {
      const raw = window.localStorage.getItem(PREVIOUS_CASES_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
      return [];
    }
  }

  function recordPreviousCase(caseId, applicationNumber) {
    try {
      const withoutThisCase = getPreviousCases().filter((entry) => entry.case_id !== caseId);
      withoutThisCase.unshift({
        case_id: caseId,
        application_number: applicationNumber,
        created_at: Date.now(),
      });
      window.localStorage.setItem(
        PREVIOUS_CASES_KEY,
        JSON.stringify(withoutThisCase.slice(0, MAX_REMEMBERED_CASES))
      );
    } catch (err) {
      // A remembered-cases convenience only -- never block case creation.
    }
  }

  function renderPreviousCases() {
    const section = document.getElementById("previous-cases");
    const list = document.getElementById("previous-cases-list");
    if (!section || !list) return;
    const previous = getPreviousCases();
    if (previous.length === 0) return;
    list.innerHTML = "";
    for (const entry of previous) {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = `/cases/${entry.case_id}`;
      link.textContent = entry.application_number || entry.case_id;
      item.appendChild(link);
      list.appendChild(item);
    }
    section.hidden = false;
  }

  function initLandingPage() {
    const form = document.getElementById("start-case-form");
    const input = document.getElementById("application-number-input");
    const errorEl = document.getElementById("start-case-error");
    if (!form || !input) return; // not the landing page

    renderPreviousCases();

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const applicationNumber = input.value.trim();
      if (!applicationNumber) return;
      if (errorEl) {
        errorEl.hidden = true;
        errorEl.textContent = "";
      }
      const submitButton = form.querySelector("button");
      const originalLabel = submitButton ? submitButton.textContent : "";
      if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = "Starting...";
      }
      try {
        const response = await fetch("/api/cases", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            application_number: applicationNumber,
            resident_session: getResidentSessionId(),
          }),
        });
        if (response.ok) {
          const body = await response.json();
          recordPreviousCase(body.case_id, body.application_number || applicationNumber);
          window.location.href = `/cases/${body.case_id}`;
          return;
        }
        let detail = "Could not start a case for that application number. Please try again.";
        try {
          const errorBody = await response.json();
          if (errorBody && errorBody.detail) detail = errorBody.detail;
        } catch (err) {
          // Non-JSON error body: keep the generic message above.
        }
        if (errorEl) {
          errorEl.textContent = detail;
          errorEl.hidden = false;
        }
      } catch (err) {
        if (errorEl) {
          errorEl.textContent =
            "Could not reach the server. Please check your connection and try again.";
          errorEl.hidden = false;
        }
      } finally {
        if (submitButton) {
          submitButton.disabled = false;
          submitButton.textContent = originalLabel;
        }
      }
    });
  }

  initLandingPage();

  const caseId = document.body.getAttribute("data-case-id");
  if (!caseId) {
    return;
  }

  // Record this case in "your previous cases" if it was reached directly
  // (e.g. a bookmarked/shared link) rather than via the landing-page form
  // above, so the localStorage list stays complete either way.
  recordPreviousCase(caseId, document.title.replace(/^Setback -- /, ""));

  // ===========================================================================
  // Round-2 UI feedback, item 1: real tabs, not a ref-link nav -- the
  // founder's own correction: "the tabs rendered on the right side do not
  // show which one is selected, and content should only be rendered for
  // the selected tab (it's not a ref link for the page block, it's an
  // interactive component that renders the associated content when it's
  // selected)." console/app.py server-renders every panel plus the full
  // WAI-ARIA tablist markup (role="tab"/"tabpanel", aria-selected,
  // aria-controls, tabindex) with Grounds selected by default -- this
  // wires the switching behaviour (click + full arrow-key/Home/End
  // keyboard support per the ARIA Authoring Practices tabs pattern) by
  // toggling `hidden`, never `style.display` (a stray inline style would
  // permanently defeat the `[hidden]` CSS contract every other component
  // in this file already relies on -- see the typing-indicator/lightbox
  // `[hidden]` fixes above).
  // ===========================================================================

  const sectionTabs = Array.from(document.querySelectorAll('.section-tabs [role="tab"]'));

  function switchToTab(tabId) {
    let switched = false;
    for (const tab of sectionTabs) {
      const isTarget = tab.id === `tab-${tabId}`;
      tab.setAttribute("aria-selected", isTarget ? "true" : "false");
      tab.tabIndex = isTarget ? 0 : -1;
      const panel = document.getElementById(tab.getAttribute("aria-controls"));
      if (panel) panel.hidden = !isTarget;
      if (isTarget) switched = true;
    }
    return switched;
  }

  sectionTabs.forEach((tab, index) => {
    tab.addEventListener("click", () => {
      switchToTab(tab.id.replace(/^tab-/, ""));
    });
    tab.addEventListener("keydown", (event) => {
      let targetIndex = null;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        targetIndex = (index + 1) % sectionTabs.length;
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        targetIndex = (index - 1 + sectionTabs.length) % sectionTabs.length;
      } else if (event.key === "Home") {
        targetIndex = 0;
      } else if (event.key === "End") {
        targetIndex = sectionTabs.length - 1;
      }
      if (targetIndex === null) return;
      event.preventDefault();
      const targetTab = sectionTabs[targetIndex];
      switchToTab(targetTab.id.replace(/^tab-/, ""));
      targetTab.focus();
    });
  });

  // Used by the citation-chip jump-to-evidence behaviour below: a chip can
  // point at content living inside a currently-hidden tabpanel (e.g. the
  // Overlay tab while Grounds is showing) -- switch to that tab first so
  // "scroll to and flash the cited evidence" actually shows the resident
  // something, rather than scrolling inside an invisible panel.
  function ensureVisibleInTabs(el) {
    if (!el) return;
    const panel = el.closest('[role="tabpanel"]');
    if (panel && panel.hidden) {
      switchToTab(panel.id.replace(/^panel-/, ""));
    }
  }

  // ===========================================================================
  // Shared helpers
  // ===========================================================================

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function formatCost(usd) {
    const n = Number(usd);
    // No chip before there is a real cost to be quietly proud of --
    // `data-run-cost-usd` is always present (per Package B's contract,
    // `"0.000000"` before any tribunal run), so treat <=0 as "nothing to
    // show yet" rather than rendering a literal "$0.00".
    if (!Number.isFinite(n) || n <= 0) return null;
    return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
  }

  // ===========================================================================
  // Sec 2.1 -- message bubbles, Sec 2.3 -- stage stepper
  // ===========================================================================

  const transcriptEl = document.getElementById("interview-transcript");
  const interviewForm = document.getElementById("interview-form");
  const interviewInput = document.getElementById("interview-input");
  const uploadTriggerBtn = document.getElementById("upload-trigger");
  const uploadInput = document.getElementById("upload-input");
  const uploadStatusChip = document.getElementById("upload-status-chip");
  const startTribunalBtn = document.getElementById("start-tribunal");
  const typingIndicatorEl = document.getElementById("typing-indicator");
  const interviewSendBtn = interviewForm ? interviewForm.querySelector("button[type=submit]") : null;

  // LEO-FEEDBACK-UIUX.md §2: "I had no visual cue the model was thinking...
  // I don't know if the chat is broken." An animated ellipsis while a reply
  // is pending, input+send disabled meanwhile so a resident can't fire a
  // second answer into an already-in-flight turn.
  function setInterviewPending(pending) {
    if (typingIndicatorEl) typingIndicatorEl.hidden = !pending;
    if (interviewInput) interviewInput.disabled = pending;
    if (interviewSendBtn) interviewSendBtn.disabled = pending;
  }

  const STAGE_LABELS = {
    opening: "Starting",
    clarifying: "Clarifying",
    requesting_evidence: "Gathering evidence",
    confirming: "Confirming",
    ask_more: "Anything else?",
    done: "Interview complete",
  };

  // The old role vocabulary ("system"/"resident") is renamed here to the
  // wave-5 bubble vocabulary ("ai"/"resident"/"ai-system") -- see
  // UI-SPEC.md Sec 2.1/3.2. `_persist_system_turn`'s stored `role: "system"`
  // and `_turn_to_json`'s replayed turns are mapped through this alias so
  // nothing in app.py needs to change for the bubble asymmetry to apply.
  const ROLE_ALIASES = { system: "ai", ai: "ai", resident: "resident", "ai-system": "ai-system" };

  let lastAiTurnEl = null;
  let stageStepperEl = null;

  function ensureStageStepper() {
    if (stageStepperEl || !transcriptEl || !transcriptEl.parentNode) return stageStepperEl;
    stageStepperEl = document.createElement("div");
    stageStepperEl.className = "stage-stepper";
    stageStepperEl.innerHTML = '<span class="stage-stepper__count"></span>';
    // A working "go back" API doesn't exist in the interview flow this
    // wave (UI-SPEC.md Sec 2.3) -- ship the counter alone rather than a
    // back-arrow button that does nothing when pressed.
    transcriptEl.parentNode.insertBefore(stageStepperEl, transcriptEl);
    return stageStepperEl;
  }

  function updateStageStepper(stage) {
    const el = ensureStageStepper();
    if (!el) return;
    const label = STAGE_LABELS[stage] || "Working";
    const countEl = el.querySelector(".stage-stepper__count");
    if (countEl) countEl.textContent = label;
  }

  function appendTurn(role, text) {
    if (!transcriptEl) return null;
    const resolvedRole = ROLE_ALIASES[role] || "ai";
    const turn = document.createElement("div");
    turn.className = `chat-turn chat-turn--${resolvedRole}`;
    if (resolvedRole === "ai") {
      turn.innerHTML =
        '<span class="chat-turn__label">Setback</span>' +
        `<p class="chat-turn__text">${escapeHtml(text)}</p>`;
      lastAiTurnEl = turn;
    } else if (resolvedRole === "ai-system") {
      turn.innerHTML = `<p class="chat-turn__text">${escapeHtml(text)}</p>`;
    } else {
      turn.innerHTML = `<p class="chat-turn__text">${escapeHtml(text)}</p>`;
    }
    transcriptEl.appendChild(turn);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    return turn;
  }

  function renderTranscript(turns) {
    if (!transcriptEl) return;
    transcriptEl.innerHTML = "";
    lastAiTurnEl = null;
    for (const turn of turns) {
      appendTurn("ai", turn.prompt);
    }
  }

  // --- Sec 2.2 -- quick-reply chip row --------------------------------------
  //
  // Renders under the latest AI turn when its response carries a closed
  // answer set. Clicking a chip calls the *same* `submitAnswer` path the
  // typed-input form uses -- this is what keeps "the input is never
  // disabled while chips are showing" true by construction, not by
  // convention, and it's why the chip row must be built after
  // `submitAnswer` is declared but wired through it, never a fork.

  function clearQuickReplies() {
    if (!transcriptEl) return;
    const existing = transcriptEl.querySelector(".quick-replies");
    if (existing) existing.remove();
  }

  function renderQuickReplies(suggestions) {
    clearQuickReplies();
    if (!transcriptEl || !Array.isArray(suggestions) || suggestions.length === 0) return;
    const row = document.createElement("div");
    row.className = "quick-replies";
    for (const suggestion of suggestions) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip chip--reply";
      chip.textContent = suggestion;
      chip.addEventListener("click", () => {
        row.remove();
        submitAnswer(suggestion);
      });
      row.appendChild(chip);
    }
    // Anchored right after the AI turn it answers, not just appended at
    // the very end -- keeps the row visually paired with its question
    // even if a resident bubble is still mid-flight above it.
    if (lastAiTurnEl && lastAiTurnEl.parentNode === transcriptEl) {
      lastAiTurnEl.insertAdjacentElement("afterend", row);
    } else {
      transcriptEl.appendChild(row);
    }
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  // --- Sec 2.13 -- inline error state (interview submit path only; the
  // upload/tribunal actions below already have their own error handling
  // from before this wave, now upgraded to the same three-part copy). ---

  function renderTranscriptError(message, onRetry) {
    if (!transcriptEl) return;
    const card = document.createElement("div");
    card.className = "state-card state-card--error";
    card.setAttribute("role", "alert");
    card.innerHTML = `<p class="state-card__heading">${escapeHtml(message)}</p>
      <p>Nothing you've entered was lost, and your deadline is unaffected.</p>`;
    if (onRetry) {
      const retryBtn = document.createElement("button");
      retryBtn.type = "button";
      retryBtn.className = "chip chip--reply";
      retryBtn.textContent = "Try again";
      retryBtn.addEventListener("click", () => {
        card.remove();
        onRetry();
      });
      card.appendChild(retryBtn);
    }
    transcriptEl.appendChild(card);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  // --- the one submit path (Sec 2.2's prerequisite refactor) ----------------
  //
  // Split in two: `submitAnswer` appends the resident's bubble exactly
  // once and hands off to `postAnswer`; a failed request's "Try again"
  // retries `postAnswer` alone, so retrying never re-appends a second,
  // duplicate resident bubble for the same answer.

  async function postAnswer(answer) {
    setInterviewPending(true);
    try {
      const response = await fetch(`/api/cases/${caseId}/interview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer }),
      });
      if (!response.ok) {
        renderTranscriptError("We couldn't send that just now.", () => postAnswer(answer));
        return;
      }
      const body = await response.json();
      appendTurn("ai", body.prompt);
      updateStageStepper(body.stage);
      renderQuickReplies(body.suggested_replies);
    } catch (err) {
      renderTranscriptError("We couldn't reach the server just now.", () => postAnswer(answer));
    } finally {
      setInterviewPending(false);
    }
  }

  async function submitAnswer(text) {
    const answer = (text || "").trim();
    if (!answer) return;
    clearQuickReplies();
    appendTurn("resident", answer);
    if (interviewInput) interviewInput.value = "";
    await postAnswer(answer);
  }

  async function loadInterview() {
    setInterviewPending(true);
    try {
      const response = await fetch(`/api/cases/${caseId}/interview`);
      if (!response.ok) return;
      const body = await response.json();
      renderTranscript(body.turns);
      updateStageStepper(body.stage);
      renderQuickReplies(body.suggested_replies);
    } catch (err) {
      // A failed initial load leaves the transcript empty -- the resident
      // can still type once the form is visible; nothing to recover here
      // beyond letting the next real interaction retry naturally.
    } finally {
      setInterviewPending(false);
    }
  }

  if (interviewForm) {
    interviewForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const answer = interviewInput ? interviewInput.value : "";
      submitAnswer(answer);
    });
  }

  // ===========================================================================
  // Round-2 UI feedback, item 3: "upload button is spilling out of the chat
  // container... remove the 'Choose file / No file chosen' part. Make it a
  // one-liner: User answer input text | Send button | Upload button."
  //
  // The native `<input type="file">` (`#upload-input`) is visually hidden
  // (`.visually-hidden`, see console/app.py) -- `#upload-trigger` is the
  // styled button a resident actually sees and clicks, which just opens the
  // browser's own file picker; selecting a file uploads it immediately
  // (no separate "confirm" step), with feedback shown as a small chip
  // (`#upload-status-chip`) rather than the native input's own "Choose
  // file / No file chosen" text, which is never shown at all now.
  // ===========================================================================

  function setUploadStatus(message, isError) {
    if (!uploadStatusChip) return;
    if (!message) {
      uploadStatusChip.hidden = true;
      uploadStatusChip.textContent = "";
      uploadStatusChip.classList.remove("upload-chip--error");
      return;
    }
    uploadStatusChip.textContent = message;
    uploadStatusChip.hidden = false;
    uploadStatusChip.classList.toggle("upload-chip--error", !!isError);
  }

  if (uploadTriggerBtn && uploadInput) {
    uploadTriggerBtn.addEventListener("click", () => uploadInput.click());

    uploadInput.addEventListener("change", async () => {
      const file = uploadInput.files[0];
      if (!file) return;
      setUploadStatus(`Uploading ${file.name}…`, false);
      uploadTriggerBtn.disabled = true;
      const formData = new FormData();
      formData.append("file", file);
      try {
        const response = await fetch(`/api/cases/${caseId}/documents`, {
          method: "POST",
          body: formData,
        });
        if (response.ok) {
          // A non-conversational log line (UI-SPEC.md Sec 2.1's `--ai-
          // system` variant) -- an upload isn't something either party
          // "said", so it shouldn't render as a resident bubble.
          appendTurn("ai-system", `Evidence added: ${file.name}`);
          setUploadStatus(`${file.name} uploaded`, false);
          window.setTimeout(() => setUploadStatus(null), 2500);
          await loadInterview();
        } else {
          setUploadStatus("We couldn't upload that file just now.", true);
          renderTranscriptError("We couldn't upload that file just now.", null);
        }
      } catch (err) {
        setUploadStatus("We couldn't reach the server just now.", true);
        renderTranscriptError("We couldn't reach the server just now.", null);
      } finally {
        uploadTriggerBtn.disabled = false;
        uploadInput.value = "";
      }
    });
  }

  if (startTribunalBtn) {
    const idleLabel = startTribunalBtn.getAttribute("data-idle-label") || "Start tribunal";
    startTribunalBtn.addEventListener("click", async () => {
      startTribunalBtn.disabled = true;
      startTribunalBtn.textContent = "Tribunal running...";
      const existingError = document.getElementById("start-tribunal-error");
      if (existingError) existingError.remove();
      try {
        const response = await fetch(`/api/cases/${caseId}/tribunal`, { method: "POST" });
        if (!response.ok) {
          // A 429 (rate/concurrency/spend guard) or 5xx (e.g. the job
          // trigger itself failing) both leave the resident staring at a
          // button stuck on "Tribunal running..." with no way to tell
          // whether it is actually safe to retry -- surface the server's
          // own detail message and re-enable the button so they can.
          let detail = `Could not start the tribunal (status ${response.status}). Please try again shortly.`;
          try {
            const errorBody = await response.json();
            if (errorBody && errorBody.detail) detail = errorBody.detail;
          } catch (err) {
            // Non-JSON error body: keep the generic message above.
          }
          const errorEl = document.createElement("p");
          errorEl.id = "start-tribunal-error";
          errorEl.className = "create-case-error";
          errorEl.textContent = detail;
          startTribunalBtn.insertAdjacentElement("afterend", errorEl);
          startTribunalBtn.disabled = false;
          startTribunalBtn.textContent = idleLabel;
        }
      } catch (err) {
        // Network failure: same recovery as an HTTP error above.
        const errorEl = document.createElement("p");
        errorEl.id = "start-tribunal-error";
        errorEl.className = "create-case-error";
        errorEl.textContent = "Could not reach the server. Please check your connection and try again.";
        startTribunalBtn.insertAdjacentElement("afterend", errorEl);
        startTribunalBtn.disabled = false;
        startTribunalBtn.textContent = idleLabel;
      }
    });
  }

  // ===========================================================================
  // Wave 9 (LEO-FEEDBACK-UIUX.md §1/§2/§6): copy-link + copy-text, both via
  // the same small clipboard helper with a manual-select fallback for a
  // context where the async Clipboard API isn't available.
  // ===========================================================================

  async function copyTextToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }

  function flashButtonFeedback(button, message) {
    const original = button.textContent;
    button.textContent = message;
    window.setTimeout(() => {
      button.textContent = original;
    }, 2000);
  }

  const copyLinkBtn = document.getElementById("copy-link-button");
  if (copyLinkBtn) {
    copyLinkBtn.addEventListener("click", async () => {
      const path = copyLinkBtn.getAttribute("data-case-path") || window.location.pathname;
      try {
        await copyTextToClipboard(`${window.location.origin}${path}`);
        flashButtonFeedback(copyLinkBtn, "Copied!");
      } catch (err) {
        flashButtonFeedback(copyLinkBtn, "Could not copy");
      }
    });
  }

  // One delegated handler covers every "Copy text" button the submission
  // section renders (one per document) without needing to know how many
  // there are up front.
  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".copy-text-button");
    if (!button) return;
    const sourceId = button.getAttribute("data-copy-source");
    const sourceEl = sourceId && document.getElementById(sourceId);
    if (!sourceEl) return;
    try {
      await copyTextToClipboard(sourceEl.value);
      flashButtonFeedback(button, "Copied!");
    } catch (err) {
      flashButtonFeedback(button, "Could not copy");
    }
  });

  // ===========================================================================
  // Wave 9 (LEO-FEEDBACK-UIUX.md §5): a lightbox for the annotated overlay
  // image -- previously inert, now clickable to see it at full resolution
  // instead of squeezed into the doc-viewer's fixed-height stage.
  // ===========================================================================

  function ensureLightbox() {
    let lightbox = document.getElementById("setback-lightbox");
    if (lightbox) return lightbox;
    lightbox = document.createElement("div");
    lightbox.id = "setback-lightbox";
    lightbox.className = "lightbox";
    lightbox.hidden = true;
    lightbox.innerHTML =
      '<button type="button" class="lightbox__close" aria-label="Close full-size image">' +
      "&times;</button>" +
      '<img class="lightbox__image" alt="">';
    lightbox.addEventListener("click", (event) => {
      if (event.target === lightbox || event.target.classList.contains("lightbox__close")) {
        closeLightbox();
      }
    });
    document.body.appendChild(lightbox);
    return lightbox;
  }

  function openLightbox(src, alt) {
    const lightbox = ensureLightbox();
    const img = lightbox.querySelector(".lightbox__image");
    img.src = src;
    img.alt = alt || "";
    lightbox.hidden = false;
  }

  function closeLightbox() {
    const lightbox = document.getElementById("setback-lightbox");
    if (lightbox) lightbox.hidden = true;
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeLightbox();
  });

  function wireOverlayLightbox(img) {
    if (!img || img.dataset.lightboxWired) return;
    img.dataset.lightboxWired = "1";
    img.classList.add("doc-viewer__stage-image--clickable");
    // Wave 9 (LEO-FEEDBACK-UIUX.md §5): prefer the genuinely
    // full-resolution image (`data-full-res-src`, wired by
    // `console/app.py`'s `_render_annotated_overlay_item` /
    // `handleAnnotatedOverlay` below when `job.pipeline` stored one) over
    // `img.src`, which is only the shrunk copy embedded directly in the
    // event -- clicking used to just re-display that same downscaled
    // image bigger, not the real full-resolution render.
    img.addEventListener("click", () =>
      openLightbox(img.dataset.fullResSrc || img.src, img.alt)
    );
  }

  // The server-rendered overlay (a page reload/reopen after the run already
  // finished) needs wiring on load too, not just the live SSE-driven one.
  document
    .querySelectorAll(".doc-viewer__stage img")
    .forEach((img) => wireOverlayLightbox(img));

  // ===========================================================================
  // Sec 3.8 -- check-answers "Change" links: graceful degrade
  //
  // The interview state machine cannot reopen an arbitrary past stage this
  // wave (this wave's explicit design-judgment note) -- rather than fake a
  // per-row edit that doesn't work, a "Change" click scrolls to the
  // always-live typed-answer input and lets the resident say what to
  // change in their own words, which posts through the exact same
  // `submitAnswer` path as everything else (an appended correction turn,
  // not a reopened stage).
  // ===========================================================================

  document.addEventListener("click", (event) => {
    const changeLink = event.target.closest(".summary-list__change");
    if (!changeLink) return;
    event.preventDefault();
    if (!interviewInput) return;
    appendTurn(
      "ai-system",
      "No problem -- just tell us what you'd like to change below."
    );
    interviewInput.scrollIntoView({ behavior: "smooth", block: "center" });
    interviewInput.focus();
  });

  // ===========================================================================
  // Sec 2.5 -- citation chip behaviour
  //
  // One delegated handler for all three variants (`--clause`/`--doc`/
  // `--region`), reused wherever a `.citation-chip` appears (transcript,
  // ground cards, the final letter's rendered HTML) -- per the spec's
  // `citationChip.onActivate(chipEl)` contract.
  // ===========================================================================

  const citationChip = (function () {
    // Real statutory text, not invented -- copied from `gate/s415.py`'s
    // own quoted chapeau/heads so the popover says something true even
    // though this module cannot import Python. Any clause id outside this
    // small set (e.g. a specific LEP/DCP clause number) still gets a
    // correct, generic explanation rather than nothing.
    const CLAUSE_TEXT = {
      "s4.15(1)(a)": "The consent authority must consider the provisions of any relevant " +
        "environmental planning instrument, DCP, or planning agreement -- s4.15(1)(a) of the " +
        "Environmental Planning and Assessment Act 1979.",
      "s4.15(1)(b)": "The consent authority must consider 'the likely impacts of that " +
        "development, including environmental impacts on both the natural and built " +
        "environments, and social and economic impacts in the locality' -- s4.15(1)(b) of the " +
        "Environmental Planning and Assessment Act 1979.",
      "s4.15(1)(c)": "The consent authority must consider 'the suitability of the site for " +
        "the development' -- s4.15(1)(c) of the Environmental Planning and Assessment Act 1979.",
      "s4.15(1)(d)": "The consent authority must consider 'any submissions made in " +
        "accordance with this Act or the regulations' -- s4.15(1)(d) of the Environmental " +
        "Planning and Assessment Act 1979.",
      "s4.15(1)(e)": "The consent authority must consider 'the public interest' -- s4.15(1)(e) " +
        "of the Environmental Planning and Assessment Act 1979.",
    };
    const GENERIC_CLAUSE_TEXT = (id) =>
      `${id} is a clause of the Environmental Planning and Assessment Act 1979 or an ` +
      "applicable planning instrument that the consent authority must weigh in assessing " +
      "this development application.";

    let openPopover = null;

    function closePopover() {
      if (openPopover) {
        openPopover.remove();
        openPopover = null;
      }
    }

    function openClausePopover(chipEl) {
      closePopover();
      const clauseId = chipEl.getAttribute("data-clause") || chipEl.textContent.trim();
      const popover = document.createElement("div");
      popover.className = "citation-chip__popover";
      popover.setAttribute("role", "note");
      popover.textContent = CLAUSE_TEXT[clauseId] || GENERIC_CLAUSE_TEXT(clauseId);
      document.body.appendChild(popover);
      const rect = chipEl.getBoundingClientRect();
      popover.style.top = `${window.scrollY + rect.bottom + 6}px`;
      popover.style.left = `${window.scrollX + rect.left}px`;
      openPopover = popover;
    }

    // A real per-anchor bbox overlay layer (individually clickable/
    // highlightable regions in the DOM) isn't built this wave -- the
    // annotated-overlay event still renders one flattened raster image
    // server-side (`_render_annotated_overlay_item`). Degrade gracefully:
    // scroll to the best-matching image and flash its whole frame rather
    // than a precise sub-region, and upgrade automatically the moment a
    // `[data-bbox-region]` element matching this chip's doc/page exists.
    function findDocViewerImage(docId, page) {
      if (docId) {
        const byDoc = document.querySelector(
          `[data-doc-id="${CSS.escape(docId)}"] img, img[data-doc-id="${CSS.escape(docId)}"]`
        );
        if (byDoc) return byDoc;
      }
      // Fallback: the wave-4 overlay section always renders at most one
      // annotated image per document today, so the first (only) one on
      // the page is the correct target in the common single-document case.
      void page;
      return document.querySelector(".doc-viewer__stage img, .annotated-overlay img");
    }

    function flashElement(el) {
      if (!el) return;
      ensureVisibleInTabs(el);
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      const previousOutline = el.style.outline;
      const previousOffset = el.style.outlineOffset;
      el.style.outline = "3px solid var(--accent)";
      el.style.outlineOffset = "2px";
      window.setTimeout(() => {
        el.style.outline = previousOutline;
        el.style.outlineOffset = previousOffset;
      }, 1200);
    }

    function activateRegion(chipEl) {
      const docId = chipEl.getAttribute("data-doc-id");
      const page = chipEl.getAttribute("data-page");
      const bbox = chipEl.getAttribute("data-bbox");
      let region = null;
      if (docId && bbox) {
        region = document.querySelector(
          `[data-bbox-region][data-doc-id="${CSS.escape(docId)}"][data-bbox="${CSS.escape(bbox)}"]`
        );
      }
      flashElement(region || findDocViewerImage(docId, page));
    }

    function activateDoc(chipEl) {
      const docId = chipEl.getAttribute("data-doc-id");
      const page = chipEl.getAttribute("data-page");
      flashElement(findDocViewerImage(docId, page));
    }

    function onActivate(chipEl) {
      if (chipEl.classList.contains("citation-chip--clause")) {
        if (openPopover) {
          closePopover();
        } else {
          openClausePopover(chipEl);
        }
      } else if (chipEl.classList.contains("citation-chip--region")) {
        closePopover();
        activateRegion(chipEl);
      } else if (chipEl.classList.contains("citation-chip--doc")) {
        closePopover();
        activateDoc(chipEl);
      }
    }

    document.addEventListener("click", (event) => {
      const chip = event.target.closest(".citation-chip");
      if (chip) {
        onActivate(chip);
        return;
      }
      if (!event.target.closest(".citation-chip__popover")) {
        closePopover();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        const chip = event.target.closest(".citation-chip");
        if (chip) {
          event.preventDefault();
          onActivate(chip);
        }
      } else if (event.key === "Escape") {
        closePopover();
      }
    });

    return { onActivate };
  })();
  void citationChip; // exposed for the delegated listeners above only

  // ===========================================================================
  // Sec 2.7/2.8/2.10, Sec 3.5 -- the tribunal "courtroom sitting" timeline
  //
  // Built entirely from existing SSE events (no new backend events): the
  // three formerly-flat "Reviewer opinions"/"Adjudication"/"Gate decisions"
  // cards stay in the server-rendered page as a safety net (a case a
  // resident reopens *after* its tribunal run already finished this
  // session or a prior one still shows its full history there even if
  // this script never runs), and are hidden only once this richer view has
  // live data of its own to show in their place.
  // ===========================================================================

  const REVIEWER_LABELS = {
    clause_reviewer: "Clause Reviewer",
    evidence_reviewer: "Evidence Reviewer",
  };

  const CATEGORY_LABELS = {
    epi_dcp_provisions: "Planning instrument compliance",
    environmental_and_social_impacts: "Environmental & social impact",
    site_suitability: "Site suitability",
    public_submissions: "Public submissions",
    public_interest: "Public interest",
    property_value: "Property value",
    private_view_loss: "Loss of a private view",
  };

  const GATE_WORD = {
    shipped: "Shipped",
    "refused-irrelevant": "Refused",
    "refused-unsubstantiated": "Refused",
  };
  const GATE_BASIS = {
    "refused-irrelevant": "not a planning matter",
    "refused-unsubstantiated": "citation didn't check out",
  };

  const groundState = new Map(); // ground_id -> { claim, category, reviewers, adjudication, gate, rowEl }
  let tribunalRunEl = null;
  let tribunalTimelineEl = null;
  let flatSectionsHidden = false;

  function capitalize(word) {
    const text = String(word || "");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function lookupGroundClaim(groundId, fallback) {
    // Upgrades automatically once Package B's ground renderer stamps
    // `data-ground-id` onto each ground element (see this file's header
    // notes) -- today it falls back to the category label captured from
    // `ground_category_assigned`, which every confirmed concern already
    // emits during the interview.
    const el = document.querySelector(`[data-ground-id="${CSS.escape(groundId)}"]`);
    if (el) {
      const claimEl = el.querySelector(".ground-card__claim, .ground__claim");
      const text = (claimEl || el).textContent.trim();
      if (text) return text;
    }
    return fallback;
  }

  function getOrCreateGround(groundId) {
    let entry = groundState.get(groundId);
    if (!entry) {
      entry = {
        claim: lookupGroundClaim(groundId, `Ground ${groundId.replace(/^ground-/, "").slice(0, 8)}`),
        category: null,
        reviewers: {},
        adjudication: null,
        gate: null,
        rowEl: null,
      };
      groundState.set(groundId, entry);
    }
    return entry;
  }

  function ensureTribunalRun() {
    if (tribunalRunEl) return tribunalRunEl;
    // Lives inside the Grounds tabpanel (round-2 UI feedback, item 1) --
    // the live "courtroom sitting" timeline is about how the grounds are
    // being checked, so it belongs in the same tab as the grounds
    // themselves, not the page-level sections container at large.
    const anchor = document.getElementById("panel-grounds");
    if (!anchor) return null;
    tribunalRunEl = document.createElement("section");
    tribunalRunEl.className = "card tribunal-run";
    tribunalRunEl.innerHTML =
      '<h3>Tribunal sitting</h3>' +
      '<p class="tribunal-run__plan"></p>' +
      '<ul class="tribunal-timeline"></ul>';
    // Right after the grounds card when one is found -- keeps the reading
    // order the spec implies (grounds, then how they're being checked,
    // then the output docs); otherwise appended at the end is still a
    // correct, visible placement.
    const groundsSection = anchor.querySelector(".ground-list, .ground-card")?.closest("section");
    if (groundsSection && groundsSection.parentNode === anchor) {
      groundsSection.insertAdjacentElement("afterend", tribunalRunEl);
    } else {
      anchor.appendChild(tribunalRunEl);
    }
    tribunalTimelineEl = tribunalRunEl.querySelector(".tribunal-timeline");
    return tribunalRunEl;
  }

  function hideFlatSectionsOnce() {
    if (flatSectionsHidden) return;
    // Wave 9 (LEO-FEEDBACK-UIUX.md §3): "Reviewer opinions"/"Adjudication"/
    // "Gate decisions" no longer exist as standalone sections at all (they
    // render inside each ground's own accordion server-side now) -- only
    // "Annotated evidence overlay" still has a flat, server-rendered
    // fallback worth hiding once this richer live view has its own data to
    // show in its place. "Tribunal" is gone as a section entirely (round-2
    // UI feedback, item 4) -- nothing left here to hide for it.
    const titles = ["Annotated evidence overlay"];
    document.querySelectorAll(".case-layout__sections section.card").forEach((section) => {
      const heading = section.querySelector("h3");
      if (heading && titles.includes(heading.textContent.trim())) {
        section.style.display = "none";
      }
    });
    flatSectionsHidden = true;
  }

  function renderPlanLine() {
    if (!tribunalRunEl) return;
    const planEl = tribunalRunEl.querySelector(".tribunal-run__plan");
    if (!planEl) return;
    const count = groundState.size;
    const groundsText = count > 0 ? `${count} ground${count === 1 ? "" : "s"}` : "your grounds";
    let text =
      `Checking ${groundsText} against s4.15(1) · Clause Reviewer, Evidence Reviewer, ` +
      "Adjudicator on splits";
    const cost = formatCost(document.body.getAttribute("data-run-cost-usd"));
    if (cost) text += ` · This run: ${cost}`;
    planEl.textContent = text;
  }

  function reviewerCardMarkup(key, state, label) {
    return (
      `<div class="reviewer-card reviewer-card--${key} reviewer-card--${state}" data-reviewer="${key}">` +
      `<span class="reviewer-card__name">${escapeHtml(REVIEWER_LABELS[key] || capitalize(key))}</span>` +
      `<span class="reviewer-card__state">${escapeHtml(label)}</span>` +
      "</div>"
    );
  }

  function renderRowColumns(entry) {
    const clause = entry.reviewers.clause_reviewer;
    const evidence = entry.reviewers.evidence_reviewer;
    const clauseState = clause ? "done" : "active";
    const evidenceState = evidence ? "done" : "active";
    const clauseLabel = clause
      ? clause.voided
        ? "Opinion voided"
        : `${capitalize(clause.stance)} (confidence ${Number(clause.confidence).toFixed(2)})`
      : "Deliberating…";
    const evidenceLabel = evidence
      ? evidence.voided
        ? "Opinion voided"
        : `${capitalize(evidence.stance)} (confidence ${Number(evidence.confidence).toFixed(2)})`
      : "Deliberating…";
    const disagreeing =
      clause && evidence && !clause.voided && !evidence.voided && clause.stance !== evidence.stance;
    const adjudicatorState = entry.adjudication ? "done" : disagreeing ? "active" : "dim";
    const adjudicatorLabel = entry.adjudication
      ? "Ruled"
      : disagreeing
        ? "Deliberating…"
        : "Standing by";
    return (
      '<div class="tribunal-columns">' +
      reviewerCardMarkup("clause", clauseState, clauseLabel) +
      reviewerCardMarkup("adjudicator", adjudicatorState, adjudicatorLabel) +
      reviewerCardMarkup("evidence", evidenceState, evidenceLabel) +
      "</div>"
    );
  }

  function disruptionCardMarkup(entry) {
    const clause = entry.reviewers.clause_reviewer;
    const evidence = entry.reviewers.evidence_reviewer;
    return (
      '<div class="disruption-card">' +
      '<span class="disruption-card__eyebrow">Reviewers disagree</span>' +
      `<h4>${escapeHtml(entry.claim)}</h4>` +
      '<div class="disruption-card__opinions">' +
      `<p><strong>Clause Reviewer:</strong> ${escapeHtml(clause ? clause.rationale : "")}</p>` +
      `<p><strong>Evidence Reviewer:</strong> ${escapeHtml(evidence ? evidence.rationale : "")}</p>` +
      "</div>" +
      `<p class="disruption-card__ruling"><strong>Adjudicator's ruling:</strong> ` +
      `${escapeHtml(entry.adjudication ? entry.adjudication.rationale : "")}</p>` +
      "</div>"
    );
  }

  function verdictMarkup(status) {
    const modifier = escapeHtml(status);
    const word = GATE_WORD[status];
    if (word) {
      const basis = GATE_BASIS[status];
      return (
        `<div class="verdict-stamp verdict-stamp--${modifier} verdict-stamp--animating ` +
        'verdict-stamp--fade" role="status">' +
        `<span class="verdict-stamp__word">${escapeHtml(word)}</span>` +
        (basis ? `<span class="verdict-stamp__basis">${escapeHtml(basis)}</span>` : "") +
        "</div>"
      );
    }
    // `flagged` has no verdict-stamp colour token of its own (this wave's
    // 4-token status map reserves that visual weight for a genuine
    // ship/refuse outcome) -- the shared `.tag` component already covers
    // it correctly, so reuse that rather than mis-colouring a stamp.
    return `<span class="tag tag--flagged">Flagged</span>`;
  }

  function renderGroundRow(groundId) {
    const entry = getOrCreateGround(groundId);
    ensureTribunalRun();
    if (!tribunalTimelineEl) return;
    if (!entry.rowEl) {
      entry.rowEl = document.createElement("li");
      entry.rowEl.className = "tribunal-row";
      entry.rowEl.setAttribute("data-ground-id", groundId);
      tribunalTimelineEl.appendChild(entry.rowEl);
    }
    if (entry.gate) {
      entry.rowEl.className = "tribunal-row tribunal-row--collapsed";
      entry.rowEl.innerHTML =
        `<span class="tribunal-row__claim">${escapeHtml(entry.claim)}</span>` +
        verdictMarkup(entry.gate.status);
      return;
    }
    const clause = entry.reviewers.clause_reviewer;
    const evidence = entry.reviewers.evidence_reviewer;
    const disagreeing =
      clause && evidence && !clause.voided && !evidence.voided && clause.stance !== evidence.stance;
    entry.rowEl.className = "tribunal-row";
    let body = `<span class="tribunal-row__claim">${escapeHtml(entry.claim)}</span>`;
    if (disagreeing && entry.adjudication) {
      body += disruptionCardMarkup(entry);
    } else {
      body += renderRowColumns(entry);
    }
    entry.rowEl.innerHTML = body;
  }

  function handleGroundCategoryAssigned(payload) {
    const groundId = payload.ground_id;
    if (!groundId) return;
    const entry = getOrCreateGround(groundId);
    entry.category = payload.category;
    if (!lookupGroundClaim(groundId, null)) {
      entry.claim = CATEGORY_LABELS[payload.category] || "Objection ground";
    }
  }

  function handleTribunalRequested() {
    ensureTribunalRun();
    hideFlatSectionsOnce();
    renderPlanLine();
    for (const groundId of groundState.keys()) {
      renderGroundRow(groundId);
    }
  }

  function handleReviewVerdict(payload) {
    const groundId = payload.ground_id;
    if (!groundId) return;
    const entry = getOrCreateGround(groundId);
    entry.reviewers[payload.reviewer] = payload;
    renderGroundRow(groundId);
    renderPlanLine();
  }

  function handleAdjudicationDecision(payload) {
    const groundId = payload.ground_id;
    if (!groundId) return;
    const entry = getOrCreateGround(groundId);
    entry.adjudication = payload;
    renderGroundRow(groundId);
  }

  function handleGateDecision(payload) {
    const groundId = payload.ground_id;
    if (!groundId) return;
    const entry = getOrCreateGround(groundId);
    entry.gate = payload;
    renderGroundRow(groundId);
    renderPlanLine();
  }

  function handleAnnotatedOverlay(payload) {
    // The concurrent wave's semantic-overlay pixels; this wave only adds
    // the docked chrome (Sec 2.12) around whatever image the event
    // carries. The four legend items below (order, CSS class suffix, and
    // copy) must stay in lockstep with `evidence.overlays.OverlayRole` /
    // `ROLE_CSS_CLASS_SUFFIX` / `ROLE_LEGEND_TEXT` -- the Python side's
    // single source of truth -- and with `console/app.py`'s
    // `_render_annotated_overlay_item`, which builds the same
    // `.doc-viewer__legend` server-side so a page reload never loses it.
    // A prior version of this legend only listed three items (shipped/
    // flagged/refused), omitting the fourth "not yet decided" swatch even
    // though `style.css` already shipped `.legend-swatch--pending` for it
    // -- every neutral (not-yet-cited) box rendered in that colour with no
    // legend entry explaining it at all, reported live as "empty floating
    // boxes" with no visible meaning.
    hideFlatSectionsOnce();
    // Lives inside the Overlay tabpanel (round-2 UI feedback, item 1),
    // not the page-level sections container -- a live overlay event on a
    // case with no prior server-rendered overlay must still land in the
    // right tab, not float outside the tab structure entirely.
    const anchor = document.getElementById("panel-overlay");
    if (!anchor) return;
    let viewer = document.querySelector(".doc-viewer");
    if (!viewer) {
      viewer = document.createElement("section");
      viewer.className = "card";
      viewer.innerHTML =
        '<h3>Annotated evidence overlay</h3>' +
        '<div class="doc-viewer"><div class="doc-viewer__stage"></div>' +
        '<div class="doc-viewer__legend">' +
        '<span class="legend-item"><i class="legend-swatch legend-swatch--shipped"></i>Supports a shipped ground</span>' +
        '<span class="legend-item"><i class="legend-swatch legend-swatch--flagged"></i>Needs more evidence</span>' +
        '<span class="legend-item"><i class="legend-swatch legend-swatch--refused"></i>Cited in a refused ground</span>' +
        '<span class="legend-item"><i class="legend-swatch legend-swatch--pending"></i>Evidence anchor, not yet decided</span>' +
        "</div></div>";
      anchor.appendChild(viewer);
    }
    const stage = viewer.querySelector(".doc-viewer__stage");
    if (!stage) return;
    const img = document.createElement("img");
    img.src = `data:${payload.mime_type || "image/png"};base64,${payload.image_base64 || ""}`;
    img.alt = "Annotated evidence overlay";
    if (payload.document_id) img.setAttribute("data-doc-id", payload.document_id);
    // Wave 9 (LEO-FEEDBACK-UIUX.md §5): mirrors `console/app.py`'s
    // `_render_annotated_overlay_item` -- see `wireOverlayLightbox` for
    // why the lightbox prefers this over `img.src`.
    if (payload.full_res_document_id) {
      img.dataset.fullResSrc = `/api/cases/${caseId}/documents/${payload.full_res_document_id}`;
    }
    stage.innerHTML = "";
    stage.appendChild(img);
    wireOverlayLightbox(img);
  }

  // ===========================================================================
  // SSE event stream
  // ===========================================================================

  // Event types this page already reflects itself the moment it makes the
  // request that produced them (an interview answer, an upload), or that
  // the tribunal-timeline handlers above now render live in place --
  // reload is reserved for events with no client-side renderer at all
  // (the composed submission, refusal feedback), where a full reload of
  // Package B's server-rendered section is still the simplest correct way
  // to reflect them.
  const LOCALLY_HANDLED_EVENT_TYPES = new Set([
    "interview_turn",
    "document_uploaded",
    "ground_category_assigned",
    "tribunal_requested",
    "review_verdict",
    "adjudication_decision",
    "gate_decision",
    "annotated_overlay",
  ]);

  const EVENT_HANDLERS = {
    ground_category_assigned: handleGroundCategoryAssigned,
    tribunal_requested: handleTribunalRequested,
    review_verdict: handleReviewVerdict,
    adjudication_decision: handleAdjudicationDecision,
    gate_decision: handleGateDecision,
    annotated_overlay: handleAnnotatedOverlay,
  };

  function connectEventStream() {
    // `data-last-sequence` (rendered server-side, see console/app.py's
    // render_case_page) is the sequence number of the newest event this
    // very page load already reflects -- passed back as `after` so a
    // fresh SSE connection doesn't replay the case's whole history and
    // treat it as new, which would reload the page in an infinite loop.
    const afterSequence = document.body.getAttribute("data-last-sequence") || "-1";
    const source = new EventSource(`/api/cases/${caseId}/events?after=${afterSequence}`);
    source.onmessage = (message) => {
      let parsed = null;
      try {
        parsed = JSON.parse(message.data);
      } catch (err) {
        // Malformed payload: fall through and reload defensively.
      }
      const eventType = parsed && parsed.event_type;
      if (eventType && LOCALLY_HANDLED_EVENT_TYPES.has(eventType)) {
        const handler = EVENT_HANDLERS[eventType];
        if (handler) handler(parsed.payload || {});
        return;
      }
      // Any other new case event may have changed a server-rendered
      // section (the submission, refusal feedback) -- reloading is the
      // simplest correct way to reflect that without duplicating
      // setback.console.app's rendering logic in JS.
      window.location.reload();
    };
    source.onerror = () => {
      source.close();
    };
  }

  loadInterview();
  connectEventStream();
})();
