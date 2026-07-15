import type { TestInfo } from "@playwright/test";

export type StepEvent = {
  step_name: string;
  step_type: "action" | "assertion" | "navigation";
  status: "PASSED" | "FAILED" | "HEALED";
  failed_selector?: string;
  healed_selector?: string;
  healing_confidence?: number | null;
  message?: string;
  timestamp: string;
};

type FailureContext = {
  failedSelector?: string;
  failureReason?: string;
  selectorType?: string;
  pageUrl?: string;
  healingAttempted?: boolean;
  healingOutcome?: "NOT_ATTEMPTED" | "SUCCESS" | "FAILED";
  healedSelector?: string;
  healingConfidence?: number | null;
  validationStatus?: string;
  uiChangeLevel?: string;
  historyAssisted?: boolean;
  historyHits?: number;
  cacheHit?: boolean;
  cacheFallbackToFresh?: boolean;
  rootCause?: string;
  stepEvents?: StepEvent[];
};

const contextByTestId = new Map<string, FailureContext>();

const UI_CHANGE_PRIORITY: Record<string, number> = {
  UNKNOWN: 0,
  UNCHANGED: 1,
  MINOR_CHANGE: 2,
  MAJOR_CHANGE: 3,
  ELEMENT_REMOVED: 4,
};

function pickMostSevereUiChange(previous?: string, incoming?: string): string | undefined {
  const prev = String(previous || "").toUpperCase();
  const next = String(incoming || "").toUpperCase();
  if (!prev && !next) return undefined;
  if (!prev) return next || undefined;
  if (!next) return prev || undefined;
  const prevRank = UI_CHANGE_PRIORITY[prev] ?? 0;
  const nextRank = UI_CHANGE_PRIORITY[next] ?? 0;
  return nextRank >= prevRank ? next : prev;
}

function getContextKey(testInfo?: TestInfo): string | undefined {
  if (!testInfo) return undefined;
  return testInfo.testId || testInfo.title;
}

export function setFailureContext(testInfo: TestInfo | undefined, context: FailureContext) {
  const contextKey = getContextKey(testInfo);
  if (!contextKey) return;
  const previous = contextByTestId.get(contextKey) || {};
  contextByTestId.set(contextKey, {
    ...previous,
    ...context,
    uiChangeLevel: pickMostSevereUiChange(previous.uiChangeLevel, context.uiChangeLevel),
    stepEvents: context.stepEvents ?? previous.stepEvents ?? [],
  });
}

export function addStepEvent(testInfo: TestInfo | undefined, event: Omit<StepEvent, "timestamp">) {
  const contextKey = getContextKey(testInfo);
  if (!contextKey) return;
  const previous = contextByTestId.get(contextKey) || {};
  const existingEvents = previous.stepEvents || [];
  contextByTestId.set(contextKey, {
    ...previous,
    stepEvents: [
      ...existingEvents,
      {
        ...event,
        timestamp: new Date().toISOString(),
      },
    ],
  });
}

export function getFailureContext(testInfo: TestInfo | undefined): FailureContext | undefined {
  const contextKey = getContextKey(testInfo);
  if (!contextKey) return undefined;
  return contextByTestId.get(contextKey);
}

export function clearFailureContext(testInfo: TestInfo | undefined) {
  const contextKey = getContextKey(testInfo);
  if (!contextKey) return;
  contextByTestId.delete(contextKey);
}

export function parseFailureFromError(errorMessage?: string): FailureContext {
  if (!errorMessage) return {};

  const locatorMatch = errorMessage.match(/Locator:\s*([^\n]+)/i);
  const failedSelector = locatorMatch?.[1]?.trim();

  return {
    failedSelector,
    failureReason: errorMessage.split("\n")[0]?.trim() || "assertion_failed",
  };
}
