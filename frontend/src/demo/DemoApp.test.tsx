import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DemoApp from './DemoApp';

describe('DemoApp (visual demo mode)', () => {
  const fetchSpy = vi.fn();
  beforeEach(() => {
    vi.stubGlobal('fetch', fetchSpy);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    fetchSpy.mockClear();
  });

  it('renders branding, demo mode indicator and demo data flag', () => {
    render(<DemoApp />);
    expect(screen.getByRole('heading', { level: 1, name: /screenshot graveyard/i })).toBeInTheDocument();
    expect(screen.getByText('Demo AI mode')).toBeInTheDocument();
    expect(screen.getByText('Demo data')).toBeInTheDocument();
  });

  it('never calls fetch while interacting', () => {
    render(<DemoApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    fireEvent.click(screen.getByRole('button', { name: /The Crypt/ }));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('navigates between all four destinations', () => {
    render(<DemoApp />);
    const nav = screen.getByRole('navigation', { name: 'Main' });
    for (const name of [/Creations/, /The Crypt/, /Connections/, /Workspace/]) {
      fireEvent.click(within(nav).getByRole('button', { name }));
      expect(within(nav).getByRole('button', { name })).toHaveAttribute('aria-current', 'page');
    }
  });

  it('toggles gallery selection and shows the selection bar', () => {
    render(<DemoApp />);
    expect(screen.getByText('3 selected')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
    expect(screen.queryByText(/selected$/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: /Select Shoe/ }));
    expect(screen.getByText('1 selected')).toBeInTheDocument();
  });

  it('filters the gallery', () => {
    render(<DemoApp />);
    fireEvent.change(screen.getByLabelText('Screenshot type'), { target: { value: 'chat' } });
    expect(screen.getAllByRole('article')).toHaveLength(1);
  });

  it('moves the Assistant from Prepare to Review and keeps Approve disabled', () => {
    render(<DemoApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(screen.getByText(/disabled in demo mode/i)).toBeInTheDocument();
  });

  it('collapses and expands the Assistant', () => {
    render(<DemoApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Collapse assistant' }));
    const expand = screen.getByRole('button', { name: /Quiet Assistant/ });
    expect(expand).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(expand);
    expect(screen.getByRole('button', { name: 'Collapse assistant' })).toBeInTheDocument();
  });

  it('dismisses a suggestion', () => {
    render(<DemoApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }));
    expect(screen.queryByText(/Looks like lecture screenshots/)).not.toBeInTheDocument();
  });

  it('opens the detail drawer and closes it with Escape', () => {
    render(<DemoApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Open details for Event poster – Tech Mixer' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('buries to and restores from The Crypt', () => {
    render(<DemoApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Select Shoe/ }));
    fireEvent.click(screen.getAllByRole('button', { name: 'Safe Burial' })[0]);
    fireEvent.click(screen.getByRole('button', { name: /The Crypt/ }));
    expect(screen.getAllByRole('button', { name: /^Restore/ })).toHaveLength(3);
  });

  it('shows honest not-connected states in Connections', () => {
    render(<DemoApp />);
    fireEvent.click(screen.getByRole('button', { name: /Connections/ }));
    expect(screen.getAllByText('Not connected').length).toBeGreaterThan(0);
    for (const b of screen.getAllByRole('button', { name: /^Connect /})) expect(b).toBeDisabled();
  });
});
