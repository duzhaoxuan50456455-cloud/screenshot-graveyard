/**
 * DEMO FIXTURES. Everything in this file is invented sample data used only by
 * the visual demo mode. It is never sent to an API and no AI produced it.
 */
import type {
  ActionDef,
  ActionId,
  ActivityVM,
  ArtifactVM,
  ConnectionVM,
  CreationVM,
  ShotVM,
} from '../shell/types';

export const DEMO_SHOTS: ShotVM[] = [
  { id: 'd1', title: 'Lecture notes – Math 33A', savedLabel: 'Saved today', sizeLabel: '1.2 MB', savedDaysAgo: 0, art: 'lecture', topic: 'lecture', suggestedAction: 'make_notes' },
  { id: 'd2', title: 'Event poster – Tech Mixer', savedLabel: 'Saved today', sizeLabel: '0.9 MB', savedDaysAgo: 0, art: 'poster', topic: 'event', suggestedAction: 'add_calendar' },
  { id: 'd3', title: 'Shoe – New Balance 574', savedLabel: 'Saved yesterday', sizeLabel: '1.1 MB', savedDaysAgo: 1, art: 'shoe', topic: 'shopping', suggestedAction: 'wishlist' },
  { id: 'd4', title: 'Chat – Trip planning', savedLabel: 'Saved 2 days ago', sizeLabel: '432 KB', savedDaysAgo: 2, art: 'chat', topic: 'chat', suggestedAction: 'save_memory' },
];

export const DEMO_ARCHIVED: ShotVM[] = [
  { id: 'a1', title: 'Chat – Group dinner', savedLabel: 'Buried 3 weeks ago', sizeLabel: '380 KB', savedDaysAgo: 21, art: 'chat', topic: 'chat' },
  { id: 'a2', title: 'Lecture notes – Bio 101', savedLabel: 'Buried last month', sizeLabel: '1.0 MB', savedDaysAgo: 35, art: 'lecture', topic: 'lecture' },
];

export const ACTIONS: Record<ActionId, ActionDef> = {
  make_notes: {
    id: 'make_notes',
    label: 'Make Notes',
    message: 'Looks like lecture screenshots. I can organize them into notes and prepare a OneNote draft for review.',
    destination: { app: 'OneNote', path: 'Math 33A > Lectures', connected: false },
  },
  add_calendar: {
    id: 'add_calendar',
    label: 'Add to Calendar',
    message: 'Looks like an event poster. I can prepare a calendar event for you to review.',
    destination: { app: 'Calendar', path: 'Personal > Events', connected: false },
  },
  wishlist: {
    id: 'wishlist',
    label: 'Wishlist',
    message: 'Looks like something you may want later. I can add it to a wishlist draft.',
  },
  save_memory: {
    id: 'save_memory',
    label: 'Save as Memory',
    message: 'Looks like a conversation worth keeping. I can save it as a memory card.',
  },
  checklist: {
    id: 'checklist',
    label: 'Make a checklist',
    message: 'I can turn what is on this screenshot into a checklist draft.',
  },
};

export const ACTION_ORDER: ActionId[] = ['make_notes', 'add_calendar', 'save_memory', 'wishlist', 'checklist'];

/** Sample draft content. In the real app this would come from the backend. */
export function buildArtifact(shot: ShotVM, action: ActionId): ArtifactVM {
  const def = ACTIONS[action];
  const base = { id: `${shot.id}:${action}`, sourceTitle: shot.title, destination: def.destination?.app ?? 'Local only' };
  if (action === 'make_notes' && shot.topic === 'lecture') {
    return {
      ...base,
      title: 'Math 33A — Lecture 5',
      kindLabel: 'Lecture Notes (Draft)',
      sections: [
        { heading: 'Key Topics', lines: ['Eigenvalues & Eigenvectors', 'Characteristic equation: det(A − λI) = 0', 'Diagonalizable: n linearly independent eigenvectors'] },
        { heading: 'Summary', lines: [] },
      ],
    };
  }
  if (action === 'add_calendar' && shot.topic === 'event') {
    return {
      ...base,
      title: 'Fall Tech Mixer',
      kindLabel: 'Calendar Event (Draft)',
      sections: [{ heading: 'Details', lines: ['Thu, Nov 14', '6:00 – 8:30 PM', 'Student Union, Ballroom A'] }],
    };
  }
  const kindLabel: Record<ActionId, string> = {
    make_notes: 'Notes (Draft)',
    add_calendar: 'Calendar Event (Draft)',
    wishlist: 'Wishlist Item (Draft)',
    save_memory: 'Memory Card (Draft)',
    checklist: 'Checklist (Draft)',
  };
  return {
    ...base,
    title: shot.title.replace(/^[^–]+–\s*/, ''),
    kindLabel: kindLabel[action],
    sections: [{ heading: 'Sample content', lines: ['Placeholder text for the demo.', `Source: ${shot.title}`] }],
  };
}

export const DEMO_CREATIONS: CreationVM[] = [
  {
    id: 'c1',
    status: 'awaiting_approval',
    artifact: buildArtifact(DEMO_SHOTS[0], 'make_notes'),
  },
  {
    id: 'c2',
    status: 'draft_ready',
    artifact: buildArtifact(DEMO_SHOTS[1], 'add_calendar'),
  },
  {
    id: 'c3',
    status: 'preparing',
    artifact: { ...buildArtifact(DEMO_SHOTS[2], 'wishlist'), title: 'New Balance 574', kindLabel: 'Wishlist Item (Draft)' },
  },
  {
    id: 'c4',
    status: 'draft_ready',
    artifact: { ...buildArtifact(DEMO_SHOTS[3], 'save_memory'), title: 'Trip planning', kindLabel: 'Memory Card (Draft)', sections: [{ heading: 'Moment', lines: ['“We should make it an annual thing!”'] }] },
  },
];

export const DEMO_ACTIVITY: ActivityVM[] = [
  { id: 'r1', status: 'done', label: 'Added after approval', detail: 'Lecture notes (Math 33A)', when: '3 min ago' },
  { id: 'r2', status: 'ready', label: 'Ready for review', detail: 'Event – Tech Mixer', when: '8 min ago' },
  { id: 'r3', status: 'preparing', label: 'Preparing draft…', detail: 'Lecture notes (3 items)', when: 'Just now' },
  { id: 'r4', status: 'decision', label: 'Needs your decision', detail: 'New Balance 574 (wishlist?)', when: 'Yesterday' },
];

export const DEMO_CONNECTIONS: ConnectionVM[] = [
  { id: 'onenote', name: 'OneNote', description: 'Send approved lecture notes to a notebook section.', status: 'not_connected' },
  { id: 'gcal', name: 'Google Calendar', description: 'Add approved events from posters and invitations.', status: 'not_connected' },
  { id: 'phone', name: 'Phone gallery sync', description: 'Pull new screenshots from your phone automatically.', status: 'coming_later' },
  { id: 'notion', name: 'Notion', description: 'Save memories and wishlists to a database.', status: 'coming_later' },
];

export const CRYPT_NOTE = 'Archiving does not delete the original from the phone gallery.';
