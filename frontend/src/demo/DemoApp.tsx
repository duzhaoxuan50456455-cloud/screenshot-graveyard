import '@fontsource-variable/inter';
import '@fontsource/cormorant-garamond/600.css';
import '@fontsource/cormorant-garamond/700.css';
import '../ui/theme.css';
import '../ui/components.css';
import '../shell/shell.css';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AssistantPanel, ArtifactPreview, type AssistantStage } from '../shell/Assistant';
import { Drawer } from '../shell/DetailDrawer';
import { AppHeader, NavTabs } from '../shell/Header';
import { ConnectionsView, CreationsView, CryptView } from '../shell/Views';
import { DEFAULT_FILTERS, type FilterState } from '../shell/filters';
import {
  FilterBar,
  ScreenshotGallery,
  SelectionBar,
  UploadDropZone,
  WorkspaceHeading,
} from '../shell/Workspace';
import type { ActionId, AssistantSuggestionVM, ShotVM, ViewId } from '../shell/types';
import { Badge, Button } from '../ui/primitives';
import { ShotArt } from '../ui/ShotArt';
import {
  ACTIONS,
  ACTION_ORDER,
  CRYPT_NOTE,
  DEMO_ACTIVITY,
  DEMO_ARCHIVED,
  DEMO_CONNECTIONS,
  DEMO_CREATIONS,
  DEMO_SHOTS,
  buildArtifact,
} from './fixtures';

type ArchivedShot = ShotVM & { origLabel?: string };
type Drawered = { kind: 'shot' | 'creation'; id: string } | null;

function formatBytes(n: number) {
  return n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * Visual demo shell. Frontend-only: it imports no API client, performs no
 * network requests, and writes nowhere. All data comes from `./fixtures` or
 * from files the user drops (kept as local object URLs).
 */
export default function DemoApp() {
  const [view, setView] = useState<ViewId>('workspace');
  const [shots, setShots] = useState<ShotVM[]>(DEMO_SHOTS);
  const [archived, setArchived] = useState<ArchivedShot[]>(DEMO_ARCHIVED);
  const [selectedIds, setSelectedIds] = useState<string[]>(['d1', 'd2', 'd4']);
  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);
  const [dismissed, setDismissed] = useState<string[]>([]);
  const [override, setOverride] = useState<Record<string, ActionId>>({});
  const [collapsed, setCollapsed] = useState(
    () => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 899px)').matches,
  );
  const [stageState, setStageState] = useState<{ key: string; stage: AssistantStage }>({ key: '', stage: 'prepare' });
  const [notice, setNotice] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<Drawered>(null);
  const [opener, setOpener] = useState<HTMLElement | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const urlsRef = useRef<string[]>([]);
  const counter = useRef(0);

  useEffect(() => {
    const urls = urlsRef.current;
    return () => urls.forEach((u) => URL.revokeObjectURL(u));
  }, []);

  /* ----- derived gallery ----- */
  const visible = useMemo(() => {
    const list = shots.filter((s) => {
      if (filters.topic !== 'all' && s.topic !== filters.topic) return false;
      const eff = override[s.id] ?? s.suggestedAction;
      if (filters.suggestion === 'none' ? eff : filters.suggestion !== 'any' && eff !== filters.suggestion) return false;
      if (filters.time !== 'all' && s.savedDaysAgo >= Number(filters.time)) return false;
      return true;
    });
    const sorted = [...list];
    if (filters.sort === 'oldest') sorted.sort((a, b) => b.savedDaysAgo - a.savedDaysAgo);
    else if (filters.sort === 'name') sorted.sort((a, b) => a.title.localeCompare(b.title));
    else sorted.sort((a, b) => a.savedDaysAgo - b.savedDaysAgo);
    return sorted.map((s) => (override[s.id] ? { ...s, suggestedAction: override[s.id] } : s));
  }, [shots, filters, override]);

  const selected = selectedIds.filter((id) => shots.some((s) => s.id === id));

  /* ----- assistant: one suggestion at a time ----- */
  const suggestion: AssistantSuggestionVM | null = useMemo(() => {
    const focus =
      shots.find((s) => s.id === selected[0]) ??
      visible.find((s) => (override[s.id] ?? s.suggestedAction) && !dismissed.includes(s.id));
    if (!focus) return null;
    const actionId = override[focus.id] ?? focus.suggestedAction;
    if (!actionId || (dismissed.includes(focus.id) && !override[focus.id])) return null;
    return {
      shotId: focus.id,
      action: ACTIONS[actionId],
      artifact: buildArtifact(focus, actionId),
      alternatives: ACTION_ORDER.filter((a) => a !== actionId).map((a) => ACTIONS[a]),
    };
  }, [shots, selected, visible, override, dismissed]);

  const stageKey = suggestion ? `${suggestion.shotId}:${suggestion.action.id}` : '';
  const stage: AssistantStage = stageState.key === stageKey ? stageState.stage : 'prepare';

  const flash = useCallback((msg: string) => {
    setToast(msg);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3500);
    return () => clearTimeout(t);
  }, [toast]);

  /* ----- handlers ----- */
  const toggle = (id: string) => setSelectedIds((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]));
  const chooseAction = (action: ActionId) => {
    if (!suggestion) return;
    setOverride((o) => ({ ...o, [suggestion.shotId]: action }));
    setDismissed((d) => d.filter((x) => x !== suggestion.shotId));
    setNotice(null);
  };
  const onExtraChoice = (id: string) => {
    if (id === 'keep') {
      setNotice('Okay. Left as is. Nothing was changed.');
      return;
    }
    chooseAction(id as ActionId);
  };
  const dismiss = () => {
    if (!suggestion) return;
    setDismissed((d) => [...d, suggestion.shotId]);
    setOverride((o) => {
      const { [suggestion.shotId]: _drop, ...rest } = o;
      void _drop;
      return rest;
    });
    setSelectedIds((c) => c.filter((x) => x !== suggestion.shotId));
    setNotice(null);
  };
  const bury = (ids: string[]) => {
    const moving = shots.filter((s) => ids.includes(s.id));
    if (!moving.length) return;
    setShots((c) => c.filter((s) => !ids.includes(s.id)));
    setArchived((c) => [...moving.map((s) => ({ ...s, origLabel: s.savedLabel, savedLabel: 'Buried just now' })), ...c]);
    setSelectedIds((c) => c.filter((x) => !ids.includes(x)));
    flash(`${moving.length} moved to The Crypt (demo only; no files were deleted).`);
  };
  const restore = (id: string) => {
    const item = archived.find((s) => s.id === id);
    if (!item) return;
    const { origLabel, ...rest } = item;
    setArchived((c) => c.filter((s) => s.id !== id));
    setShots((c) => [{ ...rest, savedLabel: origLabel ?? 'Restored just now' }, ...c]);
    flash(`Restored “${item.title}” to the Workspace.`);
  };
  const addFiles = (files: File[]) => {
    const added: ShotVM[] = files.map((f) => {
      const url = URL.createObjectURL(f);
      urlsRef.current.push(url);
      counter.current += 1;
      return {
        id: `local-${counter.current}`,
        title: f.name.replace(/\.[^.]+$/, ''),
        savedLabel: 'Added just now',
        sizeLabel: formatBytes(f.size),
        savedDaysAgo: 0,
        art: 'image',
        topic: 'other',
        imageSrc: url,
        local: true,
      };
    });
    setShots((c) => [...added, ...c]);
    flash(`${added.length} added as local previews. Nothing was uploaded.`);
  };
  const openShot = (id: string, el: HTMLElement) => {
    setOpener(el);
    setDrawer({ kind: 'shot', id });
  };
  const prepareSelection = () => {
    setCollapsed(false);
    setStageState({ key: stageKey, stage: 'prepare' });
    setNotice(`Sample draft shown for ${selected.length} selected. Demo only, nothing is generated by AI.`);
  };
  const selectionChoose = (id: string) => {
    if (id === 'bury') return bury(selected);
    setOverride((o) => ({ ...o, ...Object.fromEntries(selected.map((s) => [s, id as ActionId])) }));
    setCollapsed(false);
    setNotice(`Suggestion for ${selected.length} selected changed to “${ACTIONS[id as ActionId].label}”.`);
  };

  // Escape clears the selection (menus and drawer stop propagation first).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !drawer && selected.length > 0 && !e.defaultPrevented) setSelectedIds([]);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [drawer, selected.length]);

  const drawerShot = drawer?.kind === 'shot' ? [...shots, ...archived].find((s) => s.id === drawer.id) : undefined;
  const drawerCreation = drawer?.kind === 'creation' ? DEMO_CREATIONS.find((c) => c.id === drawer.id) : undefined;
  const closeDrawer = () => setDrawer(null);

  const suggestionOptions = [
    { value: 'any', label: 'Any suggestion' },
    { value: 'none', label: 'No suggestion' },
    ...ACTION_ORDER.slice(0, 4).map((a) => ({ value: a, label: ACTIONS[a].label })),
  ];

  const extraChoices = suggestion
    ? [
        ...suggestion.alternatives.slice(2).map((a) => ({ id: a.id, label: a.label })),
        { id: 'keep', label: 'Just keep it as is' },
      ]
    : [];

  return (
    <div className="sg" data-testid="demo-app">
      <a className="sg-skip" href="#sg-main">Skip to content</a>
      <AppHeader modeLabel="Demo AI mode" />
      <div className="sg-layout" data-assistant={collapsed ? 'collapsed' : 'open'}>
        <div className="sg-col">
          <NavTabs
            active={view}
            onNavigate={setView}
            cryptCount={archived.length}
            pendingCount={DEMO_CREATIONS.filter((c) => c.status === 'awaiting_approval').length}
          />
          <main id="sg-main" className="sg-panel" tabIndex={-1}>
            {view === 'workspace' && (
              <>
                <WorkspaceHeading demo />
                <UploadDropZone
                  onFiles={addFiles}
                  hint="Demo mode: dropped images stay in this browser tab as local previews and are never uploaded."
                />
                <FilterBar value={filters} onChange={setFilters} suggestionOptions={suggestionOptions} />
                <ScreenshotGallery shots={visible} selectedIds={selected} actions={ACTIONS} onToggle={toggle} onOpen={openShot} />
                {selected.length > 0 && (
                  <SelectionBar
                    count={selected.length}
                    onPrepare={prepareSelection}
                    onChoose={selectionChoose}
                    onClear={() => setSelectedIds([])}
                    choices={[
                      ...ACTION_ORDER.map((a) => ({ id: a, label: ACTIONS[a].label })),
                      { id: 'bury', label: 'Send to The Crypt' },
                    ]}
                  />
                )}
              </>
            )}
            {view === 'creations' && (
              <CreationsView
                items={DEMO_CREATIONS}
                onPreview={(id, el) => {
                  setOpener(el);
                  setDrawer({ kind: 'creation', id });
                }}
              />
            )}
            {view === 'crypt' && <CryptView shots={archived} onRestore={restore} note={CRYPT_NOTE} />}
            {view === 'connections' && <ConnectionsView items={DEMO_CONNECTIONS} />}
          </main>
        </div>
        <div className="sg-side">
          <AssistantPanel
            collapsed={collapsed}
            onToggleCollapsed={() => setCollapsed((c) => !c)}
            suggestion={suggestion}
            stage={stage}
            onReview={() => setStageState({ key: stageKey, stage: 'review' })}
            onBack={() => setStageState({ key: stageKey, stage: 'prepare' })}
            onDismiss={dismiss}
            onChooseAction={chooseAction}
            extraChoices={extraChoices}
            onExtraChoice={onExtraChoice}
            notice={notice}
            activity={DEMO_ACTIVITY}
            onSeeAll={() => setView('creations')}
            onSafeBurial={() => bury(selected)}
            onResurrection={() => archived[0] && restore(archived[0].id)}
            canBury={selected.length > 0}
            canResurrect={archived.length > 0}
            archiveNote={CRYPT_NOTE}
          />
        </div>
      </div>

      {drawerShot && (
        <Drawer
          title={drawerShot.title}
          opener={opener}
          onClose={closeDrawer}
          footer={
            shots.some((s) => s.id === drawerShot.id) ? (
              <Button
                icon="box"
                onClick={() => {
                  bury([drawerShot.id]);
                  closeDrawer();
                }}
              >
                Safe Burial
              </Button>
            ) : (
              <Button
                icon="restore"
                onClick={() => {
                  restore(drawerShot.id);
                  closeDrawer();
                }}
              >
                Restore
              </Button>
            )
          }
        >
          <div className="sg-drawer__art">
            <ShotArt kind={drawerShot.art} src={drawerShot.imageSrc} alt={`Preview of ${drawerShot.title}`} />
          </div>
          <dl className="sg-facts">
            <dt>Saved</dt>
            <dd>{drawerShot.savedLabel}</dd>
            <dt>Size</dt>
            <dd>{drawerShot.sizeLabel}</dd>
            <dt>Suggested</dt>
            <dd>{(override[drawerShot.id] ?? drawerShot.suggestedAction) ? ACTIONS[(override[drawerShot.id] ?? drawerShot.suggestedAction)!].label : 'None'}</dd>
            <dt>Source</dt>
            <dd>
              <Badge tone="muted">{drawerShot.local ? 'Local preview' : 'Demo data'}</Badge>
            </dd>
          </dl>
          <p className="sg-note">Sample details only. No analysis has been run and nothing leaves this page.</p>
        </Drawer>
      )}
      {drawerCreation && (
        <Drawer
          title={drawerCreation.artifact.title}
          opener={opener}
          onClose={closeDrawer}
          footer={
            <Button variant="primary" disabled aria-describedby="creation-why">
              Approve
            </Button>
          }
        >
          <ArtifactPreview artifact={drawerCreation.artifact} />
          <p className="sg-note" id="creation-why">
            Approval is disabled in demo mode. {drawerCreation.artifact.destination} is not connected and nothing is written.
          </p>
        </Drawer>
      )}

      <div className="sg-toast" role="status" aria-live="polite">
        {toast}
      </div>
    </div>
  );
}
