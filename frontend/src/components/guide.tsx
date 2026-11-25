"use client";

import { Lightbulb, X } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

/**
 * A four-step, non-modal guided tour. Each step renders inline next to the
 * thing it explains (no overlays, no focus traps). Dismissible at any point;
 * the choice is remembered in this browser.
 */
const STORAGE_KEY = "conductor-guide-done";

export const GUIDE_STEPS = [
  {
    id: "workflow",
    title: "This is a workflow",
    body: "Six steps, each run by whichever worker is free. Two of them run in parallel. It's all coordinated through PostgreSQL alone.",
  },
  {
    id: "run",
    title: "Run it",
    body: "Press “Run the pipeline”. Each step lights up as a worker claims it, then turns green when it finishes.",
  },
  {
    id: "chaos",
    title: "Now break something",
    body: "Kill a worker while it's busy. Its claim on the task expires, and another worker picks it up. Nothing is lost, and nothing happens twice.",
  },
  {
    id: "story",
    title: "Read the story",
    body: "This feed explains every event in plain language, including the recovery after you kill a worker.",
  },
] as const;

type StepId = (typeof GUIDE_STEPS)[number]["id"];

interface GuideState {
  current: StepId | null;
  next: () => void;
  dismiss: () => void;
  restart: () => void;
}

const GuideContext = createContext<GuideState>({
  current: null,
  next: () => {},
  dismiss: () => {},
  restart: () => {},
});

function setDocumentGuide(state: "done" | "todo") {
  document.documentElement.dataset.guide = state;
}

export function GuideProvider({ children }: { children: ReactNode }) {
  // Starts on step 1 so the server renders it; the pre-paint script hides it
  // (via data-guide on <html>) for visitors who already dismissed the tour.
  const [index, setIndex] = useState<number | null>(0);

  useEffect(() => {
    let done = false;
    try {
      done = localStorage.getItem(STORAGE_KEY) === "1";
    } catch {
      // storage unavailable: show the guide
    }
    if (done) {
      // Already hidden by CSS before paint; this just syncs React's state.
      const id = setTimeout(() => setIndex(null), 0);
      return () => clearTimeout(id);
    }
  }, []);

  const finish = useCallback(() => {
    setIndex(null);
    setDocumentGuide("done");
    try {
      localStorage.setItem(STORAGE_KEY, "1");
    } catch {
      // ignore
    }
  }, []);

  const value: GuideState = {
    current: index === null ? null : GUIDE_STEPS[index].id,
    next: () => (index !== null && index < GUIDE_STEPS.length - 1 ? setIndex(index + 1) : finish()),
    dismiss: finish,
    restart: () => {
      setDocumentGuide("todo");
      setIndex(0);
    },
  };
  return <GuideContext.Provider value={value}>{children}</GuideContext.Provider>;
}

export const useGuide = () => useContext(GuideContext);

export function GuideStep({ id }: { id: StepId }) {
  const { current, next, dismiss } = useGuide();
  const index = GUIDE_STEPS.findIndex((s) => s.id === id);
  if (current !== id) return null;
  const step = GUIDE_STEPS[index];
  const last = index === GUIDE_STEPS.length - 1;
  return (
    <div
      role="region"
      data-guide-step=""
      aria-label={`Guide, step ${index + 1} of ${GUIDE_STEPS.length}`}
      className="mb-3 flex items-start gap-3 rounded-xl border p-3 text-sm"
      style={{ borderColor: "var(--accent)", backgroundColor: "var(--accent-soft)" }}
      onKeyDown={(e) => e.key === "Escape" && dismiss()}
    >
      <Lightbulb aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-accent" />
      <div className="flex-1">
        <p className="font-semibold">
          <span className="sr-only">Tip: </span>
          {step.title}
          <span className="ml-2 text-xs font-normal text-muted">
            {index + 1} / {GUIDE_STEPS.length}
          </span>
        </p>
        <p className="mt-1 text-muted">{step.body}</p>
        <div className="mt-2 flex gap-2">
          <button type="button" className="btn-primary px-2.5 py-1 text-xs" onClick={next}>
            {last ? "Got it" : "Next tip"}
          </button>
          {!last ? (
            <button type="button" className="btn-secondary px-2.5 py-1 text-xs" onClick={dismiss}>
              Skip the tour
            </button>
          ) : null}
        </div>
      </div>
      <button
        type="button"
        onClick={dismiss}
        className="rounded p-0.5 text-muted hover:text-text"
        aria-label="Close the guide"
      >
        <X aria-hidden="true" className="size-4" />
      </button>
    </div>
  );
}
