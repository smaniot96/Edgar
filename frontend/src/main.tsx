import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";

import "./index.css";
import App from "./App.tsx";
import { ModeProvider } from "./dev/ModeContext.tsx";
import Campaign from "./routes/Campaign.tsx";
import Characters from "./routes/Characters.tsx";
import Home from "./routes/Home.tsx";
import Landing from "./routes/Landing.tsx";
import NewAdventure from "./routes/NewAdventure.tsx";
import Play from "./routes/Play.tsx";
import Settings from "./routes/Settings.tsx";

const router = createBrowserRouter(
  [
    // Full-bleed splash, rendered outside the standard app nav chrome.
    { path: "/", element: <Landing /> },
    {
      element: <App />,
      children: [
        { path: "campaigns", element: <Home /> },
        { path: "new", element: <NewAdventure /> },
        { path: "campaigns/:id", element: <Campaign /> },
        { path: "characters", element: <Characters /> },
        { path: "settings", element: <Settings /> },
        { path: "play/:sessionId", element: <Play /> },
      ],
    },
  ],
  { basename: "/ui" },
);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ModeProvider>
      <RouterProvider router={router} />
    </ModeProvider>
  </StrictMode>,
);
