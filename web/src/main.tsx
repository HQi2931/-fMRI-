import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { Router } from "./routing";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Router>
      <App />
    </Router>
  </StrictMode>,
);
