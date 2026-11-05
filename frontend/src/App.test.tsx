import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import App from './App';

describe('App', () => {
  it('renders the app title', () => {
    render(<App />);
    expect(
      screen.getByRole('heading', { name: /screenshot graveyard/i }),
    ).toBeInTheDocument();
  });

  it('renders visible tagline content', () => {
    render(<App />);
    expect(
      screen.getByText(/finish their unfinished business/i),
    ).toBeInTheDocument();
  });
});
