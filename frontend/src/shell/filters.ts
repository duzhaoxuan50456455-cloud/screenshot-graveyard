export interface FilterState {
  topic: string;
  suggestion: string;
  time: string;
  sort: string;
}

export const DEFAULT_FILTERS: FilterState = { topic: 'all', suggestion: 'any', time: 'all', sort: 'newest' };
