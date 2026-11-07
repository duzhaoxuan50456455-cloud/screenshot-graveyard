import './App.css';
import Graveyard from './Graveyard';
import HealthStatus from './HealthStatus';

function App() {
  return (
    <main className="app">
      <header className="app__header">
        <h1>Screenshot Graveyard</h1>
        <p className="tagline">
          Where screenshots finish their unfinished business.
        </p>
        <HealthStatus />
      </header>
      <Graveyard />
    </main>
  );
}

export default App;
