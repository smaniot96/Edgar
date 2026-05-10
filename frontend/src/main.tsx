import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";

import "./index.css";
import App from "./App.tsx";
import Campaign from "./routes/Campaign.tsx";
import Home from "./routes/Home.tsx";
import NewAdventure from "./routes/NewAdventure.tsx";
import Play from "./routes/Play.tsx";
import Settings from "./routes/Settings.tsx";

const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <App />,
      children: [
        { index: true, element: <Home /> },
        { path: "new", element: <NewAdventure /> },
        { path: "campaigns/:id", element: <Campaign /> },
        { path: "settings", element: <Settings /> },
        { path: "play/:sessionId", element: <Play /> },
      ],
    },
  ],
  { basename: "/ui" },
);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
