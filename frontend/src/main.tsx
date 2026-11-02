import React from 'react';
import { createRoot } from 'react-dom/client';
import { WorkbenchProvider } from './store';
import App from './App';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <WorkbenchProvider>
      <App />
    </WorkbenchProvider>
  </React.StrictMode>,
);
