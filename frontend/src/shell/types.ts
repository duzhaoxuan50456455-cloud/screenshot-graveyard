/**
 * View-model types for the presentational shell. Components in `src/shell` and
 * `src/ui` depend ONLY on these shapes, never on the API client. A real-data
 * adapter can map `Screenshot`, `Analysis`, study notes, etc. onto them later
 * (see docs/frontend-integration-map.md).
 */
import type { ArtKind } from '../ui/ShotArt';

export type ViewId = 'workspace' | 'creations' | 'crypt' | 'connections';

export type ShotTopic = 'lecture' | 'event' | 'shopping' | 'chat' | 'other';

export type ActionId = 'make_notes' | 'add_calendar' | 'save_memory' | 'wishlist' | 'checklist';

export interface ShotVM {
  id: string;
  title: string;
  savedLabel: string;
  sizeLabel: string;
  savedDaysAgo: number;
  art: ArtKind;
  topic: ShotTopic;
  /** Object URL for a locally dropped file. Never uploaded in demo mode. */
  imageSrc?: string;
  /** Suggested purpose, shown as a small label on the card. */
  suggestedAction?: ActionId;
  /** True for screenshots added from the drop zone in this browser session. */
  local?: boolean;
}

export interface ArtifactVM {
  id: string;
  title: string;
  kindLabel: string;
  destination: string;
  sourceTitle: string;
  sections: { heading: string; lines: string[] }[];
}

export type ArtifactStatus = 'awaiting_approval' | 'draft_ready' | 'preparing';

export interface CreationVM {
  id: string;
  status: ArtifactStatus;
  artifact: ArtifactVM;
}

export interface ActionDef {
  id: ActionId;
  label: string;
  message: string;
  destination?: { app: string; path: string; connected: boolean };
}

export interface AssistantSuggestionVM {
  shotId: string;
  action: ActionDef;
  artifact: ArtifactVM;
  alternatives: ActionDef[];
}

export type ActivityStatus = 'done' | 'ready' | 'preparing' | 'decision';

export interface ActivityVM {
  id: string;
  status: ActivityStatus;
  label: string;
  detail: string;
  when: string;
}

export interface ConnectionVM {
  id: string;
  name: string;
  description: string;
  status: 'not_connected' | 'coming_later';
}
