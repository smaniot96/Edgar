import { useLocation } from "react-router-dom";

import { ButtonLink } from "../components/ui/Button";
import { Page } from "../components/ui/Page";

export default function NotFound() {
  const { pathname } = useLocation();
  return (
    <Page width="sm" className="py-16 text-center">
      <p className="font-display text-display-lg text-ember-400">404</p>
      <h1 className="font-display text-display-md">This path leads nowhere</h1>
      <p className="text-sm text-ink-muted">
        There's no page at <code className="break-all font-mono text-ink">{pathname}</code>. The
        map may be out of date.
      </p>
      <div className="flex justify-center gap-2">
        <ButtonLink to="/campaigns">Go to Campaigns</ButtonLink>
        <ButtonLink to="/" variant="secondary">
          Home
        </ButtonLink>
      </div>
    </Page>
  );
}
