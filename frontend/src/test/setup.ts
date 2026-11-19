import '@testing-library/jest-dom/vitest';
import { beforeEach } from 'vitest';

// The Revive registry is module-level state; start every test with it empty.
// Imported lazily so a test file's vi.mock of '../api/ai' applies to it.
beforeEach(async () => {
  const { resetReviveCoordinator } = await import('../ai/reviveCoordinator');
  resetReviveCoordinator();
});
