import { useAuthorizedObjectUrl } from "../../api/resources";

/** An API image shown from an authorized object URL (EDG-160). Renders nothing while it loads. */
export function AuthorizedImage({
  url,
  alt,
  className,
  loading,
}: {
  url: string;
  alt: string;
  className?: string;
  loading?: "lazy" | "eager";
}) {
  const { objectUrl, error } = useAuthorizedObjectUrl(url);
  if (error) return <p className="p-3 text-sm text-ink-muted" role="status">This image is unavailable.</p>;
  if (!objectUrl) return null;
  return <img src={objectUrl} alt={alt} className={className} loading={loading} />;
}
