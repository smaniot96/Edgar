import { useState, type ReactNode } from "react";

import { hasApiToken } from "../../api/auth";
import { downloadAuthorized } from "../../api/resources";

/**
 * A link to an API resource. Local mode (no token): a plain `<a href>`, as before. Token mode: a
 * button that downloads through the authorized client, because the browser would not send the token.
 * `plainDownload` keeps the `download` attribute on the plain link where it was already present.
 */
export function ApiDownloadLink({
  url,
  filename,
  className,
  plainDownload = false,
  children,
}: {
  url: string;
  filename: string;
  className?: string;
  plainDownload?: boolean;
  children: ReactNode;
}) {
  const [failed, setFailed] = useState(false);
  if (!hasApiToken()) {
    return (
      <a className={className} href={url} download={plainDownload ? filename : undefined}>
        {children}
      </a>
    );
  }
  return (
    <div>
      <button
        type="button"
        className={className}
        onClick={() => {
          setFailed(false);
          void downloadAuthorized(url, filename).catch(() => setFailed(true));
        }}
      >
        {children}
      </button>
      {failed ? <p role="status" className="px-3 pb-2 text-xs text-danger">The file could not be downloaded. Try again.</p> : null}
    </div>
  );
}
