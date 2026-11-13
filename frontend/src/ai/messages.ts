import type { ApiFailure, OperationStatus } from '../api/ai';

/** Plain-language text for a failed AI request (never shows raw server detail). */
export function describeRequestFailure(f: ApiFailure): string {
  switch (f.status) {
    case 400:
      return f.code === 'invalid_selection'
        ? f.message
        : 'That request was not valid. Please check it and try again.';
    case 403:
      return 'AI consent is required first. Nothing was processed.';
    case 404:
      return 'That screenshot could not be found in your workspace.';
    case 429:
      return 'You have used up this workspace\u2019s AI request allowance. Nothing was processed.';
    case 503:
      return 'The service is temporarily unavailable. Please try again shortly.';
    case 0:
      return 'Could not reach the server. Check your connection and try again.';
    default:
      return 'Something went wrong. Please try again.';
  }
}

/** Plain-language text for a finished operation. */
export function describeOperationOutcome(
  status: OperationStatus,
  errorDetail: string | null,
): string {
  if (status === 'successful') return 'Finished.';
  if (status === 'uncertain') {
    return 'The AI was not sure about this one, so no result was saved. Your screenshot is unchanged.';
  }
  if (status === 'pending') return 'Working\u2026';
  switch (errorDetail) {
    case 'AI processing consent required':
      return 'AI consent is missing or was withdrawn before the work started. Nothing was processed.';
    case 'AI operation quota exceeded':
      return 'This workspace\u2019s AI request allowance is used up.';
    case 'AI output failed validation':
    case 'AI output references invalid':
      return 'The AI returned a result we could not trust, so nothing was saved.';
    case 'Processing interrupted; please retry':
      return 'The work was interrupted before it finished. Please try again.';
    case 'Not found':
      return 'A selected screenshot could not be found.';
    case 'AI input exceeds configured limits':
      return 'The selection is too large for one AI request.';
    case 'AI analysis failed':
    case 'AI processing unavailable':
      return 'The AI analysis did not complete (this includes timeouts). Nothing was saved and your screenshot is unchanged.';
    default:
      return 'The work failed. Nothing was saved and your screenshot is unchanged.';
  }
}
