import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { StatusBadge } from './StatusBadge';
import type { OperationStatus } from '../api/ai';

describe('StatusBadge (Req 11.5: status never relies on color alone)', () => {
  const cases: [OperationStatus, string][] = [
    ['pending', 'Status: Pending'],
    ['successful', 'Status: Successful'],
    ['failed', 'Status: Failed'],
    ['uncertain', 'Status: Uncertain'],
  ];
  it.each(cases)('%s is conveyed by a text label AND a distinct glyph', (status, text) => {
    const { container, unmount } = render(<StatusBadge status={status} />);
    expect(container).toHaveTextContent(text);
    const glyph = container.querySelector('[aria-hidden="true"]')?.textContent ?? '';
    expect(glyph.trim().length).toBeGreaterThan(0);
    unmount();
  });

  it('uses four different glyphs so states are distinguishable without color', () => {
    const glyphs = cases.map(([s]) => {
      const { container, unmount } = render(<StatusBadge status={s} />);
      const g = container.querySelector('[aria-hidden="true"]')?.textContent ?? '';
      unmount();
      return g;
    });
    expect(new Set(glyphs).size).toBe(4);
  });
});
