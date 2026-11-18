import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { initMsal } from './auth/msal';
import './index.css';

// Finish a Microsoft sign-in redirect (if any) before the first render.
void initMsal().then(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});
