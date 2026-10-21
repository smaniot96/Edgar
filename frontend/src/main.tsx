import { StrictMode, type ComponentType } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";

import "./index.css";
import App from "./App.tsx";
import { ModeProvider } from "./dev/ModeProvider.tsx";
import Home from "./routes/Home.tsx";
import Landing from "./routes/Landing.tsx";
import NotFound from "./routes/NotFound.tsx";
import RouteError from "./routes/RouteError.tsx";

// Route-level code splitting: the play screen pulls in react-markdown and the combat HUD, so
// only load each page's chunk when it is first visited.
const lazyPage = (load: () => Promise<{ default: ComponentType }>) => async () => ({
  Component: (await load()).default,
});

const router = createBrowserRouter(
  [
    {
      // Root boundary: catches anything the shell itself throws.
      errorElement: <RouteError fullScreen />,
      children: [
        // Full-bleed splash, rendered outside the standard app nav chrome.
        { path: "/", element: <Landing /> },
        {
          element: <App />,
          children: [
            {
              // Page-level boundary: errors render inside the shell, nav stays usable.
              errorElement: <RouteError />,
              children: [
                { path: "campaigns", lazy: lazyPage(() => import("./routes/Campaigns.tsx")) },
                { path: "adventures", element: <Home /> },
                { path: "adventures/:slug", lazy: lazyPage(() => import("./routes/Adventure.tsx")) },
                { path: "campaigns/:id/setup", lazy: lazyPage(() => import("./routes/CampaignSetup.tsx")) },
                { path: "campaigns/:id", lazy: lazyPage(() => import("./routes/Campaign.tsx")) },
                { path: "characters", lazy: lazyPage(() => import("./routes/Characters.tsx")) },
                { path: "settings", lazy: lazyPage(() => import("./routes/Settings.tsx")) },
                { path: "join", lazy: lazyPage(() => import("./routes/Join.tsx")) },
                { path: "play/:sessionId/table", lazy: lazyPage(() => import("./routes/Lobby.tsx")) },
                { path: "play/:sessionId", lazy: lazyPage(() => import("./routes/Play.tsx")) },
                // The old /new wizard was removed; campaigns are created from /campaigns.
                { path: "new", element: <Navigate to="/adventures" replace /> },
                { path: "*", element: <NotFound /> },
              ],
            },
          ],
        },
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
