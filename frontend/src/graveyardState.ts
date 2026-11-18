import type { Screenshot } from './api/client';

/** Which view a card belongs to. Derived ONLY from real state (Req 10.1, 10.2, 10.6). */
export type CardKind = 'ghost' | 'revived' | 'crypt';

/**
 * Partition screenshots by what is actually true of them:
 *   crypt   : state === 'archived' (exactly the archived set, analysed or not)
 *   ghost   : active and NOT analysed yet (pending)
 *   revived : active and analysed
 * Every screenshot lands in exactly one list.
 */
export function partitionScreenshots(all: Screenshot[]) {
  return {
    ghosts: all.filter((s) => s.state === 'active' && s.analysis === null),
    revived: all.filter((s) => s.state === 'active' && s.analysis !== null),
    crypt: all.filter((s) => s.state === 'archived'),
  };
}
