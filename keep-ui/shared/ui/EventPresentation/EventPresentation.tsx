import type { EventPresentation as Presentation } from "@/shared/lib/event-presentation";

export function EventPresentation({ presentation, showDescription = true }: {
  presentation?: Presentation | null;
  showDescription?: boolean;
}) {
  if (!presentation) return null;
  return (
    <section aria-label="Normalized object" className="mt-3 space-y-2 rounded border p-3 text-sm">
      <h3 className="font-medium" style={presentation.severity_color ? { color: presentation.severity_color } : undefined}>
        {presentation.title}
      </h3>
      {showDescription && <p className="whitespace-pre-wrap">{presentation.description}</p>}
      <dl className="grid grid-cols-2 gap-2">
        {presentation.fields.map((field) => (
          <div key={field.path}>
            <dt className="text-gray-500">{field.label}</dt>
            <dd className="whitespace-pre-wrap break-words" title={field.source || undefined}>
              {String(field.value ?? "unknown")}{!field.known && <span className="text-gray-500"> (incomplete)</span>}
            </dd>
          </div>
        ))}
      </dl>
      {presentation.links.map((link) => /^https?:\/\//i.test(link.url) && (
        <a key={link.url} className="mr-3 text-blue-600 underline" href={link.url} target="_blank" rel="noopener noreferrer">
          {link.label}
        </a>
      ))}
      {presentation.missing_fields.length > 0 && (
        <p className="text-amber-700">Incomplete data: {presentation.missing_fields.join(", ")}</p>
      )}
    </section>
  );
}
