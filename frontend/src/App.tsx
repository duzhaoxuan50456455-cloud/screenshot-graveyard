import './App.css';
import HealthStatus from './HealthStatus';

function App() {
  return (
    <main className="app">
      <h1>Screenshot Graveyard</h1>
      <p className="tagline">
        Where screenshots finish their unfinished business.
      </p>
      <HealthStatus />
    </main>
  );
}

export default App;
