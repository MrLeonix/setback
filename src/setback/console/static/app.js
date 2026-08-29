// Setback console -- vanilla-JS client for the case page.
//
// Deliberately dependency-free: this is a small, judge-readable script
// wiring three things against console/app.py's JSON API --
//   1. the interview chat (GET/POST /api/cases/{id}/interview)
//   2. photo/document upload (POST /api/cases/{id}/documents)
//   3. the SSE event stream (GET /api/cases/{id}/events), which reloads
//      the page's server-rendered sections whenever a new event lands, so
//      reviewer opinions / gate decisions / the submission appear live as
//      the tribunal job progresses, with no client-side re-implementation
//      of the rendering setback.console.app already does server-side.
(function () {
  "use strict";

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
      await fetch(`/api/cases/${caseId}/tribunal`, { method: "POST" });
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
