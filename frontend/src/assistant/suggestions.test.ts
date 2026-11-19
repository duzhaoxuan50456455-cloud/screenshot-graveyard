import { describe, it, expect } from 'vitest';
import type { Screenshot } from '../api/client';
import { deriveSuggestions, pickPrimary, type ConsentStatus } from './suggestions';

const shot = (id: number, over: Partial<Screenshot> = {}): Screenshot => ({
  screenshotId: id, state: 'active', mimeType: 'image/png', originalFilename: `s${id}.png`,
  isDuplicate: false, duplicateOf: null, byteSize: 1, uploadedAt: '2026-01-01T00:00:00+00:00',
  analysis: null, ...over,
});
const analysed = (id: number, uncertain = false) =>
  shot(id, { analysis: { category: 'Notes', uncertain, epitaph: 'e', epitaphOrigin: 'ai_generate' } });

describe('deriveSuggestions', () => {
  it('is idle with no selection and has no action', () => {
    const [s] = deriveSuggestions({ selected: [], consent: {}, processing: false });
    expect(s.kind).toBe('idle');
    expect(s.acceptLabel).toBeNull();
  });

  it('reports processing state only', () => {
    const all = deriveSuggestions({ selected: [shot(1)], consent: { 1: 'granted' }, processing: true });
    expect(all.map((s) => s.kind)).toEqual(['processing']);
  });

  it('asks for consent first when a ghost lacks it, and never offers classify', () => {
    const all = deriveSuggestions({ selected: [shot(1), shot(2)], consent: { 1: 'granted', 2: 'not_granted' }, processing: false });
    expect(all[0].kind).toBe('consent_needed');
    expect(all[0].targetIds).toEqual([2]);
    expect(all.some((s) => s.kind === 'classify')).toBe(false);
  });

  it('treats a failed consent lookup as not granted, with its own wording', () => {
    const failed = deriveSuggestions({ selected: [shot(1)], consent: { 1: 'lookup_failed' }, processing: false });
    expect(failed[0].kind).toBe('consent_needed');
    expect(failed[0].text).toMatch(/Could not check consent/);
    const revoked = deriveSuggestions({ selected: [shot(1)], consent: { 1: 'not_granted' }, processing: false });
    expect(revoked[0].text).toMatch(/still needs your consent/);
    expect(revoked[0].text).not.toMatch(/Could not check/);
  });

  it('shows a checking state with no action while consent is being read', () => {
    for (const consent of [{ 1: 'checking' }, {}] as Array<Record<number, ConsentStatus>>) {
      const all = deriveSuggestions({ selected: [shot(1)], consent, processing: false });
      expect(all[0].kind).toBe('consent_needed');
      expect(all[0].text).toMatch(/Checking consent/);
      expect(all[0].acceptLabel).toBeNull();
    }
  });

  it('offers classify when all ghosts have consent, then a study note', () => {
    const all = deriveSuggestions({ selected: [shot(1), shot(2)], consent: { 1: 'granted', 2: 'granted' }, processing: false });
    expect(all.map((s) => s.kind)).toEqual(['classify', 'study_note']);
    expect(all[0].targetIds).toEqual([1, 2]);
  });

  it('keeps the user selection order in study note targets', () => {
    const all = deriveSuggestions({ selected: [analysed(3), analysed(1)], consent: {}, processing: false });
    expect(all[0].kind).toBe('study_note');
    expect(all[0].targetIds).toEqual([3, 1]);
  });

  it('labels only classifier-derived suggestions as AI', () => {
    const all = deriveSuggestions({ selected: [analysed(1, true)], consent: {}, processing: false });
    const bySource = Object.fromEntries(all.map((s) => [s.kind, s.source]));
    expect(bySource).toEqual({ review_uncertain: 'ai', study_note: 'rules' });
    expect(deriveSuggestions({ selected: [shot(1)], consent: { 1: 'granted' }, processing: false }).every((s) => s.source === 'rules')).toBe(true);
  });

  it('does not suggest analysis for archived screenshots', () => {
    const all = deriveSuggestions({ selected: [shot(1, { state: 'archived' })], consent: {}, processing: false });
    expect(all.map((s) => s.kind)).toEqual(['study_note']);
  });

  it('blocks study note over 12 selections', () => {
    const many = Array.from({ length: 13 }, (_, i) => analysed(i + 1));
    const all = deriveSuggestions({ selected: many, consent: {}, processing: false });
    expect(all[0].kind).toBe('too_many');
    expect(all[0].acceptLabel).toBeNull();
  });
});

describe('pickPrimary', () => {
  const all = deriveSuggestions({ selected: [shot(1)], consent: { 1: 'granted' }, processing: false });
  it('returns the first, skips dismissed, and honours skip', () => {
    expect(pickPrimary(all, new Set(), 0)?.kind).toBe('classify');
    expect(pickPrimary(all, new Set(), 1)?.kind).toBe('study_note');
    expect(pickPrimary(all, new Set([all[0].key]), 0)?.kind).toBe('study_note');
    expect(pickPrimary(all, new Set(all.map((s) => s.key)), 0)).toBeNull();
    expect(pickPrimary(all, new Set(), 5)).toBeNull();
  });
});
