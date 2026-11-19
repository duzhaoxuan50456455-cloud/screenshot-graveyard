import type { Screenshot } from '../api/client';

/**
 * Quiet Assistant suggestion rules.
 *
 * These are deterministic UI rules over real, already-loaded state. They do NOT
 * infer user intent. A suggestion is labelled 'ai' only when its content comes
 * from a backend classification result (for example the uncertain flag the
 * classifier returned). Everything else is labelled 'rules'.
 */

export type SuggestionSource = 'rules' | 'ai';

export type SuggestionKind =
  | 'idle'
  | 'processing'
  | 'consent_needed'
  | 'classify'
  | 'review_uncertain'
  | 'study_note'
  | 'too_many';

export type ConsentStatus = boolean | 'unknown';

export interface AssistantInput {
  /** Selected screenshots, in the order the user picked them. */
  selected: Screenshot[];
  consent: Record<number, ConsentStatus>;
  /** True while a classification started by the assistant is running. */
  processing: boolean;
}

export interface Suggestion {
  kind: SuggestionKind;
  /** Stable key for session-only dismissal. */
  key: string;
  source: SuggestionSource;
  text: string;
  /** Accept button label. Null when the suggestion has no action. */
  acceptLabel: string | null;
  /** Screenshot ids the action applies to. */
  targetIds: number[];
}

export const MAX_STUDY_NOTE_SELECTION = 12;

const keyOf = (kind: SuggestionKind, ids: number[]) => `${kind}:${[...ids].sort((a, b) => a - b).join(',')}`;
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;

/** All applicable suggestions, best first. The UI shows only the first one. */
export function deriveSuggestions(input: AssistantInput): Suggestion[] {
  const { selected, consent, processing } = input;

  if (processing) {
    return [
      {
        kind: 'processing',
        key: 'processing',
        source: 'rules',
        text: 'Working on your selection. Results appear on the cards when the server confirms them.',
        acceptLabel: null,
        targetIds: selected.map((s) => s.screenshotId),
      },
    ];
  }

  if (selected.length === 0) {
    return [
      {
        kind: 'idle',
        key: 'idle',
        source: 'rules',
        text: 'Select screenshots with the checkboxes on the cards to see a suggestion.',
        acceptLabel: null,
        targetIds: [],
      },
    ];
  }

  const out: Suggestion[] = [];
  const ghosts = selected.filter((s) => s.state === 'active' && s.analysis === null);
  const ghostIds = ghosts.map((s) => s.screenshotId);

  if (ghosts.length > 0) {
    const notGranted = ghosts.filter((s) => consent[s.screenshotId] !== true);
    if (notGranted.length > 0) {
      const unknown = notGranted.some((s) => consent[s.screenshotId] === undefined || consent[s.screenshotId] === 'unknown');
      out.push({
        kind: 'consent_needed',
        key: keyOf('consent_needed', ghostIds),
        source: 'rules',
        text: unknown
          ? `${plural(notGranted.length, 'selected ghost')} may need your consent before analysis. Review it first, nothing is analyzed without it.`
          : `${plural(notGranted.length, 'selected ghost')} still need${notGranted.length === 1 ? 's' : ''} your consent before analysis.`,
        acceptLabel: 'Review consent',
        targetIds: [notGranted[0].screenshotId],
      });
    } else {
      out.push({
        kind: 'classify',
        key: keyOf('classify', ghostIds),
        source: 'rules',
        text: `Consent is on record for ${plural(ghosts.length, 'selected ghost')}. Revive ${ghosts.length === 1 ? 'it' : 'them'} to get a category and Epitaph?`,
        acceptLabel: `Revive ${plural(ghosts.length, 'ghost')}`,
        targetIds: ghostIds,
      });
    }
  }

  const uncertain = selected.filter((s) => s.analysis?.uncertain);
  if (uncertain.length > 0) {
    out.push({
      kind: 'review_uncertain',
      key: keyOf('review_uncertain', uncertain.map((s) => s.screenshotId)),
      source: 'ai',
      text: `The AI classification marked ${plural(uncertain.length, 'selected screenshot')} as uncertain. You may want to check the category.`,
      acceptLabel: 'Open to review',
      targetIds: [uncertain[0].screenshotId],
    });
  }

  const ids = selected.map((s) => s.screenshotId);
  if (selected.length > MAX_STUDY_NOTE_SELECTION) {
    out.push({
      kind: 'too_many',
      key: keyOf('too_many', ids),
      source: 'rules',
      text: `A Study Note takes at most ${MAX_STUDY_NOTE_SELECTION} screenshots. Deselect ${selected.length - MAX_STUDY_NOTE_SELECTION} to continue.`,
      acceptLabel: null,
      targetIds: ids,
    });
  } else {
    out.push({
      kind: 'study_note',
      key: keyOf('study_note', ids),
      source: 'rules',
      text: `Combine ${plural(selected.length, 'selected screenshot')} into a Study Note? You will review order and consent in Creations before anything is generated.`,
      acceptLabel: 'Set up Study Note',
      targetIds: ids,
    });
  }

  return out;
}

/** Pick the one primary suggestion, skipping dismissed keys and earlier "something else" picks. */
export function pickPrimary(
  all: Suggestion[],
  dismissed: ReadonlySet<string>,
  skip: number,
): Suggestion | null {
  const live = all.filter((s) => !dismissed.has(s.key));
  if (live.length === 0) return null;
  return live[skip] ?? null;
}
