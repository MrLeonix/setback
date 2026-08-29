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
(function () {
  "use strict";

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

  const caseId = document.body.getAttribute("data-case-id");
  if (!caseId) {
    return;
  }

  const transcriptEl = document.getElementById("interview-transcript");
  const interviewForm = document.getElementById("interview-form");
  const interviewInput = document.getElementById("interview-input");
  const uploadForm = document.getElementById("upload-form");
  const uploadInput = document.getElementById("upload-input");
  const startTribunalBtn = document.getElementById("start-tribunal");

  function appendTurn(role, text) {
    if (!transcriptEl) return;
    const turn = document.createElement("div");
    turn.className = "chat-turn chat-turn--" + role;
    turn.textContent = text;
    transcriptEl.appendChild(turn);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  function renderTranscript(turns) {
    if (!transcriptEl) return;
    transcriptEl.innerHTML = "";
    for (const turn of turns) {
      appendTurn("system", turn.prompt);
    }
  }

  async function loadInterview() {
    const response = await fetch(`/api/cases/${caseId}/interview`);
    if (!response.ok) return;
    const body = await response.json();
    renderTranscript(body.turns);
  }

  if (interviewForm) {
    interviewForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const answer = interviewInput.value.trim();
      if (!answer) return;
      appendTurn("resident", answer);
      interviewInput.value = "";
      const response = await fetch(`/api/cases/${caseId}/interview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer }),
      });
      if (response.ok) {
        const body = await response.json();
        appendTurn("system", body.prompt);
      }
    });
  }

  if (uploadForm) {
    uploadForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const file = uploadInput.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append("file", file);
      const response = await fetch(`/api/cases/${caseId}/documents`, {
        method: "POST",
        body: formData,
      });
      if (response.ok) {
        appendTurn("resident", `[uploaded ${file.name}]`);
        await loadInterview();
      }
      uploadInput.value = "";
    });
  }

  if (startTribunalBtn) {
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
          startTribunalBtn.textContent = "Start tribunal";
        }
      } catch (err) {
        // Network failure: same recovery as an HTTP error above.
        const errorEl = document.createElement("p");
        errorEl.id = "start-tribunal-error";
        errorEl.className = "create-case-error";
        errorEl.textContent = "Could not reach the server. Please check your connection and try again.";
        startTribunalBtn.insertAdjacentElement("afterend", errorEl);
        startTribunalBtn.disabled = false;
        startTribunalBtn.textContent = "Start tribunal";
      }
    });
  }

  // Event types this page already reflects itself the moment it makes the
  // request that produced them (an interview answer, an upload) -- reload
  // is only useful for events the *background tribunal job* produces
  // asynchronously, which this tab has no other way to learn about.
  const LOCALLY_HANDLED_EVENT_TYPES = new Set(["interview_turn", "document_uploaded"]);

  function connectEventStream() {
    // `data-last-sequence` (rendered server-side, see console/app.py's
    // render_case_page) is the sequence number of the newest event this
    // very page load already reflects -- passed back as `after` so a
    // fresh SSE connection doesn't replay the case's whole history and
    // treat it as new, which would reload the page in an infinite loop.
    const afterSequence = document.body.getAttribute("data-last-sequence") || "-1";
    const source = new EventSource(`/api/cases/${caseId}/events?after=${afterSequence}`);
    source.onmessage = (message) => {
      let eventType = null;
      try {
        eventType = JSON.parse(message.data).event_type;
      } catch (err) {
        // Malformed payload: fall through and reload defensively.
      }
      if (eventType && LOCALLY_HANDLED_EVENT_TYPES.has(eventType)) {
        return;
      }
      // Any other new case event may have changed a server-rendered
      // section (reviewer opinions, gate decisions, the submission) --
      // reloading is the simplest correct way to reflect that without
      // duplicating setback.console.app's rendering logic in JS.
      window.location.reload();
    };
    source.onerror = () => {
      source.close();
    };
  }

  loadInterview();
  connectEventStream();
})();
