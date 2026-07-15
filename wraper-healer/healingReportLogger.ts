import type { TestInfo } from "@playwright/test";

const logsByTestId = new Map<string, string[]>();

function getTestKey(testInfo?: TestInfo): string | undefined {
  if (!testInfo) return undefined;
  return testInfo.testId || testInfo.title;
}

export function appendHealingReportLog(testInfo: TestInfo | undefined, message: string): void {
  const key = getTestKey(testInfo);
  if (!key) return;
  const existing = logsByTestId.get(key) || [];
  existing.push(message);
  logsByTestId.set(key, existing);
}

export function consumeHealingReportLogs(testInfo: TestInfo | undefined): string[] {
  const key = getTestKey(testInfo);
  if (!key) return [];
  const logs = logsByTestId.get(key) || [];
  logsByTestId.delete(key);
  return logs;
}

