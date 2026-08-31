# Testing Setback

**Note for judges:** the key mentioned below (docket board, live Veo generation) is in the Devpost testing-instructions field for this submission — it doesn't live in this file.

**Live app:** https://setback-console-956646636969.australia-southeast1.run.app

That link lands on the public landing page. No passphrase, no login, just Setback's name, a one-line tagline, and one field: "DA number, e.g. DA2026/0359." That field is how you create a case. There's no separate signup step.

## One already-completed example

If you'd rather look before you drive, there's a real, live-fetched DA with the tribunal already complete: https://setback-console-956646636969.australia-southeast1.run.app/cases/1f4b7367fd30c089173ef09d7e8383a4 — a real, currently-exhibited Georges River Council DA (portal reference PAN-661190, 65A Vista Street, Sans Souci), fetched live from the government's own register, not a canned fixture. Shows a clean SHIPPED overshadowing ground (s4.15(1)(b), both reviewers in agreement), a REFUSED property-value ground with the strong "not a matter listed in s4.15(1)" wording, a Street View fallback card since no resident photo was uploaded, and a real annotated overlay on the DA's own Site Plan drawing — stroke-only green boxes with compact chip labels (a couple truncate with an ellipsis at normal zoom, a known and accepted limitation of the current renderer), click it for the full-resolution version.

This case's Evidence tab also carries a Veo-generated illustration card: a short video, generated once ahead of time from the DA's own elevation drawing, simulating the overshadowing the shipped ground describes, labelled on the card itself: "Pre-generated with Veo 3.1 · one-time cost US$1.60 · not part of this case's run cost." It can't be cited or graded by the tribunal — it's an illustration of the claim, not a source for it.

This page: Grounds, Evidence, Overlay, and Documents are separate tabs. In the Grounds tab, each ground is a one-line accordion (claim + status) that expands into the full reviewer opinions and the legal-relevance decision merged together. A ground that didn't qualify reads "We didn't include: ..." with a plain-English reason, not a silent drop. Photo evidence and the annotated overlay are clickable and open a full-resolution lightbox; PDF documents open in a new browser tab instead, since a lightbox can't render a PDF. The header's light/dark toggle works everywhere (`?theme=light` / `?theme=dark` also force it, if you'd rather not rely on your system setting). The whole thing is phone-friendly too, worth trying on an actual phone or a narrow browser window: it switches to a single stacked column, no horizontal scroll, full-size tap targets.

## Run your own case (2-4 minutes, real Gemini/Gemma calls on Vertex AI)

1. On the landing page, type a DA number and submit. `PAN-661190` resolves against real, live council data (the same DA the example case above uses). Any other number falls back to a clearly-labelled demo mode instead of failing, so you can try the flow with whatever you type.
2. You'll land in an interview. Answer in plain language, as a resident would, e.g. "the new building next door will block sunlight to my backyard for most of the afternoon." The system asks clarifying questions and may invite you to upload a supporting photo or document (any image or PDF works for a test run).
3. Try raising more than one concern in the same interview: mix something the system can act on (a shadow/overshadowing or privacy concern) with something it genuinely can't ("I don't like the applicant" or "it will hurt my property value" work well, kept as their own plain sentence, separate from any "bulky"/"overbearing" language, which the live model can sometimes read as a different, evidence-shaped concern instead). This is the flow that shows the refusal behaviour actually working, not failing.
4. Once the interview reaches its closing message, click "Start tribunal" on the case page. That triggers a real Cloud Run Job execution: a small panel of AI reviewers checks each of your grounds against the actual evidence and the relevant statutory list. Usually takes under a minute and costs a few cents, visible on the case page's own running-cost total once it's done.
5. Refresh (or watch the live-updating timeline) to see the result. Each ground either ships with a real legal citation and, where photo or plan evidence was involved, an annotated overlay showing what the AI actually looked at, or it's refused with a specific, plain-English explanation of why it doesn't qualify and what would.
6. The composed submission, and the refusal explainer where applicable, are both viewable inline on the case page. "Copy text" and "Email this" are the fastest ways to take them with you (an HTML download is also there if you want it).

## The docket board

The docket board, the list of every case, lives at `https://setback-console-956646636969.australia-southeast1.run.app/docket`. Add `?key=` followed by the key from the Devpost testing-instructions field to get in — that gate only protects the board listing itself. Individual case pages, including the example above, never need it.

Opening that link with the key does two more things worth knowing. It unlocks unrestricted usage if the public demo has reached its budget cap. And it marks your browser session as privileged, which is what lets you see Veo actually run live, next.

## See Veo generate something live

Open the docket link with the key from the Devpost testing-instructions field first, then run the same "create your own case" flow from step 1 above, but raise an overshadowing-shaped concern this time — something like "the new second storey will cast a shadow over most of my backyard in the afternoon." Once that ground ships in step 5 (a real SHIPPED overshadowing ground, from your session, not a canned one), the case page starts a genuinely new `veo-3.1-generate-001` generation in the background, conditioned on that DA's own elevation drawing — nothing pre-baked, nothing reused. It takes a couple of minutes (up to 6 as a hard ceiling); the case page shows a plain "being generated, refresh in a bit" placeholder until it's ready, then the same "AI-generated illustration — not evidence" card as the canonical example, with its own line: "Generated live with Veo 3.1 · US$1.60 · not part of this case's run cost." This path is deliberately capped at a small, fixed number of real generations for the whole hackathon, so it's there for judges to actually see, not for open-ended replay — the case you tried without the key never triggers it at all, on purpose, at any budget level.

## Disagree with a refusal

Worth trying on any refused ground: every refusal card (the "we didn't include this, here's why" panel) has a "Disagree with this refusal? Tell the tribunal." toggle. Open it, write a real pushback in your own words, and submit — Setback makes one more real model call and writes back a short, honest response acknowledging what you said, without ever pretending the refusal itself might flip. It's there so a resident's disagreement is heard and answered, not just recorded as a support ticket nobody reads.

## One honest note

This is a real deployed system making real model calls, not a scripted demo. Your result depends on what you actually type and upload, so don't expect it to match any screenshot in the gallery exactly. Interview state is best-effort session-affinity-routed to one Cloud Run instance, so if you reload a case mid-interview shortly after a redeploy, you might occasionally see a duplicated opening turn. That's a known, documented limitation, not a sign anything's broken.
